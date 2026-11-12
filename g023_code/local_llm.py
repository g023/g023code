"""
Optional local model (llama.cpp ``llama-server``) for free helper work.

DeepSeek stays the brain: every decision, edit and answer is made by the paid
model. The local model only does bounded side jobs whose output is checked or
cheap to ignore:

* salience rerank: choose which lines of a vaulted tool output the paid model
  sees (salience.py; deterministic picks stay in the excerpt either way);
* LocalAsk: bulk reading of files the paid model would otherwise pay to read,
  returning an answer plus verbatim cited lines that are checked against the
  file (cite-or-drop);
* AnalyzeImage: a local vision model answers first when configured.

Everything fails open: no binary, no model, a busy GPU, a timeout or a bad JSON
reply means "no local help" and the old deterministic path runs.

One server is shared by every g023 process on the machine (state file in the
global cache), started detached on first use and stopped by ``/local unload``,
by idle sleep (``--sleep-idle-seconds``), or when the GPU is wanted elsewhere:
another CUDA process appears (GPU guard) or a Bash command looks like GPU work
(the server is released for the rest of the turn).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .config import get_global_cache_dir, settings

HEALTH_WAIT_S = 90.0
IDLE_SLEEP_S = 300
GUARD_TTL_S = 5.0
GUARD_MIN_MIB = 1024
RERANK_TIMEOUT_S = 4.0

_GPU_WORK = re.compile(
    r"(?:^|[\s;&|(/'\"])("
    r"nvidia-smi|nvcc|nvprof|nsys|ncu|cuda-gdb|torchrun|deepspeed|vllm|ollama|tensorrt\w*|trtexec|"
    r"llama-(?:server|cli|bench|perplexity|quantize|finetune)|hipcc|rocm-smi|rocminfo|"
    r"accelerate\s+launch|CUDA_VISIBLE_DEVICES=|HIP_VISIBLE_DEVICES="
    r")"
    r"|\b(?:import|from)\s+(?:torch|tensorflow|jax|cupy|triton|pycuda|numba\.cuda)\b"
    r"|\.cuda\(\)|device=['\"]cuda|--gpus?\b|\b(?:train|finetune|fine_tune)\w*\.py\b",
    re.I,
)

_turn_yield = False
_guard_cache: tuple[float, bool] = (0.0, False)
_spawn_task: Optional[asyncio.Task] = None
_proc: Optional[subprocess.Popen] = None  # the server this process started (reaped on release)
_last_error = ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p or ""))).resolve()


def find_server_binary(llama_dir: Optional[str] = None) -> Optional[Path]:
    """llama-server under the llama.cpp folder (build/bin, bin, root), else on PATH."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    base = _expand(llama_dir if llama_dir is not None else settings.local_llama_dir)
    for rel in ("build/bin", "bin", "build/bin/Release", "."):
        cand = base / rel / exe
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(exe)
    return Path(found) if found else None


def resolve_model(name: str, models_dir: Optional[str] = None) -> Optional[Path]:
    """A .gguf by absolute path, by file name in the models folder, or by unique prefix."""
    if not name:
        return None
    p = _expand(name)
    if p.is_file():
        return p
    base = _expand(models_dir if models_dir is not None else settings.local_models_dir)
    cand = base / name
    if cand.is_file():
        return cand
    if not base.is_dir():
        return None
    low = name.lower().removesuffix(".gguf")
    hits = sorted(
        f for f in base.rglob("*.gguf")
        if f.name.lower().startswith(low) and not f.name.lower().startswith("mmproj")
    )
    return hits[0] if len(hits) == 1 else None


def find_mmproj(model: Path) -> Optional[Path]:
    """The vision projector next to a VL model: mmproj-<stem>.gguf (Q8 before F16)."""
    stem = model.stem
    base = re.sub(r"[-_.](Q\d\w*|F16|BF16|F32)$", "", stem, flags=re.I)
    cands = sorted(model.parent.glob(f"mmproj-{base}*.gguf")) + sorted(model.parent.glob(f"mmproj*{base}*.gguf"))
    cands = list(dict.fromkeys(cands)) or _family_mmproj(model.parent, base)
    if not cands:
        return None
    for tag in ("q8", "f16", "bf16"):
        for c in cands:
            if tag in c.name.lower():
                return c
    return cands[0]


