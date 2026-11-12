"""
Machine probe for the optional local model: what is installed, what fits, what to run.

``local_model = "auto"`` (the default) turns the llama.cpp helper on by itself
when this machine can run it well, so every project gets the measured setup
without ``/local`` commands:

* a ``llama-server`` binary: the configured ``local_llama_dir``, then common
  checkout folders, then ``PATH``;
* a GPU that fits the model (``nvidia-smi`` total VRAM, or Apple Silicon's
  unified memory). Without one the scout card's time box (up to ~12 s per turn
  on a CPU) would cost more than it saves, so auto stays off;
* a model: the configured ``local_model_file`` when it resolves, else the best
  ``.gguf`` found, ranked by the scout eval (``KNOWN``), then by a size
  heuristic (small instruct models are fast enough for per-turn work).

One server serves both text and vision, so local vision is on exactly when the
chosen model is multimodal (an ``mmproj`` projector lies next to it): it never
causes a model swap. The probe is cached per process; ``refresh()`` re-runs it.
"""

from __future__ import annotations

import math
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import settings

# Measured on the offline scout eval (20 location questions over a 25k-line
# package; card hit = a card line inside the gold definition, of 20).
# rank: lower is better. Prefix match on the lower-cased file name.
#   LFM3-VL-2B Q8      16/20 card, 17/20 incl. also-matching, ~1 s
#   LFM3-1.7B Q8       14/20, 17/20, ~1 s   (same score at QAD Q4_0)
#   Nanbeige4.2-3B Q6  14/20 at 5-10 s, needs the grammar-less fallback
KNOWN: tuple[tuple[str, int], ...] = (
    ("lfm3-vl-2b", 0),
    ("lfm3-1.7b-q8", 1),
    ("lfm3-1.7b", 2),
    ("nanbeige", 8),
)

# Not chat models, or not useful as a per-turn helper.
_SKIP = re.compile(
    r"mmproj|embed|rerank|whisper|\btts\b|[-_.]tts|vocoder|draft|lora|clip[-_]|"
    r"-0000[2-9]-of-|-000[1-9]\d-of-",
    re.I,
)

_LLAMA_DIRS = (
    "~/llamacpp/llama.cpp", "~/llama.cpp", "~/src/llama.cpp", "~/code/llama.cpp",
    "~/git/llama.cpp", "~/projects/llama.cpp", "/opt/llama.cpp", "/usr/local/llama.cpp",
)
_MODEL_DIRS = (
    "~/MODELS", "~/models", "~/Models", "~/.cache/llama.cpp", "~/llama.cpp/models",
    "~/.lmstudio/models", "~/.cache/lm-studio/models",
)

TARGET_MIB = 2048  # a ~2 GB file: 1.7-3B at Q4-Q8, about 1 s per scout call on a mid GPU
MAX_MODEL_MIB = 6144  # larger helpers blow the per-turn time box
OFF_WORDS = ("off", "none", "no", "false", "0")


@dataclass
class Gpu:
    kind: str = ""  # "cuda" | "metal" | ""
    name: str = ""
    total_mib: int = 0


@dataclass
class Profile:
    usable: bool = False
    reason: str = ""
    binary: Optional[Path] = None
    models_dir: Optional[Path] = None
    model: Optional[Path] = None
    mmproj: Optional[Path] = None
    gpu: Gpu = field(default_factory=Gpu)
    picked_by: str = ""  # "setting" | "measured" | "size"

    @property
    def vision(self) -> bool:
        return self.mmproj is not None

    def summary(self) -> str:
        gpu = f"{self.gpu.name} {self.gpu.total_mib // 1024} GB" if self.gpu.total_mib else (self.gpu.name or "no GPU")
        model = self.model.name if self.model else "no model"
        extra = " + vision" if self.vision else ""
        return f"{model}{extra} on {gpu}" if self.usable else f"{self.reason}"


_cache: Optional[Profile] = None


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p or ""))).resolve()


def parse_smi_gpu(text: str) -> Gpu:
    """nvidia-smi ``name,memory.total`` CSV (noheader, nounits) -> the first GPU, else none."""
    for row in (text or "").splitlines():
        name, _, mib = row.rpartition(",")
        if name.strip() and mib.strip().isdigit():
            return Gpu("cuda", name.strip(), int(mib.strip()))
    return Gpu()


def probe_gpu() -> Gpu:
    """The first NVIDIA GPU (name, total MiB), or Apple Silicon (2/3 of RAM), else none."""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            gpu = parse_smi_gpu(_run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]))
            if gpu.kind:
                return gpu
        except Exception:  # noqa: BLE001
            pass
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            ram = int(_run(["sysctl", "-n", "hw.memsize"]).strip())
            return Gpu("metal", "Apple GPU", int(ram / 2**20 * 2 / 3))
        except Exception:  # noqa: BLE001
            return Gpu("metal", "Apple GPU", 0)
    return Gpu()


def _run(argv: list) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=3).stdout


