"""pytest plugin injected by g023 Code into test runs it executes (test oracle).

Loaded with ``-p g023_pytest_probe`` (PYTEST_ADDOPTS) from this directory
(PYTHONPATH) only when the setting ``test_oracle`` is on and the Bash command
runs pytest. It changes nothing about the run: it only listens to reports and,
at the end of the session, writes one JSON file into the directory named by
``G023_PYTEST_REPORT_DIR`` (a temp dir outside the project). Under
pytest-xdist only the controller writes.

Runs inside the *project's* interpreter, so it sticks to Python 3.6+ syntax and
imports nothing but the standard library and pytest.
"""
import json
import linecache
import os

import pytest

_DIR = os.environ.get("G023_PYTEST_REPORT_DIR")
_STATE = {"tests": {}, "failures": {}, "collect_errors": [], "rootdir": ""}
_MSG_CHARS = 240
_SRC_CHARS = 160
_WORSE = ("failed", "error")


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rel(path):
    root = _STATE["rootdir"]
    path = str(path)
    if root and (path == root or path.startswith(root.rstrip(os.sep) + os.sep)):
        return os.path.relpath(path, root).replace(os.sep, "/")
    return None


def pytest_configure(config):
    root = getattr(config, "rootpath", None) or getattr(config, "rootdir", "")
    _STATE["rootdir"] = os.path.realpath(str(root)) if root else ""


def _locus(excinfo):
    """Deepest traceback frame inside the rootdir (not site-packages): (rel, line, src)."""
    best = None
    try:
        entries = list(excinfo.traceback)
    except Exception:
        return None
    for entry in entries:
        try:
            path = os.path.realpath(str(entry.path))
        except Exception:
            continue
        if "site-packages" in path or os.sep + "_pytest" + os.sep in path:
            continue
        rel = _rel(path)
        if rel is None:
            continue
        line = int(entry.lineno) + 1
        best = (rel, line, path)
    if best is None:
        return None
    rel, line, path = best
    src = _short(linecache.getline(path, line).strip(), _SRC_CHARS)
    return {"at": "%s:%d" % (rel, line), "src": src}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.excinfo is None:
        return
    try:
        rep = outcome.get_result()
    except Exception:
        return
    if not rep.failed:
        return
    info = {"when": call.when}
    try:
        info["err"] = _short(call.excinfo.exconly().splitlines()[0], _MSG_CHARS)
    except Exception:
        info["err"] = ""
    loc = _locus(call.excinfo)
    if loc:
        info.update(loc)
    _STATE["failures"].setdefault(item.nodeid, info)


def pytest_runtest_logreport(report):
    nodeid = report.nodeid
    if getattr(report, "wasxfail", None) is not None:
        outcome = "xfailed" if report.skipped else "xpassed"
    elif report.failed:
        outcome = "failed" if report.when == "call" else "error"
    elif report.skipped:
        outcome = "skipped"
    elif report.when == "call":
        outcome = "passed"
    else:
        return
    prev = _STATE["tests"].get(nodeid)
    if prev in _WORSE:
        return
    _STATE["tests"][nodeid] = outcome
    if report.failed and nodeid not in _STATE["failures"]:
        # xdist controller: no excinfo here, use the serialised crash line.
        crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
        info = {"when": report.when}
        if crash is not None:
            info["err"] = _short(getattr(crash, "message", ""), _MSG_CHARS)
            rel = _rel(os.path.realpath(str(getattr(crash, "path", ""))))
            if rel:
                info["at"] = "%s:%s" % (rel, getattr(crash, "lineno", "?"))
        _STATE["failures"][nodeid] = info


def pytest_collectreport(report):
    if report.failed:
        text = ""
        try:
            text = str(report.longrepr).strip().splitlines()[-1]
        except Exception:
            pass
        _STATE["collect_errors"].append({"id": report.nodeid, "err": _short(text, _MSG_CHARS)})


def pytest_sessionfinish(session, exitstatus):
    if not _DIR or hasattr(session.config, "workerinput"):
        return
    data = {
        "v": 1,
        "rootdir": _STATE["rootdir"],
        "exit": int(exitstatus),
        "tests": _STATE["tests"],
        "failures": _STATE["failures"],
        "collect_errors": _STATE["collect_errors"],
    }
    try:
        path = os.path.join(_DIR, "report-%d.json" % os.getpid())
        with open(path, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass
