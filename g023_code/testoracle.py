"""Test oracle: structured pytest facts that survive the model's own output filter.

Measured on live runs: the model runs ``pytest -q | tail -5``, the filter hides
which test failed (or where), and the next call is a narrower re-run to find
out. A check written by the model this turn is also invisible as such: the
model re-runs it by id "to confirm my tests pass".

The harness can record those facts at the source. When a Bash command runs
pytest (and the setting ``test_oracle`` is on), the command's environment
gets ``PYTHONPATH=<g023_code/probes>`` and ``PYTEST_ADDOPTS=-p
g023_pytest_probe``; the plugin writes a JSON report per pytest process into a
temporary directory outside the project (so the project stat fingerprint does
not change). After the command the report is read and the directory removed.

What reaches the model (``tests`` rows on the Bash result) is only what its
view did not already show:

  * failing tests whose id the visible output does not contain, with the
    deepest project frame (``at path:line``), that line's source and the
    exception line (failure locus);
  * a "(new this turn)" tag on failing tests this turn added or changed (AST
    diff of the test files against the turn's pre-images);
  * the vacuity verdict for new passing tests (see :func:`vacuity_command`).

Safety: commands that hand pytest a fresh environment (tox, nox, docker, ssh,
``env -i``, explicit PYTHONPATH/PYTEST_ADDOPTS) are not touched. If pytest
reports that the plugin could not be loaded, the command is re-run once
without it and the oracle stays off for the session.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

PROBE_DIR = Path(__file__).resolve().parent / "probes"
PLUGIN = "g023_pytest_probe"
REPORT_ENV = "G023_PYTEST_REPORT_DIR"
MAX_FAIL_ROWS = 8
MAX_NEW_ROWS = 12

_PYTEST = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?(?:py\.test|pytest)(?=[\s;&|)`]|$)")
_PYTEST_MODULE = re.compile(r"python[\d.]*\s+(?:-\S+\s+)*-m\s+pytest\b")
_EXCLUDE = re.compile(
    r"(?:^|[\s;&|(])(?:tox|nox|docker|podman|ssh|kubectl|nix-shell)(?:\s|$)"
    r"|\benv\s+-i\b|\bPYTHONPATH=|\bPYTEST_ADDOPTS=|\bpython[\d.]*\s+-I\b|-p\s+no:" + PLUGIN
)
_PLUGIN_FAIL = re.compile(
    r"(?:Error importing plugin|ImportError while loading plugin|No module named)\W+" + PLUGIN
)
_PREFIX = re.compile(r"((?:\S*python[\d.]*\s+(?:-\S+\s+)*-m\s+pytest)|(?:\S*/)?(?:py\.test|pytest))(?=\s|$)")
_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py)$")


def enabled() -> bool:
    try:
        from .config import settings

        return bool(getattr(settings, "test_oracle", False))
    except Exception:
        return False


def wants(command: str) -> bool:
    """Does this shell command run pytest in an environment we may extend?"""
    if not command or PLUGIN in command and "-p" in command:
        return False
    if not (_PYTEST.search(command) or _PYTEST_MODULE.search(command)):
        return False
    return not _EXCLUDE.search(command)


def plugin_failed(output: str) -> bool:
    return bool(_PLUGIN_FAIL.search(output or ""))


def is_test_file(rel: str) -> bool:
    return bool(_TEST_FILE.search(rel.replace(os.sep, "/")))


class Probe:
    """One injected run: a private report dir plus the env additions."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="g023_oracle_"))

    def env(self, base: dict) -> dict:
        env = dict(base)
        prior = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(PROBE_DIR) + (os.pathsep + prior if prior else "")
        addopts = env.get("PYTEST_ADDOPTS", "")
        env["PYTEST_ADDOPTS"] = (addopts + " " if addopts else "") + f"-p {PLUGIN}"
        env[REPORT_ENV] = str(self.dir)
        return env

    def collect(self) -> Optional[dict]:
        """Merge every report the run wrote; None when pytest wrote none."""
        try:
            files = sorted(self.dir.glob("report-*.json"), key=lambda p: p.stat().st_mtime_ns)
        except OSError:
            files = []
        merged: Optional[dict] = None
        for f in files:
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if merged is None:
                merged = {"rootdir": data.get("rootdir", ""), "exit": 0, "tests": {},
                          "failures": {}, "collect_errors": []}
            merged["tests"].update(data.get("tests") or {})
            merged["failures"].update(data.get("failures") or {})
            merged["collect_errors"].extend(data.get("collect_errors") or [])
            code = int(data.get("exit") or 0)
            if code and not merged["exit"]:
                merged["exit"] = code
        return merged

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Turn novelty: which test functions did this turn add or change?
# ---------------------------------------------------------------------------

