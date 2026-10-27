"""A Project's intervention preview sends a running generated Scenario to Opus, and
an approved preview forks the Branch without changing it.

The preview-and-apply tests are the Project screen's counterparts of the demo's
tests/integration/test_nl_demo.py and tests/e2e/test_nl_demo_branching.py (#137);
their text is read by the offline rules, as in
tests/e2e/test_project_scenario_to_results.py.

The compiler's own contract (repair, fallback, redaction, stop policy) is
checked in tests/unit/llm/test_natural_language.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMErrorPolicy, LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMProviderError, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web import workspace
from sheltersim.web.projects import ProjectAPI


class FakeProvider:
    def __init__(self, *responses: str, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        self.calls.append((model_id, task))
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            text=self.responses.pop(0),
            model_id=model_id,
            input_tokens=120,
            output_tokens=80,
            elapsed_ms=12.0,
        )


def _router(provider: FakeProvider, *, stop: bool = False) -> LLMRouter:
    return LLMRouter(
        LLMSettings(
            mode=LLMMode.RECORD,
            api_key="test-key",
            max_api_calls=2,
            max_input_tokens=16_384,
            max_output_tokens=4096,
            budget_usd=1.0,
            opus_price=ModelPrice(1, 2),
            haiku_price=ModelPrice(0.1, 0.5),
            error_policy=LLMErrorPolicy.STOP if stop else LLMErrorPolicy.FALLBACK,
        ),
        provider=provider,
        tape=ResponseTape(),
    )


def _mid_run_project(tmp_path, scenario, minute: int) -> tuple[ProjectStore, str]:
    """A Project store whose generated Scenario's main Branch reached `minute`."""
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    timeline = TimelineStore(store, "project", "run")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", minute)
    return store, sorted(timeline.seek("main", minute).world.zones)[0]


