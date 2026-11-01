"""A save's validation skips only history entries it can show are the ones that passed.

Whatever is done to an old entry or to what its rules read, a capture with a writer's
history cache reaches the verdict of the plain complete validation, in the same save.
Without the Rust kernels there is no memo, so the verdicts hold in both modes; the tests
of what is not read need the kernels.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain import common_operations, resources
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink

_OPERATIONS = ("shared_operations_v1", "history")
_AUDIT = ("resource_ledger_v1", "audit")
_REQUESTS = ("resource_service_v1", "request_history")
_STALLS = ("resource_service_v1", "queue_stall_history")
_BODY = ("body_state_v1", "history")

needs_kernels = pytest.mark.skipif(
    not native.available(), reason="the memo exists only with the sheltersim_native extension"
)


@pytest.fixture(scope="module")
def finished_world(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    document = run_scenario(scenario, tmp_path_factory.mktemp("run")).to_dict()
    for namespace, name in (_OPERATIONS, _AUDIT, _REQUESTS, _STALLS, _BODY):
        assert len(document["model_state"][namespace][name]) > 8, (namespace, name)
    return document


def _restored(document: dict) -> WorldState:
    return WorldState.from_dict(copy.deepcopy(document))


def _saved(document: dict) -> tuple[WorldState, CheckpointHistoryCache]:
    """A world saved once by a writer, so the writer's memo holds its histories."""
    world = _restored(document)
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    return world, cache


def _entry(world: WorldState, path: tuple[str, str], index: int = 3) -> dict:
    return world.model_state[path[0]][path[1]][index]


def _set(path: tuple[str, str], name: str, value: object):
    def corrupt(world: WorldState) -> None:
        _entry(world, path)[name] = value

    return corrupt


def _as_tuple(path: tuple[str, str], name: str):
    def corrupt(world: WorldState) -> None:
        entry = _entry(world, path)
        entry[name] = tuple(entry[name])

    return corrupt


def _append_to(path: tuple[str, str], name: str, value: object):
    def corrupt(world: WorldState) -> None:
        _entry(world, path)[name].append(value)

    return corrupt


def _to_bool(path: tuple[str, str], name: str):
    """1 for True, which equal each other, in a field holding 0 or 1."""

    def corrupt(world: WorldState) -> None:
        for entry in world.model_state[path[0]][path[1]]:
            if entry[name] == 1 and type(entry[name]) is int:
                entry[name] = True
                return
        raise AssertionError(f"no entry of {path} has {name} 1")

    return corrupt


def _removed(path: tuple[str, str]):
    def corrupt(world: WorldState) -> None:
        del world.model_state[path[0]][path[1]][3]

    return corrupt


def _swapped(path: tuple[str, str]):
    def corrupt(world: WorldState) -> None:
        history = world.model_state[path[0]][path[1]]
        history[3], history[-2] = history[-2], history[3]

    return corrupt


def _new_entry(path: tuple[str, str], name: str, value: object):
    def corrupt(world: WorldState) -> None:
        history = world.model_state[path[0]][path[1]]
        history.append({**copy.deepcopy(history[-1]), name: value})

    return corrupt


def _clock_back(world: WorldState) -> None:
    world.minute_index = 0


def _zone_removed(world: WorldState) -> None:
    zone_id = _entry(world, _REQUESTS)["origin_zone_id"]
    del world.zones[zone_id]


def _facility_kind(world: WorldState) -> None:
    facility_id = next(
        item["facility_id"]
        for item in world.model_state[_REQUESTS[0]][_REQUESTS[1]]
        if item["facility_id"]
    )
    world.facilities[facility_id].kind = "toilet"


def _resident_forgotten(world: WorldState) -> None:
    world.model_state["population_dynamics_v1"]["initial_agent_ids"].remove(
        _entry(world, _AUDIT, -1)["agent_id"]
    )


