"""The Project screen's draft routes: the structured-form preview and household correction.

Household correction links unaccompanied minors to a parent. Every draft comes
from the same Scenario preview the screen uses (tests/conftest.py
ui_project_api), so the people corrected here are generated ones, found in the
draft rather than pinned by ID.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sheltersim.contracts.population import HouseholdCorrectionRequest
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.initial_scenario import InitialScenarioRequest
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web.projects import ProjectAPI
from sheltersim.web.workspace import ProjectWorkspace

# Operator household shapes with no declared links. Generated minors then have
# no guardian, which is the case the correction route exists for; the default
# synthetic templates would link every minor to an adult instead.
UNLINKED_SOURCE_ID = "SRC-TEST-UNLINKED-HOUSEHOLDS"
UNLINKED_CONDITIONS = {
    "household_composition": {
        "templates": [
            {"template_id": f"unlinked-{size}", "members": [{"role": "unspecified"}] * size}
            for size in range(1, 6)
        ],
        "source_ids": [UNLINKED_SOURCE_ID],
    },
    "source_ids": [UNLINKED_SOURCE_ID],
}
MINOR_BLOCKER = "保護者を確認できない未成年者が"


def _preview(api, request: dict) -> dict:
    return api.workspace.post(["project", "scenario-preview"], {"text": "", "request": request})


def _unaccompanied_minors(scenario: dict) -> list[str]:
    people = scenario["model_configs"]["population"]["people"]
    context = scenario["model_configs"]["population_context"]["people"]
    return [
        agent_id
        for agent_id, person in sorted(context.items())
        if people[agent_id]["age_years"] < 18 and person.get("guardian_status") == "unaccompanied"
    ]


def _parent_in_household(scenario: dict, agent_id: str) -> str | None:
    """An adult in the minor's household old enough to be their parent, if any."""
    people = scenario["model_configs"]["population"]["people"]
    age, household_id = people[agent_id]["age_years"], people[agent_id]["household_id"]
    return next(
        (
            other
            for other in sorted(people)
            if people[other]["household_id"] == household_id
            and people[other]["age_years"] >= max(18, age + 16)
        ),
        None,
    )


def _correctable_minor(scenario: dict) -> tuple[str, str, str]:
    """The first unaccompanied minor with a possible parent: (minor, household, parent)."""
    people = scenario["model_configs"]["population"]["people"]
    for agent_id in _unaccompanied_minors(scenario):
        parent_id = _parent_in_household(scenario, agent_id)
        if parent_id is not None:
            return agent_id, people[agent_id]["household_id"], parent_id
    raise AssertionError("the draft has no unaccompanied minor with an adult in the household")


def _correction(scenario_hash: str, *rows: dict) -> dict:
    return {"schema_version": 1, "expected_scenario_hash": scenario_hash, "corrections": list(rows)}


def _link(agent_id: str, household_id: str, parent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "household_id": household_id,
        "parent_ids": [parent_id],
        "caregiver_ids": [],
    }


@pytest.fixture
def unlinked_preview(ui_project_api, generated_request) -> dict:
    """A draft whose generated minors have no guardian, so it cannot start yet."""
    preview = _preview(ui_project_api, generated_request(population_conditions=UNLINKED_CONDITIONS))
    assert len(_unaccompanied_minors(preview["scenario"])) >= 2
    return preview


def test_household_correction_creates_hash_bound_immutable_preview(
    ui_project_api, unlinked_preview
) -> None:
    api, source = ui_project_api, unlinked_preview
    minor, household_id, parent_id = _correctable_minor(source["scenario"])

    response = api.workspace.post(
        ["project", "scenario-household-corrections"],
        {
            "preview_id": source["preview_id"],
            "correction": _correction(
                source["approved_hash"], _link(minor, household_id, parent_id)
            ),
        },
    )

    assert response["preview_id"] != source["preview_id"]
    assert response["source_preview_id"] == source["preview_id"]
    assert response["source_scenario_hash"] == source["approved_hash"]
    assert response["corrected_agent_ids"] == [minor]
    assert response["request"] == source["request"]
    assert not response["can_start"]  # Other unreviewed minors remain.
    remaining = len(_unaccompanied_minors(source["scenario"])) - 1
    assert any(
        f"{MINOR_BLOCKER}{remaining}人います" in row for row in response["approval_blockers"]
    )
    corrected = response["scenario"]
    corrected_person = corrected["model_configs"]["population_context"]["people"][minor]
    assert corrected_person["parent_ids"] == [parent_id]
    assert corrected_person["guardian_status"] == "accompanied"
    assert (
        corrected["provenance"]["household_assignment_corrections"]["seed"]
        == (source["scenario"]["seed"])
    )
    # The source preview is unchanged.
    kept = api.workspace._preview("project", source["preview_id"])
    assert kept["hash"] == source["approved_hash"]
    assert kept["scenario"].model_configs["population_context"]["people"][minor]["parent_ids"] == []


