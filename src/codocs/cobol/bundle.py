"""the COBOL test bundle: one text file holding the test plan, the driver program and the
stub programs of one target, plus the normalisation of the model's JSON reply.

layout (section markers are whole lines starting in column 1):

    *> CODOCS-COBOL-TEST v1
    *> target: <target rel>
    *> program: <PROGRAM-ID of the primary program>
    *> mode: unit|batch|spec
    *> === PLAN ===
    <JSON, sort_keys, indent 2>
    *> === DRIVER ===
    <fixed-format driver (unit mode)>
    *> === STUBS ===
    <stub programs (fixed format)>

test names: unit: every case id with a `TEST <id>` literal in the driver, plus the
checks; batch: the checks (`return-code`, `stdout-NN`, `file-<dd>`); spec: none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codocs.cobol.classify import SPEC_TERMINAL, spec_kind
from codocs.cobol.layout import driver_hint
from codocs.cobol.oracle import weak_cases
from codocs.cobol.reflow import program_ids, reflow_fixed, rename_program
from codocs.cobol.testmodels import FixtureFile, GeneratedTest, Testability, TestCaseSpec
from codocs.repowiki.llm.prompts import extract_json

__all__ = ["close_json", "parse_response", "rename_program", "spec_markdown", "mode_label",
           "Bundle", "parse_bundle", "from_generated", "to_generated", "test_names",
           "test_lines", "remove_tests", "normalize", "is_bundle"]

MAGIC = "*> CODOCS-COBOL-TEST v1"
PLAN_MARK = "*> === PLAN ==="
DRIVER_MARK = "*> === DRIVER ==="
STUBS_MARK = "*> === STUBS ==="
MAX_CASE_ID = 30
_CASE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_HEADER_RE = re.compile(r"^\*> (target|program|mode): ?(.*)$")
_CHECK_IDS = ("return-code",)


# -- JSON reply normalisation ---------------------------------------------------------------


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v)
    return json.dumps(v, ensure_ascii=False)


def _str_map(v) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    return {str(k): (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False))
            for k, x in v.items()}


def _lines(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [_text(x) for x in v]
    return [_text(v)]


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "y", "1")
    return bool(v)


def _stdout_items(v) -> list[str | dict[str, str]]:
    """expected_stdout entries: substrings, or {"line": text} for whole lines."""
    out: list[str | dict[str, str]] = []
    for x in (v if isinstance(v, list) else [v] if v else []):
        if isinstance(x, dict):
            if "line" in x and _text(x["line"]).strip():
                out.append({"line": _text(x["line"])})
            elif _text(x.get("text")).strip():
                out.append(_text(x.get("text")))
        elif _text(x).strip():
            out.append(_text(x))
    return out


def _fixtures(v) -> list[FixtureFile]:
    out: list[FixtureFile] = []
    if not isinstance(v, list):
        return out
    for i, f in enumerate(v):
        if not isinstance(f, dict):
            continue
        dd = str(f.get("dd") or f.get("name") or "").strip()
        raw_path = str(f.get("path") or dd or f"file{i}").replace("\\", "/")
        # relative paths only (dynamic ASSIGNs may name sub dirs); never escape the run dir
        path = "/".join(p for p in PurePosixPath(raw_path).parts if p not in ("/", ".", ".."))
        if not path:
            path = f"file{i}.txt"
        content = f.get("content", f.get("records", ""))
        records: list[str | dict[str, str]] = []
        if isinstance(content, list):
            if any(isinstance(x, dict) for x in content):
                records = [_str_map(x) if isinstance(x, dict) else _text(x) for x in content]
            content = "\n".join(_text(x) for x in content)
        out.append(FixtureFile(dd=dd or path, path=path, content=_text(content),
                               records=records))
    return out


def _case_id(raw: str, i: int) -> str:
    cid = _CASE_ID_RE.sub("-", raw).strip("-") or f"C{i + 1}"
    return cid[:MAX_CASE_ID].rstrip("-") or f"C{i + 1}"


def _cases(v) -> list[TestCaseSpec]:
    out: list[TestCaseSpec] = []
    seen: set[str] = set()
    for i, c in enumerate(v if isinstance(v, list) else []):
        if not isinstance(c, dict):
            continue
        cid = _case_id(str(c.get("id") or f"C{i + 1:02d}"), i)
        while cid in seen:
            cid = (cid[:MAX_CASE_ID - 1] if len(cid) >= MAX_CASE_ID else cid) + "x"
        seen.add(cid)
        out.append(TestCaseSpec(
            id=cid, title=_text(c.get("title")), given=_text(c.get("given")),
            when=_text(c.get("when")), then=_text(c.get("then")),
            inputs=_str_map(c.get("inputs")), expected=_str_map(c.get("expected")),
            ends_run=_flag(c.get("ends_run")),
        ))
    return out


def _free_name(hint: str, taken: set[str]) -> str:
    name = hint[:8]
    n = 0
    while name.upper() in taken:
        n += 1
        name = f"{hint[:6]}{n:02d}"
    return name


def close_json(text: str) -> str:
    """append the brackets a JSON text leaves open (a model sometimes drops the last '}')."""
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return text
    stack: list[str] = []
    in_str = esc = False
    for c in text[start:]:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack and stack[-1] == c:
            stack.pop()
    body = text[start:].rstrip().removesuffix("```").rstrip()
    return body + "".join(reversed(stack))


def parse_response(raw: str, t: Testability, index=None) -> GeneratedTest:
    """normalize the model's JSON into a GeneratedTest (mode comes from classification)."""
    test = GeneratedTest(program_id=t.program_id, mode=t.mode, testability=t, raw_response=raw)
    data = extract_json(raw)
    if data is None:
        data = extract_json(close_json(raw))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        test.error = "response is not a JSON object"
        return test
    test.cases = _cases(data.get("cases"))
    if t.mode == "spec":
        if not test.cases:
            test.error = "no cases in response"
        return test

    test.inputs = _fixtures(data.get("inputs"))
    test.expected_files = _fixtures(data.get("expected_files"))
    test.expected_stdout = _stdout_items(data.get("expected_stdout"))
    rc = data.get("expected_return_code")
    try:
        test.expected_return_code = None if rc in (None, "") else int(rc)
    except (TypeError, ValueError):
        test.expected_return_code = None
    test.env = _str_map(data.get("env"))
    parm = data.get("parm")
    if t.parm_param and parm is not None:
        test.parm = _text(parm)
    test.stdin = _lines(data.get("stdin"))
    stubs = _text(data.get("stubs_source")).strip()
    test.stubs_source = reflow_fixed(stubs) if stubs else ""

    if t.mode == "unit":
        src = _text(data.get("driver_source")).strip()
        if not src:
            test.error = "no driver_source in response"
            return test
        test.driver_source = reflow_fixed(src)
        ids = program_ids(test.driver_source)
        name = ids[0] if ids else str(data.get("driver_name") or "").strip()
        test.driver_name = name or driver_hint(t.program_id)
        taken = {t.program_id.upper(), *(c.upper() for c in t.callees)}
        if index is not None:
            taken |= set(index.programs)
        if test.driver_name.upper() in taken:
            # the driver must not shadow a repo program
            new = _free_name(driver_hint(t.program_id), taken)
            test.driver_source = rename_program(test.driver_source, test.driver_name, new)
            test.driver_name = new
        # deterministic oracle check (no regeneration): PASS lines that check nothing
        test.weak_cases = weak_cases(test.driver_source, [c.id for c in test.cases])
    elif not (test.inputs or test.expected_files or test.expected_stdout
              or test.expected_return_code is not None):
        test.error = "batch response has no inputs or expectations"
    return test


