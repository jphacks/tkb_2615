"""Run a pytest session with every WorldState's documents tracked; see support/tracked_mirror.py.

Not loaded by tests/conftest.py. Ask for it with PYTHONPATH including tests/:

    python -m pytest -p support.tracked_mirror_plugin ...

A test fails with Drift if, at any phase end or when it ends, a tracked document differs from what
its forwarded writes rebuilt. SHELTERSIM_MIRROR_CHECK_EVERY compares all phases only of every Nth
minute, and the last phase of the others, for the seven-day tests. What the session saw goes to
SHELTERSIM_MIRROR_REPORT if set.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from support.tracked_mirror import Drift, Session, patch_worker_processes


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    # These make their own Sessions, and one patch at a time is what the module supports. The
    # others test the production sink, and expect a plain model_state until they own a namespace.
    own_sinks = {
        "test_tracked_mirror.py",
        "test_state_mirror.py",
        "test_world_core.py",
        "test_psychology_row.py",
        "test_psychology_update.py",
        "test_information_row.py",
        "test_capture_rows.py",
        "test_social_propagation_row.py",
    }
    return True if collection_path.name in own_sinks else None


def pytest_configure(config: pytest.Config) -> None:
    every = int(os.environ.get("SHELTERSIM_MIRROR_CHECK_EVERY", "1"))
    session = Session(check_every=every)
    session.__enter__()
    config._tracked_mirror_session = session  # type: ignore[attr-defined]
    # ProjectAPI's computations run in spawned workers.
    reports = Path(tempfile.mkdtemp(prefix="tracked-mirror-workers-"))
    config._tracked_mirror_undo = patch_worker_processes(reports, {"check_every": every})  # type: ignore[attr-defined]


def pytest_unconfigure(config: pytest.Config) -> None:
    session: Session = config._tracked_mirror_session  # type: ignore[attr-defined]
    session.__exit__()
    config._tracked_mirror_undo()  # type: ignore[attr-defined]
    target = os.environ.get("SHELTERSIM_MIRROR_REPORT")
    if target:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        Path(f"{target}.{worker}.json").write_text(
            json.dumps(session.report(), ensure_ascii=False), encoding="utf-8"
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    session: Session = item.config._tracked_mirror_session  # type: ignore[attr-defined]
    result = yield
    session.check_plain_aliases()
    try:
        session.check_all_roots()
    except Drift as exc:
        raise Drift(f"{item.nodeid}: {exc}") from exc
    return result
