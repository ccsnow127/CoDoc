"""shared tree-sitter helpers: decoding, parsing, byte->char offsets, masking, walking, code.

tree-sitter and its grammar pack are imported lazily (MISSING_DEPS when absent); Go
never uses this module. Every offset this module returns is a str (character) offset into the
decoded text, never a byte offset.
"""

from __future__ import annotations

import keyword
import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

MISSING_DEPS = ("multi-language support needs tree-sitter and tree-sitter-language-pack: "
                "pip install tree-sitter tree-sitter-language-pack")
GRAMMARS = {"python": "python", "java": "java", "c": "c", "cpp": "cpp", "go": "go"}
FALLBACK_CODECS = ("euc_jp", "shift_jis")

# node types whose text is masked (comments and literals), per grammar
COMMENT_TYPES = {
    "python": frozenset({"comment"}),
    "java": frozenset({"line_comment", "block_comment"}),
    "c": frozenset({"comment"}),
    "cpp": frozenset({"comment"}),
}
STRING_TYPES = {
    "python": frozenset({"string"}),
    "java": frozenset({"string_literal", "character_literal"}),
    "c": frozenset({"string_literal", "char_literal", "system_lib_string"}),
    "cpp": frozenset({"string_literal", "char_literal", "system_lib_string",
                      "raw_string_literal"}),
}

_C_KEYWORDS = (
    "auto break case char const continue default do double else enum extern float for goto if "
    "inline int long register restrict return short signed sizeof static struct switch typedef "
    "union unsigned void volatile while _Bool _Complex _Imaginary _Alignas _Alignof _Atomic "
    "_Generic _Noreturn _Static_assert _Thread_local bool true false NULL"
).split()
_CPP_EXTRA = (
    "alignas alignof and and_eq asm catch char8_t char16_t char32_t class compl concept consteval "
    "constexpr constinit const_cast co_await co_return co_yield decltype delete dynamic_cast "
    "explicit export friend mutable namespace new noexcept not not_eq nullptr operator or or_eq "
    "private protected public reinterpret_cast requires static_assert static_cast template this "
    "thread_local throw try typeid typename using virtual wchar_t xor xor_eq override final"
).split()
_JAVA_KEYWORDS = (
    "abstract assert boolean break byte case catch char class const continue default do double "
    "else enum extends final finally float for goto if implements import instanceof int "
    "interface long native new package private protected public return short static strictfp "
    "super switch synchronized this throw throws transient try void volatile while true false "
    "null var record yield sealed permits non-sealed"
).split()
KEYWORDS = {
    "python": frozenset(keyword.kwlist) | frozenset(getattr(keyword, "softkwlist", [])),
    "java": frozenset(_JAVA_KEYWORDS),
    "c": frozenset(_C_KEYWORDS),
    "cpp": frozenset(_C_KEYWORDS) | frozenset(_CPP_EXTRA),
}
FENCE_TAGS = {
    "go": ("go", "golang"),
    "python": ("python", "py", "python3"),
    "java": ("java",),
    "c": ("c", "h"),
    "cpp": ("cpp", "c++", "cc", "cxx", "hpp", "h++"),
    "cobol": ("cobol", "cbl", "cob", "json"),
}

_IDENT_RE = re.compile(r"(?<!\w)[^\W\d]\w*")
_FENCE_RE = re.compile(r"^```[ \t]*([\w+#.-]*)[^\n]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


class MissingDependency(RuntimeError):
    """tree-sitter (or its grammar pack) is not installed."""


# -- decoding -------------------------------------------------------------------


def decode(data: bytes) -> tuple[str, str]:
    """(text, codec): strict utf-8, else euc_jp, else shift_jis, else utf-8 (surrogateescape).

    a fallback codec is used only when strict utf-8 fails and the fallback decodes the whole
    file cleanly; otherwise the utf-8 text keeps the undecodable bytes as surrogates.
    """
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for codec in FALLBACK_CODECS:
        try:
            return data.decode(codec), codec
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="surrogateescape"), "utf-8"


def encode_as(text: str, codec: str = "utf-8") -> tuple[bytes, str]:
    """(bytes, codec used): codec, or utf-8 when text has characters codec cannot hold."""
    try:
        return text.encode(codec, errors="surrogateescape"), codec
    except UnicodeEncodeError:
        # e.g. llm text with characters the original codec cannot hold
        return text.encode("utf-8", errors="surrogateescape"), "utf-8"


def encode(text: str, codec: str = "utf-8") -> bytes:
    """bytes of text in codec (utf-8 fallback); surrogate-escaped bytes round-trip exactly."""
    return encode_as(text, codec)[0]


