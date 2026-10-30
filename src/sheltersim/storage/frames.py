"""Replaceable cache of timeline frames rendered by the writer at each checkpoint.

A frame is a read model derived from one checkpoint. The worker already holds the
exact WorldState when it records a checkpoint, so rendering there avoids decoding,
validating and restoring the whole history on every timeline GET. A missing,
stale or corrupt entry is never an error: readers fall back to reconstruction.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from typing import Any

from sheltersim.core.checkpoint_cache import CanonicalEncoder, canonical_bytes
from sheltersim.storage.projects import (
    ProjectStore,
    StorageError,
    canonical,
    retry_while_shared,
)

# Bump when world_frame output changes so older cached frames are recomputed.
FRAME_PROJECTION_VERSION = "timeline-frame-v1"
_HASH = re.compile(r"^[a-f0-9]{64}$")
_MAX_BYTES = 64 * 1024 * 1024


class FrameCache:
    """Per-Run frames keyed by the checkpoint hash they were rendered from."""

    def __init__(self, store: ProjectStore, project_id: str, run_id: str) -> None:
        self.store = store
        self.directory = store._path(project_id, "runs", run_id, "frames")

    def _file(self, checkpoint_hash: str) -> Any:
        if not isinstance(checkpoint_hash, str) or not _HASH.fullmatch(checkpoint_hash):
            raise StorageError("invalid checkpoint hash for frame cache")
        return self.directory / f"{checkpoint_hash}.json.gz"

    def put(self, checkpoint_hash: str, frame: dict[str, Any]) -> bool:
        """Best effort; returns False when the cache would exceed storage capacity."""
        path = self._file(checkpoint_hash)
        if path.exists():
            return True
        encoded_frame = canonical_bytes(frame)
        document = {
            "schema_version": 1,
            "projection_version": FRAME_PROJECTION_VERSION,
            "checkpoint_hash": checkpoint_hash,
            "frame_hash": hashlib.sha256(encoded_frame).hexdigest(),
            "frame": frame,
        }
        # The document's JSON holds the frame's JSON; encode the frame once.
        encoder = CanonicalEncoder()
        encoder.remember(frame, encoded_frame, document)
        data = gzip.compress(encoder.encode(document) + b"\n", compresslevel=3, mtime=0)
        if self.store.usage()["bytes"] + len(data) > self.store.max_bytes:
            return False
        if self.directory.is_symlink():
            raise StorageError("frame cache directory cannot follow symlinks")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".frame-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            retry_while_shared(lambda: os.replace(name, path))
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return True

    def get(self, checkpoint_hash: str) -> dict[str, Any] | None:
        """Return the verified frame, or None when it must be reconstructed."""
        path = self._file(checkpoint_hash)
        if not path.exists() or path.is_symlink() or self.directory.is_symlink():
            return None
        try:
            with gzip.open(path, "rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return None
            document = json.loads(raw)
        except (OSError, EOFError, ValueError):
            return None
        if (
            not isinstance(document, dict)
            or set(document)
            != {"schema_version", "projection_version", "checkpoint_hash", "frame_hash", "frame"}
            or document["schema_version"] != 1
            or document["projection_version"] != FRAME_PROJECTION_VERSION
            or document["checkpoint_hash"] != checkpoint_hash
            or not isinstance(document["frame"], dict)
        ):
            return None
        encoded_frame = canonical(document["frame"]).encode()
        if hashlib.sha256(encoded_frame).hexdigest() != document["frame_hash"]:
            return None
        return document["frame"]
