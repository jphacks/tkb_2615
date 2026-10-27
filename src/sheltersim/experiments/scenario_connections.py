"""Translate published region previews into the additive runtime contracts.

This adapter does not generate traits or implement movement/body equations. It is used
only for newly generated previews; stored approved Scenarios are never upgraded in place.
"""

from __future__ import annotations

from typing import Any

from sheltersim.contracts.extensions import Point2D
from sheltersim.contracts.population import HouseholdContext, PersonContext, PopulationContext
from sheltersim.contracts.spatial import (
    EquipmentFootprint,
    Opening,
    SpatialConstraints,
    SpatialObstacle,
    Wall,
)


def connect_generated_context(
    population: Any, editable: Any, geometry: Any
) -> dict[str, dict[str, Any]]:
    sources = tuple(population.population.source_ids)
    households: dict[str, list[str]] = {}
    for profile in population.profile_set.profiles:
        households.setdefault(profile.group_id, []).append(profile.id)
    parents_by_child: dict[str, set[str]] = {}
    partners: dict[str, set[str]] = {}
    relatives: dict[str, set[str]] = {}
    for relation in population.household_relationships:
        if relation.relationship in {"parent_child", "caregiver_child"}:
            parents_by_child.setdefault(relation.dependent_id, set()).add(relation.caregiver_id)
        if relation.relationship in {"partner", "relative"}:
            target = partners if relation.relationship == "partner" else relatives
            target.setdefault(relation.caregiver_id, set()).add(relation.dependent_id)
            target.setdefault(relation.dependent_id, set()).add(relation.caregiver_id)
    parent_ids = {parent_id for values in parents_by_child.values() for parent_id in values}
    people = {}
    for record in population.person_requirements:
        roles: set[str] = set()
        if record.household_role != "adult":
            roles.add(record.household_role)
        if record.person_id in parent_ids:
            roles.add("parent")
        if record.dependent_ids:
            roles.add("caregiver")
        if record.household_role == "dependent":
            roles.add("dependent")
        people[record.person_id] = PersonContext(
            household_id=record.household_id,
            roles=tuple(sorted(roles)),
            parent_ids=tuple(sorted(parents_by_child.get(record.person_id, ()))),
            caregiver_ids=record.caregiver_ids,
            care_for_ids=record.dependent_ids,
            partner_ids=tuple(sorted(partners.get(record.person_id, ()))),
            relative_ids=tuple(sorted(relatives.get(record.person_id, ()))),
            requires_review=record.requires_review,
            review_reasons=record.review_reasons,
            medical_support=record.medical_support_required,
            medication_support=record.medication_support_required,
            care_support_needs=record.care_support_needs
            or tuple(
                kind
                for enabled, kind in (
                    (record.medication_support_required, "medication_support"),
                    (record.medical_support_required, "medical_assessment"),
                )
                if enabled is True
            ),
            guardian_status=record.guardian_status,
            pet_count=record.pet_count,
            information_access=(record.information_access,)
            if record.information_access is not None
            else None,
            daily_rhythm_id=record.daily_rhythm_id,
            usual_wake_minute=record.wake_minute,
            usual_sleep_minute=record.sleep_start_minute,
            source_ids=sources,
        )
    context = PopulationContext(
        people=people,
        households={
            key: HouseholdContext(member_ids=tuple(ids), source_ids=sources)
            for key, ids in households.items()
        },
        resident_distribution_source_ids=tuple(
            sorted({source for band in population.age_bands for source in band.source_ids})
        ),
        evacuation_selection_source_ids=population.evacuation_selection.source_ids,
    )
    constraints = SpatialConstraints()
    if editable is not None:
        # Region route IDs are canonical zone-edge IDs. No inferred door crossings.
        route_ids = {route.edge_id for route in geometry.routes}
        openings = []
        for record in editable.openings:
            edges = tuple(
                use.route_id
                for use in editable.route_openings
                if use.opening_id == record.opening_id and use.route_id in route_ids
            )
            openings.append(
                Opening(
                    id=record.opening_id,
                    wall_id=record.wall_id,
                    center=Point2D(
                        x=(record.start.x + record.end.x) / 2, y=(record.start.y + record.end.y) / 2
                    ),
                    width_m=record.width_m,
                    edge_ids=edges,
                    accessible=bool(edges)
                    and all(
                        route.accessible for route in geometry.routes if route.edge_id in edges
                    ),
                    source_ids=record.source_ids,
                )
            )
        constraints = SpatialConstraints(
            walls=tuple(
                Wall(id=wall.wall_id, start=wall.start, end=wall.end, source_ids=wall.source_ids)
                for wall in editable.walls
            ),
            openings=tuple(openings),
            obstacles=tuple(
                SpatialObstacle(
                    id=item.obstacle_id, polygon=item.polygon, source_ids=item.source_ids
                )
                for item in editable.obstacles
            ),
            equipment_footprints=tuple(
                EquipmentFootprint(
                    facility_id=item.facility_id,
                    polygon=item.polygon,
                    area_m2=item.area_m2,
                    source_ids=item.source_ids,
                )
                for item in editable.equipment_footprints
            ),
        )
    from sheltersim.region.facility import validate_facility_walkability

    validate_facility_walkability(geometry, constraints)
    return {
        "population_context": context.model_dump(mode="json"),
        "spatial_constraints": constraints.model_dump(mode="json"),
    }
