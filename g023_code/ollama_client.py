"""
Ollama integration — host resolution, model discovery, and vision inference.

The daemon does not have to be local. Anything reachable over HTTP works:
another box on the LAN, a GPU server, an SSH tunnel. The host is resolved once,
here, from a single precedence chain (see ``resolve_host``), so every caller —
``/ollama`` and ``/config_vision`` for discovery, the Vision subagent for
inference — talks to the same daemon.

Philosophy is unchanged from the rest of g023: the orchestrator only ever sees a
compact textual answer, never image bytes.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import httpx

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_PORT = 11434

# Model families known to be multimodal, used as a fallback when the Ollama
# /api/show response does not advertise capabilities (older daemons).
KNOWN_VISION_FAMILIES = {
    "clip",
    "gemma3",
    "llava",
    "mllama",
    "minicpmv",
    "mistral3",
    "qwen2vl",
    "qwen25vl",
    "qwen3vl",
    "qwen35",
    "vlm",
}

KNOWN_VISION_NAME_HINTS = (
    "llava",
    "bakllava",
    "moondream",
    "minicpm-v",
    "llama3.2-vision",
    "granite3.2-vision",
    "gemma3",
    "-vl",
    "vision",
)


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------

def normalize_host(raw: str) -> str:
    """
    Turn whatever the user typed into a canonical base URL.

        localhost            → http://localhost:11434
        192.168.1.50         → http://192.168.1.50:11434
        192.168.1.50:11500   → http://192.168.1.50:11500
        gpu.lan:11434/       → http://gpu.lan:11434
        http://gpu.lan       → http://gpu.lan:11434
        https://ollama.me    → https://ollama.me      (443 is meant literally)

    A portless plain-HTTP host gets ``:11434`` appended, because forgetting the
    port is the single most common way to point at nothing. An https URL is left
    alone — those go through a reverse proxy on 443 far more often than not.
    """
    host = (raw or "").strip().rstrip("/")
    if not host:
        return DEFAULT_HOST

    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"

    scheme, _, rest = host.partition("://")
    # Split off any path so ":port" detection only looks at the authority.
    authority, slash, path = rest.partition("/")

    # IPv6 literals are bracketed — [::1]:11434 — so only look after the bracket.
    has_port = ":" in authority.rsplit("]", 1)[-1]
    if not has_port and scheme == "http":
        authority = f"{authority}:{DEFAULT_PORT}"

    return f"{scheme}://{authority}{slash}{path}".rstrip("/")


def env_host() -> Optional[str]:
    """OLLAMA_HOST from the environment, normalized — None when unset."""
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    return normalize_host(raw) if raw else None


def resolve_host(override: Optional[str] = None) -> str:
    """
    The daemon every part of g023 should talk to.

    Precedence: explicit argument → configured ``vision_host`` → ``OLLAMA_HOST``
    → localhost. Importing settings lazily keeps this module usable standalone.
    """
    if override:
        return normalize_host(override)
    try:
        from .config import settings

        if settings.vision_host:
            return normalize_host(settings.vision_host)
    except Exception:
        pass
    return env_host() or DEFAULT_HOST


def host_source() -> str:
    """Human-readable explanation of where the active host came from."""
    try:
        from .config import settings

        configured = settings.vision_host
    except Exception:
        configured = None

    if configured:
        return "config.json (vision_host)"
    if env_host():
        return "OLLAMA_HOST environment variable"
    return "default (localhost)"


def is_local(host: Optional[str] = None) -> bool:
    """True when the daemon runs on this machine — used for hint wording."""
    h = (host or resolve_host()).lower()
    return any(marker in h for marker in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0"))


# Kept for backwards compatibility with older call sites / configs.
def get_host() -> str:
    return resolve_host()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class OllamaModel:
    name: str
    size_bytes: int = 0
    families: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    parameter_size: str = ""
    quantization: str = ""
    modified_at: str = ""

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1_000_000_000

    @property
    def supports_vision(self) -> bool:
        if self.capabilities:
            return "vision" in self.capabilities
        # Older Ollama daemons don't report capabilities — guess from metadata.
        if any(f.lower() in KNOWN_VISION_FAMILIES for f in self.families):
            return True
        low = self.name.lower()
        return any(h in low for h in KNOWN_VISION_NAME_HINTS)

    @property
    def vision_certainty(self) -> str:
        """'reported' when the daemon said so, 'guessed' when we inferred it."""
        if self.capabilities:
            return "reported" if "vision" in self.capabilities else "no"
        return "guessed" if self.supports_vision else "no"


@dataclass
class HostProbe:
    """Result of a reachability check — everything /ollama needs to explain itself."""

    host: str
    ok: bool = False
    version: str = ""
    latency_ms: int = 0
    error: str = ""
    model_count: Optional[int] = None

    @property
    def summary(self) -> str:
        if self.ok:
            models = f" · {self.model_count} model(s)" if self.model_count is not None else ""
            return f"reachable · Ollama {self.version or '?'} · {self.latency_ms} ms{models}"
        return self.error or "unreachable"


def probe(host: Optional[str] = None, timeout: float = 4.0, count_models: bool = True) -> HostProbe:
    """Check whether a daemon answers, and report why not when it doesn't."""
    base = resolve_host(host)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base}/api/version")
            elapsed = int((time.perf_counter() - started) * 1000)
            if resp.status_code != 200:
                return HostProbe(
                    host=base,
                    error=f"HTTP {resp.status_code} from /api/version — is that really Ollama?",
                    latency_ms=elapsed,
                )
            version = ""
            try:
                version = str(resp.json().get("version") or "")
            except Exception:
                pass

            model_count = None
            if count_models:
                try:
                    tags = client.get(f"{base}/api/tags")
                    if tags.status_code == 200:
                        model_count = len(tags.json().get("models") or [])
                except Exception:
                    model_count = None

            return HostProbe(
                host=base,
                ok=True,
                version=version,
                latency_ms=elapsed,
                model_count=model_count,
            )
    except httpx.ConnectError:
        return HostProbe(host=base, error="connection refused — nothing is listening there")
    except httpx.ConnectTimeout:
        return HostProbe(host=base, error=f"connection timed out after {timeout:g}s")
    except httpx.TimeoutException:
        return HostProbe(host=base, error=f"timed out after {timeout:g}s")
    except Exception as e:  # DNS failures, bad URLs, TLS errors…
        return HostProbe(host=base, error=f"{type(e).__name__}: {e}")