# -- report helpers -------------------------------------------------------------------------


def spec_markdown(test: GeneratedTest) -> str:
    """the human-readable test specification of a spec-mode program."""
    lines = [f"# Test specification: {test.program_id}", ""]
    if test.testability and test.testability.reasons:
        where = ("Not executable headless (needs an interactive terminal): "
                 if spec_kind(test.testability) == SPEC_TERMINAL else
                 "Not executable off-mainframe: ")
        lines += [where + "; ".join(test.testability.reasons), ""]
    for c in test.cases:
        lines.append(f"## {c.id}: {c.title}".rstrip(": "))
        for label, text in (("Given", c.given), ("When", c.when), ("Then", c.then)):
            if text:
                lines.append(f"- **{label}** {text}")
        if c.inputs:
            lines.append("- inputs: " + ", ".join(f"`{k}`={v!r}" for k, v in c.inputs.items()))
        if c.expected:
            lines.append("- expected: "
                         + ", ".join(f"`{k}`={v!r}" for k, v in c.expected.items()))
        lines.append("")
    return "\n".join(lines)


def mode_label(test: GeneratedTest) -> str:
    """'unit' | 'batch' | 'spec (CICS/DB2)' | 'spec (terminal)'."""
    kind = spec_kind(test.testability) if test.mode == "spec" else ""
    return f"spec ({kind})" if kind else test.mode


