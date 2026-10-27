"""Scan progress is signed, so a client cannot claim that a range was already checked."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from sheltersim.storage.projects import ProjectStore, StorageError


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
