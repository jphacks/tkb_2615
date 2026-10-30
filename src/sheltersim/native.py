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


def social_relationships_problem(
    relationships: Any, good_keys: set[Any], key_problem: Any
) -> int | None:
    """The first rule a social_network_v1 relationship breaks (1 key, 2 record, 3 trust,
    4 contact minute; 0 when all are valid), or None without the kernel."""
    if _native is None:
        return None
    return _native.social_relationships_problem(relationships, good_keys, key_problem) or 0


def body_relationships_problem(
    relationships: Any, agents: Any, parsed_keys: dict[Any, Any], parse_key: Any
) -> int | None:
    """The first rule a body_state_v1 relationship breaks (1 key, 2 key or record,
    3 trust, 4 contact minute; 0 when all are valid), or None without the kernel."""
    if _native is None:
        return None
    return _native.body_relationships_problem(relationships, agents, parsed_keys, parse_key) or 0


def event_store() -> Any:
    """A new Rust EventLog store (sheltersim_native.EventStore), or None."""
    return None if _native is None else _native.EventStore()


def split_json_array(raw: bytes) -> list[bytes] | None:
    """The bytes of each item of the canonical JSON array raw, or None without the kernel.

    Raises ValueError when raw is not one JSON array.
    """
    return None if _native is None else _native.split_json_array(raw)


def sensitive_candidates(raw: bytes) -> tuple[list[str], list[str]] | None:
    """The distinct object keys of the JSON text raw, and its distinct string values the
    privacy patterns could match, or None without the kernel. Every other string value
    can not match them (native/src/checkpoint.rs). Raises ValueError on a bad string."""
    return None if _native is None else _native.sensitive_candidates(raw)
