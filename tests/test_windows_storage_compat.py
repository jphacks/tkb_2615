"""Windows-only module surface for project storage, simulated on Linux.

Windows has no ``fcntl`` and no ``os.O_NOFOLLOW``; ``msvcrt.locking`` locks a
byte range per file handle. Linux open-file-description locks give the same
per-handle behaviour, so these tests run in the normal CI.
"""

from __future__ import annotations

import fcntl
import os
import struct
import types
from pathlib import Path

import pytest

import sheltersim.storage.projects as projects_module
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.storage.query_cursors import seal, unseal

pytestmark = pytest.mark.skipif(
    not hasattr(fcntl, "F_OFD_SETLK"), reason="needs Linux open-file-description locks"
)


def _fake_msvcrt() -> types.SimpleNamespace:
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
    monkeypatch.setattr(projects_module, "fcntl", None)
    monkeypatch.setattr(projects_module, "msvcrt", _fake_msvcrt(), raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW")


def _store_with_run(root: Path) -> ProjectStore:
    import json

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = ProjectStore(root)
    store.create_project("project")
    fixture = Path(__file__).resolve().parents[1] / "fixtures/scenarios/h00_small_replay.json"
    scenario = ScenarioSpec.load(fixture)
    approved = digest(json.loads(scenario.canonical_json()))
    saved = store.save_scenario("project", scenario, approved_hash=approved)
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    return store


def test_run_writer_lease_works_without_fcntl_or_nofollow(
    tmp_path: Path, windows_like: None
) -> None:
    store = _store_with_run(tmp_path)
    lease = store.claim_run("project", "run")
    try:
        with pytest.raises(StorageError, match="run_worker_busy"):
            ProjectStore(tmp_path).claim_run("project", "run")
    finally:
        store.release_run(lease)
    ProjectStore(tmp_path).release_run(ProjectStore(tmp_path).claim_run("project", "run"))


def test_scan_cursor_signing_key_works_without_nofollow(tmp_path: Path, windows_like: None) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    assert unseal(store, seal(store, "progress")) == "progress"
