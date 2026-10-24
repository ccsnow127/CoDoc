#!/usr/bin/env bash
# Run the Codocs pipeline on one COBOL dataset (analyze -> baseline -> testgen -> docgen ->
# judge -> wiki -> report) and log everything. A full run on a large repo takes a while —
# run it in tmux:
#
#   tmux new -s codocs
#   bash scripts/cobol/run_dataset.sh carddemo           # $CODOCS_DATASETS/carddemo -> out/carddemo
#   bash scripts/cobol/run_dataset.sh /path/to/cobol-repo      # any directory -> out/<basename>
#   PROGRAMS=CBACT01C,COSGN00C bash scripts/cobol/run_dataset.sh carddemo   # small trial run
#   STEPS="analyze plan" bash scripts/cobol/run_dataset.sh genapp           # static only, no LLM
#   STEPS="analyze plan bizplan" bash scripts/cobol/run_dataset.sh cbi      # + bizlogic plan
#   STEPS="run translate check" LANG_TO=ja bash scripts/cobol/run_dataset.sh genapp
#
# A dataset name needs $CODOCS_DATASETS, the directory the datasets were fetched into with
# scripts/cobol/fetch_datasets.sh. A name listed in datasets/manifest.json uses its "root"
# as --subtree and its "exclude" list as --exclude-path. Every LLM reply is cached in
# ~/.codocs/cache.db, so re-running after an interruption only redoes unfinished files.
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ $# -lt 1 ]]; then
  sed -n '2,17p' "$0"; exit 2
fi
TARGET="$1"

# ---- configurable via env ---------------------------------------------------
if [[ -z "${CODOCS:-}" ]]; then
  if [[ -x .venv/bin/codocs ]]; then CODOCS=.venv/bin/codocs; else CODOCS=codocs; fi
fi
DATASETS="${CODOCS_DATASETS:-}"
MODEL="${MODEL:-gpt}"                     # model alias or full model id (see codocs/config.py)
CODEGEN_MODEL="${CODEGEN_MODEL:-}"        # COBOL regeneration from the doc ("" = MODEL)
LANG_OUT="${LANG_OUT:-en}"                # language of the generated docs and wiki
LANG_TO="${LANG_TO:-ja}"                  # target of the optional translate step
PROGRAMS="${PROGRAMS:-}"                  # comma-separated subset of programs
SKIP="${SKIP:-}"                          # e.g. "judge" or "testgen,judge"
JOBS="${JOBS:-5}"                         # parallel LLM calls
WORKERS="${WORKERS:-4}"                   # parallel GnuCOBOL runs
STEPS="${STEPS:-analyze plan run check}"
OUT_ROOT="${OUT_ROOT:-out}"
MERMAID_VERSION="${MERMAID_VERSION:-11.16.0}"

# ---- resolve the dataset ------------------------------------------------------
if [[ -d "$TARGET" ]]; then
  REPO="$TARGET"
  NAME="$(basename "$TARGET")"
else
  NAME="$TARGET"
  if [[ -z "$DATASETS" ]]; then
    echo "[run] ERROR: $TARGET is not a directory and CODOCS_DATASETS is not set: set it to" \
      "the datasets directory (fetch with CODOCS_DATASETS=... bash" \
      "scripts/cobol/fetch_datasets.sh $NAME)" >&2
    exit 1
  fi
  REPO="$DATASETS/$NAME"
fi
[[ -d "$REPO" ]] || {
  echo "[run] ERROR: $REPO not found (CODOCS_DATASETS=... scripts/cobol/fetch_datasets.sh $NAME?)" >&2
  exit 1
}
NAME="${OUT_NAME:-$NAME}"
OUT="${OUT:-$OUT_ROOT/$NAME}"

# subtree + excludes from the manifest (paths there are relative to the dataset root)
scan_args=(--source-lang cobol)
if [[ -f datasets/manifest.json ]]; then
  while IFS= read -r arg; do
    [[ -n "$arg" ]] && scan_args+=("$arg")
  done < <(python3 - "$NAME" <<'PY'
import json, sys
m = json.load(open("datasets/manifest.json"))
for d in m.get("datasets", []) + m.get("optional", []):
    if d["name"] != sys.argv[1]:
        continue
    root = d.get("root", "").strip("/")
    if root:
        print("--subtree"); print(root)
    for ex in d.get("exclude", []):
        print("--exclude-path"); print(f"{root}/{ex}" if root else ex)
PY
)
fi

mkdir -p "$OUT_ROOT/logs" "$OUT"
LOG="$OUT_ROOT/logs/${NAME}_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "[run] log: $LOG"
echo "[run] repo: $REPO -> $OUT | steps: $STEPS | model: $MODEL | programs: ${PROGRAMS:-<all>}"
echo "[run] scan options: ${scan_args[*]}"

has_step() { [[ " $STEPS " == *" $1 "* ]]; }

need_llm() {
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "[run] ERROR: OPENAI_API_KEY is not set (export OPENAI_BASE_URL too for a non-default endpoint)." >&2
    exit 1
  fi
}

# offline mermaid bundle: inlined into the exported HTML so it needs no CDN
ensure_mermaid() {
  local f="$HOME/.repowiki/mermaid.min.js"
  if [[ ! -s "$f" ]]; then
    echo "[run] fetching mermaid@$MERMAID_VERSION -> $f"
    mkdir -p "$HOME/.repowiki"
    curl -fsSL "https://cdn.jsdelivr.net/npm/mermaid@${MERMAID_VERSION}/dist/mermaid.min.js" -o "$f" \
      || echo "[run] WARN: could not fetch mermaid; the HTML will load it from the CDN"
  fi
}

