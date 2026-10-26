from __future__ import annotations

import copy
import os
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


@pytest.fixture(scope="session")
def generated_scenario_preview(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The Scenario preview the Project screen gets for GENERATED_SCENARIO_REQUEST.

    It goes through the same ProjectAPI preview as the UI, so the Scenario has a
    floor plan (facility_geometry) and facility awareness (agent_dynamics) like
    every Scenario an operator creates. Only the regional research is replaced by
    an empty result, because the real one calls public web services.
    Built once per worker; use generated_scenario for a Scenario to change.
    """
    from sheltersim.region.models import RegionResearchResult
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    root = tmp_path_factory.mktemp("generated-scenario")
    with pytest.MonkeyPatch.context() as monkeypatch:
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
            preview = api.workspace.preview(
                "project", {"text": "", "request": copy.deepcopy(GENERATED_SCENARIO_REQUEST)}
            )
        finally:
            api.close()
    return preview


@pytest.fixture
def generated_scenario(generated_scenario_preview: dict[str, Any]):
    """A fresh ScenarioSpec of the shared generated preview, safe to change in one test."""
    from sheltersim.experiments.scenario import ScenarioSpec

    return ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
