"""Exploration ledger: a deterministic, in-memory "session wiki" of what was seen.

Compaction keeps an LLM-written summary, the working set (files written,
verify commands, todos) and a recent tail. What the session *read* - which
files, what kind, what the digest said, which ranges were viewed - lives only
in tool results that compaction drops. A follow-up after compaction then
re-lists and re-reads to rediscover facts it already had.

The ledger records one short entry per explored target straight from tool
results (no API call, no model judgement): path, kind and size, a one-line
gist taken from the digest fields, ranges and focus phrases viewed, and the
file's (mtime, size) at read time. At compaction it renders newest-first under
a character budget as one user item, marking any file that changed or vanished
since it was read, so stale facts are labelled rather than trusted.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

LEDGER_BUDGET_CHARS = 1_500
LEDGER_MAX_ENTRIES = 200
GIST_CHARS = 170
LEDGER_LEADIN = (
    "Exploration notes (recorded from tool results before compaction; a file "
    "marked [changed] or [gone] was modified after it was read - re-read it):\n"
)


def _clip(text: str, n: int = GIST_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _rel(path: str, root: Optional[Path]) -> str:
    if not path:
        return ""
    if root is not None:
        try:
            return Path(path).resolve().relative_to(root.resolve()).as_posix() or "."
        except (ValueError, OSError):
            pass
    return path


def _stamp(path: str) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _kv(details: Any, n: int = 5) -> str:
    if not isinstance(details, dict):
        return ""
    bits = []
    for k, v in list(details.items())[:n]:
        if isinstance(v, (dict, list)):
            continue
        bits.append(f"{k}={v}")
    return " ".join(bits)


def _read_gist(data: dict) -> str:
    """One line of facts from a ReadFile payload (binary, digest or summary)."""
    if data.get("mode") == "binary":
        return _clip(f"{data.get('format', 'binary')} {data.get('size', '')} {_kv(data.get('details'))}")
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
    bits: list[str] = []
    summary = data.get("summary")
    if isinstance(summary, str) and summary:
        bits.append(summary.split("\n", 1)[0])
    else:
        lang, loc = meta.get("language") or data.get("language"), meta.get("loc") or data.get("total_lines")
        if lang or loc:
            bits.append(f"{lang or 'file'}, {loc} lines" if loc else str(lang))
    shape = meta.get("shape")
    if isinstance(shape, str):
        bits.append(shape)
    jl = meta.get("jsonl")
    if isinstance(jl, dict) and jl.get("record_shape"):
        bits.append(f"{jl.get('records')} records {jl['record_shape']}")
    outline = meta.get("outline")
    if isinstance(outline, list) and (meta.get("line_templates") or meta.get("table")):
        # Rare log lines and column profiles are the facts people come back for.
        rows = [r for r in outline if isinstance(r, str)]
        rare = [r.strip() for r in rows if r.startswith("  x")]
        pick = rare[:2] if rare else rows[:3]
        bits.extend(pick)
    return _clip("; ".join(b for b in bits if b))


class Ledger:
    """Newest-last ordered map of explored targets to short entries."""

    def __init__(self) -> None:
        self.entries: "OrderedDict[str, dict]" = OrderedDict()

    def __len__(self) -> int:
        return len(self.entries)

    def clear(self) -> None:
        self.entries.clear()

    def _touch(self, key: str) -> dict:
        e = self.entries.pop(key, None) or {}
        self.entries[key] = e
        while len(self.entries) > LEDGER_MAX_ENTRIES:
            self.entries.popitem(last=False)
        return e

    def note(self, tool: str, args: dict, result: str, root: Optional[Path] = None) -> None:
        """Record what one tool result showed. Never raises."""
        try:
            self._note(tool, args or {}, result, root)
        except Exception:
            pass

    def _note(self, tool: str, args: dict, result: str, root: Optional[Path]) -> None:
        if not result or not result.lstrip().startswith("{"):
            return
        data = json.loads(result)
        if not isinstance(data, dict) or data.get("error"):
            return
        if tool == "ReadFile":
            path = str(data.get("path") or args.get("path") or "")
            rel = _rel(path, root)
            e = self._touch("R:" + rel)
            e["label"] = rel
            e["stamp"] = _stamp(path)
            e["path"] = path
            if data.get("start_line") is not None and "matches" not in data:
                ranges = e.setdefault("ranges", [])
                r = f"L{data.get('start_line')}-{data.get('end_line')}"
                if r not in ranges:
                    ranges.append(r)
                    del ranges[:-4]
                if data.get("total_lines") and not e.get("gist"):
                    e["gist"] = f"{data.get('language') or 'file'}, {data['total_lines']} lines"
            elif args.get("focus"):
                foci = e.setdefault("focus", [])
                f = _clip(args["focus"], 40)
                if f not in foci:
                    foci.append(f)
                    del foci[:-3]
                if not e.get("gist") and data.get("total_lines"):
                    e["gist"] = f"{data.get('language') or 'file'}, {data['total_lines']} lines"
            else:
                g = _read_gist(data)
                if g:
                    e["gist"] = g
        elif tool == "ListDir":
            rel = _rel(str(data.get("path") or args.get("path") or "."), root)
            e = self._touch("L:" + rel)
            e["label"] = rel.rstrip("/") + "/"
            if data.get("digest"):
                rows = [r for r in data.get("lines") or [] if isinstance(r, str)]
                e["gist"] = _clip(f"{data.get('dirs')} dirs, {data.get('files')} files: " + "; ".join(rows[:4]))
            else:
                entries = data.get("entries") or []
                names = [str(x.get("name", "")) + ("/" if x.get("type") == "dir" else "")
                         for x in entries if isinstance(x, dict)]
                e["gist"] = _clip(f"{len(names)} entries: " + ", ".join(names[:12]))
        elif tool == "Glob":
            pat = str(args.get("pattern") or data.get("pattern") or "")
            e = self._touch("G:" + pat + "@" + str(args.get("path") or ""))
            e["label"] = f"glob {pat}"
            rows = data.get("digest") or data.get("files") or []
            e["gist"] = _clip(f"{data.get('total', 0)} matches: " + "; ".join(str(r) for r in rows[:3]))
        elif tool == "SearchContent":
            q = str(args.get("query") or args.get("pattern") or "")
            where = _rel(str(args.get("path") or ""), root) if args.get("path") else ""
            e = self._touch("S:" + q + "@" + where)
            e["label"] = f"search {_clip(q, 50)!r}" + (f" in {where}" if where else "")
            total = data.get("total_matches", data.get("total", data.get("count")))
            files = data.get("files") or []
            paths = []
            for f in files[:4]:
                p = f.get("path") if isinstance(f, dict) else f
                if p:
                    paths.append(_rel(str(p), root))
            e["gist"] = _clip(f"{total if total is not None else '?'} matches" + (": " + ", ".join(paths) if paths else ""))

    def render(self, budget: int = LEDGER_BUDGET_CHARS) -> str:
        """Newest-first lines under ``budget`` chars; empty when nothing was explored."""
        if not self.entries:
            return ""
        lines: list[str] = []
        used = len(LEDGER_LEADIN)
        omitted = 0
        for e in reversed(self.entries.values()):
            tag = ""
            if e.get("path"):
                now = _stamp(e["path"])
                if now is None:
                    tag = " [gone]"
                elif e.get("stamp") and now != e["stamp"]:
                    tag = " [changed]"
            extra = []
            if e.get("ranges"):
                extra.append("viewed " + ",".join(e["ranges"]))
            if e.get("focus"):
                extra.append("focus " + ",".join(repr(f) for f in e["focus"]))
            line = f"- {e.get('label', '?')}{tag}: {e.get('gist') or ''}"
            if extra:
                line += " (" + "; ".join(extra) + ")"
            line = _clip(line, 260)
            if used + len(line) + 1 > budget:
                omitted += 1
                continue
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        if omitted:
            lines.append(f"(+{omitted} older entries omitted)")
        return LEDGER_LEADIN + "\n".join(lines)

    def item(self) -> Optional[dict]:
        text = self.render()
        return {"role": "user", "content": text} if text else None
