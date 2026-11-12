"""Request obligations: what the user's message commits the turn to, checked by the harness.

Two kinds, both compiled from the request text with regular expressions and
checked locally (ast, path rules), never by a model call:

Positive — named callables. "Add a function average_entry(ledger, account) to
ledgerkit/ledger.py" names a callable with a shape: a module-level function
whose parameters are (ledger, account). A measured failure mode is the model
writing ``Ledger.average_entry(self, account)`` instead and answering "done".
At the point where the model gives its final answer (the commit point), the
harness looks for the definition; when it is missing or has another shape, it
sends one corrective note and the turn continues (at most once per turn).
Only signatures count (``name(a, b)`` with identifier parameters, near a
creation verb such as add/implement/create); calls with literal arguments
(``format_cents(-5)``) are examples, not obligations.

Negative — protected files. "do not change the tests" / "don't modify
config.py" becomes a write guard: the first Edit/WriteFile to a protected
existing file is refused with the quoted constraint; repeating the identical
call goes through (the model may have a reason; the guard makes it a decision
instead of an accident). Shell commands are not guarded.

Settings ``obligations`` (G023_OBLIGATIONS) and ``write_guards``
(G023_WRITE_GUARDS).
"""
from __future__ import annotations

import ast
import builtins
import keyword
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_SCAN_FILES = 2000
MAX_FILE_BYTES = 400_000
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".g023", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".venv", "venv", "node_modules", "build", "dist", "site-packages",
})

_CREATE = re.compile(
    r"\b(?:add|adds|adding|implement|implementing|create|creating|write|writing|define|defining|"
    r"introduce|expose|provide|new|need|needs)\b",
    re.I,
)
_SIG = re.compile(r"(?<![\w.])((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\(([^()\n]{0,160})\)")
_PARAM = re.compile(r"^\*{0,2}([A-Za-z_]\w*)\s*(?::[^=]+)?(?:=.+)?$")
_METHOD_HINT = re.compile(r"\bmethod\b", re.I)
_MODULE_HINT = re.compile(r"\b(?:module[- ]level|top[- ]level|standalone|free)\s+function\b|\bfunction\b", re.I)

_NEG = re.compile(
    r"\b(?:do\s+not|don'?t|never|must\s+not|should\s+not|shouldn'?t|without)\s+"
    r"(?:\w+\s+){0,2}?(?:change|changing|modify|modifying|touch|touching|edit|editing|alter|altering|"
    r"rewrite|rewriting|update|updating)\s+"
    r"((?:(?:any|the|of|existing|those|these|its)\s+){0,3})"
    r"([`'\"]?[\w./*-]+[`'\"]?)",
    re.I,
)
_TEST_WORDS = frozenset({"test", "tests", "testcase", "testcases", "test-suite", "testsuite", "specs"})
_BUILTIN_NAMES = frozenset(dir(builtins)) | frozenset(keyword.kwlist)


@dataclass
class CallableObligation:
    name: str
    owner: Optional[str]  # class name when the request says Owner.name(...)
    params: list[str]
    quote: str
    method_hint: bool = False

    def signature(self) -> str:
        head = f"{self.owner}.{self.name}" if self.owner else self.name
        return f"{head}({', '.join(self.params)})"


@dataclass
class Guard:
    kind: str  # "tests" | "path"
    target: str
    quote: str


@dataclass
class Obligations:
    callables: list[CallableObligation] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.callables or self.guards)


def _params(text: str) -> Optional[list[str]]:
    text = text.strip()
    if not text:
        return []
    out = []
    for raw in text.split(","):
        raw = raw.strip()
        if raw in ("*", "/", "..."):
            continue
        m = _PARAM.match(raw)
        if not m:
            return None  # a literal or expression: an example call, not a signature
        out.append(m.group(1))
    return out


