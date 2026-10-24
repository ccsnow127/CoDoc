"""shared pytest configuration of the codocs tests."""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: needs a slow toolchain (Maven); deselect with "
                                       "-m 'not slow'")
