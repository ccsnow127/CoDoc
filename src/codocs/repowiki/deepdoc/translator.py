"""LLM translation of an exported markdown wiki, structure-preserving.

Strategy: protect code blocks behind placeholders, translate prose whole-file,
translate mermaid labels separately with strict validation, and fall back to
the untranslated original whenever a machine check fails — content fidelity
beats translation coverage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from codocs.repowiki.core.cache import Cache, content_hash
from codocs.repowiki.core.wiki_builder import SidebarItem, Wiki, WikiPage
from codocs.repowiki.deepdoc import mermaid
from codocs.repowiki.llm.client import LLMClient
from codocs.repowiki.llm.prompts import extract_json

logger = logging.getLogger(__name__)

TRANSLATE_PROMPT_VERSION = "translate-v4"

_CHUNK_CHARS = 14_000  # split protected prose into chunks this large for the LLM
_MAX_TOKENS = 16_000

_PLACEHOLDER_FMT = "<<<BLOCK_{i}>>>"
_PLACEHOLDER_RE = re.compile(r"<<<BLOCK_(\d+)>>>")

_LANGUAGES = {
    "ja": "Japanese. Use polite です・ます体 for prose.",
    "ko": "Korean.",
    "en": "English.",
}

# only acronyms and product names stay in English; ordinary technical concepts
# must use the target language's established terminology
_KEEP_ACRONYMS = (
    "API, SDK, CLI, HTTP, HTTPS, JSON, YAML, XML, URL, SQL, REST, RPC, gRPC, TCP, UDP, "
    "DNS, TLS, KV, PaaS, CI/CD"
)
_KEEP_PRODUCTS = (
    "Go, Python, Java, Kubernetes, Docker, Helm, MongoDB, Redis, PostgreSQL, MySQL, Kafka, "
    "Prometheus, Grafana, Gin, gRPC, Protocol Buffers, OpenTelemetry"
)


@dataclass
class Segment:
    kind: str  # "prose" | "code"
    text: str
    lang: str = ""  # fence language for code segments


@dataclass
class TranslateReport:
    files_ok: list[str] = field(default_factory=list)
    files_fallback: list[str] = field(default_factory=list)  # kept in source language
    mermaid_fallback: int = 0  # diagrams kept in source language


# -- structure-preserving primitives -------------------------------------


def split_segments(md: str) -> list[Segment]:
    """split a markdown document into alternating prose / fenced-code segments."""
    segments: list[Segment] = []
    cur: list[str] = []
    in_code = False
    fence_lang = ""

    def flush(kind: str, lang: str = "") -> None:
        nonlocal cur
        if cur:
            segments.append(Segment(kind=kind, text="\n".join(cur), lang=lang))
            cur = []

    for line in md.splitlines():
        if line.startswith("```"):
            if in_code:
                cur.append(line)
                flush("code", fence_lang)
                in_code = False
            else:
                flush("prose")
                cur = [line]
                in_code = True
                fence_lang = line[3:].strip()
        else:
            cur.append(line)
    flush("code" if in_code else "prose", fence_lang if in_code else "")
    return segments


def join_segments(segments: list[Segment]) -> str:
    return "\n".join(s.text for s in segments)


def structure_fingerprint(md: str) -> dict:
    """machine-checkable structure of a page; must survive translation intact."""
    headings: list[int] = []
    fences = 0
    table_rows = 0
    links: set[str] = set()
    in_code = False
    for line in md.splitlines():
        if line.startswith("```"):
            fences += 1
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s", line)
        if m:
            headings.append(len(m.group(1)))
        if line.lstrip().startswith("|"):
            table_rows += 1
        links.update(re.findall(r"\]\(([^)\s]+\.md)\)", line))
    return {
        "headings": headings,
        "fences": fences,
        "table_rows": table_rows,
        "links": sorted(links),
    }


def fingerprint_diff(a: dict, b: dict) -> list[str]:
    problems = []
    for key in ("headings", "fences", "table_rows", "links"):
        if a[key] != b[key]:
            problems.append(f"{key} changed: {a[key]!r} -> {b[key]!r}"[:200])
    return problems


# -- mermaid label extraction ---------------------------------------------

_MM_QUOTED_RE = re.compile(r'"([^"]+)"')
_MM_EDGE_LABEL_RE = re.compile(r"\|([^|]+)\|")
_MM_SEQ_MSG_RE = re.compile(r"^\s*[\w()+-]+\s*-{1,2}(?:>>?|[)x])\s*[+-]?\s*[\w()]+\s*:\s*(.+)$")
_MM_NOTE_RE = re.compile(r"^\s*[Nn]ote\s+(?:over|left of|right of)\s+[^:]+:\s*(.+)$")
_MM_PARTICIPANT_RE = re.compile(r"^\s*(?:participant|actor)\s+\S+\s+as\s+(.+?)\s*$")
_MM_BLOCK_KEYWORD_RE = re.compile(r"^\s*(?:loop|alt|else|opt|par|and|critical)\s+(.+?)\s*$")


def mermaid_translatables(code: str) -> list[str]:
    """human-visible strings in a mermaid block that are safe to translate.

    Node IDs, package paths, and single-token labels are excluded so the
    diagram's identity and grounding survive translation.
    """
    found: list[str] = []
    lines = code.splitlines()
    head = lines[0].strip() if lines else ""
    is_seq = head.startswith("sequenceDiagram")
    for line in lines[1:]:
        for m in _MM_QUOTED_RE.finditer(line):
            found.append(m.group(1))
        for m in _MM_EDGE_LABEL_RE.finditer(line):
            found.append(m.group(1))
        if is_seq:
            for pat in (_MM_SEQ_MSG_RE, _MM_NOTE_RE, _MM_PARTICIPANT_RE, _MM_BLOCK_KEYWORD_RE):
                m = pat.match(line)
                if m:
                    found.append(m.group(1))

    def translatable(s: str) -> bool:
        s = s.strip()
        # single tokens are node ids / package paths / identifiers — leave them
        return " " in s and not re.fullmatch(r"[\w./\-|>=<+ ]+->.*", s)

    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        s = s.strip()
        if translatable(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def apply_mermaid_translations(code: str, mapping: dict[str, str]) -> str:
    """replace each source string with its translation.

    Two-phase (source -> token -> translation, longest source first) so that a
    source string which is a substring of another never re-fires inside an
    already-translated span.
    """
    ordered = [s for s in sorted(mapping, key=len, reverse=True) if mapping[s] and mapping[s] != s]
    tokens = {src: f"\x00MMT{i}\x00" for i, src in enumerate(ordered)}
    for src in ordered:
        code = code.replace(src, tokens[src])
    for src in ordered:
        # translations must stay single-line: a newline inside a label/message
        # splits the diagram statement and breaks mermaid syntax
        dst = " ".join(mapping[src].replace('"', "'").split())
        code = code.replace(tokens[src], dst)
    return code


# -- prompts ---------------------------------------------------------------


def _lang_name(lang: str) -> str:
    return _LANGUAGES.get(lang, f"the language with code '{lang}'")


_JA_PROSE_SYSTEM = f"""\
あなたはソフトウェアアーキテクチャ文書を専門とするプロの技術翻訳者です。ユーザーが送る Markdown を、日本語ネイティブのエンジニアが書いたような自然な技術文書に翻訳してください。

