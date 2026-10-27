"""Optional synthetic context, separate from immutable PopulationSpec v1 encodings."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Identifier
from sheltersim.contracts.operations import SupportKind


class HouseholdRelationshipCorrection(ExtensionModel):
    """A reviewed, ID-targeted edit to one generated synthetic person."""

    agent_id: Identifier
    household_id: Identifier
    parent_ids: tuple[Identifier, ...] | None = Field(default=None, max_length=4)
    caregiver_ids: tuple[Identifier, ...] | None = Field(default=None, max_length=16)
    guardian_status: Literal["accompanied", "unaccompanied", "not_applicable"] | None = None

    def has_change(self) -> bool:
        return any(
            value is not None
            for value in (self.parent_ids, self.caregiver_ids, self.guardian_status)
        )


class HouseholdCorrectionRequest(ExtensionModel):
    schema_version: Literal[1] = 1
    expected_scenario_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    corrections: tuple[HouseholdRelationshipCorrection, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_targets_and_explicit_changes(self) -> HouseholdCorrectionRequest:
        ids = [row.agent_id for row in self.corrections]
        if len(ids) != len(set(ids)):
            raise ValueError("each synthetic person may be corrected once per preview")
        if any(not row.has_change() for row in self.corrections):
            raise ValueError(
                "each household correction must change at least one relationship field"
            )
        return self


class PersonContext(ExtensionModel):
    household_id: Identifier | None = None
    parent_ids: tuple[Identifier, ...] = Field(default=(), max_length=4)
    caregiver_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    care_for_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    partner_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    relative_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)
    requires_review: bool = False
    review_reasons: tuple[str, ...] = Field(default=(), max_length=32)
    roles: tuple[Literal["parent", "child", "caregiver", "dependent", "partner", "alone"], ...] = ()
    medical_support: bool | None = None
    guardian_status: Literal["accompanied", "unaccompanied", "not_applicable"] | None = None
    daily_rhythm_id: Identifier | None = None
    medication_support: bool | None = None
    care_support_needs: tuple[SupportKind, ...] = Field(default=(), max_length=8)
    pet_count: int | None = Field(default=None, strict=True, ge=0, le=20)
    information_access: tuple[Identifier, ...] | None = None
    usual_wake_minute: int | None = Field(default=None, strict=True, ge=0, lt=1440)
    usual_sleep_minute: int | None = Field(default=None, strict=True, ge=0, lt=1440)
    required_sleep_minutes: int | None = Field(default=None, strict=True, ge=0, le=1440)
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def unique_care_support_needs(self) -> PersonContext:
        if len(self.care_support_needs) != len(set(self.care_support_needs)):
            raise ValueError("care_support_needs cannot contain duplicates")
        return self


class HouseholdContext(ExtensionModel):
    member_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    source_ids: tuple[Identifier, ...] = ()


class DynamicArrivalPersonContext(ExtensionModel):
    """Optional per-member support request context for a typed arrival batch."""

    member_index: int = Field(strict=True, ge=0, le=999)
    care_support_needs: tuple[SupportKind, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def unique_nonempty_support_needs(self) -> DynamicArrivalPersonContext:
        if len(self.care_support_needs) != len(set(self.care_support_needs)):
            raise ValueError("dynamic arrival care support kinds cannot contain duplicates")
        if not self.care_support_needs:
            raise ValueError("dynamic arrival context override requires a care support kind")
        return self


class PopulationContext(ExtensionModel):
    schema_version: Literal[1] = 1
    people: dict[Identifier, PersonContext]
    households: dict[Identifier, HouseholdContext] = Field(default_factory=dict)
    resident_distribution_source_ids: tuple[Identifier, ...] = ()
    evacuation_selection_source_ids: tuple[Identifier, ...] = ()


def population_context_people(world: Any) -> dict[str, dict[str, Any]]:
    """Merge immutable Scenario context with persisted context for dynamic arrivals."""
    configured = world.model_configs.get("population_context", {})
    people = configured.get("people", {}) if isinstance(configured, dict) else {}
    merged = dict(people) if isinstance(people, dict) else {}
    saved_context = world.model_state.get("population_context_v1", {})
    saved_people = saved_context.get("people", {}) if isinstance(saved_context, dict) else {}
    if isinstance(saved_people, dict):
        merged.update(saved_people)
    population_state = world.model_state.get("population_dynamics_v1", {})
    dynamic_people = (
        population_state.get("profile_context", {}) if isinstance(population_state, dict) else {}
    )
    if isinstance(dynamic_people, dict):
        merged.update(dynamic_people)
    return merged


def validate_population_context(scenario: Any) -> None:
    raw = scenario.model_configs.get("population_context")
    if raw is None:
        return
    context = PopulationContext.model_validate(raw)
    profiles = {profile.id: profile for profile in scenario.profiles}
    if not set(context.people) <= set(profiles):
        raise ValueError("person context references an unknown initial profile")
    claimed_sources = set(context.resident_distribution_source_ids) | set(
        context.evacuation_selection_source_ids
    )
    for identity, person in context.people.items():
        relatives = [
            *person.parent_ids,
            *person.caregiver_ids,
            *person.care_for_ids,
            *person.partner_ids,
            *person.relative_ids,
        ]
        if identity in relatives or not set(relatives) <= set(profiles):
            raise ValueError("synthetic relationship contains a self/unknown reference")
        if person.household_id is not None and person.household_id != profiles[identity].group_id:
            raise ValueError("initial household context must agree with Profile.group_id")
        facts = person.model_dump(
            exclude_defaults=True, exclude_none=True, exclude={"household_id", "source_ids"}
        )
        if facts and not person.source_ids:
            raise ValueError("synthetic personal-context assumptions require source IDs")
        claimed_sources.update(person.source_ids)
    children: dict[str, list[str]] = {key: [] for key in profiles}
    degrees = {key: 0 for key in profiles}
    for identity, person in context.people.items():
        for parent in set(person.parent_ids):
            children[parent].append(identity)
            degrees[identity] += 1
    ready = [key for key, degree in degrees.items() if degree == 0]
    visited = 0
    while ready:
        parent = ready.pop()
        visited += 1
        for child in children[parent]:
            degrees[child] -= 1
            if degrees[child] == 0:
                ready.append(child)
    if visited != len(profiles):
        raise ValueError("synthetic parent relationships cannot contain cycles")
    seen_members: set[str] = set()
    for identity, household in context.households.items():
        members = set(household.member_ids)
        if (
            len(members) != len(household.member_ids)
            or seen_members.intersection(members)
            or not members <= set(profiles)
            or any(profiles[key].group_id != identity for key in members)
        ):
            raise ValueError("household membership conflicts with immutable initial profiles")
        seen_members.update(members)
        claimed_sources.update(household.source_ids)
    sources = {source["id"] for source in scenario.provenance.get("sources", [])}
    if not claimed_sources <= sources:
        raise ValueError("population context provenance references an unknown source")


def correct_synthetic_household_relationships(
    scenario: Any, request: HouseholdCorrectionRequest
) -> Any:
    """Return a new ScenarioSpec for an approved-preview workflow; never edit a saved Run.

    The generated profile IDs and immutable Profile.group_id assignments are exact
    selectors. Corrections cannot move a person to another household or introduce
    human/real identities. Scenario validation remains the final graph/cycle check.
    """
    from copy import deepcopy

    from sheltersim.experiments.scenario import ScenarioSpec

    scenario = ScenarioSpec.model_validate(
        scenario.model_dump(mode="json") if hasattr(scenario, "model_dump") else scenario
    )
    if (
        "population_context" not in scenario.model_configs
        or "population" not in scenario.model_configs
    ):
        raise ValueError(
            "household_context_unavailable: generate a new synthetic population preview"
        )
    context = PopulationContext.model_validate(scenario.model_configs["population_context"])
    population = scenario.model_configs["population"]
    people = deepcopy({key: value.model_dump(mode="json") for key, value in context.people.items()})
    profiles = {profile.id: profile for profile in scenario.profiles}
    traits = population.get("people", {}) if isinstance(population, dict) else {}
    sources = list(deepcopy(scenario.provenance).get("sources", []))
    correction_source = "OPERATOR-HOUSEHOLD-REVIEW-V1"
    if not any(row.get("id") == correction_source for row in sources):
        sources.append(
            {
                "id": correction_source,
                "title": "Operator-reviewed synthetic household relationships",
                "source_kind": "operator",
                "description": "Relationship links reviewed in a scenario preview; no real-person identity is accepted.",
            }
        )
    changed_ids: set[str] = set()
    for correction in request.corrections:
        profile = profiles.get(correction.agent_id)
        person = people.get(correction.agent_id)
        trait = traits.get(correction.agent_id) if isinstance(traits, dict) else None
        if profile is None or person is None or not isinstance(trait, dict):
            raise ValueError(f"unknown_synthetic_agent_id: {correction.agent_id}")
        if profile.provenance.source_kind not in {"synthetic", "assumption"}:
            raise ValueError("household correction is limited to synthetic or assumption profiles")
        if (
            profile.group_id is None
            or correction.household_id != profile.group_id
            or person.get("household_id") != profile.group_id
        ):
            raise ValueError(
                "household correction cannot move a person across immutable household IDs"
            )
        age = trait.get("age_years")
        if type(age) is not int:
            raise ValueError("synthetic_age_unavailable: regenerate a population with age traits")
        proposed_parents = (
            list(correction.parent_ids)
            if correction.parent_ids is not None
            else list(person.get("parent_ids", []))
        )
        proposed_caregivers = (
            list(correction.caregiver_ids)
            if correction.caregiver_ids is not None
            else list(person.get("caregiver_ids", []))
        )
        prior_caregivers = list(person.get("caregiver_ids", []))
        if len(proposed_parents) != len(set(proposed_parents)) or len(proposed_caregivers) != len(
            set(proposed_caregivers)
        ):
            raise ValueError("household relationship IDs must be unique")
        for related_id in (*proposed_parents, *proposed_caregivers):
            related_profile = profiles.get(related_id)
            related_trait = traits.get(related_id) if isinstance(traits, dict) else None
            if (
                related_profile is None
                or related_id not in people
                or related_trait is None
                or related_profile.group_id != profile.group_id
            ):
                raise ValueError(
                    "parent and caregiver links must target generated members of the same household"
                )
        for parent_id in proposed_parents:
            parent_age = traits[parent_id].get("age_years")
            if type(parent_age) is not int or parent_age <= age:
                raise ValueError("a synthetic parent must be older than the generated child")
        guardian_status = correction.guardian_status or person.get("guardian_status")
        if age < 18:
            if correction.guardian_status == "not_applicable":
                raise ValueError("a minor must be accompanied or explicitly unaccompanied")
            if correction.guardian_status is None and (proposed_parents or proposed_caregivers):
                guardian_status = "accompanied"
            if guardian_status == "accompanied" and not (proposed_parents or proposed_caregivers):
                raise ValueError("accompanied_minor_requires_a_household_guardian_link")
            if guardian_status == "unaccompanied" and (proposed_parents or proposed_caregivers):
                raise ValueError("unaccompanied_minor_cannot_keep_parent_or_caregiver_links")
        elif guardian_status not in (None, "not_applicable"):
            raise ValueError("adult_profile_cannot_have_minor_guardian_status")
        person["parent_ids"] = proposed_parents
        person["caregiver_ids"] = proposed_caregivers
        if guardian_status is not None:
            person["guardian_status"] = guardian_status
        person["source_ids"] = sorted(set(person.get("source_ids", [])) | {correction_source})
        changed_ids.add(correction.agent_id)
        for caregiver_id in set(prior_caregivers) | set(proposed_caregivers):
            caregiver_context = people[caregiver_id]
            care_for = set(caregiver_context.get("care_for_ids", []))
            care_for.discard(correction.agent_id)
            if caregiver_id in proposed_caregivers:
                care_for.add(correction.agent_id)
            caregiver_context["care_for_ids"] = sorted(care_for)
            caregiver_context["source_ids"] = sorted(
                set(caregiver_context.get("source_ids", [])) | {correction_source}
            )
            changed_ids.add(caregiver_id)
        for parent_id in proposed_parents:
            parent_context = people[parent_id]
            parent_context["source_ids"] = sorted(
                set(parent_context.get("source_ids", [])) | {correction_source}
            )
            changed_ids.add(parent_id)
    # Roles and the old region preview are derived from the corrected typed graph so
    # the approved artifact has one consistent account of the reviewed assignment.
    parents = {
        parent_id for person in people.values() for parent_id in person.get("parent_ids", [])
    }
    for identity, person in people.items():
        roles = set(person.get("roles", [])) - {"parent", "child", "caregiver", "dependent"}
        if person.get("parent_ids") or person.get("caregiver_ids"):
            roles.add("child" if person.get("parent_ids") else "dependent")
        if identity in parents:
            roles.add("parent")
        if person.get("care_for_ids"):
            roles.add("caregiver")
        person["roles"] = sorted(roles)
        age = traits.get(identity, {}).get("age_years")
        if type(age) is int and age < 18:
            person["guardian_status"] = (
                "accompanied"
                if person.get("parent_ids") or person.get("caregiver_ids")
                else "unaccompanied"
            )
        if identity in changed_ids:
            person["source_ids"] = sorted(set(person.get("source_ids", [])) | {correction_source})
    updated_context = context.model_copy(
        update={
            "people": {key: PersonContext.model_validate(value) for key, value in people.items()}
        }
    )
    configs = deepcopy(scenario.model_configs)
    configs["population_context"] = updated_context.model_dump(mode="json")
    provenance = deepcopy(scenario.provenance)
    provenance["sources"] = sorted(sources, key=lambda row: row["id"])
    provenance["household_assignment_corrections"] = {
        "schema_version": 1,
        "source_id": correction_source,
        "reviewed_agent_ids": [row.agent_id for row in request.corrections],
        "household_ids": sorted({row.household_id for row in request.corrections}),
        "seed": scenario.seed,
    }
    population_preview = provenance.get("population_generation", {})
    requirement_rows = population_preview.get("person_requirements_preview", [])
    for requirement in requirement_rows:
        identity = requirement.get("person_id")
        person = people.get(identity)
        if person is None:
            continue
        age = traits.get(identity, {}).get("age_years")
        has_guardian = bool(person.get("parent_ids") or person.get("caregiver_ids"))
        if type(age) is int:
            requirement["guardian_status"] = (
                "not_applicable"
                if age >= 18
                else "accompanied"
                if has_guardian
                else "unaccompanied"
            )
        requirement["caregiver_ids"] = list(person.get("caregiver_ids", []))
        requirement["dependent_ids"] = sorted(
            key for key, related in people.items() if identity in related.get("care_for_ids", [])
        )
        reasons = set(requirement.get("review_reasons", []))
        if has_guardian:
            reasons.discard("unaccompanied_minor")
            reasons.discard("support_need_without_explicit_household_caregiver")
        requirement["review_reasons"] = sorted(reasons)
        requirement["requires_review"] = bool(reasons)
    population_preview["unaccompanied_minor_ids"] = sorted(
        identity
        for identity, person in people.items()
        if type(traits.get(identity, {}).get("age_years")) is int
        and traits[identity]["age_years"] < 18
        and not person.get("parent_ids")
        and not person.get("caregiver_ids")
    )
    provenance["population_generation"] = population_preview
    return ScenarioSpec.model_validate(
        scenario.model_copy(
            update={
                "model_configs": configs,
                "provenance": provenance,
            }
        ).model_dump(mode="json")
    )
