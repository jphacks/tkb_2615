"""The Project panel's evidence routes on a generated Run with a Branch that adds toilets:
history of every recorded kind, sampled people and households, the comparison, the
first divergence, and the approved-condition differences.

These are the requests project-round3.js sends. The routes only read the Run.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import StorageError, digest
from sheltersim.web.projects import ProjectAPI

END = 60
FORK_MINUTE = 10
ADDED_TOILETS = 2
TARGETS = [{"run_id": "r", "branch_id": "main"}, {"run_id": "r", "branch_id": "capacity"}]
INDEXED_KINDS = (
    "events",
    "kpis",
    "conversations",
    "social",
    "staff",
    "actions",
    "transitions",
    "dialogue",
    "care",
    "operations",
)


@pytest.fixture(scope="module")
def evidence_api(tmp_path_factory, generated_previews, add_toilets_program, saved_timeline):
    """A ProjectAPI over Run r of the generated Scenario, with a capacity Branch."""
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        store, timeline = saved_timeline(tmp_path_factory.mktemp("evidence") / "projects", scenario)
        timeline.advance("main", END, checkpoint_interval=10)
        program = add_toilets_program(ADDED_TOILETS, start_minute=FORK_MINUTE)
        timeline.fork(
            "main",
            FORK_MINUTE,
            "capacity",
            program=program,
            approved_program_hash=digest(program.model_dump(mode="json")),
        )
        timeline.advance("capacity", END)
        api = ProjectAPI(store)
        try:
            yield api, scenario
        finally:
            api.close()


def _history(api: ProjectAPI, branch_id: str, **query: object) -> dict:
    return api.get(
        ["p", "runs", "r", "branches", branch_id, "history"],
        {key: [str(value)] for key, value in query.items()},
    )


@pytest.mark.parametrize("kind", INDEXED_KINDS)
def test_every_indexed_history_kind_answers_for_both_branches(evidence_api, kind):
    api, _ = evidence_api
    for branch_id in ("main", "capacity"):
        result = _history(api, branch_id, kind=kind, start=FORK_MINUTE, end=END, limit=100)

        assert (result["run_id"], result["branch_id"], result["kind"]) == ("r", branch_id, kind)
        assert result["computed_until"] == END
        expected = "end_of_tick" if kind == "kpis" else "recorded_event_minute"
        assert result["time_semantics"] == expected
        assert all(item["branch_id"] == branch_id for item in result["items"])


def test_kpi_history_has_one_end_of_tick_row_per_computed_minute(evidence_api):
    api, _ = evidence_api
    rows = _history(api, "capacity", kind="kpis", start=FORK_MINUTE, end=END, limit=100)["items"]

    # The tick at END has not run, so its end-of-tick row does not exist yet.
    assert [row["minute_index"] for row in rows] == list(range(FORK_MINUTE, END))


@pytest.mark.parametrize("start,end", [(0, END), (FORK_MINUTE, END + 1)])
def test_history_outside_the_branch_range_is_refused(evidence_api, start, end):
    api, _ = evidence_api
    with pytest.raises(StorageError, match="history_outside_computed_range"):
        _history(api, "capacity", kind="kpis", start=start, end=end)


def test_people_and_household_history_samples_exact_minutes(evidence_api):
    api, scenario = evidence_api
    profile = scenario.profiles[0]

    people = _history(
        api, "capacity", kind="people", start=FORK_MINUTE, end=END, step=25, agent_id=profile.id
    )
    households = _history(
        api,
        "capacity",
        kind="households",
        start=FORK_MINUTE,
        end=END,
        step=25,
        household_id=profile.group_id,
    )

    assert [row["minute"] for row in people["items"]] == [10, 35, 60]
    assert all(row["exact"] and not row["absent"] for row in people["items"])
    assert all(row["people"][0]["id"] == profile.id for row in people["items"])
    members = {item.id for item in scenario.profiles if item.group_id == profile.group_id}
    for row in households["items"]:
        assert {person["id"] for person in row["people"]} == members


def test_comparison_of_two_branches_of_one_run_reports_matching_inputs(evidence_api):
    api, _ = evidence_api
    result = api.post(["p", "comparison-query"], {"targets": TARGETS, "minute": END})

    assert [frame["branch_id"] for frame in result["frames"]] == ["main", "capacity"]
    assert all(frame["minute"] == END for frame in result["frames"])
    (compatibility,) = result["compatibility"]
    assert compatibility["same_seed"] and compatibility["same_models"]
    assert compatibility["same_configuration"] and compatibility["same_initial_conditions"]


def test_first_kpi_divergence_is_the_added_toilets_at_the_fork(evidence_api):
    api, _ = evidence_api
    result = api.post(
        ["p", "first-divergence"],
        {"targets": TARGETS, "start": FORK_MINUTE, "end": END, "mode": "kpis", "scan_limit": 20},
    )

    divergence = result["first_divergence"]
    assert result["earliest_in_requested_interval"] and result["complete"]
    assert divergence["minute"] == FORK_MINUTE
    baseline, (candidate,) = divergence["baseline_sample"], divergence["candidate_samples"]
    changed = {key for key in baseline if baseline[key] != candidate[key]}
    assert changed == {"toilet_working_units"}
    assert candidate["toilet_working_units"] - baseline["toilet_working_units"] == ADDED_TOILETS


@pytest.mark.parametrize("mode", ["world", "person"])
def test_first_world_and_person_divergence_is_at_or_after_the_fork(evidence_api, mode):
    api, scenario = evidence_api
    body = {"targets": TARGETS, "start": FORK_MINUTE, "end": END, "mode": mode, "scan_limit": 20}
    if mode == "person":
        body["agent_id"] = scenario.profiles[0].id

    result = api.post(["p", "first-divergence"], body)

    assert result["first_divergence"]["minute"] >= FORK_MINUTE
    assert result["earliest_in_requested_interval"]


def test_condition_differences_are_only_the_approved_program(evidence_api):
    api, _ = evidence_api
    result = api.post(
        ["p", "condition-differences"], {"targets": TARGETS, "minute": END, "limit": 50}
    )

    (item,) = result["items"]
    assert item["path"] == "/approved_commands"
    assert item["baseline"]["value"] == []
    (command,) = item["candidate"]["value"]
    assert (command["command"], command["minute"]) == ("effect_program", FORK_MINUTE)


@pytest.mark.parametrize("step", [3, 7, 25])
def test_people_history_replayed_between_minutes_equals_restoring_each_minute(
    evidence_api, monkeypatch, step
):
    """Nearby minutes are replayed from the previous one; the samples equal restoring each."""
    import sheltersim.web.project_queries as queries

    api, scenario = evidence_api
    query = dict(kind="people", start=FORK_MINUTE, end=END, step=step)
    query["agent_id"] = scenario.profiles[0].id
    replayed = _history(api, "capacity", **query)
    monkeypatch.setattr(queries, "_REPLAY_RATHER_THAN_RESTORE", -1)
    restored = _history(api, "capacity", **query)
    assert replayed == restored
    assert replayed["items"]


def test_world_divergence_hashes_are_the_digests_of_each_branch_world(evidence_api):
    """World samples are hashed from the history cache; they equal digest(world.to_dict())."""
    from sheltersim.experiments.timeline import TimelineStore

    api, _ = evidence_api
    body = {"targets": TARGETS, "start": FORK_MINUTE, "end": END, "mode": "world"}

    divergence = api.post(["p", "first-divergence"], body)["first_divergence"]

    timeline = TimelineStore(api.store, "p", "r")
    minute = divergence["minute"]
    assert divergence["sample_hashes"] == [
        digest(timeline.seek(target["branch_id"], minute).world.to_dict()) for target in TARGETS
    ]
