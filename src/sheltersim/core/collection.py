"""Fewer cyclic garbage collections while a worker builds or loads large histories.

Python collects its youngest generation after every 700 new container objects, and
each third-generation collection walks every live object. A day of computation for
100 people keeps millions of history objects alive, so those collections took about
7% of the time (#234). While a computation runs or a Checkpoint loads, the youngest
generation is collected after 50,000 new objects instead; the previous thresholds
are restored when the last such section ends. This is process-wide, so it also
applies to other threads while a section is active.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_YOUNGEST_THRESHOLD = 50_000
_OLDER_THRESHOLDS = (20, 10)

_lock = threading.Lock()
_active = 0
_saved: tuple[int, int, int] | None = None


@contextmanager
def fewer_collections() -> Iterator[None]:
    """Collect cycles less often until the outermost active section ends."""
    global _active, _saved
    with _lock:
        if _active == 0:
            _saved = gc.get_threshold()
            gc.set_threshold(_YOUNGEST_THRESHOLD, *_OLDER_THRESHOLDS)
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1
            if _active == 0 and _saved is not None:
                gc.set_threshold(*_saved)
                _saved = None