def is_available(host: Optional[str] = None, timeout: float = 4.0) -> bool:
    """True if an Ollama daemon answers on the resolved host."""
    return probe(host, timeout=timeout, count_models=False).ok


def unreachable_hint(host: str) -> str:
    """A next step tailored to whether the host is local or remote."""
    if is_local(host):
        return (
            "Start it with [bold]ollama serve[/bold], or point g023 at another machine "
            "with [bold]/ollama host <ip:port>[/bold]."
        )
    return (
        "Check the machine is up and that Ollama is bound to the network there "
        "([bold]OLLAMA_HOST=0.0.0.0:11434 ollama serve[/bold]), and that no firewall "
        "blocks the port. [bold]/ollama host default[/bold] returns to localhost."
    )


def list_models(
    host: Optional[str] = None,
    timeout: float = 15.0,
    probe_capabilities: bool = True,
) -> list[OllamaModel]:
    """
    List installed models, enriched with per-model capabilities.

    The capability probe is one extra request per model, which is cheap locally
    and noticeably slower across a network — pass ``probe_capabilities=False``
    to skip it and fall back to family/name heuristics.
    """
    base = resolve_host(host)
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{base}/api/tags")
        resp.raise_for_status()
        raw = resp.json().get("models", [])

        models: list[OllamaModel] = []
        for entry in raw:
            name = entry.get("name") or entry.get("model") or ""
            if not name:
                continue
            details = entry.get("details") or {}
            families = tuple(
                details.get("families") or ([details["family"]] if details.get("family") else [])
            )
            capabilities: tuple[str, ...] = ()
            if probe_capabilities:
                try:
                    show = client.post(f"{base}/api/show", json={"model": name}, timeout=timeout)
                    if show.status_code == 200:
                        capabilities = tuple(show.json().get("capabilities") or ())
                except Exception:
                    pass  # capability probe is best-effort
            models.append(
                OllamaModel(
                    name=name,
                    size_bytes=int(entry.get("size") or 0),
                    families=families,
                    capabilities=capabilities,
                    parameter_size=str(details.get("parameter_size") or ""),
                    quantization=str(details.get("quantization_level") or ""),
                    modified_at=str(entry.get("modified_at") or ""),
                )
            )
    return sorted(models, key=lambda m: m.name)


def find_model(models: Iterable[OllamaModel], wanted: str) -> Optional[OllamaModel]:
    """Match a model by exact name, then by bare name without the :tag."""
    wanted = wanted.strip()
    for m in models:
        if m.name == wanted:
            return m
    candidates = [m for m in models if m.name.split(":")[0] == wanted]
    if len(candidates) == 1:
        return candidates[0]
    # Last resort: a unique substring match, so ":latest" typos still land.
    loose = [m for m in models if wanted.lower() in m.name.lower()]
    return loose[0] if len(loose) == 1 else None


def running_models(host: Optional[str] = None, timeout: float = 5.0) -> list[dict]:
    """Models currently loaded in the daemon's memory (/api/ps). Best-effort."""
    base = resolve_host(host)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base}/api/ps")
            if resp.status_code != 200:
                return []
            return resp.json().get("models") or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------

