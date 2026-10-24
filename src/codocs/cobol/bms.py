"""BMS map parsing: mapset (DFHMSD), maps (DFHMDI) and field count (DFHMDF)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from codocs.cobol.models import BmsMapset

_MACRO_RE = re.compile(r"^([A-Za-z@#$][\w@#$]*)?\s+(DFHMSD|DFHMDI|DFHMDF)\b", re.IGNORECASE)


def is_bms_text(text: str) -> bool:
    return bool(re.search(r"\bDFHMSD\b", text[:200_000], re.IGNORECASE))


def _statements(text: str) -> list[str]:
    """assembler statements: comments dropped, col-72 continuations joined."""
    out: list[str] = []
    cont = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not cont and (line.startswith("*") or line.startswith(".*")):
            continue
        body = line[:71].rstrip()
        continued = len(line) > 71 and line[71] not in (" ", "")
        if cont and out:
            out[-1] += " " + body.strip()
        elif body.strip():
            out.append(body)
        cont = continued
    return out


def parse_bms(rel_path: str, text: str) -> BmsMapset:
    """mapset name (DFHMSD label, else file stem), map names (DFHMDI labels), DFHMDF count."""
    ms = BmsMapset(name=PurePosixPath(rel_path).stem.upper(), path=rel_path)
    named = False
    for st in _statements(text):
        m = _MACRO_RE.match(st)
        if not m:
            continue
        label, macro = (m.group(1) or "").upper(), m.group(2).upper()
        if macro == "DFHMSD":
            if label and not named and "TYPE=FINAL" not in st.upper().replace(" ", ""):
                ms.name = label
                named = True
        elif macro == "DFHMDI":
            if label and label not in ms.maps:
                ms.maps.append(label)
        else:
            ms.fields += 1
    return ms