def test_household_correction_rejects_stale_hash_and_cross_household_links(
    ui_project_api, unlinked_preview
) -> None:
    api, source = ui_project_api, unlinked_preview
    minor, household_id, parent_id = _correctable_minor(source["scenario"])
    people = source["scenario"]["model_configs"]["population"]["people"]
    stranger = next(
        other
        for other in sorted(people)
        if people[other]["household_id"] != household_id and people[other]["age_years"] >= 18
    )

    with pytest.raises(StorageError, match="stale_preview"):
        api.workspace.post(
            ["project", "scenario-household-corrections"],
            {
                "preview_id": source["preview_id"],
                "correction": _correction("0" * 64, _link(minor, household_id, parent_id)),
            },
        )
    with pytest.raises(StorageError, match="same household"):
        api.workspace.post(
            ["project", "scenario-household-corrections"],
            {
                "preview_id": source["preview_id"],
                "correction": _correction(
                    source["approved_hash"], _link(minor, household_id, stranger)
                ),
            },
        )


def test_household_correction_request_requires_a_real_change() -> None:
    with pytest.raises(ValueError, match="change at least one"):
        HouseholdCorrectionRequest.model_validate(
            {
                "schema_version": 1,
                "expected_scenario_hash": "a" * 64,
                "corrections": [{"agent_id": "A005", "household_id": "H000002"}],
            }
        )


def test_household_correction_keeps_other_blockers_from_a_real_preview(
    ui_project_api, generated_request
) -> None:
    api = ui_project_api
    # Every minor must be correctable here, which the shared seed does not give (one
    # minor lives without an adult); seed 0 at 10 people does. A 30 m² building
    # cannot hold them, so the capacity blocker must survive the correction.
    preview = _preview(
        api,
        generated_request(
            evacuees=10, seed=0, gross_area_m2=30, population_conditions=UNLINKED_CONDITIONS
        ),
    )
    assert any(MINOR_BLOCKER in row for row in preview["approval_blockers"])
    assert any("避難スペース容量" in row for row in preview["approval_blockers"])
    people = preview["scenario"]["model_configs"]["population"]["people"]
    links = [
        _link(
            minor, people[minor]["household_id"], _parent_in_household(preview["scenario"], minor)
        )
        for minor in _unaccompanied_minors(preview["scenario"])
    ]

    response = api.workspace.post(
        ["project", "scenario-household-corrections"],
        {
            "preview_id": preview["preview_id"],
            "correction": _correction(preview["approved_hash"], *links),
        },
    )

    assert not any(MINOR_BLOCKER in row for row in response["approval_blockers"])
    assert any("避難スペース容量" in row for row in response["approval_blockers"])
    assert response["can_start"] is False
    with pytest.raises(StorageError, match="startable reviewed scenario"):
        api.workspace.post(
            ["project", "scenario-approve"],
            {
                "preview_id": response["preview_id"],
                "approved_hash": response["approved_hash"],
                "approved": True,
                "run_id": "run-blocked",
            },
        )
    assert not api.store._path("project", "runs", "run-blocked").exists()


def test_household_correction_refuses_a_draft_without_recorded_blockers(
    ui_project_api, unlinked_preview
) -> None:
    api, source = ui_project_api, unlinked_preview
    minor, household_id, parent_id = _correctable_minor(source["scenario"])
    # A preview cached before approval blockers were recorded.
    legacy_preview_id = api.workspace._cache(
        {
            "usage_account_id": "draft-account",
            "kind": "scenario",
            "project_id": "project",
            "scenario": api.workspace._preview("project", source["preview_id"])["scenario"],
            "can_start": False,
            "hash": source["approved_hash"],
            "facility_design": None,
            "request": None,
            "research": None,
        }
    )

    with pytest.raises(StorageError, match="stale_preview"):
        api.workspace.post(
            ["project", "scenario-household-corrections"],
            {
                "preview_id": legacy_preview_id,
                "correction": _correction(
                    source["approved_hash"], _link(minor, household_id, parent_id)
                ),
            },
        )


def test_preparation_edits_share_account_even_without_new_client_field(
    project_store_at, tmp_path
) -> None:
    store = project_store_at(tmp_path)
    workspace = SimpleNamespace(store=store)
    first = ProjectWorkspace._preparation_account(workspace, "project")
    assert ProjectWorkspace._preparation_account(SimpleNamespace(store=store), "project") == first
    ProjectWorkspace._bind_preparation(workspace, "project", first, "approved-run")
    assert ProjectWorkspace._preparation_account(workspace, "project") != first