# run "$@", print its duration, and return its exit status (a failure still stops the script
# under set -e unless the caller handles it, as the run step does)
timed() {
  local t0=$SECONDS rc=0
  "$@" || rc=$?
  echo "[run] ($(( SECONDS - t0 ))s) $*"
  return "$rc"
}

if has_step analyze; then
  echo "[run] === analyze (static, no LLM) ==="
  timed "$CODOCS" analyze "$REPO" -o "$OUT" "${scan_args[@]}"
fi

if has_step plan; then
  echo "[run] === wiki plan (static, no LLM) ==="
  timed "$CODOCS" wiki "$REPO" -o "$OUT" --plan-only "${scan_args[@]}"
fi

if has_step bizplan; then
  echo "[run] === business-logic plan (static, no LLM) ==="
  timed "$CODOCS" bizlogic "$REPO" -o "$OUT" --plan-only -k "${BIZ_K:-10}" "${scan_args[@]}"
fi

if has_step run; then
  echo "[run] === codocs run -> $OUT ==="
  need_llm
  ensure_mermaid
  run_args=(-m "$MODEL" -l "$LANG_OUT" -j "$JOBS" --workers "$WORKERS")
  [[ -n "$PROGRAMS" ]] && run_args+=(--programs "$PROGRAMS")
  [[ -n "$SKIP" ]] && run_args+=(--skip "$SKIP")
  [[ -n "$CODEGEN_MODEL" ]] && run_args+=(--codegen-model "$CODEGEN_MODEL")
  rc=0
  timed "$CODOCS" run "$REPO" -o "$OUT" "${run_args[@]}" "${scan_args[@]}" || rc=$?
  if (( rc )); then
    echo "[run] WARN: codocs run exited with $rc (a stage failed; see $OUT/report.md)"
  fi
fi

if has_step translate; then
  echo "[run] === translate wiki -> $OUT/wiki_$LANG_TO ==="
  need_llm
  ensure_mermaid
  timed "$CODOCS" translate "$OUT/wiki" -o "$OUT/wiki_$LANG_TO" -l "$LANG_TO" -m "$MODEL" \
    -j "$JOBS" --glossary cobol
fi

if has_step check; then
  echo "[run] === acceptance checks ==="
  for d in "$OUT/wiki" "$OUT/wiki_$LANG_TO"; do
    [[ -d "$d" ]] || continue
    md=$(find "$d" -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')
    err=$(grep -l '\[LLM Error' "$d"/*.md 2>/dev/null | wc -l | tr -d ' ' || true)
    links=$(cat "$d"/*.html 2>/dev/null | grep -c '<a href="http' || true)
    cdn=$(cat "$d"/*.html 2>/dev/null | grep -c 'cdn.jsdelivr.net' || true)
    echo "[check] $d: md=$md pages_with_LLM_error=$err external_links=$links cdn_refs=$cdn"
  done
  python3 - "$OUT" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path

out = Path(sys.argv[1])
pct = lambda v: "-" if v is None else f"{v * 100:.1f}%"
mean = lambda xs: sum(xs) / len(xs) if xs else None


def results(stage):
    rows = []
    for p in sorted((out / stage).rglob("result.json")) if (out / stage).is_dir() else []:
        try:
            rows.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return [r for r in rows if r.get("lang") == "cobol"]


docs = [p for p in (out / "docs").rglob("*.md") if p.parent.name == "markdown_docs"] \
    if (out / "docs").is_dir() else []
print(f"[check] docs: {len(docs)} files under {out / 'docs'}")
dg = results("docgen")
if dg:
    ok = [r for r in dg if r.get("status") == "ok"]
    signals = Counter(r.get("signal") or "-" for r in ok)
    print(f"[check] docgen: {len(ok)}/{len(dg)} ok, mean best_phi "
          f"{pct(mean([r.get('best_phi', 0.0) for r in ok]))}, signals {dict(signals)}")
tg = results("testgen")
if tg:
    statuses = Counter(r.get("status") for r in tg)
    ran = [r for r in tg if r.get("status") not in ("spec", "no-toolchain")]
    green = [r for r in ran if r.get("best_size", 0) > 0]
    muts = [r["best_mut"] for r in green if r.get("best_mut") is not None]
    print(f"[check] testgen: statuses {dict(statuses)}, green suites {len(green)}/{len(ran)}, "
          f"mean best_cov {pct(mean([r.get('best_cov', 0.0) for r in green]))}, "
          f"mean best_mut {pct(mean(muts))}, spec targets {statuses.get('spec', 0)}")
judge = out / "judge" / "summary.json"
if judge.is_file():
    s = json.loads(judge.read_text())
    gate = s.get("gate", {})
    print(f"[check] judge: gate pass {gate.get('pass', 0)}/{s.get('docs', 0)}, accuracy "
          f"{pct(s.get('accuracy_micro'))}, coverage {pct(s.get('coverage_macro'))}, "
          f"high-severity errors {s.get('high_errors', 0)}")
PY
  [[ -f "$OUT/report.md" ]] && echo "[check] report: $OUT/report.md"
  echo "[check] expect: LLM_error=0, external_links=0, cdn_refs=0"
fi

echo "[run] done."
