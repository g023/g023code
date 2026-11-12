"""
Intent-salient excerpts of bulky tool output, plus vault files the model can read back.

A long Bash/ListDir/Glob result never enters history in full: the vault keeps the
body and history gets a short excerpt. The excerpt used to be blind (first 400 +
last 800 chars). On a corpus of 14 real long outputs only
3/14 of the lines the model was looking for survived that cut.

The model already says what it is looking for: the reasoning and commentary of the
step that issued the call ("Let me find where the vault excerpt is built"). That
text is free (it is already paid for and already in the reply), so it is used as
the query: BM25 over the output's lines plus error-shaped lines. 11/14 with no
model call at all; a local small model can rerank the candidates (salience
``extra`` picks, see local_llm.py) for 12/14.

The full output is also written to ``.g023/vault/<id>.txt`` so the model can read
any range (ReadFile start_line/end_line) or ``grep -n`` it instead of re-running the
command (a paid step, and wrong for slow or non-idempotent commands).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, Optional

from . import docindex
from .config import get_project_root

VAULT_DIR = ".g023/vault"
VAULT_KEEP_FILES = 300
VAULT_FILE_MAX_CHARS = 2_000_000

# Excerpt budget (chars). History pays these once as a cache miss, then as cheap
# hits; a re-run step costs far more, so the selected block is the larger part.
HEAD_CHARS = 240
PICK_CHARS = 1_000
TAIL_CHARS = 500
LINE_CLIP = 200
MAX_PICKS = 14
MAX_SCAN_LINES = 40_000

INTENT_REASONING_TAIL = 600
_INTENT_MAX = 1_200

_ERR = re.compile(
    r"\b(error|exception|traceback|fatal|failed|failure|panic|denied|not found|undefined|"
    r"segmentation fault|assert(?:ion)?)\b|Error:",
    re.I,
)


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


def step_intent(reasoning: str = "", commentary: str = "", fallback: str = "") -> str:
    """What the model said it is looking for in this step (free text, no model call).

    The end of the reasoning is the part next to the tool call, so it names the
    target. ``fallback`` (the user's request) is used only when the step said
    almost nothing (thinking off and no commentary).
    """
    parts = []
    r = (reasoning or "").strip()
    if r:
        parts.append(r[-INTENT_REASONING_TAIL:])
    c = (commentary or "").strip()
    if c:
        parts.append(c[-400:])
    text = "\n".join(parts)
    if len(text) < 40 and fallback:
        text = (text + "\n" + fallback.strip()[-400:]).strip()
    return text[-_INTENT_MAX:]


def call_intent(step: str, args: Optional[dict]) -> str:
    """The step intent; the call's own arguments only when the step said almost nothing.

    Command words (``kb.py ls decisions``, ``grep -rn "def "``) tend to occur on
    every output line, so mixing them into a real intent dilutes the ranking
    (measured: 11/14 -> 10/14 on the session-24 corpus).
    """
    if len((step or "").strip()) >= 40:
        return step
    bits = [step or ""]
    for key in ("command", "pattern", "path", "question"):
        val = (args or {}).get(key)
        if isinstance(val, str) and val:
            bits.append(val[:400])
    return "\n".join(b for b in bits if b)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def candidate_lines(lines: list, intent: str, top: int = 12, errors: int = 4) -> list:
    """Line indexes most relevant to ``intent``: BM25 best first, then error lines."""
    scan = lines if len(lines) <= MAX_SCAN_LINES else lines[:MAX_SCAN_LINES]
    q = docindex.terms(intent or "")
    ranked = []
    if q and scan:
        ranked = [i for _s, i in docindex.rank_texts([("", l[:LINE_CLIP * 2]) for l in scan], q, top=top, name_boost=1.0)]
    errs = [i for i, l in enumerate(scan) if _ERR.search(l)]
    # The last errors are usually the cause (a build's first error is near its
    # end summary, a traceback's exception line is last).
    picked = ranked + errs[:errors // 2] + errs[-(errors - errors // 2):] if errs else ranked
    return list(dict.fromkeys(picked))


def rerank_pool(lines: list, intent: str, top: int = 60, errors: int = 20) -> list:
    """A wider candidate pool (with +-1 neighbours) for a local model to choose from."""
    base = candidate_lines(lines, intent, top=top, errors=errors)
    return sorted({j for i in base for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})


def build_excerpt(
    text: str,
    intent: str = "",
    *,
    extra: Iterable[int] = (),
    head: int = HEAD_CHARS,
    pick: int = PICK_CHARS,
    tail: int = TAIL_CHARS,
) -> str:
    """Head + numbered intent-salient lines + tail, within about head+pick+tail chars.

    ``extra`` are line indexes chosen elsewhere (a local model); they go first.
    Line numbers are 1-based and refer to ``text`` (the vault file), so the model
    can ReadFile that range.
    """
    if len(text) <= head + pick + tail + 80:
        return text
    lines = text.splitlines()
    head_txt = text[:head]
    tail_txt = text[-tail:]
    head_end = head_txt.count("\n")
    tail_start = len(lines) - tail_txt.count("\n") - 1
    order = list(dict.fromkeys([i for i in extra if 0 <= i < len(lines)] + candidate_lines(lines, intent)))
    rows, used = [], 0
    for i in order:
        if i < head_end or i > tail_start:
            continue  # already visible in head/tail
        row = f"L{i + 1}: {lines[i][:LINE_CLIP]}"
        if used + len(row) + 1 > pick or len(rows) >= MAX_PICKS:
            continue
        rows.append((i, row))
        used += len(row) + 1
    rows.sort()
    out = [head_txt.rstrip()]
    if rows:
        out.append(f"… [{len(rows)} lines picked for this step's intent, of {len(lines)}] …")
        out += [r for _i, r in rows]
    out.append("… [tail] …")
    out.append(tail_txt.lstrip("\n"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Vault files (readable full bodies)
# ---------------------------------------------------------------------------


def vault_dir(root: Optional[Path] = None) -> Path:
    return (root or get_project_root()) / VAULT_DIR


def write_vault_file(body: str, root: Optional[Path] = None) -> tuple[str, int]:
    """Store ``body`` under .g023/vault/<sha16>.txt; returns (project-relative path, lines).

    Content-addressed, so the same bytes are written once. Oversized bodies keep
    their head and tail. Never raises: ("", 0) on failure.
    """
    try:
        if len(body) > VAULT_FILE_MAX_CHARS:
            half = VAULT_FILE_MAX_CHARS // 2
            body = body[:half] + f"\n… [{len(body) - VAULT_FILE_MAX_CHARS} chars not kept] …\n" + body[-half:]
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
        d = vault_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{digest}.txt"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8", errors="replace")
            tmp.replace(path)
            prune_vault(d)
        return f"{VAULT_DIR}/{digest}.txt", body.count("\n") + (0 if body.endswith("\n") else 1)
    except Exception:
        return "", 0


def read_vault_file(rel: str, root: Optional[Path] = None) -> Optional[str]:
    if not rel or not rel.startswith(VAULT_DIR + "/") or ".." in rel:
        return None
    try:
        return ((root or get_project_root()) / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def prune_vault(d: Path, keep: int = VAULT_KEEP_FILES) -> None:
    try:
        files = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def bash_full_body(stdout: str, stderr: str) -> str:
    """The text a Bash vault file holds: raw streams under [stdout] / [stderr] tags."""
    parts = []
    if stdout:
        parts += ["[stdout]", stdout.rstrip("\n")]
    if stderr:
        parts += ["[stderr]", stderr.rstrip("\n")]
    return "\n".join(parts) + "\n"


def readback_note(rel: str, lines: int) -> str:
    return (
        f"… full output kept: {rel} ({lines} lines). Read any range with ReadFile "
        f"start_line/end_line or `grep -n PATTERN {rel}`; no need to re-run the command."
    )
