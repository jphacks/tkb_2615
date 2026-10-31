"""Compressed content-addressed checkpoint chunks with bounded read amplification.

A v3 manifest stores the chunks one save adds in one pack file and records where
each of its chunks is. Chunks of v1/v2 archives are one file each and stay readable.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tempfile
from bisect import bisect_right
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CanonicalEncoder, canonical_bytes
from sheltersim.core.saved_items import SavedItems
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricsRecorder

_HASH = re.compile(r"^[a-f0-9]{64}$")

# Compressing a save's new chunks: at most this many threads, each with this much to do.
_COMPRESS_THREADS = 4
_COMPRESS_THREAD_BYTES = 256 * 1024

# Every manifest version this archive reads; saves write the last one.
ARCHIVE_STORAGE_TYPES = frozenset(
    {"checkpoint_archive_v1", "checkpoint_archive_v2", "checkpoint_archive_v3"}
)

# The list a restore reads as item bytes (SavedItems) when the Rust EventLog is there.
_SAVED_EVENT_LOG = ("world_state", "event_log")


def _compress_all(raws: list[bytes]) -> list[bytes]:
    return [gzip.compress(raw, compresslevel=3, mtime=0) for raw in raws]


def _compressed(raws: list[bytes]) -> list[bytes]:
    """Each chunk compressed, in order; the same bytes however many threads do it.

    zlib releases the GIL. The caller waits here, so a save stays on its own thread.
    """
    total = sum(map(len, raws))
    workers = min(_COMPRESS_THREADS, os.cpu_count() or 1, total // _COMPRESS_THREAD_BYTES)
    if workers <= 1:
        return _compress_all(raws)
    # One long run per thread: a task per chunk would cost as much as a small chunk does.
    runs: list[list[bytes]] = [[]]
    size = 0
    for raw in raws:
        if size >= total / workers:
            runs.append([])
            size = 0
        runs[-1].append(raw)
        size += len(raw)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [item for run in pool.map(_compress_all, runs) for item in run]


class CheckpointArchive:
    """Storage representation only; restores the unchanged Checkpoint and its hashes."""

    def __init__(
        self,
        directory: Path,
        *,
        chunk_records: int = 128,
        max_new_bytes: int | None = None,
        content_check: Callable[[list[tuple[Any, bytes | None]]], None] | None = None,
        encoder: CanonicalEncoder | None = None,
        known_locations: dict[str, tuple[str, int, int]] | None = None,
    ) -> None:
        self.directory = directory
        self.chunk_records = chunk_records
        self.max_new_bytes = max_new_bytes
        # Called with every chunk that is not already stored, as (value, canonical JSON)
        # pairs, and with large-object field names. Stored chunks passed the same check
        # when first written. A value may be a function returning it, so a check that
        # reads the bytes does not parse a slice of SavedItems.
        self.content_check = content_check
        # The encoder of a just-captured Checkpoint already holds the bytes of the
        # history it reused; chunks and sizes are the same with or without it.
        self.encoder = encoder
        self.new_bytes = 0
        self.new_files = 0
        self.chunk_directory = directory / "chunks"
        self._has_chunk_files: bool | None = None
        self.pack_directory = directory / "packs"
        # The new chunks of the save being prepared: their values and bytes, then each
        # chunk compressed, once the content checks have passed.
        self._staged: dict[str, tuple[Any, bytes]] | None = None
        # Where saved chunks in packs are: the locations of a manifest saved before
        # and, through the encoder, of every save of the same writer. A save refers
        # to them instead of writing them again (any other chunk is a v1/v2 file).
        self._known = dict(known_locations or {})
        # The pack chunks the manifest being prepared refers to, and the new pack.
        self._referenced: dict[str, tuple[str, int, int] | None] = {}
        self._new_pack: tuple[str, bytes] | None = None
        self._prepared: dict[str, tuple[str, int, int]] = {}
        # The locations of the manifest being read.
        self._locations: dict[str, tuple[str, int, int]] = {}

    def _check_directories(self) -> None:
        for directory in (self.chunk_directory, self.pack_directory):
            if directory.is_symlink() or not directory.resolve().is_relative_to(
                self.directory.resolve()
            ):
                raise CheckpointError("checkpoint chunk directory cannot follow symlinks")

    def _stored(self, key: str) -> bool:
        """Whether a chunk is saved or staged already; the manifest then refers to it."""
        if key in self._referenced:
            return True
        if self._staged is not None and key in self._staged:
            self._referenced[key] = None
            return True
        location = self._known.get(key)
        if location is None and self.encoder is not None:
            location = self.encoder.pack_locations.get(key)
        if location is not None:
            self._referenced[key] = location
            return True
        if self._has_chunk_files is None:
            # Only v1/v2 saves wrote that directory; nearly every new chunk asks.
            self._has_chunk_files = self.chunk_directory.exists()
        if self._has_chunk_files and (self.chunk_directory / f"{key}.json.gz").exists():
            # A v1/v2 chunk file; the manifest records no location for it.
            self._referenced[key] = None
            return True
        return False

    def _put(self, value: Any, raw: bytes | None = None) -> str:
        if self._staged is None:
            raise CheckpointError("checkpoint chunks are written only by a prepared save")
        if raw is None:
            raw = canonical_bytes(value)
        if len(raw) > 16_777_216:
            raise CheckpointError("checkpoint chunk exceeds the supported size")
        key = hashlib.sha256(raw).hexdigest()
        if self._stored(key):
            return key
        self._staged[key] = (value, raw)
        self._referenced[key] = None
        return key

    def _compressed_chunks(self, staged: dict[str, tuple[Any, bytes]]) -> dict[str, bytes]:
        """Check all new chunks of a save at once, then compress them."""
        if self.content_check is not None and staged:
            self.content_check(list(staged.values()))
        compressed = dict(
            zip(staged, _compressed([raw for _, raw in staged.values()]), strict=True)
        )
        if (
            self.max_new_bytes is not None
            and self.new_bytes + sum(map(len, compressed.values())) > self.max_new_bytes
        ):
            raise CheckpointError("checkpoint storage capacity exceeded")
        return compressed

    def _write_pack(self, name: str, data: bytes) -> None:
        """Publish one pack; a pack with the same name holds the same bytes."""
        path = self.pack_directory / f"{name}.pack"
        self.pack_directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pack-", dir=self.pack_directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                self.new_bytes += len(data)
                self.new_files += 1
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _pack(self, value: Any) -> dict[str, Any]:
        if isinstance(value, (list, SavedItems)) and len(value) > self.chunk_records:
            if isinstance(value, SavedItems):
                items = value.raws
            else:
                items = self.encoder.list_items(value) if self.encoder is not None else None
            return {
                "kind": "list_v2",
                "chunks": self._list_chunks(value, items),
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
        if isinstance(value, dict) and self._exceeds(value, 65_536):
            if self.content_check is not None:
                # Field names of a split object live in the manifest, not a chunk.
                self.content_check([(dict.fromkeys(value), None)])
            return {
                "kind": "object",
                "fields": {key: self._pack(item) for key, item in sorted(value.items())},
            }
        return {
            "kind": "value",
            "chunk": self._put(value, None if self.encoder is None else self.encoder.encode(value)),
        }

    def _list_chunks(self, value: Sequence[Any], items: list[bytes] | None) -> list[str]:
        leading: list[str] = []
        if items and self.encoder is not None:
            leading = self.encoder.leading_chunks(value, items, self.chunk_records)
        # What this writer stored before needs no slice read; the new chunks are at the end.
        stored = 0
        for key in leading:
            if not self._stored(key):
                break
            stored += 1
        del leading[stored:]
        leading.extend(
            self._put_slice(value, items, start)
            for start in range(stored * self.chunk_records, len(value), self.chunk_records)
        )
        return leading

    def _put_slice(self, value: Sequence[Any], items: list[bytes] | None, start: int) -> str:
        count = min(self.chunk_records, len(value) - start)
        if items is None:
            return self._put(value[start : start + count])
        sealed = self.encoder is not None and self.encoder.append_only(value)
        if sealed:
            key = self.encoder.chunk_key(items, start, count)
            if key is not None and self._stored(key):
                # The same item bytes as a chunk saved before by this writer.
                return key
        raw = b"[" + b",".join(items[start : start + count]) + b"]"
        key = hashlib.sha256(raw).hexdigest()
        if len(raw) > 16_777_216 or not self._stored(key):
            # Only a new chunk reads its items (SavedItems parses them).
            key = self._put(lambda: value[start : start + count], raw)
        if sealed:
            self.encoder.remember_chunk(items, start, count, key)
        return key

    def _exceeds(self, value: Any, limit: int) -> bool:
        if self.encoder is None:
            return len(canonical_bytes(value)) > limit
        return self.encoder.exceeds(value, limit)

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
        """Pack and run content checks without writing; _write_prepared publishes the pack."""
        self._check_directories()
        self._staged, self._referenced = {}, {}
        try:
            packed = self._pack(document)
            staged = self._compressed_chunks(self._staged)
        finally:
            self._staged = None
        self._new_pack = None
        if staged:
            data = b"".join(staged.values())
            name = hashlib.sha256(data).hexdigest()
            offset = 0
            for key, compressed in staged.items():
                self._referenced[key] = (name, offset, len(compressed))
                offset += len(compressed)
            self._new_pack = (name, data)
        self._prepared = {
            key: location for key, location in self._referenced.items() if location is not None
        }
        body = {
            "checkpoint_hash": checkpoint_hash,
            "document": packed,
            "locations": _location_table(self._prepared),
        }
        return {
            "schema_version": 1,
            "storage_type": "checkpoint_archive_v3",
            **body,
            "manifest_hash": hashlib.sha256(canonical_bytes(body)).hexdigest(),
        }

    def _write_prepared(self) -> None:
        pack, self._new_pack = self._new_pack, None
        if pack is not None:
            self._write_pack(*pack)
        # Only chunks now on disk are referred to by later saves.
        prepared, self._prepared = self._prepared, {}
        self._known.update(prepared)
        if self.encoder is not None:
            self.encoder.pack_locations.update(prepared)

    def _get(self, key: str) -> Any:
        raw = self._get_raw(key)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CheckpointError("checkpoint chunk is missing or corrupt") from exc

    def _get_raw(self, key: str) -> bytes:
        """The hash-checked bytes of one chunk."""
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            raise CheckpointError("invalid checkpoint chunk hash")
        location = self._locations.get(key)
        directory = self.chunk_directory if location is None else self.pack_directory
        path = (
            directory / f"{key}.json.gz" if location is None else directory / f"{location[0]}.pack"
        )
        if path.is_symlink() or directory.is_symlink():
            raise CheckpointError("checkpoint chunks cannot follow symlinks")
        try:
            if location is None:
                # A single chunk has a hard decompression bound to reject corrupt archives.
                with gzip.open(path, "rb") as stream:
                    raw = stream.read(16_777_217)
            else:
                with path.open("rb") as stream:
                    stream.seek(location[1])
                    compressed = stream.read(location[2])
                if len(compressed) != location[2]:
                    raise CheckpointError("checkpoint chunk integrity/size mismatch")
                with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as member:
                    raw = member.read(16_777_217)
            if len(raw) > 16_777_216 or hashlib.sha256(raw).hexdigest() != key:
                raise CheckpointError("checkpoint chunk integrity/size mismatch")
            return raw
        except (OSError, EOFError) as exc:
            raise CheckpointError("checkpoint chunk is missing or corrupt") from exc

    def _unpack(
        self,
        node: dict[str, Any],
        *,
        depth: int = 0,
        path: tuple[str, ...] = (),
        saved: bool = False,
    ) -> Any:
        # With saved, a restore reads the EventLog as item bytes, which the Rust
        # EventLog parses and checks (SavedItems); anything else is parsed here.
        if depth > 64 or not isinstance(node, dict):
            raise CheckpointError("invalid checkpoint archive structure")
        if node.get("kind") == "value" and set(node) == {"kind", "chunk"}:
            return self._get(node["chunk"])
        if node.get("kind") == "list_v2" and set(node) == {"kind", "chunks", "starts", "length"}:
            self._indexed_list_metadata(node)
            if saved and path == _SAVED_EVENT_LOG and native.available():
                return self._saved_items(node)
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
                key: self._unpack(value, depth=depth + 1, path=(*path, key), saved=saved)
                for key, value in node["fields"].items()
            }
        raise CheckpointError("unsupported checkpoint archive node")

    def _saved_items(self, node: dict[str, Any]) -> SavedItems:
        raws: list[bytes] = []
        for index, key in enumerate(node["chunks"]):
            if node["starts"][index] != len(raws):
                raise CheckpointError("indexed list chunk/offset mismatch")
            try:
                items = native.split_json_array(self._get_raw(key))
            except ValueError as exc:
                raise CheckpointError("list chunk has incompatible type") from exc
            assert items is not None
            raws.extend(items)
        if len(raws) != node["length"]:
            raise CheckpointError("indexed list length mismatch")
        return SavedItems(raws)

    def decode(self, record: dict[str, Any]) -> Checkpoint:
        return self.decode_restored(record)[0]

    def decode_restored(
        self, record: dict[str, Any]
    ) -> tuple[Checkpoint, tuple[WorldState, MetricsRecorder]]:
        """decode(), and the world and metrics its restore check built."""
        self._validate_manifest(record)
        # Each chunk is parsed anew, so the unpacked document is not shared.
        checkpoint, restored = Checkpoint._load(self._unpack(record["document"], saved=True))
        if checkpoint.checkpoint_hash != record["checkpoint_hash"]:
            raise CheckpointError("checkpoint archive pointer hash mismatch")
        return checkpoint, restored

    def unpack_document(self, record: dict[str, Any]) -> Any:
        """The unpacked Checkpoint document of a valid manifest, before any hash check."""
        self._validate_manifest(record)
        return self._unpack(record["document"])

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
        try:
            _, checkpoint_hash = Checkpoint.document_hashes(document)
        except KeyError as exc:
            raise CheckpointError("checkpoint logical integrity mismatch") from exc
        if checkpoint_hash != record["checkpoint_hash"]:
            raise CheckpointError("checkpoint logical integrity mismatch")
        return document

    def _validate_manifest(self, record: dict[str, Any]) -> None:
        if (
            type(record.get("schema_version")) is not int
            or record.get("schema_version") != 1
            or record.get("storage_type") not in ARCHIVE_STORAGE_TYPES
        ):
            raise CheckpointError("unsupported checkpoint archive version")
        self._locations = {}
        if record["storage_type"] == "checkpoint_archive_v1":
            return
        body = {"checkpoint_hash": record["checkpoint_hash"], "document": record["document"]}
        if record["storage_type"] == "checkpoint_archive_v3":
            body["locations"] = record.get("locations")
        if hashlib.sha256(canonical_bytes(body)).hexdigest() != record.get("manifest_hash"):
            raise CheckpointError("checkpoint archive manifest integrity mismatch")
        if record["storage_type"] == "checkpoint_archive_v3":
            self._locations = self.pack_locations(record)

    @staticmethod
    def pack_locations(record: dict[str, Any]) -> dict[str, tuple[str, int, int]]:
        """The chunk locations of a v3 manifest, empty for v1/v2; malformed ones fail."""
        if record.get("storage_type") != "checkpoint_archive_v3":
            return {}
        table = record.get("locations")
        packs = table.get("packs") if isinstance(table, dict) else None
        chunks = table.get("chunks") if isinstance(table, dict) else None
        if (
            not isinstance(table, dict)
            or set(table) != {"packs", "chunks"}
            or not isinstance(packs, list)
            or not all(isinstance(name, str) and _HASH.fullmatch(name) for name in packs)
            or len(set(packs)) != len(packs)
            or not isinstance(chunks, dict)
            or not all(
                isinstance(key, str)
                and _HASH.fullmatch(key)
                and isinstance(location, list)
                and len(location) == 3
                and all(type(number) is int for number in location)
                and 0 <= location[0] < len(packs)
                and location[1] >= 0
                and 0 < location[2] <= 16_777_216 + 65_536
                for key, location in chunks.items()
            )
        ):
            raise CheckpointError("invalid checkpoint archive locations")
        return {
            key: (packs[index], offset, length) for key, (index, offset, length) in chunks.items()
        }

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


def _location_table(locations: dict[str, tuple[str, int, int]]) -> dict[str, Any]:
    """The manifest form of chunk locations: each pack name once, chunks by key."""
    packs = sorted({name for name, _, _ in locations.values()})
    number = {name: index for index, name in enumerate(packs)}
    return {
        "packs": packs,
        "chunks": {
            key: [number[name], offset, length]
            for key, (name, offset, length) in sorted(locations.items())
        },
    }
