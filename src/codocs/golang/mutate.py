"""mutation operators on go source."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass

from codocs.golang.source import Tok, _Lex, _lex, _parse

_SWAPS = {
    "==": ("cmp", "!="),
    "!=": ("cmp", "=="),
    "<": ("cmp", "<="),
    "<=": ("cmp", "<"),
    ">": ("cmp", ">="),
    ">=": ("cmp", ">"),
    "&&": ("logic", "||"),
    "||": ("logic", "&&"),
    "+=": ("assign", "-="),
    "-=": ("assign", "+="),
    "++": ("incdec", "--"),
    "--": ("incdec", "++"),
}
_ARITH = {"+": "-", "-": "+", "*": "/", "/": "*", "%": "*"}
_BOOLS = {"true": "false", "false": "true"}


@dataclass
class Mutant:
    id: str
    offset: int
    line: int
    operator: str
    original: str
    replacement: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Mutant:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def _operand_end(t: Tok) -> bool:
    return t.kind in ("ident", "num") or (t.kind == "op" and t.text in (")", "]"))


def _binary(src: str, lx: _Lex, k: int) -> bool:
    """an arithmetic token is binary when an operand ends right before it on the same line."""
    toks = lx.toks
    if k == 0 or not _operand_end(toks[k - 1]):
        return False
    t = toks[k]
    if t.text != "*":
        return True
    # gofmt spacing: binary `a * b` / `a*b`; pointer types `p *T`, `) *T`
    before = src[t.start - 1] in " \t"
    after = t.end >= len(src) or src[t.end] in " \t\r\n"
    if before != after:
        return False
    # `a[i] * b` / `a[i][j] * b` are binary; `[]*T`, `[N]*T`, `[][N]*T`, `map[K][N]*T` are types
    j = k - 1
    while toks[j].kind == "op" and toks[j].text == "]":
        o = lx.match[j]
        if o < 1 or o == j - 1:
            return False
        lead = toks[o - 1]
        if toks[o].start != lead.end:
            return False
        if lead.kind == "op" and lead.text == "]":
            j = o - 1
            continue
        return _operand_end(lead)
    return True


def _is_text(t: Tok) -> bool:
    return t.kind == "str" and t.text[:1] in ('"', "`")


def _string_names(lx: _Lex) -> set[str]:
    """names declared as `x string` (also `a, b string`) or assigned a string literal."""
    toks = lx.toks
    names: set[str] = set()
    for i, t in enumerate(toks):
        if t.kind == "ident" and t.text == "string" and i and toks[i - 1].kind == "ident":
            j = i - 1
            while toks[j].kind == "ident":
                names.add(toks[j].text)
                if j >= 2 and toks[j - 1].text == "," and toks[j - 2].kind == "ident":
                    j -= 2
                else:
                    break
        elif (t.kind == "op" and t.text in (":=", "=") and i and toks[i - 1].kind == "ident"
              and i + 1 < len(toks) and _is_text(toks[i + 1])):
            names.add(toks[i - 1].text)
    return names


def _on_strings(lx: _Lex, k: int, strings: set[str]) -> bool:
    """`+` / `+=` that (very likely) concatenates strings: a mutant would never compile."""
    toks = lx.toks
    left = toks[k - 1] if k else None
    right = toks[k + 1] if k + 1 < len(toks) else None
    if (left is not None and _is_text(left)) or (right is not None and _is_text(right)):
        return True
    if left is not None and left.kind == "ident" and left.text in strings:
        if not (k >= 2 and toks[k - 2].text == "."):
            return True
    if right is not None and right.kind == "ident" and right.text in strings:
        after = toks[k + 2] if k + 2 < len(toks) else None
        if after is None or after.text not in (".", "(", "["):
            return True
    return False


def _excluded_spans(src: str) -> list[tuple[int, int]]:
    parsed = _parse(src)
    spans = {(d.start, d.end) for d in parsed.imports}
    if parsed.package:
        spans.add(parsed.package[:2])
    spans.update((d.start, d.end) for d in parsed.decls if d.kind in ("var", "const"))
    return sorted(spans)


def mutation_sites(src: str) -> list[Mutant]:
    """every mutable token outside the package clause, imports and top-level var/const decls."""
    lx = _lex(src)
    toks = lx.toks
    spans = _excluded_spans(src)
    strings = _string_names(lx)
    out: list[Mutant] = []
    for k, t in enumerate(toks):
        if t.kind == "op":
            if t.text in _SWAPS:
                cat, repl = _SWAPS[t.text]
            elif t.text in _ARITH and _binary(src, lx, k):
                cat, repl = "arith", _ARITH[t.text]
            else:
                continue
            if t.text in ("+", "+=") and _on_strings(lx, k, strings):
                continue
        elif t.kind == "ident" and t.text in _BOOLS:
            if k and toks[k - 1].kind == "op" and toks[k - 1].text == ".":
                continue
            cat, repl = "bool", _BOOLS[t.text]
        else:
            continue
        if any(a <= t.start < b for a, b in spans):
            continue
        out.append(
            Mutant(
                id=f"m{t.start}",
                offset=t.start,
                line=lx.line(t.start),
                operator=f"{cat}: {t.text} -> {repl}",
                original=t.text,
                replacement=repl,
            )
        )
    return out


def apply(src: str, m: Mutant) -> str:
    end = m.offset + len(m.original)
    if src[m.offset : end] != m.original:
        raise ValueError(f"mutant {m.id} does not match the source at offset {m.offset}")
    return src[: m.offset] + m.replacement + src[end:]


def sample(mutants: list[Mutant], n: int, seed: int) -> list[Mutant]:
    """deterministic subset of n mutants, in source order."""
    if n <= 0:
        return []
    if n >= len(mutants):
        return list(mutants)
    picked = random.Random(seed).sample(list(mutants), n)
    return sorted(picked, key=lambda m: m.offset)
