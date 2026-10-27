"""Bounded, read-only Project evidence queries. No live provider capability."""

from __future__ import annotations

import base64
import json
from typing import Any

from sheltersim.contracts.workspace import ComparisonQuery, HistoryQuery
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest


def page(
    items: list[Any], query: dict[str, Any], anchor: str, *, limit: int, cursor: str | None = None
) -> dict[str, Any]:
    scope = digest(query)
    offset = 0
    if cursor is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except (ValueError, UnicodeDecodeError) as exc:
            raise StorageError("invalid_cursor") from exc
        if (
            not isinstance(token, dict)
            or set(token) != {"scope", "anchor", "offset"}
            or token["scope"] != scope
            or token["anchor"] != anchor
            or type(token["offset"]) is not int
            or token["offset"] < 0
        ):
            raise StorageError("stale_or_foreign_cursor: restart this query")
        offset = token["offset"]
    next_offset = offset + limit
    following = None
    if next_offset < len(items):
        following = base64.urlsafe_b64encode(
            canonical(
                {
                    "scope": scope,
                    "anchor": anchor,
                    "offset": next_offset,
                }
            ).encode()
        ).decode()
    return {
        "schema_version": 1,
        "items": items[offset:next_offset],
        "next_cursor": following,
        "anchor_hash": anchor,
        "total_items": len(items),
    }


