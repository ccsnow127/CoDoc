"""tests for the structure-preserving wiki translator."""

import asyncio

import pytest

from codocs.repowiki.core.cache import Cache
from codocs.repowiki.deepdoc.translator import (
    WikiTranslator,
    _chunk_lines,
    _protect,
    _restore,
    apply_mermaid_translations,
    build_wiki_from_dir,
    fingerprint_diff,
    join_segments,
    mermaid_translatables,
    parse_sidebar,
    split_segments,
    structure_fingerprint,
)

_DOC = """# Title

Intro paragraph with [a link](other.md) and `code`.

## Section

```mermaid
graph TD
  a["common/logger"] -->|"writes structured logs"| b["common/store"]
```

| Col1 | Col2 |
|---|---|
| x | y |

```go
func main() {}
```

Closing prose.
"""


def test_split_and_join_roundtrip():
    segments = split_segments(_DOC)
    assert join_segments(segments) == _DOC.rstrip("\n")
    kinds = [(s.kind, s.lang) for s in segments]
    assert ("code", "mermaid") in kinds and ("code", "go") in kinds
    assert kinds[0] == ("prose", "")


def test_structure_fingerprint_and_diff():
    fp = structure_fingerprint(_DOC)
    assert fp["headings"] == [1, 2]
    assert fp["fences"] == 4
    assert fp["table_rows"] == 3  # header, |---| separator, data row
    assert fp["links"] == ["other.md"]
    assert fingerprint_diff(fp, fp) == []
    other = structure_fingerprint(_DOC.replace("## Section", "### Section"))
    assert fingerprint_diff(fp, other)


def test_protect_restore_roundtrip():
    segments = split_segments(_DOC)
    protected, blocks = _protect(segments)
    assert "```" not in protected
    assert len(blocks) == 2
    restored, problems = _restore(protected, blocks)
    assert problems == []
    assert restored == join_segments(segments)


def test_restore_detects_damaged_placeholders():
    segments = split_segments(_DOC)
    protected, blocks = _protect(segments)
    dropped = "\n".join(ln for ln in protected.splitlines() if "<<<BLOCK_" not in ln)
    restored, problems = _restore(dropped, blocks)
    assert restored is None and problems
    duplicated = protected + "\n" + protected.splitlines()[-1]
    if "<<<BLOCK_" not in duplicated.splitlines()[-1]:
        duplicated = protected + "\n<<<BLOCK_999>>>"
    restored, problems = _restore(duplicated, blocks)
    assert restored is None and problems


def test_chunk_lines():
    text = "\n".join(f"line {i}" for i in range(100))
    chunks = _chunk_lines(text, 200)
    assert len(chunks) > 1
    assert "\n".join(chunks) == text
    assert all(len(c) <= 220 for c in chunks)


def test_mermaid_translatables_filters_identifiers():
    code = (
        'graph TD\n'
        '  a["common/logger"] -->|"writes structured logs"| b["common/store"]\n'
        '  c["service registration flow"] --> a\n'
    )
    strings = mermaid_translatables(code)
    assert "writes structured logs" in strings
    assert "service registration flow" in strings
    assert "common/logger" not in strings  # package path stays


def test_mermaid_translatables_sequence():
    code = (
        "sequenceDiagram\n"
        "  participant SVC as service registration client\n"
        "  SVC->>REG: PUT register the service profile\n"
        "  Note over SVC,REG: heartbeat loop continues\n"
        "  loop every 5 seconds\n"
        "    SVC->>REG: heartbeat\n"
        "  end\n"
    )
    strings = mermaid_translatables(code)
    assert "service registration client" in strings
    assert "PUT register the service profile" in strings
    assert "heartbeat loop continues" in strings
    assert "every 5 seconds" in strings
    assert "heartbeat" not in strings  # single token stays


def test_apply_mermaid_translations_quotes_safe():
    code = '  a["common/logger"] -->|"writes structured logs"| b'
    out = apply_mermaid_translations(code, {"writes structured logs": '構造化"ログ"を書く'})
    assert '構造化' in out and '"' + "構造化" in out or "構造化" in out
    assert '""' not in out  # inner double quotes replaced by single


def test_parse_sidebar_roundtrip():
    text = (
        "# My Project\n\n"
        "- [Overview](index.md)\n"
        "- [Architecture](architecture.md)\n"
        "- **Section A**\n"
        "  - [Page One](page-one.md)\n"
        "  - [Page Two](page-two.md)\n"
        "- [Package Index](package-index.md)\n"
    )
    project, items = parse_sidebar(text)
    assert project == "My Project"
    assert [i.title for i in items] == [
        "Overview", "Architecture", "Section A", "Package Index",
    ]
    section = items[2]
    assert section.page_id == ""
    assert [c.page_id for c in section.children] == ["page-one", "page-two"]