def test_project_preview_accepts_structured_form_without_free_text(ui_project_api) -> None:
    request = InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=8,
        duration_days=1,
        seed=1,
        model_parameter_overrides={
            "body_dynamics.sleep_debt_stress_rate_by_agent.*": {"A001": 0.003},
            "facility_queues.abandon_after_minutes_by_service.*": {"toilet": 60},
            "zone_environment.baseline_by_zone.*": {
                "sleeping": {"privacy_level": 0.2, "temperature_c": 30.0}
            },
        },
        evacuation_selection={"age_band_multipliers": {"child": 0, "adult": 1, "older_adult": 0}},
        field_origins={
            "municipality": "user",
            "disaster": "user",
            "evacuees": "user",
            "duration_days": "user",
        },
    )

    preview = _preview(ui_project_api, request.model_dump(mode="json"))

    scenario = preview["scenario"]
    assert scenario["model_configs"]["facility_queues"]["abandon_after_minutes_by_service"] == {
        "toilet": 60
    }
    assert scenario["model_configs"]["body_dynamics"]["sleep_debt_stress_rate_by_agent"] == {
        "A001": 0.003
    }
    assert scenario["model_configs"]["zone_environment"]["baseline_by_zone"]["sleeping"] == {
        "ventilation_effectiveness": 0.7,
        "lighting_level": 0.5,
        "privacy_level": 0.2,
        "temperature_c": 30.0,
    }
    overrides = preview["request"]["model_parameter_overrides"]
    assert overrides["facility_queues.abandon_after_minutes_by_service.*"] == {"toilet": 60}
    assert overrides["body_dynamics.sleep_debt_stress_rate_by_agent.*"] == {"A001": 0.003}
    assert overrides["zone_environment.baseline_by_zone.*"] == {
        "sleeping": {"privacy_level": 0.2, "temperature_c": 30.0}
    }
    provenance = scenario["provenance"]["scenario_input"]
    normalized_request = InitialScenarioRequest.model_validate(preview["request"])
    expected_source = digest(normalized_request.model_dump(mode="json"))
    assert preview["can_start"] is True
    assert provenance["extraction_source"] == "structured_form"
    assert provenance["sha256"] == expected_source
    assert preview["llm_usage"]["api_request_count"] == 0


def test_repeated_identical_preview_reuses_the_saved_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from sheltersim.region.models import RegionResearchResult
    from sheltersim.web import workspace as workspace_module

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []

    class ContractProvider:
        """Return the task's own contract-shaped answer, as an accepted reply."""

        def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
            del max_output_tokens, timeout_seconds
            calls.append(task.prompt_version)
            return ProviderResponse(text=task.fallback_text, model_id=model_id, input_tokens=10,
                                    output_tokens=5, elapsed_ms=0.0)

    real_router = workspace_module.accounted_router

    def live_like_router(*args, **kwargs):
        settings = LLMSettings(mode=LLMMode.RECORD, api_key="mock-key", budget_usd=10.0)
        router = real_router(*args, **{**kwargs, "settings": settings})
        router.provider.provider = ContractProvider()  # keep the real usage ledger
        return router

    monkeypatch.setattr(workspace_module, "accounted_router", live_like_router)
    store = ProjectStore(tmp_path / "projects")
    store.create_project("project")
    api = ProjectAPI(store, recover=False)
    monkeypatch.setattr(api.workspace.research, "research",
                        lambda query, **_kwargs: RegionResearchResult(
                            request=query, researched_at=datetime.now(UTC)))
    body = {"text": "横浜市で地震が発生。避難者30人、期間は1日。"}
    try:
        first = api.workspace.preview("project", body)
        second = api.workspace.preview("project", body)
    finally:
        api.close()

    source = "extraction_source"
    assert first["scenario"]["provenance"]["scenario_input"][source] == "prepare_llm"
    # Before stored tapes, the ledger refused this identical paid request.
    assert second["scenario"]["provenance"]["scenario_input"][source] == "prepare_llm"
    assert second["llm_usage"]["api_request_count"] == 0
    assert calls == ["region-scenario-extraction-v1"]


def test_repeated_identical_intervention_preview_reuses_the_saved_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, small_benchmark_scenario
) -> None:
    from sheltersim.web import workspace as workspace_module

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []

    class AcceptedProvider:
        def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
            del max_output_tokens, timeout_seconds
            calls.append(task.prompt_version)
            return ProviderResponse(text=task.fallback_text, model_id=model_id, input_tokens=10,
                                    output_tokens=5, elapsed_ms=0.0)

    real_router = workspace_module.accounted_router

    def live_like_router(*args, **kwargs):
        settings = LLMSettings(mode=LLMMode.RECORD, api_key="mock-key", budget_usd=10.0)
        router = real_router(*args, **{**kwargs, "settings": settings})
        router.provider.provider = AcceptedProvider()  # keep the real usage ledger
        return router

    monkeypatch.setattr(workspace_module, "accounted_router", live_like_router)
    scenario = small_benchmark_scenario(30)
    store = ProjectStore(tmp_path / "projects")
    store.create_project("project")
    saved = store.save_scenario("project", scenario,
                                approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    TimelineStore(store, "project", "run").record(
        BranchRuntime.from_world(WorldState.from_scenario(scenario))
    )
    api = ProjectAPI(store, recover=False)
    body = {"minute": 0, "text": "information_desk ゾーンにスタッフを2人追加してください。"}
    try:
        first = api.workspace.intervention("project", "run", "main", dict(body))
        calls_after_first = len(calls)
        second = api.workspace.intervention("project", "run", "main", dict(body))
    finally:
        api.close()

    assert calls_after_first >= 1 and first["fallback_used"] is False
    # Before stored compiler tapes, the ledger refused this identical paid request.
    assert second["fallback_used"] is False
    assert len(calls) == calls_after_first
