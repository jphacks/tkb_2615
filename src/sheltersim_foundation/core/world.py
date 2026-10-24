"""domain固有の状態を追加するための最小WorldState契約。"""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WorldState:
    """1つの実行を表す作業用状態。製品の避難所モデルではありません。"""

    scenario_id: str
    seed: int
    duration_minutes: int
    minute_index: int = 0
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_events: list[dict[str, Any]] = field(default_factory=list)
    model_state: dict[str, Any] = field(default_factory=dict)
    rng_state: dict[str, str | int] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario_id must not be empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if (
            isinstance(self.duration_minutes, bool)
            or not isinstance(self.duration_minutes, int)
            or self.duration_minutes <= 0
        ):
            raise ValueError("duration_minutes must be positive")
        if (
            isinstance(self.minute_index, bool)
            or not isinstance(self.minute_index, int)
            or not 0 <= self.minute_index <= self.duration_minutes
        ):
            raise ValueError("minute_index is outside the simulation duration")
        if self.schema_version != 1:
            raise ValueError("unsupported WorldState schema_version")

    def to_dict(self) -> dict[str, Any]:
        """JSON互換の独立した辞書を返します。"""
        return deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorldState":
        return cls(**deepcopy(value))
