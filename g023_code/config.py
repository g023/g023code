"""
Configuration and environment for g023 Code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Sequence, Union

from .fsutil import atomic_write_text, file_lock

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


def get_global_cache_dir() -> Path:
    """Machine-wide cache folder in the install directory (next to K.dat).

    Shared by every project and every running instance, so a web search or
    page paid for once is free everywhere. ``G023_GLOBAL_CACHE_DIR`` overrides.
    """
    env = os.environ.get("G023_GLOBAL_CACHE_DIR")
    return Path(env).resolve() if env else get_home() / "global_cache"


def get_cache_db() -> Path:
    return get_scratch_dir() / "cache.db"


def get_config_file() -> Path:
    """
    Machine-level config (next to K.dat): the global defaults every project
    inherits unless it pins its own in ``.g023/config.json``.
    """
    return get_home() / "config.json"


def get_project_config_file() -> Path:
    """Per-project config (inside the project's .g023/ folder).

    When it exists it overrides the machine-level config.json, so a project
    can pin its own vision / surge / effort settings while the machine-level
    file keeps the global defaults. ``/config`` manages the split.
    """
    return get_project_root() / ".g023" / "config.json"


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


def load_api_key() -> str:
    """Load DeepSeek API key from K.dat inside the program folder."""
    from .ui import display_path  # lazy: ui pulls in rich; shown as ~/... (no account name)

    key_file = get_home() / "K.dat"
    if not key_file.exists():
        raise FileNotFoundError(
            f"API key file not found: {display_path(key_file)}\n"
            "Create K.dat in the g023-code folder and put your DeepSeek API key on the first line."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    # The installers leave a placeholder behind when the user skips the key
    # prompt; recognising it turns a confusing 401 into a clear instruction.
    if not key or key in ("YOUR_DEEPSEEK_API_KEY_HERE", "sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY"):
        raise ValueError(
            f"Please put a valid DeepSeek API key into {display_path(key_file)}\n"
            "Get one at https://platform.deepseek.com/"
        )
    return key


# ---------------------------------------------------------------------------
# Model & Runtime Settings
# ---------------------------------------------------------------------------

# The one model this program speaks to: DeepSeek V4.1 Flash
# (DeepSeek-V4.1-Flash-1017, served as ``deepseek-flash``), over DeepSeek's
# Anthropic-format Messages API. It is a constant, not a setting, so no config
# file can name a model the API will not serve. 1M context, vision-capable
# (image blocks in user / tool_result content).
MODEL = "deepseek-flash"
MODEL_LABEL = "DeepSeek V4.1 Flash"
# Anthropic-format Messages endpoint, relative to ``Settings.base_url``.
MESSAGES_PATH = "/anthropic/v1/messages"

# Stored as int 0–75 (0 = off; 75 is default and ceiling). String aliases
# coerce on load / ``/thinking``; ``max`` / 100 cap to 75.
ReasoningEffort = Union[int, str]

# Auto-compaction trigger in tokens (0 = use compact_threshold of the window).
DEFAULT_COMPACT_AT_TOKENS = 200_000
# ``economic`` also compacts earlier, once re-reading the history has cost
# more than compacting it would (g023_code/economics.py), never below
# ``compact_floor_tokens``. ``fixed`` uses only the token trigger.
COMPACT_POLICIES: tuple[str, ...] = ("economic", "fixed")
DEFAULT_COMPACT_FLOOR_TOKENS = 64_000

# Keys older versions wrote to config.json that no longer mean anything: other
# providers (OpenRouter, Ollama), other wires (/responses, chat completions) and
# per-role model choices. They are ignored on load and removed on the next save.
# ``vision_backend: "none"`` is the one with a meaning worth keeping — it is
# migrated to ``vision_enabled = False`` (see ``_migrate_legacy``).
LEGACY_KEYS: frozenset[str] = frozenset(
    {
        "api_format",
        "orchestrator_model",
        "subagent_model",
        "deepseek_vision_model",
        "vision_backend",
        "vision_model",
        "vision_host",
        "vision_num_ctx",
        "vision_keep_alive",
        "vision_endpoint",
        "vision_max_image_dim",
        "or_default_model",
        "or_vision_models",
        "or_credits",
        "or_sub_enabled",
        # 1.5.0: web access is DeepSeek's Search + Fetch APIs only, as
        # client tools behind the machine-wide cache (g023_code/web.py). The
        # server-side web_search and the local browser engine are gone.
        "fetch_backend",
        "web_search_max_uses",
    }
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
# 1M and is the default; ``compact_at_tokens`` (200k) is the normal trigger),
# ``compact_threshold`` the fraction of it at which auto-compaction fires. A
# 0-token window is meaningless, a threshold of 0 would compact every turn, and
# one >1 would never fire.
MIN_CONTEXT_TOKENS = 1_000
MAX_CONTEXT_TOKENS = 1_000_000
DEFAULT_CONTEXT_TOKENS = 1_000_000
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

# After a /goal turn the judge may still say not completed. Run at most this
# many extra turns that name the open leaves. A continuation that does not
# change the working set stops early — another identical refusal is output
# tokens spent on the same unfinished answer.
GOAL_CONTINUATION_CAP = 2

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


def _load_ds_holidays() -> frozenset:
    """Beijing calendar dates (``YYYY-MM-DD``) that are off-peak all day.

    DeepSeek's sheet says peak hours exclude Chinese public holidays but does
    not publish the dates, so none are shipped: list them under ``"holidays"``
    in ds_times.json (first readable file wins, same search order as above).
    Malformed entries are ignored.
    """
    from datetime import date

    for path in (get_home() / "ds_times.json", get_project_root() / "ds_times.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out = set()
        for raw in data.get("holidays") or []:
            try:
                out.add(date.fromisoformat(str(raw)).isoformat())
            except ValueError:
                continue
        return frozenset(out)
    return frozenset()


SURGE_HOLIDAYS: frozenset = _load_ds_holidays()


LOCAL_MODES = ("auto", "on", "off")


def _local_mode_env() -> str:
    """G023_LOCAL=1/0 -> "on"/"off"; unset or unrecognised -> "auto"."""
    raw = os.environ.get("G023_LOCAL", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "on"
    if raw in ("0", "false", "no", "off"):
        return "off"
    return "auto"


def _env_flag(name: str, default: bool) -> bool:
    """On/off from the environment; unset or unrecognised keeps ``default``."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


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
    # Reasoning (the model itself is the MODEL constant)
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

    # Web access: WebSearch / FetchUrl call DeepSeek's Search and Fetch APIs
    # through the machine-wide cache in get_global_cache_dir() (g023_code/web.py).

    # Context
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS  # DeepSeek allows up to 1M
    compact_threshold: float = 0.85
    # Absolute trigger: auto-compact (checked between every model step, not
    # only at turn start) once the live context reaches this many tokens. The
    # fraction above still applies as a ceiling when the window is smaller.
    compact_at_tokens: int = DEFAULT_COMPACT_AT_TOKENS
    compact_policy: str = "economic"
    compact_floor_tokens: int = DEFAULT_COMPACT_FLOOR_TOKENS

    # Agent loop: how many model calls one user turn may spend before the loop
    # gives up. Every step that returns tool calls costs another round trip, so
    # this is the backstop against a turn that tools forever without answering.
    # 0 removes the backstop entirely — see UNLIMITED_ITERATIONS.
    max_iterations: int = 255

    # Subagents / Tools
    # Single-agent by default: the Agent tool is hidden and ReadFile's
    # ``focus`` is answered locally (no LLM). /subagents on restores both.
    subagents_enabled: bool = False
    # Code ownership (g023_code/ownership.py): "auto" lets the model tidy files
    # it created itself (merge repeats, fix nearby defects); "user" keeps every
    # file minimal-change; "agent" extends the tidy-up to the whole project.
    ownership: str = "auto"
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
    # Verify replay: after a successful Edit/WriteFile, re-run the check
    # command (tests/linters) that last failed this turn and attach its exit
    # code + output tail to the write's result, saving the model a hop. Only
    # when Bash is set to allow and the check took < 30 s. G023_VERIFY_REPLAY=0 disables.
    verify_replay: bool = field(
        default_factory=lambda: os.environ.get("G023_VERIFY_REPLAY", "").lower()
        not in ("0", "false", "no", "off")
    )
    # Failure attribution: when a check fails after this turn's writes, re-run
    # it in a disposable shadow copy of the project with the turn's pre-edit
    # file images restored, and label each failure new or pre-existing
    # (g023_code/attribution.py). Same gate as verify replay (Bash allow,
    # check < 30 s). G023_ATTRIBUTION=0 disables.
    failure_attribution: bool = field(
        default_factory=lambda: os.environ.get("G023_ATTRIBUTION", "").lower()
        not in ("0", "false", "no", "off")
    )

    # Predicated finish (g023_code/finish.py): Bash/Edit/WriteFile take an
    # optional final_answer, delivered only when that step succeeds, so the
    # turn ends without an answer-only model call. On by default: measured
    # live together with step_budget (7 task shapes, n=35 each, all solved):
    # $ -8 %, calls -16 %, wall time -10 % (docs/hops.md). G023_FINISH_ON_SUCCESS=0
    # disables; when off the schemas and standing prompt are exactly as before.
    finish_on_success: bool = field(default_factory=lambda: _env_flag("G023_FINISH_ON_SUCCESS", True))

    # Lean-turn features (docs/lean.md). Measured live together on 7 task
    # shapes x 8 runs: $/solved -26% (output -32%, calls -13%), 56/56 solved.
    # Each has an env override (=0 disables).
    # Closure receipts (g023_code/closure.py): an Edit result lists whole-token
    # references left to the names/numbers it removed (0 = none left), so a
    # rename needs no confirming grep. G023_CLOSURE_RECEIPTS.
    closure_receipts: bool = field(default_factory=lambda: _env_flag("G023_CLOSURE_RECEIPTS", True))
    # Lean codec (g023_code/tool_codec.py): Edit/WriteFile results drop the
    # backup path; Bash output follows a JSON header raw, not JSON-escaped.
    # G023_LEAN_CODEC.
    lean_codec: bool = field(default_factory=lambda: _env_flag("G023_LEAN_CODEC", True))
    # Lean prompt (orchestrator.lean_system): drops a line that contradicted
    # the batching rule; final answers skip what the trace already shows.
    # G023_LEAN_PROMPT.
    lean_prompt: bool = field(default_factory=lambda: _env_flag("G023_LEAN_PROMPT", True))
    # Orientation card (g023_code/orient.py): names and paths the request
    # mentions are looked up locally and one [where: ...] line rides on the
    # user message. G023_ORIENT_CARD.
    orient_card: bool = field(default_factory=lambda: _env_flag("G023_ORIENT_CARD", True))
    # Scope count (subagents/searcher.py): a SearchContent scoped to a
    # subdirectory also reports elsewhere_in_project (hits outside it), so
    # no widening re-search. G023_SCOPE_COUNT.
    scope_count: bool = field(default_factory=lambda: _env_flag("G023_SCOPE_COUNT", False))
    # Search tiers (subagents/searcher.py): hits rank by what the line defines
    # — a def/class named by the query, then module-level names, then local
    # assignments, then plain uses — before file order. G023_SEARCH_TIERS.
    search_tiers: bool = field(default_factory=lambda: _env_flag("G023_SEARCH_TIERS", False))
    # Definition peek (subagents/searcher.py): the strongest definition hit's
    # body (bounded) rides on the SearchContent result. G023_DEF_PEEK.
    def_peek: bool = field(default_factory=lambda: _env_flag("G023_DEF_PEEK", False))
    # View deltas (g023_code/views.py): a Bash call that changes a file the
    # model has read shows a diff against that view. G023_VIEW_DELTAS.
    view_deltas: bool = field(default_factory=lambda: _env_flag("G023_VIEW_DELTAS", True))
    # Test oracle (g023_code/testoracle.py): a pytest run through Bash also
    # writes a structured report (outside the project); the result carries the
    # facts its own output filter (| tail) hid: failing ids, where each failure
    # happened, which failing tests this turn added. G023_TEST_ORACLE.
    test_oracle: bool = field(default_factory=lambda: _env_flag("G023_TEST_ORACLE", True))
    # Vacuity check: new passing tests are re-run against the turn's pre-edit
    # source in a shadow copy; passing there too = they do not test the change.
    # G023_VACUITY.
    vacuity_check: bool = field(default_factory=lambda: _env_flag("G023_VACUITY", True))
    # Request obligations (g023_code/obligations.py): a callable the request
    # names (``name(params)``) is checked against the code before the answer
    # is accepted; one corrective note when it is missing or misshapen.
    # G023_OBLIGATIONS.
    obligations: bool = field(default_factory=lambda: _env_flag("G023_OBLIGATIONS", True))
    # Write guards: "don't change the tests"-style constraints in the request
    # refuse the first write to a protected file (repeat the call to proceed).
    # G023_WRITE_GUARDS.
    write_guards: bool = field(default_factory=lambda: _env_flag("G023_WRITE_GUARDS", True))
    # Callee digests: ReadFile of a Python file lists the signatures of the
    # project-local names it imports (``uses:`` rows). G023_CALLEE_DIGEST.
    callee_digest: bool = field(default_factory=lambda: _env_flag("G023_CALLEE_DIGEST", True))
    # Claim check: an answer that says the tests pass is compared with the
    # turn's last test run; a mismatch adds a visible note. G023_CLAIM_CHECK.
    claim_check: bool = field(default_factory=lambda: _env_flag("G023_CLAIM_CHECK", True))

    # Round-trip economics (docs/hops.md). A model call's price under prefix
    # caching is mostly the re-thinking it produces, so these cut calls.
    # Ordered batches: the prompt states the program-order contract (put a
    # write and the check of it in one response) and a failed write
    # short-circuits the later Bash/same-file writes of that response
    # (&& semantics). G023_ORDERED_BATCHES.
    ordered_batches: bool = field(default_factory=lambda: _env_flag("G023_ORDERED_BATCHES", False))
    # Step budgets (hops.py): the model may declare [budget MIN-MAX +OVER]
    # model calls for the step it is starting; each batch's last result
    # carries a hop/cost tracker line, and an overrun past MAX+OVER gets one
    # wrap-up note. On by default (with finish_on_success, see above).
    # G023_STEP_BUDGET=0 disables.
    step_budget: bool = field(default_factory=lambda: _env_flag("G023_STEP_BUDGET", True))
    # Write checks (hops.py): Edit/WriteFile take an optional ``check`` command
    # that runs after the response's last write, only if every write in it
    # succeeded, and returns with the write. G023_WRITE_CHECK.
    write_check: bool = field(default_factory=lambda: _env_flag("G023_WRITE_CHECK", False))
    # Pre-emptive reads (preread.py). ``preread``: before the first model call
    # of a turn the harness runs the ReadFile calls the request makes
    # predictable (named small files, the project modules a named test file
    # imports, a named source's test file, the unique definition of a named
    # identifier; one SearchContent for a rename) and records them as the
    # model's own tool exchange ("ghost", default), or appends the same text
    # to the request ("prose"), or not at all ("off"). G023_PREREAD.
    preread: str = field(default_factory=lambda: os.environ.get("G023_PREREAD", "ghost"))
    # Counterfactual-licensed finish (finish.py): a final_answer on a failed
    # check is still delivered when the failure is not this turn's — every
    # failure also fails with the turn's edits reverted (attribution), the
    # request is not about those tests, and the answer claims no clean run.
    # The answer is stamped with a warning line naming them. On by default
    # (measured with the four keys below: $/solved -20 %). G023_FINISH_LICENSED=0.
    finish_licensed: bool = field(default_factory=lambda: _env_flag("G023_FINISH_LICENSED", True))
    # Claimed-write guard (finish.unwritten_claims): a final_answer that says a
    # file the request names was changed is held back when no call this turn
    # wrote that file. On by default. G023_CLAIMED_WRITE_GUARD=0.
    claimed_write_guard: bool = field(default_factory=lambda: _env_flag("G023_CLAIMED_WRITE_GUARD", True))
    # Answer escrow (finish.py): a discarded final_answer is kept; the result
    # says so, and the model can release it by replying "[deliver]" (plus an
    # optional added line) instead of writing it again. On by default.
    # G023_ANSWER_ESCROW=0.
    answer_escrow: bool = field(default_factory=lambda: _env_flag("G023_ANSWER_ESCROW", True))
    # Multi-hunk Edit (multi_edit.py): Edit takes an optional ``more_edits``
    # list of further replacements (an item may name another file), applied in
    # order, all files or none. On by default. G023_MULTI_EDIT=0.
    multi_edit: bool = field(default_factory=lambda: _env_flag("G023_MULTI_EDIT", True))
    # Rename tool (rename.py): one call renames an identifier across the
    # project (whole-word, all files or none) instead of grep/sed hop loops.
    # On by default. G023_RENAME_TOOL=0.
    rename_tool: bool = field(default_factory=lambda: _env_flag("G023_RENAME_TOOL", True))

    # Vision: AnalyzeImage sends the image to the same model (deepseek-flash
    # accepts image blocks) in one isolated call and returns only the answer,
    # so image tokens never enter the main transcript. On by default — it needs
    # nothing beyond K.dat; /vision off hides the tool.
    vision_enabled: bool = True
    vision_timeout: int = 180

    # Salient vault excerpts (salience.py): a bulky Bash/ListDir/Glob result keeps
    # the lines matching what the step said it was looking for, and the full body
    # is a readable .g023/vault/ file. G023_SALIENCE=0 restores head+tail.
    salience_excerpt: bool = field(default_factory=lambda: _env_flag("G023_SALIENCE", True))
    # Local model (local_llm.py): llama.cpp llama-server for free helper work
    # (scout card, LocalAsk bulk reads, salience rerank, local-first
    # AnalyzeImage). "auto" (default): on when autotune.py finds llama-server,
    # a model and a GPU it fits on; "on" forces it, "off" never. G023_LOCAL=1/0
    # -> on/off. Paths accept ~ and $VARS. Empty local_model_file /
    # local_vision_file = pick automatically (autotune.KNOWN, measured);
    # local_vision_file "off" = DeepSeek vision only.
    local_model: str = field(default_factory=lambda: _local_mode_env())
    local_llama_dir: str = "~/llamacpp/llama.cpp"
    local_models_dir: str = "~/MODELS"
    local_model_file: str = ""
    local_vision_file: str = ""
    local_gpu_layers: int = 99
    local_ctx: int = 16384
    local_port: int = 0
    local_gpu_guard: bool = True
    # Scout card (scout.py): before the first paid call of a turn, the project is
    # ranked against the request and the local model picks the lines that locate
    # it; a short verbatim card rides on the user message. "local" = only when the
    # local model is on and free; "always" = fall back to the top keyword-matched
    # code units without it; "off". G023_SCOUT.
    scout_card: str = field(default_factory=lambda: os.environ.get("G023_SCOUT", "local"))

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
    # Exploration ledger: carry a deterministic list of files/dirs/searches seen
    # (with one-line digests) across compaction. See g023_code/ledger.py. Off by
    # default: a live A/B (explore, /compact, follow-up questions; 4 scenarios) found
    # the compaction summary already keeps those facts, so the item only added
    # ~1.5k chars per request. Opt in for very long sessions.
    exploration_ledger: bool = False
    show_tool_timing: bool = True  # per-tool duration + result size in the trace
    show_context_bar: bool = True  # context/cost status line after every turn
    project_root: Path = field(default_factory=get_project_root)
    home: Path = field(default_factory=get_home)

    # Transient overlay: key -> the value config.json should keep while the
    # live attribute carries a per-turn override ([next_effort], /goal).
    # Invariant: save_config never persists a transient value. A plain
    # assignment (a user command, load_config) makes the value persistent.
    _transient: dict = field(default_factory=dict, repr=False, compare=False)
    # Persisted keys assigned in this process since they were last loaded or
    # saved. An implicit save writes only these, so a key another process
    # changed meanwhile is not overwritten with this process's stale copy
    # (whole-snapshot saves were last-writer-wins across *all* keys).
    _dirty: set = field(default_factory=set, repr=False, compare=False)

    def __post_init__(self):
        self.project_root = Path(self.project_root).resolve()
        self.home = Path(self.home).resolve()

    def __setattr__(self, name: str, value: Any) -> None:
        transient = self.__dict__.get("_transient")
        if transient and name in transient:
            del transient[name]
        object.__setattr__(self, name, value)
        dirty = self.__dict__.get("_dirty")
        if dirty is not None and name in PERSISTED_KEYS:
            dirty.add(name)

    def dirty_keys(self) -> set:
        return set(self._dirty)

    def set_transient(self, name: str, value: Any) -> None:
        """Set ``name`` for this process only; config keeps the prior persisted value."""
        baseline = self._transient[name] if name in self._transient else getattr(self, name)
        object.__setattr__(self, name, value)
        self._transient[name] = baseline

    def persisted_value(self, name: str) -> Any:
        """What config.json should hold for ``name`` (ignores transient overrides)."""
        if name in self._transient:
            return self._transient[name]
        return getattr(self, name)

    def is_transient(self, name: str) -> bool:
        return name in self._transient

    def snapshot(self, names: Sequence[str]) -> dict[str, tuple[Any, bool, Any]]:
        """Capture value + transient state so :meth:`restore` puts both back exactly."""
        return {
            n: (getattr(self, n), n in self._transient, self._transient.get(n))
            for n in names
        }

    def restore(self, snap: dict[str, tuple[Any, bool, Any]], names: Optional[Sequence[str]] = None) -> None:
        for n in (names if names is not None else list(snap)):
            value, was_transient, baseline = snap[n]
            object.__setattr__(self, n, value)
            if was_transient:
                self._transient[n] = baseline
            else:
                self._transient.pop(n, None)

    @property
    def local_enabled(self) -> bool:
        """The local model is switched on: "on", or "auto" and this machine can run it."""
        if self.local_model == "on":
            return True
        if self.local_model != "auto":
            return False
        from . import autotune

        return autotune.profile().usable

    @property
    def show_answer_detail(self) -> bool:
        """mid and high: per-turn usage line and tool result previews."""
        return self.verbose in ("mid", "high")

    @property
    def show_thinking(self) -> bool:
        """high only: reasoning excerpts from the orchestrator."""
        return self.verbose == "high"

    @property
    def orchestrator_model(self) -> str:
        """The model every call uses. Read-only: see ``MODEL``."""
        return MODEL

    @property
    def subagent_model(self) -> str:
        """Subagents and helpers use the same model. Read-only: see ``MODEL``."""
        return MODEL


# ---------------------------------------------------------------------------
# Persisted config (config.json next to K.dat)
# ---------------------------------------------------------------------------

# Only these keys survive between sessions — everything else is per-session.
PERSISTED_KEYS = (
    "subagents_enabled",
    "ownership",
    "compact_at_tokens",
    "compact_policy",
    "compact_floor_tokens",
    "reasoning_effort",
    "thinking_enabled",
    "verbose",
    "auto_compact",
    "exploration_ledger",
    "show_tool_timing",
    "show_context_bar",
    "permission_default",
    "max_iterations",
    "max_tokens",
    "max_context_tokens",
    "compact_threshold",
    "vision_enabled",
    "vision_timeout",
    "salience_excerpt",
    "local_model",
    "local_llama_dir",
    "local_models_dir",
    "local_model_file",
    "local_vision_file",
    "local_gpu_layers",
    "local_ctx",
    "local_port",
    "local_gpu_guard",
    "scout_card",
    "surge_notify",
    "surge_pause",
    "surge_pause_buffer_minutes",
    "write_backup",
    "write_backup_keep",
    "verify_replay",
    "failure_attribution",
    "finish_on_success",
    "closure_receipts",
    "lean_codec",
    "lean_prompt",
    "orient_card",
    "scope_count",
    "search_tiers",
    "def_peek",
    "view_deltas",
    "test_oracle",
    "vacuity_check",
    "obligations",
    "write_guards",
    "callee_digest",
    "claim_check",
    "ordered_batches",
    "step_budget",
    "write_check",
    "preread",
    "finish_licensed",
    "answer_escrow",
    "multi_edit",
    "rename_tool",
    "claimed_write_guard",
)

# Values a saved config is allowed to take. A key missing from here accepts
# anything of the right Python type; a key present is rejected unless it matches.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "verbose": VERBOSE_LEVELS,
    "permission_default": ("allow", "ask", "block"),
    "compact_policy": COMPACT_POLICIES,
    "ownership": ("auto", "user", "agent"),
    "scout_card": ("local", "always", "off"),
    "preread": ("ghost", "prose", "off"),
    "local_model": LOCAL_MODES,
}


