#!/usr/bin/env bash
# One-shot setup: a conda environment with codocs and every toolchain it drives
# (Go, JDK + Maven, GnuCOBOL 3.2, pytest + coverage, C/C++ with gcov), plus the offline
# Mermaid bundle for the wiki HTML.  Usage: ./install.sh [ENV_NAME]   (default: codocs)
set -euo pipefail
cd "$(dirname "$0")"
ENV_NAME="${1:-codocs}"

CONDA="$(command -v mamba || command -v conda || true)"
if [ -z "$CONDA" ]; then
  echo "conda not found: install Miniforge first (https://github.com/conda-forge/miniforge)" >&2
  exit 1
fi

if "$CONDA" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  "$CONDA" env update -n "$ENV_NAME" -f environment.yml
else
  "$CONDA" env create -n "$ENV_NAME" -f environment.yml
fi

# C/C++: cc, c++ and a matching gcov.  On macOS they come from the Xcode Command Line
# Tools (a conda GCC there would put a gcov on PATH that cannot read clang's coverage data).
case "$(uname -s)" in
  Linux)
    # go-cgo: conda-forge's plain `go` is built without cgo on Linux (the lower bound keeps
    # the solver from trading a current Go for an old go-cgo that allows a newer GCC);
    # binutils: unprefixed ar/ld for the C/C++ unit libraries
    "$CONDA" install -y -n "$ENV_NAME" -c conda-forge gcc gxx binutils "go-cgo>=1.24"
    # conda-forge's GnuCOBOL 3.2 records the compiler path and flags of its build machine,
    # and its generated C needs C17 (GCC 15+ defaults to C23): point cobc at the env's
    # compiler with -std=gnu17 whenever the env is activated
    PREFIX="$("$CONDA" run -n "$ENV_NAME" python -c 'import sys; print(sys.prefix)')"
    mkdir -p "$PREFIX/etc/conda/activate.d"
    cat > "$PREFIX/etc/conda/activate.d/codocs-cobc.sh" <<EOF_ACT
export COB_CC=$(uname -m)-conda-linux-gnu-cc
export COB_CFLAGS="-pipe -fPIC -I\$CONDA_PREFIX/include -Wno-unused -fsigned-char -Wno-pointer-sign -std=gnu17"
EOF_ACT
    ;;
  Darwin)
    if ! xcode-select -p >/dev/null 2>&1; then
      echo "Xcode Command Line Tools missing (C/C++ and cobc need them): run xcode-select --install" >&2
    fi ;;
esac

MERMAID="$HOME/.repowiki/mermaid.min.js"
if [ ! -s "$MERMAID" ]; then
  mkdir -p "$(dirname "$MERMAID")"
  curl -fsSL https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.min.js -o "$MERMAID" \
    || echo "WARNING: could not download the Mermaid bundle; wiki HTML will load it from the CDN" >&2
fi

echo "== toolchains in $ENV_NAME"
"$CONDA" run -n "$ENV_NAME" bash -c '
  go version
  java -version 2>&1 | head -1
  mvn -v 2>/dev/null | head -1
  cobc --version | head -1
  cc --version | head -1
  c++ --version | head -1
  gcov --version | head -1
  python -c "import sys, pytest, coverage, tree_sitter_language_pack; print(\"Python\", sys.version.split()[0], \"pytest\", pytest.__version__, \"coverage\", coverage.__version__)"
  codocs --version'
echo "Done. Activate with: conda activate $ENV_NAME"
