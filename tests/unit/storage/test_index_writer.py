"""A save sent to the indexing process stands for the whole Checkpoint only from the
index's offsets on; reading before them fails instead of indexing a gap.

That a computation indexed this way gets the index a direct ingest gets is checked in
tests/integration/test_checkpoint_saves.py.
"""

from __future__ import annotations

import pytest

from sheltersim.storage.index_writer import _Tail
from sheltersim.storage.projects import StorageError


def test_a_tail_reads_its_items_where_the_whole_list_has_them() -> None:
    tail = _Tail(5, 3, ["d", "e"])
    assert len(tail) == 5 and bool(tail)
    assert tail[3:] == ["d", "e"] and tail[4] == "e" and tail[-1] == "e"
    assert tail[5:] == []
    assert list(_Tail(2, 0, ["a", "b"])) == ["a", "b"]


@pytest.mark.parametrize("read", [lambda tail: tail[2:], lambda tail: tail[1], list])
def test_reading_before_a_tail_fails(read) -> None:
    with pytest.raises(StorageError, match="history_index_out_of_step"):
        read(_Tail(5, 3, ["d", "e"]))


def test_a_tail_must_end_its_list() -> None:
    with pytest.raises(ValueError, match="does not end the list"):
        _Tail(6, 3, ["d", "e"])