@dataclass
class LoadedImage:
    b64: str
    raw: bytes
    original_bytes: int = 0
    original_size: tuple[int, int] = (0, 0)
    final_size: tuple[int, int] = (0, 0)
    downscaled: bool = False
    source: str = ""

    @property
    def bytes_sent(self) -> int:
        return len(self.raw)


async def load_image(path_or_url: str, max_dim: int = 0, timeout: float = 30.0) -> LoadedImage:
    """
    Load an image from a local path or http(s) URL, optionally downscaled.

    Returns the base64 payload plus the measurements the UI reports back to the
    user — how big the image was, what we actually sent, and whether Pillow was
    there to shrink it. Fewer pixels means far less VRAM and a much faster pass,
    which matters more, not less, when the daemon is across a network.
    """
    if path_or_url.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(path_or_url)
            resp.raise_for_status()
            data = resp.content
        source = "url"
    else:
        p = Path(path_or_url).expanduser()
        if not p.is_absolute():
            from .config import get_project_root

            p = get_project_root() / p
        p = p.resolve()
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {p}")
        data = p.read_bytes()
        source = str(p)

    original = len(data)
    before = _dimensions(data)
    if max_dim > 0:
        data = _maybe_downscale(data, max_dim)
    after = _dimensions(data)

    return LoadedImage(
        b64=base64.b64encode(data).decode("ascii"),
        raw=data,
        original_bytes=original,
        original_size=before,
        final_size=after,
        downscaled=before != after and after != (0, 0),
        source=source,
    )


async def load_image_b64(path_or_url: str, max_dim: int = 0, timeout: float = 30.0) -> tuple[str, bytes]:
    """Backwards-compatible wrapper returning just (base64, raw bytes)."""
    img = await load_image(path_or_url, max_dim=max_dim, timeout=timeout)
    return img.b64, img.raw


def _dimensions(data: bytes) -> tuple[int, int]:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return (0, 0)


def _maybe_downscale(data: bytes, max_dim: int) -> bytes:
    """Downscale with Pillow when available; otherwise return the original."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return data

    try:
        with Image.open(io.BytesIO(data)) as img:
            if max(img.size) <= max_dim:
                return data
            img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:
        return data


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

@dataclass
class VisionResult:
    answer: str
    model: str = ""
    host: str = ""
    elapsed_ms: int = 0
    eval_count: int = 0
    prompt_eval_count: int = 0
    load_ms: int = 0

    @property
    def tokens_per_second(self) -> float:
        if not self.eval_count or self.elapsed_ms <= 0:
            return 0.0
        return self.eval_count / (self.elapsed_ms / 1000)


async def vision_chat_detailed(
    model: str,
    prompt: str,
    image_b64: str,
    *,
    system: Optional[str] = None,
    host: Optional[str] = None,
    timeout: float = 180.0,
    num_ctx: int = 4096,
    temperature: float = 0.2,
    keep_alive: Optional[str] = None,
) -> VisionResult:
    """Run a single-image vision turn and report the daemon's own timings."""
    base = resolve_host(host)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt, "images": [image_b64]})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/api/chat", json=payload)
    except httpx.ConnectError as e:
        raise RuntimeError(f"Cannot reach Ollama at {base} — {e}") from e
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"Ollama at {base} did not answer within {timeout:g}s. "
            "A cold model load on a remote GPU can exceed this — raise vision_timeout."
        ) from e

    elapsed = int((time.perf_counter() - started) * 1000)

    if resp.status_code != 200:
        detail = resp.text[:400]
        try:
            detail = json.loads(resp.text).get("error", detail)
        except Exception:
            pass
        raise RuntimeError(f"Ollama error ({resp.status_code}) from {base}: {detail}")

    body = resp.json()
    return VisionResult(
        answer=(body.get("message") or {}).get("content", "").strip(),
        model=model,
        host=base,
        elapsed_ms=elapsed,
        eval_count=int(body.get("eval_count") or 0),
        prompt_eval_count=int(body.get("prompt_eval_count") or 0),
        load_ms=int((body.get("load_duration") or 0) // 1_000_000),
    )


async def vision_chat(
    model: str,
    prompt: str,
    image_b64: str,
    *,
    system: Optional[str] = None,
    host: Optional[str] = None,
    timeout: float = 180.0,
    num_ctx: int = 4096,
    temperature: float = 0.2,
) -> str:
    """Backwards-compatible wrapper returning just the answer text."""
    result = await vision_chat_detailed(
        model,
        prompt,
        image_b64,
        system=system,
        host=host,
        timeout=timeout,
        num_ctx=num_ctx,
        temperature=temperature,
    )
    return result.answer
