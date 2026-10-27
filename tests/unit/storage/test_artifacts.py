"""Drafts and assets: a stale edit never overwrites a newer revision, and a used asset stays."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from sheltersim.contracts.workspace import AssetBegin, AssetChunk, DraftInput
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.projects import StorageError


def test_draft_cas_preserves_prior_revision_and_rejects_stale_editor(
    project_store_at, tmp_path: Path
) -> None:
    store = project_store_at(tmp_path)
    drafts = ArtifactStore(store)
    initial = drafts.save_draft("project", "draft", DraftInput(content={"people": 100}))
    edited = drafts.save_draft(
        "project",
        "draft",
        DraftInput(content={"people": 500}, expected_hash=initial["content_hash"]),
    )
    with pytest.raises(StorageError, match="draft_conflict"):
        drafts.save_draft(
            "project",
            "draft",
            DraftInput(content={"people": 30}, expected_hash=initial["content_hash"]),
        )
    assert ArtifactStore(store).draft("project", "draft")["content_hash"] == edited["content_hash"]
    assert store._read(store._path("project", "drafts", "draft", "v1.json"))["content"] == {
        "people": 100
    }


def test_chunk_retry_hash_verification_and_referenced_asset_retention(
    project_store_at, tmp_path: Path
) -> None:
    store = project_store_at(tmp_path)
    assets = ArtifactStore(store)
    content = b'{"rooms":[]}'
    sha = hashlib.sha256(content).hexdigest()
    assets.begin(
        "project",
        "upload",
        AssetBegin(
            media_type="application/json",
            byte_count=len(content),
            sha256=sha,
            permission_confirmed=True,
        ),
    )
    chunk = AssetChunk(index=0, data_base64=base64.b64encode(content).decode())
    assets.chunk("project", "upload", chunk)
    assert assets.chunk("project", "upload", chunk)["chunks"] == 1
    assert assets.finish("project", "upload")["asset_id"] == sha
    assert assets.finish("project", "upload")["asset_id"] == sha
    assert (
        base64.b64decode(assets.asset("project", sha, include_content=True)["data_base64"])
        == content
    )
    assets.save_draft("project", "plan", DraftInput(content={"plan_sha256": sha}))
    with pytest.raises(StorageError, match="asset_in_use"):
        assets.delete("project", "assets", sha, confirmation=sha)
