"""shared fixtures of the COBOL tests.

- `tests/cobol/_helpers` is put on sys.path (`from wiki_fixtures import ...`);
- marker `needs_cobc`: the test is skipped when the configured cobc is not on PATH;
- fixture `cobc_ok`: whether that cobc is available;
- fixture `datasets`: a function name -> dataset clone dir (or None when absent); the
  tests that use it are skipped when $CODOCS_DATASETS is not set;
- fixtures `cobolmod_copy` / `mixedcobol_copy`: copies of the fixture repos under tmp_path.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXTURES = REPO / "tests" / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"
MIXEDCOBOL = FIXTURES / "mixedcobol"

if str(HERE / "_helpers") not in sys.path:
    sys.path.insert(0, str(HERE / "_helpers"))


def cobc_path() -> str | None:
    """the cobc the settings would use (CODOCS_COBC, else cobc), resolved on PATH."""
    return shutil.which(os.environ.get("CODOCS_COBC") or "cobc")


def datasets_dir() -> Path | None:
    """$CODOCS_DATASETS (read-only), or None when it is not set."""
    env = os.environ.get("CODOCS_DATASETS")
    return Path(env).expanduser() if env else None


def dataset(name: str) -> Path | None:
    """the clone dir of dataset *name*, or None when it is not present."""
    root = datasets_dir()
    if root is not None and (root / name).is_dir():
        return root / name
    return None


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_cobc: needs GnuCOBOL cobc (skipped without it)")


def pytest_runtest_setup(item):
    if item.get_closest_marker("needs_cobc") and cobc_path() is None:
        pytest.skip("GnuCOBOL cobc not available")


@pytest.fixture(scope="session")
def cobc_ok() -> bool:
    return cobc_path() is not None


@pytest.fixture(scope="session")
def datasets() -> Callable[[str], Path | None]:
    if datasets_dir() is None:
        pytest.skip("CODOCS_DATASETS not set (clone the datasets with "
                    "scripts/cobol/fetch_datasets.sh)")
    return dataset


def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


@pytest.fixture
def cobolmod_copy(tmp_path: Path) -> Path:
    return _copy(COBOLMOD, tmp_path / "cobolmod")


@pytest.fixture
def mixedcobol_copy(tmp_path: Path) -> Path:
    return _copy(MIXEDCOBOL, tmp_path / "mixedcobol")
