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
def generated_request() -> Callable[..., dict[str, Any]]:
    """The structured form of GENERATED_SCENARIO_REQUEST with some fields changed."""
    return _generated_request
