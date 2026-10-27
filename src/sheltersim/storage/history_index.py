"""Derived SQLite evidence/checkpoint index. Never owns simulation state or RNG.

Writers ingest immutable checkpoint deltas. Reads require the published branch head
and use B-tree ranges/keyset cursors; old Runs opt in through explicit index rebuild.
The authoritative JSON/checkpoint hashes remain unchanged and independently readable.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from bisect import bisect_left
from contextlib import contextmanager
from typing import Any, Iterator

from sheltersim.domain.social_dialogue import virtual_dialogue_example
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest, safe_document

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations(branch TEXT NOT NULL, kind TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY(branch,kind));
CREATE TABLE IF NOT EXISTS heads(branch TEXT PRIMARY KEY, hash TEXT NOT NULL, minute INTEGER NOT NULL, counts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(branch TEXT NOT NULL, minute INTEGER NOT NULL, hash TEXT NOT NULL, state_hash TEXT NOT NULL, PRIMARY KEY(branch,minute));
CREATE TABLE IF NOT EXISTS records(branch TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, minute INTEGER NOT NULL, event_id TEXT, cause TEXT, source TEXT, event_type TEXT, status TEXT, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, PRIMARY KEY(branch,kind,key));
CREATE INDEX IF NOT EXISTS record_time ON records(branch,kind,minute,key);
CREATE INDEX IF NOT EXISTS record_event ON records(branch,event_id,kind,minute,key);
CREATE INDEX IF NOT EXISTS record_cause ON records(branch,cause,kind,minute,key);
CREATE INDEX IF NOT EXISTS record_source ON records(branch,kind,source,minute,key);
CREATE INDEX IF NOT EXISTS record_status ON records(branch,kind,status,minute,key);
CREATE INDEX IF NOT EXISTS record_type ON records(branch,kind,event_type,minute,key);
CREATE TABLE IF NOT EXISTS entities(branch TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, entity TEXT NOT NULL, minute INTEGER NOT NULL, PRIMARY KEY(branch,kind,key,entity));
CREATE INDEX IF NOT EXISTS entity_time ON entities(branch,kind,entity,minute,key);
CREATE INDEX IF NOT EXISTS entity_turn_order ON entities(branch,kind,entity,key);
CREATE TABLE IF NOT EXISTS tokens(branch TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL, minute INTEGER NOT NULL, key TEXT NOT NULL, PRIMARY KEY(branch,kind,token,minute,key));
PRAGMA user_version=1;
"""  # noqa: E501