def find_binary(configured: str) -> Optional[Path]:
    from .local_llm import find_server_binary

    seen = set()
    for d in (configured, *_LLAMA_DIRS):
        if not d or d in seen:
            continue
        seen.add(d)
        b = find_server_binary(d)  # also falls back to PATH
        if b and (d == configured or _expand(d) in b.parents):
            return b
    return find_server_binary(configured)


def find_models_dir(configured: str) -> Optional[Path]:
    for d in (configured, *_MODEL_DIRS):
        p = _expand(d) if d else None
        if p and p.is_dir() and any(p.rglob("*.gguf")):
            return p
    return None


def candidates(models_dir: Path) -> list[Path]:
    return sorted(f for f in models_dir.rglob("*.gguf") if f.is_file() and not _SKIP.search(f.name))


def _mib(p: Path) -> int:
    try:
        return int(p.stat().st_size / 2**20)
    except OSError:
        return 0


def need_mib(model: Path, mmproj: Optional[Path]) -> int:
    """VRAM to load model + projector: weights ×1.2 plus 768 MiB of KV/compute (as local_llm's guard)."""
    return int((_mib(model) + (_mib(mmproj) if mmproj else 0)) * 1.2) + 768


def rank(models: Iterable[Path], budget_mib: int) -> list[Path]:
    """Best first: measured models by their eval rank, then unmeasured ones closest to TARGET_MIB."""
    from .local_llm import find_mmproj

    def key(p: Path):
        low = p.name.lower()
        known = next((r for pre, r in KNOWN if low.startswith(pre)), None)
        size = max(_mib(p), 1)
        return (known is None, known if known is not None else 0, abs(math.log(size / TARGET_MIB)), low)

    fits = []
    for p in models:
        size = _mib(p)
        if size < 256 or size > MAX_MODEL_MIB:
            continue
        if budget_mib and need_mib(p, find_mmproj(p)) > budget_mib:
            continue
        fits.append(p)
    return sorted(fits, key=key)


def probe(
    *,
    llama_dir: Optional[str] = None,
    models_dir: Optional[str] = None,
    model_file: Optional[str] = None,
    vision_file: Optional[str] = None,
    gpu: Optional[Gpu] = None,
) -> Profile:
    """What auto mode would run on this machine. Arguments default to the settings."""
    from .local_llm import find_mmproj, resolve_model

    llama_dir = settings.local_llama_dir if llama_dir is None else llama_dir
    models_dir = settings.local_models_dir if models_dir is None else models_dir
    model_file = settings.local_model_file if model_file is None else model_file
    vision_file = settings.local_vision_file if vision_file is None else vision_file
    prof = Profile(gpu=gpu if gpu is not None else probe_gpu())
    prof.binary = find_binary(llama_dir)
    if prof.binary is None:
        prof.reason = "llama-server not found (local_llama_dir)"
        return prof
    prof.models_dir = find_models_dir(models_dir)
    no_vision = (vision_file or "").strip().lower() in OFF_WORDS
    if vision_file and not no_vision:
        prof.model = resolve_model(vision_file, str(prof.models_dir or models_dir))
        prof.picked_by = "setting"
    if prof.model is None and model_file and model_file.strip().lower() != "auto":
        prof.model = resolve_model(model_file, str(prof.models_dir or models_dir))
        prof.picked_by = "setting"
    if prof.model is None and prof.models_dir is not None:
        budget = int(prof.gpu.total_mib * 0.9) if prof.gpu.total_mib else 0
        ranked = rank(candidates(prof.models_dir), budget)
        if ranked:
            prof.model = ranked[0]
            low = prof.model.name.lower()
            prof.picked_by = "measured" if any(low.startswith(pre) for pre, _ in KNOWN) else "size"
    if prof.model is None:
        prof.reason = "no usable .gguf model found (local_models_dir / local_model_file)"
        return prof
    prof.mmproj = None if no_vision else find_mmproj(prof.model)
    if not prof.gpu.kind:
        prof.reason = "no GPU found (a CPU is too slow for per-turn help; /local on forces it)"
        return prof
    if prof.gpu.total_mib and need_mib(prof.model, prof.mmproj) > prof.gpu.total_mib:
        if prof.mmproj and need_mib(prof.model, None) <= prof.gpu.total_mib:
            prof.mmproj = None
        else:
            prof.reason = f"{prof.model.name} does not fit in {prof.gpu.total_mib} MiB of VRAM"
            return prof
    prof.usable = True
    return prof


def profile() -> Profile:
    """The cached probe for the current settings (re-probed when a local_* setting changes)."""
    global _cache
    key = (settings.local_llama_dir, settings.local_models_dir, settings.local_model_file, settings.local_vision_file)
    if _cache is None or getattr(_cache, "_key", None) != key:
        _cache = probe()
        _cache._key = key  # type: ignore[attr-defined]
    return _cache


def refresh() -> Profile:
    global _cache
    _cache = None
    return profile()
