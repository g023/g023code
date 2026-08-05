"""
Configuration and environment for g023 Code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_home() -> Path:
    """Directory containing the g023-code installation (where K.dat lives)."""
    env = os.environ.get("G023_HOME")
    if env:
        return Path(env).resolve()
    # Fallback: package parent
    return Path(__file__).resolve().parent.parent


def get_project_root() -> Path:
    """Current project working directory (where the user launched from)."""
    env = os.environ.get("G023_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def get_scratch_dir() -> Path:
    """Per-project .g023/ scratch folder."""
    root = get_project_root() / ".g023"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_cache_db() -> Path:
    return get_scratch_dir() / "cache.db"


def get_config_file() -> Path:
    """
    Machine-level config (next to K.dat). Vision setup describes the local
    machine's GPU/Ollama install, so it belongs here rather than per-project.
    """
    return get_home() / "config.json"


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    """Load DeepSeek API key from K.dat inside the program folder."""
    key_file = get_home() / "K.dat"
    if not key_file.exists():
        raise FileNotFoundError(
            f"API key file not found: {key_file}\n"
            "Create K.dat in the g023-code folder and put your DeepSeek API key on the first line."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    # The installers leave a placeholder behind when the user skips the key
    # prompt; recognising it turns a confusing 401 into a clear instruction.
    if not key or key in ("YOUR_DEEPSEEK_API_KEY_HERE", "sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY"):
        raise ValueError(
            f"Please put a valid DeepSeek API key into {key_file}\n"
            "Get one at https://platform.deepseek.com/"
        )
    return key


# ---------------------------------------------------------------------------
# Model & Runtime Settings
# ---------------------------------------------------------------------------

ModelId = Literal["deepseek-v4-flash", "deepseek-v4-pro"]
ReasoningEffort = Literal["low", "high", "max"]

# Models the Responses API will actually serve. Everything here runs on
# ``/responses`` — that is the only endpoint exposing DeepSeek's server-side
# web_search — and as of 2026-08 it answers a v4-pro request with "Codex
# integration with deepseek-v4-pro will be available starting early August 2026.
# Please use deepseek-v4-flash instead for now." Offering pro would just hand the
# user a model every call fails on, so the choice is withheld until it works.
AVAILABLE_MODELS: tuple[str, ...] = ("deepseek-v4-flash",)
UNAVAILABLE_MODELS: dict[str, str] = {
    "deepseek-v4-pro": (
        "DeepSeek has not enabled deepseek-v4-pro on the Responses API yet "
        "(it reports availability from early August 2026)."
    ),
}

# low  — final answer only (default)
# mid  — answer + per-turn token/cost line + tool result previews
# high — everything in mid, plus the model's reasoning/thinking excerpts
VerboseLevel = Literal["low", "mid", "high"]
VERBOSE_LEVELS: tuple[str, ...] = ("low", "mid", "high")


@dataclass
class Settings:
    # Models
    orchestrator_model: ModelId = "deepseek-v4-flash"
    subagent_model: ModelId = "deepseek-v4-flash"
    reasoning_effort: ReasoningEffort = "high"
    thinking_enabled: bool = True

    # API
    base_url: str = "https://api.deepseek.com"
    max_tokens: int = 8192
    temperature: float = 0.2

    # Web search is DeepSeek's server-side ``web_search`` tool, offered to the
    # model as a peer of our own tools on every orchestrator call — there is no
    # separate search request to configure. The server may run a long agentic
    # loop (5-20 searches plus page opens) inside a single turn, which is why the
    # client timeout in api.py is generous.

    # Context
    max_context_tokens: int = 900_000  # leave headroom under 1M
    compact_threshold: float = 0.85

    # Subagents / Tools
    max_search_matches: int = 12
    file_summary_max_tokens: int = 400
    permission_default: str = "ask"  # allow | ask | block

    # Vision (external — DeepSeek V4 Flash is text-only)
    vision_backend: str = "none"  # none | ollama | glm | openai | local
    vision_api_key: Optional[str] = None
    vision_model: Optional[str] = None  # e.g. "qwen3.5:2b" for the ollama backend
    # Ollama daemon. None = fall back to OLLAMA_HOST, then localhost. Set it to
    # anything reachable over HTTP — the daemon does not have to be local.
    vision_host: Optional[str] = None
    vision_max_image_dim: int = 1024  # downscale longest edge before inference (0 = off)
    vision_timeout: int = 180
    vision_num_ctx: int = 4096  # context window asked of the local model
    vision_keep_alive: str = "5m"  # how long the daemon holds the model in VRAM

    # Runtime
    stream: bool = True
    verbose: VerboseLevel = "low"
    auto_compact: bool = True
    show_tool_timing: bool = True  # per-tool duration + result size in the trace
    show_context_bar: bool = True  # context/cost status line after every turn
    project_root: Path = field(default_factory=get_project_root)
    home: Path = field(default_factory=get_home)

    def __post_init__(self):
        self.project_root = Path(self.project_root).resolve()
        self.home = Path(self.home).resolve()

    @property
    def show_answer_detail(self) -> bool:
        """mid and high: per-turn usage line and tool result previews."""
        return self.verbose in ("mid", "high")

    @property
    def show_thinking(self) -> bool:
        """high only: reasoning excerpts from the orchestrator."""
        return self.verbose == "high"

    @property
    def vision_enabled(self) -> bool:
        if self.vision_backend in ("", "none"):
            return False
        if self.vision_backend == "ollama":
            return bool(self.vision_model)
        return True


# ---------------------------------------------------------------------------
# Persisted config (config.json next to K.dat)
# ---------------------------------------------------------------------------

# Only these keys survive between sessions — everything else is per-session.
PERSISTED_KEYS = (
    "orchestrator_model",
    "subagent_model",
    "reasoning_effort",
    "thinking_enabled",
    "verbose",
    "auto_compact",
    "show_tool_timing",
    "show_context_bar",
    "permission_default",
    "vision_backend",
    "vision_model",
    "vision_host",
    "vision_max_image_dim",
    "vision_timeout",
    "vision_num_ctx",
    "vision_keep_alive",
)

# Values a saved config is allowed to take. A key missing from here accepts
# anything of the right Python type; a key present is rejected unless it matches.
_ALLOWED: dict[str, tuple[str, ...]] = {
    # A config naming a model the API won't serve is dropped rather than obeyed,
    # so an old file that still says deepseek-v4-pro falls back to the default.
    "orchestrator_model": AVAILABLE_MODELS,
    "subagent_model": AVAILABLE_MODELS,
    "reasoning_effort": ("low", "high", "max"),
    "verbose": VERBOSE_LEVELS,
    "vision_backend": ("none", "ollama", "glm", "openai", "local"),
    "permission_default": ("allow", "ask", "block"),
}


def _coerce(key: str, value: Any, default: Any) -> Any:
    """
    Validate one persisted value, returning ``None`` to mean "ignore this key".

    A config file edited by hand — or written by an older version — should never
    put the app into a state its own commands cannot describe.
    """
    if key == "verbose" and isinstance(value, bool):
        # Older configs stored a bool; map it onto the three-level scale.
        return "high" if value else "low"

    allowed = _ALLOWED.get(key)
    if allowed is not None:
        return value if value in allowed else None

    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int) and not isinstance(value, bool):
        return int(value) if isinstance(value, (int, float)) else None
    if default is None or isinstance(default, str):
        if value is None or isinstance(value, str):
            return value
        return None
    return value


def load_config(target: Optional["Settings"] = None) -> dict[str, Any]:
    """Apply config.json onto the settings object. Missing/broken file is a no-op."""
    target = target if target is not None else settings
    path = get_config_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    applied = {}
    for key in PERSISTED_KEYS:
        if key not in data:
            continue
        value = _coerce(key, data[key], getattr(target, key, None))
        if value is None and data[key] is not None:
            continue  # invalid — keep the built-in default
        setattr(target, key, value)
        applied[key] = value
    return applied


def save_config(target: Optional["Settings"] = None) -> Path:
    """Write the persisted subset of settings to config.json."""
    target = target if target is not None else settings
    path = get_config_file()
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            pass
    existing.update({key: getattr(target, key) for key in PERSISTED_KEYS})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return path


# Global settings instance (mutable at runtime via slash commands)
settings = Settings()
load_config(settings)
