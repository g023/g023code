"""
Configuration and environment for g023 Code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Union

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


def get_project_config_file() -> Path:
    """Per-project config (inside the project's .g023/ folder).

    When it exists it overrides the machine-level config.json, so a project
    can pin its own model / vision / surge settings while the machine-level
    file keeps the global defaults. ``/config`` manages the split.
    """
    return get_project_root() / ".g023" / "config.json"


# ---------------------------------------------------------------------------
# API Keys
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


def load_openrouter_key() -> str:
    """Load OpenRouter API key from OK.dat inside the program folder."""
    key_file = get_home() / "OK.dat"
    if not key_file.exists():
        raise FileNotFoundError(
            f"OpenRouter API key file not found: {key_file}\n"
            "Create OK.dat in the g023-code folder and put your OpenRouter API key on the first line."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    if not key or key in (
        "YOUR_OPENROUTER_API_KEY_HERE",
        "sk-or-v1-REPLACE-WITH-YOUR-OPENROUTER-KEY",
    ):
        raise ValueError(
            f"Please put a valid OpenRouter API key into {key_file}\n"
            "Get one at https://openrouter.ai/keys"
        )
    return key


# ---------------------------------------------------------------------------
# Model & Runtime Settings
# ---------------------------------------------------------------------------

ModelId = Literal["deepseek-flash", "deepseek-v4-pro"]
# Stored as int 0–75 (0 = off; 75 is default and ceiling). String aliases
# coerce on load / ``/thinking``; ``max`` / 100 cap to 75.
ReasoningEffort = Union[int, str]

# Models the Responses API will actually serve. Everything here runs on
# ``/responses`` — that is the only endpoint exposing DeepSeek's server-side
# web_search. The configured/sent identity is ``deepseek-flash`` (orchestrator
# and DeepSeek vision both use it). ``deepseek-v4-pro`` is withheld: the
# endpoint still reports it is not enabled for this integration.
AVAILABLE_MODELS: tuple[str, ...] = ("deepseek-flash",)
UNAVAILABLE_MODELS: dict[str, str] = {
    "deepseek-v4-pro": (
        "DeepSeek has not enabled deepseek-v4-pro on the Responses API yet "
        "(it reports availability from early August 2026)."
    ),
}
# Retired ids still found in older config.json files. Mapped onto the live
# identity on load so an old file never sends a name the API no longer serves.
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
_MODEL_ALIAS_KEYS = (
    "orchestrator_model",
    "subagent_model",
    "deepseek_vision_model",
    "vision_model",
)

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

# Bounds on the context window and the auto-compact trigger. The window is the
# ceiling the conversation may grow into before the cheap model summarises it:
# ``max_context_tokens`` is that ceiling in tokens (DeepSeek's real limit is
# 1M; the shipped default is 256k so compaction stays continuation-safe),
# ``compact_threshold`` the fraction of it at which auto-compaction fires. A
# 0-token window is meaningless, a threshold of 0 would compact every turn, and
# one >1 would never fire.
MIN_CONTEXT_TOKENS = 1_000
MAX_CONTEXT_TOKENS = 1_000_000
DEFAULT_CONTEXT_TOKENS = 256_000
MIN_COMPACT_THRESHOLD = 0.01
MAX_COMPACT_THRESHOLD = 1.0

# How often the loop prints a progress line, in steps. Every step already updates
# the spinner, but that line is transient; this one stays in the scrollback, at
# any verbosity, so a turn that is 60 steps deep says so on its own.
ITERATION_NOTICE_EVERY = 10

# When a reply is cut off at max_output_tokens with no tool calls, the turn used
# to end mid-thought — fatal for a long /goal. Nudge and keep going this many
# times in one turn, then stop so a model that cannot finish still surfaces.
MAX_OUTPUT_CONTINUES = 3

# OpenRouter fallback when no model is configured for the openrouter backend.
OPENROUTER_DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"
# OpenRouter free-tier daily ceiling for :free models. Free accounts get 50
# requests/day; buying $10+ lifetime credits raises the ceiling to 1000/day.
# (Both tiers still share the 20 req/min cap.)
OPENROUTER_FREE_DAILY_LIMIT = 50
OPENROUTER_CREDITS_DAILY_LIMIT = 1000

# Vision endpoints for the deepseek backend. The Responses format is the
# program's native format; chat_completions is offered because the vision model
# also speaks it (OpenRouter only speaks chat_completions, which is fine — it
# pins its own endpoint internally).
VISION_ENDPOINTS: tuple[str, ...] = ("responses", "chat_completions")


def _load_ds_times() -> tuple[tuple[tuple[int, int], ...], int, tuple[int, ...]]:
    """Surge windows + UTC offset + weekdays from ds_times.json.

    Falls back to DeepSeek's published sheet when the file is absent or
    unreadable. Windows are parsed from ``HH:MM`` strings into Beijing
    wall-clock hours; ``surge.py`` converts them to the machine's local time.
    ``peak_weekdays`` uses Python ``datetime.weekday()`` (Monday=0). An empty
    or missing list means Monday–Friday, matching the published sheet.
    """
    default_windows: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))
    default_offset = 8
    default_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    for path in (
        get_home() / "ds_times.json",
        get_project_root() / "ds_times.json",
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        windows = data.get("peak_windows", {}).get("beijing")
        if not windows:
            continue
        try:
            parsed = tuple(
                (int(start.split(":")[0]), int(end.split(":")[0]))
                for start, end in windows
            )
        except (AttributeError, TypeError, ValueError):
            continue
        offset = data.get("utc_offset_hours", default_offset)
        if not isinstance(offset, int):
            offset = default_offset
        raw_days = data.get("peak_weekdays", default_weekdays)
        try:
            weekdays = tuple(int(d) for d in raw_days)
        except (TypeError, ValueError):
            weekdays = default_weekdays
        if not weekdays:
            weekdays = default_weekdays
        return parsed, offset, weekdays
    return default_windows, default_offset, default_weekdays


# Surge pricing windows, DeepSeek peak/off-peak (tidal) pricing effective
# 2026-09-10. Peak hours are 09:00-12:00 and 14:00-18:00 Beijing time (UTC+8),
# Monday–Friday; weekends are off-peak. Off-peak prices are half of peak. The
# windows below are stored in *Beijing* wall-clock hours and converted to the
# machine's local time by ``surge.py``, so notifications always match the
# user's clock. The values come from ds_times.json when present.
SURGE_PEAK_WINDOWS_BEIJING: tuple[tuple[int, int], ...]
SURGE_UTC_OFFSET: int
SURGE_PEAK_WEEKDAYS: tuple[int, ...]
SURGE_PEAK_WINDOWS_BEIJING, SURGE_UTC_OFFSET, SURGE_PEAK_WEEKDAYS = _load_ds_times()



def _env_int(name: str, default: int) -> int:
    """Integer from the environment; falls back to ``default`` when absent or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass
class Settings:
    # Models
    orchestrator_model: ModelId = "deepseek-flash"
    subagent_model: ModelId = "deepseek-flash"
    reasoning_effort: ReasoningEffort = 75
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
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS  # DeepSeek allows up to 1M
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

    # WriteFile safety net: before overwriting a file, keep a timestamped copy
    # of the previous version in .g023/backups/ (mirroring the file tree, with
    # only the newest few versions retained per file). Skipped for files inside
    # .g023/ itself, for brand-new files, and when the content is unchanged.
    # Disable with G023_WRITE_BACKUP=0|false|no|off; tune retention with
    # G023_WRITE_BACKUP_KEEP (minimum 1).
    write_backup: bool = field(
        default_factory=lambda: os.environ.get("G023_WRITE_BACKUP", "").lower()
        not in ("0", "false", "no", "off")
    )
    write_backup_keep: int = field(default_factory=lambda: _env_int("G023_WRITE_BACKUP_KEEP", 5))

    # Vision (optional backends; deepseek reuses deepseek-flash on K.dat)
    vision_backend: str = "none"  # none | ollama | deepseek | openrouter
    vision_api_key: Optional[str] = None
    vision_model: Optional[str] = None  # e.g. "qwen3.5:2b" for the ollama backend
    # Ollama daemon. None = fall back to OLLAMA_HOST, then localhost. Set it to
    # anything reachable over HTTP — the daemon does not have to be local.
    vision_host: Optional[str] = None
    vision_max_image_dim: int = 800  # display/legacy; vision send is always 800px constrained
    vision_timeout: int = 180
    vision_num_ctx: int = 4096  # context window asked of the local model
    vision_keep_alive: str = "5m"  # how long the daemon holds the model in VRAM

    # Wire shape for the deepseek backend: the native Responses format vs the
    # OpenAI-style chat_completions format (see VISION_ENDPOINTS).
    vision_endpoint: str = VISION_ENDPOINTS[0]
    # Backend defaults used when no explicit vision_model is set.
    deepseek_vision_model: str = "deepseek-flash"
    or_default_model: str = OPENROUTER_DEFAULT_MODEL
    # Explicit model list, usually set by /or models from OR_VISION.json.
    or_vision_models: Optional[list[str]] = None
    # True once the user bought $10+ lifetime credits → 1000 req/day ceiling.
    or_credits: bool = False

    # OpenRouter subagent offload (/orsub on): the Agent tool's Explore/Plan
    # turns run on the OR_SUB_MODELS.json list (primary first, hopping through
    # the rest on rate limits) instead of the DeepSeek subagent model.
    or_sub_enabled: bool = False

    # Surge pricing (DeepSeek peak/off-peak, Beijing 09-12 & 14-18)
    # Notify when the API is at surge pricing; optionally pause usage from
    # ``surge_pause_buffer_minutes`` before peak starts until that long after
    # the last peak ends.
    surge_notify: bool = True
    surge_pause: bool = False
    surge_pause_buffer_minutes: int = 5

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
        if self.vision_backend == "openrouter":
            return (self.home / "OK.dat").exists()
        # deepseek is billed through K.dat; without the key it cannot be enabled.
        return (self.home / "K.dat").exists()

    @property
    def effective_vision_model(self) -> Optional[str]:
        """The model a vision call will actually use, for the current backend."""
        if self.vision_backend == "deepseek":
            return self.vision_model or self.deepseek_vision_model
        if self.vision_backend == "openrouter":
            return self.vision_model or self.or_default_model
        return self.vision_model


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
    "max_context_tokens",
    "compact_threshold",
    "vision_backend",
    "vision_model",
    "vision_host",
    "vision_max_image_dim",
    "vision_timeout",
    "vision_num_ctx",
    "vision_keep_alive",
    "vision_endpoint",
    "deepseek_vision_model",
    "or_default_model",
    "or_vision_models",
    "or_credits",
    "or_sub_enabled",
    "surge_notify",
    "surge_pause",
    "surge_pause_buffer_minutes",
    "write_backup",
    "write_backup_keep",
)

