"""最小の内容hash付きCheckpoint形式。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sheltersim_foundation.core.world import WorldState


class CheckpointError(ValueError):
    """Checkpointの形式または内容が不正です。"""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CheckpointError("state is not JSON-compatible") from exc


@dataclass(frozen=True)
class Checkpoint:
    """検証・復元境界を示す作業用Checkpoint。"""

    schema_version: int
    state: dict[str, Any]
    state_hash: str

    @classmethod
    def capture(cls, world: WorldState) -> "Checkpoint":
        state = world.to_dict()
        digest = hashlib.sha256(_canonical_json(state)).hexdigest()
        return cls(schema_version=1, state=state, state_hash=digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "state_hash": self.state_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Checkpoint":
        try:
            checkpoint = cls(
                schema_version=value["schema_version"],
                state=value["state"],
                state_hash=value["state_hash"],
            )
        except (KeyError, TypeError) as exc:
            raise CheckpointError("Checkpoint fields are missing or invalid") from exc
        if checkpoint.schema_version != 1:
            raise CheckpointError("unsupported Checkpoint schema_version")
        actual_hash = hashlib.sha256(_canonical_json(checkpoint.state)).hexdigest()
        if actual_hash != checkpoint.state_hash:
            raise CheckpointError("Checkpoint state hash mismatch")
        return checkpoint

    def restore(self) -> WorldState:
        verified = self.from_dict(self.to_dict())
        return WorldState.from_dict(verified.state)
