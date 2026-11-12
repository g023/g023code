"""Predicated finish: the last step of a turn carries the final answer.

A tool turn normally ends with a model call that only reads a passing result
and writes the answer. On the live bench that answer-only call was about a
quarter of all output tokens, and it re-reads the whole context to do it.

With ``finish_on_success`` on, Bash, Edit and WriteFile accept an optional
``final_answer`` argument. The model fills it on the step it expects to
complete the request (usually the test run that verifies a fix). The harness
delivers it only when the step's success test holds, for every call in that
batch:

* Bash: exit code 0, no error (a timeout is an error), and no test-failure
  summary in the output (``1 failed``, ``FAILED``, a traceback): a pipe or
  ``;`` chain can exit 0 after a failing run.
* Edit / WriteFile: ``ok``, no ``syntax_error``, and an automatic verify
  replay (if one ran) exited 0.
* Any other call in the batch: no ``error``.

Then the turn ends without another model call, and an evidence line quoting
the real result is appended to the answer, so the user sees what the model
did not. If any test fails, the text is discarded and the loop continues as
if it had never been written.

No synthetic assistant message is added: the answer already sits in the
``tool_use`` input, so history ends ``tool_use -> tool_result``, the same
shape an interrupted turn leaves.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from .config import settings

FINISH_ARG = "final_answer"
FINISH_TOOLS = ("Bash", "Edit", "WriteFile")
EVIDENCE_LINE_MAX = 110
EVIDENCE_LINES_MAX = 3
DELIVERED_NOTE = "[final_answer delivered to the user; the turn ended here]"

SCHEMA_PROPERTY = {
    "type": "string",
    "description": (
        "Optional. Your final answer to the user, written now, on the step that should complete "
        "the request (e.g. the test run that verifies your change). If this call succeeds "
        "(Bash exit 0; Edit/WriteFile applied with no syntax error and any automatic re-run "
        "passing), the turn ends with this answer and you are not called again. If it fails, the "
        "text is discarded and you see the result as usual. Leave it out when you still need to "
        "read the result. Do not state numbers you have not seen: the real result is appended."
    ),
}

SYSTEM_ADDENDUM = (
    "\nFinishing: when a Bash, Edit or WriteFile call is the step that completes the request "
    "(typically the final test run), put your final answer, with its trailers, in that call's "
    "final_answer argument. If the call succeeds the answer is delivered and you are not called "
    "again, which saves a full round trip; if it fails you continue as usual.\n"
)


def enabled() -> bool:
    return bool(getattr(settings, "finish_on_success", False))


def licensed_enabled() -> bool:
    """Counterfactual-licensed finish: pre-existing, unrelated failures (``finish_licensed``)."""
    return enabled() and bool(getattr(settings, "finish_licensed", False))


def escrow_enabled() -> bool:
    """Answer escrow: a discarded answer is kept and released with ``[deliver]`` (``answer_escrow``)."""
    return enabled() and bool(getattr(settings, "answer_escrow", False))


def system_addendum() -> str:
    return SYSTEM_ADDENDUM if enabled() else ""


def add_to_schemas(tools: Iterable[dict]) -> List[dict]:
    """Return tool schemas with ``final_answer`` on Bash/Edit/WriteFile (copies, never in place)."""
    out: List[dict] = []
    for schema in tools:
        if schema.get("name") in FINISH_TOOLS:
            schema = copy.deepcopy(schema)
            params = schema.setdefault("parameters", {})
            params.setdefault("properties", {})[FINISH_ARG] = dict(SCHEMA_PROPERTY)
        out.append(schema)
    return out


def take(args: dict) -> Optional[str]:
    """Remove and return the finish text from parsed arguments (None when absent/blank)."""
    if not isinstance(args, dict) or FINISH_ARG not in args:
        return None
    value = args.pop(FINISH_ARG)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _data(result: str) -> dict:
    try:
        data = json.loads(result or "")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clip(text: str, limit: int = EVIDENCE_LINE_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _last_line(text: str) -> str:
    for line in reversed(str(text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


# A pipeline (``pytest | tail``) or a ``;`` chain reports the exit code of its
# last command, so exit 0 can hide a failing test run. These are failure
# summaries common test runners print; any of them vetoes the finish.
_FAIL_MARKS = re.compile(
    r"\b[1-9]\d* (?:failed|errors?|failing)\b"
    r"|^FAILED\b|^FAIL[: ]|^ERROR:|Traceback \(most recent call last\)"
    r"|\btest result: FAILED\b|^--- FAIL",
    re.MULTILINE,
)


def failure_marked(*texts: str) -> bool:
    return any(_FAIL_MARKS.search(t or "") for t in texts)


def step_ok(name: str, args: dict, result: str) -> Tuple[bool, str]:
    """Success test for one executed call, plus a one-line evidence string."""
    data = _data(result)
    if not data:
        # A non-JSON body (an encoded read, a plain-text tool) is not a failure
        # signal, but it gives no evidence either.
        return (name not in FINISH_TOOLS), ""
    if data.get("error"):
        return False, ""
    if name == "Bash":
        code = data.get("exit_code")
        if code != 0 or failure_marked(str(data.get("stdout") or ""), str(data.get("stderr") or "")):
            return False, ""
        cmd = _clip(args.get("command") or "", 60)
        tail = _last_line(data.get("stdout")) or _last_line(data.get("stderr"))
        line = f"`{cmd}` exit 0"
        return True, (f"{line} — {_clip(tail)}" if tail else line)
    if name in ("Edit", "WriteFile"):
        if not data.get("ok") or data.get("syntax_error"):
            return False, ""
        verify = data.get("verify")
        if isinstance(verify, dict) and (
            verify.get("exit_code") != 0 or failure_marked(str(verify.get("output_tail") or ""))
        ):
            return False, ""
        check = data.get("check")
        if isinstance(check, dict) and (
            check.get("error") or check.get("skipped") or check.get("exit_code") != 0
            or failure_marked(str(check.get("stdout") or ""), str(check.get("stderr") or ""))
        ):
            # A write-carried check (hops.py) is part of the step: it must pass too.
            return False, ""
        path = args.get("path") or data.get("path") or "?"
        where = f" lines {data['lines']}" if data.get("lines") else ""
        what = "unchanged" if data.get("unchanged") else "applied"
        line = f"{name} {path}{where} {what}"
        if isinstance(verify, dict):
            vcmd = _clip(verify.get("command") or "", 50)
            vtail = _clip(_last_line(verify.get("output_tail")), 70)
            line += f"; re-ran `{vcmd}` exit 0" + (f" — {vtail}" if vtail else "")
        if isinstance(check, dict):
            ccmd = _clip(check.get("command") or "", 50)
            ctail = _clip(_last_line(check.get("stdout")) or _last_line(check.get("stderr")), 70)
            line += f"; `{ccmd}` exit 0" + (f" — {ctail}" if ctail else "")
        return True, line
    return True, ""


# The request itself asks for a run ("... and run them", "make sure the tests
# pass"): an answer carried by a bare write must not end the turn unrun.
_RUN_DEMAND = re.compile(
    r"\b(?:run|re-?run|execute)\b[^.?!\n]{0,40}?\b(?:tests?|them|suite|pytest|specs?|checks?)\b"
    r"|\b(?:make sure|ensure|verify|confirm)\b[^.?!\n]{0,40}?\b(?:tests?|imports?|builds?|compiles?|works|passes)\b"
    r"|\btests? (?:still )?pass\b",
    re.IGNORECASE,
)
RUN_HELD_NOTE = (
    "[final_answer held back: the request asks for the tests to be run and this batch ran none. "
    "Run them; put the answer on that call.]"
)


def run_demanded(request: str) -> bool:
    """Does the user's request ask for a test run / verification?"""
    return bool(_RUN_DEMAND.search(request or ""))


