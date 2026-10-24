"""the COBOL glossary translator: protected code, spans and placeholders, glossary, -j."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from codocs.cobol.wiki.translator import (
    WikiTranslator,
    _prose_prompt,
    _protect_inline,
    _restore_inline,
    build_wiki_from_dir,
    mermaid_translatables,
)
from codocs.repowiki.core.cache import Cache

PAGE = """\
# Account posting

The batch job reads the account file through `ACCOUNT-FILE` and calls [[prog:ACCT01]],
which COPYs [[copy:ACCTREC]] and writes the CICS file. See [the index](program-index.md).

```cobol
       READ ACCOUNT-FILE INTO ACCT-REC
```

```mermaid
flowchart TD
  A["read the account file"] -->|posts the account| B["ACCT01"]
```
"""

WORDS = {"account": "compte", "reads": "lit", "file": "fichier", "posts": "poste",
         "read": "lire", "writes": "ecrit", "calls": "appelle", "batch": "lot"}


def _fr(text: str) -> str:
    return re.sub(r"\b(" + "|".join(WORDS) + r")\b", lambda m: WORDS[m.group(1)], text)


class FakeTranslatorLLM:
    model = "fake-model"

    def __init__(self, drop_token: bool = False, delay: float = 0.0):
        self.calls: list[list[dict]] = []
        self.drop_token = drop_token
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def complete(self, messages: list[dict], max_tokens: int = 4096) -> str:
        self.calls.append(messages)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            user = messages[-1]["content"]
            if user.lstrip().startswith("{"):          # mermaid labels: a JSON object
                import json

                return json.dumps({k: _fr(v) for k, v in json.loads(user).items()})
            out = _fr(user)
            if self.drop_token:
                out = out.replace("<<<CODE_0>>>", "ACCOUNT-FILE")
            return out
        finally:
            self.active -= 1


@pytest.fixture
async def cache(tmp_path: Path):
    c = Cache(db_path=tmp_path / "cache.db")
    await c.init()
    yield c
    await c.close()


def test_inline_protection_round_trip():
    text = "uses `X-Y` and [[prog:ACCT01]] and [[copy:ACCTREC]] but not [x](y.md)"
    protected, spans = _protect_inline(text)
    assert protected == ("uses <<<CODE_0>>> and <<<CODE_1>>> and <<<CODE_2>>> "
                         "but not [x](y.md)")
    assert spans == {0: "`X-Y`", 1: "[[prog:ACCT01]]", 2: "[[copy:ACCTREC]]"}
    assert _restore_inline(protected, spans) == (text, [])
    lost, problems = _restore_inline(protected.replace("<<<CODE_1>>>", ""), spans)
    assert lost is None and "<<<CODE_1>>>" in problems[0]


async def test_translate_file_keeps_code_spans_and_placeholders(cache):
    llm = FakeTranslatorLLM()
    tr = WikiTranslator(llm, cache, "fr")
    out = await tr.translate_file("page.md", PAGE)
    assert "The lot job lit the compte fichier through `ACCOUNT-FILE`" in out
    assert "appelle [[prog:ACCT01]]" in out and "[[copy:ACCTREC]]" in out
    assert "READ ACCOUNT-FILE INTO ACCT-REC" in out     # code blocks are never sent
    assert '"lire the compte fichier"' in out and "|poste the compte|" in out
    assert "(program-index.md)" in out
    assert tr.report.files_ok == ["page.md"]
    prose = next(c for c in llm.calls if "<<<BLOCK_" in c[-1]["content"])
    assert "`ACCOUNT-FILE`" not in prose[-1]["content"]
    assert "<<<CODE_0>>>" in prose[-1]["content"]
    system = prose[0]["content"]
    for term in ("COBOL", "CICS", "JCL", "VSAM", "COMMAREA", "[[prog:NAME]]", "<<<CODE_n>>>"):
        assert term in system
    # a second translation is served from the cache
    n = len(llm.calls)
    assert await WikiTranslator(llm, cache, "fr").translate_file("page.md", PAGE) == out
    assert len(llm.calls) == n


async def test_damaged_tokens_fall_back_to_the_source(cache):
    llm = FakeTranslatorLLM(drop_token=True)
    tr = WikiTranslator(llm, cache, "fr")
    out = await tr.translate_file("page.md", PAGE)
    assert out == PAGE and tr.report.files_fallback == ["page.md"]
    retry = [c for c in llm.calls if "<<<BLOCK_" in c[-1]["content"]][-1]
    assert "<<<CODE_0>>>" in retry[0]["content"]       # the feedback names the token


async def test_translate_dir_concurrency(tmp_path: Path, cache):
    src = tmp_path / "wiki"
    src.mkdir()
    for i in range(6):
        (src / f"p{i}.md").write_text(f"# Page {i}\n\nThe account file {i}.\n")
    (src / "_sidebar.md").write_text("# Demo\n\n- [Page 0](p0.md)\n")
    llm = FakeTranslatorLLM(delay=0.01)
    report = await WikiTranslator(llm, cache, "fr", concurrency=2).translate_dir(
        src, tmp_path / "fr")
    assert len(report.files_ok) == 7 and llm.peak <= 2
    wiki = build_wiki_from_dir(tmp_path / "fr")
    assert wiki.project_name == "Demo" and wiki.pages[0].id == "p0"
    assert "compte fichier" in wiki.pages[0].content


def test_glossary_keeps_mainframe_terms():
    system = _prose_prompt("x", "ja")[0]["content"]
    assert "CICS" in system and "VSAM" in system and "<<<CODE_n>>>" in system
    labels = mermaid_translatables('flowchart TD\n  A["read the file"] -->|LINK| B["ACCT01"]')
    assert labels == ["read the file"]                 # ids and single tokens stay
