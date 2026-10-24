"""Persistent, hash-keyed response tape used by record/replay and checkpoint branches."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sheltersim.llm.contracts import ProviderResponse


class LLMCacheError(ValueError):
    """Raised when a response tape is corrupt or from an unsupported schema."""


def _entry_document(response: ProviderResponse) -> dict[str, Any]:
    # Use JSON-native lists: Project storage compares Checkpoints with their sanitized
    # copy, and a tuple there would differ from the list it becomes on disk.
    document = asdict(response)
    document["citations"] = list(document["citations"])
    return document


@dataclass
class ResponseTape:
    """Small local store of accepted API outputs; cache keys include all decision inputs."""

    path: Path | None = None
    entries: dict[str, ProviderResponse] | None = None

    CURRENT_SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        self.entries = dict(self.entries or {})
        if self.path is not None and self.path.exists():
            self._load()

    def get(self, key: str) -> ProviderResponse | None:
        """Return a detached response for one exact, fully-versioned cache key."""
        assert self.entries is not None
        return self.entries.get(key)

    def put(self, key: str, value: ProviderResponse) -> None:
        """Persist an accepted response atomically when a path is configured."""
        if not key:
            raise ValueError("cache key cannot be empty")
        assert self.entries is not None
        self.entries[key] = value
        self.save()

    def save(self) -> None:
        """Atomically persist the response tape without prompts or API credentials."""
        if self.path is None:
            return
        assert self.entries is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = self.to_dict()
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                document,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def to_dict(self) -> dict[str, Any]:
        """Return the full response tape for an independent Checkpoint runtime snapshot."""
        assert self.entries is not None
        return {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "entries": {
                key: _entry_document(response) for key, response in sorted(self.entries.items())
            },
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> ResponseTape:
        """Restore a detached response tape from checkpoint-owned data."""
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != cls.CURRENT_SCHEMA_VERSION
            or not isinstance(document.get("entries"), dict)
        ):
            raise LLMCacheError("unsupported response tape schema")
        try:
            entries = {key: ProviderResponse(**value) for key, value in document["entries"].items()}
        except (TypeError, ValueError) as exc:
            raise LLMCacheError(f"invalid response tape entry: {exc}") from exc
        return cls(entries=entries)

    def _load(self) -> None:
        assert self.path is not None
        try:
            document: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMCacheError(f"could not load response tape: {exc}") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != self.CURRENT_SCHEMA_VERSION
            or not isinstance(document.get("entries"), dict)
        ):
            raise LLMCacheError("unsupported response tape schema")
        try:
            self.entries = {
                key: ProviderResponse(**value) for key, value in document["entries"].items()
            }
        except (TypeError, ValueError) as exc:
            raise LLMCacheError(f"invalid response tape entry: {exc}") from exc