def _ran_check(name: str, result: str) -> bool:
    """A passing step that ran something: a Bash call, or a write's verify replay / write check."""
    if name == "Bash":
        return True
    data = _data(result)
    return isinstance(data.get("verify"), dict) or isinstance(data.get("check"), dict)


# --- Counterfactual licence -----------------------------------------------------
# A failing check whose attribution (attribution.py: the same command re-run with
# this turn's edits reverted) found every failure failing there too did not fail
# because of this turn. The answer still ships, stamped with those failures,
# unless the request is about them or the answer claims a clean run.
_FIX_INTENT = re.compile(r"\b(?:failing|failures?|fails|failed|red|broken|green|regressions?)\b", re.I)
_ID_STOP = frozenset({"test", "tests", "spec", "specs", "py", "js", "ts", "rs", "go", "src", "lib", "unit",
                      "case", "cases", "check", "main", "init", "self", "func", "method"})


def _id_tokens(test_id: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", test_id.lower()) if len(t) >= 4 and t not in _ID_STOP}


def _related(ids: Iterable[str], request: str) -> bool:
    words = [w for w in re.split(r"[^a-z0-9]+", (request or "").lower()) if len(w) >= 4]
    for tid in ids:
        for tok in _id_tokens(str(tid)):
            if any(w.startswith(tok) or tok.startswith(w) for w in words):
                return True
    return False


def licence(name: str, args: dict, result: str, request: str, answer: str) -> Optional[str]:
    """Caveat line when a failed check may still finish, else None."""
    if name != "Bash":
        return None
    data = _data(result)
    if not data or data.get("error"):
        return None
    cmd = _clip(args.get("command") or "", 60)
    att = data.get("attribution")
    if not isinstance(att, dict) or att.get("new") or att.get("note") or not att.get("pre_existing"):
        return None
    if data.get("exit_code") in (0, None) and not failure_marked(str(data.get("stdout") or "")):
        return None
    pre = [str(x) for x in att.get("pre_existing") or [] if not str(x).startswith("... +")]
    if not pre or _FIX_INTENT.search(request or "") or _related(pre, request):
        return None
    from .claims import pass_claim

    if pass_claim(answer or ""):
        return None  # "all tests pass" would be false: let the model say it right
    shown = ", ".join(pre[:3]) + (f" +{len(pre) - 3} more" if len(pre) > 3 else "")
    return (f"`{cmd}` exit {data.get('exit_code')} — {len(att.get('pre_existing'))} pre-existing failure(s), "
            f"also failing with this turn's edits reverted: {shown}")


# --- Answer escrow --------------------------------------------------------------
# --- Claimed-write guard -------------------------------------------------------
# A pre-written answer describes the turn's end state before the turn reaches
# it. Seen live: the answer, carried by an Edit of app/config.py, said the
# README was updated too — that Edit never came, and the turn ended. A file the
# request names, that the answer says was changed, must have been written this
# turn (Edit/WriteFile/Rename, or a Bash run that changed it).

_PATHISH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,8})(?![\w/])")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CHANGE_WORDS = re.compile(
    r"\b(?:updated?|added|adds|changed?|now|edited|wrote|written|set|bumped|switched|replaced|renamed|"
    r"documents|documented)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|no|unchanged|already|untouched|didn't|did not|left)\b", re.I)


