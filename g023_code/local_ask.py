"""
LocalAsk: bulk reading on the local model, with cite-or-drop evidence.

The paid model hands over a question and some files (project files, or the
.g023/vault/ file of an earlier long output). The local model reads them in
numbered chunks and points at line numbers; the lines returned as evidence are
copied from the files by the harness, never generated, so they are verbatim by
construction. A final local call drafts a short answer from those lines only.

The answer is a small-model lead; the evidence is the fact. This is the Minions
pattern (arXiv 2502.15964: local reads, remote reasons) with the remote side
paying only for the question and the few lines that matter.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import docindex, local_llm
from .config import get_project_root, settings
from .paths import resolve_in_project

CHUNK_LINES = 250
MAX_CHUNKS = 12
MAX_FILE_CHARS = 600_000
MAX_FILES = 8
MAX_EVIDENCE = 12
TOTAL_BUDGET_S = 90.0
LINE_CLIP = 240

_SYS_CHUNK = (
    "You read part of a file for an engineer. Given the question and numbered lines, return the ids "
    "of the lines (at most 6) that help answer it, and a short note of what they show. If nothing "
    "here helps, return relevant=false and no ids. Only ids that appear."
)
_SCHEMA_CHUNK = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "lines": {"type": "array", "items": {"type": "integer"}, "maxItems": 6},
        "note": {"type": "string", "maxLength": 240},
    },
    "required": ["relevant", "lines"],
}
_SYS_ANSWER = (
    "Answer the engineer's question using ONLY the quoted lines. Be brief (at most 4 sentences). "
    "If the lines do not answer it, say found=false and what is missing."
)
_SCHEMA_ANSWER = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "answer": {"type": "string", "maxLength": 800}},
    "required": ["found", "answer"],
}


def _load(paths: list) -> tuple[list[tuple[str, list[str]]], list[str]]:
    root = get_project_root()
    files, skipped = [], []
    for raw in list(paths or [])[:MAX_FILES]:
        try:
            p = resolve_in_project(str(raw))
            data = p.read_bytes()[: MAX_FILE_CHARS * 2]
            if b"\x00" in data[:4096]:
                skipped.append(f"{raw}: binary")
                continue
            text = data.decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = str(p)
            files.append((rel, text.splitlines()))
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{raw}: {e}")
    return files, skipped


def plan_chunks(files: list, question: str, max_chunks: int = MAX_CHUNKS) -> tuple[list, int]:
    """(file index, start, end) chunks to read, best BM25 first when there are too many."""
    chunks = [
        (fi, a, min(a + CHUNK_LINES, len(lines)))
        for fi, (_rel, lines) in enumerate(files)
        for a in range(0, max(len(lines), 1), CHUNK_LINES)
        if lines
    ]
    total = len(chunks)
    if total <= max_chunks:
        return chunks, total
    q = docindex.terms(question)
    texts = [("", "\n".join(files[fi][1][a:b])) for fi, a, b in chunks]
    ranked = [i for _s, i in docindex.rank_texts(texts, q, top=max_chunks, name_boost=1.0)] if q else []
    keep = list(dict.fromkeys(ranked + list(range(total))))[:max_chunks]
    return [chunks[i] for i in sorted(keep)], total


_MD_HEAD = re.compile(r"^(#{1,6})\s+\S")
_CODE_ANCHOR = re.compile(r"^\s*(?:async\s+def|def|class|function|func|fn|pub\s+fn|impl|struct|interface)\s+\w")
_INI_ANCHOR = re.compile(r"^\s*\[[^\]]+\]\s*$")
_MD_SUFFIXES = (".md", ".markdown", ".rst", ".txt")


def section_of(lines: list, n: int, rel: str = "", reach: int = 600) -> str:
    """Where 1-based line ``n`` sits: markdown heading path, else the enclosing def/class/[section].

    Markdown gets the chain of parent headings ("## [1.4.0] > ### Fixed"), so a
    changelog line names its release; code gets the nearest def/class above.
    """
    lo = max(-1, n - 1 - reach)
    if rel.lower().endswith(_MD_SUFFIXES):
        chain, level = [], 7
        for i in range(n - 1, lo, -1):
            m = _MD_HEAD.match(lines[i])
            if m and len(m.group(1)) < level:
                level = len(m.group(1))
                chain.append(lines[i].strip()[:80])
                if level == 1:
                    break
        return " > ".join(reversed(chain[:3]))
    for i in range(n - 1, lo, -1):
        if _CODE_ANCHOR.match(lines[i]) or _INI_ANCHOR.match(lines[i]):
            return lines[i].strip()[:100]
    return ""


def rank_evidence(evidence: list, question: str) -> list:
    """Most question-like lines first (BM25 with the line's section), stable otherwise."""
    q = docindex.terms(question)
    if not q or len(evidence) <= 1:
        return evidence
    texts = [("", f"{sec} {t}") for _r, _n, t, sec in evidence]
    order = [i for _s, i in docindex.rank_texts(texts, q, top=len(texts), name_boost=1.0)]
    order += [i for i in range(len(evidence)) if i not in set(order)]
    return [evidence[i] for i in order]


def verify_ids(ids: Any, start: int, end: int) -> list[int]:
    """Cite-or-drop: keep only 1-based ids that lie inside the chunk that was shown."""
    out = []
    for x in ids if isinstance(ids, list) else []:
        if isinstance(x, int) and not isinstance(x, bool) and start < x <= end:
            out.append(x)
    return list(dict.fromkeys(out))


_SYS_PROJECT = (
    "An engineer asks a question about a codebase. You are shown numbered lines from the parts of the "
    "code a search found. Return the ids of at most 6 lines that answer it or show where it is "
    "handled, most useful first. If none do, return an empty list. Only ids that appear."
)


async def _run_project(question: str, chat) -> str:
    """No paths: the whole project. Scout retrieval (BM25 + local query expansion) picks the
    code units, the local model points at lines (cite-or-drop), then drafts an answer."""
    from . import scout

    root = get_project_root()
    found = await scout.locate(question, root, chat, system=_SYS_PROJECT)
    if not found or not found["ranked"]:
        return json.dumps({"error": "nothing in the project matches the question's words",
                           "hint": "Pass paths, or use SearchContent with a name or phrase."})
    if not found["answered"]:
        return json.dumps({"error": "local model did not answer", "status": local_llm.status().get("last_error", "")})
    idx = found["idx"]
    evidence = [(rel, n, idx.lines(rel)[n - 1][:LINE_CLIP], section_of(idx.lines(rel), n, rel))
                for rel, n in found["rows"]]
    answer, ok = "", False
    if evidence:
        quoted = "\n".join(f"{r}:{n}: {t}" + (f"   (in: {sec})" if sec else "") for r, n, t, sec in evidence)
        obj = await chat(_SYS_ANSWER, f"Question: {question}\n\nQuoted lines:\n{quoted}", _SCHEMA_ANSWER, max_tokens=300)
        if obj:
            answer = str(obj.get("answer") or "").strip()
            ok = bool(obj.get("found"))
    st = local_llm.status() if chat is local_llm.chat_json else {}
    out: dict[str, Any] = {
        "backend": f"local:{st.get('served_model') or 'model'}",
        "found": ok and bool(evidence),
        "answer": answer or ("no relevant lines found" if not evidence else ""),
        "evidence": [f"{r}:{n}: {t}" + (f"   (in: {sec})" if sec else "") for r, n, t, sec in evidence],
        "searched": "whole project: " + scout.render_units(found["ranked"], k=5),
        "note": "small-model lead; evidence lines are copied from the files (verbatim)",
    }
    if not evidence:
        out["hint"] = "the searched units did not answer it; try SearchContent or pass paths"
    return json.dumps(out, ensure_ascii=False)


async def run(question: str, paths: list, *, chat=None) -> str:
    """LocalAsk executor. ``chat`` is injectable for tests (defaults to local_llm.chat_json)."""
    chat = chat or local_llm.chat_json
    question = (question or "").strip()
    if not question:
        return json.dumps({"error": "question is required"})
    if chat is local_llm.chat_json and not local_llm.available():
        why = "the GPU is in use (GPU work this turn or another process)" if settings.local_enabled else "local model is off"
        return json.dumps({
            "error": f"LocalAsk unavailable: {why}.",
            "hint": "Read the file with ReadFile (ranges) or SearchContent instead.",
        })
    if not paths:
        return await _run_project(question, chat)
    files, skipped = _load(paths)
    if not files:
        return json.dumps({"error": "no readable files", "skipped": skipped})
    chunks, total = plan_chunks(files, question)
    t0 = time.monotonic()
    evidence: list[tuple[str, int, str, str]] = []
    weak: list[tuple[str, int, str, str]] = []
    notes: list[str] = []
    read = 0
    for fi, a, b in chunks:
        if time.monotonic() - t0 > TOTAL_BUDGET_S:
            break
        rel, lines = files[fi]
        rows = "\n".join(f"{i + 1}: {lines[i][:LINE_CLIP]}" for i in range(a, b))
        obj = await chat(_SYS_CHUNK, f"Question: {question}\nFile: {rel}\n\nLines:\n{rows}", _SCHEMA_CHUNK, max_tokens=160)
        if obj is None:
            if read == 0:
                return json.dumps({"error": "local model did not answer", "status": local_llm.status().get("last_error", "")})
            break
        read += 1
        # Cite-or-drop: ids outside the shown chunk are discarded; the text is
        # copied from the file, never taken from the model.
        rows_out = [(rel, n, lines[n - 1][:LINE_CLIP], section_of(lines, n, rel)) for n in verify_ids(obj.get("lines"), a, b)]
        (evidence if obj.get("relevant") else weak).extend(rows_out)
        note = str(obj.get("note") or "").strip()
        if obj.get("relevant") and note:
            notes.append(f"{rel}:{a + 1}-{b}: {note[:200]}")
    evidence = rank_evidence(evidence or weak, question)[:MAX_EVIDENCE]
    evidence.sort(key=lambda e: (e[0], e[1]))
    answer, found = "", False
    if evidence:
        quoted = "\n".join(f"{r}:{n}: {t}" + (f"   (in: {sec})" if sec else "") for r, n, t, sec in evidence)
        obj = await chat(_SYS_ANSWER, f"Question: {question}\n\nQuoted lines:\n{quoted}", _SCHEMA_ANSWER, max_tokens=300)
        if obj:
            answer = str(obj.get("answer") or "").strip()
            found = bool(obj.get("found"))
    st = local_llm.status() if chat is local_llm.chat_json else {}
    out: dict[str, Any] = {
        "backend": f"local:{st.get('served_model') or 'model'}",
        "found": found and bool(evidence),
        "answer": answer or ("no relevant lines found" if not evidence else ""),
        "evidence": [f"{r}:{n}: {t}" + (f"   (in: {sec})" if sec else "") for r, n, t, sec in evidence],
        "read": f"{read}/{total} chunks of {CHUNK_LINES} lines",
        "note": "small-model lead; evidence lines are copied from the files (verbatim)",
    }
    if notes and not evidence:
        out["chunk_notes"] = notes[:6]
    if skipped:
        out["skipped"] = skipped
    if read < total:
        out["unread"] = "chunks beyond the budget were ranked lower for this question; ask narrower or pass fewer files"
    return json.dumps(out, ensure_ascii=False)
