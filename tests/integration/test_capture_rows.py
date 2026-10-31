"""A Checkpoint capture takes what Rust rows hold from the rows, and saves the same bytes.

Rows write their canonical JSON themselves (native/src/state/entities.rs, core/row.rs), and a
capture reads neither the objects nor the trees they mirror. Whatever the rows hold or fail to
hold, the Checkpoint is the one a capture from the Python objects makes, and a restore check does
not restore or compare again a row that is the one it showed equal.
"""

from __future__ import annotations

import copy
import json
import math
import random
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core import checkpoint_cache, state_mirror
from sheltersim.core import world as world_module
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache, canonical_bytes
from sheltersim.core.engine import TickEngine
from sheltersim.core.saved_items import SavedItems
from sheltersim.core.world import WorldState
from sheltersim.domain.models import SupportRequestStatus
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _IndexedMetricSink, _MetricSink
from sheltersim.storage.checkpoints import CheckpointArchive

needs_kernels = pytest.mark.skipif(
    not native.available(), reason="rows exist only with the sheltersim_native extension"
)
_ENTITIES = ("agents", "facilities", "staff", "support_requests", "active_tasks")


@pytest.fixture(scope="module")
def finished_document(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    document = run_scenario(scenario, tmp_path_factory.mktemp("run")).to_dict()
    assert document["support_requests"]
    assert document["active_tasks"] or document["agents"]
    return document


def _world(document: dict) -> WorldState:
    return WorldState.from_dict(copy.deepcopy(document))


def _without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoint_cache, "owned_row_bytes", lambda _world: ({}, {}, {}))


def _python_items(world: WorldState) -> dict[str, list[bytes]]:
    data = world._canonical_data(include_event_log=False)
    return {name: [canonical_bytes(item) for item in data[name]] for name in _ENTITIES}


@needs_kernels
@pytest.mark.parametrize("version", [6, 5])
def test_each_row_writes_the_bytes_of_its_python_document(finished_document, version) -> None:
    world = _world(finished_document)
    world.__dict__["schema_version"] = version
    assert world._core.entities().rows_json(version).keys() == set(_ENTITIES)
    assert dict(world._core.entities().rows_json(version)) == _python_items(world)


@needs_kernels
def test_rows_write_optional_fields_text_and_numbers_as_python_does(finished_document) -> None:
    world = _world(finished_document)
    agent = next(iter(world.agents.values()))
    agent.stress = 1e-05
    agent.trust = 0.1 + 0.2
    agent.target_id = 'おにぎり"\\\n\t\x01'
    agent.needs.hunger = 5e-324
    agent.next_decision_at = -(2**63)
    request = next(iter(world.support_requests.values()))
    request.last_assignment_attempt_minute = 3
    request.last_assignment_wait_reason_code = "no_available_qualified_staff"
    other = list(world.support_requests.values())[-1]
    other.last_assignment_attempt_minute = None
    other.last_assignment_wait_reason_code = None
    assert dict(world._core.entities().rows_json(6)) == _python_items(world)


@needs_kernels
def test_a_row_nothing_wrote_keeps_its_bytes_object(finished_document) -> None:
    world = _world(finished_document)
    store = world._core.entities()
    before = store.rows_json(6)
    after = store.rows_json(6)
    for name in _ENTITIES:
        assert all(map(lambda a, b: a is b, before[name], after[name]))
    agent = sorted(world.agents)[0]
    world.agents[agent].stress = 0.123
    again = store.rows_json(6)
    assert again["agents"][0] is not before["agents"][0]
    assert all(a is b for a, b in zip(before["agents"][1:], again["agents"][1:], strict=True))
    world.agents[agent].needs.hunger = 0.25
    assert store.rows_json(6)["agents"][0] is not again["agents"][0]
    facility = sorted(world.facilities)[0]
    third = store.rows_json(6)
    world.facilities[facility].queue_agent_ids.append(agent)
    assert store.rows_json(6)["facilities"][0] is not third["facilities"][0]