def _family_mmproj(folder: Path, base: str) -> list[Path]:
    """Projectors named <family>-mmproj-<quant>.gguf (e.g. Qwen3.5-4B-mmproj-BF16) whose family
    prefixes the model's base name at a separator; the longest family wins."""
    low = base.lower()
    best: dict[int, list[Path]] = {}
    for c in folder.glob("*.gguf"):
        if "mmproj" not in c.name.lower():
            continue
        core = re.sub(r"mmproj", "", c.stem, flags=re.I)
        core = re.sub(r"[-_.](Q\d\w*|F16|BF16|F32)$", "", core.strip("-_."), flags=re.I).strip("-_.").lower()
        if len(core) >= 4 and low.startswith(core) and (len(low) == len(core) or low[len(core)] in "-_."):
            best.setdefault(len(core), []).append(c)
    return sorted(best[max(best)]) if best else []


def list_models(models_dir: Optional[str] = None) -> list[str]:
    base = _expand(models_dir if models_dir is not None else settings.local_models_dir)
    if not base.is_dir():
        return []
    return sorted(f.name for f in base.glob("*.gguf") if "mmproj" not in f.name.lower())


# ---------------------------------------------------------------------------
# GPU etiquette
# ---------------------------------------------------------------------------


def is_gpu_work(command: str) -> bool:
    """A Bash command that probably wants the GPU (CUDA tools, ML frameworks, model servers)."""
    return bool(command) and bool(_GPU_WORK.search(command))


_GPU_TALK = re.compile(
    r"\b(?:gpu|gpus|cuda|vram|cudnn|nvidia|rocm|tensorrt|vllm|ollama|llama\.cpp|gguf|"
    r"train(?:ing)?\s+(?:a\s+|the\s+)?(?:model|network|net|lora)|fine-?tun\w*|inference\s+server)\b",
    re.I,
)


def request_wants_gpu(text: str) -> bool:
    """A user request that talks about GPU work: the local model stays off the card for the turn."""
    return bool(text) and (bool(_GPU_TALK.search(text)) or is_gpu_work(text))


# Desktop clients that hold a small GPU context all day (browsers' gpu-process,
# compositors, Electron apps). They are not "GPU work" and must not block the helper.
_GRAPHICS_CLIENTS = re.compile(
    r"^(chrome|chromium|brave|firefox|msedge|opera|vivaldi|xorg|xwayland|gnome-shell|kwin\w*|"
    r"plasmashell|mutter|picom|compiz|electron|code|slack|discord|steamwebhelper|obs|zoom|teams)",
    re.I,
)


def _proc_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def parse_compute_apps(text: str, own_pids: set, names: Optional[dict] = None) -> bool:
    """nvidia-smi ``pid,used_memory`` CSV -> a foreign non-desktop app holds >= GUARD_MIN_MIB."""
    names = names if names is not None else {}
    for row in (text or "").splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        try:
            mib = float(re.sub(r"[^\d.]", "", parts[1]) or 0)
        except ValueError:
            mib = 0.0
        if pid in own_pids or mib < GUARD_MIN_MIB:
            continue
        name = names.get(pid, _proc_name(pid))
        if name and _GRAPHICS_CLIENTS.match(name):
            continue
        return True
    return False


def _need_mib() -> int:
    w = wanted()
    size = 0
    for key in ("model", "mmproj"):
        if w.get(key):
            try:
                size += Path(w[key]).stat().st_size
            except OSError:
                pass
    # weights + KV cache / compute buffers headroom
    return int(size / 2**20 * 1.2) + 768