def cursor_encode(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(canonical(value).encode()).decode()


def cursor_decode(value: str | None, scope: str, anchor: str) -> tuple[int, str] | None:
    if value is None:
        return None
    try:
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (
            set(data) != {"scope", "anchor", "minute", "key"}
            or data["scope"] != scope
            or data["anchor"] != anchor
            or type(data["minute"]) is not int
            or not isinstance(data["key"], str)
            or len(data["key"]) > 128
        ):
            raise ValueError("foreign cursor")
        return data["minute"], data["key"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise StorageError("stale_or_foreign_cursor: restart this query") from exc


def _terms(text: str) -> list[str]:
    # Literal tokens, not SQL/FTS expressions. Japanese contiguous terms stay intact.
    return sorted(set(re.findall(r"[\w:-]{1,80}", text.casefold())))[:16]


def _identities(value: Any) -> set[str]:
    identities: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith(("_id", "_ids")):
                if isinstance(item, str):
                    identities.add(item)
                elif isinstance(item, list):
                    identities.update(x for x in item if isinstance(x, str))
            elif isinstance(item, (dict, list)):
                identities.update(_identities(item))
    elif isinstance(value, list):
        for item in value:
            identities.update(_identities(item))
    return identities


def _event_references(value: Any) -> set[str]:
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"event_id", "causation_id"} and isinstance(item, str):
                result.add(item)
            elif key in {
                "event_ids",
                "source_event_ids",
                "causation_event_ids",
                "modifier_event_ids",
                "environmental_stressor_event_ids",
            } and isinstance(item, (list, tuple)):
                result.update(part for part in item if isinstance(part, str))
            elif isinstance(item, (dict, list, tuple)):
                result.update(_event_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_event_references(item))
    return result


def _virtual_utterance_example(
    record: dict[str, Any], observed_model_effects: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Build a labeled, deterministic illustration from applied intent and state deltas."""
    interaction = record.get("interaction_type")
    reason = record.get("reason_short")
    if not isinstance(interaction, str) or not isinstance(reason, str):
        return None
    stressed = reason in {"high_stress", "long_wait", "elevated_stress"}
    deltas: dict[str, float] = {}
    for effect in observed_model_effects:
        values = effect.get("deltas") if isinstance(effect, dict) else None
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if isinstance(name, str) and type(value) in (int, float):
                deltas[name] = deltas.get(name, 0.0) + float(value)
    reassurance_up = deltas.get("reassurance", 0.0) > 0
    uncertainty_down = deltas.get("uncertainty", 0.0) < 0
    negative_affect_down = deltas.get("negative_affect", 0.0) < 0
    discontent_down = deltas.get("discontent", 0.0) < 0
    state_cues = [
        cue
        for cue, matched in (
            ("reassurance_increased", reassurance_up),
            ("uncertainty_decreased", uncertainty_down),
            ("negative_affect_decreased", negative_affect_down),
            ("discontent_decreased", discontent_down),
        )
        if matched
    ]

    if interaction == "reassurance_exchange":
        if reassurance_up and uncertainty_down:
            example = (
                "わかっていることを一緒に確かめたら、少し安心できた気がします。"
                "新しい案内も一緒に確認しましょう。"
            )
        elif stressed:
            example = "不安が強いときは、今わかっていることを一つずつ確認しましょう。"
        else:
            example = "気になっている点を、今わかっている範囲で一緒に整理しましょう。"
    elif interaction == "family_care":
        example = (
            "必要なことを一緒に確認できて、少し安心しました。次にできることを考えましょう。"
            if reassurance_up
            else "まず必要なことを確認して、一緒に対応しよう。"
        )
    elif interaction == "voluntary_support":
        example = (
            "声をかけてもらえて、少し気持ちが軽くなった気がします。必要なことを一緒に確認しましょう。"
            if negative_affect_down
            else "必要なことがあれば、一緒に確認しましょう。"
        )
    elif interaction == "cooperation":
        example = (
            "役割を分けて進められて、少し安心しました。できることから一緒に続けましょう。"
            if discontent_down and reassurance_up
            else "役割を分けて、できることから一緒に進めましょう。"
        )
    elif interaction == "complaint_response":
        example = (
            "困っている点を聞いてもらえて、少し落ち着けました。今わかっている案内を一緒に確かめましょう。"
            if discontent_down
            else "困っている点を確認しました。必要な案内を一緒に確かめましょう。"
        )
    elif interaction == "information_reaction":
        example = "聞いた内容の出どころを確かめてから、周りと共有したいです。"
    else:
        example = None
    if example is None:
        return None
    result = {
        "virtual_utterance_example": example,
        "virtual_utterance_policy": "typed-interaction-state-grounded-v2",
        "virtual_utterance_basis": {
            "interaction_type": interaction,
            "action": record.get("action"),
            "reason_short": reason,
            "topic_id": record.get("topic_id"),
            "state_cues": state_cues,
            "observed_model_effects": observed_model_effects,
        },
        "virtual_utterance_label": "fictional_example_not_recorded_speech",
    }
    dialogue = virtual_dialogue_example(record, observed_model_effects)
    if dialogue is not None:
        result["virtual_dialogue_example"] = dialogue
        result["virtual_utterance_policy"] = "typed-interaction-state-grounded-v3"
    return result


class HistoryIndex:
    def __init__(self, store: ProjectStore, project_id: str, run_id: str) -> None:
        self.store, self.project_id, self.run_id = store, project_id, run_id
        self.path = store._path(project_id, "runs", run_id, "history.sqlite")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not write and not self.path.exists():
            raise StorageError("history_index_required: explicitly rebuild the saved Run index")
        if self.path.is_symlink():
            raise StorageError("unsafe_history_index")
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=rwc" if write else "?mode=ro"), uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        try:
            if write:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise StorageError("history_index_version_unsupported")
                connection.executescript(_SCHEMA)
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                # A derived file cannot individually exceed the configured disk cap.
                connection.execute(
                    f"PRAGMA max_page_count={max(1, self.store.max_bytes // page_size)}"
                )
                connection.execute("BEGIN IMMEDIATE")
            else:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise StorageError("history_index_version_unsupported")
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
            yield connection
            if write:
                if self.store.usage()["bytes"] > self.store.max_bytes:
                    connection.rollback()
                    raise StorageError("storage_capacity_exceeded")
                connection.commit()
        except sqlite3.Error as exc:
            if write:
                connection.rollback()
            code = (
                "storage_capacity_exceeded"
                if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
                else "history_index_unavailable"
            )
            raise StorageError(code) from exc
        finally:
            connection.close()

    @staticmethod
    def _put(
        db: sqlite3.Connection,
        branch: str,
        kind: str,
        row: dict[str, Any],
        *,
        key: str | None = None,
    ) -> None:
        row = safe_document(row)
        minute = row.get("minute", row.get("minute_index"))
        if type(minute) is not int or minute < 0:
            raise StorageError("invalid_index_record_minute")
        key = key or digest(row)
        encoded = canonical(row)
        existing = db.execute(
            "SELECT payload FROM records WHERE branch=? AND kind=? AND key=?", (branch, kind, key)
        ).fetchone()
        if existing is not None:
            if existing[0] != encoded:
                raise StorageError("immutable_history_record_conflict")
            db.executemany(
                "INSERT OR IGNORE INTO tokens VALUES(?,?,?,?,?)",
                [
                    (branch, kind, "@event:" + event, minute, key)
                    for event in _event_references(row)
                ],
            )
            return
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                branch,
                kind,
                key,
                minute,
                row.get("event_id"),
                row.get("causation_id"),
                row.get("source_id"),
                row.get("event_type", row.get("action", row.get("kind"))),
                row.get("status"),
                encoded,
                digest(row),
            ),
        )
        db.execute(
            "INSERT INTO generations VALUES(?,?,1) ON CONFLICT(branch,kind) DO UPDATE SET "
            "value=value+1",
            (branch, kind),
        )
        db.executemany(
            "INSERT INTO entities VALUES(?,?,?,?,?)",
            [(branch, kind, key, item, minute) for item in _identities(row)],
        )
        # Index bounded metadata, never long answers, raw prompts or arbitrary prose.
        words = _terms(
            " ".join(
                str(row.get(name, ""))
                for name in (
                    "event_type",
                    "action",
                    "kind",
                    "status",
                    "trigger",
                    "topic_id",
                    "task_kind",
                )
            )
        )
        words = sorted(set(words) | {"@event:" + event for event in _event_references(row)})
        db.executemany(
            "INSERT INTO tokens VALUES(?,?,?,?,?)",
            [(branch, kind, token, minute, key) for token in words],
        )

    def ingest(
        self,
        branch: str,
        checkpoint: Any,
        *,
        observations: list[dict[str, Any]] | None = None,
        rebuild: bool = False,
    ) -> None:
        """Append only new immutable records; checkpoint lists are already in writer memory."""
        with self.connect(write=True) as db:
            prior = db.execute("SELECT * FROM heads WHERE branch=?", (branch,)).fetchone()
            if rebuild:
                # This index is a replaceable read model of immutable source logs.
                # Clear only the requested branch so a new projection version can
                # rebuild it without conflicting with rows derived by older code.
                # Saved conversation turns are their own append-only source and are
                # not reconstructed from simulation checkpoints, so retain them.
                for table in ("records", "entities", "tokens", "generations"):
                    db.execute(
                        f"DELETE FROM {table} WHERE branch=? AND kind!='conversations'",
                        (branch,),
                    )
                for table in ("heads", "checkpoints"):
                    db.execute(f"DELETE FROM {table} WHERE branch=?", (branch,))
                prior = None
            previous_minute = prior["minute"] if prior else -1
            counts = json.loads(prior["counts"]) if prior and not rebuild else {}
            if rebuild:
                previous_minute = -1
            if previous_minute > checkpoint.simulation_minute:
                raise StorageError("history_index_ahead_of_checkpoint")
            known = db.execute(
                "SELECT hash FROM checkpoints WHERE branch=? AND minute=?",
                (branch, checkpoint.simulation_minute),
            ).fetchone()
            if known and known[0] != checkpoint.checkpoint_hash:
                raise StorageError("immutable_checkpoint_index_conflict")
            world, metrics = checkpoint.world_state, checkpoint.metrics_state
            events = world["event_log"]
            body_history = world.get("model_state", {}).get("body_state_v1", {}).get("history", [])
            body_effects_by_event: dict[str, list[dict[str, Any]]] = {}
            if isinstance(body_history, list):
                for item in body_history:
                    if not isinstance(item, dict) or not isinstance(item.get("event_id"), str):
                        continue
                    deltas = item.get("deltas", {})
                    if not isinstance(deltas, dict):
                        continue
                    observed_deltas = {
                        name: value
                        for name, value in deltas.items()
                        if name
                        in {
                            "positive_affect",
                            "negative_affect",
                            "reassurance",
                            "discontent",
                            "uncertainty",
                            "relationship_trust_delta",
                        }
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    }
                    if not observed_deltas:
                        continue
                    body_effects_by_event.setdefault(item["event_id"], []).append(
                        {
                            "state_kind": item.get("kind"),
                            "agent_ids": item.get("actor_ids", []),
                            "deltas": observed_deltas,
                        }
                    )
            offset = counts.get("events", 0)
            if offset > len(events):
                raise StorageError("history_event_prefix_shortened")
            for record in events[offset:]:
                event = record["event"]
                row = {
                    **event,
                    "minute": record["realized_minute"],
                    "status": record["status"],
                    "detail": record.get("detail"),
                    "time_semantics": "recorded_event_minute",
                }
                self._put(db, branch, "events", row, key=digest(record))
                if record["status"] == "applied":
                    event_type = event.get("event_type")
                    payload = event.get("payload", {})
                    if event_type in {
                        "information_contact_update",
                        "information_correction",
                        "proximity_contact_update",
                        "social_interaction",
                    } and isinstance(payload, dict):
                        actor_ids = sorted(
                            {
                                value
                                for name in (
                                    "agent_id",
                                    "participant_agent_id",
                                    "sender_agent_id",
                                    "recipient_agent_id",
                                    "first_agent_id",
                                    "second_agent_id",
                                )
                                if isinstance((value := payload.get(name)), str)
                            }
                        )
                        social = {
                            "schema_version": 1,
                            "event_id": event["event_id"],
                            "minute": record["realized_minute"],
                            "time_semantics": "recorded_event_minute",
                            "event_type": event_type,
                            "status": "applied",
                            "actor_ids": actor_ids,
                            "source_id": event.get("source_id"),
                            "causation_id": event.get("causation_id"),
                            "evidence_semantics": (
                                "structured_model_social_event"
                                if event_type == "social_interaction"
                                else "recorded_information_or_contact_event"
                            ),
                        }
                        for name in (
                            "agent_id",
                            "participant_agent_id",
                            "interaction_type",
                            "action",
                            "topic_id",
                            "message_id",
                            "belief",
                            "contact_basis",
                            "contact_minutes",
                            "distance_m",
                            "zone_id",
                            "reason_short",
                        ):
                            value = payload.get(name)
                            if type(value) in (str, int, float, bool):
                                social[name] = value
                        state_context = payload.get("conversation_state_context")
                        if isinstance(state_context, dict):
                            social["conversation_state_context"] = state_context
                        self._put(db, branch, "social", social, key="event:" + event["event_id"])
                        if event_type == "social_interaction":
                            # A deterministic, explicitly fictional example may help readers
                            # understand the applied typed intent. It is never a stored quote.
                            observed_model_effects = body_effects_by_event.get(
                                event["event_id"], []
                            )
                            dialogue = {
                                **social,
                                "record_type": "structured_social_interaction",
                                "synthetic": True,
                                "utterance_recorded": False,
                                "utterance_status": "not_recorded",
                                "observed_model_effects": observed_model_effects,
                            }
                            dialogue.update(
                                _virtual_utterance_example(social, observed_model_effects) or {}
                            )
                            self._put(
                                db, branch, "dialogue", dialogue, key="event:" + event["event_id"]
                            )
                    from sheltersim.inspection.action_history import applied_action_record

                    action = applied_action_record(event, record["realized_minute"])
                    continuity_is_shared = event[
                        "event_type"
                    ] == "staff_support_continuity" and "shared_operations" in world.get(
                        "model_configs", {}
                    )
                    if action is not None and not continuity_is_shared:
                        index_kind = {
                            "care": "care",
                            "operations": "operations",
                        }.get(action.get("record_kind"), "actions")
                        self._put(
                            db, branch, index_kind, action, key="applied:" + event["event_id"]
                        )
                    if event["event_type"] in {
                        "care_support_transition",
                        "staff_support_continuity",
                        "staff_handoff",
                        "facility_recovery_transition",
                        "staff_operation_transition",
                    }:
                        if "shared_operations" not in world.get("model_configs", {}):
                            raise StorageError("operation_event_without_signed_contract")
                        from pydantic import TypeAdapter

                        from sheltersim.contracts.operations import OperationHistoryRecord

                        try:
                            receipt = TypeAdapter(OperationHistoryRecord).validate_python(
                                event["payload"]
                            )
                        except (TypeError, ValueError) as exc:
                            raise StorageError("operation_event_receipt_invalid") from exc
                        if receipt.causation_id != event.get("causation_id") or (
                            event.get("source_id") is not None
                            and event["source_id"] not in receipt.source_ids
                        ):
                            raise StorageError("operation_event_receipt_provenance_mismatch")
                        row = receipt.model_dump(mode="json")
                        row["source_id"] = event.get("source_id") or next(
                            iter(row.get("source_ids", ())), None
                        )
                        row["minute"] = record["realized_minute"]
                        if (
                            receipt.event_id != event["event_id"]
                            or receipt.minute != record["realized_minute"]
                        ):
                            raise StorageError("operation_event_receipt_time_or_id_mismatch")
                        self._put(
                            db,
                            branch,
                            "care" if row["record_type"] == "care" else "operations",
                            row,
                            key=f"{row['record_type']}:{receipt.event_id}",
                        )
                    if event_type == "facility_queue_transition" and isinstance(payload, dict):
                        from pydantic import TypeAdapter

                        from sheltersim.contracts.extensions import FacilityGeometry
                        from sheltersim.contracts.operations import FacilityQueueTransitionReceipt
                        from sheltersim.domain.facility_queues import (
                            FACILITY_QUEUES_POLICY_VERSION,
                            FACILITY_QUEUES_STATE_KEY,
                        )

                        try:
                            receipt = TypeAdapter(FacilityQueueTransitionReceipt).validate_python(
                                payload
                            )
                            geometry = FacilityGeometry.model_validate(
                                world.get("model_configs", {}).get("facility_geometry", {})
                            )
                        except (TypeError, ValueError) as exc:
                            raise StorageError("facility_queue_event_receipt_invalid") from exc
                        point = next(
                            (
                                row
                                for row in geometry.service_points
                                if row.queue_id == receipt.queue_id
                            ),
                            None,
                        )
                        if (
                            receipt.event_id != event.get("event_id")
                            or receipt.minute != record["realized_minute"]
                            or receipt.causation_id != event.get("causation_id")
                            or event.get("source_id") != FACILITY_QUEUES_POLICY_VERSION
                            or FACILITY_QUEUES_STATE_KEY not in world.get("model_state", {})
                            or point is None
                            or (point.id, point.facility_id)
                            != (receipt.service_point_id, receipt.facility_id)
                        ):
                            raise StorageError("facility_queue_event_receipt_provenance_mismatch")
                        row = receipt.model_dump(mode="json")
                        row["source_id"] = event.get("source_id")
                        self._put(
                            db, branch, "operations", row, key=f"operations:{receipt.event_id}"
                        )
            counts["events"] = len(events)
            snapshots = metrics["snapshots"]
            start = counts.get("kpis", 0)
            resource_rows = metrics.get("resource_snapshots", [])
            for index in range(start, len(snapshots)):
                row = dict(snapshots[index])
                row.pop("source_ids_json", None)
                if resource_rows:
                    row.update(resource_rows[index])
                row["time_semantics"] = "end_of_tick"
                self._put(db, branch, "kpis", row, key=str(row["minute_index"]))
            counts["kpis"] = len(snapshots)
            counts["model_namespaces"] = sorted(world["model_state"])
            counts.setdefault("first_indexed_checkpoint_minute", checkpoint.simulation_minute)
            for kind, namespace, field in (
                ("social", "body_state_v1", "minute"),
                ("staff", "staff_operations_v1", "complete_minute"),
                ("transitions", "population_transitions_v1", "minute"),
                ("transitions", "population_presence_v1", "minute"),
                ("transitions", "population_lifecycle_v1", "minute"),
            ):
                history = world["model_state"].get(namespace, {}).get("history", [])
                # Histories are chronological, may roll over, and may contain several
                # records at one minute. Revisit that minute; stable hashes deduplicate.
                first = bisect_left(
                    history, max(0, previous_minute - 1), key=lambda row: row[field]
                )
                for record in history[first:]:
                    self._put(db, branch, kind, {**record, "minute": record[field]})
            for row in observations or []:
                self._put(db, branch, row.get("record_kind", "actions"), row)
            db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (
                    branch,
                    checkpoint.simulation_minute,
                    checkpoint.checkpoint_hash,
                    checkpoint.state_hash,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO heads VALUES(?,?,?,?)",
                (
                    branch,
                    checkpoint.checkpoint_hash,
                    checkpoint.simulation_minute,
                    canonical(counts),
                ),
            )

    def add_turn(self, branch: str, turn: dict[str, Any]) -> None:
        with self.connect(write=True) as db:
            self._put(db, branch, "conversations", turn, key=turn["turn_id"])

    def ready(self, db: sqlite3.Connection, branch: str, anchor: str) -> None:
        row = db.execute("SELECT hash FROM heads WHERE branch=?", (branch,)).fetchone()
        if row is None or row[0] != anchor:
            raise StorageError("history_index_stale: explicitly resume index rebuild")

    def nearest(self, branch: str, minute: int, anchor: str) -> tuple[int, str] | None:
        with self.connect() as db:
            self.ready(db, branch, anchor)
            row = db.execute(
                "SELECT minute,hash FROM checkpoints WHERE branch=? AND minute<=? ORDER BY "
                "minute DESC LIMIT 1",
                (branch, minute),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def search(self, branch: str, query: Any, anchor: str) -> dict[str, Any]:
        scope = digest(
            {
                "project": self.project_id,
                "run": self.run_id,
                "branch": branch,
                **query.model_dump(mode="json", exclude={"cursor", "limit"}),
            }
        )
        after = (
            cursor_decode(query.cursor, scope, anchor) if query.kind != "conversations" else None
        )
        kind = "events" if query.kind == "causal" else query.kind
        kinds = (
            (
                "events",
                "actions",
                "social",
                "staff",
                "transitions",
                "dialogue",
                "care",
                "operations",
            )
            if query.kind == "causal" and query.include_related
            else (kind,)
        )
        where, args = (
            [
                "r.branch=?",
                "r.kind IN (" + ",".join("?" for _ in kinds) + ")",
                "r.minute BETWEEN ? AND ?",
            ],
            [branch, *kinds, query.start, query.end],
        )
        if kind == "kpis":
            where.append("(r.minute-?) % ? = 0")
            args.extend([query.start, query.step])
        if after:
            if ":" not in after[1]:
                raise StorageError("stale_or_foreign_cursor")
            after_kind, after_key = after[1].split(":", 1)
            if after_kind not in kinds:
                raise StorageError("stale_or_foreign_cursor")
            where.append("(r.minute,r.kind,r.key)>(?,?,?)")
            args.extend((after[0], after_kind, after_key))
        for identity in (
            query.agent_id,
            query.household_id,
            query.staff_id,
            query.zone_id,
            query.conversation_id,
        ):
            if identity:
                where.append(
                    "EXISTS(SELECT 1 FROM entities e WHERE e.branch=r.branch AND "
                    "e.kind=r.kind AND e.key=r.key AND e.entity=?)"
                )
                args.append(identity)
        for name, value in (
            ("event_type", query.event_type),
            ("source", query.source_id),
            ("status", query.status),
        ):
            if value is not None:
                where.append(f"r.{name}=?")
                args.append(value)
        if query.event_id and query.kind != "causal":
            where.append(
                "EXISTS(SELECT 1 FROM tokens t WHERE t.branch=r.branch AND t.kind=r.kind AND "
                "t.key=r.key AND t.token=?)"
            )
            args.append("@event:" + query.event_id)
        for term in _terms(query.text or ""):
            where.append(
                "EXISTS(SELECT 1 FROM tokens t WHERE t.branch=r.branch AND t.kind=r.kind AND "
                "t.key=r.key AND t.token=?)"
            )
            args.append(term)
        graph = None
        with self.connect() as db:
            self.ready(db, branch, anchor)
            if query.kind == "causal":
                visited, frontier = {query.event_id}, [query.event_id]
                link_limit_hit = False
                while frontier and len(visited) < 500:
                    current = frontier.pop(0)
                    links = db.execute(
                        "SELECT event_id,cause FROM records WHERE branch=? AND kind='events' "
                        "AND (event_id=? OR cause=?) LIMIT 501",
                        (branch, current, current),
                    ).fetchall()
                    link_limit_hit = link_limit_hit or len(links) >= 501
                    for row in links:
                        for other in row:
                            if other and other not in visited:
                                if len(visited) == 500:
                                    break
                                visited.add(other)
                                frontier.append(other)
                where.append(
                    (
                        "EXISTS(SELECT 1 FROM tokens t WHERE t.branch=r.branch AND t.kind=r.kind "
                        "AND t.key=r.key AND t.token IN ("
                    )
                    + ",".join("?" for _ in visited)
                    + "))"
                )
                args.extend("@event:" + event for event in sorted(visited))
                graph = {
                    "causal_graph_truncated": link_limit_hit
                    or bool(frontier)
                    or len(visited) >= 500,
                    "root_present": db.execute(
                        "SELECT 1 FROM records WHERE branch=? AND kind='events' AND "
                        "event_id=? LIMIT 1",
                        (branch, query.event_id),
                    ).fetchone()
                    is not None,
                }
            if kind == "conversations":
                counter = db.execute(
                    "SELECT value FROM generations WHERE branch=? AND kind='conversations'",
                    (branch,),
                ).fetchone()
                anchor = digest({"head": anchor, "turn_count": counter[0] if counter else 0})
                after = cursor_decode(query.cursor, scope, anchor)
                if after:
                    if ":" not in after[1]:
                        raise StorageError("stale_or_foreign_cursor")
                    after_kind, after_key = after[1].split(":", 1)
                    if after_kind != "conversations":
                        raise StorageError("stale_or_foreign_cursor")
                    where.append("(r.minute,r.kind,r.key)>(?,?,?)")
                    args.extend((after[0], after_kind, after_key))
            rows = db.execute(
                "SELECT r.kind,r.minute,r.key,r.payload,r.payload_hash FROM records r WHERE "
                + " AND ".join(where)
                + " ORDER BY r.minute,r.kind,r.key LIMIT ?",
                (*args, query.limit + 1),
            ).fetchall()
            coverage_rows = db.execute(
                "SELECT kind,MIN(minute),MAX(minute),COUNT(*) FROM records "
                "WHERE branch=? AND kind IN (" + ",".join("?" for _ in kinds) + ") "
                "AND minute BETWEEN ? AND ? GROUP BY kind",
                (branch, *kinds, query.start, query.end),
            ).fetchall()
            head = db.execute("SELECT counts FROM heads WHERE branch=?", (branch,)).fetchone()
        coverage_by_kind = {
            row[0]: {"oldest_minute": row[1], "newest_minute": row[2], "indexed_records": row[3]}
            for row in coverage_rows
        }
        indexed_minutes = [
            minute
            for item in coverage_by_kind.values()
            for minute in (item["oldest_minute"], item["newest_minute"])
            if minute is not None
        ]
        counts = json.loads(head[0]) if head is not None else {}
        selected = rows[: query.limit]
        for row in selected:
            if digest(json.loads(row["payload"])) != row["payload_hash"]:
                raise StorageError("history_index_record_corrupt: rebuild from immutable source")
        following = (
            cursor_encode(
                {
                    "scope": scope,
                    "anchor": anchor,
                    "minute": selected[-1]["minute"],
                    "key": selected[-1]["kind"] + ":" + selected[-1]["key"],
                }
            )
            if len(rows) > query.limit
            else None
        )
        return {
            "schema_version": 1,
            "items": [
                {**json.loads(row["payload"]), "record_kind": row["kind"]} for row in selected
            ],
            "next_cursor": following,
            "anchor_hash": anchor,
            "total_items": None,
            "count_status": "not_calculated",
            "query_strategy": "indexed_keyset",
            "coverage": "persisted_index_records",
            "coverage_scope": "branch_kind_time_window_before_identity_and_text_filters",
            "completeness": "unknown",
            "completeness_reason": (
                "The index exposes persisted producer records but cannot certify that every "
                "domain event was produced or retained."
            ),
            "oldest_minute": min(indexed_minutes) if indexed_minutes else None,
            "newest_minute": max(indexed_minutes) if indexed_minutes else None,
            "retention_limit": None,
            "retention_limit_reason": (
                "No per-kind source eviction boundary is persisted; absence of records is not "
                "evidence that no event occurred."
            ),
            "requested_range": [query.start, query.end],
            "coverage_by_kind": coverage_by_kind,
            "first_indexed_checkpoint_minute": counts.get("first_indexed_checkpoint_minute"),
            "complete_lifetime_history": False,
            "causal_interpretation": "Recorded model links, not empirical causal proof",
            **(graph or {}),
        }

    def rebuild(self, branch: str) -> dict[str, Any]:
        """Explicit legacy migration, never a hidden GET side effect or simulation."""
        lease = self.store.claim_run(self.project_id, self.run_id)
        try:
            from types import SimpleNamespace

            reference = self.store.branch_reference(self.project_id, self.run_id, branch)
            document = self.store.checkpoint_document(
                self.project_id, self.run_id, reference.checkpoint_hash
            )
            checkpoint = SimpleNamespace(**document)
            self.ingest(branch, checkpoint, rebuild=True)
            legacy = self.store._path(
                self.project_id, "runs", self.run_id, "branches", branch, "timeline.json"
            )
            entries = self.store._read(legacy)["checkpoints"] if legacy.exists() else {}
            with self.connect(write=True) as db:
                for minute, key in entries.items():
                    if (
                        not re.fullmatch(r"[a-f0-9]{64}", key)
                        or not reference.fork_minute <= int(minute) <= reference.computed_until
                    ):
                        raise StorageError("invalid_legacy_checkpoint_index")
                    # The original checkpoint is hash-validated when selected for replay.
                    known = db.execute(
                        "SELECT hash FROM checkpoints WHERE branch=? AND minute=?",
                        (branch, int(minute)),
                    ).fetchone()
                    if known and known[0] != key:
                        raise StorageError("immutable_checkpoint_index_conflict")
                    db.execute(
                        "INSERT OR IGNORE INTO checkpoints VALUES(?,?,?,?)",
                        (branch, int(minute), key, ""),
                    )
            for turn in self.store.conversations(self.project_id, self.run_id, branch):
                self.add_turn(branch, turn)
            return {
                "schema_version": 1,
                "status": "indexed_not_verified",
                "branch_id": branch,
                "checkpoint_hash": checkpoint.checkpoint_hash,
                "limits": (
                    "Legacy actions/expired contacts absent from source cannot be reconstructed "
                    "by an index."
                ),
            }
        finally:
            self.store.release_run(lease)

    def checkpoints(
        self, branch: str, start: int, end: int, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        reference = self.store.branch_reference(self.project_id, self.run_id, branch)
        if (
            not reference.fork_minute <= start <= end <= reference.computed_until
            or not 1 <= limit <= 100
        ):
            raise StorageError("invalid_checkpoint_page_range")
        scope = digest(
            {
                "run": self.run_id,
                "branch": branch,
                "start": start,
                "end": end,
                "kind": "checkpoints",
            }
        )
        after = cursor_decode(cursor, scope, reference.checkpoint_hash)
        with self.connect() as db:
            self.ready(db, branch, reference.checkpoint_hash)
            rows = db.execute(
                "SELECT minute,hash,state_hash FROM checkpoints WHERE branch=? AND minute "
                "BETWEEN ? AND ? AND minute>? ORDER BY minute LIMIT ?",
                (branch, start, end, after[0] if after else -1, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        return {
            "schema_version": 1,
            "items": [dict(row) for row in selected],
            "anchor_hash": reference.checkpoint_hash,
            "next_cursor": cursor_encode(
                {
                    "scope": scope,
                    "anchor": reference.checkpoint_hash,
                    "minute": selected[-1]["minute"],
                    "key": "",
                }
            )
            if len(rows) > limit
            else None,
            "total_items": None,
            "query_strategy": "indexed_keyset",
        }

    def conversation_tail(
        self, branch: str, conversation_id: str | None, limit: int = 6
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise StorageError("invalid_page_limit")
        reference = self.store.branch_reference(self.project_id, self.run_id, branch)
        with self.connect() as db:
            self.ready(db, branch, reference.checkpoint_hash)
            if conversation_id:
                rows = db.execute(
                    "SELECT r.payload,r.payload_hash FROM entities e JOIN records r ON "
                    "r.branch=e.branch AND r.kind=e.kind AND r.key=e.key WHERE e.branch=? "
                    "AND e.kind='conversations' AND e.entity=? ORDER BY e.key DESC LIMIT ?",
                    (branch, conversation_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload,payload_hash FROM records WHERE branch=? AND "
                    "kind='conversations' ORDER BY key DESC LIMIT ?",
                    (branch, limit),
                ).fetchall()
        for row in rows:
            if digest(json.loads(row["payload"])) != row["payload_hash"]:
                raise StorageError("history_index_record_corrupt")
        return [json.loads(row["payload"]) for row in reversed(rows)]