@needs_kernels
def test_a_rust_rule_writing_a_row_changes_its_bytes(finished_document) -> None:
    world = _world(finished_document)
    store = world._core.entities()
    agent = sorted(world.agents)[0]
    before = store.rows_json(6)["agents"][0]
    native.state("entities").add_agent_stress(world, agent, 0.01)
    after = store.rows_json(6)["agents"][0]
    assert after is not before
    assert dict(store.rows_json(6)) == _python_items(world)


@needs_kernels
def test_entities_added_and_removed_are_in_the_order_of_their_ids(finished_document) -> None:
    world = _world(finished_document)
    store = world._core.entities()
    store.rows_json(6)
    key = sorted(world.support_requests)[0]
    removed = world.support_requests.pop(key)
    assert dict(store.rows_json(6)) == _python_items(world)
    world.support_requests[key] = removed
    assert dict(store.rows_json(6)) == _python_items(world)
    assert json.loads(store.rows_json(6)["support_requests"][0])["id"] == key


def _capture_twice(document: dict, between: Any = None) -> list[Checkpoint]:
    world = _world(document)
    cache = CheckpointHistoryCache()
    metrics = MetricsRecorder()
    first = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
    if between is not None:
        between(world)
    return [first, Checkpoint.capture(world, metrics=metrics, history_cache=cache)]


def _settle_one(world: WorldState) -> None:
    world.metric_state["support_requests_total"] = 1.5
    agent = sorted(world.agents)[0]
    world.agents[agent].stress = 0.5
    request = sorted(world.support_requests)[-1]
    world.support_requests[request].cooldown_until += 1