def gpu_busy() -> bool:
    """The GPU is taken: a foreign compute job, or too little free VRAM to load the model.

    Cached a few seconds. No nvidia-smi (CPU build, other vendor) -> not busy.
    """
    global _guard_cache
    if not settings.local_gpu_guard:
        return False
    now = time.monotonic()
    if now - _guard_cache[0] < GUARD_TTL_S:
        return _guard_cache[1]
    busy = False
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            st = _read_state()
            own_pid = int(st.get("pid") or 0)
            ours = bool(own_pid) and _alive(own_pid)
            out = subprocess.run(
                [smi, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            busy = parse_compute_apps(out, {own_pid} if ours else set())
            if not busy and not ours:
                free = subprocess.run(
                    [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.split()
                if free and free[0].isdigit():
                    busy = int(free[0]) < _need_mib()
        except Exception:
            busy = False
    _guard_cache = (now, busy)
    return busy


def new_turn() -> None:
    """A user turn starts: a previous turn's GPU-work yield ends."""
    global _turn_yield
    _turn_yield = False


def note_command(command: str) -> bool:
    """Called before every Bash command. GPU work -> release the server for this turn."""
    global _turn_yield
    if not settings.local_enabled or not is_gpu_work(command):
        return False
    _turn_yield = True
    release()
    return True


def available() -> bool:
    """Local help may be used right now (on, not yielded, GPU not taken by someone else)."""
    if _turn_yield or not settings.local_enabled:
        return False
    if gpu_busy():
        release()
        return False
    return True


# ---------------------------------------------------------------------------
# Shared server
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return get_global_cache_dir() / "local_llm" / "server.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def _alive(pid: int) -> bool:
    """Running (a zombie is not: kill(pid, 0) succeeds on one)."""
    if _proc is not None and _proc.pid == pid and _proc.poll() is not None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wanted() -> dict:
    """What to serve: binary, model, mmproj (vision) — the settings, auto-filled by autotune.py.

    One server serves text and vision: a set ``local_vision_file`` is the served
    model; otherwise ``local_model_file`` (empty = the best model found), with
    vision on when that model has a projector next to it.
    """
    from . import autotune

    prof = autotune.profile()
    return {"bin": prof.binary, "model": prof.model, "mmproj": prof.mmproj}


def vision_model() -> str:
    """File name of the served model when it can see images (has an mmproj), else ""."""
    w = wanted()
    return w["model"].name if (w["model"] and w["mmproj"]) else ""


def status() -> dict:
    st = _read_state()
    w = wanted()
    running = bool(st.get("pid")) and _alive(int(st["pid"]))
    from . import autotune

    prof = autotune.profile()
    return {
        "mode": settings.local_model,
        "enabled": bool(settings.local_enabled),
        "auto": prof.summary(),
        "picked_by": prof.picked_by,
        "gpu": f"{prof.gpu.name} {prof.gpu.total_mib} MiB" if prof.gpu.kind else "",
        "binary": str(w["bin"] or ""),
        "model": str(w["model"] or ""),
        "mmproj": str(w["mmproj"] or ""),
        "running": running,
        "pid": st.get("pid") if running else None,
        "port": st.get("port") if running else None,
        "served_model": Path(st.get("model", "")).name if running else "",
        "yielded": _turn_yield,
        "last_error": _last_error,
    }


def release() -> bool:
    """Stop the shared server (frees VRAM). True when one was stopped."""
    st = _read_state()
    pid = int(st.get("pid") or 0)
    stopped = False
    if pid and _alive(pid) and _is_llama(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
    if _proc is not None and _proc.pid == pid:
        try:
            _proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass
    return stopped


def _is_llama(pid: int) -> bool:
    """Never signal a recycled pid: check the process is a llama-server."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"llama-server" in cmd
    except OSError:
        return os.name != "posix" or not Path("/proc").is_dir()


async def _healthy(port: int, timeout: float = 2.0) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"http://127.0.0.1:{port}/health")
            return r.status_code == 200
    except Exception:
        return False


def _spawn(w: dict) -> dict:
    port = int(settings.local_port) or _free_port()
    argv = [
        str(w["bin"]), "-m", str(w["model"]),
        "-ngl", str(settings.local_gpu_layers), "-c", str(settings.local_ctx), "-np", "1",
        "--host", "127.0.0.1", "--port", str(port), "--no-webui",
        "--sleep-idle-seconds", str(IDLE_SLEEP_S),
    ]
    if w["mmproj"]:
        argv += ["--mmproj", str(w["mmproj"])]
    logdir = get_global_cache_dir() / "local_llm"
    logdir.mkdir(parents=True, exist_ok=True)
    log = open(logdir / "server.log", "ab")
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
    if os.name == "posix":
        kw["start_new_session"] = True
    global _proc
    proc = subprocess.Popen(argv, **kw)
    _proc = proc
    log.close()
    st = {"pid": proc.pid, "port": port, "model": str(w["model"]),
          "mmproj": str(w["mmproj"] or ""), "started": time.time()}
    _write_state(st)
    return st


async def ensure(wait: bool = True) -> Optional[int]:
    """Port of a healthy server for the wanted model, starting one if needed.

    ``wait=False`` never blocks a tool call on a model load: it starts the
    server in the background and returns None until it is up.
    """
    global _last_error, _spawn_task
    if not available():
        return None
    w = wanted()
    if not w["bin"] or not w["model"]:
        _last_error = "llama-server not found" if not w["bin"] else "model file not found"
        return None
    st = _read_state()
    pid = int(st.get("pid") or 0)
    if pid and _alive(pid) and st.get("model") == str(w["model"]) and st.get("mmproj", "") == str(w["mmproj"] or ""):
        if await _healthy(int(st["port"])):
            return int(st["port"])
        started = float(st.get("started") or 0)
        if time.time() - started > HEALTH_WAIT_S:
            release()  # wedged
            st = {}
    elif pid:
        release()  # other model: one server at a time on a 12 GB card
        st = {}
    if not st.get("pid"):
        try:
            st = _spawn(w)
        except Exception as e:  # noqa: BLE001
            _last_error = f"spawn failed: {e}"
            return None
    port = int(st["port"])
    if not wait:
        if _spawn_task is None or _spawn_task.done():
            _spawn_task = asyncio.ensure_future(_wait_health(port))
        return None
    return port if await _wait_health(port) else None


async def _wait_health(port: int) -> bool:
    global _last_error
    deadline = time.monotonic() + HEALTH_WAIT_S
    while time.monotonic() < deadline:
        if await _healthy(port, 1.0):
            return True
        st = _read_state()
        if not st.get("pid") or not _alive(int(st["pid"])):
            _last_error = "server exited during load (see global_cache/local_llm/server.log)"
            return False
        await asyncio.sleep(0.25)
    _last_error = "server did not become healthy"
    return False


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


_no_grammar: set = set()  # (port, model) of servers whose model cannot build the JSON-schema grammar


def parse_json_reply(text: str) -> Optional[dict]:
    """The first JSON object in a free-form reply (think blocks and code fences removed)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S)  # an opened-by-template think block
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _end = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def chat_json(
    system: str,
    user: Any,
    schema: dict,
    *,
    max_tokens: int = 400,
    timeout: float = 30.0,
    wait: bool = True,
) -> Optional[dict]:
    """One JSON chat call, thinking off. None on any failure.

    The reply is grammar-constrained by ``schema``. Some GGUF vocabularies
    cannot build llama.cpp's grammar sampler (HTTP 400 "Failed to initialize
    samplers"); for such a server the call is repeated once, and every later
    call made, without the grammar: the schema goes into the prompt and the
    first JSON object of the reply is parsed.
    """
    global _last_error
    port = await ensure(wait=wait)
    if port is None:
        return None
    import httpx

    key = (port, str(_read_state().get("model", "")))
    body: dict[str, Any] = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    for constrained in ((False,) if key in _no_grammar else (True, False)):
        req = dict(body)
        if constrained:
            req["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        else:
            req["messages"] = [
                {"role": "system", "content": f"{system}\nReply with one JSON object only, matching this "
                                              f"JSON schema: {json.dumps(schema)}"},
                {"role": "user", "content": user},
            ]
            req["max_tokens"] = max_tokens + 200
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=req)
            if constrained and r.status_code == 400 and "sampler" in r.text.lower():
                _no_grammar.add(key)
                continue
            text = r.json()["choices"][0]["message"].get("content") or ""
            if constrained:
                obj = json.loads(text)
                return obj if isinstance(obj, dict) else None
            obj = parse_json_reply(text)
            if obj is None:
                _last_error = "reply was not JSON"
            return obj
        except Exception as e:  # noqa: BLE001
            _last_error = f"call failed: {type(e).__name__}"
            return None
    return None


_SYS_PICK = (
    "You select lines from a command's output. The user gives what an engineer is looking for "
    "and numbered lines. Return the ids of the lines (at most 8) that best answer it, most useful first. "
    "Only ids that appear."
)
_SCHEMA_IDS = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "integer"}, "maxItems": 8}},
    "required": ["lines"],
}


