"""The metrics phase: the minute's snapshot, and closing open actions at the end of a Run."""

from __future__ import annotations

from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import finalize_active_actions


class MetricsPhase:
    """Steps of TickPhase.COLLECT_METRICS, a mixin of OfflineSimulationController."""

    def _run_metrics_phase(self, world: WorldState, minute: int) -> None:
        self.artifacts.capture_metric(world, minute=minute)
        self._finalize_actions_at_end(world, minute)

    def _finalize_actions_at_end(self, world: WorldState, minute: int) -> None:
        if minute + 1 >= world.duration_minutes:
            finalize_active_actions(world, minute, active_actions=self.active_actions)