def claimed_write_enabled() -> bool:
    return enabled() and bool(getattr(settings, "claimed_write_guard", False))


def request_files(request: str, root) -> List[str]:
    """Project files the request names: a path (``app/config.py``) or a top-level stem (``README``)."""
    from pathlib import Path

    root = Path(root)
    out: List[str] = []
    for m in _PATHISH.finditer(request or ""):
        rel = m.group(1).lstrip("./")
        if rel and (root / rel).is_file() and rel not in out:
            out.append(rel)
    try:
        top = {p.stem.lower(): p.name for p in root.iterdir() if p.is_file() and not p.name.startswith(".")}
    except OSError:
        top = {}
    for w in _WORD.findall(request or ""):
        name = top.get(w.lower())
        # A capitalised word (README, Makefile, CHANGELOG): "config" as prose is not config.json.
        if name and w[0].isupper() and name not in out:
            out.append(name)
    return out


def unwritten_claims(request: str, answer: str, written, root) -> List[str]:
    """Request-named files the answer says were changed, that no call this turn wrote."""
    from pathlib import PurePosixPath

    written = {str(w) for w in written or ()}
    if not written:
        return []  # a turn that changed nothing is answering, not reporting changes
    claims: List[str] = []
    lines = [ln for ln in re.split(r"(?<=[.!?])\s+|\n", answer or "") if ln.strip()]
    for rel in request_files(request, root):
        if rel in written:
            continue
        p = PurePosixPath(rel)
        names = {rel.lower(), p.name.lower()} | ({p.stem.lower()} if len(p.stem) >= 4 else set())
        pat = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                         + r")(?![\w-])", re.I)
        for ln in lines:
            if pat.search(ln) and _CHANGE_WORDS.search(ln) and not _NEGATED.search(ln):
                claims.append(rel)
                break
    return claims


