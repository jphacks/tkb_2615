"""Compressed content-addressed checkpoint chunks with bounded read amplification."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from bisect import bisect_right
from pathlib import Path
from typing import Any, Callable

from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CanonicalEncoder

_HASH = re.compile(r"^[a-f0-9]{64}$")


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class CheckpointArchive:
    """Storage representation only; restores the unchanged Checkpoint v1 hash."""

    def __init__(
        self,
        directory: Path,
        *,
        chunk_records: int = 128,
        max_new_bytes: int | None = None,
        content_check: Callable[[Any], None] | None = None,
        encoder: CanonicalEncoder | None = None,
    ) -> None:
        self.directory = directory
        self.chunk_records = chunk_records
        self.max_new_bytes = max_new_bytes
        # Called on every chunk value (and large-object field names) that is not
        # already stored. Stored chunks passed the same check when first written.
        self.content_check = content_check
        # The encoder of a just-captured Checkpoint already holds the bytes of the
        # history it reused; chunks and sizes are the same with or without it.
        self.encoder = encoder
        self.new_bytes = 0
        self.chunk_directory = directory / "chunks"
        self._staged: dict[str, bytes] | None = None
        self._staged_bytes = 0

    def _put(self, value: Any, raw: bytes | None = None) -> str:
        if self.chunk_directory.is_symlink() or not self.chunk_directory.resolve().is_relative_to(
            self.directory.resolve()
        ):
            raise CheckpointError("checkpoint chunk directory cannot follow symlinks")
        if raw is None:
            raw = _encoded(value)
        if len(raw) > 16_777_216:
            raise CheckpointError("checkpoint chunk exceeds the supported size")
        key = hashlib.sha256(raw).hexdigest()
        path = self.chunk_directory / f"{key}.json.gz"
        if path.exists() or (self._staged is not None and key in self._staged):
            return key
        if self.content_check is not None:
            self.content_check(value)
        compressed = gzip.compress(raw, compresslevel=3, mtime=0)
        if (
            self.max_new_bytes is not None
            and self.new_bytes + self._staged_bytes + len(compressed) > self.max_new_bytes
        ):
            raise CheckpointError("checkpoint storage capacity exceeded")
        if self._staged is not None:
            self._staged[key] = compressed
            self._staged_bytes += len(compressed)
        else:
            self._write_chunk(key, compressed)
        return key

    def _write_chunk(self, key: str, compressed: bytes) -> None:
        path = self.chunk_directory / f"{key}.json.gz"
        self.chunk_directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".chunk-", dir=self.chunk_directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(compressed)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
                self.new_bytes += len(compressed)
            except FileExistsError:
                pass
        finally:
            Path(name).unlink(missing_ok=True)

    def _pack(self, value: Any) -> dict[str, Any]:
        if isinstance(value, list) and len(value) > self.chunk_records:
            items = self.encoder.list_items(value) if self.encoder is not None else None
            return {
                "kind": "list_v2",
                "chunks": [
                    self._put(
                        value[i : i + self.chunk_records],
                        None
                        if items is None
                        else b"[" + b",".join(items[i : i + self.chunk_records]) + b"]",
                    )
                    for i in range(0, len(value), self.chunk_records)
                ],
                "starts": list(range(0, len(value), self.chunk_records)),
                "length": len(value),
            }
        if isinstance(value, dict) and len(value) > self.chunk_records:
            keys = sorted(value)
            return {
                "kind": "map",
                "chunks": [
                    self._put({key: value[key] for key in keys[i : i + self.chunk_records]})
                    for i in range(0, len(keys), self.chunk_records)
                ],
            }
        if isinstance(value, dict) and self._size(value) > 65_536:
            if self.content_check is not None:
                # Field names of a split object live in the manifest, not a chunk.
                self.content_check(dict.fromkeys(value))
            return {
                "kind": "object",
                "fields": {key: self._pack(item) for key, item in sorted(value.items())},
            }
        return {
            "kind": "value",
            "chunk": self._put(value, None if self.encoder is None else self.encoder.encode(value)),
        }

    def _size(self, value: Any) -> int:
        return len(_encoded(value)) if self.encoder is None else self.encoder.size(value)

    def encode(self, checkpoint: Checkpoint) -> dict[str, Any]:
        return self._encode_validated_document(
            checkpoint.to_dict(),
            checkpoint_hash=checkpoint.checkpoint_hash,
        )

    def _encode_validated_document(
        self,
        document: dict[str, Any],
        *,
        checkpoint_hash: str,
    ) -> dict[str, Any]:
        """Pack an owned snapshot already checked by Checkpoint validation.

        Never pass an unvalidated external document here.
        """
        manifest = self._prepare_validated_document(document, checkpoint_hash=checkpoint_hash)
        self._write_prepared()
        return manifest

    def _prepare_validated_document(
        self,
        document: dict[str, Any],
        *,
        checkpoint_hash: str,
    ) -> dict[str, Any]:
        """Pack and run content checks without writing; _write_prepared publishes chunks."""
        self._staged, self._staged_bytes = {}, 0
        try:
            body = {"checkpoint_hash": checkpoint_hash, "document": self._pack(document)}
        except BaseException:
            self._staged, self._staged_bytes = None, 0
            raise
        return {
            "schema_version": 1,
            "storage_type": "checkpoint_archive_v2",
            **body,
            "manifest_hash": hashlib.sha256(_encoded(body)).hexdigest(),
        }

    def _write_prepared(self) -> None:
        staged, self._staged, self._staged_bytes = self._staged or {}, None, 0
        for key, compressed in staged.items():
            self._write_chunk(key, compressed)

    def _get(self, key: str) -> Any:
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            raise CheckpointError("invalid checkpoint chunk hash")
        path = self.chunk_directory / f"{key}.json.gz"
        if path.is_symlink() or self.chunk_directory.is_symlink():
            raise CheckpointError("checkpoint chunks cannot follow symlinks")
        try:
            # A single chunk has a hard decompression bound to reject corrupt archives.
            with gzip.open(path, "rb") as stream:
                raw = stream.read(16_777_217)
            if len(raw) > 16_777_216 or hashlib.sha256(raw).hexdigest() != key:
                raise CheckpointError("checkpoint chunk integrity/size mismatch")
            return json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError("checkpoint chunk is missing or corrupt") from exc

    def _unpack(self, node: dict[str, Any], *, depth: int = 0) -> Any:
        if depth > 64 or not isinstance(node, dict):
            raise CheckpointError("invalid checkpoint archive structure")
        if node.get("kind") == "value" and set(node) == {"kind", "chunk"}:
            return self._get(node["chunk"])
        if node.get("kind") == "list_v2" and set(node) == {"kind", "chunks", "starts", "length"}:
            self._indexed_list_metadata(node)
            values = []
            for index, key in enumerate(node["chunks"]):
                chunk = self._get(key)
                if not isinstance(chunk, list) or node["starts"][index] != len(values):
                    raise CheckpointError("indexed list chunk/offset mismatch")
                values.extend(chunk)
            if len(values) != node["length"]:
                raise CheckpointError("indexed list length mismatch")
            return values
        if node.get("kind") == "list" and set(node) == {"kind", "chunks"}:
            values = []
            for key in node["chunks"]:
                chunk = self._get(key)
                if not isinstance(chunk, list):
                    raise CheckpointError("list chunk has incompatible type")
                values.extend(chunk)
            return values
        if node.get("kind") == "map" and set(node) == {"kind", "chunks"}:
            values = {}
            for key in node["chunks"]:
                chunk = self._get(key)
                if not isinstance(chunk, dict) or set(chunk).intersection(values):
                    raise CheckpointError("map chunk has incompatible or duplicate fields")
                values.update(chunk)
            return values
        if node.get("kind") == "object" and set(node) == {"kind", "fields"}:
            return {
                key: self._unpack(value, depth=depth + 1) for key, value in node["fields"].items()
            }
        raise CheckpointError("unsupported checkpoint archive node")

    def decode(self, record: dict[str, Any]) -> Checkpoint:
        self._validate_manifest(record)
        checkpoint = Checkpoint.from_dict(self._unpack(record["document"]))
        if checkpoint.checkpoint_hash != record["checkpoint_hash"]:
            raise CheckpointError("checkpoint archive pointer hash mismatch")
        return checkpoint

    def document(self, record: dict[str, Any]) -> dict[str, Any]:
        """Verify original logical hashes without interpreting a newer model schema.

        Used only for recorded-evidence indexing/compatibility. Simulation restore
        still goes through Checkpoint and current implementation/schema validation.
        """
        self._validate_manifest(record)
        document = self._unpack(record["document"])
        if (
            not isinstance(document, dict)
            or document.get("checkpoint_hash") != record["checkpoint_hash"]
        ):
            raise CheckpointError("checkpoint archive pointer hash mismatch")
        body = {key: value for key, value in document.items() if key != "checkpoint_hash"}
        if hashlib.sha256(_encoded(body)).hexdigest() != record["checkpoint_hash"]:
            raise CheckpointError("checkpoint logical integrity mismatch")
        return document

    def _validate_manifest(self, record: dict[str, Any]) -> None:
        if (
            type(record.get("schema_version")) is not int
            or record.get("schema_version") != 1
            or record.get("storage_type") not in {"checkpoint_archive_v1", "checkpoint_archive_v2"}
        ):
            raise CheckpointError("unsupported checkpoint archive version")
        if record["storage_type"] == "checkpoint_archive_v2":
            body = {"checkpoint_hash": record["checkpoint_hash"], "document": record["document"]}
            if hashlib.sha256(_encoded(body)).hexdigest() != record.get("manifest_hash"):
                raise CheckpointError("checkpoint archive manifest integrity mismatch")

    def _path_node(self, node: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
        for index, field in enumerate(path):
            if node.get("kind") == "object":
                try:
                    node = node["fields"][field]
                except KeyError as exc:
                    raise CheckpointError("checkpoint projection field is missing") from exc
            else:
                value = self._unpack(node)
                for part in path[index:]:
                    if not isinstance(value, dict) or part not in value:
                        raise CheckpointError("checkpoint projection field is missing")
                    value = value[part]
                return {"kind": "inline", "value": value}
        return node

    def project(self, record: dict[str, Any], path: tuple[str, ...]) -> Any:
        """Read a verified named field without reconstructing the WorldState."""
        self._validate_manifest(record)
        node = self._path_node(record["document"], path)
        return node["value"] if node.get("kind") == "inline" else self._unpack(node)

    @staticmethod
    def _indexed_list_metadata(node: dict[str, Any]) -> None:
        starts = node.get("starts")
        chunks = node.get("chunks")
        length = node.get("length")
        if (
            not isinstance(starts, list)
            or not isinstance(chunks, list)
            or not starts
            or len(starts) != len(chunks)
            or starts[0] != 0
            or any(type(value) is not int or value < 0 for value in starts)
            or starts != sorted(set(starts))
            or type(length) is not int
            or starts[-1] >= length
        ):
            raise CheckpointError("invalid indexed list offsets")

    def list_page(
        self, record: dict[str, Any], path: tuple[str, ...], *, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise CheckpointError("invalid checkpoint projection page")
        self._validate_manifest(record)
        node = self._path_node(record["document"], path)
        if node.get("kind") == "list_v2":
            starts, length = node["starts"], node["length"]
            self._indexed_list_metadata(node)
            selected = []
            index = max(0, bisect_right(starts, offset) - 1)
            while index < len(starts) and len(selected) < limit and offset < length:
                chunk = self._get(node["chunks"][index])
                end = starts[index + 1] if index + 1 < len(starts) else length
                if not isinstance(chunk, list) or len(chunk) != end - starts[index]:
                    raise CheckpointError("indexed list chunk length mismatch")
                selected.extend(
                    chunk[
                        max(0, offset - starts[index]) : max(0, offset - starts[index])
                        + limit
                        - len(selected)
                    ]
                )
                index += 1
        elif node.get("kind") == "list":
            raise CheckpointError(
                "legacy list has no seek offsets; use the explicit index migration"
            )
        else:
            value = node["value"] if node.get("kind") == "inline" else self._unpack(node)
            if not isinstance(value, list):
                raise CheckpointError("projection is not a list")
            length = len(value)
            selected = value[offset : offset + limit]
        return {
            "items": selected,
            "offset": offset,
            "total_items": length,
            "next_offset": offset + len(selected) if offset + len(selected) < length else None,
            "checkpoint_hash": record["checkpoint_hash"],
            "projection_integrity": "manifest_and_selected_chunks",
            "full_checkpoint_verified_by_this_read": False,
        }
