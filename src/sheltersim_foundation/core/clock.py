"""実時間に依存しない整数分単位のシミュレーション時計。"""

from dataclasses import dataclass


class ClockExhaustedError(RuntimeError):
    """設定された実行時間を超えて進めようとしたときに発生します。"""


@dataclass
class SimulationClock:
    """現在位置を次に実行するtickとして扱う有限時計。"""

    duration_minutes: int
    minute_index: int = 0

    def __post_init__(self) -> None:
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

    @property
    def complete(self) -> bool:
        return self.minute_index >= self.duration_minutes

    def advance(self) -> None:
        if self.complete:
            raise ClockExhaustedError("simulation clock is already complete")
        self.minute_index += 1
