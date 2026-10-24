"""Seeded, order-independent random draws for reproducible simulation decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

RNGKeyPart = str | int


class KeyedRandom(Protocol):
    """Interface injected into simulation phases that need random values."""

    @property
    def metadata(self) -> dict[str, str | int]: ...

    def random(self, *key_parts: RNGKeyPart) -> float: ...


class CounterRNG:
    """Stateless SHA-256 draws keyed by seed and stable domain identifiers."""

    algorithm_id = "sha256-keyed-v1"

    def __init__(self, seed: int) -> None:
        if seed < 0:
            raise ValueError("seed cannot be negative")
        self.seed = seed

    @property
    def metadata(self) -> dict[str, str | int]:
        """Version and seed needed to identify this stateless random stream."""
        return {"algorithm": self.algorithm_id, "seed": self.seed}

    def random(self, *key_parts: RNGKeyPart) -> float:
        """Return the same value for a seed/key pair, independent of call order."""
        if not key_parts:
            raise ValueError("at least one stable RNG key part is required")
        if any(not isinstance(part, (str, int)) or isinstance(part, bool) for part in key_parts):
            raise TypeError("RNG key parts must be strings or integers")
        encoded_key = json.dumps(
            list(key_parts), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        seed_bytes = str(self.seed).encode("ascii")
        digest = hashlib.sha256(
            b"sheltersim:sha256-keyed-v1\0" + seed_bytes + b"\0" + encoded_key
        ).digest()
        value = int.from_bytes(digest[:8], byteorder="big", signed=False)
        return value / 2**64