# Smallest accepted value for integer keys with no other rule.
_INT_FLOORS: dict[str, int] = {
    "write_backup_keep": 1,
    "vision_timeout": 1,
    "surge_pause_buffer_minutes": 0,
    "local_gpu_layers": 0,
    "local_ctx": 1024,
    "local_port": 0,
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

    if key == "local_model":
        # "auto" | "on" | "off"; a bool or an on/off word maps onto it.
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, str):
            low = value.strip().lower()
            low = "on" if low in _TRUE_WORDS else "off" if low in _FALSE_WORDS else low
            return low if low in LOCAL_MODES else None
        return None

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

    if key == "compact_at_tokens":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if value == 0 or MIN_CONTEXT_TOKENS <= value <= MAX_CONTEXT_TOKENS else None

    if key == "compact_floor_tokens":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if 0 <= value <= MAX_CONTEXT_TOKENS else None

    if key == "compact_threshold":
        # The fraction of the window that trips auto-compaction. Anything in
        # (0, 1] is honoured; 0 would compact every turn and >1 never fires.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if MIN_COMPACT_THRESHOLD <= value <= MAX_COMPACT_THRESHOLD else None

    if key in _INT_FLOORS:
        # Counts and durations: a negative backup count or timeout is a broken
        # value, not a request, so the default is kept.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = int(value)
        return value if value >= _INT_FLOORS[key] else None

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


