"""measure how many non-CICS/SQL/DLI programs compile under each cobc flag set (no network).

usage: python scripts/cobol/measure_cobc_flags.py REPO [REPO ...] [--json OUT.json]

Each program file is compiled with `cobc -fsyntax-only`, the repo's copybook dirs
as -I and the program's detected source format; prints a success table per flag set.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codocs.cobol.harness import DIALECTS, choose_dialect
from codocs.cobol.index import CobolRepoIndex

VARIANTS: dict[str, list[str]] = {
    "default": [],
    "std=ibm": ["-std=ibm"],
    "std=mvs": ["-std=mvs"],
    "relax": ["-frelax-syntax-checks"],
    "ibm+relax": ["-std=ibm", "-frelax-syntax-checks"],
    "default+fmt": ["@fmt"],
    "relax+fmt": ["-frelax-syntax-checks", "@fmt"],
    "ibm+relax+fmt": ["-std=ibm", "-frelax-syntax-checks", "@fmt"],
    "auto": ["@auto"],
    # the harness policy: per-program fallback through DIALECT_ORDER, preferring a
    # dialect without [-Wdialect] warnings (harness.choose_dialect)
    "fallback": ["@fallback"],
}

# "@auto": IBM dialect for fixed-format (mainframe) sources, GnuCOBOL default for free
_AUTO = {
    "fixed": ["-std=ibm", "-frelax-syntax-checks", "-fformat=fixed"],
    "free": ["-frelax-syntax-checks", "-fformat=free"],
}


def _compile(path: str, incs: list[str], flags: list[str], fmt: str) -> tuple[bool, str]:
    if flags == ["@fallback"]:
        here = Path(path).parent
        inc_args = []
        for d in [Path(i) for i in incs] + [here, *here.parents][:4]:
            inc_args += ["-I", str(d)]
        name, _note = choose_dialect("cobc", Path(path), fmt, inc_args)
        ok, first = _compile(path, incs, DIALECTS[name] + ["@fmt"], fmt)
        return ok, (first if not ok else name)
    args = ["cobc", "-fsyntax-only"]
    for f in flags:
        if f == "@auto":
            args += _AUTO[fmt]
        else:
            args.append(f"-fformat={fmt}" if f == "@fmt" else f)
    # copybook dirs first; then the program's own dir and its ancestors, for
    # COPY "sub/dir/member.cpy" (last, so COPY X never finds program file X.cbl)
    here = Path(path).parent
    for d in [Path(i) for i in incs] + [here, *here.parents][:4]:
        args += ["-I", str(d)]
    p = subprocess.run(args + [path], capture_output=True, text=True, timeout=120)
    first = next((ln for ln in p.stderr.splitlines() if "error" in ln), "")
    return p.returncode == 0, first


def measure(repo: str) -> dict:
    idx = CobolRepoIndex.build(repo)
    incs = [str(d) for d in idx.copybook_dirs()]
    files: dict[str, str] = {}
    for p in idx.programs.values():
        if not (p.has_cics or p.has_sql or p.has_dli):
            files.setdefault(p.path, p.format)
    out: dict = {"repo": idx.name, "files": len(files), "variants": {}}
    for name, flags in VARIANTS.items():
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(
                lambda kv: _compile(str(idx.root / kv[0]), incs, flags, kv[1]), files.items()
            ))
        errs = [e for ok, e in res if not ok]
        out["variants"][name] = {"ok": len(res) - len(errs), "errors": errs[:5]}
        if name == "fallback":
            picked: dict[str, int] = {}
            for ok, e in res:
                if ok:
                    picked[e] = picked.get(e, 0) + 1
            out["variants"][name]["dialects"] = picked
    return out


def main() -> None:
    args = sys.argv[1:]
    out_json = None
    if "--json" in args:
        i = args.index("--json")
        out_json = args[i + 1]
        args = args[:i] + args[i + 2:]
    rows = [measure(r) for r in args]
    names = list(VARIANTS)
    print("| repo | files | " + " | ".join(names) + " |")
    print("|---|---|" + "---|" * len(names))
    for r in rows:
        cells = [str(r["variants"][n]["ok"]) for n in names]
        print(f"| {r['repo']} | {r['files']} | " + " | ".join(cells) + " |")
    for r in rows:
        print(f"<!-- {r['repo']}: fallback picked {r['variants']['fallback'].get('dialects')} -->")
    tot = {n: sum(r["variants"][n]["ok"] for r in rows) for n in names}
    print(f"| total | {sum(r['files'] for r in rows)} | "
          + " | ".join(str(tot[n]) for n in names) + " |")
    if out_json:
        with open(out_json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
