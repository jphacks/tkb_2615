"""Optional Rust kernels (docs/rust-port.md).

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


def information_contacts_without_messages(*arguments: Any) -> tuple | None:
    """One tick of InformationSpreadPolicy.update while nobody holds a belief, or None."""
    if _native is None:
        return None
    try:
        return _native.information_contacts_without_messages(*arguments)
    except (_native.Unsupported, TypeError):
        return None


def social_channel_update(*arguments: Any) -> tuple | None:
    """One tick of ExpressivenessChannelOpennessPolicy.update, or None."""
    if _native is None:
        return None
    try:
        return _native.social_channel_update(*arguments)
    except (_native.Unsupported, TypeError):
        return None


def social_relationships_valid(relationships: Any, good_keys: set[str], key_ok: Any) -> bool:
    """True only if every social_network_v1 relationship passes the Python rules."""
    return _native is not None and _native.social_relationships_valid(
        relationships, good_keys, key_ok
    )


def body_relationships_valid(relationships: Any, good_keys: set[str], key_ok: Any) -> bool:
    """True only if every body_state_v1 relationship passes the Python rules."""
    return _native is not None and _native.body_relationships_valid(
        relationships, good_keys, key_ok
    )


def body_agents_valid(agents: Any) -> bool:
    """True only if every body_state_v1 Agent record passes the Python rules."""
    return _native is not None and _native.body_agents_valid(agents)


def psychological_agents_valid(agents: Any) -> bool:
    """True only if every psychological_distress_v1 Agent record passes the Python rules."""
    return _native is not None and _native.psychological_agents_valid(agents)


def agent_dynamics_valid(agents: Any, facilities: Any) -> bool:
    """True only if every agent_dynamics_v1 record passes the Python rules."""
    return _native is not None and _native.agent_dynamics_valid(agents, facilities)


def movement_positions_valid(positions: Any, zones: Any, edges: Any) -> bool:
    """True only if every movement_v1 position passes the Python rules."""
    return _native is not None and _native.movement_positions_valid(positions, zones, edges)


def information_contacts_valid(
    contacts: Any, good_keys: set[str], key_ok: Any, duration_minutes: int
) -> bool:
    """True only if every information-spread contact passes the Python rules."""
    return _native is not None and _native.information_contacts_valid(
        contacts, good_keys, key_ok, duration_minutes
    )


def unit_values_valid(values: Any) -> bool:
    """True only if every value of the dict is a finite number within 0..1."""
    return _native is not None and _native.unit_values_valid(values)


def world_agents_valid(
    agents: Any, zones: Any, activity_type: Any, initial_needs_type: Any, needs_type: Any
) -> bool:
    """True only if every AgentState passes the per-Agent rules of a v6 WorldState."""
    return _native is not None and _native.world_agents_valid(
        agents, zones, activity_type, initial_needs_type, needs_type
    )


def _dict_watchers_installed() -> bool:
    """Hand the dict watcher functions, which are outside the stable ABI, to the kernel."""
    try:
        import ctypes

        api = ctypes.pythonapi
        addresses = [
            ctypes.cast(getattr(api, name), ctypes.c_void_p).value or 0
            for name in ("PyDict_AddWatcher", "PyDict_Watch", "PyDict_Unwatch")
        ]
    except (ImportError, AttributeError, OSError):
        return False
    return _native.install_dict_watcher(*addresses)


def record_watch(list_field: str | None = None) -> Any:
    """A new watch of a dict's records (sheltersim_native.RecordWatch), or None if this
    Python or build cannot watch dicts; the caller then reads every record."""
    if _native is None or not _dict_watchers_installed():
        return None
    return _native.RecordWatch(list_field)


def values_snapshot() -> Any:
    """A new held list of a dict's values (sheltersim_native.ValuesSnapshot), or None."""
    return None if _native is None else _native.ValuesSnapshot()


def event_store() -> Any:
    """A new Rust EventLog store (sheltersim_native.EventStore), or None."""
    return None if _native is None else _native.EventStore()


def split_json_array(raw: bytes) -> list[bytes] | None:
    """The bytes of each item of the canonical JSON array raw, or None without the kernel.

    Raises ValueError when raw is not one JSON array.
    """
    return None if _native is None else _native.split_json_array(raw)


def sensitive_candidates(raws: list[bytes]) -> tuple[list[str], list[str]] | None:
    """The distinct object keys of the JSON texts raws, and their distinct string values the
    privacy patterns could match, or None without the kernel. Every other string value
    can not match them (native/src/checkpoint.rs). Raises ValueError on a bad string."""
    return None if _native is None else _native.sensitive_candidates(raws)


def chunk_hashes(items: list[bytes], chunks: list[tuple[int, int]]) -> list[str] | None:
    """The SHA-256 hex digest of b"[" + b",".join(items[start : start + count]) + b"]" for
    each (start, count) of chunks, computed in parallel, or None without the kernel."""
    return None if _native is None else _native.chunk_hashes(items, chunks)