def claimed_write_note(rels: List[str]) -> str:
    names = ", ".join(rels[:3])
    return (f"[final_answer held back: it says {names} changed, but no call this turn wrote "
            f"{'it' if len(rels) == 1 else 'them'}. Make the change (or correct the answer).]")


RELEASE_TOKEN = "[deliver]"
_RELEASE = re.compile(r"^\s*\[deliver\]\s*", re.I)


def escrow_note(reason: str) -> str:
    return (f"[final_answer not delivered ({reason}); it is kept. If it still holds, reply with just "
            f"{RELEASE_TOKEN} (optionally followed by a line to add) instead of rewriting it; "
            "otherwise continue or write a new answer.]")


def release(text: str, escrow: Optional[str]) -> Optional[str]:
    """The escrowed answer (+ the model's added line) when ``text`` starts with [deliver]."""
    if not escrow or not isinstance(text, str):
        return None
    m = _RELEASE.match(text)
    if not m:
        return None
    extra = text[m.end():].strip()
    return escrow + (("\n\n" + extra) if extra else "")


@dataclass
class Decision:
    """Outcome for one tool batch: deliver ``answer`` (+ evidence) or not."""

    answer: Optional[str] = None
    carrier: Optional[str] = None  # call id whose argument held the answer
    evidence: List[str] = field(default_factory=list)
    discarded: bool = False  # an answer was offered but a success test failed
    held_for_run: bool = False  # discarded because the request asked for a run and none ran
    caveats: List[str] = field(default_factory=list)  # licensed failures, shown with a warning mark
    offered: Optional[str] = None  # the discarded answer (answer escrow)
    reason: str = ""  # why it was discarded, for the escrow note
    held_note: str = ""  # discarded by the claimed-write guard: the note for the carrier's result

    @property
    def deliver(self) -> bool:
        return self.answer is not None

    def text(self) -> str:
        if not self.deliver:
            return ""
        lines = [e for e in self.evidence if e and e not in self.caveats][-EVIDENCE_LINES_MAX:]
        stamp = [f"✓ {e}" for e in lines] + [f"⚠ {c}" for c in self.caveats]
        if not stamp:
            return self.answer or ""
        return f"{self.answer}\n\n" + "\n".join(stamp)


def decide(batch: Iterable[tuple], require_run: bool = False, request: str = "") -> Decision:
    """``batch`` rows: (call_id, name, args, result, ran, finish_text). Program order.

    The last offered answer wins. Every call must have run and passed its
    success test, or nothing is delivered. With ``require_run`` (the request
    asked for the tests to be run) the batch must also have run something:
    a Bash call, or a write's verify replay / write check. With
    ``finish_licensed`` on, a failed Bash step still passes when ``licence``
    grants it (only pre-existing failures); its caveat is
    stamped on the answer.
    """
    rows = list(batch)
    offered = [(cid, text) for cid, _n, _a, _r, _ran, text in rows if text]
    if not offered:
        return Decision()
    carrier, answer = offered[-1]
    evidence: List[str] = []
    caveats: List[str] = []
    for _cid, name, args, result, ran, _text in rows:
        if not ran:
            return Decision(discarded=True, carrier=carrier, offered=answer, reason="a call did not run")
        ok, line = step_ok(name, args or {}, result)
        if not ok:
            granted = licence(name, args or {}, result, request, answer) if licensed_enabled() else None
            if granted is None:
                return Decision(discarded=True, carrier=carrier, offered=answer,
                                reason=f"{name} did not succeed")
            caveats.append(granted)
            line = granted
        evidence.append(line)
    if require_run and not any(_ran_check(name, result) for _c, name, _a, result, _r, _t in rows):
        return Decision(discarded=True, held_for_run=True, carrier=carrier)
    return Decision(answer=answer, carrier=carrier, evidence=evidence, caveats=caveats)
