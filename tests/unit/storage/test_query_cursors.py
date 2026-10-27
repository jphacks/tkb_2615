"""Scan progress is signed, so a client cannot claim that a range was already checked."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.storage.query_cursors import seal, unseal

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


def test_scan_progress_cannot_be_forged_to_claim_an_earlier_prefix_was_checked(
    tmp_path: Path,
) -> None:
    from sheltersim.storage.query_cursors import seal, unseal

    store = ProjectStore(tmp_path)
    token = seal(store, "original signed progress with a gap")
    assert unseal(store, token) == "original signed progress with a gap"
    body = json.loads(base64.urlsafe_b64decode(token))
    body["payload"] = "forged progress without a gap"
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
    with pytest.raises(StorageError, match="invalid_scan_progress_cursor"):
        unseal(store, forged)


@needs_ofd_locks
def test_scan_cursor_signing_key_works_without_nofollow(tmp_path: Path, windows_like: None) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    assert unseal(store, seal(store, "progress")) == "progress"
