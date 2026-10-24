"""source text with its per-file codec, a thin layer over treesitter.

read_source decodes strict utf-8, then euc_jp / shift_jis, then utf-8 with surrogateescape;
write_source encodes with the file's codec (utf-8 when the text cannot be held in it), so every
byte that was not changed round-trips exactly. Go files keep their own reading code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from codocs.lang.treesitter import decode, encode_as


@dataclass(frozen=True)
class SourceText:
    text: str
    codec: str = "utf-8"


def read_source(path: str | os.PathLike) -> SourceText:
    text, codec = decode(Path(path).read_bytes())
    return SourceText(text, codec)


def write_source(path: str | os.PathLike, text: str, codec: str = "utf-8") -> str:
    """write text in codec (utf-8 fallback); -> the codec used. parent dirs are created."""
    data, used = encode_as(text, codec)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return used