# Values a saved config is allowed to take. A key missing from here accepts
# anything of the right Python type; a key present is rejected unless it matches.
_ALLOWED: dict[str, tuple[str, ...]] = {
    # A config naming a model the API won't serve is remapped (retired flash
    # ids via MODEL_ALIASES) or dropped (deepseek-v4-pro) rather than obeyed.
    "orchestrator_model": AVAILABLE_MODELS,
    "subagent_model": AVAILABLE_MODELS,
    "verbose": VERBOSE_LEVELS,
    "vision_backend": ("none", "ollama", "deepseek", "openrouter"),
    "vision_endpoint": VISION_ENDPOINTS,
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


def describe_context_window(count: int) -> str:
    """How the context ceiling reads to the user — one wording, used everywhere."""
    return f"{count:,} tokens"


def describe_compact_threshold(fraction: float) -> str:
    """How the auto-compact trigger reads — one wording, used everywhere."""
    return f"{fraction:.0%} of the window"


def _coerce(key: str, value: Any, default: Any) -> Any:
    """
    Validate one persisted value, returning ``None`` to mean "ignore this key".

    A config file edited by hand — or written by an older version — should never
    put the app into a state its own commands cannot describe.
    """
    if key == "verbose" and isinstance(value, bool):
        # Older configs stored a bool; map it onto the three-level scale.
        return "high" if value else "low"

    if key in _MODEL_ALIAS_KEYS and isinstance(value, str):
        value = MODEL_ALIASES.get(value, value)

    if key == "reasoning_effort":
        # 0–100 on the user/model scale, or an alias (including old "high"
        # / "max" configs). Values above 75 cap to 75. Invalid values keep
        # the built-in default (75).
        from .effort import parse_effort_value

        return parse_effort_value(value)

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

    if key == "max_context_tokens":
        # The window is a ceiling in tokens: anything within the model's real
        # range is honoured, and a broken value keeps the built-in default.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if MIN_CONTEXT_TOKENS <= value <= MAX_CONTEXT_TOKENS else None

    if key == "compact_threshold":
        # The fraction of the window that trips auto-compaction. Anything in
        # (0, 1] is honoured; 0 would compact every turn and >1 never fires.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if MIN_COMPACT_THRESHOLD <= value <= MAX_COMPACT_THRESHOLD else None

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
    """Apply config.json onto the settings object; a project file overrides.

    The machine-level file (next to K.dat) holds the global defaults; when the
    project's ``.g023/config.json`` exists it is applied afterwards, so a
    project pins its own settings while the machine file keeps the defaults.
    Missing/broken files are a no-op. Returns the dict of applied keys.
    """
    target = target if target is not None else settings
    applied: dict[str, Any] = {}
    for path in (get_config_file(), get_project_config_file()):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key in PERSISTED_KEYS:
            if key not in data:
                continue
            current = getattr(target, key, None)
            value = _coerce(key, data[key], current)
            if value is None and current is not None:
                # Either _coerce rejected the value, or the file holds an explicit
                # null for a key that has no meaningful null (only the optional
                # vision keys default to None, and those still accept one). Keep
                # the built-in default rather than writing None into a typed field.
                continue
            setattr(target, key, value)
            applied[key] = value
    return applied


def save_config(target: Optional["Settings"] = None, scope: str = "auto") -> Path:
    """Write the persisted subset of settings to config.json.

    ``scope`` is "auto" (the project's .g023/config.json when it exists, else
    the machine file next to K.dat), "global" (the machine file — what new
    projects inherit), or "project" (pin to this project only). ``/config``
    manages the split.
    """
    target = target if target is not None else settings
    if scope == "global":
        path = get_config_file()
    elif scope == "project":
        path = get_project_config_file()
    else:  # auto
        path = get_project_config_file() if get_project_config_file().exists() else get_config_file()
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
