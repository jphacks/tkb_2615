"""The package reports its version."""

from __future__ import annotations

import sheltersim


def test_package_has_version() -> None:
    assert sheltersim.__version__ == "0.1.0"
