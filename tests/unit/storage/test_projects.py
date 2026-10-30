"""Project storage keeps a saved answer's proofs and lets one worker write a Run at a time;
a restart keeps the exact Checkpoint, and stale saves, path traversal and edits of a
started Run are refused.

Saving a Checkpoint validates and detaches it once and refuses mutated or sensitive
data before anything is written; saved Runs over time are checked in
tests/integration/test_checkpoint_saves.py. The storage usage counts the regular files
on disk at each call, and a write over the capacity is refused.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest
from support.paths import SCENARIO_100_AGENTS, SMALL_REPLAY

from sheltersim import native
from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import (
    ProjectStore,
    StorageError,
    _reject_sensitive_checkpoint_content,
    canonical,
    digest,
    safe_document,
)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

# Windows has no fcntl and no os.O_NOFOLLOW; msvcrt.locking locks a byte range per file
# handle. Linux open-file-description locks give the same per-handle behaviour, so the
# windows_like fixture (tests/support/storage.py) simulates Windows with them.
needs_ofd_locks = pytest.mark.skipif(
    not hasattr(fcntl, "F_OFD_SETLK"), reason="needs Linux open-file-description locks"
)


def test_saved_scalar_proofs_and_scope_survive_project_reload(
    project_store_at, tmp_path: Path
) -> None:
    import json
    from copy import deepcopy

    from sheltersim.contracts.api import BranchReference
    from sheltersim.core.checkpoint import Checkpoint
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = project_store_at(tmp_path)
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    checkpoint = Checkpoint.capture(WorldState.from_scenario(scenario))
    store.save_branch(
        "project",
        BranchReference(run_id="run", branch_id="main", fork_minute=0, computed_until=0),
        checkpoint,
    )
    reference = {
        "run_id": "run",
        "branch_id": "main",
        "minute": 0,
        "kind": "kpi",
        "reference_id": "boundary-kpis",
        "status": "recorded",
    }
    turn = {
        "question": "Recorded minute?",
        "answer": "Recorded minute = 0",
        "minute": 0,
        "evidence": [reference],
        "limitations": [],
        "usage": {},
        "model_version": "fake",
        "conversation_id": "conversation",
        "context": {"run_id": "run", "branch_id": "main", "captured_minute": 0},
        "sources": [{"alias": "S1", "reference": reference}],
        "claims": [
            {
                "text": "Recorded minute = 0",
                "evidence": [{"alias": "S1", "field": "minute", "value": 0}],
            }
        ],
    }
    store.save_turn("project", "run", "main", "turn", turn)
    restored = ProjectStore(tmp_path).conversations("project", "run", "main")[0]
    assert restored["schema_version"] == 2
    assert restored["claims"] == turn["claims"]
    assert restored["context"] == turn["context"]
    invalid = deepcopy(turn)
    invalid["claims"][0]["evidence"][0]["alias"] = "unknown"
    with pytest.raises(StorageError, match="invalid_conversation_proof"):
        store.save_turn("project", "run", "main", "invalid-turn", invalid)


def test_run_writer_lease_blocks_second_worker_and_preserves_live_status_on_recovery(
    project_store_at, tmp_path: Path
) -> None:
    import json

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = project_store_at(tmp_path)
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    lease = store.claim_run("project", "run")
    try:
        store.set_status("project", "run", "running")
        other = ProjectStore(tmp_path)
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.claim_run("project", "run")
        assert other.recover_interrupted() == []
        assert other.run("project", "run")["status"] == "running"
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.delete_run("project", "run", confirmation="run")
    finally:
        store.release_run(lease)
    assert ProjectStore(tmp_path).recover_interrupted() == [("project", "run")]
    assert store.run("project", "run")["status"] == "interrupted"


def files(store):
    return sorted((str(p), p.read_bytes()) for p in store.root.rglob("*") if p.is_file())


def test_save_validates_and_detaches_checkpoint_only_once(tmp_path, monkeypatch, saved_timeline):
    store, _ = saved_timeline(tmp_path)
    reference, checkpoint = store.load_branch("p", "r", "main")
    calls = []
    original = Checkpoint.to_dict

    def capture(self):
        calls.append(self.checkpoint_hash)
        return original(self)

    monkeypatch.setattr(Checkpoint, "to_dict", capture)
    store.save_branch(
        "p", reference, checkpoint, expected_checkpoint_hash=checkpoint.checkpoint_hash
    )
    assert calls == [checkpoint.checkpoint_hash]


def test_save_still_rejects_mutation_and_sensitive_runtime_data(tmp_path, saved_timeline):
    store, _ = saved_timeline(tmp_path)
    reference, checkpoint = store.load_branch("p", "r", "main")
    checkpoint.world_state["seed"] += 1
    with pytest.raises(CheckpointError):
        store.save_branch(
            "p", reference, checkpoint, expected_checkpoint_hash=reference.checkpoint_hash
        )
    _, valid = store.load_branch("p", "r", "main")
    sensitive = Checkpoint.capture(valid.restore_world(), runtime_state={"secret": "test-value"})
    new = BranchReference(run_id="r", branch_id="sensitive", fork_minute=0, computed_until=0)
    with pytest.raises(StorageError, match="sensitive"):
        store.save_branch("p", new, sensitive)


def test_recorded_checkpoint_is_validated_only_by_capture(tmp_path, monkeypatch, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    _, head = store.load_branch("p", "r", "main")
    runtime = BranchRuntime.from_checkpoint(head, branch_id="main")
    calls = []
    original = Checkpoint._validate

    def validate(self, **kwargs):
        calls.append(self.checkpoint_hash)
        return original(self, **kwargs)

    monkeypatch.setattr(Checkpoint, "_validate", validate)
    recorded = timeline.record(runtime)
    assert calls == [recorded.checkpoint_hash]


@pytest.mark.parametrize("method", ["save_branch", "save_captured_branch"])
def test_sensitive_checkpoint_is_rejected_before_anything_is_written(
    tmp_path, method, saved_timeline
):
    store, _ = saved_timeline(tmp_path)
    _, valid = store.load_branch("p", "r", "main")
    sensitive = Checkpoint.capture(
        valid.restore_world(), runtime_state={"note": "sk-ant-abcdefghijklmnop"}
    )
    new = BranchReference(run_id="r", branch_id="sensitive", fork_minute=0, computed_until=0)
    before = files(store)
    with pytest.raises(StorageError, match="sensitive"):
        getattr(store, method)("p", new, sensitive)
    assert files(store) == before


def test_split_object_field_names_are_privacy_checked(tmp_path):
    archive = CheckpointArchive(tmp_path, content_check=_reject_sensitive_checkpoint_content)
    with pytest.raises(StorageError, match="sensitive"):
        archive._prepare_validated_document(
            {"model_state": {"phone_number": "x" * 70_000}}, checkpoint_hash="0" * 64
        )
    assert not any(tmp_path.rglob("*.gz"))


@pytest.mark.parametrize(
    "value", [None, True, False, 0, -1, 10**30, 0.0, -0.0, 0.12345678901234567]
)
def test_privacy_scalar_fast_path_keeps_exact_json_values(value):
    assert json.dumps(safe_document(value), allow_nan=False) == json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_privacy_scalar_fast_path_still_rejects_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        safe_document({"values": [value]})


def test_usage_counts_regular_files_without_following_symlinks(tmp_path):
    store = ProjectStore(tmp_path / "store")
    (store.root / "p" / "runs").mkdir(parents=True)
    (store.root / "p" / "runs" / ".hidden").write_bytes(b"12345")
    (store.root / "p" / "a.json").write_bytes(b"1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"0" * 100)
    (store.root / "p" / "linked").symlink_to(outside, target_is_directory=True)
    (store.root / "p" / "b.json").symlink_to(outside / "big")
    # .lock is created empty by the first transaction.
    assert store.usage() == {"bytes": 6, "files": 2, "limit_bytes": store.max_bytes}


def test_usage_sees_another_stores_writes_at_once(tmp_path):
    """Usage is measured at each call, never cached, so another process's file counts (#47)."""
    store = ProjectStore(tmp_path)
    store.create_project("a")
    before = store.usage()
    ProjectStore(tmp_path).create_project("p")
    after = store.usage()
    size = (tmp_path / "p" / "project.json").stat().st_size
    assert (after["bytes"], after["files"]) == (before["bytes"] + size, before["files"] + 1)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps each entry's size from the directory listing, so a file "
    "removed while it is measured still counts",
)
def test_usage_skips_a_file_removed_while_it_is_measured(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    (tmp_path / "kept").write_bytes(b"12")
    (tmp_path / ".pending-x").write_bytes(b"1234")
    scandir = os.scandir

    def listing_then_removal(path):
        entries = list(scandir(path))
        (tmp_path / ".pending-x").unlink(missing_ok=True)
        return iter(entries)

    monkeypatch.setattr(os, "scandir", listing_then_removal)
    assert store.usage()["bytes"] == 2


def test_a_write_over_the_capacity_is_refused_and_leaves_the_file(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    path = store._path("p", "note.json")
    store._write(path, {"schema_version": 1, "text": "a"})
    store.max_bytes = store.usage()["bytes"] + 9
    with pytest.raises(StorageError, match="capacity exceeded"):
        store._write(path, {"schema_version": 1, "text": "a" * 11})
    assert store._read(path)["text"] == "a"
    # Replacing a file counts only the bytes it adds.
    store._write(path, {"schema_version": 1, "text": "a" * 10})
    assert store._read(path)["text"] == "a" * 10


def seed_store(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "run", scenario.id, 1)
    checkpoint = Checkpoint.capture(WorldState.from_scenario(scenario))
    ref = BranchReference(run_id="run", branch_id="main", fork_minute=0, computed_until=0)
    store.save_branch("p", ref, checkpoint)
    return store, checkpoint, ref


def test_restart_keeps_exact_checkpoint_and_marks_interruption(tmp_path):
    store, checkpoint, _ = seed_store(tmp_path)
    store.set_status("p", "run", "running")
    restored = ProjectStore(tmp_path)
    assert restored.recover_interrupted() == [("p", "run")]
    assert restored.load_branch("p", "run", "main")[1].to_json() == checkpoint.to_json()


def test_stale_save_and_traversal_are_rejected(tmp_path):
    store, checkpoint, ref = seed_store(tmp_path)
    with pytest.raises(StorageError, match="branch changed"):
        store.save_branch("p", ref, checkpoint, expected_checkpoint_hash="stale")
    with pytest.raises(StorageError):
        store.project("../outside")


def test_edit_creates_new_revision_without_changing_prior_run(tmp_path):
    store, checkpoint, _ = seed_store(tmp_path)
    run = store.run("p", "run")
    scenario = store.scenario("p", run["scenario_id"], 1)
    document = scenario.model_dump(mode="json")
    document["seed"] += 1
    edited = ScenarioSpec.model_validate(document)
    revision = store.save_scenario(
        "p", edited, approved_hash=digest(json.loads(edited.canonical_json()))
    )
    assert revision["scenario_version"] == 2
    assert store.run("p", "run")["scenario_version"] == 1
    assert store.load_branch("p", "run", "main")[1].state_hash == checkpoint.state_hash


def _store_with_run(root: Path) -> ProjectStore:
    import json

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = ProjectStore(root)
    store.create_project("project")
    fixture = SMALL_REPLAY
    scenario = ScenarioSpec.load(fixture)
    approved = digest(json.loads(scenario.canonical_json()))
    saved = store.save_scenario("project", scenario, approved_hash=approved)
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    return store


@needs_ofd_locks
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


@needs_ofd_locks
def test_branch_writer_leases_work_without_fcntl_or_nofollow(
    tmp_path: Path, windows_like: None
) -> None:
    """The branch leases use exclusive locks only, as Windows offers (#48)."""
    store = _store_with_run(tmp_path)
    main = store.claim_branch("project", "run", "main")
    child = ProjectStore(tmp_path).claim_branch("project", "run", "child")
    try:
        with pytest.raises(StorageError, match="run_worker_busy"):
            ProjectStore(tmp_path).claim_branch("project", "run", "main")
        with pytest.raises(StorageError, match="run_worker_busy"):
            ProjectStore(tmp_path).claim_run("project", "run")
    finally:
        store.release_run(main)
        store.release_run(child)
    whole = ProjectStore(tmp_path).claim_run("project", "run")
    try:
        with pytest.raises(StorageError, match="run_worker_busy"):
            ProjectStore(tmp_path).claim_branch("project", "run", "main")
    finally:
        store.release_run(whole)


def test_branches_of_a_run_are_written_at_once_but_not_with_the_whole_run(tmp_path):
    """Writers of different branches work together; a whole-Run operation waits for
    none of them and starts none meanwhile (#48)."""
    store = _store_with_run(tmp_path)
    other = ProjectStore(tmp_path)
    main = store.claim_branch("project", "run", "main")
    child = other.claim_branch("project", "run", "child")
    try:
        with pytest.raises(StorageError, match="writes this branch"):
            other.claim_branch("project", "run", "main")
        with pytest.raises(StorageError, match="a branch of this Run is being written"):
            other.claim_run("project", "run")
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.delete_run("project", "run", confirmation="run")
    finally:
        store.release_run(main)
        other.release_run(child)
    whole = store.claim_run("project", "run")
    try:
        with pytest.raises(StorageError, match="another process owns this Run"):
            other.claim_branch("project", "run", "main")
    finally:
        store.release_run(whole)


def test_the_run_status_summarizes_its_branches(tmp_path):
    """The Run is running while any branch is; otherwise it shows the last branch to
    stop, and each branch lists its own status (#48)."""
    store = _store_with_run(tmp_path)
    assert store.branch_status("project", "run", "main") is None
    store.set_branch_status("project", "run", "main", "running")
    store.set_branch_status("project", "run", "child", "running")
    store.set_branch_status("project", "run", "main", "paused")
    assert store.run("project", "run")["status"] == "running"
    store.set_branch_status("project", "run", "child", "completed")
    assert store.run("project", "run")["status"] == "completed"
    store.set_branch_status("project", "run", "main", "running")
    store.forget_branch_status("project", "run", "main")
    store.settle_run_status("project", "run", "paused")
    assert store.run("project", "run")["status"] == "paused"
    assert store.branch_status("project", "run", "main") is None
    assert store.branch_status("project", "run", "child") == "completed"


def test_recovery_interrupts_a_branch_left_running_and_spares_a_live_one(tmp_path):
    """A fork that ended before its child existed leaves only the child's status; it
    is forgotten, or the Run would stay running (#48)."""
    store = _store_with_run(tmp_path)
    for branch in ("main", "child"):
        reference = BranchReference(
            run_id="run",
            branch_id=branch,
            fork_minute=0,
            computed_until=0,
            checkpoint_hash="a" * 64,
        )
        store._write(
            store._path("project", "runs", "run", "branches", branch, "branch.json"),
            reference.model_dump(mode="json"),
        )
    store.set_branch_status("project", "run", "never-created", "running")
    store.set_branch_status("project", "run", "main", "running")
    store.set_branch_status("project", "run", "child", "running")
    live = store.claim_branch("project", "run", "child")
    try:
        assert ProjectStore(tmp_path).recover_interrupted() == [("project", "run")]
        assert store.branch_status("project", "run", "main") == "interrupted"
        assert store.branch_status("project", "run", "never-created") is None
        assert store.branch_status("project", "run", "child") == "running"
        assert store.run("project", "run")["status"] == "running"
    finally:
        store.release_run(live)
    ProjectStore(tmp_path).recover_interrupted()
    assert store.branch_status("project", "run", "child") == "interrupted"
    assert store.run("project", "run")["status"] == "interrupted"


def test_phone_numbers_are_redacted_but_dois_and_dates_are_not():
    """Run artifacts share this definition; a broader one blanked model references (#190)."""
    from sheltersim.storage.projects import safe_document

    for text in ("call 090-1234-5678", "03-1234-5678", "+81 90 1234 5678"):
        assert safe_document(text) != text
    for text in (
        "DeGroot (1974), doi:10.1080/01621459.1974.10480137",
        "https://link.springer.com/article/10.1007/s10458-012-9201-1",
        "doi:10.1111/1467-8721.ep10770953",
        "accessed 2026-10-28",
    ):
        assert safe_document(text) == text


def test_model_signatures_are_kept_while_email_addresses_are_redacted():
    """A decimal after "@" in a model signature was taken for an address (#187)."""
    from sheltersim.storage.projects import safe_document

    signature = "distance-sampled-proximity-v1@1:state1:distance=1.5:interval=15:pairs=500"
    assert safe_document("contact name@example.com today") == "contact [redacted] today"
    assert safe_document(signature) == signature


def test_repeated_short_and_long_texts_and_text_subclasses_are_redacted_alike() -> None:
    """Short texts are redacted once and remembered (#223); every text gets the same result."""
    short = "call 090-1234-5678"
    long = "note " * 30 + "name@example.com"

    class Label(str):
        pass

    for _ in range(2):
        assert safe_document({"a": short, "b": [long, (short,)], "c": Label(short)}) == {
            "a": "call [redacted]",
            "b": ["note " * 30 + "[redacted]", ["call [redacted]"]],
            "c": "call [redacted]",
        }
    with pytest.raises(StorageError, match="sensitive fields"):
        safe_document({"note": short, 1: "x"})


def test_texts_are_redacted_as_by_the_secret_and_phone_patterns() -> None:
    """Text without "@" skips the address pattern, and text without "@", "sk-" or "bearer"
    the whole secret pattern; results are unchanged."""
    from sheltersim.storage.projects import _SECRET, PHONE_NUMBER

    texts = [
        '{"realized_events":["action-lifecycle-38460"],"busy_staff":[]}' * 40,
        '{"staff_tasks":["task-1","task-2"]}' * 40,  # "sk-" without "@"
        "staff-task-abcdefghijklmnop",
        "sk-abcdefghijklmnop@example.com and Bearer name@example.com",
        "token sk-ant-abcdefghijklmnop end",
        "SK-ABCDEFGHIJKLMNOP",
        "ſK-abcdefghijklmnop",  # long s and Kelvin sign match "sk" ignoring case
        "Authorization: BEARER abc.def",
        "beаrer abc",  # Cyrillic a: not "bearer"
        "write to name@example.com or call 090-1234-5678",
        "straße k- 03-1234-5678",
        "no secrets here",
        "",
    ]
    for text in texts:
        expected = PHONE_NUMBER.sub("[redacted]", _SECRET.sub("[redacted]", text))
        assert safe_document(text) == expected
        assert safe_document([text]) == [expected]


def test_a_save_counts_its_own_writes_onto_one_measurement(tmp_path, saved_timeline) -> None:
    """#237: a save measures the disk once, and the usage it reaches by counting its
    own writes equals measuring the disk again at its end."""
    from contextlib import contextmanager

    store, timeline = saved_timeline(tmp_path)
    measured = []
    ends = []
    original_measure = store._measure_usage
    original_session = store._usage_measured_once

    def measure():
        measured.append(1)
        return original_measure()

    @contextmanager
    def session():
        before = len(measured)
        with original_session():
            yield
            ends.append((len(measured) - before, store.usage(), original_measure()))

    store._measure_usage = measure
    store._usage_measured_once = session
    timeline.advance("main", 60)
    assert len(ends) == 2
    for measurements, counted, again in ends:
        assert measurements == 1
        assert (counted["bytes"], counted["files"]) == (again["bytes"], again["files"])


def test_another_writer_during_a_save_overshoots_by_at_most_that_save(
    tmp_path, monkeypatch, saved_timeline
) -> None:
    """#237: a file another process writes during a save is counted from the next
    measurement on. That process measures the disk itself before writing, so the
    store exceeds the capacity by at most what the save writes."""
    store, _ = saved_timeline(tmp_path)
    _, head = store.load_branch("p", "r", "main")
    checkpoint = Checkpoint.capture(head.restore_world())
    reference = BranchReference(run_id="r", branch_id="other", fork_minute=0, computed_until=0)
    other = ProjectStore(tmp_path)
    size = 1_000_000
    before = store.usage()["bytes"]
    original = CheckpointArchive._write_prepared

    def write_prepared(archive):
        # Another process writes a file as FrameCache.put does, measuring the disk
        # itself and without the store lock; the capacity leaves room just for it.
        store.max_bytes = other.max_bytes = other.usage()["bytes"] + size
        assert other.usage()["bytes"] + size <= other.max_bytes
        (tmp_path / "p" / "runs" / "r" / "other-process").write_bytes(b"0" * size)
        original(archive)

    monkeypatch.setattr(CheckpointArchive, "_write_prepared", write_prepared)
    store.save_branch("p", reference, checkpoint)
    after = store.usage()["bytes"]
    saved = after - size - before
    assert 0 < after - store.max_bytes <= saved
    # The next save measures the disk again and is refused.
    monkeypatch.setattr(CheckpointArchive, "_write_prepared", original)
    refused = BranchReference(run_id="r", branch_id="third", fork_minute=0, computed_until=0)
    with pytest.raises(StorageError, match="capacity"):
        store.save_branch("p", refused, checkpoint)
    assert store.usage()["bytes"] == after


def test_a_file_shared_for_a_moment_on_windows_is_tried_again(monkeypatch):
    """Windows cannot open a file being replaced, nor replace one open elsewhere; a
    status read during a save answered HTTP 500 there."""
    from sheltersim.storage import projects

    monkeypatch.setattr(projects.os, "name", "nt")
    monkeypatch.setattr(projects.time, "sleep", lambda _seconds: None)
    tries = []

    def shared_twice():
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError("the file is being used by another process")
        return "read"

    assert projects.retry_while_shared(shared_twice) == "read"
    assert len(tries) == 3

    def always_shared():
        raise PermissionError("the file is being used by another process")

    with pytest.raises(PermissionError):
        projects.retry_while_shared(always_shared, attempts=3)


_PRIVATE_DOCUMENTS = [
    {"api_key": "x"},
    {"nested": [{"Full Name": 1}]},
    ["call 090-1234-5678"],
    ["+81 90-1234-5678"],
    ["０９０-１２３４-５６７８"],
    {"note": "write to person@example.org today"},
    ["token sk-abcdefghijkl0123"],
    ["token sk-ant-abcdefgh"],
    ["Token SK-ABCDEFGHIJKL"],
    ["token ſk-abcdefghijkl"],
    ["token sK-abcdefghijkl"],
    ["BEARER abc"],
    ["line\nBearer\tabc"],
    ['quoted "person@example.org"'],
    ["あ 090-1234-5678"],
]
_CLEAN_DOCUMENTS = [
    {"task-1": "task-1", "id": "task-abcdefghijk", "text": "あいう"},
    [1.5, -2, None, True, "2026-10-31", 'a "quoted" \\ \n string'],
    ["bearing", "1-2", "١", "minute 12-34"],
    {"a": {"b": [{"c": "no-digits - here"}]}},
    [],
    {},
]


def test_the_privacy_check_of_chunk_bytes_decides_as_the_check_of_the_document() -> None:
    """A chunk's bytes are checked in Rust when possible; nothing sensitive gets through."""

    def verdict(document: object, raw: bytes | None) -> bool:
        try:
            _reject_sensitive_checkpoint_content([(document, raw)])
        except StorageError:
            return False
        return True

    for document, clean in [
        *((item, False) for item in _PRIVATE_DOCUMENTS),
        *((item, True) for item in _CLEAN_DOCUMENTS),
    ]:
        raw = canonical(document).encode()
        assert verdict(document, None) is clean, document
        assert verdict(document, raw) is clean, document


def test_the_strings_rust_leaves_out_can_not_match_the_privacy_patterns() -> None:
    """Every string the kernel does not return is one safe_document() leaves as it is."""
    if not native.available():
        pytest.skip("the sheltersim_native extension is not installed")
    generator = random.Random(2615)
    pieces = [
        *'@-  .:_+\n\t"\\',
        *"0123456789abKkSsfz",
        "ſ",
        "K",
        "١",
        "１",
        "²",
        "あ",
        "sk-",
        "SK-",
        "ant-",
        "abcdefghijkl",
        "bearer ",
        "Bearer",
        "090",
        "1234",
        "5678",
        "person@example.org",
    ]
    returned = 0
    for _ in range(20_000):
        text = "".join(generator.choices(pieces, k=generator.randrange(1, 9)))
        _, strings = native.sensitive_candidates([canonical([text]).encode()])
        if strings:
            assert strings == [text]
            returned += 1
        else:
            assert safe_document(text) == text, text
    assert returned > 1000


def test_a_changed_item_of_a_history_chunk_is_checked_at_the_next_save(tmp_path) -> None:
    """Items already stored are not what makes a chunk safe: new bytes are always read."""
    archive = CheckpointArchive(
        tmp_path / "archive", content_check=_reject_sensitive_checkpoint_content
    )
    items = [{"value": index} for index in range(300)]
    checkpoint_hash = "0" * 64
    archive._encode_validated_document({"items": items}, checkpoint_hash=checkpoint_hash)
    items[10] = {"value": "person@example.org"}
    with pytest.raises(StorageError, match="sensitive"):
        archive._encode_validated_document({"items": items}, checkpoint_hash=checkpoint_hash)