def test_generated_scenario_mid_run_intervention_reaches_opus(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    store, zone_id = _mid_run_project(tmp_path, generated_scenario, 60)
    provider = FakeProvider(
        json.dumps(
            {
                "status": "compiled",
                "support_mode": "native",
                "mechanism_summary": "支援要員を追加します。",
                "effects": [
                    {
                        "op": "add_staff",
                        "target_kind": "zone",
                        "target_id": zone_id,
                        "start_minute": 60,
                        "parameters": {"count": 2},
                        "assumption_indexes": [],
                    }
                ],
                "assumptions": [],
                "limitations": [],
                "questions": [],
                "choices": [],
            },
            ensure_ascii=False,
        )
    )
    # The default generic guard is 16,384 bytes; the generated context is larger.
    monkeypatch.setattr(workspace, "accounted_router", lambda *_a, **_k: _router(provider))
    api = ProjectAPI(store, recover=False)
    try:
        result = api.post(
            ["project", "runs", "run", "branches", "main", "intervention-preview"],
            {"minute": 60, "text": "スタッフを2人追加"},
        )
    finally:
        api.close()

    assert len(provider.calls) == 1
    assert len(provider.calls[0][1].input_text.encode()) > 16_384
    assert result["status"] == "compiled"
    assert result["fallback_used"] is False
    assert result["usage"]["api_request_count"] == 1
    assert result["can_apply"] is True
    assert [effect["start_minute"] for effect in result["program"]["effects"]] == [60]


def test_project_preview_returns_the_stop_reason_as_a_conflict(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    store, _ = _mid_run_project(tmp_path, generated_scenario, 30)
    provider = FakeProvider(error=LLMProviderError("rate limited", status_code=429))
    monkeypatch.setattr(
        workspace, "accounted_router", lambda *_a, **_k: _router(provider, stop=True)
    )
    api = ProjectAPI(store, recover=False)
    try:
        with pytest.raises(StorageError) as raised:
            api.post(
                ["project", "runs", "run", "branches", "main", "intervention-preview"],
                {"minute": 30, "text": "スタッフを2人追加"},
            )
    finally:
        api.close()

    assert raised.value.code == "intervention_compiler_unavailable"
    assert "429" in str(raised.value)


# The main Branch is computed to COMPUTED_UNTIL; previews pick an earlier minute
# between its saved Checkpoints (every 30 minutes), as the screen's time slider does.
COMPUTED_UNTIL = 90
PREVIEW_MINUTE = 40
ARRIVALS = "避難スペースに避難者を10人追加"
MAIN = ["project", "runs", "run", "branches", "main"]


@pytest.fixture
def project_api(tmp_path, generated_scenario, monkeypatch):
    """The Project API over a generated Scenario Run computed to COMPUTED_UNTIL, offline."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = _mid_run_project(tmp_path, generated_scenario, COMPUTED_UNTIL)
    api = ProjectAPI(store, recover=False)
    try:
        yield api
    finally:
        api.close()


def _saved_state(api: ProjectAPI) -> tuple[str, str]:
    """The main Branch's saved head and its state at PREVIEW_MINUTE."""
    timeline = TimelineStore(api.store, "project", "run")
    return (
        api.store.load_branch("project", "run", "main")[1].to_json(),
        digest(timeline.seek("main", PREVIEW_MINUTE).world.to_dict()),
    )


def _preview(api: ProjectAPI, body: dict) -> dict:
    return api.post([*MAIN, "intervention-preview"], {"minute": PREVIEW_MINUTE, **body})


def _apply(api: ProjectAPI, preview: dict, child_id: str, **overrides) -> dict:
    return api.post(
        [*MAIN, "intervention-apply"],
        {
            "preview_id": preview["preview_id"],
            "approved_program_hash": preview["approved_program_hash"],
            "approved": True,
            "child_id": child_id,
            **overrides,
        },
    )


def test_project_preview_changes_neither_the_branch_nor_its_history(project_api) -> None:
    before = _saved_state(project_api)

    preview = _preview(project_api, {"text": ARRIVALS})

    assert preview["status"] == "compiled"
    assert preview["can_apply"] is True
    assert _saved_state(project_api) == before


def test_project_apply_forks_at_a_past_minute_and_leaves_the_source_unchanged(
    project_api,
) -> None:
    before = _saved_state(project_api)
    preview = _preview(project_api, {"text": ARRIVALS})

    _apply(project_api, preview, "arrivals")

    reference = project_api.store.branch_reference("project", "run", "arrivals")
    assert (reference.parent_branch_id, reference.fork_minute) == ("main", PREVIEW_MINUTE)
    assert reference.computed_until == PREVIEW_MINUTE
    assert _saved_state(project_api) == before
    child = TimelineStore(project_api.store, "project", "run").seek("arrivals", PREVIEW_MINUTE)
    assert child.runtime_state["fork_origin"]["state_hash"] == before[1]
    assert any(
        event.provenance.get("effect_program_v1", {}).get("op") == "add_evacuees"
        for event in child.world.event_queue.pending()
    )

    # A second approval of the same plan cannot reuse the Branch name.
    again = _preview(project_api, {"text": ARRIVALS})
    with pytest.raises(StorageError, match="child branch already exists"):
        _apply(project_api, again, "arrivals")
    assert _saved_state(project_api) == before


def test_project_apply_needs_an_applicable_preview_its_hash_and_approval(
    project_api, add_toilets_program
) -> None:
    before = _saved_state(project_api)
    blocked_program = add_toilets_program(start_minute=PREVIEW_MINUTE).model_dump(mode="json")
    blocked_program["effects"][0]["target_selector"]["target_id"] = "no-such-area"
    blocked = _preview(project_api, {"program": blocked_program})
    assert blocked["can_apply"] is False
    assert blocked["validation_issues"]
    with pytest.raises(StorageError, match="does not match validated preview"):
        _apply(project_api, blocked, "blocked")

    preview = _preview(project_api, {"text": ARRIVALS})
    with pytest.raises(StorageError, match="does not match validated preview"):
        _apply(project_api, preview, "wrong-hash", approved_program_hash="0" * 64)
    with pytest.raises(StorageError, match="explicit human approval"):
        _apply(project_api, preview, "not-approved", approved=False)

    text_only = _preview(project_api, {"text": "空を飛ぶ"})
    assert text_only["status"] == "not_simulatable"
    assert "preview_id" not in text_only
    assert _saved_state(project_api) == before
    assert [branch["branch_id"] for branch in project_api.store.branches("project", "run")] == [
        "main"
    ]
