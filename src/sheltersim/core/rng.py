"""Seeded, order-independent random draws for reproducible simulation decisions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

RNGKeyPart = str | int

# Text that JSON writes between quotes unchanged (ensure_ascii=False escapes only
# control characters, backslashes and quotes).
_PLAIN_TEXT = re.compile(r'[^\x00-\x1f\\"]*')


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
        # The key is json.dumps(list(key_parts), ensure_ascii=False,
        # separators=(",", ":")), written part by part without building an encoder.
        encoded = []
        for part in key_parts:
            if type(part) is str and _PLAIN_TEXT.fullmatch(part):
                encoded.append('"' + part + '"')
            elif type(part) is int:
                encoded.append(str(part))
            elif not isinstance(part, (str, int)) or isinstance(part, bool):
                raise TypeError("RNG key parts must be strings or integers")
            else:
                encoded.append(json.dumps(part, ensure_ascii=False, allow_nan=False))
        encoded_key = ("[" + ",".join(encoded) + "]").encode("utf-8")
        seed_bytes = str(self.seed).encode("ascii")
        digest = hashlib.sha256(
            b"sheltersim:sha256-keyed-v1\0" + seed_bytes + b"\0" + encoded_key
        ).digest()
        value = int.from_bytes(digest[:8], byteorder="big", signed=False)
        return value / 2**64
