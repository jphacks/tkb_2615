"""Application wiring for generated scenarios, exact branches, and analysis."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from typing import Any

from sheltersim.contracts.workspace import DraftInput
from sheltersim.events.effects import EffectProgram, validate_effect_program
from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.accounting import StoredResponseTape, accounted_router
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.natural_language import (
    InterventionCompilerUnavailable,
    compile_natural_language,
    scenario_summary_from_world,
)
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.region_prepare import parse_scenario_text
from sheltersim.llm.report import (
    build_review_evidence,
    generate_evidence_review,
    render_review_markdown,
    scenario_assumption_ids,
)
from sheltersim.region.models import RegionResearchRequest
from sheltersim.region.population import (
    age_bands_from_observations,
    household_mix_from_observations,
)
from sheltersim.region.research import RegionResearchService
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.projects import StorageError, digest, safe_document


class ProjectWorkspace:
    def __init__(self, api: Any) -> None:
        self.api, self.store = api, api.store
        self.research = RegionResearchService()
        self.drafts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.artifacts = ArtifactStore(self.store)
        self._lock = threading.RLock()

    def _cache(self, value: dict[str, Any]) -> str:
        key = uuid.uuid4().hex
        content = {
            k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v
            for k, v in value.items()
        }
        self.artifacts.save_draft(
            value["project_id"], key, DraftInput(kind=value["kind"], content=content)
        )
        self.drafts[key] = value
        while len(self.drafts) > 20:
            self.drafts.popitem(last=False)
        return key

    def _preview(self, project_id: str, preview_id: str) -> dict[str, Any]:
        content = self.artifacts.draft(project_id, preview_id)["content"]
        if content["project_id"] != project_id:
            raise StorageError("draft belongs to another Project")
        if content["kind"] == "scenario":
            from sheltersim.experiments.scenario import ScenarioSpec

            content["scenario"] = ScenarioSpec.model_validate(content["scenario"])
        elif content["kind"] == "intervention":
            content["program"] = EffectProgram.model_validate(content["program"])
        return content

    def analysis_router(
        self,
        project_id: str,
        run_id: str,
        *,
        operation: str = "questions",
        branch_id: str | None = None,
        settings: LLMSettings | None = None,
    ) -> LLMRouter:
        tape = StoredResponseTape(
            self.store, self.store._path(project_id, "runs", run_id, "analysis-tape.json")
        )
        return accounted_router(
            self.store,
            project_id,
            run_id=run_id,
            operation=operation,
            branch_id=branch_id,
            tape=tape,
            settings=settings,
        )

    def _preparation_account(self, project_id: str, requested: str | None = None) -> str:
        if requested is not None:
            self.store._path(project_id, "usage", requested)
            return requested
        # Old clients need not send draft_id: retries/edits still share one allowance.
        with self.store.transaction():
            path = self.store._path(project_id, "preparation.json")
            pending = self.store._read(path) if path.exists() else None
            if pending and pending["status"] == "pending":
                return pending["account_id"]
            account_id = uuid.uuid4().hex
            self.store._write(
                path, {"schema_version": 1, "account_id": account_id, "status": "pending"}
            )
            return account_id

    def _bind_preparation(self, project_id: str, account_id: str, run_id: str) -> None:
        with self.store.transaction():
            path = self.store._path(project_id, "preparation.json")
            if path.exists():
                pending = self.store._read(path)
                if pending["account_id"] == account_id:
                    self.store._write(path, {**pending, "status": "bound", "run_id": run_id})

    def save_analysis(self, project_id: str, run_id: str, router: LLMRouter) -> None:
        snapshot = router.snapshot_state()
        # Answers and prompts are represented only by sanitized turns and hashes.
        snapshot["response_tape"] = {"schema_version": 1, "entries": {}}
        with self.store.transaction():
            self.store._write(
                self.store._path(project_id, "runs", run_id, "analysis.json"),
                {"schema_version": 1, "router": safe_document(snapshot)},
            )

    def preview(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        fields(
            body,
            {"text"},
            {
                "request",
                "estat_stats_data_id",
                "estat_area_code",
                "estat_period_code",
                "estat_category_codes",
                "selected_shelter_id",
                "plan_override",
                "geometry_id",
                "draft_id",
                "target_date",
                "weather_area_code",
            },
        )
        self.store.project(project_id)
        draft_id = self._preparation_account(project_id, body.get("draft_id"))
        tape = StoredResponseTape(
            self.store, self.store._path(project_id, "usage-tapes", f"{draft_id}.json")
        )
        router = accounted_router(
            self.store, project_id, account_id=draft_id, operation="scenario_prepare", tape=tape
        )
        text = body["text"]
        if not isinstance(text, str):
            raise StorageError("scenario text must be a string")
        if text.strip():
            parsed = parse_scenario_text(text, router=router)
            request = (
                InitialScenarioRequest.model_validate(body["request"])
                if body.get("request")
                else parsed.request
            )
            input_sha256 = parsed.input_sha256
            extraction_source = parsed.source
            parse_warnings = list(parsed.warnings)
            missing_fields = list(parsed.missing_fields)
        elif body.get("request"):
            request = InitialScenarioRequest.model_validate(body["request"])
            structured_input = json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            input_sha256 = hashlib.sha256(structured_input).hexdigest()
            extraction_source = "structured_form"
            parse_warnings = []
            missing_fields = []
        else:
            return {
                "can_start": False,
                "missing_fields": ["municipality", "disaster", "evacuees"],
                "warnings": [],
                "draft_id": draft_id,
            }
        if request is None:
            return {
                "can_start": False,
                "missing_fields": missing_fields,
                "warnings": parse_warnings,
                "draft_id": draft_id,
            }
        research = self.research.research(
            RegionResearchRequest(
                municipality_query=request.municipality,
                municipality_code=request.municipality_code,
                disaster=request.disaster,
                season=request.season,
                selected_municipality_name=request.municipality,
                estat_stats_data_id=body.get("estat_stats_data_id"),
                estat_area_code=body.get("estat_area_code"),
                estat_period_code=body.get("estat_period_code"),
                estat_category_codes=body.get("estat_category_codes", {}),
                target_date=body.get("target_date", request.target_date),
                weather_area_code=body.get("weather_area_code", request.weather_area_code),
            ),
            router=router,
        )
        if body.get("geometry_id") is not None:
            if body.get("plan_override") is not None:
                raise StorageError("select_geometry_or_plan_override")
            body = {
                **body,
                "plan_override": self.api.services.plans.geometry(project_id, body["geometry_id"])[
                    "geometry"
                ],
            }
        plan = None
        if body.get("plan_override") is not None:
            from sheltersim.region.plan_import import ImportedPlanGeometry

            plan = ImportedPlanGeometry.model_validate(body["plan_override"])
            stored = self.artifacts.asset(project_id, plan.asset.sha256)
            if (stored["byte_count"], stored["media_type"]) != (
                plan.asset.byte_count,
                plan.asset.media_type,
            ):
                raise StorageError("plan_asset_manifest_mismatch")
        try:
            draft = InitialScenarioBuilder().build(
                request,
                research=research,
                selected_shelter_id=body.get("selected_shelter_id"),
                plan_override=plan,
                age_bands=age_bands_from_observations(research.population_observations),
                household_mix=household_mix_from_observations(research.population_observations),
            )
        except ValueError as exc:
            if plan is None or isinstance(exc, StorageError):
                raise
            from sheltersim.web.plan_services import plan_rejection_reason

            # Tell the operator what the selected plan lacks, e.g. a sleeping zone (#204).
            raise StorageError(f"scenario_plan_invalid: {plan_rejection_reason(exc)}") from exc
        document = draft.scenario.model_dump(mode="json")
        # A public building's address is not needed in anonymous stored experiments.
        document["provenance"].get("selected_shelter", {}).pop("address", None)
        for shelter in document["provenance"].get("region_research", {}).get("shelters", []):
            shelter.pop("address", None)
        document["provenance"]["scenario_input"] = {
            "sha256": input_sha256,
            "extraction_source": extraction_source,
        }
        from sheltersim.experiments.scenario import ScenarioSpec

        scenario = ScenarioSpec.model_validate(document)
        approval_hash = digest(json.loads(scenario.canonical_json()))
        result = {
            "scenario": json.loads(scenario.canonical_json()),
            "approved_hash": approval_hash,
            "can_start": draft.can_start,
            "approval_blockers": list(draft.approval_blockers),
            "warnings": [*parse_warnings, *draft.warnings],
            "research": research.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "llm_usage": {
                "mode": router.mode.value,
                "api_request_count": router.api_request_count,
                "spent_usd": router.spent_usd,
            },
        }
        result["draft_id"] = draft_id
        research_record = research.model_dump(mode="json")
        for shelter in research_record.get("shelters", []):
            shelter.pop("address", None)
        design_record = (
            draft.facility_design.model_dump(mode="json") if draft.facility_design else None
        )
        result["facility_design"] = design_record
        result["geometry_hash"] = digest(design_record["geometry"]) if design_record else None
        result["preview_id"] = self._cache(
            {
                "usage_account_id": draft_id,
                "kind": "scenario",
                "project_id": project_id,
                "scenario": scenario,
                "can_start": draft.can_start,
                "hash": approval_hash,
                "approval_blockers": list(draft.approval_blockers),
                "facility_design": design_record,
                "request": request.model_dump(mode="json"),
                "research": research_record,
            }
        )
        return result

    def approve(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        fields(body, {"preview_id", "approved_hash", "approved", "run_id"})
        preview = self._preview(project_id, body["preview_id"])
        if not preview or preview["kind"] != "scenario" or preview["project_id"] != project_id:
            raise StorageError("preview expired; generate and review a new preview")
        if (
            body["approved"] is not True
            or body["approved_hash"] != preview["hash"]
            or not preview["can_start"]
        ):
            raise StorageError("approval must match a startable reviewed scenario")
        run_path = self.store._path(project_id, "runs", body["run_id"], "run.json")
        if run_path.exists():
            existing = self.store.run(project_id, body["run_id"])
            if existing["scenario_hash"] != preview["hash"]:
                raise StorageError("run_id_conflict")
            result = self.api.post(
                [project_id, "runs"],
                {
                    "run_id": body["run_id"],
                    "scenario_id": existing["scenario_id"],
                    "scenario_version": existing["scenario_version"],
                    "usage_account_id": preview["usage_account_id"],
                },
            )
            self._bind_preparation(project_id, preview["usage_account_id"], body["run_id"])
            return result
        saved = self.store.save_scenario(
            project_id, preview["scenario"], approved_hash=preview["hash"]
        )
        result = self.api.post(
            [project_id, "runs"],
            {
                "run_id": body["run_id"],
                "scenario_id": saved["scenario_id"],
                "scenario_version": saved["scenario_version"],
                "usage_account_id": preview["usage_account_id"],
            },
        )
        self._bind_preparation(project_id, preview["usage_account_id"], body["run_id"])
        self.drafts.pop(body["preview_id"], None)
        return result

    def correct_household_preview(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a new approval-bound preview from reviewed generated-person links."""
        from sheltersim.contracts.population import (
            HouseholdCorrectionRequest,
            correct_synthetic_household_relationships,
        )
        from sheltersim.web.projects import fields

        fields(body, {"preview_id", "correction"})
        preview = self._preview(project_id, body["preview_id"])
        if (
            not preview
            or preview.get("kind") != "scenario"
            or preview.get("project_id") != project_id
        ):
            raise StorageError("preview expired or does not belong to this Project")
        try:
            request = HouseholdCorrectionRequest.model_validate(body["correction"])
        except ValueError as exc:
            raise StorageError(f"household_correction_invalid: {exc}") from exc
        if request.expected_scenario_hash != preview["hash"]:
            raise StorageError(
                "household_correction_stale_preview: generate a fresh Scenario preview"
            )
        try:
            corrected = correct_synthetic_household_relationships(preview["scenario"], request)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"household_correction_invalid: {exc}") from exc
        updated_hash = digest(json.loads(corrected.canonical_json()))
        # Drafts saved before blockers were recorded cannot show which other
        # blockers still apply, so they are refused rather than read as clear.
        if not isinstance(preview.get("approval_blockers"), list):
            raise StorageError(
                "household_correction_stale_preview: generate a fresh Scenario preview"
            )
        blockers = [
            item
            for item in preview["approval_blockers"]
            if not item.startswith("同居世帯内の年齢・支援情報から保護者を確認できない未成年者が")
        ]
        context = corrected.model_configs.get("population_context", {}).get("people", {})
        population = corrected.model_configs.get("population", {}).get("people", {})
        unaccompanied = []
        for identity, person in context.items():
            age = population.get(identity, {}).get("age_years")
            if (
                type(age) is int
                and age < 18
                and person.get("guardian_status") == "unaccompanied"
                and not person.get("parent_ids")
                and not person.get("caregiver_ids")
            ):
                unaccompanied.append(identity)
        if unaccompanied:
            blockers.append(
                f"同居世帯内の年齢・支援情報から保護者を確認できない未成年者が{len(unaccompanied)}人います。"
                "家族関係を確認するまでシミュレーションを開始できません。"
            )
        corrected_preview_id = self._cache(
            {
                "usage_account_id": preview["usage_account_id"],
                "kind": "scenario",
                "project_id": project_id,
                "scenario": corrected,
                "can_start": not blockers,
                "hash": updated_hash,
                "approval_blockers": blockers,
                "facility_design": preview.get("facility_design"),
                "request": preview.get("request"),
                "research": preview.get("research"),
                "correction_parent_preview_id": body["preview_id"],
            }
        )
        return {
            "schema_version": 1,
            "preview_id": corrected_preview_id,
            "scenario": json.loads(corrected.canonical_json()),
            "approved_hash": updated_hash,
            "can_start": not blockers,
            "approval_blockers": blockers,
            "corrected_agent_ids": [row.agent_id for row in request.corrections],
            "household_review": "operator_reviewed_synthetic_relationships",
            "source_preview_id": body["preview_id"],
            "source_scenario_hash": preview["hash"],
            # The browser applies this response through the same renderer as an
            # ordinary Scenario Preview. Preserve its operator request and facility
            # context so correcting relationships cannot blank the reviewed setup.
            "request": preview.get("request"),
            "facility_design": preview.get("facility_design"),
            "research": preview.get("research"),
        }

    def comparison(
        self, project_id: str, run_id: str, branch_ids: list[str], minute: int
    ) -> dict[str, Any]:
        if not 2 <= len(branch_ids) <= 8 or len(set(branch_ids)) != len(branch_ids):
            raise StorageError("comparison requires 2–8 distinct project branches")
        refs = {b["branch_id"]: b for b in self.store.branches(project_id, run_id)}
        if any(key not in refs for key in branch_ids):
            raise StorageError("comparison branch is missing from this Run")
        lower = max(refs[key]["fork_minute"] for key in branch_ids)
        upper = min(refs[key]["computed_until"] for key in branch_ids)
        if type(minute) is not int or not lower <= minute <= upper:
            raise StorageError("comparison minute is outside the common computed range")
        timeline = TimelineStore(self.store, project_id, run_id)
        frames = [timeline.frame(key, minute) for key in branch_ids]
        receipts = []
        for key in branch_ids:
            # Only the fork receipt is needed; read the head's verified runtime_state
            # instead of decoding and validating each branch's whole history.
            runtime_state = self.store.checkpoint_fields(
                project_id, run_id, refs[key]["checkpoint_hash"], ("runtime_state",)
            )["runtime_state"]
            receipts.append(runtime_state.get("fork_origin"))
        origins = [r for r in receipts if r is not None]
        if origins:
            first = origins[0]
            paired = all(
                (r == first or key == first["branch_id"])
                for key, r in zip(branch_ids, receipts, strict=True)
            )
        else:
            paired = False
        from sheltersim.web.replay import comparison_projection

        return comparison_projection(run_id, minute, [lower, upper], frames, receipts, paired)

    def intervention(
        self, project_id: str, run_id: str, branch_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        fields(body, {"minute"}, {"text", "program"})
        if ("text" in body) == ("program" in body):
            raise StorageError("provide exactly one of text or program")
        runtime = TimelineStore(self.store, project_id, run_id).seek(branch_id, body["minute"])
        if "program" in body:
            try:
                program = EffectProgram.model_validate(body["program"])
            except (TypeError, ValueError) as exc:
                raise StorageError(f"invalid typed EffectProgram: {exc}") from exc
            issues = validate_effect_program(runtime.world, program)
            result = {
                "schema_version": 1,
                "status": "compiled" if not issues else "blocked",
                "support_mode": program.support_mode.value,
                "compiler_provenance": program.compiler_provenance.model_dump(mode="json"),
                "explanation": (
                    "Operator supplied a typed EffectProgram; validation is required before "
                    "approval."
                ),
                "program": program.model_dump(mode="json"),
                "can_apply": not issues,
                "validation_issues": list(issues),
            }
        else:
            compiler_tape = StoredResponseTape(
                self.store, self.store._path(project_id, "runs", run_id, "compiler-tape.json")
            )
            try:
                compiled = compile_natural_language(
                    {
                        "text": body["text"],
                        "branch_id": branch_id,
                        "selected_minute": body["minute"],
                        "scenario_summary": scenario_summary_from_world(runtime.world).model_dump(
                            mode="json"
                        ),
                    },
                    router=accounted_router(
                        self.store,
                        project_id,
                        run_id=run_id,
                        operation="compiler",
                        branch_id=branch_id,
                        tape=compiler_tape,
                    ),
                )
            except InterventionCompilerUnavailable as exc:
                raise StorageError(str(exc), code="intervention_compiler_unavailable") from exc
            result = compiled.model_dump(mode="json")
            program = None
            if compiled.draft_plan is not None and compiled.status == "compiled":
                raw = compiled.draft_plan.model_dump(mode="json")
                program = EffectProgram.model_validate(
                    {k: v for k, v in raw.items() if k in EffectProgram.model_fields}
                )
                issues = validate_effect_program(runtime.world, program)
                result["can_apply"] = not issues
                result["validation_issues"] = list(issues)
                result["program"] = program.model_dump(mode="json")
        if program is not None:
            if "approved_program_hash" not in result:
                result["approved_program_hash"] = digest(program.model_dump(mode="json"))
            if "preview_id" not in result:
                result["preview_id"] = self._cache(
                    {
                        "kind": "intervention",
                        "project_id": project_id,
                        "run_id": run_id,
                        "branch_id": branch_id,
                        "minute": body["minute"],
                        "state_hash": digest(runtime.world.to_dict()),
                        "program": program,
                        "hash": result["approved_program_hash"],
                        "can_apply": result["can_apply"],
                    }
                )
        return result

    def post(self, parts: list[str], body: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if len(parts) == 2 and parts[1] == "facility-preview":
                from sheltersim.web.facility_services import facility_preview

                return facility_preview(self, parts[0], body)
            if len(parts) == 2 and parts[1] == "scenario-preview":
                return self.preview(parts[0], body)
            if len(parts) == 2 and parts[1] == "scenario-household-corrections":
                return self.correct_household_preview(parts[0], body)
            if len(parts) == 2 and parts[1] == "scenario-approve":
                return self.approve(parts[0], body)
            if len(parts) == 6 and parts[1] == "runs" and parts[3] == "branches":
                project_id, run_id, branch_id, action = parts[0], parts[2], parts[4], parts[5]
                if action == "questions":
                    from sheltersim.web.results_chat import ResultsChatService

                    return ResultsChatService(None, None, self.api).ask(
                        {**body, "project_id": project_id, "run_id": run_id, "branch_id": branch_id}
                    )
                if action == "intervention-preview":
                    return self.intervention(project_id, run_id, branch_id, body)
                if action == "intervention-apply":
                    from sheltersim.web.projects import fields

                    fields(body, {"preview_id", "approved_program_hash", "approved", "child_id"})
                    preview = self._preview(project_id, body["preview_id"])
                    if (
                        not preview
                        or preview.get("kind") != "intervention"
                        or (preview["project_id"], preview["run_id"], preview["branch_id"])
                        != (project_id, run_id, branch_id)
                    ):
                        raise StorageError("intervention preview expired or has another scope")
                    if not preview["can_apply"] or body["approved_program_hash"] != preview["hash"]:
                        raise StorageError("intervention approval does not match validated preview")
                    current = TimelineStore(self.store, project_id, run_id).seek(
                        branch_id, preview["minute"]
                    )
                    if digest(current.world.to_dict()) != preview["state_hash"]:
                        raise StorageError("source state changed; preview the intervention again")
                    result = self.api.post(
                        [project_id, "runs", run_id, "branches", branch_id, "fork"],
                        {
                            "minute": preview["minute"],
                            "child_id": body["child_id"],
                            "approved": body["approved"],
                            "program": preview["program"].model_dump(mode="json"),
                            "approved_program_hash": preview["hash"],
                        },
                    )
                    self.drafts.pop(body["preview_id"], None)
                    return result
            return None

    def finish_review(self, project_id: str, run_id: str, branch_id: str, runtime: Any) -> None:
        if runtime.runtime_state.get("execution_purpose") == "offline_experiment":
            with self.store.transaction():
                self.store._write(
                    self.store._path(
                        project_id, "runs", run_id, "branches", branch_id, "report.json"
                    ),
                    {
                        "schema_version": 1,
                        "status": "offline_experiment_no_paid_review",
                        "limits": "Use saved experiment evaluation; no automatic provider review.",
                    },
                )
            return
        path = self.store._path(project_id, "runs", run_id, "branches", branch_id, "report.json")
        if path.exists():
            return
        run = self.store.run(project_id, run_id)
        scenario = self.store.scenario(project_id, run["scenario_id"], run["scenario_version"])
        evidence = build_review_evidence(
            metrics=[s.csv_row() for s in runtime.metrics.snapshots[-1:]],
            realized_events=[r.to_dict() for r in runtime.world.event_log.records[-100:]],
            assumptions=scenario_assumption_ids(scenario),
        )
        with self._lock:
            router = self.analysis_router(
                project_id, run_id, operation="report", branch_id=branch_id
            )
            result = generate_evidence_review(evidence, router)
            self.save_analysis(project_id, run_id, router)
            document = {
                "schema_version": 1,
                "branch_id": branch_id,
                "minute": runtime.world.minute_index,
                "state_hash": digest(runtime.world.to_dict()),
                "status": result.status,
                "markdown": render_review_markdown(result),
                "review": result.to_dict(),
            }
            with self.store.transaction():
                self.store._write(path, safe_document(document), immutable=True)