# -- the bundle file -------------------------------------------------------------------------


@dataclass
class Bundle:
    """a parsed bundle; line numbers are 1-based lines of the bundle text."""

    target: str = ""
    program: str = ""
    mode: str = "unit"
    plan: dict = field(default_factory=dict)
    driver: str = ""
    stubs: str = ""
    plan_line: int = 0      # line of the PLAN marker
    driver_line: int = 0    # line of the DRIVER marker (driver line n = driver_line + n)
    stubs_line: int = 0     # line of the STUBS marker
    error: str = ""

    @property
    def stdout_ids(self) -> list[str]:
        return [str(x.get("id", "")) for x in self.plan.get("expected_stdout") or []
                if isinstance(x, dict)]


def is_bundle(text: str) -> bool:
    return text.lstrip().startswith(MAGIC)


def parse_bundle(text: str) -> Bundle | None:
    """the sections of a bundle text, None when it is not one (or its PLAN is not JSON)."""
    if not is_bundle(text):
        return None
    lines = text.lstrip("﻿").split("\n")
    b = Bundle()
    section = "head"
    plan: list[str] = []
    driver: list[str] = []
    stubs: list[str] = []
    for n, ln in enumerate(lines, start=1):
        if ln.rstrip() == PLAN_MARK:
            section, b.plan_line = "plan", n
            continue
        if ln.rstrip() == DRIVER_MARK:
            section, b.driver_line = "driver", n
            continue
        if ln.rstrip() == STUBS_MARK:
            section, b.stubs_line = "stubs", n
            continue
        if section == "head":
            m = _HEADER_RE.match(ln.rstrip())
            if m:
                setattr(b, m.group(1), m.group(2).strip())
        elif section == "plan":
            plan.append(ln)
        elif section == "driver":
            driver.append(ln)
        else:
            stubs.append(ln)
    try:
        data = json.loads("\n".join(plan) or "{}")
    except ValueError as e:
        b.error = f"the PLAN section is not JSON: {e}"
        data = {}
    b.plan = data if isinstance(data, dict) else {}
    if not b.error and b.plan.get("error"):
        b.error = str(b.plan["error"])
    b.driver = "\n".join(driver).strip("\n")
    b.stubs = "\n".join(stubs).strip("\n")
    return b


def _fixture_dict(fx: FixtureFile) -> dict:
    d: dict = {"dd": fx.dd, "path": fx.path}
    if fx.records:
        d["records"] = list(fx.records)
    else:
        d["content"] = fx.content
    return d


def _fixture(d: dict) -> FixtureFile | None:
    if not isinstance(d, dict):
        return None
    records = d.get("records")
    dd, path = str(d.get("dd", "")), str(d.get("path", ""))
    if isinstance(records, list):
        recs = [_str_map(x) if isinstance(x, dict) else _text(x) for x in records]
        content = "\n".join(_text(x) for x in records)
        if not any(isinstance(x, dict) for x in recs):
            recs = []
        return FixtureFile(dd=dd, path=path, content=content, records=recs)
    return FixtureFile(dd=dd, path=path, content=_text(d.get("content", "")))