def test_build_wiki_from_dir(tmp_path):
    (tmp_path / "_sidebar.md").write_text(
        "# Proj\n\n- [Overview](index.md)\n- **S**\n  - [P](p.md)\n"
    )
    (tmp_path / "index.md").write_text("# Overview\n\nhello")
    (tmp_path / "p.md").write_text("# P\n\nworld")
    (tmp_path / "extra.md").write_text("# Extra\n\n!")
    (tmp_path / "README.md").write_text("# Proj\n\nlanding")
    wiki = build_wiki_from_dir(tmp_path)
    assert wiki.project_name == "Proj"
    assert [p.id for p in wiki.pages] == ["index", "p", "extra"]
    assert wiki.get_page("extra").title == "Extra"


class _FakeLLM:
    """returns queued responses; records prompts."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0

    async def complete(self, messages, *, max_tokens=4096, **kw):
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else "[LLM Error: exhausted]"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def cache(tmp_path):
    c = Cache(db_path=tmp_path / "cache.db")
    _run(c.init())
    yield c
    _run(c.close())


_SIMPLE = "# Title\n\nHello world.\n\n```go\ncode\n```\n\nBye.\n"


def test_translate_file_success(tmp_path, cache):
    segments = split_segments(_SIMPLE)
    protected, _ = _protect(segments)
    fake_translation = protected.replace("Hello world.", "こんにちは。").replace("Bye.", "さようなら。")
    llm = _FakeLLM([fake_translation])
    tr = WikiTranslator(llm, cache, target_lang="ja")
    out = _run(tr.translate_file("x.md", _SIMPLE))
    assert "こんにちは。" in out
    assert "```go\ncode\n```" in out
    assert tr.report.files_ok == ["x.md"] and not tr.report.files_fallback


def test_translate_file_falls_back_on_broken_structure(tmp_path, cache):
    # both attempts drop the placeholder -> keep English
    llm = _FakeLLM(["# Title\n\nこんにちは。", "# Title\n\nこんにちは。"])
    tr = WikiTranslator(llm, cache, target_lang="ja")
    out = _run(tr.translate_file("x.md", _SIMPLE))
    assert out == _SIMPLE
    assert tr.report.files_fallback == ["x.md"]


def test_translate_file_mermaid_fallback_on_invalid(tmp_path, cache):
    doc = '# T\n\n```mermaid\ngraph TD\n  a["x y"] -->|"do the thing"| b\n```\n\ntext\n'
    segments = split_segments(doc)
    protected, _ = _protect(segments)
    llm = _FakeLLM([
        "sorry, I cannot produce JSON today",  # unparseable label response
        protected.replace("text", "テキスト"),
    ])
    tr = WikiTranslator(llm, cache, target_lang="ja")
    out = _run(tr.translate_file("x.md", doc))
    # diagram kept in source language, prose translated
    assert 'do the thing' in out and "テキスト" in out
    assert tr.report.mermaid_fallback == 1


def test_apply_mermaid_translations_no_cascade_on_substrings():
    code = "  A->>B: set the list\n  C->>D: please set the list now"
    mapping = {
        "set the list": "リストを設定します",
        "please set the list now": "今すぐリストを設定してください",
    }
    out = apply_mermaid_translations(code, mapping)
    assert "リストを設定します" in out
    assert "今すぐリストを設定してください" in out
    assert "設定します 設定します" not in out
    assert "set the list" not in out


def test_apply_mermaid_translations_flattens_newlines():
    code = "  Note over A: examples: x,\\ny"
    out = apply_mermaid_translations(code, {"examples: x,\\ny": "例: x、\ny"})
    assert "\n" not in out.replace("  Note", "Note").split(": ", 1)[1]
    assert "例: x、 y" in out


def test_residual_lines_detects_untranslated_headers():
    from codocs.repowiki.deepdoc.translator import _TARGET_SCRIPTS, residual_lines

    md = (
        "# タイトル\n\n"
        "| Package | Used by | What the symbols indicate |\n"
        "|---|---|---|\n"
        "| `a/b` | 説明済みの行 | 翻訳済み |\n\n"
        "### If you need the most reused packages first\n\n"
        "翻訳済みの本文です。\n\n"
        "```go\nfunc English words here() {}\n```\n"
    )
    found = residual_lines(md, _TARGET_SCRIPTS["ja"])
    values = list(found.values())
    assert any(v.startswith("| Package") for v in values)
    assert any(v.startswith("### If you need") for v in values)
    assert len(found) == 2  # code block and translated lines excluded


def test_residual_sweep_translates_with_guards(tmp_path, cache):
    doc = (
        "# タイトル\n\n"
        "| Package | Used by |\n"
        "|---|---|\n"
        "| `a/b` | 済み |\n"
    )
    llm = _FakeLLM(['{"2": "| パッケージ | 利用元 |"}'])
    tr = WikiTranslator(llm, cache, target_lang="ja")
    out = _run(tr._residual_sweep(doc))
    assert "| パッケージ | 利用元 |" in out
    assert "| Package |" not in out


def test_residual_sweep_rejects_column_change(tmp_path, cache):
    doc = "| Package | Used by |\n|---|---|\n| `a/b` | 済み |\n"
    llm = _FakeLLM(['{"0": "| パッケージのみ |"}'])  # wrong column count
    tr = WikiTranslator(llm, cache, target_lang="ja")
    out = _run(tr._residual_sweep(doc))
    assert "| Package | Used by |" in out  # guard kept the original line
