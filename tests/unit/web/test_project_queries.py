"""A history page cursor belongs to one Branch head and one query."""

from __future__ import annotations

import pytest

from sheltersim.storage.projects import StorageError
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