def _stdout_plan(items: list, ids: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    used = set(ids or [])
    n = 0
    for k, item in enumerate(items):
        if ids is not None and k < len(ids) and ids[k]:
            sid = ids[k]
        else:
            n += 1
            while f"stdout-{n:02d}" in used:
                n += 1
            sid = f"stdout-{n:02d}"
            used.add(sid)
        if isinstance(item, dict) and "line" in item:
            out.append({"id": sid, "line": str(item["line"])})
        else:
            out.append({"id": sid, "text": str(item.get("text", "") if isinstance(item, dict)
                                                else item)})
    return out


def from_generated(test: GeneratedTest, target: str = "", *,
                   stdout_ids: list[str] | None = None, error: str = "") -> str:
    """the bundle text of a GeneratedTest (stdout ids stdout-01, ... unless given)."""
    if error:
        plan: dict = {"error": error}
    elif test.mode == "spec":
        plan = {"cases": [c.model_dump() for c in test.cases]}
    else:
        plan = {
            "driver_name": test.driver_name,
            "cases": [c.model_dump() for c in test.cases],
            "inputs": [_fixture_dict(f) for f in test.inputs],
            "expected_files": [_fixture_dict(f) for f in test.expected_files],
            "expected_stdout": _stdout_plan(test.expected_stdout, stdout_ids),
            "expected_return_code": test.expected_return_code,
            "stdin": list(test.stdin),
            "env": dict(test.env),
            "parm": test.parm,
        }
    head = [MAGIC, f"*> target: {target}", f"*> program: {test.program_id}",
            f"*> mode: {test.mode}", PLAN_MARK,
            json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False)]
    if test.mode == "spec" or error:
        return "\n".join(head) + "\n"
    driver = test.driver_source.rstrip("\n")
    stubs = test.stubs_source.rstrip("\n")
    parts = head + [DRIVER_MARK] + ([driver] if driver else []) + [STUBS_MARK] + \
        ([stubs] if stubs else [])
    return "\n".join(parts) + "\n"


def to_generated(bundle: str | Bundle, testability: Testability | None = None
                 ) -> GeneratedTest:
    """the GeneratedTest a bundle holds (error set when it is malformed)."""
    b = parse_bundle(bundle) if isinstance(bundle, str) else bundle
    if b is None:
        pid = testability.program_id if testability else ""
        mode = testability.mode if testability else "unit"
        return GeneratedTest(program_id=pid, mode=mode, testability=testability,
                             error="not a COBOL test bundle")
    pid = b.program or (testability.program_id if testability else "")
    mode = b.mode or (testability.mode if testability else "unit")
    test = GeneratedTest(program_id=pid, mode=mode, testability=testability)
    if b.error:
        test.error = b.error
        return test
    p = b.plan
    test.cases = [TestCaseSpec(**{k: v for k, v in c.items()
                                  if k in TestCaseSpec.model_fields})
                  for c in p.get("cases") or [] if isinstance(c, dict) and c.get("id")]
    if mode == "spec":
        return test
    test.driver_name = str(p.get("driver_name") or "")
    test.inputs = [f for f in (_fixture(x) for x in p.get("inputs") or []) if f is not None]
    test.expected_files = [f for f in (_fixture(x) for x in p.get("expected_files") or [])
                           if f is not None]
    stdout: list[str | dict[str, str]] = []
    for item in p.get("expected_stdout") or []:
        if isinstance(item, dict) and "line" in item:
            stdout.append({"line": str(item["line"])})
        elif isinstance(item, dict):
            stdout.append(str(item.get("text", "")))
        else:
            stdout.append(str(item))
    test.expected_stdout = stdout
    rc = p.get("expected_return_code")
    test.expected_return_code = rc if isinstance(rc, int) else None
    test.stdin = [str(x) for x in p.get("stdin") or []]
    test.env = {str(k): str(v) for k, v in (p.get("env") or {}).items()}
    test.parm = p.get("parm") if isinstance(p.get("parm"), str) else None
    test.driver_source = b.driver + "\n" if b.driver else ""
    test.stubs_source = b.stubs + "\n" if b.stubs else ""
    if mode == "unit" and not test.driver_source:
        test.error = "no driver in the bundle"
    return test


# -- static members on bundles ------------------------------------------------------------


def _literal_ids(driver: str) -> list[str]:
    """case ids with a `TEST <id> PASS|FAIL` literal in the driver."""
    out: list[str] = []
    for m in re.finditer(r"""['"]TEST\s+([A-Za-z0-9_-]+)\s+(?:PASS|FAIL)\b""", driver, re.I):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def check_ids(b: Bundle) -> list[str]:
    """the run checks of a bundle: return-code, stdout-NN, file-<dd>."""
    p = b.plan
    out: list[str] = []
    if isinstance(p.get("expected_return_code"), int):
        out.append("return-code")
    out += [sid for sid in b.stdout_ids if sid]
    for f in p.get("expected_files") or []:
        if isinstance(f, dict):
            cid = f"file-{f.get('dd') or f.get('path')}"
            if cid not in out:
                out.append(cid)
    return out


