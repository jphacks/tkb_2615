"""Project-local, quota-bound plan assets and compare-and-swap draft revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from typing import Any

from sheltersim.contracts.workspace import AssetBegin, AssetChunk, DraftInput
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest, safe_document


class ArtifactStore:
    CHUNK_BYTES = 256 * 1024
    DRAFT_BYTES = 8 * 1024 * 1024

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def draft(self, project_id: str, draft_id: str) -> dict[str, Any]:
        path = self.store._path(project_id, "drafts", draft_id, "current.json")
        pointer = self.store._read(path)
        revision = self.store._read(self.store._path(
            project_id, "drafts", draft_id, f"v{pointer['revision']}.json"))
        if pointer["content_hash"] != digest(revision["content"]):
            raise StorageError("draft_integrity_mismatch")
        return revision

    def save_draft(self, project_id: str, draft_id: str, request: DraftInput) -> dict[str, Any]:
        content = safe_document(request.content)
        if content != request.content:
            raise StorageError("remove sensitive text before saving or approving a draft")
        if len(canonical(content).encode()) > self.DRAFT_BYTES:
            raise StorageError("draft_capacity_exceeded")
        with self.store.transaction():
            self.store._read(self.store._path(project_id, "project.json"))
            pointer = self.store._path(project_id, "drafts", draft_id, "current.json")
            prior = self.store._read(pointer) if pointer.exists() else None
            if request.expected_hash != (prior["content_hash"] if prior else None):
                raise StorageError("draft_conflict: reload the current revision")
            revision = 1 + (prior["revision"] if prior else 0)
            record = {"schema_version": 1, "draft_id": draft_id, "kind": request.kind,
                      "revision": revision, "content_hash": digest(content), "content": content}
            self.store._write(self.store._path(project_id, "drafts", draft_id,
                                               f"v{revision}.json"), record, immutable=True)
            self.store._write(pointer, {key: value for key, value in record.items()
                                       if key not in {"content", "kind"}})
            return record

    def drafts(self, project_id: str) -> list[dict[str, Any]]:
        self.store._read(self.store._path(project_id, "project.json"))
        return [self.store._read(path) for path in sorted(
            self.store._path(project_id, "drafts").glob("*/current.json"))]

    def _bytes(self, path: Any, data: bytes) -> None:
        if path.exists():
            if path.read_bytes() != data:
                raise StorageError("immutable_asset_conflict")
            return
        if self.store.usage()["bytes"] + len(data) > self.store.max_bytes:
            raise StorageError("storage_capacity_exceeded")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise StorageError("immutable_asset_conflict")
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)

    def begin(self, project_id: str, upload_id: str, request: AssetBegin) -> dict[str, Any]:
        with self.store.transaction():
            self.store._read(self.store._path(project_id, "project.json"))
            if self.store.usage()["bytes"] + request.byte_count * 2 > self.store.max_bytes:
                raise StorageError("storage_capacity_exceeded")
            record = {**safe_document(request.model_dump(mode="json")),
                      "upload_id": upload_id, "chunks": 0, "received_bytes": 0,
                      "status": "uploading"}
            self.store._write(self.store._path(project_id, "uploads", upload_id,
                                               "upload.json"), record, immutable=True)
            return record

    def chunk(self, project_id: str, upload_id: str, request: AssetChunk) -> dict[str, Any]:
        try:
            data = base64.b64decode(request.data_base64, validate=True)
        except ValueError as exc:
            raise StorageError("invalid_asset_encoding") from exc
        if not 0 < len(data) <= self.CHUNK_BYTES:
            raise StorageError("asset_chunk_too_large")
        with self.store.transaction():
            path = self.store._path(project_id, "uploads", upload_id, "upload.json")
            record = self.store._read(path)
            if record["status"] != "uploading":
                raise StorageError("asset_already_completed")
            chunk_path = self.store._path(project_id, "uploads", upload_id, f"chunk-{request.index}")
            if request.index < record["chunks"] and chunk_path.read_bytes() == data:
                return record  # byte-identical retry does not append twice
            if record["status"] != "uploading" or request.index != record["chunks"]:
                raise StorageError("asset_chunk_conflict")
            if record["received_bytes"] + len(data) > record["byte_count"]:
                raise StorageError("asset_declared_size_exceeded")
            self._bytes(chunk_path, data)
            record["received_bytes"] += len(data)
            record["chunks"] += 1
            self.store._write(path, record)
            return record

    def finish(self, project_id: str, upload_id: str) -> dict[str, Any]:
        with self.store.transaction():
            path = self.store._path(project_id, "uploads", upload_id, "upload.json")
            record = self.store._read(path)
            if record["received_bytes"] != record["byte_count"]:
                raise StorageError("asset_incomplete")
            asset_id = record["sha256"]
            metadata = self.store._path(project_id, "assets", asset_id, "asset.json")
            if record["status"] == "completed":
                return self.store._read(metadata)
            data = b"".join(self.store._path(project_id, "uploads", upload_id,
                           f"chunk-{index}").read_bytes() for index in range(record["chunks"]))
            if hashlib.sha256(data).hexdigest() != asset_id:
                raise StorageError("asset_hash_mismatch")
            mime = record["media_type"]
            signatures = {
                "application/pdf": data.startswith(b"%PDF-"),
                "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            }
            if mime in signatures and not signatures[mime]:
                raise StorageError("asset_media_mismatch")
            if mime in {"application/json", "application/geo+json"}:
                document = json.loads(data)
                if not isinstance(document, dict) or safe_document(document) != document:
                    raise StorageError("invalid_or_sensitive_plan_json")
            self._bytes(self.store._path(project_id, "assets", asset_id, "content"), data)
            result = {key: record[key] for key in (
                "schema_version", "media_type", "byte_count", "sha256",
                "permission_confirmed", "sensitive_building", "license_note",
            )}
            result.update({"asset_id": asset_id, "status": "stored_uninterpreted",
                           "file_name":record.get("file_name"),"source_kind":record.get("source_kind","operator_upload"),
                           "coordinate_system":record.get("coordinate_system","unknown"),"rights_status":"operator_permission_asserted"})
            if not metadata.exists():
                self.store._write(metadata, result, immutable=True)
            record["status"] = "completed"
            self.store._write(path, record)
            for index in range(record["chunks"]):
                self.store._path(project_id, "uploads", upload_id, f"chunk-{index}").unlink()
            return self.store._read(metadata)

    def asset(self, project_id: str, asset_id: str, *, include_content: bool = False) -> dict[str, Any]:
        record = self.store._read(self.store._path(project_id, "assets", asset_id, "asset.json"))
        if include_content:
            data = self.store._path(project_id, "assets", asset_id, "content").read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise StorageError("asset_integrity_mismatch")
            record["data_base64"] = base64.b64encode(data).decode("ascii")
        return record

    def delete_upload(self, project_id: str, upload_id: str) -> dict[str, Any]:
        import shutil
        with self.store.transaction():
            folder = self.store._path(project_id, "uploads", upload_id)
            self.store._read(folder / "upload.json")
            shutil.rmtree(folder)
        return {"deleted": upload_id}

    def delete(self, project_id: str, kind: str, identity: str, *, confirmation: str) -> dict[str, Any]:
        import shutil
        if kind not in {"drafts", "assets"} or confirmation != identity:
            raise StorageError("artifact_delete_requires_exact_confirmation")
        with self.store.transaction():
            folder = self.store._path(project_id, kind, identity)
            self.store._read(folder / ("asset.json" if kind == "assets" else "current.json"))
            if kind == "assets":
                # Keep every referenced immutable Scenario/draft revision reproducible.
                for category in ("scenarios", "drafts", "geometries"):
                    root = self.store._path(project_id, category)
                    for path in root.rglob("*.json"):
                        if identity in canonical(self.store._read(path)):
                            raise StorageError("asset_in_use: retain source or remove its draft first")
            shutil.rmtree(folder)
        return {"deleted": identity, "kind": kind}
