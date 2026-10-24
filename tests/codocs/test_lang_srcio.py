"""srcio: per-file codec round trips."""

from __future__ import annotations

from pathlib import Path

from codocs.lang.srcio import SourceText, read_source, write_source

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_shift_jis_round_trip(tmp_path: Path):
    src = FIXTURES / "cmod" / "sjis" / "sjis.c"
    st = read_source(src)
    assert st.codec == "shift_jis" and "こんにちは" in st.text
    out = tmp_path / "a" / "sjis.c"
    assert write_source(out, st.text, st.codec) == "shift_jis"
    assert out.read_bytes() == src.read_bytes()
    # text the codec cannot hold falls back to utf-8
    assert write_source(out, st.text + "/* é中\U0001f600 */\n", st.codec) == "utf-8"
    assert read_source(out).codec == "utf-8"


def test_undecodable_bytes_survive(tmp_path: Path):
    p = tmp_path / "x.c"
    raw = b"int a; /* \xff\xfe */\n"
    p.write_bytes(raw)
    st = read_source(p)
    assert st == SourceText(st.text, "utf-8")
    write_source(p, st.text, st.codec)
    assert p.read_bytes() == raw
