"""Routes that need a pointer or one field must not decode whole checkpoints: the
comparison of fork receipts and the results Q&A completion check.

The report and conversation routes are checked in tests/unit/web/test_projects.py.
"""

from types import SimpleNamespace

import pytest

from sheltersim.web.projects import ProjectAPI
from sheltersim.web.results_chat import ResultsChatService, WebQueryError


def test_comparison_reads_fork_receipts_without_decoding_history(
    tmp_path, no_checkpoint_decode, saved_timeline
):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    timeline.fork("main", 13, "child")
    timeline.advance("child", 30)
    _, child = store.load_branch("p", "r", "child")
    expected = child.runtime_state["fork_origin"]
    api = ProjectAPI(store, recover=False)
    no_checkpoint_decode.clear()
    query = {"branch_ids": ["main,child"], "minute": ["30"]}
    try:
        # Both branches are at a saved boundary, so frames come from the cache.
        result = api.get(["p", "runs", "r", "compare"], query)
    finally:
        api.close()
    assert no_checkpoint_decode == []
    assert result["origin_receipts"] == [None, expected]
    assert result["shared_origin"] is True


def test_results_qa_completion_check_uses_the_pointer(
    tmp_path, no_checkpoint_decode, saved_timeline
):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 5)
    no_checkpoint_decode.clear()
    service = SimpleNamespace(projects=SimpleNamespace(store=store))
    with pytest.raises(WebQueryError, match="completed project branch"):
        ResultsChatService._project_evidence(service, "p", "r", "main", None, None, None)
    assert no_checkpoint_decode == []
