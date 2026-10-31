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


class _NeverRaised(Exception):
    """Stands for Unsupported when no kernel exists to raise it."""


# Catch this around a kernel from rules() or state(): the Python code then decides.
Unsupported: type[Exception] = _NeverRaised if _native is None else _native.Unsupported


def rules(name: str) -> Any:
    """The module of native/src/rules/<name>.rs, or None without the extension.

    Porting lanes call their kernels through it and need no function in this file.
    """
    return None if _native is None else getattr(_native.rules, name)


def state(name: str) -> Any:
    """The module of native/src/state/<name>.rs, or None without the extension."""
    return None if _native is None else getattr(_native.state, name)


def world_core() -> Any:
    """A new sheltersim_native.WorldCore, or None without the extension (core/state_mirror.py)."""
    return None if _native is None else _native.WorldCore()


def install_tracked_types(*classes: type) -> None:
    """Tell the kernels which dict and list subclasses read as plain ones (core/tracked.rs)."""
    if _native is not None:
        _native.install_tracked_types(*classes)


def install_id_map_type(id_map: type) -> None:
    """Tell the kernels that a world's maps of entities read as plain dicts (core/tracked.rs)."""
    if _native is not None:
        _native.install_id_map_type(id_map)


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


def scheduled_event_checked(*arguments: Any) -> tuple[Any, Any] | None:
    """The frozen payload and provenance of an event that passes ScheduledEvent's checks,
    or None for the Python checks to decide, which raise any error."""
    if _native is None:
        return None
    try:
        return _native.scheduled_event_checked(*arguments)
    except _native.Unsupported:
        return None


def view_state_hash(*arguments: Any) -> str | None:
    """The audit hash of a WorldView, or None when the kernel leaves it to Python."""
    if _native is None:
        return None
    try:
        return _native.view_state_hash(*arguments)
    except (_native.Unsupported, TypeError):
        return None


def social_relationships_valid(relationships: Any, good_keys: set[str], key_ok: Any) -> bool:
    """True only if every social_network_v1 relationship passes the Python rules."""
    return _native is not None and _native.social_relationships_valid(
        relationships, good_keys, key_ok
    )




def agent_dynamics_valid(agents: Any, facilities: Any) -> bool:
    """True only if every agent_dynamics_v1 record passes the Python rules."""
    return _native is not None and _native.agent_dynamics_valid(agents, facilities)


def movement_positions_valid(positions: Any, zones: Any, edges: Any) -> bool:
    """True only if every movement_v1 position passes the Python rules."""
    return _native is not None and _native.movement_positions_valid(positions, zones, edges)


def world_agents_valid(
    agents: Any, zones: Any, activity_type: Any, initial_needs_type: Any, needs_type: Any
) -> bool:
    """True only if every AgentState passes the per-Agent rules of a v6 WorldState."""
    return _native is not None and _native.world_agents_valid(
        agents, zones, activity_type, initial_needs_type, needs_type
    )


def index_row(row: dict[str, Any]) -> tuple[str, str, list, list, str, list, str | None] | None:
    """(canonical JSON, its SHA-256, identities, event references, sorted keys joined by NUL,
    strings to check, the text its tokens are read from or None) of a row of plain lists,
    str-keyed dicts and JSON scalars (storage/history_index.py), or None for any other row or
    without the kernel. Identities and references may repeat."""
    return None if _native is None else _native.index_row(row)


def dumps_length(value: Any) -> int | None:
    """len(json.dumps(value, ensure_ascii=False).encode()), or None to leave it to json."""
    if _native is None:
        return None
    try:
        return _native.dumps_length(value)
    except _native.Unsupported:
        return None


def record_json(record: Any, *types: type) -> bytes | None:
    """The canonical JSON of record.to_dict() for a record of the types EventLogRecord and
    ScheduledEvent with only frozen plain values, or None for any other or without the kernel."""
    return None if _native is None else _native.record_json(record, *types)


def history_pins() -> Any:
    """What tells cheaply that a history's entries were not changed, or None (history.rs)."""
    return None if _native is None else _native.HistoryPins()


def same_saved_records(records: list[Any], raws: list[bytes], *types: type) -> bool | None:
    """Whether each of records (of the types EventLogRecord and ScheduledEvent) is exactly the
    record restoring raws gives, read from the bytes; a false says nothing about ==, and
    the caller compares restored records. None without the kernel."""
    return None if _native is None else _native.same_saved_records(records, raws, *types)


def same_json_items(current: Any, documents: list[Any], start: int, count: int) -> bool | None:
    """Whether each of current[:count] is exactly documents[start + i], types included
    (1, 1.0 and True differ, and a tuple and a list), or None without the kernel."""
    return None if _native is None else _native.same_json_items(current, documents, start, count)


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
