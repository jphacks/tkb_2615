"""CLI entry points for durable projects; simulation work remains in shared services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.accounting import accounted_router
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError, digest


def configure_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "project", help="save, reopen, resume and inspect durable projects"
    )
    parser.add_argument("--root", type=Path, default=Path("runs/.projects"))
    actions = parser.add_subparsers(dest="project_action", required=True)
    actions.add_parser("list")
    for action in (
        "create",
        "show",
        "save-scenario",
        "new-run",
        "resume",
        "frame",
        "fork",
        "archive",
        "delete",
        "prepare",
        "approve-draft",
        "rerun",
        "usage",
        "draft-save",
        "draft-show",
        "history",
    ):
        command = actions.add_parser(action)
        command.add_argument("--project-id", required=True)
        if action in {
            "new-run",
            "resume",
            "frame",
            "fork",
            "delete",
            "approve-draft",
            "rerun",
            "usage",
            "history",
        }:
            command.add_argument("--run-id", required=True)
        if action == "prepare":
            command.add_argument("--text", required=True)
            command.add_argument("--input-json", type=Path)
            command.add_argument("--draft-id")
        if action in {"draft-save", "draft-show"}:
            command.add_argument("--draft-id", required=True)
        if action in {"draft-save", "history"}:
            command.add_argument("--input-json", type=Path, required=True)
        if action == "history":
            command.add_argument("--branch-id", default="main")
        if action == "draft-save":
            command.add_argument("--expected-hash")
        if action == "approve-draft":
            command.add_argument("--preview-id", required=True)
            command.add_argument("--approved-hash", required=True)
        if action == "rerun":
            command.add_argument("--new-run-id", required=True)
            command.add_argument("--scenario", type=Path)
            command.add_argument("--approved-hash")
        if action == "save-scenario":
            command.add_argument("--scenario", type=Path, required=True)
            command.add_argument(
                "--approved-hash", required=True, help="SHA256 of reviewed canonical ScenarioSpec"
            )
        if action == "new-run":
            command.add_argument("--scenario-id", required=True)
            command.add_argument("--scenario-version", type=int, required=True)
        if action in {"resume", "frame", "fork"}:
            command.add_argument("--branch-id", default="main")
            command.add_argument("--minute", type=int, required=True)
        if action == "fork":
            command.add_argument("--child-id", required=True)
        if action == "archive":
            command.add_argument("--run-id")
        if action == "delete":
            command.add_argument(
                "--confirmation", required=True, help="repeat exact inactive Run ID"
            )


def execute(arguments: Any) -> dict[str, Any]:
    store = ProjectStore(arguments.root)
    action = arguments.project_action
    if action == "list":
        return {"projects": store.projects(), "storage": store.usage()}
    project_id = arguments.project_id
    if action == "create":
        return store.create_project(project_id)
    if action == "show":
        return store.project(project_id)
    if action in {
        "prepare",
        "approve-draft",
        "rerun",
        "usage",
        "draft-save",
        "draft-show",
        "history",
    }:
        from sheltersim.contracts.workspace import DraftInput, HistoryQuery
        from sheltersim.storage.artifacts import ArtifactStore
        from sheltersim.web.project_queries import ProjectQueries
        from sheltersim.web.projects import ProjectAPI

        api = ProjectAPI(store, recover=False)
        try:
            if action == "prepare":
                body = json.loads(arguments.input_json.read_text()) if arguments.input_json else {}
                body["text"] = arguments.text
                if arguments.draft_id:
                    body["draft_id"] = arguments.draft_id
                return api.post([project_id, "scenario-preview"], body)
            if action == "approve-draft":
                return api.post(
                    [project_id, "scenario-approve"],
                    {
                        "run_id": arguments.run_id,
                        "preview_id": arguments.preview_id,
                        "approved_hash": arguments.approved_hash,
                        "approved": True,
                    },
                )
            if action == "rerun":
                body = {"new_run_id": arguments.new_run_id}
                if arguments.scenario:
                    body.update(
                        {
                            "scenario": json.loads(
                                ScenarioSpec.load(arguments.scenario).canonical_json()
                            ),
                            "approved_hash": arguments.approved_hash,
                            "approved": True,
                        }
                    )
                return api.post([project_id, "runs", arguments.run_id, "rerun"], body)
            if action == "usage":
                return api.get([project_id, "runs", arguments.run_id, "usage"], {})
            if action == "draft-show":
                return ArtifactStore(store).draft(project_id, arguments.draft_id)
            if action == "draft-save":
                return ArtifactStore(store).save_draft(
                    project_id,
                    arguments.draft_id,
                    DraftInput(
                        content=json.loads(arguments.input_json.read_text()),
                        expected_hash=arguments.expected_hash,
                    ),
                )
            return ProjectQueries(store).history(
                project_id,
                arguments.run_id,
                arguments.branch_id,
                HistoryQuery.model_validate(json.loads(arguments.input_json.read_text())),
            )
        finally:
            api.close()
    if action == "save-scenario":
        scenario = ScenarioSpec.load(arguments.scenario)
        return store.save_scenario(project_id, scenario, approved_hash=arguments.approved_hash)
    if action == "new-run":
        from sheltersim.web.projects import ProjectAPI

        api = ProjectAPI(store, recover=False)
        try:
            return api.post(
                [project_id, "runs"],
                {
                    "run_id": arguments.run_id,
                    "scenario_id": arguments.scenario_id,
                    "scenario_version": arguments.scenario_version,
                },
            )
        finally:
            api.close()
    if action == "archive":
        store.archive(project_id, arguments.run_id)
        return store.project(project_id)
    if action == "delete":
        store.delete_run(project_id, arguments.run_id, confirmation=arguments.confirmation)
        return {"deleted": arguments.run_id}
    timeline = TimelineStore(store, project_id, arguments.run_id)
    if action == "frame":
        return timeline.frame(arguments.branch_id, arguments.minute)
    if action == "fork":
        runtime = timeline.fork(arguments.branch_id, arguments.minute, arguments.child_id)
        return {
            "branch_id": runtime.branch_id,
            "minute": runtime.world.minute_index,
            "state_hash": digest(runtime.world.to_dict()),
        }
    if action == "resume":
        _, checkpoint = store.load_branch(project_id, arguments.run_id, arguments.branch_id)
        router = None
        if "llm" in checkpoint.runtime_state:
            settings = LLMSettings.from_env()
            if checkpoint.runtime_state["llm"]["mode"] == "offline":
                from sheltersim.experiments.timeline import settings_from_record
                from sheltersim.llm.config import LLMMode

                settings = settings_from_record(
                    checkpoint.runtime_state.get("llm_settings", {}),
                    mode=LLMMode.OFFLINE,
                    api_key=None,
                )
            elif settings.effective_mode.value != checkpoint.runtime_state["llm"]["mode"]:
                raise StorageError("restore the saved LLM configuration to continue")
            router = accounted_router(
                store,
                project_id,
                run_id=arguments.run_id,
                operation="runtime",
                branch_id=arguments.branch_id,
                settings=settings,
            )
        lease = store.claim_run(project_id, arguments.run_id)
        review_status = "not_requested"
        try:
            store.set_status(project_id, arguments.run_id, "running")
            runtime = timeline._advance_locked(arguments.branch_id, arguments.minute, router=router)
            store.set_status(
                project_id,
                arguments.run_id,
                "completed"
                if runtime.world.minute_index == runtime.world.duration_minutes
                else "paused",
            )
            if runtime.world.minute_index == runtime.world.duration_minutes:
                from sheltersim.web.projects import ProjectAPI

                api = ProjectAPI(store, recover=False)
                try:
                    api.workspace.finish_review(
                        project_id, arguments.run_id, arguments.branch_id, runtime
                    )
                    review_status = "stored"
                except Exception:
                    # Optional analysis failure must not relabel a durable completed simulation.
                    review_status = "unavailable"
                finally:
                    api.close()
        except BaseException:
            store.set_status(project_id, arguments.run_id, "interrupted")
            raise
        finally:
            store.release_run(lease)
        return {
            "run_id": arguments.run_id,
            "branch_id": runtime.branch_id,
            "minute": runtime.world.minute_index,
            "review_status": review_status,
        }
    raise StorageError("unknown project command")