def case_ids(b: Bundle) -> list[str]:
    """unit mode: plan case ids that have a TEST literal in the driver."""
    if b.mode != "unit":
        return []
    lits = set(_literal_ids(b.driver))
    return [str(c["id"]) for c in b.plan.get("cases") or []
            if isinstance(c, dict) and str(c.get("id", "")) in lits]


def test_ids(b: Bundle) -> list[str]:
    if b.error or b.mode == "spec":
        return []
    return case_ids(b) + check_ids(b)


def test_names(src: str, rel: str) -> list[str]:
    """<rel>::<id> per test of a bundle; [] for a malformed or spec bundle."""
    b = parse_bundle(src)
    if b is None:
        return []
    return [f"{rel}::{i}" for i in test_ids(b)]


def _first_line(lines: list[str], start: int, end: int, pattern: re.Pattern) -> int:
    for n in range(start, min(end, len(lines))):
        if pattern.search(lines[n]):
            return n + 1
    return 0


def test_lines(src: str, rel: str) -> dict[str, int]:
    """test name -> bundle line: a case's first `DISPLAY 'TEST <id> ` literal, a check's
    entry in the PLAN section."""
    b = parse_bundle(src)
    if b is None:
        return {}
    lines = src.split("\n")
    plan_end = (b.driver_line - 1) if b.driver_line else len(lines)
    driver_end = (b.stubs_line - 1) if b.stubs_line else len(lines)
    out: dict[str, int] = {}
    for cid in case_ids(b):
        pat = re.compile(r"""['"]TEST\s+""" + re.escape(cid) + r"\s+(?:PASS|FAIL)\b", re.I)
        n = _first_line(lines, b.driver_line, driver_end, pat)
        if n:
            out[f"{rel}::{cid}"] = n
    for cid in check_ids(b):
        if cid == "return-code":
            pat = re.compile(r'"expected_return_code"')
        elif cid.startswith("file-"):
            pat = re.compile(r'"dd":\s*' + re.escape(json.dumps(cid[5:])))
        else:
            pat = re.compile(r'"id":\s*' + re.escape(json.dumps(cid)))
        n = _first_line(lines, b.plan_line, plan_end, pat)
        if cid.startswith("file-") and n:
            # the expected_files entry, not an input of the same dd
            files_at = _first_line(lines, b.plan_line, plan_end, re.compile(r'"expected_files"'))
            if files_at and n < files_at:
                n = _first_line(lines, files_at, plan_end, pat) or n
        if n:
            out[f"{rel}::{cid}"] = n
    return out


_PARA_HEAD_RE = re.compile(r"^ {7}([A-Za-z0-9][A-Za-z0-9_-]*)(?:\s+SECTION)?\s*\.\s*$", re.I)
_END_PROGRAM_RE = re.compile(r"^ {7}\s*END\s+PROGRAM\b", re.I)


def _drop_paragraph(driver: str, cid: str) -> tuple[str, bool]:
    """the driver without paragraph TC-<id> and its PERFORM TC-<id> statement lines."""
    name = f"TC-{cid}".upper()
    lines = driver.split("\n")
    out: list[str] = []
    found = False
    skip = False
    perform = re.compile(r"^\s*PERFORM\s+" + re.escape(name) + r"\s*\.?\s*$", re.I)
    for ln in lines:
        code = ln[7:72] if len(ln) > 7 and ln[6:7] in (" ", "") else ""
        head = _PARA_HEAD_RE.match(ln[:72])
        if head:
            skip = head.group(1).upper() == name
            found = found or skip
            if skip:
                continue
        elif skip and _END_PROGRAM_RE.match(ln[:72]):
            skip = False
        if skip:
            continue
        if code and perform.match(code):
            if code.rstrip().endswith(".") and out:
                # keep the sentence end the PERFORM line carried
                prev = out[-1]
                if prev[7:72].strip() and not prev[:72].rstrip().endswith("."):
                    out[-1] = prev[:72].rstrip() + "." + (prev[72:] if len(prev) > 72 else "")
            continue
        out.append(ln)
    return "\n".join(out), found


