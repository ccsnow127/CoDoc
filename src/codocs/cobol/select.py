"""--programs selection: program-name lists and their resolution through the COBOL index."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from codocs.cobol.index import CobolRepoIndex

logger = logging.getLogger(__name__)


def parse_program_list(values: str | Iterable[str] | None) -> list[str]:
    """'A,B C' or ['A,B', 'C'] -> ['A', 'B', 'C'] (order kept, duplicates dropped)."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        for name in re.split(r"[,\s]+", v):
            if name and name not in out:
                out.append(name)
    return out


def select_programs(index: CobolRepoIndex, names: Iterable[str] | None) -> list[str] | None:
    """map requested names to program ids (exact, then case-insensitive); None means all.

    unknown names are logged as a warning; ValueError when none of them exists.
    """
    names = list(names or [])
    if not names:
        return None
    lower = {k.lower(): k for k in index.programs}
    found: list[str] = []
    unknown: list[str] = []
    for name in names:
        pid = name if name in index.programs else lower.get(name.lower())
        if pid is None:
            unknown.append(name)
        elif pid not in found:
            found.append(pid)
    if unknown:
        logger.warning("unknown programs ignored: %s", ", ".join(unknown))
    if not found:
        raise ValueError(f"none of the requested programs exist in the repo: {', '.join(names)}")
    return found


def program_files(index: CobolRepoIndex, pids: Iterable[str]) -> list[str]:
    """the program files (rels) that define *pids*, sorted."""
    return sorted({index.programs[p].path for p in pids if p in index.programs})
