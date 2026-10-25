"""Offline provider, caps, audit, validation, and cache/replay contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMErrorPolicy, LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import (
    LLMCallRecord,
    LLMContractError,
    LLMProviderError,
    LLMStage,
    LLMTask,
    ProviderResponse,
)
from sheltersim.llm.generation import generate_initial_needs
from sheltersim.llm.provider import AnthropicProvider, LLMExecutionError, LLMRouter
from sheltersim.llm.schemas import SUPPORT_ADVICE_SCHEMA


class FakeProvider:
    def __init__(self, responses: list[str] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or ['{"priority":1,"rationale":"urgent need"}'])
        self.error = error
        self.calls: list[tuple[str, int, float]] = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        self.calls.append((model_id, max_output_tokens, timeout_seconds))
        if self.error is not None:
            raise self.error
        output = self.responses.pop(0)
        return ProviderResponse(
            text=output,
            model_id=model_id,
            input_tokens=10,
            output_tokens=4,
            elapsed_ms=12.5,
            request_id="req-test",
        )


def settings(**changes) -> LLMSettings:
    values = {
        "mode": LLMMode.RECORD,
        "api_key": "test-only-key",
        "max_api_calls": 2,
        "max_input_tokens": 256,
        "max_output_tokens": 64,
        "budget_usd": 0.02,
        "opus_price": ModelPrice(1.0, 2.0),
        "haiku_price": ModelPrice(0.1, 0.5),
    }
    values.update(changes)
    return LLMSettings(**values)


def task(*, state_hash: str = "world-1", text: str = "synthetic need data") -> LLMTask:
    return LLMTask(
        stage=LLMStage.RUNTIME,
        input_text=text,
        state_hash=state_hash,
        prompt_version="support-priority-v1",
        output_schema=SUPPORT_ADVICE_SCHEMA,
        fallback_text='{"priority":0,"rationale":"rule-based fallback"}',
        max_output_tokens=32,
    )


def test_auto_mode_is_offline_without_key_and_routes_models_by_stage() -> None:
    config = LLMSettings(mode="auto", api_key=None)
    router = LLMRouter(config)

    assert config.effective_mode is LLMMode.OFFLINE
    assert config.model_for("prepare") == config.opus_model
    assert config.model_for("review") == config.opus_model
    assert config.model_for("runtime") == config.haiku_model
    assert json.loads(router.complete(task()))["priority"] == 0
    assert router.api_request_count == 0
    assert router.records[0].outcome == "offline"


def test_environment_key_is_suppressed_outside_explicit_live_integration_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-used")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_llm_provider.py::unit (call)")
    monkeypatch.delenv("SHELTERSIM_ALLOW_LIVE_API_TESTS", raising=False)
    assert LLMSettings.from_env().api_key is None
    monkeypatch.setenv("SHELTERSIM_ALLOW_LIVE_API_TESTS", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/integration/test_live_api.py::test_live_models (call)")
    assert LLMSettings.from_env().api_key == "sk-ant-test-key-not-used"


def test_record_requires_key_unless_a_test_provider_is_injected() -> None:
    with pytest.raises(LLMProviderError, match="ANTHROPIC_API_KEY"):
        LLMRouter(LLMSettings(mode="record", api_key=None))

    router = LLMRouter(settings(), provider=FakeProvider(), tape=ResponseTape())
    assert json.loads(router.complete(task()))["priority"] == 1


def test_price_floor_prevents_undercount_and_unknown_model_is_not_called() -> None:
    underpriced = settings(opus_price=ModelPrice(0.001, 0.001))
    assert underpriced.price_for(underpriced.opus_model) == ModelPrice(4.0, 20.0)

    provider = FakeProvider()
    unknown = LLMSettings(mode="record", api_key="test", opus_model="claude-unpriced-model",
        budget_usd=1, cache_path=Path("unused.json"))
    router = LLMRouter(unknown, provider=provider, tape=ResponseTape())
    assert json.loads(router.complete(LLMTask(stage=LLMStage.PREPARE,
        input_text="request", state_hash="state", prompt_version="unknown-model-v1",
        fallback_text='{"priority":0,"rationale":"rule-based fallback"}',
        output_schema=SUPPORT_ADVICE_SCHEMA, max_output_tokens=32))) == {
            "priority": 0, "rationale": "rule-based fallback"}
    assert provider.calls == []
    assert router.records[0].outcome == "unknown_model_price"


def test_cache_hit_and_replay_reuse_exact_inputs_without_network() -> None:
    tape = ResponseTape()
    provider = FakeProvider()
    recorder = LLMRouter(settings(), provider=provider, tape=tape)
    first_output = recorder.complete(task())
    repeated_output = recorder.complete(task())

    assert first_output == repeated_output
    assert len(provider.calls) == 1
    assert recorder.records[0].outcome == "recorded"
    assert recorder.records[1].cache_hit
    assert recorder.records[1].api_request_count == 0
    assert recorder.records[0].input_tokens == 10
    assert recorder.records[0].estimated_cost_usd == pytest.approx(3e-6)

    replay_provider = FakeProvider(error=AssertionError("replay made an API call"))
    replay = LLMRouter(settings(mode=LLMMode.REPLAY), provider=replay_provider, tape=tape)
    assert replay.complete(task()) == first_output
    assert replay.api_request_count == 0
    assert replay.records[0].cache_hit


def test_non_search_tape_identity_remains_compatible_with_prior_cache_keys() -> None:
    task = LLMTask(stage=LLMStage.PREPARE, input_text="same input", state_hash="same state",
        prompt_version="same prompt", max_output_tokens=32)
    old_material = {
        "stage": "prepare", "model_id": "claude-opus-5", "state_hash": "same state",
        "input_hash": hashlib.sha256(b'"same input"').hexdigest(),
        "prompt_version": "same prompt", "schema_version": 1,
        "output_schema": None, "max_output_tokens": 32,
    }
    expected = hashlib.sha256(json.dumps(old_material, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    assert task.cache_key("claude-opus-5") == expected


def test_tape_snapshot_is_json_native_for_project_checkpoints() -> None:
    from sheltersim.storage.projects import safe_document

    tape = ResponseTape()
    tape.put("key", ProviderResponse(text="{}", model_id="claude-haiku-5-5", input_tokens=1,
                                     output_tokens=1, elapsed_ms=0.0))
    document = tape.to_dict()

    # Project storage rejects a Checkpoint whose sanitized copy differs from it.
    assert safe_document(document) == document
    assert ResponseTape.from_dict(document).get("key").citations == ()


def test_replay_cache_key_changes_when_state_or_prompt_changes() -> None:
    provider = FakeProvider()
    tape = ResponseTape()
    recorder = LLMRouter(settings(), provider=provider, tape=tape)
    recorder.complete(task())
    replay = LLMRouter(settings(mode="replay"), provider=provider, tape=tape)

    assert json.loads(replay.complete(task(state_hash="branched-state")))["priority"] == 0
    assert replay.records[0].outcome == "replay_miss"
    assert replay.api_request_count == 0


def test_checkpointed_router_state_replays_calls_without_an_external_service() -> None:
    recorder = LLMRouter(settings(), provider=FakeProvider(), tape=ResponseTape())
    recorded_output = recorder.complete(task())
    snapshot = recorder.snapshot_state()
    replay = LLMRouter(
        settings(mode=LLMMode.REPLAY),
        provider=FakeProvider(error=AssertionError("checkpoint replay made an API call")),
        tape=ResponseTape(),
    )

    replay.restore_snapshot(snapshot)

    assert replay.complete(task()) == recorded_output
    assert replay.api_request_count == 0
    assert replay.records[-1].cache_hit
    assert json.loads(replay.complete(task(state_hash="branch-changed")))["priority"] == 0
    assert replay.records[-1].outcome == "replay_miss"


def test_invalid_json_is_rejected_and_logged_before_rule_fallback() -> None:
    provider = FakeProvider(responses=["not json"])
    router = LLMRouter(settings(), provider=provider, tape=ResponseTape())

    assert json.loads(router.complete(task()))["priority"] == 0
    assert router.records[0].outcome == "invalid_output"
    assert router.records[0].fallback_used
    assert router.records[0].api_request_count == 1
    assert router.records[0].error_kind == "invalid_json"
    assert router.records[0].input_tokens == 10
    assert router.records[0].output_tokens == 4
    assert router.records[0].request_id == "req-test"
    assert router.records[0].estimated_cost_usd == pytest.approx(3e-6)


def test_schema_invalid_output_is_classified_and_retains_actual_usage() -> None:
    provider = FakeProvider(
        responses=['{"priority":1,"rationale":"urgent need","extra":"unsupported"}']
    )
    router = LLMRouter(settings(), provider=provider, tape=ResponseTape())

    assert json.loads(router.complete(task()))["priority"] == 0
    assert router.records[0].outcome == "invalid_output"
    assert router.records[0].error_kind == "schema_validation_failed"
    assert router.records[0].input_tokens == 10
    assert router.records[0].output_tokens == 4
    assert router.records[0].estimated_cost_usd == pytest.approx(3e-6)


def test_empty_text_response_is_fallback_with_reported_provider_usage() -> None:
    router = LLMRouter(
        settings(), provider=FakeProvider(responses=[""]), tape=ResponseTape()
    )

    assert json.loads(router.complete(task()))["priority"] == 0
    assert router.records[0].outcome == "invalid_output"
    assert router.records[0].error_kind == "empty_text_response"
    assert router.records[0].input_tokens == 10
    assert router.records[0].output_tokens == 4
    assert router.records[0].request_id == "req-test"


def test_rate_limit_is_bounded_no_retry_and_falls_back_with_cost_record() -> None:
    provider = FakeProvider(error=LLMProviderError("rate limited", status_code=429))
    router = LLMRouter(settings(), provider=provider, tape=ResponseTape())
    observed: list[LLMCallRecord] = []
    router.observer = observed.append

    assert json.loads(router.complete(task()))["priority"] == 0
    assert len(provider.calls) == 1
    assert router.api_request_count == 1
    assert router.records[0].outcome == "rate_limited"
    assert router.records[0].estimated_cost_usd > 0
    assert observed == router.records


def test_budget_and_call_caps_prevent_provider_calls_and_are_inspectable() -> None:
    provider = FakeProvider()
    limited = LLMRouter(
        settings(budget_usd=0.000001),
        provider=provider,
        tape=ResponseTape(),
    )

    assert json.loads(limited.complete(task()))["priority"] == 0
    assert limited.records[0].outcome == "budget_exceeded"
    assert limited.records[0].api_request_count == 0
    assert provider.calls == []

    one_call = LLMRouter(
        settings(max_api_calls=1),
        provider=FakeProvider(),
        tape=ResponseTape(),
    )
    one_call.complete(task())
    assert json.loads(one_call.complete(task(text="different input")))["priority"] == 0
    assert one_call.records[-1].outcome == "max_api_calls"
    assert one_call.api_request_count == 1


def test_stop_policy_raises_after_recording_failure() -> None:
    router = LLMRouter(
        settings(error_policy=LLMErrorPolicy.STOP, budget_usd=0.000001),
        provider=FakeProvider(),
        tape=ResponseTape(),
    )

    with pytest.raises(LLMExecutionError, match="budget_exceeded"):
        router.complete(task())
    assert router.records[0].outcome == "budget_exceeded"


def test_official_domain_search_captures_citations_and_itemized_billable_usage() -> None:
    citation = SimpleNamespace(type="web_search_result_location",
        url="https://www.city.example.lg.jp/shelter", title="Municipal shelter page",
        cited_text="Official page excerpt")
    foreign = SimpleNamespace(type="web_search_result_location",
        url="https://example.com/page", title="Unapproved page", cited_text="ignored")
    text_block = SimpleNamespace(type="text", text='{"ok":true}', citations=[citation, foreign])
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
        cache_creation_input_tokens=30, cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=10, ephemeral_1h_input_tokens=20),
        cache_read_input_tokens=5, server_tool_use=SimpleNamespace(web_search_requests=1))

    class Messages:
        request = None

        def create(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(content=[text_block], usage=usage, model="claude-opus-5-5",
                                   id="msg-test")

    messages = Messages()
    task = LLMTask(stage=LLMStage.PREPARE, input_text="Find official municipal sources",
        state_hash="region-state", prompt_version="source-candidates-v1",
        fallback_text='{"ok":false}', max_output_tokens=64,
        web_search_allowed_domains=("go.jp", "lg.jp"), max_web_searches=1)
    response = AnthropicProvider("test-key", client=SimpleNamespace(messages=messages)).complete(
        model_id="claude-opus-5-5", task=task, max_output_tokens=64, timeout_seconds=5)
    assert messages.request["tools"] == [{"type": "web_search_20250305", "name": "web_search",
        "max_uses": 1, "allowed_domains": ["go.jp", "lg.jp"]}]
    assert len(response.citations) == 1
    assert response.citations[0].url.endswith("/shelter")
    assert response.cache_creation_5m_input_tokens == 10
    assert response.cache_creation_1h_input_tokens == 20
    assert response.cache_read_input_tokens == 5
    assert response.web_search_requests == 1
    persisted = ResponseTape()
    persisted.put(task.cache_key(response.model_id), response)
    restored = ResponseTape.from_dict(persisted.to_dict())
    assert restored.get(task.cache_key(response.model_id)).citations == response.citations

    router = LLMRouter(LLMSettings(mode=LLMMode.RECORD, api_key="test",
        budget_usd=10, max_input_tokens=256, max_output_tokens=64),
        provider=AnthropicProvider("test-key", client=SimpleNamespace(messages=messages)),
        tape=ResponseTape())
    router.complete(task)
    assert router.records[0].web_search_requests == 1
    assert router.records[0].cache_creation_input_tokens == 30
    assert router.records[0].estimated_search_cost_usd == .01
    assert router.records[0].estimated_cost_usd == pytest.approx(.011011)


@pytest.mark.parametrize("search_enabled", [False, True])
def test_messages_use_streaming_when_sdk_supports_it(search_enabled: bool) -> None:
    usage = SimpleNamespace(input_tokens=12, output_tokens=3)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"ok":true}', citations=[])],
        usage=usage,
        model="claude-opus-5-5",
        id="msg-stream-test",
    )

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_final_message(self):
            return response

    class Messages:
        called = False
        request = None

        def stream(self, **kwargs):
            self.called = True
            self.request = kwargs
            if search_enabled:
                assert kwargs["tools"][0]["type"] == "web_search_20250305"
            else:
                assert "tools" not in kwargs
            return Stream()

        def create(self, **_kwargs):
            raise AssertionError("Messages should use streaming when the SDK supports it")

    task = LLMTask(
        stage=LLMStage.PREPARE if search_enabled else LLMStage.RUNTIME,
        input_text="A bounded test request",
        state_hash="stream-state",
        prompt_version="message-stream-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=64,
        web_search_allowed_domains=("go.jp",) if search_enabled else (),
        max_web_searches=1 if search_enabled else 0,
    )
    messages = Messages()
    result = AnthropicProvider("test-key", client=SimpleNamespace(messages=messages)).complete(
        model_id="claude-opus-5-5", task=task, max_output_tokens=64, timeout_seconds=60)

    assert messages.called
    if search_enabled:
        assert "thinking" not in messages.request
    else:
        assert messages.request["thinking"] == {"type": "disabled"}
    assert result.text == '{"ok":true}'
    assert result.input_tokens == 12


def test_prepare_generation_routes_through_opus_and_returns_a_reviewable_draft() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = []
    scenario = ScenarioSpec.model_validate(document)
    response = json.dumps(
        {
            "profiles": [
                {
                    "id": profile["id"],
                    "initial_needs": {
                        "toilet_urgency": 0.1,
                        "hunger": 0.2,
                        "fatigue": 0.3,
                    },
                }
                for profile in document["profiles"]
            ]
        }
    )
    provider = FakeProvider(responses=[response])
    router = LLMRouter(
        settings(max_input_tokens=4096, max_output_tokens=512, budget_usd=0.5),
        provider=provider,
        tape=ResponseTape(),
    )

    draft = generate_initial_needs(scenario, router)

    assert router.records[0].stage == "prepare"
    assert router.records[0].model_id == router.settings.opus_model
    assert [profile.initial_needs.hunger for profile in draft.profiles] == [0.2, 0.2]
    assert all(profile.provenance.source_kind == "llm" for profile in draft.profiles)
    assert draft.provenance["llm_initial_needs"]["prompt_version"] == "initial-needs-v1"


def _captured_request(stage: LLMStage) -> dict:
    text_block = SimpleNamespace(type="text", text='{"ok":true}')
    usage = SimpleNamespace(input_tokens=10, output_tokens=4)

    class Messages:
        request = None

        def create(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(content=[text_block], usage=usage, model="claude-opus-5-5",
                                   id="msg-test")

    messages = Messages()
    stage_task = LLMTask(stage=stage, input_text="request", state_hash="state",
        prompt_version="stage-request-v1", fallback_text='{"ok":false}', max_output_tokens=64)
    AnthropicProvider("test-key", client=SimpleNamespace(messages=messages)).complete(
        model_id="claude-opus-5-5", task=stage_task, max_output_tokens=64, timeout_seconds=5)
    return messages.request


def test_adapter_bounds_thinking_by_stage() -> None:
    runtime = _captured_request(LLMStage.RUNTIME)
    prepare = _captured_request(LLMStage.PREPARE)
    review = _captured_request(LLMStage.REVIEW)

    assert runtime["thinking"] == {"type": "disabled"} and "output_config" not in runtime
    # Opus rejects disabled thinking, so preparation keeps it short with low effort.
    assert prepare["output_config"] == {"effort": "low"} and "thinking" not in prepare
    assert "thinking" not in review and "output_config" not in review


def test_router_adds_thinking_headroom_outside_runtime() -> None:
    from sheltersim.llm.provider import THINKING_HEADROOM_TOKENS

    provider = FakeProvider(responses=['{"ok":true}'])
    router = LLMRouter(settings(budget_usd=1.0), provider=provider, tape=ResponseTape())
    router.complete(LLMTask(stage=LLMStage.PREPARE, input_text="request", state_hash="state",
        prompt_version="headroom-v1", fallback_text='{"ok":false}', max_output_tokens=32,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False}))
    runtime_provider = FakeProvider()
    LLMRouter(settings(), provider=runtime_provider, tape=ResponseTape()).complete(task())

    # The answer cap stays the task's own size; thinking gets room on top of it.
    assert provider.calls[0][1] == 32 + THINKING_HEADROOM_TOKENS
    assert runtime_provider.calls[0][1] == 32


def _schema_task(schema: dict) -> LLMTask:
    return LLMTask(stage=LLMStage.PREPARE, input_text="request", state_hash="state",
        prompt_version="schema-v1", output_schema=schema, fallback_text="{}")


def test_validator_follows_pydantic_refs_unions_and_typed_maps() -> None:
    from sheltersim.llm.contracts import validate_task_output

    schema = {
        "type": "object",
        "$defs": {"Share": {"type": "number", "minimum": 0, "exclusiveMaximum": 1}},
        "properties": {
            "shares": {"type": "object", "additionalProperties": {"$ref": "#/$defs/Share"}},
            "kind": {"oneOf": [{"const": "outage", "type": "string"},
                               {"const": "closure", "type": "string"}]},
            "code": {"type": "string", "pattern": "^[0-9]{5}$", "minLength": 5},
        },
        "required": ["shares", "kind"],
        "additionalProperties": False,
    }
    task = _schema_task(schema)

    validate_task_output(task, '{"shares":{"sleeping":0.5},"kind":"outage","code":"14100"}')
    for invalid in (
        '{"shares":{"sleeping":1.0},"kind":"outage"}',  # exclusiveMaximum
        '{"shares":{},"kind":"flood"}',  # no oneOf branch
        '{"shares":{},"kind":"outage","code":"14-00"}',  # pattern
        '{"shares":{},"kind":"outage","extra":1}',  # unknown field still rejected
    ):
        with pytest.raises(LLMContractError):
            validate_task_output(task, invalid)


def test_scenario_text_fallback_satisfies_its_own_schema() -> None:
    from sheltersim.llm import region_prepare
    from sheltersim.llm.contracts import validate_task_output

    explicit = region_prepare._parse_explicit_fields("横浜市で地震が発生。避難者30人、期間は1日。")
    fallback = json.dumps(region_prepare._to_llm_contract(explicit), ensure_ascii=False)

    # Before $ref support every reply, including this fallback, failed validation.
    validate_task_output(_schema_task(region_prepare._intent_json_schema()), fallback)


def test_adapter_keeps_search_results_when_the_json_answer_has_no_citations() -> None:
    def result(url: str, title: str) -> SimpleNamespace:
        return SimpleNamespace(type="web_search_result", url=url, title=title)

    search_block = SimpleNamespace(type="web_search_tool_result", content=[
        result("https://www.city.example.lg.jp/bousai", "Municipal disaster plan"),
        result("https://example.com/blog", "Unapproved page"),
        result("https://www.city.example.lg.jp/bousai", "Duplicate"),
    ])
    error_block = SimpleNamespace(type="web_search_tool_result",
                                  content=SimpleNamespace(type="web_search_tool_result_error"))
    text_block = SimpleNamespace(type="text", text='{"ok":true}', citations=None)
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            server_tool_use=SimpleNamespace(web_search_requests=1))

    class Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[search_block, error_block, text_block], usage=usage,
                                   model="claude-opus-5-5", id="msg-test")

    task = LLMTask(stage=LLMStage.PREPARE, input_text="Find official municipal sources",
        state_hash="region-state", prompt_version="source-candidates-v1",
        fallback_text='{"ok":false}', max_output_tokens=64,
        web_search_allowed_domains=("go.jp", "lg.jp"), max_web_searches=1)
    response = AnthropicProvider("test-key", client=SimpleNamespace(messages=Messages())).complete(
        model_id="claude-opus-5-5", task=task, max_output_tokens=64, timeout_seconds=5)

    assert [citation.url for citation in response.citations] == [
        "https://www.city.example.lg.jp/bousai"
    ]
    assert response.citations[0].cited_text == ""


def test_task_input_byte_limit_lets_a_bounded_large_context_reach_the_provider() -> None:
    oversized = "x" * 1_000
    provider = FakeProvider(['{"priority":1,"rationale":"urgent need"}'] * 2)
    router = LLMRouter(settings(budget_usd=1.0), provider=provider, tape=ResponseTape())

    # The generic guard (256 bytes here) still stops a task that declares no limit.
    router.complete(task(text=oversized))
    assert provider.calls == []
    assert router.records[-1].outcome == "input_token_limit"

    declared = LLMTask(
        **{**task(text=oversized).__dict__, "input_byte_limit": 2_000}
    )
    router.complete(declared)
    assert len(provider.calls) == 1
    assert router.records[-1].outcome == "recorded"
    # The limit changes no response, so recorded tapes keep their keys.
    assert declared.cache_key("m") == task(text=oversized).cache_key("m")

    beyond = LLMTask(**{**task(text="y" * 3_000).__dict__, "input_byte_limit": 2_000})
    router.complete(beyond)
    assert len(provider.calls) == 1
    assert router.records[-1].outcome == "input_token_limit"


def test_task_input_byte_limit_never_lowers_a_larger_setting() -> None:
    provider = FakeProvider()
    router = LLMRouter(settings(max_input_tokens=4_096, budget_usd=1.0), provider=provider,
                       tape=ResponseTape())

    router.complete(LLMTask(**{**task(text="z" * 3_000).__dict__, "input_byte_limit": 1_000}))

    assert len(provider.calls) == 1


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_task_input_byte_limit_must_be_a_positive_integer(value) -> None:
    with pytest.raises(ValueError, match="input_byte_limit"):
        LLMTask(**{**task().__dict__, "input_byte_limit": value})
