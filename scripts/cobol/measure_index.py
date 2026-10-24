"""print static-analysis counts for one or more COBOL repos (no network).

usage: python scripts/cobol/measure_index.py REPO [REPO ...] [--json OUT.json]
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

from codocs.cobol.index import CobolRepoIndex


def measure(path: str) -> dict:
    t0 = time.time()
    idx = CobolRepoIndex.build(path)
    secs = time.time() - t0
    s = idx.summary()
    calls = Counter(
        d["kind"] for _, _, d in idx.program_graph().edges(data=True)
    )
    warn = sum(len(p.parse_warnings) for p in idx.programs.values())
    sites = Counter(
        (c.kind, "resolved" if c.resolved else "unresolved")
        for p in idx.programs.values() for c in p.calls
    )
    return {
        "repo": idx.name,
        "secs": round(secs, 2),
        "programs": s.programs,
        "copybooks": s.copybooks,
        "jobs": s.jcl_jobs,
        "procs": sum(1 for j in idx.jobs.values() if j.kind == "proc"),
        "mapsets": s.bms_mapsets,
        "loc": s.cobol_loc,
        "call_edges": dict(calls),
        "call_sites": {f"{k}/{r}": n for (k, r), n in sorted(sites.items())},
        "edge_kinds": idx.edge_counts(),
        "datastores": dict(Counter(d.kind for d in idx.datastores.values())),
        "transactions": len(idx.transactions),
        "unresolved": s.unresolved_calls,
        "unresolved_dynamic": s.unresolved_dynamic,
        "missing_copybooks": s.missing_copybooks,
        "program_warnings": warn,
        "repo_warnings": s.warnings,
        "entrypoints": {k: len(v) for k, v in idx.entrypoints().items()},
        "groups": dict(Counter(idx.group_of(p) for p in idx.programs)),
    }


def main(argv: list[str]) -> None:
    out = None
    if "--json" in argv:
        i = argv.index("--json")
        out = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    results = [measure(p) for p in argv]
    for r in results:
        print(json.dumps(r, indent=1))
    if out:
        with open(out, "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
