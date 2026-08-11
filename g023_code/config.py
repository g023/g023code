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

# Bounds on the per-turn tool loop. One is a legitimate setting — a single model
# call, no tool follow-up. There is no upper bound: a long job is a long job, and
# a cap the user cannot raise past turns work into "Stopped after N iterations".
# ``max_iterations = 0`` means run until the model answers, with no step limit at
# all. What replaces the ceiling is visibility, not a smaller number: the loop
# reports where it is (see ITERATION_NOTICE_EVERY) so an uncapped run is watched
# rather than silent, and Ctrl-C ends the turn.
MIN_ITERATIONS = 1
UNLIMITED_ITERATIONS = 0

# Bounds on one reply's output budget, in tokens. The same reasoning as the step
# cap applies, for the same reason: a ceiling the user cannot raise turns a long
# answer into a sentence that stops mid-word. ``max_tokens = 0`` sends no
# ``max_output_tokens`` at all, which leaves the ceiling to the model's own
# maximum rather than to a number this client picked. That is the default.
MIN_OUTPUT_TOKENS = 1
UNLIMITED_OUTPUT_TOKENS = 0

# How often the loop prints a progress line, in steps. Every step already updates
# the spinner, but that line is transient; this one stays in the scrollback, at
# any verbosity, so a turn that is 60 steps deep says so on its own.
ITERATION_NOTICE_EVERY = 10


@dataclass
class Settings:
    # Models
    orchestrator_model: ModelId = "deepseek-v4-flash"
    subagent_model: ModelId = "deepseek-v4-flash"
    reasoning_effort: ReasoningEffort = "high"
    thinking_enabled: bool = True

    # API
    base_url: str = "https://api.deepseek.com"
    # Per-reply output budget. 0 means the request omits ``max_output_tokens``
    # entirely and the model's own maximum is the only ceiling — see
    # UNLIMITED_OUTPUT_TOKENS. A positive value is sent as-is, however large —
    # what happens to one the model cannot honour is the server's to decide.
    max_tokens: int = UNLIMITED_OUTPUT_TOKENS
    temperature: float = 0.2

    # Web search is DeepSeek's server-side ``web_search`` tool, offered to the
    # model as a peer of our own tools on every orchestrator call — there is no
    # separate search request to configure. The server may run a long agentic
    # loop (5-20 searches plus page opens) inside a single turn, which is why the
    # client timeout in api.py is generous.

    # Context
    max_context_tokens: int = 900_000  # leave headroom under 1M
    compact_threshold: float = 0.85

    # Agent loop: how many model calls one user turn may spend before the loop
    # gives up. Every step that returns tool calls costs another round trip, so
    # this is the backstop against a turn that tools forever without answering.
    # 0 removes the backstop entirely — see UNLIMITED_ITERATIONS.
    max_iterations: int = 255

    # Subagents / Tools
    max_search_matches: int = 12
    file_summary_max_tokens: int = 400
    permission_default: str = "ask"  # allow | ask | block
    # Baseline mode for measuring what delegation actually costs: ReadFile
    # returns raw file content instead of a summary, so the same task can be run
    # both ways and the two /cost readings compared. Not a normal setting — it
    # turns the central design decision off. Off unless G023_READFILE_RAW is set.
    file_reader_raw: bool = field(
        default_factory=lambda: os.environ.get("G023_READFILE_RAW", "").lower()
        in ("1", "true", "yes", "on")
    )

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
    "max_iterations",
    "max_tokens",
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


def describe_steps(count: int) -> str:
    """How a step cap reads to the user — one wording, used everywhere."""
    return "unlimited" if count == UNLIMITED_ITERATIONS else str(count)


def describe_max_tokens(count: int) -> str:
    """How an output budget reads to the user — one wording, used everywhere."""
    if count == UNLIMITED_OUTPUT_TOKENS:
        return "unlimited (model maximum)"
    return f"{count:,}"


def _coerce(key: str, value: Any, default: Any) -> Any:
    """
    Validate one persisted value, returning ``None`` to mean "ignore this key".

    A config file edited by hand — or written by an older version — should never
    put the app into a state its own commands cannot describe.
    """
    if key == "verbose" and isinstance(value, bool):
        # Older configs stored a bool; map it onto the three-level scale.
        return "high" if value else "low"

    if key == "max_iterations":
        # Any positive step count is honoured, however large, and 0 means "no
        # limit". Only a negative or non-numeric value is a broken config, and
        # that keeps the built-in default rather than being obeyed.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if value >= UNLIMITED_ITERATIONS else None

    if key == "max_tokens":
        # Same shape as max_iterations: any non-negative budget is honoured, 0
        # means "no client-side cap", and only a negative or non-numeric value
        # is a broken config that keeps the built-in default.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if value >= UNLIMITED_OUTPUT_TOKENS else None

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
        current = getattr(target, key, None)
        value = _coerce(key, data[key], current)
        if value is None and current is not None:
            # Either _coerce rejected the value, or the file holds an explicit
            # null for a key that has no meaningful null (only the optional
            # vision keys default to None, and those still accept one). Keep the
            # built-in default rather than writing None into a typed field.
            continue
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