_TRUE_WORDS = ("on", "true", "yes", "1", "enable", "enabled")
_FALSE_WORDS = ("off", "false", "no", "0", "disable", "disabled")


def parse_setting(key: str, text: str, target: Optional["Settings"] = None) -> tuple[Any, str]:
    """Typed, validated value for ``/settings set KEY VALUE``.

    Returns ``(value, "")`` or ``(None, reason)``. The text is read by the type
    of the key's current value (bool words, ints with ``_``/``,``/``k``,
    floats, strings), then checked by the same ``_coerce`` rules a hand-edited
    config.json goes through, so the command cannot set what a load rejects.
    """
    target = target if target is not None else settings
    if key not in PERSISTED_KEYS:
        return None, f"unknown or session-only key {key!r}; saved keys: {', '.join(PERSISTED_KEYS)}"
    current = getattr(target, key, None)
    raw = (text or "").strip()
    if not raw:
        return None, "missing value"
    low = raw.lower()
    value: Any = raw
    if isinstance(current, bool):
        if low in _TRUE_WORDS:
            value = True
        elif low in _FALSE_WORDS:
            value = False
        else:
            return None, "expects on or off"
    elif isinstance(current, (int, float)) and key not in _ALLOWED:
        num = low.replace("_", "").replace(",", "")
        scale = 1
        if num.endswith("k"):
            num, scale = num[:-1], 1000
        try:
            parsed = float(num) * scale
        except ValueError:
            if key != "reasoning_effort":
                return None, "expects a number"
            parsed = None
        if parsed is not None:
            value = parsed if isinstance(current, float) else int(parsed)
    value = _coerce(key, value, current)
    if value is None:
        allowed = _ALLOWED.get(key)
        hint = f"one of {', '.join(allowed)}" if allowed else "a value in the allowed range"
        return None, f"{raw!r} rejected for {key}; expects {hint}"
    return value, ""