# -- offsets --------------------------------------------------------------------


class OffsetMap:
    """byte <-> char offsets for text encoded with codec (identity for ascii text)."""

    def __init__(self, text: str, codec: str = "utf-8"):
        self._chars: list[int] = []      # char index of each non-ascii char
        self._bytes: list[int] = []      # its byte start
        self._widths: list[int] = []     # its encoded width
        extra = 0
        for m in _NON_ASCII_RE.finditer(text):
            c = m.start()
            w = len(encode(m.group(), codec)) or 1
            self._chars.append(c)
            self._bytes.append(c + extra)
            self._widths.append(w)
            extra += w - 1
        self.identity = not self._chars

    def char(self, b: int) -> int:
        """char offset of byte offset b (a byte inside a multi-byte char maps to its start)."""
        if self.identity:
            return b
        k = bisect_right(self._bytes, b) - 1
        if k < 0:
            return b
        s, w, c = self._bytes[k], self._widths[k], self._chars[k]
        if b < s + w:
            return c
        return c + 1 + (b - s - w)

    def byte(self, c: int) -> int:
        """byte offset of char offset c."""
        if self.identity:
            return c
        k = bisect_right(self._chars, c) - 1
        if k < 0:
            return c
        s, w, ck = self._bytes[k], self._widths[k], self._chars[k]
        if c == ck:
            return s
        return s + w + (c - ck - 1)


# -- parsing --------------------------------------------------------------------


_local = threading.local()
_lock = threading.Lock()
_CACHE: OrderedDict[tuple[str, str, str], Parsed] = OrderedDict()
_CACHE_SIZE = 32


def available() -> bool:
    """whether tree-sitter and the grammar pack can be imported."""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


def with_deps(languages: list[str], what: str) -> list[str]:
    """languages usable without tree-sitter's absence getting in the way: all of them when it
    is installed; else MissingDependency when no Go is selected, or a WARNING and ["go"]."""
    from codocs.lang.base import NO_TS_LANGS

    others = [x for x in languages if x not in NO_TS_LANGS]
    if not others or available():
        return list(languages)
    kept = [x for x in languages if x in NO_TS_LANGS]
    if not kept:
        raise MissingDependency(MISSING_DEPS)
    import logging

    logging.getLogger(__name__).warning("WARNING: %s: %s; left out: %s", what, MISSING_DEPS,
                                        ", ".join(others))
    return kept


def require() -> None:
    if not available():
        raise MissingDependency(MISSING_DEPS)


def parser(lang: str) -> Any:
    """a tree-sitter parser for lang, cached per thread."""
    parsers = getattr(_local, "parsers", None)
    if parsers is None:
        parsers = _local.parsers = {}
    p = parsers.get(lang)
    if p is None:
        try:
            from tree_sitter_language_pack import get_parser
        except ImportError as e:
            raise MissingDependency(MISSING_DEPS) from e
        if lang not in GRAMMARS:
            raise ValueError(f"no tree-sitter grammar for {lang!r}")
        p = parsers[lang] = get_parser(GRAMMARS[lang])
    return p


@dataclass
class Parsed:
    """a parse tree with its text; offsets of nodes are given as char offsets."""

    text: str
    lang: str
    codec: str
    data: bytes
    tree: Any
    offsets: OffsetMap = field(repr=False)
    _lines: list[int] | None = field(default=None, repr=False)

    @property
    def root(self) -> Any:
        return self.tree.root_node

    @property
    def has_error(self) -> bool:
        return bool(self.tree.root_node.has_error)

    def start(self, node) -> int:
        return self.offsets.char(node.start_byte)

    def end(self, node) -> int:
        return self.offsets.char(node.end_byte)

    def span(self, node) -> tuple[int, int]:
        return self.start(node), self.end(node)

    def node_text(self, node) -> str:
        a, b = self.span(node)
        return self.text[a:b]

    def line(self, node) -> int:
        """1-based line of the node's start."""
        return node.start_point[0] + 1

    def end_line(self, node) -> int:
        """1-based line of the node's last character."""
        row, col = node.end_point
        return row + (1 if col > 0 or row == node.start_point[0] else 0)

    def line_at(self, offset: int) -> int:
        """1-based line of a char offset."""
        if self._lines is None:
            self._lines = [m.end() for m in re.finditer("\n", self.text)]
        return bisect_right(self._lines, offset) + 1


