"""A page of a Checkpoint section reads only the chunks it returns."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_checkpoint_section_reads_only_selected_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sheltersim.storage.checkpoints import CheckpointArchive

    rows = [{"minute_index": minute, "value": "x" * 128} for minute in range(5000)]
    archive = CheckpointArchive(tmp_path, chunk_records=128)
    record = archive.encode(
        SimpleNamespace(
            checkpoint_hash="a" * 64, to_dict=lambda: {"metrics_state": {"snapshots": rows}}
        )
    )
    reads = []
    original = archive._get

    def read(key: str) -> object:
        reads.append(key)
        return original(key)

    monkeypatch.setattr(archive, "_get", read)
    result = archive.list_page(record, ("metrics_state", "snapshots"), offset=4097, limit=3)
    assert result["items"] == rows[4097:4100]
    assert result["next_offset"] == 4100
    assert result["total_items"] == 5000
    assert len(reads) <= 2
    assert result["full_checkpoint_verified_by_this_read"] is False
