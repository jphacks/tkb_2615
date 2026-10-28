"""History pages: a cursor belongs to one Branch head and query; indexes skip the WorldState."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.web.project_queries import page


def test_page_cursor_cannot_mix_heads_or_query_context() -> None:
    first = page(list(range(10)), {"branch_id": "main"}, "head-one", limit=2)
    with pytest.raises(StorageError, match="stale_or_foreign_cursor"):
        page(
            list(range(10)), {"branch_id": "main"}, "head-two", limit=2, cursor=first["next_cursor"]
        )
    with pytest.raises(StorageError, match="stale_or_foreign_cursor"):
        page(
            list(range(10)),
            {"branch_id": "child"},
            "head-one",
            limit=2,
            cursor=first["next_cursor"],
        )


def test_indexed_search_does_not_load_a_world_and_rejects_changed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.web.project_queries import ProjectQueries

    store = ProjectStore(tmp_path)
    store.create_project("p")
    store._path("p", "runs", "r").mkdir(parents=True)
    head = "a" * 64
    reference = SimpleNamespace(fork_minute=0, computed_until=5000, checkpoint_hash=head)
    monkeypatch.setattr(store, "branch_reference", lambda *_args: reference)

    def no_world(*_args: object) -> None:
        raise AssertionError("indexed evidence must not decode a whole WorldState")

    # Every load, load_branch() included, decodes through load_restored_checkpoint().
    monkeypatch.setattr(store, "load_restored_checkpoint", no_world)
    checkpoint = SimpleNamespace(
        checkpoint_hash=head,
        simulation_minute=5000,
        state_hash="b" * 64,
        world_state={"event_log": [], "model_state": {}},
        metrics_state={
            "snapshots": [
                {"minute_index": minute, "stress_mean": minute / 5000} for minute in range(5000)
            ]
        },
    )
    HistoryIndex(store, "p", "r").ingest("main", checkpoint)
    query = HistoryQuery(kind="kpis", start=4097, end=4999, limit=3)
    result = ProjectQueries(store).history("p", "r", "main", query)
    assert [row["minute_index"] for row in result["items"]] == [4097, 4098, 4099]
    assert result["total_items"] is None
    assert result["query_strategy"] == "indexed_keyset"
    following = ProjectQueries(store).history(
        "p", "r", "main", query.model_copy(update={"cursor": result["next_cursor"]})
    )
    assert [row["minute_index"] for row in following["items"]] == [4100, 4101, 4102]
    reference.checkpoint_hash = "c" * 64
    with pytest.raises(StorageError, match="stale_or_foreign_cursor|history_index_stale"):
        ProjectQueries(store).history(
            "p", "r", "main", query.model_copy(update={"cursor": result["next_cursor"]})
        )