# Stamp written into every config.json this build saves. Bump it when a boolean
# default flips from false to true, and list the key in DEFAULTS_FLIPPED_ON.
DEFAULTS_REV_KEY = "defaults_rev"
DEFAULTS_REV = 1
# finish_on_success shipped as false in the first 1.5.0 build's config.json.
# Keys added after that build (step_budget, preread, ...) need no entry: no
# unstamped file holds them.
DEFAULTS_FLIPPED_ON: tuple[str, ...] = ("finish_on_success",)


def _migrate_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the one legacy key that still carries intent; drop the rest.

    ``vision_backend: "none"`` (the old default) meant "no image analysis", so a
    file that says it and has no ``vision_enabled`` of its own keeps vision off.
    Every other LEGACY_KEYS entry is simply not read.
    """
    if "vision_enabled" not in data and data.get("vision_backend") in ("none", ""):
        data = dict(data)
        data["vision_enabled"] = False
    # A file without the current defaults stamp was written by an older build,
    # which wrote every key at its then-default: a false there for a key whose
    # default has since become true was never a choice, so the new default wins.
    return _drop_unstamped_flips(data)


def _drop_unstamped_flips(data: dict[str, Any]) -> dict[str, Any]:
    """Without the current stamp, a stored false for a now-true default reads as unset."""
    try:
        rev = int(data.get(DEFAULTS_REV_KEY) or 0)
    except (TypeError, ValueError):
        rev = 0
    if rev >= DEFAULTS_REV:
        return data
    flipped = [k for k in DEFAULTS_FLIPPED_ON if data.get(k) is False]
    return {k: v for k, v in data.items() if k not in flipped} if flipped else data


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
        data = _migrate_legacy(data)
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
    # Loaded values are on disk already: they are not this process's edits.
    target._dirty.difference_update(applied)
    return applied


def save_config(
    target: Optional["Settings"] = None, scope: str = "auto", *, full: Optional[bool] = None
) -> Path:
    """Write the persisted subset of settings to config.json.

    ``scope`` is "auto" (the project's .g023/config.json when it exists, else
    the machine file next to K.dat), "global" (the machine file — what new
    projects inherit), or "project" (pin to this project only). ``/config``
    manages the split.

    ``full``: write every persisted key (an explicit "save my settings"; the
    default for the global/project scopes and for a file that does not exist
    yet). Otherwise only the keys this process changed are merged in, so two
    processes changing *different* keys both keep their change.
    """
    target = target if target is not None else settings
    if scope == "global":
        path = get_config_file()
    elif scope == "project":
        path = get_project_config_file()
    else:  # auto
        path = get_project_config_file() if get_project_config_file().exists() else get_config_file()
    # Read-modify-write under a cross-process lock, then an atomic replace:
    # two g023 processes saving at once cannot lose each other's unknown keys
    # or leave a torn file. Transient overrides are written as their baseline.
    with file_lock(path):
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                pass
        for key in LEGACY_KEYS:
            existing.pop(key, None)
        # Stamping an old file must not turn its wholesale defaults into choices.
        existing = _drop_unstamped_flips(existing)
        if full is None:
            full = scope in ("global", "project")
        if full or not path.exists():
            keys = list(PERSISTED_KEYS)
        else:
            keys = [k for k in PERSISTED_KEYS if k in target._dirty]
        existing.update({key: target.persisted_value(key) for key in keys})
        existing[DEFAULTS_REV_KEY] = DEFAULTS_REV
        atomic_write_text(path, json.dumps(existing, indent=2) + "\n")
        target._dirty.difference_update(keys)
    return path


# Environment switches that change a Settings default. A *default* config file
# must not depend on the shell that happened to write it. Every ``G023_*``
# variable is set aside while the defaults are built: a hand-kept list missed
# the newer switches (G023_FINISH_ON_SUCCESS=0 leaked into a fresh config.json).
_DEFAULT_ENV_PREFIX = "G023_"


def default_config() -> dict[str, Any]:
    """The persisted keys at their built-in defaults, independent of the environment.

    This is the one source the installers and the release builder use for a fresh
    ``config.json``. Hand-written copies drifted: they shipped a 256k window after
    the default became 1M and lacked six newer keys.
    """
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(_DEFAULT_ENV_PREFIX)}
    try:
        fresh = Settings()
    finally:
        os.environ.update(saved)
    return {**{key: getattr(fresh, key) for key in PERSISTED_KEYS}, DEFAULTS_REV_KEY: DEFAULTS_REV}


def write_default_config(path: Union[str, Path]) -> Path:
    """Write :func:`default_config` to ``path`` (2-space JSON, trailing newline)."""
    path = Path(path)
    atomic_write_text(path, json.dumps(default_config(), indent=2) + "\n")
    return path


# Global settings instance (mutable at runtime via slash commands)
settings = Settings()
load_config(settings)
