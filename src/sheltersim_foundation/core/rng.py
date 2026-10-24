"""seedと安定したkeyから再現可能な乱数を返します。"""

import hashlib
import json


class CounterRNG:
    """呼び出し順に依存しないSHA-256 keyed RNG。"""

    algorithm_id = "sha256-keyed-v1"

    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        self.seed = seed

    @property
    def metadata(self) -> dict[str, str | int]:
        return {"algorithm": self.algorithm_id, "seed": self.seed}

    def random(self, *key_parts: str | int) -> float:
        if not key_parts:
            raise ValueError("at least one stable RNG key part is required")
        if any(not isinstance(part, (str, int)) or isinstance(part, bool) for part in key_parts):
            raise TypeError("RNG key parts must be strings or integers")
        encoded = json.dumps(
            list(key_parts), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        digest = hashlib.sha256(
            b"sheltersim:sha256-keyed-v1\0" + str(self.seed).encode("ascii") + b"\0" + encoded
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2**64