def extract(request: str, root: Optional[Path] = None) -> Obligations:
    """Obligations stated by ``request`` (empty when it states none)."""
    found = Obligations()
    text = request or ""
    seen: set[str] = set()
    for m in _SIG.finditer(text):
        dotted, inner = m.group(1), m.group(2)
        owner, _, name = dotted.rpartition(".")
        if name in _BUILTIN_NAMES or len(name) < 3 or name in seen:
            continue
        params = _params(inner)
        if params is None:
            continue
        window = text[max(0, m.start() - 120): m.start()]
        # The verb must be in the same sentence, before the name.
        sentence = re.split(r"[.!?]\s|\n", window)[-1]
        if not _CREATE.search(sentence):
            continue
        if owner and not owner[:1].isupper():
            owner = ""  # module.func: a module prefix, not a class
        around = text[max(0, m.start() - 40): m.end() + 40]
        seen.add(name)
        found.callables.append(CallableObligation(
            name=name,
            owner=owner or None,
            params=params,
            quote=text[max(0, m.start() - len(sentence)): m.end()].strip()[:200],
            method_hint=bool(_METHOD_HINT.search(around)) and not _MODULE_HINT.search(sentence[-60:] + m.group(0)),
        ))
    for m in _NEG.finditer(text):
        target = m.group(2).strip("`'\"").rstrip(".,;:")
        quote = m.group(0).strip()
        low = target.lower()
        if low in _TEST_WORDS or low.startswith("test"):
            if not any(g.kind == "tests" for g in found.guards):
                found.guards.append(Guard("tests", "tests", quote))
            continue
        if "/" in target or re.search(r"\.\w{1,6}$", target):
            found.guards.append(Guard("path", target.strip("/"), quote))
    return found


# ---------------------------------------------------------------------------
# Positive obligations: find definitions and compare shapes
# ---------------------------------------------------------------------------

@dataclass
class Definition:
    rel: str
    line: int
    owner: Optional[str]
    params: list[str]

    def describe(self) -> str:
        head = f"{self.owner}.{self.name_}" if self.owner else self.name_
        return f"{head}({', '.join(self.params)}) at {self.rel}:{self.line}"

    name_: str = ""


def _py_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                count += 1
                if count > MAX_SCAN_FILES:
                    return
                yield Path(dirpath) / f


def find_definitions(name: str, root: Path) -> list[Definition]:
    root = Path(root)
    out: list[Definition] = []
    for p in _py_files(root):
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name not in src:
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        rel = p.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                out.append(Definition(rel, node.lineno, None, _arg_names(node), name_=name))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        out.append(Definition(rel, sub.lineno, node.name, _arg_names(sub), name_=name))
    return out


def _arg_names(fn: ast.AST) -> list[str]:
    a = fn.args  # type: ignore[attr-defined]
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        names.append(a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _matches(ob: CallableObligation, d: Definition) -> bool:
    params = list(d.params)
    if d.owner is not None and params and params[0] in ("self", "cls") and (not ob.params or ob.params[0] not in ("self", "cls")):
        params = params[1:]
    if ob.owner and d.owner != ob.owner:
        return False
    if not ob.owner and d.owner is not None and not ob.method_hint:
        return False  # the request named a function; a method has another call shape
    if ob.method_hint and d.owner is None:
        return False
    return len(params) == len(ob.params) or params[: len(ob.params)] == ob.params


def unmet(obligations: Obligations, root: Path) -> list[str]:
    """One sentence per callable obligation the code does not meet."""
    problems: list[str] = []
    for ob in obligations.callables:
        defs = find_definitions(ob.name, root)
        if any(_matches(ob, d) for d in defs):
            continue
        want = ob.signature()
        kind = "method" if (ob.owner or ob.method_hint) else "module-level function"
        if not defs:
            problems.append(f"the request asks for {kind} `{want}`; no definition of `{ob.name}` exists in the project.")
        else:
            have = "; ".join(d.describe() for d in defs[:3])
            problems.append(f"the request asks for {kind} `{want}`; the project has {have}.")
    return problems


def corrective_note(problems: list[str]) -> str:
    return (
        "[harness check] Before you answer: "
        + " ".join(problems)
        + " If the request means exactly that, add or fix it now (keep what already works); "
        "if you are sure the existing code satisfies the request, give your final answer and say why."
    )


# ---------------------------------------------------------------------------
# Negative obligations: write guards
# ---------------------------------------------------------------------------

def guarded_by(guards: list[Guard], rel: str, existed: bool) -> Optional[Guard]:
    """The guard that protects project-relative ``rel`` (only files that existed)."""
    if not existed:
        return None
    rel = rel.replace(os.sep, "/").lstrip("./")
    for g in guards:
        if g.kind == "tests":
            parts = rel.split("/")
            name = parts[-1]
            if (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py") \
                    or any(p in ("tests", "test") for p in parts[:-1]) or name == "conftest.py":
                return g
        elif g.kind == "path":
            t = g.target.lstrip("./")
            if rel == t or rel.startswith(t.rstrip("/") + "/") or (("/" not in t) and rel.split("/")[-1] == t):
                return g
    return None