class ProjectQueries:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def history(
        self, project_id: str, run_id: str, branch_id: str, query: HistoryQuery
    ) -> dict[str, Any]:
        reference = self.store.branch_reference(project_id, run_id, branch_id)
        if not reference.fork_minute <= query.start <= query.end <= reference.computed_until:
            raise StorageError("history_outside_computed_range")
        if query.kind not in {"people", "households"}:
            from sheltersim.storage.history_index import HistoryIndex

            result = HistoryIndex(self.store, project_id, run_id).search(
                branch_id, query, reference.checkpoint_hash
            )
            if (
                self.store.branch_reference(project_id, run_id, branch_id).checkpoint_hash
                != reference.checkpoint_hash
            ):
                raise StorageError("history_changed_during_query: retry")
            time_semantics = "end_of_tick" if query.kind == "kpis" else "recorded_event_minute"
            for item in result.get("items", []):
                if isinstance(item, dict):
                    item.setdefault("run_id", run_id)
                    item.setdefault("branch_id", branch_id)
                    item.setdefault("time_semantics", time_semantics)
            result.update(
                {
                    "run_id": run_id,
                    "branch_id": branch_id,
                    "kind": query.kind,
                    "computed_until": reference.computed_until,
                    "complete_lifetime_history": False,
                    "time_semantics": time_semantics,
                }
            )
            return result
        if any((query.zone_id, query.event_type, query.source_id, query.status, query.text)):
            raise StorageError("snapshot_filters_unsupported: search recorded evidence instead")
        scope = {
            "project_id": project_id,
            "run_id": run_id,
            "branch_id": branch_id,
            **query.model_dump(mode="json", exclude={"cursor", "limit"}),
        }
        anchor = reference.checkpoint_hash
        # Page timestamps before reconstruction; no request can replay an unbounded interval.
        minutes = range(query.start, query.end + 1, query.step)
        result = page(minutes, scope, anchor, limit=min(query.limit, 20), cursor=query.cursor)
        timeline = TimelineStore(self.store, project_id, run_id)
        rows = []
        requested_minutes = list(result["items"])
        replay = None
        if requested_minutes and query.step <= 5:
            replay = iter(
                timeline.iter_worlds(branch_id, requested_minutes[0], requested_minutes[-1])
            )
        for minute in requested_minutes:
            if replay is None:
                world = timeline.seek(branch_id, minute).world
            else:
                world = next(replay)
                while world.minute_index < minute:
                    world = next(replay)
            from sheltersim.inspection.frames import person_projection, social_event_records

            presence = world.model_state.get("population_presence_v1", {}).get("people", {})
            if query.kind == "people":
                ids = [query.agent_id]
            else:
                from sheltersim.contracts.population import population_context_people

                context = population_context_people(world)
                ids = sorted(
                    {
                        key
                        for key, agent in world.agents.items()
                        if (
                            presence.get(key, {}).get(
                                "household_id",
                                context.get(key, {}).get("household_id", agent.profile.group_id),
                            )
                        )
                        == query.household_id
                    }
                    | {
                        key
                        for key, value in presence.items()
                        if value.get("household_id") == query.household_id
                    }
                )
            interactions, _ = social_event_records(world)
            projections = [
                person_projection(world, identity, social_events=interactions) for identity in ids
            ]
            from sheltersim.contracts.runtime import entity_projections

            extensions = entity_projections(world, entity_ids=set(ids))
            for identity, projection in zip(ids, projections):
                if projection["person"] is not None and ("agent", identity) in extensions:
                    projection["person"]["extensions"] = extensions[("agent", identity)]
            people = [row["person"] for row in projections if row["person"] is not None]
            positions = [row["position"] for row in projections if row["position"] is not None]
            recorded_presence = [
                row["presence"] for row in projections if row["presence"] is not None
            ]
            data = {"people": people, "positions": positions, "presence_records": recorded_presence}
            rows.append(
                {
                    "minute": minute,
                    "exact": True,
                    "time_semantics": "before_tick",
                    "run_id": run_id,
                    "branch_id": branch_id,
                    "state_hash": None,
                    "projection_hash": digest(data),
                    "integrity_scope": "selected_records_only",
                    **data,
                    "absent": not people,
                }
            )
        result["items"] = rows
        result["effective_limit"] = min(query.limit, 20)
        if query.kind == "households":
            result["membership_basis"] = "presence_else_current_context_else_initial_profile"
        # A concurrent append must not silently combine two head versions.
        latest = self.store.branch_reference(project_id, run_id, branch_id)
        if latest.checkpoint_hash != reference.checkpoint_hash:
            raise StorageError("history_changed_during_query: retry")
        result.update(
            {
                "run_id": run_id,
                "branch_id": branch_id,
                "computed_until": reference.computed_until,
                "kind": query.kind,
                "coverage": "exact_checkpoint_replay_for_requested_sample_minutes",
                "completeness": "complete_for_requested_samples",
                "completeness_reason": (
                    "Each returned minute is reconstructed exactly; this sampled response does "
                    "not claim every intervening event or lifetime record."
                ),
                "oldest_minute": requested_minutes[0] if requested_minutes else None,
                "newest_minute": requested_minutes[-1] if requested_minutes else None,
                "retention_limit": None,
                "retention_limit_reason": (
                    "No sample retention cap applies; the query is bounded by pagination and the "
                    "computed Run range."
                ),
                "requested_range": [query.start, query.end],
                "complete_lifetime_history": False,
            }
        )
        return result

    @staticmethod
    def _mentions(value: Any, identity: str) -> bool:
        from collections.abc import Mapping

        if isinstance(value, Mapping):
            return any(ProjectQueries._mentions(item, identity) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(ProjectQueries._mentions(item, identity) for item in value)
        return value == identity

    def compare(self, project_id: str, query: ComparisonQuery) -> dict[str, Any]:
        keys = [(target.run_id, target.branch_id) for target in query.targets]
        if len(keys) != len(set(keys)):
            raise StorageError("comparison_targets_must_be_distinct")
        frames, metadata = [], []
        for target in query.targets:
            run = self.store.run(project_id, target.run_id)
            scenario = self.store.scenario(project_id, run["scenario_id"], run["scenario_version"])
            frame = TimelineStore(self.store, project_id, target.run_id).frame(
                target.branch_id, query.minute
            )
            frames.append(frame)
            inputs = json.loads(scenario.canonical_json())
            initial = {
                key: inputs.get(key)
                for key in (
                    "profiles",
                    "zones",
                    "zone_edges",
                    "facilities",
                    "staff",
                    "initial_stock",
                    "scheduled_events",
                    "duration_minutes",
                )
            }
            initial["spatial_population_inputs"] = {
                key: scenario.model_configs.get(key)
                for key in (
                    "facility_geometry",
                    "spatial_constraints",
                    "population",
                    "population_context",
                    "layout",
                    "layout_routing",
                )
            }
            fingerprint = digest(initial)
            metadata.append(
                {
                    "run_id": target.run_id,
                    "branch_id": target.branch_id,
                    "seed": run["seed"],
                    "model_versions": run["model_versions"],
                    "initial_conditions_hash": fingerprint,
                    "configuration_hash": digest(scenario.model_configs),
                    "scenario_hash": run["scenario_hash"],
                }
            )
        baseline = metadata[0]
        compatibility = [
            {
                "target": item,
                "same_seed": item["seed"] == baseline["seed"],
                "same_models": item["model_versions"] == baseline["model_versions"],
                "same_configuration": item["configuration_hash"] == baseline["configuration_hash"],
                "same_initial_conditions": item["initial_conditions_hash"]
                == baseline["initial_conditions_hash"],
            }
            for item in metadata[1:]
        ]
        return {
            "schema_version": 1,
            "minute": query.minute,
            "frames": frames,
            "metadata": metadata,
            "compatibility": compatibility,
            "interpretation": (
                "Descriptive model comparison; seed equality alone does not establish pairing or "
                "real-world causation."
            ),
        }

    def first_divergence(self, project: str, query: Any) -> dict[str, Any]:
        from sheltersim.contracts.workspace import HistoryQuery
        from sheltersim.storage.history_index import HistoryIndex, cursor_decode, cursor_encode

        refs = [self.store.branch_reference(project, t.run_id, t.branch_id) for t in query.targets]
        if any(
            not ref.fork_minute <= query.start <= query.end <= ref.computed_until for ref in refs
        ):
            raise StorageError("divergence_outside_common_computed_range")
        anchors = [ref.checkpoint_hash for ref in refs]
        anchor = digest(anchors)
        scope = digest(
            {"project": project, **query.model_dump(mode="json", exclude={"cursor", "scan_limit"})}
        )
        from sheltersim.storage.query_cursors import seal, unseal

        after = (
            cursor_decode(unseal(self.store, query.cursor), scope, anchor) if query.cursor else None
        )
        prior_gap = bool(after and after[1] == "gap")
        if after and (after[1] not in {"", "gap"} or not query.start <= after[0] < query.end):
            raise StorageError("invalid_scan_progress_cursor")
        start = after[0] + 1 if after else query.start
        limit = min(query.scan_limit, 20) if query.mode != "kpis" else query.scan_limit
        end = min(query.end, start + limit - 1)
        if start > query.end:
            raise StorageError("divergence_cursor_outside_range")
        streams = []
        if query.mode == "kpis":
            for target, ref in zip(query.targets, refs):
                rows = HistoryIndex(self.store, project, target.run_id).search(
                    target.branch_id,
                    HistoryQuery(kind="kpis", start=start, end=end, limit=100),
                    ref.checkpoint_hash,
                )["items"]
                streams.append({row["minute_index"]: row for row in rows})
        else:
            streams = [
                iter(
                    TimelineStore(self.store, project, t.run_id).iter_worlds(
                        t.branch_id, start, end
                    )
                )
                for t in query.targets
            ]
        found = None
        missing = []
        for minute in range(start, end + 1):
            samples = []
            for index, stream in enumerate(streams):
                if query.mode == "kpis":
                    row = stream.get(minute)
                    if row is None:
                        missing.append({"target_index": index, "minute": minute})
                        samples.append(None)
                        continue
                    keys = query.metrics or tuple(
                        key
                        for key, value in row.items()
                        if type(value) in (int, float) and key != "minute_index"
                    )
                    if any(key not in row for key in keys):
                        raise StorageError("divergence_metric_not_recorded")
                    samples.append({key: row[key] for key in keys})
                else:
                    world = next(stream)
                    if query.mode == "world":
                        samples.append(world.to_dict())
                    else:
                        from sheltersim.inspection.frames import person_projection

                        samples.append(person_projection(world, query.agent_id))
            if any(sample is None for sample in samples):
                continue
            hashes = [digest(sample) for sample in samples]
            if len(set(hashes)) > 1:
                found = {
                    "minute": minute,
                    "sample_hashes": hashes,
                    "baseline_sample": samples[0] if query.mode != "world" else None,
                    "candidate_samples": samples[1:] if query.mode != "world" else None,
                }
                break
        if any(
            self.store.branch_reference(project, t.run_id, t.branch_id).checkpoint_hash != key
            for t, key in zip(query.targets, anchors)
        ):
            raise StorageError("history_changed_during_query")
        complete = found is not None or end == query.end
        return {
            "schema_version": 1,
            "mode": query.mode,
            "first_divergence": found,
            "earliest_in_requested_interval": found is not None and not missing and not prior_gap,
            "scanned_range": [query.start, found["minute"] if found else end],
            "page_scanned_range": [start, found["minute"] if found else end],
            "missing_samples": missing,
            "earlier_pages_have_missing_samples": prior_gap,
            "complete": complete,
            "anchor_hash": anchor,
            "next_cursor": None
            if complete
            else seal(
                self.store,
                cursor_encode(
                    {
                        "scope": scope,
                        "anchor": anchor,
                        "minute": end,
                        "key": "gap" if missing or prior_gap else "",
                    }
                ),
            ),
            "time_semantics": "end_of_tick" if query.mode == "kpis" else "before_tick",
            "limits": (
                "Exact at recorded resolution and requested scope. Seed/model/input differences "
                "are descriptive; this is not empirical causality."
            ),
        }
