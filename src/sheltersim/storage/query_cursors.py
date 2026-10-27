"""Signed progress for bounded evidence scans that claim an earliest difference."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from sheltersim.storage.projects import ProjectStore, StorageError, canonical


def _key(store: ProjectStore) -> bytes:
    # Service-local signing material, not a provider key or saved Scenario field.
    path = store._path("query-signing-key")
    with store.transaction():
        if not path.exists():
            fd = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        if path.is_symlink():
            raise StorageError("unsafe_query_signing_key")
        value = path.read_bytes()
        if len(value) != 32:
            raise StorageError("query_signing_key_unavailable")
    return value


def seal(store: ProjectStore, payload: str) -> str:
    tag = hmac.new(_key(store), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(canonical({"payload": payload, "mac": tag}).encode()).decode()


def unseal(store: ProjectStore, token: str) -> str:
    try:
        data = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if (
            set(data) != {"payload", "mac"}
            or not isinstance(data["payload"], str)
            or not isinstance(data["mac"], str)
        ):
            raise ValueError("shape")
        tag = hmac.new(_key(store), data["payload"].encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(tag, data["mac"]):
            raise ValueError("signature")
        return data["payload"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise StorageError("invalid_scan_progress_cursor") from exc
