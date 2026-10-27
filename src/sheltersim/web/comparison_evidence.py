"""Bounded descriptive input/approved-command differences, without World restore."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
import json
from typing import Any

from sheltersim.contracts.workspace import ComparisonDifferenceQuery
from sheltersim.core.checkpoint import CheckpointError
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.history_index import cursor_decode, cursor_encode
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest

_MISSING = object()


def _summary(value: Any) -> dict[str, Any]:
    if value is _MISSING:
        return {"present": False}
    encoded = canonical(value)
    result = {"present": True, "hash": digest(value), "value_omitted": len(encoded.encode()) > 2048}
    if result["value_omitted"]:
        result["type"] = type(value).__name__
        if isinstance(value, (list, dict, str)):
            result["length"] = len(value)
    else:
        result["value"] = value
    return result


def _named_rows(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value:
        return None
    for field in ("id", "command_id"):
        if all(isinstance(row, dict) and isinstance(row.get(field), str) for row in value):
            result = {row[field]: row for row in value}
            if len(result) == len(value):
                return result
    return None


def _differences(left: Any, right: Any, path: str = "", depth: int = 0) -> Iterator[dict[str, Any]]:
    if left == right:
        return
    left_rows, right_rows = _named_rows(left), _named_rows(right)
    # Stable IDs align synthetic actors/facilities and approved commands. Other
    # arrays retain their stored order and are summarized as a whole changed value.
    if (left_rows is not None or left == []) and (right_rows is not None or right == []):
        left, right = left_rows or {}, right_rows or {}
        path += "/@id"
    if isinstance(left, dict) and isinstance(right, dict) and depth < 32:
        for key in sorted(left.keys() | right.keys()):
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _differences(
                left.get(key, _MISSING), right.get(key, _MISSING), path + "/" + token, depth + 1
            )
    else:
        yield {"path": path, "baseline": _summary(left), "candidate": _summary(right)}


def _commands(
    store: ProjectStore, project: str, run: str, checkpoint_hash: str
) -> list[dict[str, Any]]:
    path = store._path(project, "runs", run, "checkpoints", checkpoint_hash + ".json")
    record = store._read(path)
    if record.get("checkpoint_hash") != checkpoint_hash:
        raise StorageError("checkpoint_pointer_integrity_mismatch")
    try:
        if record.get("storage_type") in {"checkpoint_archive_v1", "checkpoint_archive_v2"}:
            # Project only Branch audit data; never load WorldState or response tape.
            commands = CheckpointArchive(path.parent).project(
                record, ("runtime_state", "branch", "command_log")
            )
        else:
            commands = (
                store.checkpoint_document(project, run, checkpoint_hash)["runtime_state"]
                .get("branch", {})
                .get("command_log", [])
            )
    except (CheckpointError, KeyError, TypeError, ValueError) as exc:
        raise StorageError("comparison_command_evidence_unavailable") from exc
    if not isinstance(commands, list) or any(
        not isinstance(row, dict) or type(row.get("minute")) is not int for row in commands
    ):
        raise StorageError("invalid_saved_command_evidence")
    return commands


def condition_differences(
    store: ProjectStore, project: str, query: ComparisonDifferenceQuery
) -> dict[str, Any]:
    keys = [(target.run_id, target.branch_id) for target in query.targets]
    if len(set(keys)) != len(keys):
        raise StorageError("comparison_targets_must_be_distinct")
    documents, references, heads = [], [], []
    for target in query.targets:
        run = store.run(project, target.run_id)
        reference = store.branch_reference(project, target.run_id, target.branch_id)
        if not reference.fork_minute <= query.minute <= reference.computed_until:
            raise StorageError("comparison_outside_common_computed_range")
        scenario = store.scenario(project, run["scenario_id"], run["scenario_version"])
        document = json.loads(scenario.canonical_json())
        inputs = {
            key: document[key]
            for key in (
                "seed",
                "duration_minutes",
                "model_configs",
                "zones",
                "zone_edges",
                "facilities",
                "staff",
                "profiles",
                "initial_stock",
                "scheduled_events",
            )
            if key in document
        }
        commands = _commands(store, project, target.run_id, reference.checkpoint_hash)
        documents.append(
            {
                "scenario_inputs": inputs,
                "approved_commands": [row for row in commands if row["minute"] <= query.minute],
            }
        )
        heads.append(reference.checkpoint_hash)
        references.append(
            {
                "run_id": target.run_id,
                "branch_id": target.branch_id,
                "checkpoint_hash": reference.checkpoint_hash,
                "scenario_id": run["scenario_id"],
                "scenario_version": run["scenario_version"],
                "scenario_hash": run["scenario_hash"],
            }
        )
    anchor = digest(references)
    scope = digest(
        {"project": project, **query.model_dump(mode="json", exclude={"limit", "cursor"})}
    )
    after = cursor_decode(query.cursor, scope, anchor)
    offset = after[0] if after else 0
    if offset < 0 or after and after[1] != "":
        raise StorageError("invalid_difference_cursor")

    def rows() -> Iterator[dict[str, Any]]:
        for index, document in enumerate(documents[1:], 1):
            for row in _differences(documents[0], document):
                yield {
                    "candidate_index": index,
                    "baseline_reference": references[0],
                    "candidate_reference": references[index],
                    **row,
                }

    selected = list(islice(rows(), offset, offset + query.limit + 1))
    if any(
        store.branch_reference(project, target.run_id, target.branch_id).checkpoint_hash != head
        for target, head in zip(query.targets, heads)
    ):
        raise StorageError("history_changed_during_query")
    return {
        "schema_version": 1,
        "items": selected[: query.limit],
        "anchor_hash": anchor,
        "next_cursor": cursor_encode(
            {"scope": scope, "anchor": anchor, "minute": offset + query.limit, "key": ""}
        )
        if len(selected) > query.limit
        else None,
        "total_items": None,
        "minute": query.minute,
        "verified": False,
        "path_semantics": "JSON pointer; /@id maps uniquely identified stored rows by id or command_id",
        "integrity_scope": "Scenario logical hashes and selected checkpoint manifest/chunks; not a full World verification",
        "limits": "Original Scenario inputs and approved command audit through minute, not an effective physical outcome or causal proof. Configuration traversal may revisit earlier paths on later pages; this is not historical-log streaming.",
    }
