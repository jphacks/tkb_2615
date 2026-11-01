"""Minute-level observational KPIs for opt-in water and meal operations."""

from __future__ import annotations

from typing import Any

from sheltersim.core.kernels import kernel_first
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricSnapshot

OPERATIONAL_KPI_FIELDS = (
    "minute_index",
    "toilet_urgency_mean",
    "hunger_need_mean",
    "hydration_need_mean",
    "water_queue_length",
    "meal_queue_length",
    "water_active_service_count",
    "meal_active_service_count",
    "water_service_wait_count",
    "water_service_wait_total_minutes",
    "water_service_wait_mean_minutes",
    "water_service_wait_max_minutes",
    "meal_service_wait_count",
    "meal_service_wait_total_minutes",
    "meal_service_wait_mean_minutes",
    "meal_service_wait_max_minutes",
    "water_outage_stall_count",
    "resource_stock_stall_count",
    "water_ml_remaining",
    "food_meal_units_remaining",
    "water_ml_reserved",
    "food_meal_units_reserved",
    "stress_mean",
    "staff_busy_count",
    "staff_load_ratio",
    "unresolved_support_request_count",
    "support_no_qualified_worker_count",
    "support_no_qualified_worker_request_minutes",
    "support_fairness_promoted_assignment_count",
    "support_wait_staff_busy_request_minutes",
    "support_wait_staff_off_shift_request_minutes",
)


@kernel_first("metrics", "kpi_values")
def operational_kpi_values(world: WorldState, snapshot: MetricSnapshot) -> dict[str, Any]:
    """Return detached aggregate measurements without changing simulation state."""
    if world.schema_version != 6 or "resource_service_v1" not in world.model_state:
        raise ValueError("operational KPIs require resource-service WorldState v6")
    if snapshot.minute_index != world.minute_index:
        raise ValueError("operational KPI minute must match WorldState")

    agents = tuple(world.agents.values())
    agent_count = len(agents)

    def mean_need(name: str) -> float:
        return sum(float(getattr(agent.needs, name)) for agent in agents) / agent_count

    queue_length_by_kind = {"water_station": 0, "meal_distribution": 0}
    active_count_by_kind = {"water_station": 0, "meal_distribution": 0}
    for facility in world.facilities.values():
        if facility.kind in queue_length_by_kind:
            queue_length_by_kind[facility.kind] += len(facility.resource_queue_agent_ids)
            active_count_by_kind[facility.kind] += len(facility.resource_active_agent_ids)

    values: dict[str, Any] = {
        "minute_index": world.minute_index,
        "toilet_urgency_mean": mean_need("toilet_urgency"),
        "hunger_need_mean": mean_need("hunger"),
        "hydration_need_mean": mean_need("hydration_need"),
        "water_queue_length": queue_length_by_kind["water_station"],
        "meal_queue_length": queue_length_by_kind["meal_distribution"],
        "water_active_service_count": active_count_by_kind["water_station"],
        "meal_active_service_count": active_count_by_kind["meal_distribution"],
        "water_ml_remaining": world.resources["water_ml"],
        "food_meal_units_remaining": world.resources["food_meal_units"],
        "water_ml_reserved": world.model_state["resource_ledger_v1"]["accounts"]["water_ml"][
            "reserved"
        ],
        "food_meal_units_reserved": world.model_state["resource_ledger_v1"]["accounts"][
            "food_meal_units"
        ]["reserved"],
        "stress_mean": snapshot.stress_mean,
        "staff_busy_count": snapshot.staff_busy_count,
        "staff_load_ratio": snapshot.staff_load_ratio,
        "unresolved_support_request_count": snapshot.unresolved_support_request_count,
        "support_no_qualified_worker_count": snapshot.support_no_qualified_worker_count,
        (
            "support_no_qualified_worker_request_minutes"
        ): snapshot.support_no_qualified_worker_request_minutes,
        (
            "support_fairness_promoted_assignment_count"
        ): snapshot.support_fairness_promoted_assignment_count,
        "support_wait_staff_busy_request_minutes": snapshot.support_wait_staff_busy_request_minutes,
        (
            "support_wait_staff_off_shift_request_minutes"
        ): snapshot.support_wait_staff_off_shift_request_minutes,
    }
    for prefix in ("water_service", "meal_service"):
        count = world.metric_state.get(f"{prefix}_wait_count", 0.0)
        total = world.metric_state.get(f"{prefix}_wait_total_minutes", 0.0)
        values[f"{prefix}_wait_count"] = count
        values[f"{prefix}_wait_total_minutes"] = total
        values[f"{prefix}_wait_mean_minutes"] = total / count if count else 0.0
        values[f"{prefix}_wait_max_minutes"] = world.metric_state.get(
            f"{prefix}_wait_max_minutes", 0.0
        )
    values["water_outage_stall_count"] = world.metric_state.get(
        "resource_service_wait_water_outage_count", 0.0
    )
    values["resource_stock_stall_count"] = world.metric_state.get(
        "resource_service_wait_stock_count", 0.0
    )
    return values
