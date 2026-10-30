"""A save sent to the indexing process stands for the whole Checkpoint only from the
index's offsets on; reading before them fails instead of indexing a gap.

That a computation indexed this way gets the index a direct ingest gets is checked in
tests/integration/test_checkpoint_saves.py.
"""

from __future__ import annotations

import multiprocessing
import queue
import threading

import pytest

from sheltersim.storage.index_writer import _SENDER_GONE, _read_sent, _Tail
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


def test_a_send_larger_than_the_pipe_does_not_wait_for_the_reader_of_the_queue() -> None:
    # Nothing takes from the queue here, as while the indexing process is in an ingest.
    ours, theirs = multiprocessing.Pipe()
    sent: queue.SimpleQueue = queue.SimpleQueue()
    reader = threading.Thread(target=_read_sent, args=(theirs, sent), daemon=True)
    reader.start()
    large = b"x" * (32 * 1024 * 1024)
    sender = threading.Thread(target=lambda: (ours.send(large), ours.send(None)), daemon=True)
    sender.start()
    sender.join(timeout=60)
    assert not sender.is_alive()
    assert sent.get(timeout=60) == large
    assert sent.get(timeout=60) is None
    reader.join(timeout=60)
    assert not reader.is_alive()


def test_the_reader_reports_a_closed_pipe() -> None:
    ours, theirs = multiprocessing.Pipe()
    ours.close()
    sent: queue.SimpleQueue = queue.SimpleQueue()
    _read_sent(theirs, sent)
    assert sent.get(timeout=60) is _SENDER_GONE