_CHANGES = {
    # Shared operations: old records once read only here (core/tick_validation.py).
    "operation_unknown_event": _set(_OPERATIONS, "event_id", "unknown-event"),
    "operation_version_true": _to_bool(_OPERATIONS, "schema_version"),
    "operation_minute_true": _set(_OPERATIONS, "minute", True),
    "operation_minute_float": _set(_OPERATIONS, "minute", 3.0),
    "operation_sources_tuple": _as_tuple(_OPERATIONS, "source_ids"),
    "operation_sources_grown": _append_to(_OPERATIONS, "source_ids", "ASM-UNKNOWN"),
    "operation_removed": _removed(_OPERATIONS),
    "operation_swapped": _swapped(_OPERATIONS),
    "operation_new_broken": _new_entry(_OPERATIONS, "event_id", "unknown-event"),
    # Resource ledger audit.
    "audit_quantity": _set(_AUDIT, "quantity", 7),
    "audit_quantity_true": _to_bool(_AUDIT, "quantity"),
    "audit_sources_tuple": _as_tuple(_AUDIT, "source_ids"),
    "audit_sources_grown": _append_to(_AUDIT, "source_ids", "ASM-UNKNOWN"),
    "audit_removed": _removed(_AUDIT),
    "audit_new_broken": _new_entry(_AUDIT, "operation", "unknown"),
    # Service histories.
    "request_unknown_agent": _set(_REQUESTS, "agent_id", "nobody"),
    "request_minute_true": _set(_REQUESTS, "minute", True),
    "request_detail_type": _set(_REQUESTS, "detail", 1),
    "request_new_broken": _new_entry(_REQUESTS, "action", "unknown"),
    "stall_reason": _set(_STALLS, "reason", "unknown"),
    "stall_order": _swapped(_STALLS),
    "stall_new_broken": _new_entry(_STALLS, "minute", -1),
    # Body history.
    "body_unknown_actor": _set(_BODY, "actor_ids", ["nobody"]),
    "body_actors_tuple": _as_tuple(_BODY, "actor_ids"),
    "body_minute_true": _set(_BODY, "minute", True),
    "body_new_broken": _new_entry(_BODY, "deltas", []),
    # What the rules of unchanged old entries read.
    "clock_moved_back": _clock_back,
    "zone_of_old_request_removed": _zone_removed,
    "facility_kind_of_old_request": _facility_kind,
    "resident_of_old_audit_forgotten": _resident_forgotten,
}


@pytest.mark.parametrize("change", _CHANGES)
def test_a_save_reaches_the_verdict_of_the_plain_validation(finished_world, change) -> None:
    plain = _restored(finished_world)
    _CHANGES[change](plain)
    try:
        plain.validate()
    except StateInvariantError as exc:
        expected = str(exc)
    else:
        expected = None

    world, cache = _saved(finished_world)
    _CHANGES[change](world)
    if expected is None:
        Checkpoint.capture(world, history_cache=cache)
    else:
        with pytest.raises(StateInvariantError) as raised:
            Checkpoint.capture(world, history_cache=cache)
        assert str(raised.value) == expected


@pytest.mark.parametrize(
    "change",
    ["operation_unknown_event", "audit_quantity", "request_unknown_agent", "stall_reason"],
)
def test_the_verdict_holds_after_several_saves(finished_world, change) -> None:
    world, cache = _saved(finished_world)
    Checkpoint.capture(world, history_cache=cache)
    _CHANGES[change](world)
    with pytest.raises(StateInvariantError):
        Checkpoint.capture(world, history_cache=cache)


def test_a_valid_change_to_an_old_entry_reaches_the_saved_bytes(finished_world) -> None:
    world, cache = _saved(finished_world)
    before = Checkpoint.capture(world, history_cache=cache)
    _entry(world, _BODY)["deltas"]["added_by_the_test"] = 1.0
    after = Checkpoint.capture(world, history_cache=cache)
    assert after.state_hash != before.state_hash
    assert after.state_hash == Checkpoint.capture(_restored(after.world_state)).state_hash


@pytest.fixture
def reads(monkeypatch) -> dict[str, list[int]]:
    """How many entries the rules read: audit records, and each parsed operations history."""
    from sheltersim.contracts.operations import SharedOperationsState
    from sheltersim.core.save_validation import SaveValidationMemo

    counts: dict[str, list[int]] = {"audit": [], "operations": [], "unread": []}
    audit_rule = resources._validate_audit_record

    def audit_record(record, ledger):
        counts["audit"].append(1)
        return audit_rule(record, ledger)

    monkeypatch.setattr(resources, "_validate_audit_record", audit_record)
    parse = SharedOperationsState.model_validate.__func__

    def parsed(cls, value, *args, **kwargs):
        counts["operations"].append(len(value["history"]))
        return parse(cls, value, *args, **kwargs)

    monkeypatch.setattr(SharedOperationsState, "model_validate", classmethod(parsed))
    unread = SaveValidationMemo.unread

    def counted(self, path, history, inputs):
        result = unread(self, path, history, inputs)
        counts["unread"].append(len(result))
        return result

    monkeypatch.setattr(SaveValidationMemo, "unread", counted)
    return counts


def _clear(counts: dict[str, list[int]]) -> None:
    for values in counts.values():
        values.clear()


@needs_kernels
@pytest.mark.python_rules
def test_unchanged_old_entries_are_not_read_again(finished_world, reads) -> None:
    world, cache = _saved(finished_world)
    assert reads["audit"] and reads["operations"][-1] > 8
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert reads["audit"] == []
    assert reads["operations"] == [0]
    # Each history's last entry is read, for ordering across the join.
    assert reads["unread"] == [1, 1, 1]