def test_a_capture_from_rows_is_the_capture_from_the_objects(
    finished_document, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    with_rows = _capture_twice(finished_document, _settle_one)
    _without_rows(monkeypatch)
    without = _capture_twice(finished_document, _settle_one)
    monkeypatch.undo()
    for mine, theirs in zip(with_rows, without, strict=True):
        assert mine.state_hash == theirs.state_hash
        assert mine.checkpoint_hash == theirs.checkpoint_hash
        assert mine.world_state == theirs.world_state
        assert mine.to_json() == theirs.to_json()
        assert mine == theirs
    # A later reader sees the bytes of an ordinary Checkpoint, and restores the same state.
    assert (
        Checkpoint.from_json(with_rows[1].to_json()).checkpoint_hash == with_rows[1].checkpoint_hash
    )
    assert with_rows[1].restore_world().same_state(without[1].restore_world())


def test_the_archive_of_a_capture_from_rows_is_the_archive_of_the_objects(
    finished_document, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def archived(name: str) -> tuple[dict, set[str]]:
        saved = _capture_twice(finished_document, _settle_one)[1]
        directory = tmp_path / name
        archive = CheckpointArchive(directory, encoder=saved._encoder)
        manifest = archive._encode_validated_document(
            saved._document_view(), checkpoint_hash=saved.checkpoint_hash
        )
        return manifest, {path.name for path in directory.rglob("*.pack")}

    with_rows = archived("rows")
    _without_rows(monkeypatch)
    assert archived("objects") == with_rows


@needs_kernels
def test_entity_lists_of_a_capture_are_rows_bytes(finished_document) -> None:
    saved = _capture_twice(finished_document)[1]
    for name in _ENTITIES:
        assert isinstance(saved.world_state[name], SavedItems)
    assert saved.world_state["agents"] == finished_document["agents"]
    assert (
        saved.to_dict()["world_state"]["support_requests"] == finished_document["support_requests"]
    )


def test_a_tainted_store_or_namespace_saves_the_same_checkpoint(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    def taint(world: WorldState) -> None:
        if not native.available():
            return
        world._core.entities().taint_now("for the test")
        world._core.mirror("psychological_distress_v1").taint("for the test")
        world._core.mirror("metric_state").taint("for the test")

    tainted = _capture_twice(finished_document, taint)[1]
    _without_rows(monkeypatch)
    assert tainted.checkpoint_hash == _capture_twice(finished_document)[1].checkpoint_hash


@needs_kernels
def test_metric_state_assigned_whole_is_not_taken_from_its_old_row(finished_document) -> None:
    def assign(world: WorldState) -> None:
        world.metric_state = {**world.metric_state, "assigned": 1.0}

    saved = _capture_twice(finished_document, assign)[1]
    assert saved.world_state["metric_state"]["assigned"] == 1.0


@pytest.mark.parametrize("field", ["stress", "trust"])
def test_nan_in_a_row_fails_as_it_does_without_rows(
    finished_document, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    def outcome() -> str:
        world = _world(finished_document)
        setattr(next(iter(world.agents.values())), field, math.nan)
        try:
            Checkpoint.capture(world, history_cache=CheckpointHistoryCache())
        except Exception as error:  # noqa: BLE001 - compared below
            return f"{type(error).__name__}: {error}"
        return "saved"

    with_rows = outcome()
    _without_rows(monkeypatch)
    assert outcome() == with_rows != "saved"


@needs_kernels
def test_a_restore_check_restores_only_rows_that_changed(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    request_class = world_module.SupportRequestState

    def counted(**fields: Any) -> Any:
        built.append(fields["id"])
        return request_class(**fields)

    world = _world(finished_document)
    cache = CheckpointHistoryCache()
    monkeypatch.setattr(world_module, "SupportRequestState", counted)
    Checkpoint.capture(world, history_cache=cache)
    assert sorted(built) == sorted(world.support_requests)
    built.clear()
    Checkpoint.capture(world, history_cache=cache)
    assert built == []
    assert cache.known_equal_rows()["support_requests"] == set(world.support_requests)
    assert cache.known_equal_rows()["agents"] == set(world.agents)
    agent, request = sorted(world.agents)[0], sorted(world.support_requests)[0]
    world.agents[agent].stress = 0.5
    world.support_requests[request].cooldown_until += 1
    saved = Checkpoint.capture(world, history_cache=cache)
    assert built == [request]
    assert saved.restore_world().agents[agent].stress == 0.5
    assert saved.restore_world().support_requests[request].cooldown_until == (
        world.support_requests[request].cooldown_until
    )


@needs_kernels
def test_a_restore_check_world_has_no_rows_and_shares_nothing_with_the_live_world(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    worlds = []
    same_state = WorldState.same_state

    def keep(self: WorldState, other: WorldState, **known: Any) -> bool:
        worlds.append(self)
        return same_state(self, other, **known)

    monkeypatch.setattr(WorldState, "same_state", keep)
    world = _world(finished_document)
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    Checkpoint.capture(world, history_cache=cache)
    assert all("_core" not in restored.__dict__ for restored in worlds)
    first, second = worlds
    key = next(iter(world.support_requests))
    assert first.support_requests[key] is second.support_requests[key]
    assert second.support_requests[key] is not world.support_requests[key]
    assert second.support_requests[key]._store is None
    assert type(second.model_state) is dict
    assert type(second.metric_state) is dict


def test_a_changed_row_that_the_rules_refuse_is_refused_by_the_same_capture(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row is known equal only as it was; what changed since goes through the rules again."""

    def outcome() -> str:
        world = _world(finished_document)
        cache = CheckpointHistoryCache()
        Checkpoint.capture(world, history_cache=cache)
        Checkpoint.capture(world, history_cache=cache)
        request = world.support_requests[sorted(world.support_requests)[0]]
        request.agent_id = "nobody"
        try:
            Checkpoint.capture(world, history_cache=cache)
        except Exception as error:  # noqa: BLE001 - compared below
            return f"{type(error).__name__}: {error}"
        return "saved"

    with_rows = outcome()
    _without_rows(monkeypatch)
    assert outcome() == with_rows != "saved"


def test_a_failed_capture_forgets_what_the_restore_checks_showed(finished_document) -> None:
    world = _world(finished_document)
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    next(iter(world.agents.values())).stress = math.nan
    with pytest.raises((CheckpointError, ValueError)):
        Checkpoint.capture(world, history_cache=cache)
    assert cache._restored_rows == {}


def test_saves_of_a_running_world_are_the_saves_from_the_objects(
    generated_scenario_preview, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run() -> list[tuple[str, str]]:
        scenario = ScenarioSpec.model_validate(
            copy.deepcopy(generated_scenario_preview["scenario"])
        )
        world = WorldState.from_scenario(scenario)
        metrics = MetricsRecorder()
        controller = OfflineSimulationController(world, _MetricSink(metrics))
        engine = TickEngine(world, phase_handler=controller)
        cache = CheckpointHistoryCache()
        hashes = []
        for _ in range(60):
            engine.step()
            if world.minute_index % 15 == 0:
                saved = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
                hashes.append((saved.state_hash, saved.checkpoint_hash))
        return hashes

    with_rows = run()
    assert len(with_rows) == 4
    _without_rows(monkeypatch)
    assert run() == with_rows


@needs_kernels
def test_a_new_owned_row_is_captured_by_its_declaration_alone(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane adds a row and its line in OWNED_NAMESPACES; a capture needs nothing else."""
    monkeypatch.setitem(
        state_mirror.OWNED_NAMESPACES,
        "zone_environment_v1",
        ("sample_namespace", "VALUE_ROW"),
    )
    world = _world(finished_document)
    assert world._core.mirror("zone_environment_v1").row_json() is not None
    cache = CheckpointHistoryCache()
    saved = Checkpoint.capture(world, history_cache=cache)
    assert "zone_environment_v1" in cache._owned_rows
    assert (
        saved.world_state["model_state"]["zone_environment_v1"]
        == (finished_document["model_state"]["zone_environment_v1"])
    )
    monkeypatch.undo()
    assert saved.checkpoint_hash == Checkpoint.capture(_world(finished_document)).checkpoint_hash


@needs_kernels
def test_a_namespace_with_histories_hashed_apart_stays_on_the_python_path(
    finished_document, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        state_mirror.OWNED_NAMESPACES, "body_state_v1", ("sample_namespace", "VALUE_ROW")
    )
    world = _world(finished_document)
    cache = CheckpointHistoryCache()
    saved = Checkpoint.capture(world, history_cache=cache)
    assert "body_state_v1" not in cache._owned_rows
    monkeypatch.undo()
    assert saved.checkpoint_hash == Checkpoint.capture(_world(finished_document)).checkpoint_hash


# ---- What tells that a history did not change ------------------------------------------------


def _history() -> tuple[list, list]:
    items = [
        {"minute": index, "ids": ["a", "b"], "inner": {"x": 0.5, "y": None, "z": [True, {"k": 1}]}}
        for index in range(5)
    ]
    return items, json.loads(json.dumps(items))


@needs_kernels
def test_pins_say_unchanged_only_while_entries_hold_the_same_objects() -> None:
    items, documents = _history()
    pins = native.history_pins()
    pins.extend(items, documents, 0)
    assert len(pins) == 5
    assert pins.unchanged(items, 0, 5)
    for change in (
        lambda entries: entries[2].__setitem__("minute", 99),
        lambda entries: entries[2].__setitem__("new", 1),
        lambda entries: entries[2].pop("ids"),
        lambda entries: entries[2]["ids"].append("c"),
        lambda entries: entries[2]["ids"].__setitem__(0, "z"),
        lambda entries: entries[2]["inner"]["z"][1].__setitem__("k", 2),
        lambda entries: entries[2]["inner"].__setitem__("x", 0.25),
        lambda entries: entries[2].__setitem__("inner", dict(entries[2]["inner"])),
    ):
        changed, saved = _history()
        pins = native.history_pins()
        pins.extend(changed, saved, 0)
        change(changed)
        assert not pins.unchanged(changed, 0, 5)


@needs_kernels
def test_pins_hold_what_they_compare_so_an_address_cannot_come_back_with_a_new_value() -> None:
    items, documents = _history()
    pins = native.history_pins()
    pins.extend(items, documents, 0)
    for number in range(1000):
        items[1]["inner"]["x"] = float(number)
        assert not pins.unchanged(items, 0, 5)
        items[1]["inner"]["x"] = 0.5 + number


@needs_kernels
def test_pins_follow_a_history_that_drops_old_entries_and_grows() -> None:
    items, documents = _history()
    pins = native.history_pins()
    pins.extend(items, documents, 0)
    new = {"minute": 9, "ids": [], "inner": {}}
    pins.drop_front(2)
    current = [*items[2:], new]
    docs = [*documents[2:], json.loads(json.dumps(new))]
    pins.extend(current, docs, 3)
    assert len(pins) == 4
    assert pins.unchanged(current, 0, 4)
    assert not pins.unchanged(items, 0, 4)
    new["ids"].append("x")
    assert not pins.unchanged(current, 0, 4)
    many = [{"minute": index, "ids": [str(index)]} for index in range(100)]
    pins = native.history_pins()
    pins.extend(many, json.loads(json.dumps(many)), 0)
    for start in range(1, 90):
        pins.drop_front(1)
        assert pins.unchanged(many[start:], 0, 100 - start)


@needs_kernels
def test_pins_refuse_entries_their_documents_do_not_read_back_as() -> None:
    for entry, document in (
        ({"a": (1, 2)}, {"a": [1, 2]}),
        ({"a": 1}, {"a": 1.0}),
        ({"a": True}, {"a": 1}),
    ):
        pins = native.history_pins()
        pins.extend([entry], [document], 0)
        assert not pins.unchanged([entry], 0, 1)


@needs_kernels
def test_an_old_entry_equal_in_another_order_is_still_reused() -> None:
    """Pins say false for a new order, and comparing with the documents decides."""
    from sheltersim.core.checkpoint_cache import _SealedItems, _world_json

    items, _ = _history()
    sealed = _SealedItems(_world_json, still_equal=True)
    sealed.update(items)
    raws = list(sealed.raws)
    entry = items[1]
    reordered = dict(reversed(list(entry.items())))
    entry.clear()
    entry.update(reordered)
    assert sealed.compare(items) == 5
    sealed.update(items)
    assert sealed.raws == raws
    assert sealed.compare(items) == 5
    entry["minute"] = 77
    assert sealed.compare(items) == 0


# ---- The size of an observed state ------------------------------------------------------------


def _random_json(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.randrange(8 if depth < 4 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        return rng.randrange(-(10**20), 10**20)
    if kind == 3:
        return rng.choice([rng.random(), 1e300, 1e-300, -0.0, 1e16, 123456.0])
    if kind in (4, 5):
        return "".join(rng.choice('ab,:"\\\n{}[]避難所\x01 ') for _ in range(rng.randrange(12)))
    if kind == 6:
        return [_random_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        "".join(rng.choice('k,:"\\所') for _ in range(rng.randrange(1, 5))): _random_json(
            rng, depth + 1
        )
        for _ in range(rng.randrange(4))
    }


@needs_kernels
def test_the_dumps_length_kernel_counts_what_json_dumps_writes() -> None:
    rng = random.Random(2026)
    for _ in range(2000):
        value = _random_json(rng)
        assert native.dumps_length(value) == len(json.dumps(value, ensure_ascii=False).encode())
    assert native.dumps_length({"a": (1, 2)}) == len(json.dumps({"a": (1, 2)}))
    assert native.dumps_length({"a": math.nan}) is None
    assert native.dumps_length({"a": object()}) is None
    assert native.dumps_length({1: 2}) is None


class _Candidate:
    def __init__(self) -> None:
        self.action = SupportRequestStatus.PENDING
        self.eligible = True
        self.trigger = "t"
        self.utility = 0.5
        self.probability = 0.5
        self.urgency_priority = 0


class _Proposal:
    agent_id = "A"
    minute_index = 1
    action = SupportRequestStatus.PENDING
    target_id = None
    trigger = "t"
    policy_version = "v"
    observed_state_hash = "h"
    candidates = (_Candidate(),)
    next_decision_at = 2


@pytest.mark.parametrize(
    ("size", "omitted"),
    [(16384 - len('{"a": ""}'), False), (16385 - len('{"a": ""}'), True)],
)
def test_a_decision_omits_an_observed_state_over_the_limit_counted_with_json_dumps_spaces(
    finished_document, size: int, omitted: bool
) -> None:
    sink = _IndexedMetricSink(MetricsRecorder(), _world(finished_document))
    sink.record_decision(_Proposal(), observed_state={"a": "x" * size})
    row = sink.observations[-1]
    assert row["observed_state_status"] == ("omitted_size_limit" if omitted else "recorded")
    assert (row["observed_state"] is None) is omitted
