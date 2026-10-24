"""shared helpers of the COBOL pipeline / CLI / e2e tests.

a scripted LLM behind the real `codocs` CLI (`LLM.complete` patched, as the other e2e tests
do), keyed on the call's tag and system prompt:

- doc (seed/refine) -> the header lines the prompt asks for, then every expected section
  (ClassDef subsections, VariableDef and MethodDef sections);
- code -> the original source of the target, in a ```cobol block;
- testgen -> the canned plans of runner_fixtures (DATEUTIL, DAYSINM: TC paragraphs; ACCTRPT:
  fixtures and checks); the spec call -> a spec plan for ACCTINQ;
- judge -> judge_fixtures.fake_judgement.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from click.testing import CliRunner
from judge_fixtures import JUDGE_SYSTEM_MARK, fake_judgement
from runner_fixtures import ACCTINQ_SPEC, ACCTRPT_PLAN, DATEUTIL_PLAN, DAYSINM_PLAN, plan

from codocs.cli import cli
from codocs.cobol.source import read_text
from codocs.llm import LLM
from codocs.specforge.docfmt import expected_sections

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_HEADER_RE = re.compile(r"```markdown\n(.*?)\n```", re.DOTALL)

PLANS = {"cbl/DATEUTIL.cbl": DATEUTIL_PLAN, "cbl/DAYSINM.cbl": DAYSINM_PLAN,
         "cbl/ACCTRPT.cbl": ACCTRPT_PLAN, "cbl/ACCTINQ.cbl": ACCTINQ_SPEC}
GENERIC_PLAN = plan("", cases=["C01"])        # other repos: a descriptive plan, no checks


def cobol_doc(user: str, src: str) -> str:
    """a COBOL file doc: the header from the prompt, ClassDef subsections, then every section."""
    m = _HEADER_RE.search(user)
    header = m.group(1) if m else "# file"
    parts = [header, "", "What this program does, in business terms."]
    for kind, name in expected_sections(src, "cobol"):
        parts += ["", f"## {kind} {name}", f"**{name}**: does its documented job."]
        if kind == "ClassDef":
            parts += ["", "### Processing logic", "The main line performs the paragraphs.",
                      "", "### Business rules", f"1. The program {name} ends with GOBACK "
                      "(`0000-MAIN`).", "", "### Error handling", "None.", "",
                      "### Dependencies", "- **Calls**: none"]
    return "\n".join(parts) + "\n"


class PipelineLLM:
    """canned replies for every COBOL prompt; records (kind, rel) per call."""

    def __init__(self, repo: Path, plans: dict[str, str] | None = None, code=None):
        self.repo = Path(repo)
        self.plans = dict(PLANS if plans is None else plans)
        self.code = code or (lambda rel, text: text)
        self.calls: list[tuple[str, str]] = []

    def source(self, rel: str) -> str:
        return read_text(self.repo / rel)[0]

    def reply(self, system: str, user: str, tag: str) -> str:
        parts = tag.split()
        kind = parts[0] if parts else ""
        rel = parts[1] if len(parts) > 1 else ""
        if JUDGE_SYSTEM_MARK in system or kind == "judge":
            self.calls.append(("judge", rel))
            return fake_judgement(user)
        if kind == "doc":
            out = f"<documentation>\n{cobol_doc(user, self.source(rel))}</documentation>"
        elif kind == "code":
            out = "```cobol\n" + self.code(rel, self.source(rel)) + "```\n"
        elif kind == "testgen":
            kind = "spec" if parts[-1] == "spec" else "suite"
            out = self.plans.get(rel, GENERIC_PLAN)
        else:
            raise AssertionError(f"unexpected LLM call {tag!r}")
        self.calls.append((kind, rel))
        return out


def isolate(monkeypatch, tmp_path: Path) -> None:
    """HOME, cwd and the environment of a test run (no real key, no real gateway)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    for var in ("CODOCS_SOURCE_LANG", "CODOCS_EXCLUDE_PATHS", "CODOCS_CODEGEN_MODEL",
                "CODOCS_JUDGE_MODEL", "CODOCS_COBC_FLAGS", "CODOCS_COBOL_COVERAGE",
                "CODOCS_MODEL"):
        monkeypatch.delenv(var, raising=False)


def install(monkeypatch, tmp_path: Path, fixture: str = "cobolmod", **kw):
    """copy the fixture, isolate the run, patch the llm; -> (repo, out, fake)."""
    isolate(monkeypatch, tmp_path)
    repo = tmp_path / "repo" / fixture
    shutil.copytree(FIXTURES / fixture, repo, ignore=shutil.ignore_patterns("__pycache__"))
    fake = PipelineLLM(repo, **kw)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                       validate=None, model=None):
        return fake.reply(system, user, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return repo, tmp_path / "out", fake


def invoke(*args, input: str | None = None):
    return CliRunner().invoke(cli, [str(a) for a in args], catch_exceptions=False, input=input)


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}