async def pick_lines(lines: list, ids: list, intent: str) -> list:
    """Local rerank: 0-based indexes (subset of ``ids``) most relevant to ``intent``."""
    if not ids or not intent.strip():
        return []
    rows = "\n".join(f"{i + 1}: {lines[i][:200]}" for i in ids[:160])
    obj = await chat_json(
        _SYS_PICK, f"Looking for: {intent[-800:]}\n\nLines:\n{rows}", _SCHEMA_IDS,
        max_tokens=80, timeout=RERANK_TIMEOUT_S, wait=False,
    )
    if not obj:
        return []
    valid = set(ids)
    out = []
    for x in obj.get("lines", []):
        if isinstance(x, int) and (x - 1) in valid:
            out.append(x - 1)
    return out


async def vision(question: str, data_uri: str, system: str = "", timeout: float = 60.0) -> Optional[str]:
    """Answer an image question with the local vision model. None when not configured or on failure."""
    global _last_error
    if not vision_model():
        return None
    port = await ensure(wait=True)
    if port is None:
        return None
    import httpx

    body = {
        "messages": ([{"role": "system", "content": system}] if system else []) + [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": question},
            ],
        }],
        "temperature": 0,
        "max_tokens": 700,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=body)
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001
        _last_error = f"vision failed: {type(e).__name__}"
        return None
