"""codocs pytest reporter: streams collection, run and result events as JSON lines.

copied to OUT/work/tools/python/ and loaded with `-p codocs_pytest_report` from PYTHONPATH; it
runs inside the project's interpreter, so it sticks to the stdlib and python 3.6 syntax.

environment:
  CODOCS_PYTEST_EVENTS  event file (appended, one JSON object per line, flushed)
  CODOCS_PYTEST_SELECT  file of requested folded test ids, one per line (others deselected)
  CODOCS_PYTEST_ROOTS   os.pathsep-separated dirs whose files count as project files
  CODOCS_TIMEOUT        seconds; faulthandler dumps the stacks and exits the process after it
  CODOCS_PYTEST_FAULT   file faulthandler writes to (pytest captures fd 2 while tests run)
  CODOCS_PYTEST_PATH_APPEND  dirs appended to sys.path (after the stdlib) when this loads
"""

import faulthandler
import json
import os
import sys
import traceback

import pytest

_OUT = []
_FAULT = []
_ERRORS = {}
_TEXT_CAP = 4000

for _p in os.environ.get("CODOCS_PYTEST_PATH_APPEND", "").split(os.pathsep):
    if _p and _p not in sys.path:
        sys.path.append(_p)


def _emit(obj):
    path = os.environ.get("CODOCS_PYTEST_EVENTS")
    if not path:
        return
    if not _OUT:
        _OUT.append(open(path, "a", buffering=1, encoding="utf-8", errors="replace"))
    f = _OUT[0]
    f.write(json.dumps(obj) + "\n")
    f.flush()


def _fold(nodeid):
    head, sep, tail = nodeid.partition("::")
    k = tail.find("[")
    if sep and k >= 0:
        tail = tail[:k]
    return head + sep + tail


def _cap(text):
    text = text or ""
    return text if len(text) <= _TEXT_CAP else text[-_TEXT_CAP:]


def _roots(config):
    roots = [str(config.rootpath)]
    roots += [r for r in os.environ.get("CODOCS_PYTEST_ROOTS", "").split(os.pathsep) if r]
    out = []
    for r in roots:
        for p in (r, os.path.realpath(r)):
            p = p.rstrip(os.sep) + os.sep
            if p not in out:
                out.append(p)
    return out


def _project(path, roots):
    if not path or path.startswith("<"):
        return False
    if "site-packages" in path or "dist-packages" in path:
        return False
    return any(path.startswith(r) or os.path.realpath(path).startswith(r) for r in roots)


def _original(exc):
    """the innermost cause of pytest's CollectError wrappers."""
    seen = 0
    while exc is not None and type(exc).__name__ == "CollectError" and seen < 10:
        nxt = exc.__cause__ or exc.__context__
        if nxt is None:
            break
        exc = nxt
        seen += 1
    return exc


def _structured(exc, roots, fallback_path):
    """[{path, line, col, type, msg}] for an exception raised while collecting a file."""
    exc = _original(exc)
    if exc is None:
        return []
    name = type(exc).__name__
    if isinstance(exc, SyntaxError) and exc.filename:
        return [{"path": exc.filename, "line": exc.lineno or 0, "col": exc.offset or 0,
                 "type": name, "msg": exc.msg or str(exc)}]
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    pick = None
    for fr in frames:
        if _project(fr.filename, roots):
            pick = fr
    msg = str(exc).splitlines()[0] if str(exc) else ""
    if pick is None:
        return [{"path": fallback_path, "line": 0, "col": 0, "type": name, "msg": msg}]
    return [{"path": pick.filename, "line": pick.lineno or 0, "col": 0, "type": name,
             "msg": msg}]


def pytest_sessionstart(session):
    _emit({"e": "start", "pid": os.getpid(), "python": sys.version.split()[0]})
    try:
        t = float(os.environ.get("CODOCS_TIMEOUT") or 0)
    except ValueError:
        t = 0
    if t > 0:
        target = sys.__stderr__
        path = os.environ.get("CODOCS_PYTEST_FAULT")
        if path:
            try:
                _FAULT.append(open(path, "w"))
                target = _FAULT[0]
            except OSError:
                pass
        faulthandler.dump_traceback_later(t, exit=True, file=target)


def pytest_sessionfinish(session, exitstatus):
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    _emit({"e": "finish", "exitstatus": int(exitstatus)})


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    try:
        rep = outcome.get_result()
    except Exception:
        return
    call = getattr(rep, "call", None)
    if rep.failed and call is not None and call.excinfo is not None:
        path = str(getattr(collector, "path", "") or getattr(collector, "fspath", ""))
        try:
            _ERRORS[rep.nodeid] = _structured(call.excinfo.value, _roots(collector.config),
                                              path)
        except Exception:
            _ERRORS[rep.nodeid] = []


def pytest_collectreport(report):
    if report.failed:
        _emit({"e": "collect_error", "path": report.nodeid,
               "errors": _ERRORS.pop(report.nodeid, []),
               "text": _cap(getattr(report, "longreprtext", "") or str(report.longrepr))})


def pytest_collection_modifyitems(session, config, items):
    select = os.environ.get("CODOCS_PYTEST_SELECT")
    if not select:
        return
    try:
        with open(select, encoding="utf-8") as f:
            wanted = {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return
    keep, drop = [], []
    for item in items:
        (keep if _fold(item.nodeid) in wanted or item.nodeid in wanted else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_collection_finish(session):
    ids = []
    for item in session.items:
        fid = _fold(item.nodeid)
        if fid not in ids:
            ids.append(fid)
    _emit({"e": "collected", "ids": ids})


def pytest_runtest_logstart(nodeid, location):
    _emit({"e": "run", "id": _fold(nodeid), "uid": nodeid})


def pytest_runtest_logreport(report):
    text = ""
    if report.failed or (report.when == "call" and report.skipped):
        parts = [getattr(report, "longreprtext", "") or ""]
        for title, content in getattr(report, "sections", []) or []:
            parts.append("----- %s -----\n%s" % (title, content))
        text = _cap("\n".join(p for p in parts if p))
    _emit({"e": "result", "id": _fold(report.nodeid), "uid": report.nodeid,
           "phase": report.when, "outcome": report.outcome,
           "xfail": hasattr(report, "wasxfail"), "text": text})