def _mute(driver: str, cid: str) -> str:
    """'TEST <id> ' literals rewritten to 'TSKP <id> ' (same length, fixed-format safe)."""
    return re.sub(r"""(['"])TEST(\s+""" + re.escape(cid) + r"\s)", r"\1TSKP\2", driver)


def remove_tests(src: str, names: set[str]) -> str:
    """the bundle without those tests: case paragraphs deleted (or their literals muted),
    check expectations removed from the plan; re-rendered."""
    b = parse_bundle(src)
    if b is None or b.error:
        return src
    ids = {n.split("::", 1)[1] if "::" in n else n for n in names}
    test = to_generated(b)
    stdout_ids = b.stdout_ids
    keep_stdout: list[tuple[str, object]] = []
    for sid, item in zip(stdout_ids, test.expected_stdout):
        if sid not in ids:
            keep_stdout.append((sid, item))
    test.expected_stdout = [item for _, item in keep_stdout]
    if "return-code" in ids:
        test.expected_return_code = None
    test.expected_files = [f for f in test.expected_files
                           if f"file-{f.dd or f.path}" not in ids]
    driver = test.driver_source
    for cid in [c.id for c in test.cases if c.id in ids]:
        driver, found = _drop_paragraph(driver, cid)
        if not found:
            driver = _mute(driver, cid)
    test.driver_source = driver
    test.cases = [c for c in test.cases if c.id not in ids]
    return from_generated(test, b.target, stdout_ids=[sid for sid, _ in keep_stdout])


# -- normalisation -----------------------------------------------------------------------


def _inject_run_markers(driver: str, ids: list[str]) -> str:
    """DISPLAY 'TEST <id> RUN' as the first statement of every TC-<id> paragraph."""
    want = {f"TC-{cid}".upper(): cid for cid in ids}
    lines = driver.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        head = _PARA_HEAD_RE.match(ln[:72])
        cid = want.get(head.group(1).upper()) if head else None
        if cid is not None:
            nxt = next((x for x in lines[i + 1:] if x[7:72].strip()), "")
            if not re.search(r"""['"]TEST\s+""" + re.escape(cid) + r"""\s+RUN['"]""", nxt,
                             re.I):
                out.append(f"           DISPLAY 'TEST {cid} RUN'")
        i += 1
    return "\n".join(out)


def normalize(raw: str, testability: Testability, index=None, target: str = "",
              sut_source: str = "") -> str:
    """the bundle text of a reply (a JSON plan) or of an echoed bundle: parsed, case ids
    cleaned, driver and stubs reflowed (driver renamed on a clash), driver-echo stdout
    expectations dropped, RUN markers injected; an unusable reply gives an error bundle."""
    if is_bundle(raw):
        test = to_generated(raw, testability)
        if test.error:
            return from_generated(test, target, error=test.error)
        if test.driver_source:
            test.driver_source = reflow_fixed(test.driver_source)
        if test.stubs_source:
            test.stubs_source = reflow_fixed(test.stubs_source)
        old = parse_bundle(raw)
        ids = old.stdout_ids if old is not None else None
    else:
        test = parse_response(raw, testability, index)
        ids = None
        if test.error and not (test.driver_source or test.cases and test.mode == "spec"):
            return from_generated(test, target, error=test.error)
    if test.mode == "spec":
        return from_generated(test, target)
    if test.mode == "unit" and test.driver_source:
        from codocs.cobol.harness import _stdout_item, driver_echo

        kept: list = []
        kept_ids: list[str] = []
        for k, item in enumerate(test.expected_stdout):
            if driver_echo(_stdout_item(item)[0], test.driver_source, sut_source):
                continue
            kept.append(item)
            kept_ids.append(ids[k] if ids is not None and k < len(ids) else "")
        test.expected_stdout = kept
        ids = kept_ids if ids is not None else None
        test.driver_source = _inject_run_markers(test.driver_source,
                                                 [c.id for c in test.cases])
    return from_generated(test, target, stdout_ids=ids)
