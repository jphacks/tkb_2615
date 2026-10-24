"""Installed queue-priority and abandonment operators for staffed facilities."""

from __future__ import annotations

from typing import Any, Protocol

from sheltersim.core.world import WorldState


class FacilityQueuePolicy(Protocol):
    model_id: str
    source_id: str
    formula: str

    def priority(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> float: ...

    def should_abandon(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> bool: ...

    def retry_delay_minutes(self) -> int: ...


class NeedWeightedPhysicalQueuePolicy:
    """Need triage with aging and a retryable, bounded wait assumption."""

    model_id = "need-weighted-physical-queue-v1"
    source_id = "ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1"
    formula = "priority = service_need + min(wait_minutes / 1440, 0.1); abandon when wait >= service_limit and need < persistence_threshold"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = {} if config is None else config
        if not isinstance(config, dict) or set(config) - {
            "schema_version", "queue_capacity_by_facility",
            "abandon_after_minutes_by_service", "retry_delay_minutes",
            "persistence_urgency_threshold",
        }:
            raise ValueError("facility_queues config must be an object")
        if config.get("schema_version", 1) != 1:
            raise ValueError("facility_queues requires schema_version 1")
        self._wait_limits = {"toilet": 120, "water": 240, "meal": 360}
        supplied_limits = config.get("abandon_after_minutes_by_service", {})
        if not isinstance(supplied_limits, dict) or set(supplied_limits) - set(self._wait_limits):
            raise ValueError("queue abandonment limits contain an unsupported service")
        self._wait_limits.update(supplied_limits)
        if any(
            type(value) is not int or not 1 <= value <= 10080
            for value in self._wait_limits.values()
        ):
            raise ValueError("queue abandonment limits must be within 1..10080 minutes")
        self._retry_delay = config.get("retry_delay_minutes", 15)
        if type(self._retry_delay) is not int or not 1 <= self._retry_delay <= 1440:
            raise ValueError("queue retry delay must be between 1 and 1440 minutes")
        self._persistence_threshold = config.get("persistence_urgency_threshold", 0.8)
        if (
            not isinstance(self._persistence_threshold, (int, float))
            or isinstance(self._persistence_threshold, bool)
            or not 0.0 <= self._persistence_threshold <= 1.0
        ):
            raise ValueError("queue persistence threshold must be normalized to 0..1")

    def priority(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> float:
        return _service_need(world, agent_id, service) + 0.1 * min(1.0, max(0, wait_minutes) / 1440.0)

    def should_abandon(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> bool:
        return (
            wait_minutes >= self._wait_limits[service]
            and _service_need(world, agent_id, service) < self._persistence_threshold
        )

    def retry_delay_minutes(self) -> int:
        return self._retry_delay


class StrictFIFOQueuePolicy:
    """Reference alternative that keeps first-in, first-out reservations."""

    model_id = "strict-fifo-queue-v1"
    source_id = "ASM-STRICT-FIFO-QUEUE-V1"
    formula = "priority = 0; queue membership persists until service or explicit cancellation"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        if config is not None and not isinstance(config, dict):
            raise ValueError("facility_queues config must be an object")

    def priority(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> float:
        del world, agent_id, service, wait_minutes
        return 0.0

    def should_abandon(self, world: WorldState, agent_id: str, service: str, wait_minutes: int) -> bool:
        del world, agent_id, service, wait_minutes
        return False

    def retry_delay_minutes(self) -> int:
        return 15


def create_queue_policy(configs: dict[str, dict[str, Any]]) -> FacilityQueuePolicy:
    """Resolve an explicitly registered policy or the geometry-aware default."""
    raw = configs.get("facility_queues", {})
    selection = configs.get("policy_registry", {}).get("queue")
    if selection is not None:
        from sheltersim.policies.registry import create_installed_model

        return create_installed_model("queue", configs)
    if "facility_geometry" in configs:
        return NeedWeightedPhysicalQueuePolicy(raw)
    return StrictFIFOQueuePolicy(raw)


def _service_need(world: WorldState, agent_id: str, service: str) -> float:
    needs = world.agents[agent_id].needs
    value = {
        "toilet": needs.toilet_urgency,
        "water": needs.hydration_need,
        "meal": needs.hunger,
    }[service]
    return min(1.0, max(0.0, float(value)))
