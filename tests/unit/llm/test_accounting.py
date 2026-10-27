"""Focused and final #122 regression contracts."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from sheltersim.contracts.workspace import AssetBegin, AssetChunk, DraftInput, UsageLimits
from sheltersim.llm.accounting import AccountedProvider
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.contracts import LLMProviderError, LLMStage, LLMTask, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.storage.usage import UsageLedger, task_campaign_ledger
from sheltersim.web.project_queries import page


def store_at(root: Path) -> ProjectStore:
    store = ProjectStore(root)
    store.create_project("project")
    return store


def limits() -> UsageLimits:
    return UsageLimits(max_calls=1, max_input_tokens=20000,
                       max_output_tokens=1000, budget_usd=1)


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs: object) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(text='{"ok":true}', model_id="fake", input_tokens=5,
                                output_tokens=3, elapsed_ms=0)


def test_sibling_routers_share_paid_cap_and_denial_costs_zero(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = UsageLedger(store, "project", "account")
    ledger.create(limits())
    provider = CountingProvider()
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=10,
                           budget_usd=1, cache_path=tmp_path / "unused.json")
    task = LLMTask(stage=LLMStage.RUNTIME, input_text="synthetic evidence", state_hash="state",
                   prompt_version="contract-v1", fallback_text='{"ok":false}',
                   max_output_tokens=100, output_schema={"type": "object", "properties": {
                       "ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False})
    routers = [LLMRouter(settings, provider=AccountedProvider(provider, ledger, settings,
               "runtime", branch), tape=ResponseTape()) for branch in ("main", "child")]
    assert routers[0].complete(task) == '{"ok":true}'
    assert routers[1].complete(task) == '{"ok":false}'
    assert provider.calls == 1
    assert ledger.summary()["totals"]["calls"] == 1
    assert routers[1].records[-1].api_request_count == 0
    assert routers[1].records[-1].estimated_cost_usd == 0


def test_uncertain_receipt_survives_reload_without_refund(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = UsageLedger(store, "project", "account")
    ledger.create(limits())
    receipt = ledger.reserve(request_key="input", operation="compiler", branch_id="main",
        stage="prepare", model_id="fake", input_tokens=100, output_tokens=50, cost_usd=.1)
    ledger.settle(receipt, input_tokens=0, output_tokens=0, cost_usd=0, outcome="uncertain")
    reloaded = UsageLedger(store, "project", "account")
    reloaded.create(UsageLimits(max_calls=100, max_input_tokens=100000,
                              max_output_tokens=100000, budget_usd=100))
    assert reloaded.summary()["limits"]["max_calls"] == 1
    assert reloaded.summary()["totals"]["cost_nusd"] == 100000000
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reloaded.reserve(request_key="another", operation="questions", branch_id="child",
            stage="review", model_id="fake", input_tokens=1, output_tokens=1, cost_usd=.001)


def test_draft_cas_preserves_prior_revision_and_rejects_stale_editor(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    drafts = ArtifactStore(store)
    initial = drafts.save_draft("project", "draft", DraftInput(content={"people": 100}))
    edited = drafts.save_draft("project", "draft", DraftInput(
        content={"people": 500}, expected_hash=initial["content_hash"]))
    with pytest.raises(StorageError, match="draft_conflict"):
        drafts.save_draft("project", "draft", DraftInput(
            content={"people": 30}, expected_hash=initial["content_hash"]))
    assert ArtifactStore(store).draft("project", "draft")["content_hash"] == edited["content_hash"]
    assert store._read(store._path("project", "drafts", "draft", "v1.json"))["content"] == {"people": 100}


def test_chunk_retry_hash_verification_and_referenced_asset_retention(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assets = ArtifactStore(store)
    content = b'{"rooms":[]}'
    sha = hashlib.sha256(content).hexdigest()
    assets.begin("project", "upload", AssetBegin(media_type="application/json",
        byte_count=len(content), sha256=sha, permission_confirmed=True))
    chunk = AssetChunk(index=0, data_base64=base64.b64encode(content).decode())
    assets.chunk("project", "upload", chunk)
    assert assets.chunk("project", "upload", chunk)["chunks"] == 1
    assert assets.finish("project", "upload")["asset_id"] == sha
    assert assets.finish("project", "upload")["asset_id"] == sha
    assert base64.b64decode(assets.asset("project", sha, include_content=True)["data_base64"]) == content
    assets.save_draft("project", "plan", DraftInput(content={"plan_sha256": sha}))
    with pytest.raises(StorageError, match="asset_in_use"):
        assets.delete("project", "assets", sha, confirmation=sha)


def test_page_cursor_cannot_mix_heads_or_query_context() -> None:
    first = page(list(range(10)), {"branch_id": "main"}, "head-one", limit=2)
    with pytest.raises(StorageError, match="stale_or_foreign_cursor"):
        page(list(range(10)), {"branch_id": "main"}, "head-two", limit=2,
             cursor=first["next_cursor"])
    with pytest.raises(StorageError, match="stale_or_foreign_cursor"):
        page(list(range(10)), {"branch_id": "child"}, "head-one", limit=2,
             cursor=first["next_cursor"])


def test_registered_state_proposals_are_atomic_and_versions_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from copy import deepcopy

    from sheltersim.contracts import runtime as contracts
    from sheltersim.contracts.workspace import StateDescriptor
    from sheltersim.core.checkpoint import Checkpoint, CheckpointError
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec

    namespace = "qa_body_v1"
    descriptor = StateDescriptor(namespace=namespace, implementation_id="qa-body",
        implementation_version="1", schema_version=1,
        fields={"sleep_debt": {"unit": "minute", "public": True}})

    def initial(world: WorldState) -> dict:
        return {"schema_version": 1, "agents": {key: {"sleep_debt": 0} for key in world.agents}}

    def validate(world: WorldState, state: dict) -> None:
        if set(state["agents"]) != set(world.agents) or any(
                row["sleep_debt"] < 0 for row in state["agents"].values()):
            raise ValueError("invalid synthetic debt state")

    def project(state: dict, entity_id: str) -> dict:
        return {"sleep_debt": state["agents"][entity_id]["sleep_debt"]}

    monkeypatch.setitem(contracts._CONTRACTS, namespace, (descriptor, validate, initial, project))
    fixture = Path(__file__).resolve().parents[1] / "fixtures/scenarios/h00_small_replay.json"
    document = ScenarioSpec.load(fixture).model_dump(mode="json")
    document["model_configs"]["state_contracts"] = {"schema_version": 1, "namespaces": {
        namespace: {"implementation_id": "qa-body", "implementation_version": "1", "schema_version": 1}}}
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    original = deepcopy(world.to_dict())
    assert contracts.entity_projections(world)
    assert world.to_dict() == original
    invalid = deepcopy(world.model_state[namespace])
    invalid["agents"][next(iter(world.agents))]["sleep_debt"] = -1
    with pytest.raises(ValueError):
        contracts.apply_model_proposal(world, contracts.ModelStateProposal(
            namespace=namespace, model_id="qa-body", minute=0, proposed_state=invalid))
    assert world.to_dict() == original
    checkpoint = Checkpoint.capture(world)
    monkeypatch.setitem(contracts._CONTRACTS, namespace, (
        descriptor.model_copy(update={"implementation_version": "2"}), validate, initial, project))
    with pytest.raises(CheckpointError, match="migration"):
        checkpoint.restore_world()


def test_historical_router_cannot_backfill_or_write_global_tape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sheltersim.experiments.timeline import (
        RecordedRouter,
        TimelineError,
        public_router_settings,
    )
    monkeypatch.chdir(tmp_path)
    settings = LLMSettings(mode="record", api_key="unused", budget_usd=1)
    task = LLMTask(stage=LLMStage.RUNTIME, input_text="recorded input", state_hash="state",
                   prompt_version="v1", fallback_text='{"ok":false}', max_output_tokens=100)
    live = LLMRouter(settings, provider=CountingProvider(), tape=ResponseTape())
    start = live.snapshot_state()
    live.complete(task)
    complete = live.snapshot_state()
    complete["response_tape"] = {"schema_version": 1, "entries": {}}
    global_path = tmp_path / ".sheltersim/llm-tape.json"
    tape = ResponseTape(path=global_path)
    tape.put(task.cache_key(settings.model_for(task.stage.value)), ProviderResponse(
        text='{"ok":true}', model_id="fake", input_tokens=1, output_tokens=1, elapsed_ms=0))
    original = global_path.read_bytes()
    recorded = public_router_settings(live)
    assert "api_key" not in recorded
    assert recorded["settings_schema_version"] == 3
    replay = RecordedRouter(start, complete, recorded)
    with pytest.raises(TimelineError, match="missing accepted response"):
        replay.complete(task)
    assert global_path.read_bytes() == original


def test_preparation_edits_share_account_even_without_new_client_field(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sheltersim.web.workspace import ProjectWorkspace
    store = store_at(tmp_path)
    workspace = SimpleNamespace(store=store)
    first = ProjectWorkspace._preparation_account(workspace, "project")
    assert ProjectWorkspace._preparation_account(SimpleNamespace(store=store), "project") == first
    ProjectWorkspace._bind_preparation(workspace, "project", first, "approved-run")
    assert ProjectWorkspace._preparation_account(workspace, "project") != first


def test_task_campaign_cap_is_shared_across_accounts_and_process_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store_a = store_at(tmp_path / "project-a")
    store_b = store_at(tmp_path / "project-b")
    campaign_a = task_campaign_ledger(store_a)
    campaign_a.reserve(request_key="held-near-cap", operation="qa", branch_id="main",
        stage="prepare", model_id="fake", input_tokens=1, output_tokens=1, cost_usd=49.999)

    local_b = UsageLedger(store_b, "project", "account-b")
    local_b.create(UsageLimits(max_calls=2, max_input_tokens=20000,
        max_output_tokens=1000, budget_usd=1))
    provider = CountingProvider()
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=10,
        budget_usd=1, cache_path=tmp_path / "unused.json")
    task = LLMTask(stage=LLMStage.PREPARE, input_text="bounded request",
        state_hash="campaign-state", prompt_version="campaign-v1",
        fallback_text='{"ok":false}', max_output_tokens=100,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False})
    router = LLMRouter(settings, provider=AccountedProvider(provider, local_b, settings,
        "prepare", "main", campaign_ledger=task_campaign_ledger(store_b)))
    assert router.complete(task) == '{"ok":false}'
    assert provider.calls == 0
    assert local_b.summary()["totals"]["calls"] == 0

    reloaded = task_campaign_ledger(ProjectStore(tmp_path / "other-project-root"))
    assert reloaded.summary()["totals"]["cost_nusd"] == 49_999_000_000
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reloaded.reserve(request_key="would-overrun", operation="qa", branch_id=None,
            stage="review", model_id="fake", input_tokens=1, output_tokens=1,
            cost_usd=.002)


def test_local_preflight_failure_releases_only_its_campaign_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = store_at(tmp_path / "projects")
    campaign = task_campaign_ledger(store)
    blocked = UsageLedger(store, "project", "blocked")
    blocked.create(UsageLimits(max_calls=1, max_input_tokens=20000,
        max_output_tokens=1000, budget_usd=0))
    existing = blocked.reserve(request_key="existing", operation="qa", branch_id=None,
        stage="prepare", model_id="fake", input_tokens=1, output_tokens=1, cost_usd=0)
    blocked.settle(existing, input_tokens=1, output_tokens=1, cost_usd=0, outcome="settled")
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=10,
        budget_usd=1, cache_path=tmp_path / "unused.json")
    provider = CountingProvider()
    task = LLMTask(stage=LLMStage.PREPARE, input_text="request", state_hash="state",
        prompt_version="cancel-v1", fallback_text='{"ok":false}', max_output_tokens=50,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False})
    router = LLMRouter(settings, provider=AccountedProvider(provider, blocked, settings,
        "prepare", "main", campaign_ledger=campaign))
    assert router.complete(task) == '{"ok":false}'
    assert provider.calls == 0
    assert campaign.summary()["totals"]["calls"] == 0


def test_campaign_retains_uncertain_provider_charge_across_store_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sheltersim.llm.accounting import runtime_usage_summary

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    project_store = store_at(tmp_path / "project-root")
    local = UsageLedger(project_store, "project", "uncertain")
    # Output limits leave room for the router's thinking headroom on non-runtime stages.
    local.create(UsageLimits(max_calls=2, max_input_tokens=2_000_000,
        max_output_tokens=10_000, budget_usd=1))
    campaign = task_campaign_ledger(project_store)

    class TimeoutProvider:
        def complete(self, **_kwargs):
            raise TimeoutError("the request may have reached the provider")

    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=2,
        budget_usd=1, cache_path=tmp_path / "unused.json")
    task = LLMTask(stage=LLMStage.REVIEW, input_text="short request", state_hash="state",
        prompt_version="uncertain-v1", fallback_text='{"ok":false}', max_output_tokens=50,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False})
    router = LLMRouter(settings, provider=AccountedProvider(TimeoutProvider(), local,
        settings, "review", "main", campaign_ledger=campaign))
    assert router.complete(task) == '{"ok":false}'
    assert local.summary()["uncertain_calls"] == 1
    assert campaign.summary()["uncertain_calls"] == 1

    summary = runtime_usage_summary(ProjectStore(tmp_path / "another-project-root"))
    assert summary["campaign"]["calls"] == 1
    assert summary["campaign"]["uncertain_calls"] == 1
    assert summary["campaign"]["remaining_usd"] < 50


def test_non_text_provider_response_settles_actual_usage_and_keeps_safe_error_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = store_at(tmp_path / "projects")
    local = UsageLedger(store, "project", "empty-text-response")
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=2,
        max_input_tokens=4096, max_output_tokens=128, budget_usd=1,
        cache_path=tmp_path / "unused.json")
    local.create(UsageLimits(max_calls=2, max_input_tokens=2_000_000,
        max_output_tokens=1000, budget_usd=1))
    campaign = task_campaign_ledger(store)
    response_metadata = ProviderResponse(
        text="", model_id=settings.haiku_model, input_tokens=12, output_tokens=5,
        elapsed_ms=2.0,
    )

    class EmptyTextProvider:
        def complete(self, **_kwargs: object) -> ProviderResponse:
            raise LLMProviderError(
                "Anthropic API returned no text content",
                failure_kind="provider_refusal",
                response_metadata=response_metadata,
            )

    task = LLMTask(stage=LLMStage.RUNTIME, input_text="bounded test", state_hash="state",
        prompt_version="empty-text-response-v1", fallback_text='{"ok":false}',
        max_output_tokens=32,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False})
    router = LLMRouter(settings, provider=AccountedProvider(
        EmptyTextProvider(), local, settings, "runtime", "main", campaign_ledger=campaign))

    assert router.complete(task) == '{"ok":false}'
    assert router.records[0].outcome == "invalid_output"
    assert router.records[0].error_kind == "provider_refusal"
    assert router.records[0].input_tokens == 12
    assert router.records[0].output_tokens == 5
    assert local.summary()["uncertain_calls"] == 0
    assert campaign.summary()["uncertain_calls"] == 0
    assert local.summary()["by_error_code"] == {"provider_refusal": 1}
    assert campaign.summary()["by_error_code"] == {"provider_refusal": 1}


def test_campaign_settlement_counts_cache_and_hosted_search_fees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = store_at(tmp_path / "projects")
    local = UsageLedger(store, "project", "search-costs")
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=2,
        max_input_tokens=4096, max_output_tokens=128, budget_usd=10,
        cache_path=tmp_path / "unused.json")
    local.create(UsageLimits(max_calls=2, max_input_tokens=2_000_000,
        max_output_tokens=10_000, budget_usd=10))
    campaign = task_campaign_ledger(store)

    class SearchProvider:
        def complete(self, **_kwargs: object) -> ProviderResponse:
            return ProviderResponse(text='{"ok":true}', model_id="fake", input_tokens=100,
                output_tokens=20, elapsed_ms=1, cache_creation_5m_input_tokens=10,
                cache_creation_1h_input_tokens=20, cache_read_input_tokens=5,
                web_search_requests=1)

    task = LLMTask(stage=LLMStage.PREPARE, input_text="short official search",
        state_hash="search-state", prompt_version="search-accounting-v1",
        fallback_text='{"ok":false}', max_output_tokens=64,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False},
        web_search_allowed_domains=("go.jp",), max_web_searches=1)
    router = LLMRouter(settings, provider=AccountedProvider(SearchProvider(), local, settings,
        "region_search", "main", campaign_ledger=campaign))
    assert router.complete(task) == '{"ok":true}'
    assert campaign.summary()["totals"]["cost_nusd"] == 11_011_000
    receipt, _ = campaign.receipt_records({"operation": "region_search"})
    assert receipt[0]["input_tokens"] == 135
    assert receipt[0]["output_tokens"] == 20


def test_search_output_over_one_pass_does_not_freeze_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = store_at(tmp_path / "projects")
    settings = LLMSettings(mode="record", api_key="unused", max_api_calls=2,
        max_input_tokens=4096, max_output_tokens=256, budget_usd=10,
        cache_path=tmp_path / "unused.json")
    from sheltersim.llm.accounting import usage_limits
    local = UsageLedger(store, "project", "search-passes")
    local.create(usage_limits(settings))
    campaign = task_campaign_ledger(store)

    class TwoPassSearchProvider:
        def complete(self, *, max_output_tokens: int, **_kwargs: object) -> ProviderResponse:
            # Server tools sample before and after the search; usage sums both passes.
            return ProviderResponse(text='{"ok":true}', model_id="fake", input_tokens=100,
                output_tokens=max_output_tokens + 253, elapsed_ms=1, web_search_requests=1)

    task = LLMTask(stage=LLMStage.PREPARE, input_text="official search",
        state_hash="search-state", prompt_version="search-passes-v1",
        fallback_text='{"ok":false}', max_output_tokens=256,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False},
        web_search_allowed_domains=("go.jp",), max_web_searches=1)
    router = LLMRouter(settings, provider=AccountedProvider(TwoPassSearchProvider(), local,
        settings, "region_search", "main", campaign_ledger=campaign))

    assert router.complete(task) == '{"ok":true}'
    assert local.summary()["frozen"] is False
    assert campaign.summary()["frozen"] is False


def test_project_preview_accepts_structured_form_without_free_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from sheltersim.experiments.initial_scenario import InitialScenarioRequest
    from sheltersim.region.models import RegionResearchResult
    from sheltersim.storage.projects import digest
    from sheltersim.web.projects import ProjectAPI

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = store_at(tmp_path)
    api = ProjectAPI(store)
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
        evacuation_selection={
            "age_band_multipliers": {"child": 0, "adult": 1, "older_adult": 0}
        },
        field_origins={
            "municipality": "user",
            "disaster": "user",
            "evacuees": "user",
            "duration_days": "user",
        },
    )
    monkeypatch.setattr(
        api.workspace.research,
        "research",
        lambda query, **_kwargs: RegionResearchResult(request=query, researched_at=datetime.now(UTC)),
    )
    try:
        preview = api.workspace.preview(
            "project",
            {"text": "", "request": request.model_dump(mode="json")},
        )
    finally:
        api.close()

    scenario = preview["scenario"]
    assert scenario["model_configs"]["facility_queues"][
        "abandon_after_minutes_by_service"
    ] == {"toilet": 60}
    assert scenario["model_configs"]["body_dynamics"][
        "sleep_debt_stress_rate_by_agent"
    ] == {"A001": 0.003}
    assert scenario["model_configs"]["zone_environment"]["baseline_by_zone"][
        "sleeping"
    ] == {
        "ventilation_effectiveness": 0.7,
        "lighting_level": 0.5,
        "privacy_level": 0.2,
        "temperature_c": 30.0,
    }
    assert preview["request"]["model_parameter_overrides"][
        "facility_queues.abandon_after_minutes_by_service.*"
    ] == {"toilet": 60}
    assert preview["request"]["model_parameter_overrides"][
        "body_dynamics.sleep_debt_stress_rate_by_agent.*"
    ] == {"A001": 0.003}
    assert preview["request"]["model_parameter_overrides"][
        "zone_environment.baseline_by_zone.*"
    ] == {"sleeping": {"privacy_level": 0.2, "temperature_c": 30.0}}
    provenance = scenario["provenance"]["scenario_input"]
    normalized_request = InitialScenarioRequest.model_validate(preview["request"])
    expected_source = digest(normalized_request.model_dump(mode="json"))
    assert preview["can_start"] is True
    assert provenance["extraction_source"] == "structured_form"
    assert provenance["sha256"] == expected_source
    assert preview["llm_usage"]["api_request_count"] == 0


def test_saved_scalar_proofs_and_scope_survive_project_reload(tmp_path: Path) -> None:
    import json
    from copy import deepcopy

    from sheltersim.contracts.api import BranchReference
    from sheltersim.core.checkpoint import Checkpoint
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = store_at(tmp_path)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/scenarios/h00_small_replay.json"
    scenario = ScenarioSpec.load(fixture)
    saved = store.save_scenario("project", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    checkpoint = Checkpoint.capture(WorldState.from_scenario(scenario))
    store.save_branch("project", BranchReference(run_id="run", branch_id="main",
        fork_minute=0, computed_until=0), checkpoint)
    reference = {"run_id": "run", "branch_id": "main", "minute": 0, "kind": "kpi",
                 "reference_id": "boundary-kpis", "status": "recorded"}
    turn = {"question": "Recorded minute?", "answer": "Recorded minute = 0", "minute": 0,
            "evidence": [reference], "limitations": [], "usage": {}, "model_version": "fake",
            "conversation_id": "conversation", "context": {"run_id": "run", "branch_id": "main", "captured_minute": 0},
            "sources": [{"alias": "S1", "reference": reference}],
            "claims": [{"text": "Recorded minute = 0", "evidence": [{"alias": "S1", "field": "minute", "value": 0}]}]}
    store.save_turn("project", "run", "main", "turn", turn)
    restored = ProjectStore(tmp_path).conversations("project", "run", "main")[0]
    assert restored["schema_version"] == 2
    assert restored["claims"] == turn["claims"]
    assert restored["context"] == turn["context"]
    invalid = deepcopy(turn)
    invalid["claims"][0]["evidence"][0]["alias"] = "unknown"
    with pytest.raises(StorageError, match="invalid_conversation_proof"):
        store.save_turn("project", "run", "main", "invalid-turn", invalid)


def test_run_writer_lease_blocks_second_worker_and_preserves_live_status_on_recovery(tmp_path: Path) -> None:
    import json

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = store_at(tmp_path)
    scenario = ScenarioSpec.load(Path(__file__).resolve().parents[1] / "fixtures/scenarios/h00_small_replay.json")
    saved = store.save_scenario("project", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    lease = store.claim_run("project", "run")
    try:
        store.set_status("project", "run", "running")
        other = ProjectStore(tmp_path)
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.claim_run("project", "run")
        assert other.recover_interrupted() == []
        assert other.run("project", "run")["status"] == "running"
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.delete_run("project", "run", confirmation="run")
    finally:
        store.release_run(lease)
    assert ProjectStore(tmp_path).recover_interrupted() == [("project", "run")]
    assert store.run("project", "run")["status"] == "interrupted"


def _frozen_ledger(store: ProjectStore, account_id: str) -> UsageLedger:
    ledger = UsageLedger(store, "project", account_id)
    ledger.create(UsageLimits(max_calls=2, max_input_tokens=1000, max_output_tokens=1000,
                              budget_usd=1))
    receipt = ledger.reserve(request_key="k", operation="review", branch_id=None,
        stage="review", model_id="m", input_tokens=10, output_tokens=10, cost_usd=0.001)
    # Actual output above the reservation freezes the account, as a server-tool call did.
    ledger.settle(receipt, input_tokens=10, output_tokens=20, cost_usd=0.001, outcome="settled")
    return ledger


def test_unfreeze_requires_confirmation_and_keeps_recorded_spend(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = _frozen_ledger(store, "frozen")
    assert ledger.summary()["frozen"] is True
    with pytest.raises(StorageError, match="confirmation_mismatch"):
        ledger.unfreeze(confirm_account_id="other", reason="reviewed")
    with pytest.raises(StorageError, match="reason_required"):
        ledger.unfreeze(confirm_account_id="frozen", reason=" ")

    event = ledger.unfreeze(confirm_account_id="frozen", reason="server-tool passes reviewed")

    summary = ledger.summary()
    assert summary["frozen"] is False
    assert summary["totals"]["output_tokens"] == 20  # spend history is untouched
    assert summary["unfreeze_events"] == [event]
    assert event["receipt_count"] == 1
    with pytest.raises(StorageError, match="not_frozen"):
        ledger.unfreeze(confirm_account_id="frozen", reason="again")


def test_unfreeze_refuses_pending_reservations(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = _frozen_ledger(store, "busy")
    document = store._read(ledger.path)
    document["receipts"]["open"] = {**next(iter(document["receipts"].values())),
                                    "status": "pending", "pid": os.getpid()}
    store._write(ledger.path, document)

    with pytest.raises(StorageError, match="pending_reservations"):
        ledger.unfreeze(confirm_account_id="busy", reason="reviewed")


def test_unfreeze_keeps_the_ledger_v1_and_audits_beside_it(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = _frozen_ledger(store, "audited")

    ledger.unfreeze(confirm_account_id="audited", reason="reviewed")

    # Older readers of a machine-wide ledger only accept schema_version 1.
    assert store._read(ledger.path)["schema_version"] == 1
    assert "unfreeze_events" not in store._read(ledger.path)
    assert len(store._read(ledger.audit_path)["events"]) == 1
    assert [path.name for path in ledger.path.parent.iterdir()] == ["audited.json"]


def test_cli_unfreezes_the_campaign_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from sheltersim import cli
    from sheltersim.storage.usage import TASK_CAMPAIGN_ACCOUNT_ID

    monkeypatch.setattr(
        "sheltersim.storage.usage.task_campaign_store",
        lambda: ProjectStore(tmp_path / "state"),
    )
    campaign = task_campaign_ledger()
    receipt = campaign.reserve(request_key="k", operation="review", branch_id=None,
        stage="review", model_id="m", input_tokens=10, output_tokens=10, cost_usd=0.001)
    campaign.settle(receipt, input_tokens=10, output_tokens=20, cost_usd=0.001,
                    outcome="settled")
    assert campaign.summary()["frozen"] is True

    with pytest.raises(SystemExit):
        cli.main(["usage", "--unfreeze", TASK_CAMPAIGN_ACCOUNT_ID, "--confirm", "wrong",
                  "--reason", "reviewed"])
    assert campaign.summary()["frozen"] is True

    assert cli.main(["usage", "--unfreeze", TASK_CAMPAIGN_ACCOUNT_ID, "--confirm",
                     TASK_CAMPAIGN_ACCOUNT_ID, "--reason", "server-tool passes reviewed"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["unfreeze_event"]["reason"] == "server-tool passes reviewed"
    assert campaign.summary()["frozen"] is False


def test_cli_increases_campaign_budget_only_with_confirmation_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from sheltersim import cli
    from sheltersim.storage import usage
    from sheltersim.storage.usage import (
        TASK_CAMPAIGN_ACCOUNT_ID,
        UsageLedger,
        task_campaign_ledger,
    )

    monkeypatch.setattr(
        usage,
        "task_campaign_store",
        lambda: ProjectStore(tmp_path / "state"),
    )
    store = usage.task_campaign_store()
    store.create_project("runtime-accounts", _internal=True)
    campaign = UsageLedger(store, "runtime-accounts", TASK_CAMPAIGN_ACCOUNT_ID)
    campaign.create(UsageLimits(
        max_calls=100_000, max_input_tokens=10_000_000_000,
        max_output_tokens=10_000_000_000, budget_usd=50.0, max_concurrency=16,
    ))
    receipt = campaign.reserve(
        request_key="before-increase", operation="test", branch_id=None,
        stage="runtime", model_id="test-model", input_tokens=10, output_tokens=5,
        cost_usd=0.001,
    )
    campaign.settle(receipt, input_tokens=10, output_tokens=5, cost_usd=0.001,
                    outcome="settled")

    with pytest.raises(SystemExit):
        cli.main([
            "usage", "--increase-budget", TASK_CAMPAIGN_ACCOUNT_ID,
            "--new-budget-usd", "90", "--confirm", "wrong",
            "--reason", "owner approved additional allowance",
        ])
    assert campaign.summary()["limits"]["budget_usd"] == 50.0

    assert cli.main([
        "usage", "--increase-budget", TASK_CAMPAIGN_ACCOUNT_ID,
        "--new-budget-usd", "90", "--confirm", TASK_CAMPAIGN_ACCOUNT_ID,
        "--reason", "owner approved additional allowance",
    ]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["budget_increase_event"]["previous_budget_usd"] == 50.0
    summary = campaign.summary()
    assert summary["limits"]["budget_usd"] == 90.0
    assert summary["totals"]["calls"] == 1
    assert summary["budget_increase_events"][-1]["new_budget_usd"] == 90.0
    assert task_campaign_ledger().summary()["limits"]["budget_usd"] == 90.0


def test_ledger_keeps_requested_calls_free_for_later_work(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    ledger = UsageLedger(store, "project", "shared-run")
    ledger.create(UsageLimits(max_calls=5, max_input_tokens=10_000, max_output_tokens=10_000,
                              budget_usd=1))

    def reserve(index: int, keep: int) -> str:
        receipt = ledger.reserve(request_key=f"k{index}", operation="runtime", branch_id=None,
            stage="runtime", model_id="m", input_tokens=1, output_tokens=1, cost_usd=0.0001,
            keep_calls_free=keep)
        ledger.settle(receipt, input_tokens=1, output_tokens=1, cost_usd=0.0001,
                      outcome="settled")
        return receipt

    reserve(0, keep=3)
    reserve(1, keep=3)
    with pytest.raises(StorageError, match="usage_runtime_share_exhausted"):
        reserve(2, keep=3)
    for index in (3, 4, 5):  # the report and questions may use the held-back calls
        reserve(index, keep=0)
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reserve(6, keep=0)

    single = UsageLedger(store, "project", "single-call")
    single.create(UsageLimits(max_calls=1, max_input_tokens=10, max_output_tokens=10,
                              budget_usd=1))
    single.reserve(request_key="only", operation="runtime", branch_id=None, stage="runtime",
                   model_id="m", input_tokens=1, output_tokens=1, cost_usd=0.0001,
                   keep_calls_free=3)
