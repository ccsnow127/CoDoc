"""shared helpers of the non-Go end-to-end tests: a scripted llm behind the real cli, run helpers.

the tests run `codocs run REPO -o OUT --skip wiki` through click's CliRunner with
`LLM.complete` patched (as tests/codocs/test_e2e.py does for Go). replies are keyed on the
system prompt; the target file is `tag.split()[1]`:

- doc (seed/refine) -> the header lines the prompt asks for, then every expected section;
- code -> the original source of the target (decoded with its own codec);
- suite -> a canned, correct suite for the target.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codocs.cli import cli
from codocs.lang.registry import adapter
from codocs.lang.treesitter import decode
from codocs.llm import LLM
from codocs.specforge.docfmt import expected_sections
from codocs.specforge.prompts import CODEGEN, REFINE, SEED, load_prompt
from codocs.tesearch.prompts import refine_system, seed_system

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXT_LANG = {".go": "go", ".py": "python", ".java": "java", ".c": "c", ".cpp": "cpp",
            ".cc": "cpp"}
_HEADER_RE = re.compile(r"```markdown\n(.*?)\n```", re.DOTALL)


def lang_of(rel: str) -> str:
    return EXT_LANG[Path(rel).suffix]


def _go_doc(rel: str, src: str) -> str:
    from codocs.golang.source import top_level_decls

    sigs = {d.qualname: d.signature for d in top_level_decls(src)}
    parts = [f"# {rel}", "", "Fixture file.", ""]
    for kind, name in expected_sections(src):
        parts += [f"## {kind} {name}", f"**{name}**: documented.", ""]
        if sigs.get(name):
            parts += [f"**Signature**: `{sigs[name]}`", ""]
    return "\n".join(parts)


def _lang_doc(user: str, src: str, lang: str) -> str:
    m = _HEADER_RE.search(user)
    header = m.group(1) if m else "# file"
    parts = [header, "", "What this file does."]
    for kind, name in expected_sections(src, lang):
        parts += ["", f"## {kind} {name}", f"**{name}**: does its documented job."]
    return "\n".join(parts) + "\n"


class ScriptedLLM:
    """canned replies keyed on the system prompt; records (kind, rel) per call."""

    def __init__(self, repo: Path, suites: dict[str, str]):
        self.repo = Path(repo)
        self.suites = suites
        self.calls: list[tuple[str, str]] = []

    def _systems(self, lang: str) -> dict[str, str]:
        if lang == "go":
            return {load_prompt(SEED): "doc", load_prompt(REFINE): "doc",
                    load_prompt(CODEGEN): "code", seed_system(): "suite",
                    refine_system(): "suite"}
        return {load_prompt(SEED, lang): "doc", load_prompt(REFINE, lang): "doc",
                load_prompt(CODEGEN, lang): "code", seed_system(lang): "suite",
                refine_system(lang): "suite"}

    def reply(self, system: str, user: str, tag: str) -> str:
        rel = tag.split()[1]
        lang = lang_of(rel)
        src, _ = decode((self.repo / rel).read_bytes())
        kind = self._systems(lang).get(system)
        fence = "go" if lang == "go" else adapter(lang).prompts.fence
        if kind == "doc":
            doc = _go_doc(rel, src) if lang == "go" else _lang_doc(user, src, lang)
            out = f"<documentation>\n{doc}</documentation>"
        elif kind == "code":
            out = f"```{fence}\n{src}```\n"
        elif kind == "suite":
            out = f"```{fence}\n{self.suites[rel]}```\n"
        else:
            raise AssertionError(f"unexpected system prompt for {tag!r}")
        self.calls.append((kind, rel))
        return out


def install(monkeypatch, tmp_path: Path, fixture: str, suites: dict[str, str]):
    """copy the fixture, isolate HOME/cwd/env, patch the llm; -> (repo, out, fake)."""
    home = tmp_path / "home"
    home.mkdir()
    m2 = Path.home() / ".m2"
    if m2.is_dir():                    # maven's local repository stays reachable (offline runs)
        (home / ".m2").symlink_to(m2)
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    for var in ("CODOCS_SOURCE_LANG", "CODOCS_PY", "CODOCS_CC", "CODOCS_CXX", "CODOCS_TEST_CMD"):
        monkeypatch.delenv(var, raising=False)
    go = shutil.which("go")
    if go:
        monkeypatch.setenv("CODOCS_GO", go)
        env = subprocess.run([go, "env", "GOCACHE", "GOMODCACHE", "GOPATH"],
                             capture_output=True, text=True, check=True).stdout.split("\n")
        for key, val in zip(("GOCACHE", "GOMODCACHE", "GOPATH"), env):
            monkeypatch.setenv(key, val.strip())
    repo = tmp_path / "repo" / fixture
    shutil.copytree(FIXTURES / fixture, repo,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "build"))
    fake = ScriptedLLM(repo, suites)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        return fake.reply(system, user, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return repo, tmp_path / "out", fake


def invoke(*args: str):
    return CliRunner().invoke(cli, [str(a) for a in args], catch_exceptions=False)


def run_cli(repo: Path, out: Path, *extra: str):
    return invoke("run", repo, "-o", out, "--skip", "wiki", "--mutants", "4", "--test-rounds",
                  "2", "-j", "3", "--workers", "4", *extra)


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def pkg_dir(rel: str) -> str:
    d = Path(rel).parent.as_posix()
    return "_root" if d in ("", ".") else d


def check_doc(out: Path, rel: str, signal: str | None = None) -> dict:
    """the doc of a non-Go target exists in the RepoWiki layout; phi 1.0 with early stop."""
    pkg, base = pkg_dir(rel), Path(rel).name
    doc = out / "docs" / pkg / "markdown_docs" / f"{base}.md"
    assert doc.is_file(), rel
    assert doc.read_text(encoding="utf-8").startswith(f"# {rel}\n"), rel
    res = load(out / "docgen" / pkg / base / "result.json")
    assert res["status"] == "ok" and res["lang"] == lang_of(rel), res
    assert res["best_phi"] == 1.0, res.get("phi_trajectory")
    assert res["early_stop"] is True and len(res["rounds"]) == 1, res.get("phi_trajectory")
    assert res["rounds"][0]["missing"] == []
    assert (out / "docgen" / pkg / base / "round_1" / f"code{Path(rel).suffix}").is_file()
    if signal:
        assert res["signal"] == signal, (rel, res["signal"])
    return res


def check_suite(out: Path, rel: str, suite_name: str, *, min_cov: float = 0.99) -> dict:
    """the delivered suite exists; the testgen result is ok, non-empty, covering."""
    pkg, base = pkg_dir(rel), Path(rel).name
    res = load(out / "testgen" / pkg / base / "result.json")
    assert res["status"] == "ok" and res["best_size"] > 0, res
    assert res["lang"] == lang_of(rel)
    assert res["best_cov"] is not None and res["best_cov"] >= min_cov, (rel, res["best_cov"])
    delivered = out / "tests" / pkg / suite_name
    assert delivered.is_file(), (rel, sorted(p.name for p in (out / "tests" / pkg).iterdir()))
    return res


def check_summary(out: Path, lang: str, docs: int, suites: int) -> dict:
    summary = load(out / "summary.json")
    assert lang in summary["languages"]
    dg = summary["docgen"]["by_language"][lang]
    assert dg["files"] == docs and dg["mean_best_phi"] == 1.0 and dg["early_stop_rate"] == 1.0
    tg = summary["testgen"]["by_language"][lang]
    assert tg["delivered_suites"] == suites
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Languages" in report and f"| {lang} |" in report
    meta = load(out / "run_meta.json")
    assert set(meta["stages"]) >= {"analyze", "baseline", "testgen", "docgen"}
    return summary


def check_resume(repo: Path, out: Path, fake: ScriptedLLM, *extra: str) -> None:
    first = len(fake.calls)
    res = run_cli(repo, out, *extra)
    assert res.exit_code == 0, res.output
    assert len(fake.calls) == first, fake.calls[first:]


def c_suite_name(rel: str) -> str:
    p = Path(rel)
    return f"{p.stem}_codocs_test{p.suffix}"


def build_and_run(compiler: str, out: Path, repo: Path, rel: str, links: list[str],
                  work: Path, includes: tuple[str, ...] = ()) -> str:
    """compile a delivered suite like a user would: the suite, _support's main, the target."""
    support = out / "tests" / "_support"
    suite = out / "tests" / str(Path(rel).parent) / c_suite_name(rel)
    main = support / ("codocs_main.cpp" if Path(rel).suffix == ".cpp" else "codocs_main.c")
    exe = work / (Path(rel).stem + "_suite")
    target = (repo / rel).resolve()
    std = ["-std=c++17"] if Path(rel).suffix == ".cpp" else []
    obj = work / (Path(rel).stem + "_suite.o")
    steps = [  # -Dmain=... only for the suite's translation unit (it renames a target main)
        [compiler, *std, "-w", "-c", "-I", str(support), "-iquote", str(target.parent),
         *[f"-I{repo / d}" for d in includes],
         f'-DCODOCS_TARGET="{target}"', "-Dmain=codocs_target_main", str(suite), "-o", str(obj)],
        [compiler, *std, "-w", "-I", str(support), str(obj), str(main),
         *[str(repo / x) for x in links], "-o", str(exe)],
    ]
    for cmd in steps:
        build = subprocess.run(cmd, capture_output=True, text=True, cwd=work)
        assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, cwd=work)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "@@CODOCS FAIL" not in run.stdout and "@@CODOCS PASS" in run.stdout, run.stdout
    return run.stdout


def wiki_plan(repo: Path, out: Path, *extra: str, source_lang: str = "auto"):
    """`codocs wiki --plan-only` (no llm) through the cli, and the same plan from run_wiki.

    -> (cli output, run_wiki result with its `languages` and `plan`)."""
    from codocs.config import Settings
    from codocs.wiki import run_wiki

    args = ("--source-lang", source_lang) if source_lang != "auto" else ()
    res = invoke("wiki", repo, "-o", out, "--plan-only", *args, *extra)
    assert res.exit_code == 0, res.output
    plan = asyncio.run(run_wiki(repo, out, Settings(source_lang=source_lang), plan_only=True))
    assert plan["pages"] > 0 and plan["plan"], plan
    return res.output, plan
