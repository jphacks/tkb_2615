"""Optional Rust kernels (experiment/rust-native only).

Without the sheltersim_native extension, or with SHELTERSIM_NATIVE=0, every
function here returns None and callers use their Python implementation. A kernel
returns exactly the bytes of the Python code it replaces, or None for a value it
does not handle, so results never depend on whether it is installed.
"""

from __future__ import annotations

import os
from typing import Any

try:
    import sheltersim_native as _native
except ImportError:  # pragma: no cover - the extension is optional
    _native = None

if os.environ.get("SHELTERSIM_NATIVE", "1") == "0":
    _native = None


def available() -> bool:
    return _native is not None


def canonical_json(value: Any) -> bytes | None:
    """json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    allow_nan=False).encode(), or None when the kernel leaves value to json."""
    if _native is None:
        return None
    try:
        return _native.canonical_json(value)
    except _native.Unsupported:
        return None


def canonical_sha256(value: Any) -> str | None:
    """The SHA-256 hex digest of canonical_json(value), or None."""
    if _native is None:
        return None
    try:
        return _native.canonical_sha256(value)
    except _native.Unsupported:
        return None


def psychology_integrated(globals_: list[float], agents: list[list[float]]) -> list | None:
    """The new state floats of IntegratedDistressLifeStatePolicy for each agent, or None."""
    if _native is None:
        return None
    try:
        return _native.psychology_integrated(globals_, agents)
    except _native.Unsupported:
        return None