def parse(text: str, lang: str, codec: str = "utf-8") -> Parsed:
    """parse text with lang's grammar (small LRU cache keyed on the text)."""
    key = (lang, codec, text)
    with _lock:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    data, used = encode_as(text, codec)
    tree = parser(lang).parse(data)
    out = Parsed(text=text, lang=lang, codec=used, data=data, tree=tree,
                 offsets=OffsetMap(text, used))
    with _lock:
        _CACHE[key] = out
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return out


# -- walking --------------------------------------------------------------------


def walk(node) -> Iterator[Any]:
    """pre-order iteration over node and all its descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        children = n.children
        if children:
            stack.extend(reversed(children))


def iter_types(node, types: Iterable[str], *, prune: Iterable[str] = ()) -> Iterator[Any]:
    """descendants (and node) of the given types; subtrees of `prune` types are not entered."""
    want = frozenset(types)
    skip = frozenset(prune)
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in want:
            yield n
        if n.type in skip and n is not node:
            continue
        children = n.children
        if children:
            stack.extend(reversed(children))


def children_of(node, *types: str) -> list[Any]:
    """direct children of node with one of the types (all named children without types)."""
    if not types:
        return list(node.named_children)
    return [c for c in node.children if c.type in types]


def ancestors(node) -> Iterator[Any]:
    p = node.parent
    while p is not None:
        yield p
        p = p.parent


def error_spans(parsed: Parsed) -> list[tuple[int, int]]:
    """char spans of ERROR and MISSING nodes, in source order."""
    out: list[tuple[int, int]] = []
    if not parsed.has_error:
        return out
    stack = [parsed.root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            out.append(parsed.span(n))
            continue
        if n.has_error:
            stack.extend(reversed(n.children))
    return sorted(out)


def in_spans(offset: int, spans: list[tuple[int, int]]) -> bool:
    """whether a char offset falls in one of the sorted (start, end) spans."""
    k = bisect_right(spans, (offset, float("inf"))) - 1
    return k >= 0 and spans[k][0] <= offset < spans[k][1]


# -- masking --------------------------------------------------------------------


def mask_types(lang: str) -> frozenset[str]:
    return COMMENT_TYPES.get(lang, frozenset()) | STRING_TYPES.get(lang, frozenset())


def masked_spans(parsed: Parsed, types: Iterable[str] | None = None) -> list[tuple[int, int]]:
    """sorted char spans of the nodes of `types` (default: the language's comments and strings)."""
    kinds = frozenset(types) if types is not None else mask_types(parsed.lang)
    return sorted(parsed.span(n) for n in iter_types(parsed.root, kinds, prune=kinds))


def blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """text with every char inside spans replaced by a space (newlines kept)."""
    chars = list(text)
    for a, b in spans:
        for i in range(a, min(b, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def mask(parsed: Parsed, types: Iterable[str] | None = None) -> str:
    """the text with comments and string literals blanked (same length, newlines kept)."""
    return blank(parsed.text, masked_spans(parsed, types))


_C_TOKEN_RE = re.compile(
    r'//[^\n]*|/\*.*?(?:\*/|\Z)|"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?',
    re.DOTALL,
)


def mask_c_like(text: str) -> str:
    """regex mask of // and /* */ comments and "..." / '...' literals (C, C++, Java); no parser."""
    return blank(text, ((m.start(), m.end()) for m in _C_TOKEN_RE.finditer(text)))


def identifiers(text: str, lang: str, parsed: Parsed | None = None) -> set[str]:
    """identifier tokens outside comments and strings, the language's keywords excluded."""
    p = parsed or parse(text, lang)
    kw = KEYWORDS.get(lang, frozenset())
    return {w for w in _IDENT_RE.findall(mask(p)) if w not in kw}


# -- code extraction ------------------------------------------------------------


def extract_code(reply: str, tags: Iterable[str]) -> str:
    """largest fenced block tagged with one of tags (or untagged) of a reply, else the reply."""
    ok = {""} | {t.lower() for t in tags}
    blocks = [m.group(2) for m in _FENCE_RE.finditer(reply) if m.group(1).lower() in ok]
    if blocks:
        code = max(blocks, key=len)
    else:
        code = reply.strip()
        nl = code.find("\n")
        tag = code[3 : nl if nl >= 0 else len(code)].strip().lower()
        if code.startswith("```") and tag in ok:
            code = "" if nl < 0 else code[nl + 1 :]
            if code.rstrip().endswith("```"):
                code = code.rstrip()[:-3]
    return code.strip() + "\n"


def line_count(text: str) -> int:
    """number of lines, counted as golang.package counts them."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)
