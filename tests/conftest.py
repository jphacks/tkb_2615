from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# The structured form the demo-v2 Scenario screen posts (scenarioRequest in
# demo-v2.js): only the fields an operator types, each marked as user input.
# Small enough that running the whole Scenario takes seconds.
GENERATED_SCENARIO_REQUEST: dict[str, Any] = {
    "municipality": "合成市",
    "disaster": "地震",
    "evacuees": 20,
    "duration_days": 0.25,
    "field_origins": {
        "municipality": "user",
        "disaster": "user",
        "evacuees": "user",
        "duration_days": "user",
    },
}


def _isolate(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(root / "projects"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("SHELTERSIM_LLM_CACHE_PATH", str(root / "llm-tape.json"))
    # macOS production storage deliberately ignores XDG_STATE_HOME. Tests must
    # still isolate the shared ledger and honour per-test XDG overrides.
    from sheltersim.storage import usage
    from sheltersim.storage.projects import ProjectStore

    monkeypatch.setattr(
        usage,
        "task_campaign_store",
        lambda: ProjectStore(Path(os.environ["XDG_STATE_HOME"]) / "sheltersim"),
    )


@pytest.fixture(autouse=True)
def _isolated_local_state(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every test away from the developer's Projects, usage ledgers and LLM cache.

    Without this, a CLI test wrote a usage account under the repository's runs/.projects,
    and parallel workers (pytest -n) would share those default locations. Tests that set
    these variables themselves still override them.

    Paid live_api tests keep the real locations on purpose: their spend must count
    against this PC's campaign ledger.
    """
    if request.node.get_closest_marker("live_api") is not None:
        return
    _isolate(monkeypatch, tmp_path_factory.mktemp("local-state"))


def _generated_request(**fields: Any) -> dict[str, Any]:
    """GENERATED_SCENARIO_REQUEST with some fields changed and marked as user input."""
    request = copy.deepcopy(GENERATED_SCENARIO_REQUEST)
    request.update(copy.deepcopy(fields))
    request["field_origins"] = {**request["field_origins"], **{name: "user" for name in fields}}
    return request


@contextmanager
def _ui_project_api(monkeypatch: pytest.MonkeyPatch, root: Path) -> Iterator[Any]:
    """The Project screen's API over a fresh store, with an empty regional research.

    The real research calls public web services, so it is replaced by an empty
    result; everything else goes through the same code as the UI.
    """
    from sheltersim.region.models import RegionResearchResult
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    _isolate(monkeypatch, root)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = ProjectStore(root / "store")
    store.create_project("project")
    api = ProjectAPI(store)
    api.workspace.research.research = lambda query, **_kwargs: RegionResearchResult(
        request=query, researched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    try:
        yield api
    finally:
        api.close()


def _ui_preview(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run the Project screen's Scenario preview for one structured form."""
    with pytest.MonkeyPatch.context() as monkeypatch, _ui_project_api(monkeypatch, root) as api:
        return api.workspace.preview("project", {"text": "", "request": request})


@pytest.fixture(scope="session")
def generated_previews(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., dict[str, Any]]:
    """Previews of GENERATED_SCENARIO_REQUEST with some form fields changed.

    Each changed field is marked as user input, as the demo-v2 form does. Every
    distinct set of fields is built once per worker and shared, so do not change
    the returned preview.
    """
    cache: dict[str, dict[str, Any]] = {}

    def preview(**fields: Any) -> dict[str, Any]:
        key = json.dumps(fields, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            cache[key] = _ui_preview(
                tmp_path_factory.mktemp("generated-scenario"), _generated_request(**fields)
            )
        return cache[key]

    return preview


@pytest.fixture(scope="session")
def generated_scenario_preview(generated_previews: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """The Scenario preview the Project screen gets for GENERATED_SCENARIO_REQUEST.

    It goes through the same ProjectAPI preview as the UI, so the Scenario has a
    floor plan (facility_geometry) and facility awareness (agent_dynamics) like
    every Scenario an operator creates. Only the regional research is replaced by
    an empty result, because the real one calls public web services.
    Built once per worker; use generated_scenario for a Scenario to change.
    """
    return generated_previews()


@pytest.fixture
def generated_scenario(generated_scenario_preview: dict[str, Any]):
    """A fresh ScenarioSpec of the shared generated preview, safe to change in one test."""
    from sheltersim.experiments.scenario import ScenarioSpec

    return ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))


@pytest.fixture
def generated_scenario_with(generated_previews: Callable[..., dict[str, Any]]):
    """Build a fresh ScenarioSpec from the shared form with some fields changed.

    For example generated_scenario_with(duration_days=4) is what an operator gets
    after typing 4 into the duration field.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    def build(**fields: Any):
        return ScenarioSpec.model_validate(copy.deepcopy(generated_previews(**fields)["scenario"]))

    return build


@pytest.fixture
def count_validations(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[int]]:
    """Record the id of every object a pydantic model validates from now on.

    For tests of parsed-config caches; monkeypatch.undo() stops the counting.
    """

    def count(model: Any) -> list[int]:
        calls: list[int] = []
        original = model.model_validate.__func__

        def validate(cls, value, *args, **kwargs):
            calls.append(id(value))
            return original(cls, value, *args, **kwargs)

        monkeypatch.setattr(model, "model_validate", classmethod(validate))
        return calls

    return count


@pytest.fixture
def ui_project_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The Project screen's API with an empty project named "project", open for one test.

    Use it when a test must call further routes on the preview it creates, which
    the cached generated_previews cannot do. Build the form with generated_request.
    """
    with _ui_project_api(monkeypatch, tmp_path) as api:
        yield api


@pytest.fixture
def project_store_at() -> Callable[[Path], Any]:
    """Open a ProjectStore at a folder, with an empty project named "project"."""

    def open_store(root: Path) -> Any:
        from sheltersim.storage.projects import ProjectStore

        store = ProjectStore(root)
        store.create_project("project")
        return store

    return open_store


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs: object) -> Any:
        from sheltersim.llm.contracts import ProviderResponse

        self.calls += 1
        return ProviderResponse(
            text='{"ok":true}', model_id="fake", input_tokens=5, output_tokens=3, elapsed_ms=0
        )


@pytest.fixture
def counting_provider() -> _CountingProvider:
    """A fake LLM provider that answers {"ok":true} and counts its calls."""
    return _CountingProvider()


@pytest.fixture
def generated_request() -> Callable[..., dict[str, Any]]:
    """The structured form of GENERATED_SCENARIO_REQUEST with some fields changed."""
    return _generated_request


@pytest.fixture
def generated_scenario_with_split_queues(generated_scenario):
    """The generated Scenario with two service points, each its own queue, at two facilities.

    The generator gives every facility one service point. toilet_01 and
    water_station_main get a left and a right point here instead, one metre either
    side of the facility, so each serves two people at once, and each point needs a
    worker with the right skill; a toilet worker is added to the toilet area for that.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    document = generated_scenario.model_dump(mode="json")
    geometry = document["model_configs"]["facility_geometry"]
    zones = {facility["id"]: facility["zone_id"] for facility in document["facilities"]}
    facilities = {facility["id"]: facility for facility in document["facilities"]}
    for facility_id, skill in (
        ("toilet_01", "toilet_service"),
        ("water_station_main", "water_service"),
    ):
        facilities[facility_id]["capacity"] = facilities[facility_id]["working_units"] = 2
        point = geometry["facility_points"][facility_id]
        geometry["service_points"] = [
            row for row in geometry["service_points"] if row["facility_id"] != facility_id
        ]
        for side, offset in (("left", -1.0), ("right", 1.0)):
            geometry["service_points"].append(
                {
                    "schema_version": 1,
                    "id": f"{facility_id}-{side}",
                    "facility_id": facility_id,
                    "point_m": {"x": point["x"], "y": point["y"] + offset},
                    "service_capacity": 1,
                    "queue_id": f"{facility_id}-{side}-fifo",
                    "access": {"accessibility": "accessible", "required_staff_skills": [skill]},
                }
            )
    document["staff"].append(
        {"id": "toilet_staff_01", "zone_id": zones["toilet_01"], "skills": ["toilet_service"]}
    )
    return ScenarioSpec.model_validate(document)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give each parallel worker one long simulation to start with.

    pytest-xdist's worksteal scheduler hands each worker a contiguous block of the
    collected items and lets idle workers steal from the end of other blocks. When
    the long simulations sit together, one worker runs them back to back while the
    others finish early; putting one at the head of each block starts them all at once.
    Every worker collects in this same order, so the blocks still agree.
    """
    workers = getattr(config, "workerinput", {}).get("workercount", 1)
    long_runs = [item for item in items if item.get_closest_marker("long_simulation")]
    if workers < 2 or not long_runs:
        return
    rest = [item for item in items if item.get_closest_marker("long_simulation") is None]
    size = len(items) // workers
    ordered: list[pytest.Item] = []
    for index in range(workers):
        block = rest[: size - (1 if index < len(long_runs) else 0)] if index < workers - 1 else rest
        rest = rest[len(block) :]
        ordered.extend(long_runs[index : index + 1] + block)
    ordered.extend(long_runs[workers:])
    items[:] = ordered
