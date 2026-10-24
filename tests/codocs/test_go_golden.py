"""the Go golden check: the Go-only output equals the recorded golden.

tests/codocs/golden_gomod/ holds the normalized output of a Go-only run (the fixture run,
`codocs wiki --plan-only`, `codocs bizlogic --plan-only -k 2`, and every prompt sent). the
snapshot depends on the Go toolchain's behaviour, so the test runs only with the Go release
(major.minor, e.g. go1.27) it was recorded with (golden_gomod/meta.json) and skips otherwise.
regenerate deliberately with `CODOCS_GOLDEN_UPDATE=1 pytest tests/codocs/test_go_golden.py`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
GOLDEN = Path(__file__).resolve().parent / "golden_gomod"
GO = shutil.which("go")
DROP = {"seconds", "finished", "created", "updated"}


def go_version(go: str) -> str:
    out = subprocess.run([go, "version"], capture_output=True, text=True, check=True).stdout
    return out.split()[2]


def go_release(version: str) -> str:
    """major.minor of a go version string: go1.27.3 -> go1.27."""
    m = re.match(r"go\d+\.\d+", version)
    return m.group(0) if m else version


def _norm_json(o):
    if isinstance(o, dict):
        return {k: _norm_json(v) for k, v in o.items() if k not in DROP}
    if isinstance(o, list):
        return [_norm_json(x) for x in o]
    return o


def produce(tmp: Path, setenv) -> dict[str, str]:
    """run the golden flow in tmp (setenv(name, value) isolates the environment)."""
    from click.testing import CliRunner
    from test_e2e import ScriptedLLM

    from codocs.cli import cli
    from codocs.llm import LLM

    env = subprocess.run([GO, "env", "GOCACHE", "GOMODCACHE", "GOPATH"], capture_output=True,
                         text=True, check=True).stdout.split("\n")
    for key, val in zip(("GOCACHE", "GOMODCACHE", "GOPATH"), env):
        setenv(key, val.strip())
    (tmp / "home").mkdir()
    for key, val in (("HOME", os.fspath(tmp / "home")), ("OPENAI_API_KEY", "sk-test"),
                     ("OPENAI_BASE_URL", "http://127.0.0.1:9/v1"), ("CODOCS_GO", GO)):
        setenv(key, val)
    os.chdir(tmp)
    repo, out = tmp / "repo", tmp / "out"
    shutil.copytree(FIXTURE, repo)
    fake = ScriptedLLM(repo)
    prompts: list[dict] = []

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        prompts.append({"tag": tag, "system": system, "user": user})
        return fake.reply(system, tag)

    saved = LLM.complete
    LLM.complete = complete
    try:
        def invoke(*args):
            res = CliRunner().invoke(cli, [str(a) for a in args], catch_exceptions=False)
            assert res.exit_code == 0, res.output
            return res.output

        invoke("run", repo, "-o", out, "--mutants", "4", "--test-rounds", "2", "-j", "1",
               "--workers", "2", "--skip", "wiki")
        wiki = invoke("wiki", repo, "-o", out, "--plan-only")
        invoke("bizlogic", repo, "-o", out, "--plan-only", "-k", "2")
    finally:
        LLM.complete = saved

    def subst(text: str) -> str:
        for p, name in ((out.resolve(), "<OUT>"), (out, "<OUT>"), (repo.resolve(), "<REPO>"),
                        (repo, "<REPO>"), (tmp.resolve(), "<TMP>"), (tmp, "<TMP>")):
            text = text.replace(os.fspath(p), name)
        # the patch release and platform are not part of the snapshot
        return re.sub(r"go version go[\w.+-]+ [\w/-]+", "go version <GO>", text)

    got: dict[str, str] = {}
    for p in sorted(out.rglob("*")):
        rel = p.relative_to(out).as_posix()
        if not p.is_file() or rel.startswith(("work/", "logs/")):
            continue
        text = p.read_bytes().decode("utf-8", errors="replace")
        if p.suffix == ".json":
            d = json.loads(text)
            if rel == "run_meta.json":     # new Settings keys are the allowed difference
                d = {"stages": sorted(d.get("stages", {})), "tokens": d.get("tokens")}
            text = json.dumps(_norm_json(d), indent=1)
        if p.name == "report.md":
            text = re.sub(r"(- \*\*seconds\*\*: ).*", r"\1<t>", text)
            text = re.sub(r"(- \*\*\w+\*\*: )[\d.]+ s$", r"\1<t>", text, flags=re.M)
        got["tree/" + rel] = subst(text)
    got["cli_wiki.txt"] = subst(wiki)
    prompts.sort(key=lambda d: (d["tag"], d["system"], d["user"]))
    got["prompts.json"] = subst(json.dumps(prompts, indent=1))
    return got


def read_golden() -> dict[str, str]:
    return {p.relative_to(GOLDEN).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(GOLDEN.rglob("*")) if p.is_file() and p.name != "meta.json"}


def write_golden(got: dict[str, str], version: str) -> None:
    if GOLDEN.exists():
        shutil.rmtree(GOLDEN)
    for rel, text in got.items():
        p = GOLDEN / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (GOLDEN / "meta.json").write_text(json.dumps({"go_version": version}, indent=1) + "\n")


@pytest.mark.skipif(GO is None, reason="go toolchain not installed")
def test_legacy_output_matches_golden(tmp_path: Path, monkeypatch):
    version = go_version(GO)
    got = produce(tmp_path, monkeypatch.setenv)
    if os.environ.get("CODOCS_GOLDEN_UPDATE") == "1":
        write_golden(got, version)
    meta = json.loads((GOLDEN / "meta.json").read_text())
    if go_release(meta["go_version"]) != go_release(version):
        pytest.skip(f"golden recorded with {meta['go_version']}, this is {version}")
    want = read_golden()
    assert sorted(got) == sorted(want)
    for rel in want:
        assert got[rel] == want[rel], rel
