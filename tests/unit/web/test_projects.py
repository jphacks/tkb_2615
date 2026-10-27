"""Project API reads that need only a Branch pointer do not decode or load whole
checkpoints.

The routes that run a computation over a saved Run are checked in
tests/integration/test_pointer_only_reads.py and test_resume_checkpoint_loads.py.
"""

import pytest

from sheltersim.storage.projects import StorageError
from sheltersim.web.projects import ProjectAPI


def test_report_and_conversations_check_only_the_branch_pointer(
    tmp_path, no_checkpoint_decode, saved_timeline
):
    store, _ = saved_timeline(tmp_path)
    api = ProjectAPI(store, recover=False)
    try:
        assert api.get(["p", "runs", "r", "branches", "main", "report"], {}) == {
            "status": "not_generated"
        }
        assert api.get(["p", "runs", "r", "branches", "main", "conversations"], {})["turns"] == []
        assert no_checkpoint_decode == []
        for action in ("report", "conversations"):
            with pytest.raises(StorageError):
                api.get(["p", "runs", "r", "branches", "missing", action], {})
    finally:
        api.close()
