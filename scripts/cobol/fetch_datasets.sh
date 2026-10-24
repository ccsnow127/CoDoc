#!/usr/bin/env bash
# Fetch the evaluation datasets listed in datasets/manifest.json at their pinned commits
# into $CODOCS_DATASETS/<name>/ (sources are never vendored). CODOCS_DATASETS must be set.
#   CODOCS_DATASETS=/data/cobol bash scripts/cobol/fetch_datasets.sh        # all
#   CODOCS_DATASETS=/data/cobol bash scripts/cobol/fetch_datasets.sh cbi    # one
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ -z "${CODOCS_DATASETS:-}" ]]; then
  echo "[fetch] ERROR: set CODOCS_DATASETS to the directory the datasets go to" >&2
  exit 2
fi
DATASETS="$CODOCS_DATASETS"
mkdir -p "$DATASETS"

python3 - "$@" <<'PY' | while IFS=$'\t' read -r name repo commit; do
import json, sys
want = set(sys.argv[1:])
for d in json.load(open("datasets/manifest.json"))["datasets"]:
    if not want or d["name"] in want:
        print(f'{d["name"]}\t{d["repo"]}\t{d["commit"]}')
PY
  dir="$DATASETS/$name"
  if [[ -d "$dir/.git" ]] && git -C "$dir" rev-parse HEAD | grep -q "^$commit"; then
    echo "[fetch] $name already at $commit"; continue
  fi
  echo "[fetch] $name <- $repo @ $commit"
  rm -rf "$dir"; mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$repo"
  if ! git -C "$dir" fetch -q --depth 1 origin "$commit" 2>/dev/null; then
    # short SHAs cannot be fetched directly: fall back to a full clone + checkout
    rm -rf "$dir"; git clone -q "$repo" "$dir"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout -q "${commit}" 2>/dev/null || git -C "$dir" checkout -q FETCH_HEAD
  echo "[fetch] $name at $(git -C "$dir" rev-parse --short HEAD)"
done