@needs_kernels
@pytest.mark.python_rules
def test_a_changed_entry_or_input_reads_that_history_again(finished_world, reads) -> None:
    world, cache = _saved(finished_world)
    audit = world.model_state[_AUDIT[0]][_AUDIT[1]]
    audit[3] = copy.deepcopy(audit[3])
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert len(reads["audit"]) == len(audit)

    # The EventLog that operations records link to is another object.
    world.event_log = copy.deepcopy(world.event_log)
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert reads["operations"][0] == len(world.model_state[_OPERATIONS[0]][_OPERATIONS[1]])


@needs_kernels
@pytest.mark.python_rules
def test_a_changed_staff_history_reads_the_operations_history_again(
    finished_world, reads, monkeypatch
) -> None:
    """An old StaffOperationReceipt is compared with the retained staff history."""
    world, cache = _saved(finished_world)
    staff_history = world.model_state["staff_operations_v1"]["history"]
    before = common_operations._staff_operations_json(world)
    staff_history.append({"task_id": "t"})
    changed = common_operations._staff_operations_json(world)
    assert changed != before
    staff_history.clear()
    monkeypatch.setattr(common_operations, "_staff_operations_json", lambda _world: changed)
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert reads["operations"][0] == len(world.model_state[_OPERATIONS[0]][_OPERATIONS[1]])


@needs_kernels
@pytest.mark.python_rules
def test_a_truncated_operations_history_is_replayed_in_full(finished_world, reads) -> None:
    """Records dropped from the front change where a replay starts."""
    world, cache = _saved(finished_world)
    state = world.model_state["shared_operations_v1"]
    state["history_truncated"] = True
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    Checkpoint.capture(world, history_cache=cache)
    assert reads["operations"] == [len(state["history"])] * 2


@needs_kernels
@pytest.mark.python_rules
def test_a_failed_capture_leaves_nothing_proven(finished_world, reads) -> None:
    world, cache = _saved(finished_world)
    entry = _entry(world, _AUDIT)
    quantity = entry["quantity"]
    entry["quantity"] = quantity + 1
    with pytest.raises(StateInvariantError):
        Checkpoint.capture(world, history_cache=cache)
    entry["quantity"] = quantity
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert len(reads["audit"]) == len(world.model_state[_AUDIT[0]][_AUDIT[1]])


@needs_kernels
@pytest.mark.python_rules
def test_a_capture_that_fails_after_validation_leaves_nothing_proven(
    finished_world, reads, monkeypatch
) -> None:
    world, cache = _saved(finished_world)
    with monkeypatch.context() as patch:

        def fail(*_args, **_kwargs):
            raise CheckpointError("restore check failed")

        patch.setattr(Checkpoint, "_validate", fail)
        with pytest.raises(CheckpointError):
            Checkpoint.capture(world, history_cache=cache)
    _clear(reads)
    Checkpoint.capture(world, history_cache=cache)
    assert len(reads["audit"]) == len(world.model_state[_AUDIT[0]][_AUDIT[1]])


@needs_kernels
@pytest.mark.python_rules
def test_a_restored_world_starts_with_a_complete_validation(finished_world, reads) -> None:
    world, cache = _saved(finished_world)
    checkpoint = Checkpoint.capture(world, history_cache=cache)
    restored = checkpoint.restore_world()
    _clear(reads)
    Checkpoint.capture(restored, history_cache=CheckpointHistoryCache())
    assert len(reads["audit"]) == len(restored.model_state[_AUDIT[0]][_AUDIT[1]])
    assert reads["operations"][0] == len(restored.model_state[_OPERATIONS[0]][_OPERATIONS[1]])


def test_the_run_end_validation_still_reads_every_entry(finished_world) -> None:
    world, cache = _saved(finished_world)
    Checkpoint.capture(world, history_cache=cache)
    world.validate()
    _entry(world, _OPERATIONS)["event_id"] = "unknown-event"
    with pytest.raises(StateInvariantError):
        world.validate()


def test_saves_during_a_run_equal_saves_without_any_memo(generated_scenario) -> None:
    """New entries continue each replay: every save gives the bytes of a full capture."""
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 240
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    metrics = MetricsRecorder()
    controller = OfflineSimulationController(world, _MetricSink(metrics))
    engine = TickEngine(world, phase_handler=controller)
    cache = CheckpointHistoryCache()
    while world.minute_index < world.duration_minutes:
        engine.step()
        if world.minute_index % 30 == 0:
            saved = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
            plain = Checkpoint.capture(world, metrics=metrics)
            assert saved.checkpoint_hash == plain.checkpoint_hash
    state = world.model_state[_OPERATIONS[0]]
    assert state["history"], "the Run must have appended to the histories"
    # The replays that continued hold what a replay from the start reaches.
    if cache._validation is not None:
        kept = cache._validation._replays
        count, (_, care, _) = kept[_OPERATIONS]
        assert count == len(state["history"])
        fresh = CheckpointHistoryCache()
        Checkpoint.capture(world, metrics=metrics, history_cache=fresh)
        assert fresh._validation._replays[_OPERATIONS][1][1] == care
        assert fresh._validation._replays[_AUDIT][1] == kept[_AUDIT][1]
