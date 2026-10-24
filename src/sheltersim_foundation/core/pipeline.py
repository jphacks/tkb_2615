"""Scenario検証から実行・Checkpoint出力までをつなぐ薄いpipeline。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sheltersim_foundation.core.checkpoint import Checkpoint
from sheltersim_foundation.core.engine import PhaseHandler, TickEngine
from sheltersim_foundation.core.world import WorldState

ScenarioValidator = Callable[[Any], Any]
WorldFactory = Callable[[Any], WorldState]
CheckpointSink = Callable[[Checkpoint], None]


@dataclass(frozen=True)
class PipelineResult:
    world: WorldState
    checkpoint_count: int


class SimulationPipeline:
    """各段階を注入可能にしてcoreと入出力を分離します。"""

    def __init__(
        self,
        *,
        validate_scenario: ScenarioValidator,
        create_world: WorldFactory,
        phase_handler: PhaseHandler | None = None,
        checkpoint_sink: CheckpointSink | None = None,
        checkpoint_interval: int | None = None,
    ) -> None:
        if checkpoint_interval is not None and (
            isinstance(checkpoint_interval, bool)
            or not isinstance(checkpoint_interval, int)
            or checkpoint_interval <= 0
        ):
            raise ValueError("checkpoint_interval must be a positive integer")
        if checkpoint_interval is not None and checkpoint_sink is None:
            raise ValueError("checkpoint_sink is required with checkpoint_interval")
        self.validate_scenario = validate_scenario
        self.create_world = create_world
        self.phase_handler = phase_handler
        self.checkpoint_sink = checkpoint_sink
        self.checkpoint_interval = checkpoint_interval

    def run(self, scenario_input: Any) -> PipelineResult:
        scenario = self.validate_scenario(scenario_input)
        world = self.create_world(scenario)
        world.validate()
        engine = TickEngine(world, self.phase_handler) if self.phase_handler else TickEngine(world)
        checkpoint_count = 0

        def save_checkpoint() -> None:
            nonlocal checkpoint_count
            if self.checkpoint_sink is not None:
                self.checkpoint_sink(Checkpoint.capture(world))
                checkpoint_count += 1

        if self.checkpoint_interval is not None:
            save_checkpoint()
        while not engine.clock.complete:
            engine.step()
            if (
                self.checkpoint_interval is not None
                and world.minute_index % self.checkpoint_interval == 0
            ):
                save_checkpoint()
        if self.checkpoint_sink is not None and (
            self.checkpoint_interval is None or world.minute_index % self.checkpoint_interval != 0
        ):
            save_checkpoint()
        return PipelineResult(world=world, checkpoint_count=checkpoint_count)
