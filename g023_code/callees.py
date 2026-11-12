"""Callee digests: what the names a Python file imports from its own project are.

Reading code is import-following: report.py calls format_cents and
monthly_totals, so the next hop reads money.py, then ledger.py, then dates.py.
Most of those hops want one fact per name: the signature, the docstring's
first line, and for one-line helpers the expression itself.

When a ReadFile returns a Python source file, this module resolves its
module-level ``from X import a, b`` statements that point inside the project
and adds one ``uses:`` row per imported name:

    format_cents(cents) -> ledgerkit/money.py:15 · 123456 -> '1,234.56'; -5 -> '-0.05'.
    month_key(day) = f"{day.year:04d}-{day.month:02d}" -> ledgerkit/dates.py:10

Bounded (MAX_ROWS rows, ROW_CHARS each), local (ast only), and only for names
the project defines, so it never lists the standard library. Setting
``callee_digest`` (env G023_CALLEE_DIGEST).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

MAX_ROWS = 10
ROW_CHARS = 180
INLINE_EXPR_CHARS = 90
MAX_SOURCE_BYTES = 400_000

_cache: dict[tuple[str, int, int], Optional[ast.Module]] = {}


def enabled() -> bool:
    try:
        from .config import settings

        return bool(getattr(settings, "callee_digest", False))
    except Exception:
        return False


def _parse(path: Path) -> Optional[ast.Module]:
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > MAX_SOURCE_BYTES:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _cache:
        return _cache[key]
    try:
        tree: Optional[ast.Module] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        tree = None
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = tree
    return tree


def _resolve_module(src: Path, root: Path, module: Optional[str], level: int) -> Optional[Path]:
    """The project file a ``from module import ...`` refers to, or None (stdlib, site)."""
    parts = (module or "").split(".") if module else []
    if level:
        base = src.parent
        for _ in range(level - 1):
            base = base.parent
    else:
        base = root
    target = base.joinpath(*parts) if parts else base
    for cand in (target.with_suffix(".py") if parts else None, target / "__init__.py"):
        if cand is None:
            continue
        try:
            cand = cand.resolve()
            cand.relative_to(root)
        except (OSError, ValueError):
            continue
        if cand.is_file():
            return cand
    if not level and parts:
        # src-layout: from pkg.mod import x with pkg under src/
        alt = root / "src"
        if alt.is_dir():
            return _resolve_in(alt, parts, root)
    return None


def _resolve_in(base: Path, parts: list[str], root: Path) -> Optional[Path]:
    target = base.joinpath(*parts)
    for cand in (target.with_suffix(".py"), target / "__init__.py"):
        try:
            cand = cand.resolve()
            cand.relative_to(root)
        except (OSError, ValueError):
            continue
        if cand.is_file():
            return cand
    return None


def _signature(fn: ast.AST) -> str:
    try:
        args = ast.unparse(fn.args)  # type: ignore[attr-defined]
    except Exception:
        args = "..."
    ret = ""
    returns = getattr(fn, "returns", None)
    if returns is not None:
        try:
            ret = " -> " + ast.unparse(returns)
        except Exception:
            ret = ""
    prefix = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    return f"{prefix}{fn.name}({args}){ret}"  # type: ignore[attr-defined]


def _doc_line(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node)  # type: ignore[arg-type]
    except TypeError:
        doc = None
    if not doc:
        return ""
    return " ".join(doc.strip().splitlines()[0].split())


def _inline_expr(fn: ast.AST) -> str:
    """The returned expression of a one-statement function (docstring aside), if short."""
    body = list(getattr(fn, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return ""
    try:
        text = ast.unparse(body[0].value)
    except Exception:
        return ""
    return text if len(text) <= INLINE_EXPR_CHARS and "\n" not in text else ""


def _row(name: str, alias: str, tree: ast.Module, rel: str) -> Optional[str]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            sig = _signature(node)
            if alias != name:
                sig = f"{alias} = {sig}"
            expr = _inline_expr(node)
            row = f"{sig}" + (f" = {expr}" if expr else "") + f" -> {rel}:{node.lineno}"
            doc = _doc_line(node)
            if doc and not expr:
                row += f" · {doc}"
            return row
        if isinstance(node, ast.ClassDef) and node.name == name:
            methods = [
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (not m.name.startswith("_") or m.name == "__init__")
            ]
            listed = ", ".join(_signature(m).replace("(self, ", "(").replace("(self)", "()") for m in methods[:6])
            more = f", +{len(methods) - 6}" if len(methods) > 6 else ""
            head = f"class {name}" if alias == name else f"{alias} = class {name}"
            return f"{head} -> {rel}:{node.lineno}" + (f" · {listed}{more}" if listed else "")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                try:
                    value = ast.unparse(node.value) if node.value is not None else ""
                except Exception:
                    value = ""
                if len(value) > INLINE_EXPR_CHARS:
                    value = value[:INLINE_EXPR_CHARS] + "…"
                return f"{alias} = {value} -> {rel}:{node.lineno}"
    return None


def digest(path: Path, root: Path) -> list[str]:
    """``uses:`` rows for ``path`` (a .py file inside ``root``); empty when nothing resolves."""
    path = Path(path)
    root = Path(root).resolve()
    if path.suffix != ".py":
        return []
    try:
        path = path.resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return []
    tree = _parse(path)
    if tree is None:
        return []
    rows: list[str] = []
    seen: set[tuple[str, str]] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _resolve_module(path, root, node.module, node.level or 0)
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            row = None
            if target is not None and target != path:
                key = (str(target), alias.name)
                if key in seen:
                    continue
                seen.add(key)
                mod_tree = _parse(target)
                if mod_tree is not None:
                    row = _row(alias.name, local, mod_tree, target.relative_to(root).as_posix())
            if row is None:
                # ``from pkg import module``: a submodule, not a name.
                sub = _resolve_module(path, root, ".".join(filter(None, [node.module, alias.name])), node.level or 0)
                if sub is not None and sub != path:
                    sub_tree = _parse(sub)
                    if sub_tree is not None:
                        names = [
                            n.name for n in sub_tree.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                            and not n.name.startswith("_")
                        ]
                        if names:
                            row = f"module {local} -> {sub.relative_to(root).as_posix()} · " + ", ".join(names[:10])
            if row:
                if len(row) > ROW_CHARS:
                    row = row[: ROW_CHARS - 1] + "…"
                rows.append(row)
            if len(rows) >= MAX_ROWS:
                return rows
    return rows


def attach(result: str, path_arg: str, root: Path) -> str:
    """Add ``uses`` to a successful ReadFile JSON result for a Python file."""
    import json

    if not path_arg or not str(path_arg).endswith(".py"):
        return result
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, ValueError, TypeError):
        return result
    if not isinstance(data, dict) or data.get("error"):
        return result
    p = Path(str(data.get("path") or path_arg))
    if not p.is_absolute():
        p = Path(root) / p
    try:
        rows = digest(p, root)
    except Exception:
        return result
    if not rows:
        return result
    data["uses"] = rows
    return json.dumps(data, ensure_ascii=False)