文体と用語:
- 逐語訳を避け、日本語として自然な文に書き直すこと。地の文はです・ます体。
- 一般的な技術概念には日本語の定訳(カタカナ語・漢語)を使うこと。英単語を地の文に混ぜないこと。例: request→リクエスト、configuration→設定、dependency→依存関係、error handling→エラー処理、cache→キャッシュ、notification→通知、subscription→サブスクリプション、monitoring→監視、logging→ロギング、handler→ハンドラー、endpoint→エンドポイント、health check→ヘルスチェック、entrypoint→エントリポイント、runtime binary→実行バイナリ。
- 英語のまま残してよいのは次のものだけ: 略語({_KEEP_ACRONYMS})、製品名・技術名({_KEEP_PRODUCTS})、コード識別子・関数名・ファイルパス・パッケージパス。

構造の厳守(出力は機械検証されます):
- Markdown 構造を完全に維持すること: 見出しのレベルと数、表の行数と列数、リスト構造、引用、強調の位置。
- <<<BLOCK_n>>> 形式の行はコードブロックのプレースホルダです。一切変更せず、同じ位置に単独の行としてそのまま残すこと。削除・複製・改変は禁止。
- 翻訳しないもの: バッククォート内のインラインコード、ファイルパス、URL、リンク先。[ラベル](target.md) はラベルのみ翻訳し、リンク先は変更しないこと。
- 内容の追加・削除・注釈の付加は禁止。
- 出力は翻訳後の Markdown のみ。"""

_SWEEP_VERSION = "sweep-v1"

# scripts that reliably indicate a line has been translated (CJK targets only)
_TARGET_SCRIPTS = {
    "ja": re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]"),
    "ko": re.compile(r"[가-힣]"),
}
_EN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

_JA_SWEEP_SYSTEM = f"""\
以下の JSON の各値は Markdown 文書の 1 行です(表のヘッダー行や見出しなど、翻訳漏れの行)。
- 各行を自然な日本語に翻訳すること。表の行は | の数と列の順序を維持し、見出しは行頭の # をそのまま残すこと。
- バッククォート内のコード、パス、略語({_KEEP_ACRONYMS})、製品名({_KEEP_PRODUCTS})は英語のまま。
- 改行を入れないこと。同じキーを持つ JSON オブジェクトのみを返すこと。"""


_JA_MERMAID_SYSTEM = f"""\
技術文書の図(mermaid)のラベルを自然な日本語に翻訳します。
- 略語({_KEEP_ACRONYMS})、製品名({_KEEP_PRODUCTS})、パッケージパス・コード識別子は英語のまま残すこと。
- 訳文に二重引用符(")と改行を使わないこと。
- 入力と同じキーを持つ JSON オブジェクトのみを返すこと。"""


def residual_lines(md: str, script_re: re.Pattern) -> dict[int, str]:
    """0-based line index -> line, for prose lines with English phrases but no target script."""
    out: dict[int, str] = {}
    in_code = False
    for i, ln in enumerate(md.splitlines()):
        if ln.startswith("```"):
            in_code = not in_code
            continue
        if in_code or "<<<BLOCK_" in ln or not ln.strip():
            continue
        if script_re.search(ln):
            continue
        if re.fullmatch(r"\s*\|[\s\-:|]+\|\s*", ln):
            continue  # table separator row
        judged = re.sub(r"`[^`]*`|\]\([^)]+\)", "", ln)  # ignore code spans & link targets
        # the line has no target-script characters at all, so two or more plain
        # English words already mark it as likely untranslated
        if len(_EN_WORD_RE.findall(judged)) >= 2:
            out[i] = ln
    return out


def _sweep_prompt(residual: dict[int, str], lang: str) -> list[dict]:
    payload = json.dumps({str(i): ln for i, ln in residual.items()}, ensure_ascii=False)
    if lang == "ja":
        system = _JA_SWEEP_SYSTEM
    else:
        system = (
            "Each value in the JSON below is one markdown line that was left untranslated "
            f"(table headers, short headings). Translate each into {_lang_name(lang)}\n"
            "Keep table rows with the same number of | columns in the same order; keep "
            "leading # characters of headings; keep inline code, paths, acronyms "
            f"({_KEEP_ACRONYMS}) and product names ({_KEEP_PRODUCTS}) in English. "
            "No newlines. Return ONLY a JSON object with the same keys."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": payload},
    ]


def _prose_prompt(text: str, lang: str, feedback: str = "") -> list[dict]:
    if lang == "ja":
        system = _JA_PROSE_SYSTEM
        if feedback:
            system += f"\n前回の出力は検証に失敗しました: {feedback}。規則を厳守してください。"
    else:
        system = (
            "You are a professional technical translator for software architecture "
            "documentation.\n"
            f"Translate the user's markdown into {_lang_name(lang)} Rewrite sentences so "
            "they read naturally to a native reader — no word-for-word translation, and no "
            "English words mixed into prose when the target language has an established term.\n"
            "Hard rules:\n"
            "- Preserve markdown structure EXACTLY: same headings at the same levels, "
            "same tables with the same rows and columns, same list structure, same "
            "blockquotes, same bold/italic placement.\n"
            "- Lines of the form <<<BLOCK_n>>> are placeholders for code blocks: keep each "
            "one exactly as-is, alone on its line, in the same position. Never drop, "
            "duplicate, or alter them.\n"
            "- Never translate: inline code in backticks, file and package paths, URLs, and "
            "link targets — in [label](target.md) translate only the label, never the target.\n"
            f"- Keep in English ONLY: acronyms ({_KEEP_ACRONYMS}), product names "
            f"({_KEEP_PRODUCTS}), and code identifiers.\n"
            "- Do not add or remove content, do not add commentary.\n"
            "Output ONLY the translated markdown."
        )
        if feedback:
            system += f"\nPrevious attempt failed validation: {feedback}. Follow the rules exactly."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def _mermaid_prompt(strings: list[str], lang: str) -> list[dict]:
    payload = json.dumps({str(i): s for i, s in enumerate(strings)}, ensure_ascii=False)
    if lang == "ja":
        system = _JA_MERMAID_SYSTEM
    else:
        system = (
            "You translate short diagram labels for technical documentation into "
            f"{_lang_name(lang)}\n"
            f"Keep in English only: acronyms ({_KEEP_ACRONYMS}), product names "
            f"({_KEEP_PRODUCTS}), package paths and identifiers. Never use double quotes "
            "or newlines in translations. "
            "Return ONLY a JSON object with the same keys and translated values."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": payload},
    ]


# -- translator ------------------------------------------------------------


class WikiTranslator:
    """translates every markdown page of an exported wiki directory."""

    def __init__(
        self,
        llm: LLMClient,
        cache: Cache,
        target_lang: str,
        concurrency: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ):
        self.llm = llm
        self.cache = cache
        self.lang = target_lang
        self._sem = asyncio.Semaphore(concurrency)
        self._on_progress = on_progress
        self.report = TranslateReport()

    def _progress(self, msg: str) -> None:
        if self._on_progress:
            self._on_progress(msg)

    async def translate_dir(self, src: Path, dst: Path) -> TranslateReport:
        src, dst = Path(src), Path(dst)
        files = sorted(p for p in src.glob("*.md"))
        if not files:
            raise ValueError(f"no markdown files found in {src}")
        dst.mkdir(parents=True, exist_ok=True)

        done = 0

        async def _one(path: Path) -> None:
            nonlocal done
            async with self._sem:
                translated = await self.translate_file(path.name, path.read_text(encoding="utf-8"))
            (dst / path.name).write_text(translated, encoding="utf-8")
            done += 1
            self._progress(f"Translated {done}/{len(files)}: {path.name}")

        await asyncio.gather(*(_one(p) for p in files))
        return self.report

    async def translate_file(self, name: str, md: str) -> str:
        cache_key = "translate:" + content_hash(
            TRANSLATE_PROMPT_VERSION + self.lang + name + md
        )
        cached = await self.cache.get(cache_key)
        if cached and isinstance(cached, dict) and "text" in cached:
            if cached.get("fallback"):
                self.report.files_fallback.append(name)
                return cached["text"]
            self.report.files_ok.append(name)
            return await self._residual_sweep(cached["text"])

        segments = split_segments(md)
        original_fp = structure_fingerprint(md)

        # 1. translate mermaid blocks in place (fallback: keep original block)
        for seg in segments:
            if seg.kind == "code" and seg.lang == "mermaid":
                seg.text = await self._translate_mermaid_block(seg.text)

        # 2. protect code blocks, translate the prose skeleton
        protected, blocks = _protect(segments)
        feedback = ""
        translated_doc: str | None = None
        for _attempt in (1, 2):
            candidate = await self._translate_prose(protected, feedback)
            restored, problems = _restore(candidate, blocks)
            if restored is not None:
                problems.extend(fingerprint_diff(original_fp, structure_fingerprint(restored)))
            if restored is not None and not problems:
                translated_doc = restored
                break
            feedback = "; ".join(problems)[:400] or "placeholders were altered"
            logger.warning("translation of %s failed validation: %s", name, feedback)

        fallback = translated_doc is None
        if fallback:
            translated_doc = md  # keep the source language rather than ship broken structure
            self.report.files_fallback.append(name)
        else:
            self.report.files_ok.append(name)

        await self.cache.put(cache_key, {"text": translated_doc, "fallback": fallback})
        if not fallback:
            translated_doc = await self._residual_sweep(translated_doc)
        return translated_doc

    async def _residual_sweep(self, doc: str) -> str:
        """re-translate lines the main pass left untranslated (CJK targets only).

        The main pass occasionally skips table headers and short headings; this
        finds lines with English phrases but no target-script characters and
        translates them line-by-line, with per-line structure guards.
        """
        script_re = _TARGET_SCRIPTS.get(self.lang)
        if script_re is None:
            return doc
        lines = doc.splitlines()
        residual = residual_lines(doc, script_re)
        if not residual:
            return doc

        cache_key = "translate-sweep:" + content_hash(_SWEEP_VERSION + self.lang + doc)
        cached = await self.cache.get(cache_key)
        if cached and isinstance(cached, dict) and "text" in cached:
            return cached["text"]

        raw = await self._complete(_sweep_prompt(residual, self.lang), max_tokens=8_000)
        data = extract_json(raw)
        if not isinstance(data, dict):
            return doc
        for key, value in data.items():
            if not str(key).isdigit():
                continue
            i = int(key)
            original = residual.get(i)
            if original is None or not isinstance(value, str):
                continue
            value = " ".join(value.split())
            # per-line guards: table shape and heading prefix must survive
            if original.count("|") != value.count("|"):
                continue
            orig_prefix = re.match(r"\s*#*", original).group(0)
            if not value.startswith(orig_prefix.strip() or ""):
                value = orig_prefix + value.lstrip("# ")
            lines[i] = value
        swept = "\n".join(lines)
        if fingerprint_diff(structure_fingerprint(doc), structure_fingerprint(swept)):
            return doc  # sweep broke structure — discard it
        await self.cache.put(cache_key, {"text": swept})
        return swept

    async def _translate_prose(self, protected: str, feedback: str) -> str:
        chunks = _chunk_lines(protected, _CHUNK_CHARS)
        out: list[str] = []
        for chunk in chunks:
            raw = await self._complete(_prose_prompt(chunk, self.lang, feedback))
            out.append(_strip_outer_fence(raw))
        return "\n".join(out)

    async def _translate_mermaid_block(self, block_text: str) -> str:
        # block_text includes the ```mermaid fences
        inner = "\n".join(block_text.splitlines()[1:-1])
        strings = mermaid_translatables(inner)
        if not strings:
            return block_text
        raw = await self._complete(_mermaid_prompt(strings, self.lang), max_tokens=4096)
        data = extract_json(raw)
        if not isinstance(data, dict):
            self.report.mermaid_fallback += 1
            return block_text
        mapping = {
            strings[int(k)]: str(v)
            for k, v in data.items()
            if str(k).isdigit() and int(k) < len(strings)
        }
        candidate = apply_mermaid_translations(inner, mapping)
        candidate = mermaid.sanitize(candidate)
        if mermaid.check(candidate):
            self.report.mermaid_fallback += 1
            return block_text
        return f"```mermaid\n{candidate}\n```"

    async def _complete(self, messages: list[dict], max_tokens: int = _MAX_TOKENS) -> str:
        for attempt in (1, 2):
            raw = await self.llm.complete(messages, max_tokens=max_tokens)
            if not raw.startswith("[LLM Error"):
                return raw
            logger.warning("LLM call failed (attempt %d): %s", attempt, raw[:200])
            await asyncio.sleep(3 * attempt)
        raise RuntimeError(f"LLM call failed twice: {raw[:300]}")


def _protect(segments: list[Segment]) -> tuple[str, dict[int, str]]:
    """replace code segments with placeholder lines; return (doc, {index: code})."""
    blocks: dict[int, str] = {}
    lines: list[str] = []
    for i, seg in enumerate(segments):
        if seg.kind == "code":
            blocks[i] = seg.text
            lines.append(_PLACEHOLDER_FMT.format(i=i))
        else:
            lines.append(seg.text)
    return "\n".join(lines), blocks


def _restore(translated: str, blocks: dict[int, str]) -> tuple[str | None, list[str]]:
    """re-insert code blocks; None + problems if placeholders were damaged."""
    problems: list[str] = []
    found = _PLACEHOLDER_RE.findall(translated)
    counts: dict[int, int] = {}
    for f in found:
        counts[int(f)] = counts.get(int(f), 0) + 1
    for i in blocks:
        n = counts.get(i, 0)
        if n != 1:
            problems.append(f"placeholder <<<BLOCK_{i}>>> appeared {n} times (expected 1)")
    for i in counts:
        if i not in blocks:
            problems.append(f"unknown placeholder <<<BLOCK_{i}>>> introduced")
    if problems:
        return None, problems

    def _sub(m: re.Match) -> str:
        return blocks[int(m.group(1))]

    return _PLACEHOLDER_RE.sub(_sub, translated), []


def _chunk_lines(text: str, max_chars: int) -> list[str]:
    """split on line boundaries into chunks of at most ~max_chars."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.splitlines():
        if cur and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _strip_outer_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            inner = text[first_nl + 1 : -3]
            if inner.count("```") % 2 == 0:
                return inner.strip()
    return text


# -- wiki reassembly for HTML export ---------------------------------------


def parse_sidebar(text: str) -> tuple[str, list[SidebarItem]]:
    """parse an export_markdown _sidebar.md back into (project name, sidebar tree)."""
    project = ""
    items: list[SidebarItem] = []
    for line in text.splitlines():
        if line.startswith("# "):
            project = line[2:].strip()
            continue
        m = re.match(r"^(\s*)-\s+\[(.+?)\]\((.+?)\.md\)\s*$", line)
        if m:
            indent, title, page_id = m.group(1), m.group(2), m.group(3)
            item = SidebarItem(title=title, page_id=page_id)
            if indent and items:
                items[-1].children.append(item)
            else:
                items.append(item)
            continue
        m = re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*$", line)
        if m:
            items.append(SidebarItem(title=m.group(1), page_id="", children=[]))
    return project, items


def build_wiki_from_dir(wiki_dir: Path) -> Wiki:
    """reconstruct a Wiki (for export_html) from a markdown wiki directory."""
    wiki_dir = Path(wiki_dir)
    sidebar_path = wiki_dir / "_sidebar.md"
    project, sidebar = ("", [])
    if sidebar_path.exists():
        project, sidebar = parse_sidebar(sidebar_path.read_text(encoding="utf-8"))

    titles: dict[str, str] = {}
    for item in sidebar:
        if item.page_id:
            titles[item.page_id] = item.title
        for child in item.children:
            titles[child.page_id] = child.title

    order: list[str] = [pid for pid in titles]
    pages: list[WikiPage] = []
    seen: set[str] = set()
    for i, pid in enumerate(order):
        path = wiki_dir / f"{pid}.md"
        if not path.exists():
            continue
        pages.append(
            WikiPage(id=pid, title=titles[pid], content=path.read_text(encoding="utf-8"), order=i)
        )
        seen.add(pid)
    # any page not referenced by the sidebar still gets included at the end
    for path in sorted(wiki_dir.glob("*.md")):
        pid = path.stem
        if pid in seen or path.name in ("_sidebar.md", "README.md"):
            continue
        pages.append(
            WikiPage(
                id=pid,
                title=_first_heading(path.read_text(encoding="utf-8")) or pid,
                content=path.read_text(encoding="utf-8"),
                order=len(pages),
            )
        )

    return Wiki(pages=pages, sidebar=sidebar, project_name=project or wiki_dir.name)


def _first_heading(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
