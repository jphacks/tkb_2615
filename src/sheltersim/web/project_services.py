"""Coordinator-owned shared Project routes, independent of model/UI implementations."""

from __future__ import annotations

from typing import Any

from sheltersim.contracts.workspace import (
    AssetBegin,
    AssetChunk,
    ComparisonQuery,
    DatasetInput,
    DraftInput,
    HistoryQuery,
)
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.evaluations import EvaluationStore
from sheltersim.storage.projects import StorageError, digest
from sheltersim.storage.usage import UsageLedger
from sheltersim.web.project_queries import ProjectQueries, page


def one_query(query: dict[str, list[str]], allowed: set[str]) -> dict[str, str]:
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise StorageError("invalid_query_fields")
    return {key: values[0] for key, values in query.items()}


class ProjectServices:
    def __init__(self, api: Any) -> None:
        self.api, self.store = api, api.store
        self.artifacts = ArtifactStore(self.store)
        self.queries = ProjectQueries(self.store)
        self.evaluations = EvaluationStore(self.store)
        from sheltersim.web.plan_services import PlanServices

        self.plans = PlanServices(self.store)
        from sheltersim.storage.experiments import ExperimentStore

        self.experiments = ExperimentStore(self.store)

    def get(self, parts: list[str], query: dict[str, list[str]]) -> dict[str, Any] | None:
        if not parts:
            return None
        project_id = parts[0]
        if len(parts) == 2 and parts[1] == "contracts" and not query:
            self.store.project(project_id)
            from sheltersim.contracts.evidence import operator_evidence_catalog
            from sheltersim.contracts.extensions import FacilityServicePoint as GeometryServicePoint
            from sheltersim.contracts.operations import (
                ActionLifecycleReceipt,
                CareTransitionReceipt,
                EquipmentTransferParameters,
                FacilityRecoveryReceipt,
                FacilityRepairParameters,
                OperationalDemandSnapshot,
                SharedOperationsConfig,
                SharedOperationsState,
                StaffHandoffParameters,
                StaffHandoffReceipt,
                StaffSupportContinuityReceipt,
                TemporaryRoomGeometry,
            )
            from sheltersim.contracts.policies import model_catalog
            from sheltersim.contracts.population import (
                HouseholdCorrectionRequest,
                PopulationContext,
            )
            from sheltersim.contracts.presence import PresenceState, PresenceTransition
            from sheltersim.contracts.runtime import ModelStateProposal, state_catalog
            from sheltersim.contracts.spatial import PopulationTransition, SpatialConstraints
            from sheltersim.contracts.workspace import DivergenceQuery, StateDescriptor
            from sheltersim.domain.simulator_state import simulator_state_catalog
            from sheltersim.events.effects import available_primitives
            from sheltersim.region.plan_import import ManualPlanTrace, PlanSourceProvenance
            from sheltersim.storage.experiments import ExperimentInput, ObservationTarget

            return {
                "schema_version": 1,
                "history_query": HistoryQuery.model_json_schema(),
                "asset_begin": AssetBegin.model_json_schema(),
                "asset_chunk": AssetChunk.model_json_schema(),
                "draft_input": DraftInput.model_json_schema(),
                "dataset_input": DatasetInput.model_json_schema(),
                "comparison_query": ComparisonQuery.model_json_schema(),
                "spatial_constraints": SpatialConstraints.model_json_schema(),
                "population_transition": PopulationTransition.model_json_schema(),
                "population_context": PopulationContext.model_json_schema(),
                "divergence_query": DivergenceQuery.model_json_schema(),
                "experiment_input": ExperimentInput.model_json_schema(),
                "observation_target": ObservationTarget.model_json_schema(),
                "presence_state": PresenceState.model_json_schema(),
                "presence_transition": PresenceTransition.model_json_schema(),
                "state_descriptor": StateDescriptor.model_json_schema(),
                "model_state_proposal": ModelStateProposal.model_json_schema(),
                "states": state_catalog(),
                "simulator_states": simulator_state_catalog(),
                "operator_evidence": operator_evidence_catalog(),
                "models": model_catalog(),
                "policy_registry_schema_versions": [1, 2, 3],
                "policy_models": model_catalog(),
                "household_correction": HouseholdCorrectionRequest.model_json_schema(),
                "facility_service_point": GeometryServicePoint.model_json_schema(),
                "manual_plan_trace": ManualPlanTrace.model_json_schema(),
                "plan_source_provenance": PlanSourceProvenance.model_json_schema(),
                "shared_operations_config": SharedOperationsConfig.model_json_schema(),
                "shared_operations_state": SharedOperationsState.model_json_schema(),
                "action_lifecycle_receipt": ActionLifecycleReceipt.model_json_schema(),
                "care_transition_receipt": CareTransitionReceipt.model_json_schema(),
                (
                    "staff_support_continuity_receipt"
                ): StaffSupportContinuityReceipt.model_json_schema(),
                "staff_handoff_receipt": StaffHandoffReceipt.model_json_schema(),
                "facility_recovery_receipt": FacilityRecoveryReceipt.model_json_schema(),
                "operational_demand_snapshot": OperationalDemandSnapshot.model_json_schema(),
                "executable_effect_primitives": available_primitives(),
                "typed_effect_contracts_only": {
                    "execution_status": "schemas_with_registered_effect_program_handlers",
                    "schemas": {
                        "temporary_room_geometry": TemporaryRoomGeometry.model_json_schema(),
                        "equipment_transfer": EquipmentTransferParameters.model_json_schema(),
                        "staff_handoff": StaffHandoffParameters.model_json_schema(),
                        "facility_repair": FacilityRepairParameters.model_json_schema(),
                    },
                    "temporary_room_beds": {
                        "policy_version": "exclusive-physical-sleep-beds-v1",
                        "enabled_by": "geometry_overlay.schema_version=2, 3 or 4",
                        "route_scope": "same_zone_in_room_route",
                    },
                    "temporary_room_intake": {
                        "policy_version": "physical-temporary-room-intake-v1",
                        "enabled_by": "geometry_overlay.schema_version=3 or 4",
                        "arrival_parameters": {
                            "admission_zone_ids": "explicit ordered fallback zones",
                            "approach_route_id": (
                                "optional FacilityGeometry.arrival_approach_routes ID ending at "
                                "the preferred zone"
                            ),
                        },
                        "capacity": (
                            "active room people_capacity; assignments persist through expiry"
                        ),
                        "arrival_movement": (
                            "optional operator/source-supplied route with explicit metric "
                            "coordinates and walking speed, checkpointed progress, entrance "
                            "waiting, and admission history; route-free arrivals retain direct "
                            "admission"
                        ),
                    },
                },
                "time_semantics": {
                    "operation_receipts": (
                        "event realized_minute; transition occurs at this boundary"
                    ),
                    "timeline_frames": "before_tick",
                    "operational_demand": "before_tick observed state only",
                },
                "errors": {
                    "household_preview": [
                        "stale_preview_hash",
                        "unknown_synthetic_agent_id",
                        "cross_household_relation",
                        "invalid_guardian_graph",
                        "non_synthetic_identity",
                        "age_unavailable",
                    ],
                    "operation_receipts": [
                        "contract_not_enabled",
                        "unapplied_or_unmatched_event",
                        "event_payload_mismatch",
                        "identity_or_transition_mismatch",
                        "unobserved_state_result",
                    ],
                    "service_point": [
                        "geometry_schema_invalid",
                        "service_point_unknown_facility",
                        "service_point_outside_zone",
                        "service_point_in_obstacle",
                        "service_point_on_wall",
                        "service_point_outside_equipment",
                        "service_point_in_other_equipment",
                        "service_point_access_route_missing",
                        "service_point_capacity_exceeds_facility",
                    ],
                    "plan_geometry": [
                        "plan_geometry_revision_stale_base",
                        "plan_geometry_revision_asset_mismatch",
                        "plan_raster_preview_context_mismatch",
                        "plan_raster_preview_trace_calibration_mismatch",
                        "plan_geometry_raster_context_invalid",
                        "plan_geometry_raster_provenance_missing",
                        "plan_geometry_raster_provenance_mismatch",
                        "plan_geometry_raster_asset_binding_mismatch",
                        "plan_raster_preview_missing",
                        "plan_raster_preview_reference_conflict",
                        "plan_raster_provenance_missing",
                        "plan_raster_provenance_mismatch",
                        "plan_trace_page_reference_mismatch",
                        "invalid_plan_trace_page_index",
                    ],
                },
            }
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "evidence" and not query:
            from sheltersim.contracts.runtime import state_catalog
            from sheltersim.experiments.validation import evidence_context

            run = self.store.run(project_id, parts[2])
            scenario = self.store.scenario(project_id, run["scenario_id"], run["scenario_version"])
            result = evidence_context(scenario)
            from sheltersim.domain.simulator_state import simulator_state_catalog

            result["state_contracts"] = state_catalog()
            result["simulator_states"] = simulator_state_catalog()
            from sheltersim.contracts.evidence import operator_evidence_catalog

            result["operator_evidence"] = operator_evidence_catalog()
            from sheltersim.policies.agents_social_manifest import agents_social_model_catalog

            result["agents_social_operators"] = agents_social_model_catalog()
            paths = sorted(self.store._path(project_id, "datasets").glob("*.json"))
            result["project_datasets"] = [self.store._read(path) for path in paths[:100]]
            result["project_datasets_truncated"] = len(paths) > 100
            return result
        if len(parts) == 2 and parts[1] in {
            "drafts",
            "assets",
            "evaluations",
            "datasets",
            "usage",
            "geometries",
        }:
            values = one_query(query, {"limit", "cursor"})
            limit = int(values.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise StorageError("invalid_page_limit")
            self.store._read(self.store._path(project_id, "project.json"))
            kind = parts[1]
            if kind == "drafts":
                rows = self.artifacts.drafts(project_id)
            elif kind == "assets":
                rows = [
                    self.store._read(p)
                    for p in sorted(self.store._path(project_id, "assets").glob("*/asset.json"))
                ]
            else:
                rows = [
                    self.store._read(p)
                    for p in sorted(self.store._path(project_id, kind).glob("*.json"))
                ]
                if kind == "usage":
                    rows = [
                        UsageLedger(self.store, project_id, row["account_id"]).summary()
                        for row in rows
                    ]
                if kind == "evaluations":
                    rows = [
                        {k: v for k, v in row.items() if k not in {"runs", "comparisons"}}
                        for row in rows
                    ]
            result = page(
                rows,
                {"project_id": project_id, "kind": kind},
                digest(rows),
                limit=limit,
                cursor=values.get("cursor"),
            )
            if kind == "usage":
                result["project_totals"] = {
                    key: sum(row["totals"][key] for row in rows)
                    for key in ("calls", "input_tokens", "output_tokens", "cost_nusd")
                }
            return result
        if len(parts) == 2 and parts[1] == "experiments":
            values = one_query(query, {"limit", "cursor"})
            limit = int(values.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise StorageError("invalid_page_limit")
            rows = [
                self.store._read(path)
                for path in sorted(
                    self.store._path(project_id, "experiments").glob("*/experiment.json")
                )
            ]
            return page(
                rows,
                {"project": project_id, "kind": "experiments"},
                digest(rows),
                limit=limit,
                cursor=values.get("cursor"),
            )
        if len(parts) == 3 and parts[1] == "experiments" and not query:
            return self.experiments.plan(project_id, parts[2])
        if (
            len(parts) == 4
            and parts[1] == "experiments"
            and parts[3] in {"cells", "results", "fit"}
        ):
            record = self.experiments.plan(project_id, parts[2])
            if parts[3] == "fit" and not query:
                return self.store._read(self.experiments._path(project_id, parts[2], "fit.json"))
            values = one_query(query, {"limit", "cursor"})
            limit = int(values.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise StorageError("invalid_page_limit")
            generation_path = self.experiments._path(project_id, parts[2], "result-generation.json")
            generation = (
                self.store._read(generation_path)["generation"] if generation_path.exists() else 0
            )
            anchor = digest(
                {
                    "plan": record["plan_hash"],
                    "generation": generation if parts[3] == "results" else 0,
                }
            )
            result = page(
                record["cells"],
                {"project": project_id, "experiment": parts[2], "section": parts[3]},
                anchor,
                limit=limit,
                cursor=values.get("cursor"),
            )
            if parts[3] == "results":
                rows = []
                for cell in result["items"]:
                    path = self.experiments._path(
                        project_id, parts[2], "results", cell["cell_id"] + ".json"
                    )
                    rows.append(
                        self.store._read(path)
                        if path.exists()
                        else {"cell_id": cell["cell_id"], "status": "not_collected"}
                    )
                result["items"] = rows
            return result
        if len(parts) == 3 and parts[1] == "geometries" and not query:
            return self.plans.geometry(project_id, parts[2])
        if len(parts) == 3 and parts[1] == "drafts" and not query:
            return self.artifacts.draft(project_id, parts[2])
        if len(parts) == 3 and parts[1] == "uploads" and not query:
            return self.store._read(
                self.store._path(project_id, "uploads", parts[2], "upload.json")
            )
        if len(parts) in {3, 4} and parts[1] == "assets" and not query:
            if len(parts) == 4 and parts[3] != "content":
                raise StorageError("unknown_asset_operation")
            return self.artifacts.asset(project_id, parts[2], include_content=len(parts) == 4)
        if len(parts) == 3 and parts[1] in {"evaluations", "datasets"}:
            record = self.store._read(self.store._path(project_id, parts[1], f"{parts[2]}.json"))
            if parts[1] == "datasets" and not query:
                return record
            values = one_query(query, {"section", "limit", "cursor"})
            section = values.get("section")
            if section is None:
                return {k: v for k, v in record.items() if k not in {"runs", "comparisons"}}
            if section not in {"runs", "comparisons"}:
                raise StorageError("invalid_evaluation_section")
            limit = int(values.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise StorageError("invalid_page_limit")
            return page(
                record[section],
                {"project_id": project_id, "evaluation": parts[2], "section": section},
                digest(record),
                limit=limit,
                cursor=values.get("cursor"),
            )
        if (
            len(parts) == 6
            and parts[1] == "runs"
            and parts[3] == "checkpoints"
            and parts[5] == "section"
        ):
            from sheltersim.storage.checkpoints import CheckpointArchive

            values = one_query(query, {"kind", "offset", "limit"})
            paths = {"events": ("world_state", "event_log"), "kpis": ("metrics_state", "snapshots")}
            if values.get("kind") not in paths:
                raise StorageError("unsupported_checkpoint_section")
            path = self.store._path(project_id, "runs", parts[2], "checkpoints", f"{parts[4]}.json")
            record = self.store._read(path)
            if record.get("checkpoint_hash") != parts[4]:
                raise StorageError("checkpoint_pointer_corrupt")
            return {
                "schema_version": 1,
                **CheckpointArchive(path.parent).list_page(
                    record,
                    paths[values["kind"]],
                    offset=int(values.get("offset", "0")),
                    limit=int(values.get("limit", "100")),
                ),
            }
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "audit":
            values = one_query(
                query,
                {
                    "kind",
                    "limit",
                    "cursor",
                    "branch_id",
                    "operation",
                    "stage",
                    "model_id",
                    "status",
                },
            )
            limit = int(values.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise StorageError("invalid_page_limit")
            kind = values.get("kind", "approvals")
            run = self.store.run(project_id, parts[2])
            filters = {
                key: value
                for key, value in values.items()
                if key in {"branch_id", "operation", "stage", "model_id", "status"}
            }
            if kind == "usage":
                binding = self.store._path(project_id, "runs", parts[2], "usage.json")
                if not binding.exists():
                    return {
                        "schema_version": 1,
                        "items": [],
                        "next_cursor": None,
                        "coverage": "legacy_account_not_initialized",
                        "unrecorded_spend": "unknown",
                    }
                ledger = UsageLedger(
                    self.store, project_id, self.store._read(binding)["account_id"]
                )
                rows, anchor = ledger.receipt_records(filters)
            elif kind == "branches":
                if filters:
                    raise StorageError("audit_filters_apply_only_to_usage")
                rows = [{"record_kind": "branch_lineage", **row} for row in run["branches"]]
                anchor = digest(rows)
            elif kind == "approvals":
                if filters:
                    raise StorageError("audit_filters_apply_only_to_usage")
                source = self.store._read(
                    self.store._path(
                        project_id,
                        "scenarios",
                        run["scenario_id"],
                        f"v{run['scenario_version']}.json",
                    )
                )
                if digest(source["scenario"]) != source["scenario_hash"]:
                    raise StorageError("saved scenario integrity mismatch")
                rows = [
                    {
                        "record_kind": "approved_scenario_binding",
                        "scenario_id": run["scenario_id"],
                        "scenario_version": run["scenario_version"],
                        "scenario_hash": source["scenario_hash"],
                        "approved": source["approved"],
                        "seed": run["seed"],
                        "run_id": run["run_id"],
                        "approval_time": None,
                        "approval_time_status": "not_recorded",
                        "model_versions": run["model_versions"],
                    }
                ]
                anchor = digest(rows)
            else:
                raise StorageError("unsupported_run_audit_kind")
            result = page(
                rows,
                {"project": project_id, "run": parts[2], "kind": kind, **filters},
                anchor,
                limit=limit,
                cursor=values.get("cursor"),
            )
            result.update(
                {
                    "coverage": "persisted_records_only",
                    "verified": False,
                    "ordering": (
                        "saved_sequence_then_receipt_id; legacy request order may be unavailable"
                    )
                    if kind == "usage"
                    else "stored_reference_order",
                    "limits": (
                        "No raw prompts/responses, provider keys, host ownership records or "
                        "invented approval dates."
                    ),
                }
            )
            return result
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "index":
            from sheltersim.storage.history_index import HistoryIndex

            values = one_query(query, {"branch_id"})
            branch = values.get("branch_id", "main")
            reference = self.store.branch_reference(project_id, parts[2], branch)
            index = HistoryIndex(self.store, project_id, parts[2])
            status = "required"
            indexed = None
            if index.path.exists():
                with index.connect() as db:
                    row = db.execute(
                        "SELECT hash,minute FROM heads WHERE branch=?", (branch,)
                    ).fetchone()
                    if row:
                        indexed = {"checkpoint_hash": row["hash"], "computed_until": row["minute"]}
                        status = "ready" if row["hash"] == reference.checkpoint_hash else "stale"
            return {
                "schema_version": 1,
                "status": status,
                "branch_id": branch,
                "source_checkpoint_hash": reference.checkpoint_hash,
                "indexed": indexed,
                "rebuild_operation": "POST same route with {branch_id}",
                "verified": False,
            }
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "checkpoints":
            from sheltersim.storage.history_index import HistoryIndex

            values = one_query(query, {"branch_id", "start", "end", "limit", "cursor"})
            branch = values.get("branch_id", "main")
            reference = self.store.branch_reference(project_id, parts[2], branch)
            return HistoryIndex(self.store, project_id, parts[2]).checkpoints(
                branch,
                int(values.get("start", str(reference.fork_minute))),
                int(values.get("end", str(reference.computed_until))),
                int(values.get("limit", "25")),
                values.get("cursor"),
            )
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "usage" and not query:
            self.store.run(project_id, parts[2])
            path = self.store._path(project_id, "runs", parts[2], "usage.json")
            if not path.exists():
                return {"schema_version": 1, "status": "legacy_account_not_initialized"}
            return UsageLedger(
                self.store, project_id, self.store._read(path)["account_id"]
            ).summary()
        if (
            len(parts) == 6
            and parts[1] == "runs"
            and parts[3] == "branches"
            and parts[5] == "compatibility"
            and not query
        ):
            from sheltersim.contracts.policies import model_signatures

            reference = self.store.branch_reference(project_id, parts[2], parts[4])
            saved = self.store.checkpoint_fields(
                project_id, parts[2], reference.checkpoint_hash, ("model_versions", "runtime_state")
            )
            run = self.store.run(project_id, parts[2])
            installed = {}
            try:
                scenario = self.store.scenario(
                    project_id, run["scenario_id"], run["scenario_version"]
                )
                installed = model_signatures(scenario.model_configs)
                # Checkpoint includes the controller/interpreter versions as well.
                checkpoint = self.store.load_checkpoint(
                    project_id, parts[2], reference.checkpoint_hash
                )
                checkpoint.restore_world()
                differences = []
                compatible = True
                limitation = None
            except (ValueError, TypeError, KeyError):
                differences = [
                    {
                        "component": key,
                        "recorded": saved["model_versions"].get(key),
                        "installed": installed.get(key),
                    }
                    for key in sorted(set(saved["model_versions"]) | set(installed))
                    if (
                        key.startswith(("policy:", "state:", "simulator:", "operator:"))
                        or key == "model_suite"
                    )
                    if saved["model_versions"].get(key) != installed.get(key)
                ]
                compatible = False
                limitation = (
                    "Installed schema/model validation requires an explicit migration/new Run; "
                    "original checkpoint is retained."
                )
            settings = saved["runtime_state"].get("llm_settings", {})
            return {
                "schema_version": 1,
                "continuation_model_state_compatible": compatible,
                "checkpoint_hash": reference.checkpoint_hash,
                "recorded_versions": saved["model_versions"],
                "installed_policy_state_versions": installed,
                "differences": differences,
                "limitation": limitation,
                "saved_router_settings": settings,
                "live_configuration_still_required": saved["runtime_state"]
                .get("llm", {})
                .get("mode")
                not in (None, "offline"),
                "verified": False,
                "side_effects": "none; no provider and no migration executed",
            }
        if (
            len(parts) == 6
            and parts[1] == "runs"
            and parts[3] == "branches"
            and parts[5] == "history"
        ):
            values: dict[str, Any] = one_query(
                query, set(HistoryQuery.model_fields) - {"schema_version"}
            )
            for name in ("start", "end", "step", "limit"):
                if name in values:
                    values[name] = int(values[name])
            return self.queries.history(
                project_id, parts[2], parts[4], HistoryQuery.model_validate(values)
            )
        return None

    def post(self, parts: list[str], body: dict[str, Any]) -> dict[str, Any] | None:
        from sheltersim.web.projects import fields

        if len(parts) < 2:
            return None
        project_id = parts[0]
        if len(parts) == 4 and parts[1] in {"drafts", "assets"} and parts[3] == "delete":
            fields(body, {"confirmation"})
            return self.artifacts.delete(
                project_id, parts[1], parts[2], confirmation=body["confirmation"]
            )
        if len(parts) == 3 and parts[1] == "experiments":
            return self.experiments.create(project_id, parts[2], body)
        if len(parts) == 6 and parts[1] == "experiments" and parts[3] == "cells":
            if parts[5] == "prepare":
                return self.experiments.prepare(self.api, project_id, parts[2], parts[4], body)
            if parts[5] == "collect":
                fields(body, set())
                return self.experiments.collect(project_id, parts[2], parts[4])
        if len(parts) == 4 and parts[1] == "experiments":
            if parts[3] == "finalize":
                fields(body, {"evaluation_id"})
                return self.experiments.finalize(project_id, parts[2], body["evaluation_id"])
            if parts[3] == "fit":
                fields(body, set())
                return self.experiments.fit(project_id, parts[2])
        if len(parts) == 4 and parts[1] == "assets" and parts[3] == "plan-preview":
            return self.plans.preview(project_id, parts[2], body)
        if len(parts) == 3 and parts[1] == "geometries":
            return self.plans.approve(project_id, parts[2], body)
        if len(parts) == 3 and parts[1] == "drafts":
            return self.artifacts.save_draft(project_id, parts[2], DraftInput.model_validate(body))
        if len(parts) == 3 and parts[1] == "uploads":
            return self.artifacts.begin(project_id, parts[2], AssetBegin.model_validate(body))
        if len(parts) == 4 and parts[1] == "uploads":
            if parts[3] == "chunks":
                return self.artifacts.chunk(project_id, parts[2], AssetChunk.model_validate(body))
            if parts[3] in {"finish", "abort"}:
                fields(body, set())
                return (
                    self.artifacts.finish(project_id, parts[2])
                    if parts[3] == "finish"
                    else self.artifacts.delete_upload(project_id, parts[2])
                )
        if len(parts) == 2 and parts[1] == "first-divergence":
            from sheltersim.contracts.workspace import DivergenceQuery

            return self.queries.first_divergence(project_id, DivergenceQuery.model_validate(body))
        if len(parts) == 2 and parts[1] == "comparison-query":
            return self.queries.compare(project_id, ComparisonQuery.model_validate(body))
        if len(parts) == 2 and parts[1] == "condition-differences":
            from sheltersim.contracts.workspace import ComparisonDifferenceQuery
            from sheltersim.web.comparison_evidence import condition_differences

            return condition_differences(
                self.store, project_id, ComparisonDifferenceQuery.model_validate(body)
            )
        if len(parts) == 3 and parts[1] == "evaluations":
            fields(body, {"report", "approved_hash"})
            return self.evaluations.import_report(
                project_id, parts[2], body["report"], body["approved_hash"]
            )
        if len(parts) == 3 and parts[1] == "datasets":
            return self.evaluations.dataset(project_id, parts[2], body)
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "index":
            fields(body, {"branch_id"})
            from sheltersim.storage.history_index import HistoryIndex

            return HistoryIndex(self.store, project_id, parts[2]).rebuild(body["branch_id"])
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "rerun":
            fields(body, {"new_run_id"}, {"scenario", "approved_hash", "approved"})
            source = self.store.run(project_id, parts[2])
            scenario_id, version = source["scenario_id"], source["scenario_version"]
            if "scenario" in body:
                if body.get("approved") is not True:
                    raise StorageError("edited_scenario_requires_approval")
                from sheltersim.experiments.scenario import ScenarioSpec

                record = self.store.save_scenario(
                    project_id,
                    ScenarioSpec.model_validate(body["scenario"]),
                    approved_hash=body.get("approved_hash"),
                )
                scenario_id, version = record["scenario_id"], record["scenario_version"]
            return self.api.post(
                [project_id, "runs"],
                {
                    "run_id": body["new_run_id"],
                    "scenario_id": scenario_id,
                    "scenario_version": version,
                },
            )
        return None