def _test_defs(source: Optional[bytes]) -> dict[str, str]:
    """{qualname: ast dump} of test functions (module level and in classes)."""
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    out: dict[str, str] = {}

    def visit(body: list, owner: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                out[owner + node.name] = ast.dump(node, include_attributes=False)
            elif isinstance(node, ast.ClassDef) and not owner:
                visit(node.body, node.name + ".")

    visit(tree.body, "")
    return out


def novel_tests(preimages: dict[str, Optional[bytes]], root: Path) -> set[tuple[str, str]]:
    """(rel path, qualname) of test functions this turn added or changed."""
    found: set[tuple[str, str]] = set()
    for rel, before in preimages.items():
        if not is_test_file(rel):
            continue
        try:
            now = (Path(root) / rel).read_bytes()
        except OSError:
            continue
        old_defs = _test_defs(before)
        for name, dump in _test_defs(now).items():
            if old_defs.get(name) != dump:
                found.add((rel, name))
    return found


def _split_id(nodeid: str, rootdir: str, root: Path) -> tuple[str, str]:
    """nodeid -> (project-relative file, qualname without parameters)."""
    path, _, rest = nodeid.partition("::")
    if rootdir:
        try:
            path = (Path(rootdir) / path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    qual = ".".join(part.split("[", 1)[0] for part in rest.split("::") if part)
    return path, qual


def novel_ids(report: dict, novel: set[tuple[str, str]], root: Path) -> dict[str, str]:
    if not novel:
        return {}
    rootdir = str(report.get("rootdir") or "")
    out = {}
    for nodeid, outcome in (report.get("tests") or {}).items():
        if _split_id(nodeid, rootdir, root) in novel:
            out[nodeid] = outcome
    return out


# ---------------------------------------------------------------------------
# Rendering: only facts the visible output does not already carry
# ---------------------------------------------------------------------------

_ORDER = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")


def summary_line(report: dict) -> str:
    counts = Counter((report.get("tests") or {}).values())
    bits = [f"{counts[k]} {k}" for k in _ORDER if counts.get(k)]
    errs = len(report.get("collect_errors") or [])
    if errs:
        bits.append(f"{errs} collection error{'s' if errs > 1 else ''}")
    return ", ".join(bits) or "no tests ran"


def render(
    report: dict,
    visible: str,
    filtered: bool,
    new: Optional[dict[str, str]] = None,
    vacuous: Iterable[str] = (),
) -> list[str]:
    """``tests`` rows for a Bash result, or [] when the output already said it all."""
    visible = visible or ""
    rows: list[str] = []
    failures = report.get("failures") or {}
    failing = [i for i, o in (report.get("tests") or {}).items() if o in ("failed", "error")]
    shown = 0
    for nodeid in failing:
        info = failures.get(nodeid) or {}
        at = str(info.get("at") or "")
        hidden_id = nodeid not in visible
        hidden_at = bool(at) and at not in visible
        if not (hidden_id or hidden_at):
            continue
        if shown >= MAX_FAIL_ROWS:
            rows.append(f"... {len(failing) - shown} more failing")
            break
        shown += 1
        row = f"FAIL {nodeid}" + (" (new this turn)" if new and nodeid in new else "")
        if at:
            row += f" @ {at}"
            if info.get("src"):
                row += f"  {info['src']}"
        if info.get("err"):
            row += f"  -- {info['err']}"
        rows.append(row)
    for err in (report.get("collect_errors") or [])[:3]:
        if str(err.get("id") or "") not in visible:
            rows.append(f"COLLECT ERROR {err.get('id')}  -- {err.get('err')}")
    vac = sorted(vacuous)
    if vac:
        rows.append(
            "vacuous (also pass with this turn's source edits reverted; they do not test the change): "
            + ", ".join(vac[:MAX_NEW_ROWS])
        )
    if not rows and not filtered:
        return []
    if rows or filtered:
        line = summary_line(report)
        # A filtered view usually keeps pytest's own summary line; repeat it
        # only when something above needs the context or it is not visible.
        if rows or not _summary_visible(visible, report):
            rows.insert(0, line)
    return rows


def _summary_visible(visible: str, report: dict) -> bool:
    counts = Counter((report.get("tests") or {}).values())
    for key in ("passed", "failed"):
        n = counts.get(key)
        if n and f"{n} {key}" not in visible:
            return False
    return True


# ---------------------------------------------------------------------------
# Vacuity: do the new passing tests depend on this turn's source edits?
# ---------------------------------------------------------------------------

def runner_prefix(command: str) -> Optional[str]:
    """The pytest invocation the model used (``.venv/bin/python -m pytest``)."""
    head = re.split(r"\s*(?:\|\||&&|[|;])\s*", command)
    for part in head:
        m = _PREFIX.search(part)
        if m and wants(part):
            return m.group(1)
    return None


def vacuity_command(command: str, nodeids: Iterable[str]) -> Optional[str]:
    prefix = runner_prefix(command)
    ids = sorted(nodeids)
    if prefix is None or not ids:
        return None
    import shlex

    return f"{prefix} -q -p no:cacheprovider " + " ".join(shlex.quote(i) for i in ids)


def source_preimages(changed: dict[str, Optional[bytes]]) -> dict[str, Optional[bytes]]:
    """The turn's changed non-test files (what the vacuity shadow reverts)."""
    return {rel: data for rel, data in changed.items() if not is_test_file(rel) and rel.endswith(".py")}
