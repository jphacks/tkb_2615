"""Starting a computation decodes and validates the head checkpoint only once.

The range check and a corrupt head are checked in tests/unit/web/test_projects.py.
"""

import threading

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.storage.projects import ProjectStore
from sheltersim.web.projects import ProjectAPI


def test_resume_loads_the_head_checkpoint_once(tmp_path, monkeypatch, saved_run_with_scenario):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_run_with_scenario(tmp_path)
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    loads, validations = [], []
    load, validate = ProjectStore.load_checkpoint, Checkpoint._validate

    def counted_load(self, project_id, run_id, checkpoint_hash):
        loads.append(checkpoint_hash)
        return load(self, project_id, run_id, checkpoint_hash)

    def counted_validate(self, *args, **kwargs):
        if self.checkpoint_hash == head:
            validations.append(1)
        return validate(self, *args, **kwargs)

    monkeypatch.setattr(ProjectStore, "load_checkpoint", counted_load)
    monkeypatch.setattr(Checkpoint, "_validate", counted_validate)
    api = ProjectAPI(store)
    # The pool has one worker. Holding it keeps the queued computation from starting, so
    # the check sees only what submit() itself read (#145: the worker sometimes won the race).
    release = threading.Event()
    held = api._pool.submit(release.wait, 60)
    try:
        api.submit("p", "r", "main", 5)
        assert loads == []
        release.set()
        assert held.result(timeout=60)
        api._pool.submit(lambda: None).result(timeout=60)
    finally:
        release.set()
        api.close()
    assert store.branch_reference("p", "r", "main").computed_until == 5
    # One decode with its validation, then one restore into the runtime.
    assert loads == [head]
    assert len(validations) == 2
