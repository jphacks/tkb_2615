"""Project stores, saved Runs and their Checkpoints, and storage on another platform."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.paths import SCENARIO_100_AGENTS


@pytest.fixture
def project_store_at() -> Callable[[Path], Any]:
    """Open a ProjectStore at a folder, with an empty project named "project"."""

    def open_store(root: Path) -> Any:
        from sheltersim.storage.projects import ProjectStore

        store = ProjectStore(root)
        store.create_project("project")
        return store

    return open_store


def _saved_timeline(tmp_path: Path, scenario: Any = None) -> tuple[Any, Any]:
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.branch import BranchRuntime
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.experiments.timeline import TimelineStore
    from sheltersim.storage.projects import ProjectStore, digest

    store = ProjectStore(tmp_path)
    store.create_project("p")
    if scenario is None:
        scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


@pytest.fixture(scope="session")
def saved_timeline() -> Callable[..., tuple[Any, Any]]:
    """Open a store at a folder with project p, Run r and its first Checkpoint recorded;
    return the store and the Run's TimelineStore.

    Session-scoped so module-scoped Runs can use it; each call opens a new store.

    The Run is of scenario_100_agents unless a ScenarioSpec is passed, for example
    saved_timeline(tmp_path, generated_scenario) for a Run with a floor plan.
    """
    return _saved_timeline


def _fake_msvcrt() -> Any:
    import fcntl
    import struct
    import types

    def locking(fd: int, mode: int, size: int) -> None:
        kind = fcntl.F_UNLCK if mode == 0 else fcntl.F_WRLCK
        try:
            lock = struct.pack("hh4xqqi4x", kind, os.SEEK_CUR, 0, size, 0)
            fcntl.fcntl(fd, fcntl.F_OFD_SETLK, lock)
        except OSError as exc:
            raise PermissionError(13, "locked") from exc

    return types.SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0, locking=locking)


@pytest.fixture
def windows_like(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make project storage see Windows: no fcntl, msvcrt locks, and no os.O_NOFOLLOW.

    The msvcrt locks are Linux open-file-description locks, so a test using this needs
    them (fcntl.F_OFD_SETLK).
    """
    import sheltersim.storage.projects as projects_module

    monkeypatch.setattr(projects_module, "fcntl", None)
    monkeypatch.setattr(projects_module, "msvcrt", _fake_msvcrt(), raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW")


@pytest.fixture
def no_checkpoint_decode(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the hash of every checkpoint ProjectStore loads from now on."""
    from sheltersim.storage.projects import ProjectStore

    decoded = []
    # Every load, load_checkpoint() and load_branch() included, decodes through here.
    original = ProjectStore.load_restored_checkpoint

    def load(self, project_id, run_id, checkpoint_hash, **options):
        decoded.append(checkpoint_hash)
        return original(self, project_id, run_id, checkpoint_hash, **options)

    monkeypatch.setattr(ProjectStore, "load_restored_checkpoint", load)
    return decoded
