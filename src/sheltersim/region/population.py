"""Deterministic synthetic household and demographic generation."""

from __future__ import annotations

import math
import re
from typing import Iterable, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import PersonTraits, PopulationSpec
from sheltersim.core.rng import CounterRNG
from sheltersim.domain.models import Mobility
from sheltersim.experiments.profiles import (
    GeneratedProfileSet,
    ProfileTemplate,
    SyntheticProfileGenerator,
)
from sheltersim.experiments.scenario import ProfileSpec
from sheltersim.region.models import RegionModel, StatisticalObservation

SYNTHETIC_AGE_ASSUMPTION_ID = "ASM-SYNTHETIC-AGE-BANDS-V1"
SYNTHETIC_HOUSEHOLD_ASSUMPTION_ID = "ASM-SYNTHETIC-HOUSEHOLD-SIZES-V1"
SYNTHETIC_LANGUAGE_ASSUMPTION_ID = "ASM-SYNTHETIC-LANGUAGE-V1"
SYNTHETIC_TRAIT_ASSUMPTION_ID = "ASM-SYNTHETIC-PERSON-TRAITS-V1"
SYNTHETIC_EVACUATION_SELECTION_ID = "ASM-EVACUATION-SELECTION-UNIFORM-V1"
SYNTHETIC_HOUSEHOLD_CONSTRUCTION_ID = "ASM-HOUSEHOLD-CARE-RELATIONSHIPS-V1"
OPERATOR_POPULATION_SOURCE_ID = "SRC-OPERATOR-POPULATION-CONDITIONS"
SYNTHETIC_HOUSEHOLD_TEMPLATE_ID = "ASM-SYNTHETIC-HOUSEHOLD-TEMPLATES-V1"


class AgeBand(RegionModel):
    """Observed count for an age band. Counts must retain their source IDs."""

    label: str = Field(min_length=1, max_length=100)
    age_min: int = Field(ge=0, le=120)
    age_max: int = Field(ge=0, le=120)
    population_count: int = Field(ge=0)
    source_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_band(self) -> AgeBand:
        if self.age_min > self.age_max:
            raise ValueError("age_min cannot exceed age_max")
        return self


class HouseholdSizeMix(RegionModel):
    """Explicit household-size weights sourced from an observed regional table."""

    size_weights: dict[int, float]
    source_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_weights(self) -> HouseholdSizeMix:
        if (
            not self.size_weights
            or any(
                isinstance(size, bool)
                or not 1 <= size <= 10
                or not math.isfinite(weight)
                or weight < 0
                for size, weight in self.size_weights.items()
            )
            or not math.isfinite(sum(self.size_weights.values()))
            or sum(self.size_weights.values()) <= 0
        ):
            raise ValueError("household size weights must include positive weights for sizes 1..10")
        return self


class EvacuationSelection(RegionModel):
    """Selection multipliers applied after the resident age distribution is assembled."""

    age_band_multipliers: dict[str, float] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_multipliers(self) -> EvacuationSelection:
        if any(
            not label or not math.isfinite(multiplier) or not 0 <= multiplier <= 10
            for label, multiplier in self.age_band_multipliers.items()
        ):
            raise ValueError("evacuation selection multipliers must be finite values from 0 to 10")
        return self


class DailyRhythmTemplate(RegionModel):
    rhythm_id: str
    sleep_start_minute: int = Field(ge=0, le=1439)
    wake_minute: int = Field(ge=0, le=1439)
    weight: float = Field(gt=0, allow_inf_nan=False)
    source_ids: tuple[str, ...] = ()


class HouseholdMemberPattern(RegionModel):
    """One anonymous slot in an operator supplied household template."""

    role: Literal["unspecified", "adult", "child", "caregiver", "dependent"] = "unspecified"
    age_band_label: str | None = Field(default=None, min_length=1, max_length=100)


class HouseholdRelationshipPattern(RegionModel):
    """An explicit link between member slots; age alone never creates a family link."""

    caregiver_member_index: int = Field(ge=0, le=9)
    dependent_member_index: int = Field(ge=0, le=9)
    relationship: Literal[
        "parent_child", "caregiver_child", "caregiver_support", "partner", "relative"
    ]
    source_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class HouseholdTemplate(RegionModel):
    """Weighted, typed household shape selected only for a matching household size."""

    template_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    members: tuple[HouseholdMemberPattern, ...] = Field(min_length=1, max_length=10)
    relationships: tuple[HouseholdRelationshipPattern, ...] = Field(default=(), max_length=20)
    source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_relationship_patterns(self) -> HouseholdTemplate:
        if len(self.source_ids) != len(set(self.source_ids)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", source_id)
            for source_id in self.source_ids
        ):
            raise ValueError("household template source IDs must be unique stable identifiers")
        pairs: set[tuple[int, int, str]] = set()
        parent_edges: dict[int, set[int]] = {}
        for item in self.relationships:
            caregiver = item.caregiver_member_index
            dependent = item.dependent_member_index
            if (
                caregiver >= len(self.members)
                or dependent >= len(self.members)
                or caregiver == dependent
            ):
                raise ValueError(
                    "household relationship member indexes must be distinct template slots"
                )
            caregiver_role = self.members[caregiver].role
            dependent_role = self.members[dependent].role
            if (
                item.relationship in {"parent_child", "caregiver_child", "caregiver_support"}
                and caregiver_role == "child"
            ):
                raise ValueError("a declared caregiver slot cannot have the child role")
            if item.relationship in {"parent_child", "caregiver_child"} and dependent_role in {
                "adult",
                "caregiver",
            }:
                raise ValueError(
                    "a parent/caregiver-child dependent slot cannot require an adult role"
                )
            if item.relationship == "partner" and (
                caregiver_role == "child" or dependent_role == "child"
            ):
                raise ValueError("operator-declared partner slots cannot require the child role")
            pair = (caregiver, dependent, item.relationship)
            if pair in pairs:
                raise ValueError("household relationship template contains a duplicate link")
            pairs.add(pair)
            if item.relationship in {"parent_child", "caregiver_child"}:
                parent_edges.setdefault(caregiver, set()).add(dependent)
        indegree = [0] * len(self.members)
        for targets in parent_edges.values():
            for target in targets:
                indegree[target] += 1
        ready = [index for index, degree in enumerate(indegree) if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for target in parent_edges.get(current, ()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(self.members):
            raise ValueError("parent/caregiver-child template relationships cannot contain cycles")
        return self


class HouseholdCompositionInput(RegionModel):
    """Optional operator templates layered on the sourced household-size distribution."""

    templates: tuple[HouseholdTemplate, ...] = Field(min_length=1, max_length=64)
    household_size_weights: dict[int, float] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    source_kind: Literal["operator", "assumption"] = "operator"

    @model_validator(mode="after")
    def unique_templates(self) -> HouseholdCompositionInput:
        if len({item.template_id for item in self.templates}) != len(self.templates):
            raise ValueError("household composition template IDs must be unique")
        if self.source_kind == "assumption" and not self.source_ids:
            raise ValueError("assumed household composition templates must cite an assumption ID")
        if len(self.source_ids) != len(set(self.source_ids)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", source_id)
            for source_id in self.source_ids
        ):
            raise ValueError("household composition source IDs must be unique stable identifiers")
        if self.household_size_weights and (
            any(
                isinstance(size, bool)
                or not 1 <= size <= 10
                or not math.isfinite(weight)
                or weight < 0
                for size, weight in self.household_size_weights.items()
            )
            or not math.isfinite(sum(self.household_size_weights.values()))
            or sum(self.household_size_weights.values()) <= 0
        ):
            raise ValueError(
                "operator household-size weights must have a positive finite total for sizes 1..10"
            )
        return self


def _default_household_templates() -> tuple[HouseholdTemplate, ...]:
    def template(template_id: str, adults: int, children: int, weight: float) -> HouseholdTemplate:
        # One adult slot is linked to each child; no other relationship is declared.
        return HouseholdTemplate(
            template_id=template_id,
            weight=weight,
            members=(
                *(HouseholdMemberPattern(role="adult") for _ in range(adults)),
                *(HouseholdMemberPattern(role="child") for _ in range(children)),
            ),
            relationships=tuple(
                HouseholdRelationshipPattern(
                    caregiver_member_index=0,
                    dependent_member_index=adults + child,
                    relationship="parent_child",
                )
                for child in range(children)
            ),
        )

    templates = [
        template("adult-1", 1, 0, 1.0),
        template("adults-2", 2, 0, 0.9),
        template("adult-1-children-1", 1, 1, 0.1),
        template("adults-3", 3, 0, 0.6),
        template("adults-2-children-1", 2, 1, 0.4),
        template("adults-4", 4, 0, 0.4),
        template("adults-3-children-1", 3, 1, 0.2),
        template("adults-2-children-2", 2, 2, 0.4),
    ]
    for size in range(5, 11):
        templates.extend(
            (
                template(f"adults-{size}", size, 0, 0.3),
                template(f"adults-3-children-{size - 3}", 3, size - 3, 0.4),
                template(f"adults-2-children-{size - 2}", 2, size - 2, 0.3),
            )
        )
    return tuple(templates)


DEFAULT_SYNTHETIC_HOUSEHOLD_COMPOSITION = HouseholdCompositionInput(
    templates=_default_household_templates(),
    source_ids=(SYNTHETIC_HOUSEHOLD_TEMPLATE_ID,),
    source_kind="assumption",
)


class PopulationConditionInput(RegionModel):
    """Optional operator-entered rates; omitted requirements remain explicitly unknown."""

    medical_support_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    medication_support_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    care_support_need_rates: dict[
        Literal[
            "caregiver_assistance",
            "privacy_accommodation",
            "mobility_assistance",
            "hygiene_support",
            "emotional_support",
            "language_access",
            "dependent_assistance",
        ],
        float,
    ] = Field(default_factory=dict, max_length=7)
    pet_household_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    pet_count_weights: dict[int, float] = Field(default_factory=dict)
    information_access_weights: dict[str, float] = Field(default_factory=dict)
    language_weights: dict[str, float] = Field(default_factory=dict)
    additional_access_need_rates: dict[str, float] = Field(default_factory=dict)
    daily_rhythms: tuple[DailyRhythmTemplate, ...] = Field(default=(), max_length=24)
    household_composition: HouseholdCompositionInput | None = None
    source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_distributions(self) -> PopulationConditionInput:
        for distribution_name, distribution in (
            ("information access", self.information_access_weights),
            ("language", self.language_weights),
        ):
            if distribution and (
                any(
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", key)
                    or not math.isfinite(weight)
                    or weight < 0
                    for key, weight in distribution.items()
                )
                or not math.isfinite(sum(distribution.values()))
                or sum(distribution.values()) <= 0
            ):
                raise ValueError(
                    f"{distribution_name} weights must contain a positive finite total"
                )
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", key)
            or not math.isfinite(rate)
            or not 0 <= rate <= 1
            for key, rate in self.additional_access_need_rates.items()
        ):
            raise ValueError(
                "additional access-need rates must be finite probabilities from 0 to 1"
            )
        if any(
            not math.isfinite(rate) or not 0 <= rate <= 1
            for rate in self.care_support_need_rates.values()
        ):
            raise ValueError("care support-need rates must be finite probabilities from 0 to 1")
        if self.pet_household_rate is not None and self.pet_count_weights:
            raise ValueError(
                "pet household rate and pet count distribution cannot both be supplied"
            )
        if self.pet_count_weights and (
            any(
                isinstance(count, bool)
                or not 0 <= count <= 10
                or not math.isfinite(weight)
                or weight < 0
                for count, weight in self.pet_count_weights.items()
            )
            or not math.isfinite(sum(self.pet_count_weights.values()))
            or sum(self.pet_count_weights.values()) <= 0
        ):
            raise ValueError(
                "pet count distribution must use counts 0..10 and positive finite weights"
            )
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", item.rhythm_id)
            for item in self.daily_rhythms
        ):
            raise ValueError("daily rhythm IDs must be stable identifiers")
        if len({item.rhythm_id for item in self.daily_rhythms}) != len(self.daily_rhythms):
            raise ValueError("daily rhythm IDs must be unique")
        return self


class HouseholdRelationship(RegionModel):
    household_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    caregiver_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    dependent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    relationship: Literal[
        "parent_child", "caregiver_child", "caregiver_support", "partner", "relative"
    ]
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class GeneratedPersonRequirements(RegionModel):
    """Region preview data awaiting the shared PopulationSpec v2 contract and consumer."""

    person_id: str
    household_id: str
    household_role: Literal["adult", "child", "dependent", "alone"]
    guardian_status: Literal["accompanied", "unaccompanied", "not_applicable"]
    caregiver_ids: tuple[str, ...] = ()
    dependent_ids: tuple[str, ...] = ()
    medical_support_required: bool | None = None
    medication_support_required: bool | None = None
    care_support_needs: tuple[
        Literal[
            "medical_assessment",
            "medication_support",
            "caregiver_assistance",
            "privacy_accommodation",
            "mobility_assistance",
            "hygiene_support",
            "emotional_support",
            "language_access",
            "dependent_assistance",
        ],
        ...,
    ] = ()
    information_access: str | None = None
    pet_count: int | None = Field(default=None, ge=0)
    daily_rhythm_id: str | None = None
    sleep_start_minute: int | None = Field(default=None, ge=0, le=1439)
    wake_minute: int | None = Field(default=None, ge=0, le=1439)
    requires_review: bool = False
    review_reasons: tuple[str, ...] = ()


class GeneratedPopulation(RegionModel):
    profile_set: GeneratedProfileSet
    population: PopulationSpec
    household_sizes: dict[str, int]
    age_bands: tuple[AgeBand, ...]
    resident_age_distribution: dict[str, float]
    evacuee_age_distribution: dict[str, float]
    household_mix: HouseholdSizeMix
    evacuation_selection: EvacuationSelection
    household_relationships: tuple[HouseholdRelationship, ...] = ()
    household_template_ids: dict[str, str] = Field(default_factory=dict)
    unselected_template_ids: tuple[str, ...] = ()
    household_composition: HouseholdCompositionInput | None = None
    person_requirements: tuple[GeneratedPersonRequirements, ...] = ()
    trait_input: PopulationConditionInput = Field(default_factory=PopulationConditionInput)
    generator: str = "synthetic-household-profile-v1"
    assumptions: tuple[dict[str, object], ...]
    sources: tuple[dict[str, object], ...]

    @model_validator(mode="after")
    def profile_household_and_trait_ids_match(self) -> GeneratedPopulation:
        profile_by_id = {profile.id: profile for profile in self.profile_set.profiles}
        if set(profile_by_id) != set(self.population.people):
            raise ValueError(
                "generated profiles and population traits must have identical person IDs"
            )
        household_counts: dict[str, int] = {}
        for profile_id, profile in profile_by_id.items():
            traits = self.population.people[profile_id]
            if not profile.group_id or profile.group_id != traits.household_id:
                raise ValueError("ProfileSpec.group_id must match PersonTraits.household_id")
            household_counts[profile.group_id] = household_counts.get(profile.group_id, 0) + 1
        if household_counts != self.household_sizes:
            raise ValueError("generated household sizes must match profile group IDs")
        if set(self.household_template_ids) - set(self.household_sizes):
            raise ValueError("household template assignments must reference generated households")
        templates = {
            item.template_id: item
            for item in (self.household_composition.templates if self.household_composition else ())
        }
        for household_id, template_id in self.household_template_ids.items():
            template = templates.get(template_id)
            if template is None or len(template.members) != self.household_sizes[household_id]:
                raise ValueError(
                    "household template assignment must match the generated member count"
                )
        if set(self.unselected_template_ids) & set(self.household_template_ids.values()):
            raise ValueError("unselected household templates cannot also be assigned")
        if set(self.unselected_template_ids) - set(templates):
            raise ValueError(
                "unselected template IDs must belong to the supplied composition input"
            )
        if self.person_requirements:
            requirement_ids = [item.person_id for item in self.person_requirements]
            if len(requirement_ids) != len(set(requirement_ids)) or set(requirement_ids) != set(
                profile_by_id
            ):
                raise ValueError(
                    "person requirement previews must contain one record per generated profile"
                )
            for item in self.person_requirements:
                if item.household_id != profile_by_id[item.person_id].group_id:
                    raise ValueError(
                        "person requirement household ID must match ProfileSpec.group_id"
                    )
        for relationship in self.household_relationships:
            caregiver = profile_by_id.get(relationship.caregiver_id)
            dependent = profile_by_id.get(relationship.dependent_id)
            if (
                caregiver is None
                or dependent is None
                or relationship.caregiver_id == relationship.dependent_id
                or caregiver.group_id != relationship.household_id
                or dependent.group_id != relationship.household_id
            ):
                raise ValueError(
                    "household relationship endpoints must be distinct members of the same "
                    "household"
                )
            if relationship.source_id not in self.population.source_ids:
                raise ValueError("household relationships must cite a population source ID")
            if relationship.relationship in {"parent_child", "caregiver_child"}:
                parent_age = self.population.people[relationship.caregiver_id].age_years
                child_age = self.population.people[relationship.dependent_id].age_years
                if child_age >= 18 or parent_age < 18 or parent_age - child_age < 16:
                    raise ValueError(
                        "synthetic parent/guardian links require an adult caregiver at least "
                        "16 years older than a minor"
                    )
            elif relationship.relationship == "caregiver_support":
                caregiver_age = self.population.people[relationship.caregiver_id].age_years
                if (
                    caregiver_age < 18
                    or not self.population.people[relationship.dependent_id].medical_support
                ):
                    raise ValueError(
                        "synthetic care-support links require an adult caregiver and a "
                        "person with a support need"
                    )
            elif relationship.relationship == "partner":
                if any(
                    self.population.people[person_id].age_years < 18
                    for person_id in (relationship.caregiver_id, relationship.dependent_id)
                ):
                    raise ValueError("operator-declared partner links require two adults")
        if self.person_requirements:
            relation_pairs = {
                (item.caregiver_id, item.dependent_id)
                for item in self.household_relationships
                if item.relationship in {"parent_child", "caregiver_child", "caregiver_support"}
            }
            requirements = {item.person_id: item for item in self.person_requirements}
            for person_id, item in requirements.items():
                expected_caregivers = {
                    caregiver for caregiver, dependent in relation_pairs if dependent == person_id
                }
                expected_dependents = {
                    dependent for caregiver, dependent in relation_pairs if caregiver == person_id
                }
                if (
                    set(item.caregiver_ids) != expected_caregivers
                    or set(item.dependent_ids) != expected_dependents
                ):
                    raise ValueError(
                        "person caregiver/dependent lists must match household relationship records"
                    )
                age = self.population.people[person_id].age_years
                has_guardian = any(
                    relation.relationship in {"parent_child", "caregiver_child"}
                    and relation.dependent_id == person_id
                    for relation in self.household_relationships
                )
                expected_guardian_status = (
                    "not_applicable"
                    if age >= 18
                    else "accompanied"
                    if has_guardian
                    else "unaccompanied"
                )
                if item.guardian_status != expected_guardian_status:
                    raise ValueError(
                        "minor guardian status must agree with explicit caregiver links"
                    )
                if item.requires_review != bool(item.review_reasons):
                    raise ValueError("person review flag must agree with explicit review reasons")
                if (
                    age < 18
                    and not has_guardian
                    and "unaccompanied_minor" not in item.review_reasons
                ):
                    raise ValueError("an unaccompanied minor must be flagged for operator review")
            pet_records: dict[str, int] = {}
            for item in self.person_requirements:
                if item.pet_count is not None:
                    pet_records[item.household_id] = pet_records.get(item.household_id, 0) + 1
            if any(count > 1 for count in pet_records.values()):
                raise ValueError("household pet counts must have one deterministic context owner")
        expected_bands = {item.label for item in self.age_bands}
        if set(self.resident_age_distribution) != expected_bands:
            raise ValueError("resident age distribution must map every observed age band")
        if set(self.evacuee_age_distribution) != expected_bands:
            raise ValueError("evacuee age distribution must map every resident age band")
        return self


class ResidentPopulationSampler:
    """Read resident age observations as a distinct distribution before selection."""

    @staticmethod
    def distribution(age_bands: tuple[AgeBand, ...]) -> dict[str, float]:
        return _resident_age_distribution(age_bands)

    @staticmethod
    def sample_selected_person(
        rng_value: float,
        rng: CounterRNG,
        person_id: str,
        age_bands: tuple[AgeBand, ...],
        selected_distribution: dict[str, float],
    ) -> int:
        return _draw_age_from_distribution(
            rng_value, rng, person_id, age_bands, selected_distribution
        )


class EvacuationSelector:
    """Apply explicit evacuation-selection assumptions to resident distributions."""

    @staticmethod
    def distribution(
        resident_distribution: dict[str, float], selection: EvacuationSelection
    ) -> dict[str, float]:
        return _evacuee_age_distribution(resident_distribution, selection)


class HouseholdGenerator:
    """Assign stable synthetic groups independently from resident age observations."""

    @staticmethod
    def group_indices(
        count: int, rng: CounterRNG, mix: HouseholdSizeMix
    ) -> tuple[dict[str, int], dict[int, str]]:
        household_sizes: dict[str, int] = {}
        household_by_index: dict[int, str] = {}
        index = 0
        household_number = 0
        while index < count:
            household_number += 1
            household_id = f"H{household_number:06d}"
            remaining = count - index
            size = min(
                remaining, _draw_weighted_size(rng.random("population", "household", index), mix)
            )
            household_sizes[household_id] = size
            for member_index in range(index, index + size):
                household_by_index[member_index] = household_id
            index += size
        return household_sizes, household_by_index

    @staticmethod
    def choose_templates(
        household_sizes: dict[str, int],
        rng: CounterRNG,
        composition: HouseholdCompositionInput | None,
    ) -> dict[str, HouseholdTemplate]:
        """Choose a typed composition only when its member count fits the generated group."""
        if composition is None:
            return {}
        by_size: dict[int, list[HouseholdTemplate]] = {}
        for template in composition.templates:
            by_size.setdefault(len(template.members), []).append(template)
        selected: dict[str, HouseholdTemplate] = {}
        for household_number, (household_id, size) in enumerate(sorted(household_sizes.items()), 1):
            candidates = sorted(by_size.get(size, ()), key=lambda item: item.template_id)
            if not candidates:
                continue
            weights = _normalize_weights({item.template_id: item.weight for item in candidates})
            template_id = _draw_weighted_key(
                rng.random("population", "household_template", str(household_number)), weights
            )
            selected[household_id] = next(
                item for item in candidates if item.template_id == template_id
            )
        return selected


class SyntheticPopulationGenerator:
    """Generate exact-count synthetic profiles while preserving household mappings."""

    def __init__(self, profile_generator: SyntheticProfileGenerator | None = None) -> None:
        self.profile_generator = profile_generator or SyntheticProfileGenerator()

    def generate(
        self,
        *,
        count: int,
        seed: int,
        zone_ids: Iterable[str],
        age_bands: Iterable[AgeBand] | None = None,
        household_mix: HouseholdSizeMix | None = None,
        languages: tuple[str, ...] = ("ja",),
        language_source_ids: tuple[str, ...] = (),
        evacuation_selection: EvacuationSelection | None = None,
        conditions: PopulationConditionInput | None = None,
        household_composition: HouseholdCompositionInput | None = None,
    ) -> GeneratedPopulation:
        conditions = conditions or PopulationConditionInput()
        household_composition = household_composition or conditions.household_composition
        zones = tuple(sorted(set(zone_ids)))
        if not zones:
            raise ValueError("synthetic population needs at least one starting zone")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 10_000:
            raise ValueError("synthetic population count must be between 1 and 10,000")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if not languages or len(languages) != len(set(languages)):
            raise ValueError("languages must be a non-empty tuple without duplicates")

        bands = tuple(age_bands or ())
        mix = household_mix
        if (
            mix is None
            and household_composition is not None
            and household_composition.household_size_weights
        ):
            mix = HouseholdSizeMix(
                size_weights=household_composition.household_size_weights,
                source_ids=household_composition.source_ids or (OPERATOR_POPULATION_SOURCE_ID,),
            )
        age_assumed = not bands
        household_assumed = mix is None
        if age_assumed:
            # Equal weights across broad bands are a transparent generic fallback, not census data.
            bands = (
                AgeBand(
                    label="child",
                    age_min=0,
                    age_max=17,
                    population_count=1,
                    source_ids=(SYNTHETIC_AGE_ASSUMPTION_ID,),
                ),
                AgeBand(
                    label="adult",
                    age_min=18,
                    age_max=64,
                    population_count=1,
                    source_ids=(SYNTHETIC_AGE_ASSUMPTION_ID,),
                ),
                AgeBand(
                    label="older_adult",
                    age_min=65,
                    age_max=90,
                    population_count=1,
                    source_ids=(SYNTHETIC_AGE_ASSUMPTION_ID,),
                ),
            )
        if not bands:
            raise ValueError("at least one age band is required")
        if sum(band.population_count for band in bands) <= 0:
            raise ValueError(
                "resident age bands must contain a positive observed or assumed population total"
            )
        ordered_bands = sorted(bands, key=lambda band: band.age_min)
        if any(
            current.age_min <= previous.age_max
            for previous, current in zip(ordered_bands, ordered_bands[1:])
        ):
            raise ValueError("age bands must not overlap")
        if household_assumed:
            # This default is only a generator heuristic and must not be read as a local statistic.
            mix = HouseholdSizeMix(
                size_weights={1: 0.20, 2: 0.25, 3: 0.25, 4: 0.20, 5: 0.10},
                source_ids=(SYNTHETIC_HOUSEHOLD_ASSUMPTION_ID,),
            )
        assert mix is not None

        selection = evacuation_selection or EvacuationSelection()
        unknown_selection_bands = set(selection.age_band_multipliers) - {
            band.label for band in bands
        }
        if unknown_selection_bands:
            raise ValueError(
                "evacuation selection references unknown resident age bands: "
                + ", ".join(sorted(unknown_selection_bands))
            )
        resident_age_distribution = ResidentPopulationSampler.distribution(bands)
        evacuee_age_distribution = EvacuationSelector.distribution(
            resident_age_distribution, selection
        )
        configured_languages = conditions.language_weights or {
            language: 1.0 for language in languages
        }
        if not conditions.language_weights and len(languages) == 1:
            configured_languages = {languages[0]: 1.0}
        language_ids = tuple(sorted(configured_languages))
        if not language_ids or any(not language for language in language_ids):
            raise ValueError("language choices must have non-empty identifiers")
        normalized_language_weights = _normalize_weights(configured_languages)
        information_choices = _normalize_weights(conditions.information_access_weights)
        rhythm_by_id = {item.rhythm_id: item for item in conditions.daily_rhythms}
        rhythm_choices = _normalize_weights(
            {item.rhythm_id: item.weight for item in conditions.daily_rhythms}
        )

        profile_set = self.profile_generator.generate(
            count=count,
            seed=seed,
            zone_ids=zones,
            template=ProfileTemplate(
                template_id="synthetic-regional-profile-v1",
                group_size=1,
                initial_zone_ids=zones,
            ),
        )
        if (
            household_composition is not None
            and household_composition.source_kind == "assumption"
            and _maximum_supported_age(
                bands=bands,
                selected_distribution=evacuee_age_distribution,
                minimum_age=0,
                maximum_age=17,
                age_band_label=None,
            )
            is None
        ):
            # Assumed templates must not make an adult-only selection fail.
            household_composition = household_composition.model_copy(
                update={
                    "templates": tuple(
                        template
                        for template in household_composition.templates
                        if all(member.role != "child" for member in template.members)
                    )
                }
            )
        rng = CounterRNG(seed)
        household_sizes, household_by_index = HouseholdGenerator.group_indices(count, rng, mix)
        household_templates = HouseholdGenerator.choose_templates(
            household_sizes, rng, household_composition
        )
        unknown_template_bands = {
            member.age_band_label
            for template in household_templates.values()
            for member in template.members
            if member.age_band_label and member.age_band_label not in {band.label for band in bands}
        }
        if unknown_template_bands:
            raise ValueError(
                "household template references unknown age bands: "
                + ", ".join(sorted(unknown_template_bands))
            )

        source_id_set = (
            {source for band in bands for source in band.source_ids}
            | set(mix.source_ids)
            | {SYNTHETIC_TRAIT_ASSUMPTION_ID}
        )
        language_assumed = not language_source_ids and not (
            conditions.language_weights and conditions.source_ids
        )
        language_source_records = language_source_ids or (
            conditions.source_ids
            if conditions.language_weights and conditions.source_ids
            else (SYNTHETIC_LANGUAGE_ASSUMPTION_ID,)
        )
        source_id_set.update(language_source_records)
        source_id_set.update(selection.source_ids)
        source_id_set.update(conditions.source_ids)
        source_id_set.update(
            source_id for rhythm in conditions.daily_rhythms for source_id in rhythm.source_ids
        )
        if household_composition is not None:
            source_id_set.update(household_composition.source_ids)
            source_id_set.update(
                source_id
                for template in household_composition.templates
                for source_id in template.source_ids
            )
            source_id_set.update(
                relationship.source_id
                for template in household_composition.templates
                for relationship in template.relationships
                if relationship.source_id is not None
            )
            if (
                household_composition.source_kind == "operator"
                and not household_composition.source_ids
            ):
                source_id_set.add(OPERATOR_POPULATION_SOURCE_ID)
        operator_composition = (
            household_composition is not None and household_composition.source_kind == "operator"
        )
        has_explicit_condition_input = bool(
            conditions.medical_support_rate is not None
            or conditions.medication_support_rate is not None
            or conditions.care_support_need_rates
            or conditions.pet_household_rate is not None
            or conditions.pet_count_weights
            or conditions.information_access_weights
            or conditions.language_weights
            or conditions.additional_access_need_rates
            or conditions.daily_rhythms
            or operator_composition
        )
        has_explicit_selection = bool(selection.age_band_multipliers)
        if (
            (has_explicit_condition_input or has_explicit_selection)
            and not conditions.source_ids
            and not selection.source_ids
        ):
            source_id_set.add(OPERATOR_POPULATION_SOURCE_ID)
        source_ids = tuple(sorted(source_id_set))
        profiles: list[ProfileSpec] = []
        people: dict[str, PersonTraits] = {}
        ages_by_index: dict[int, int] = {}
        medical_support_by_index: dict[int, bool] = {}
        medication_support_by_index: dict[int, bool] = {}
        care_support_needs_by_index: dict[int, tuple[str, ...]] = {}
        rhythm_by_index: dict[int, DailyRhythmTemplate | None] = {}
        household_members: dict[str, list[int]] = {}
        for member_index, household_id in household_by_index.items():
            household_members.setdefault(household_id, []).append(member_index)
        pattern_by_index: dict[int, HouseholdMemberPattern] = {}
        for household_id, template in household_templates.items():
            for member_index, pattern in zip(
                sorted(household_members[household_id]), template.members
            ):
                pattern_by_index[member_index] = pattern

        household_relationships: list[HouseholdRelationship] = []
        child_caregiver_by_index: dict[int, set[int]] = {}
        support_caregiver_by_index: dict[int, set[int]] = {}
        support_required_by_relationship: set[int] = set()
        explicit_dependent_indexes = {
            index for index, pattern in pattern_by_index.items() if pattern.role == "dependent"
        }
        for household_id, template in sorted(household_templates.items()):
            members = sorted(household_members[household_id])
            for link in template.relationships:
                caregiver_index = members[link.caregiver_member_index]
                dependent_index = members[link.dependent_member_index]
                source_id = (
                    link.source_id
                    or (template.source_ids[0] if template.source_ids else None)
                    or (
                        household_composition.source_ids[0]
                        if household_composition and household_composition.source_ids
                        else None
                    )
                    or OPERATOR_POPULATION_SOURCE_ID
                )
                household_relationships.append(
                    HouseholdRelationship(
                        household_id=household_id,
                        caregiver_id=profile_set.profiles[caregiver_index].id,
                        dependent_id=profile_set.profiles[dependent_index].id,
                        relationship=link.relationship,
                        source_id=source_id,
                    )
                )
                if link.relationship in {"parent_child", "caregiver_child"}:
                    child_caregiver_by_index.setdefault(dependent_index, set()).add(caregiver_index)
                if link.relationship == "caregiver_support":
                    support_required_by_relationship.add(dependent_index)
                    support_caregiver_by_index.setdefault(dependent_index, set()).add(
                        caregiver_index
                    )

        for household_id, member_indexes in sorted(household_members.items()):
            ages_by_index.update(
                _sample_household_ages(
                    member_indexes=tuple(sorted(member_indexes)),
                    profiles=profile_set.profiles,
                    template=household_templates.get(household_id),
                    bands=bands,
                    selected_distribution=evacuee_age_distribution,
                    rng=rng,
                )
            )

        for index, profile in enumerate(profile_set.profiles):
            medical_support_by_index[index] = index in support_required_by_relationship or (
                rng.random("population", profile.id, "medical_support")
                < conditions.medical_support_rate
                if conditions.medical_support_rate is not None
                else False
            )
            medication_support_by_index[index] = (
                rng.random("population", profile.id, "medication_support")
                < conditions.medication_support_rate
                if conditions.medication_support_rate is not None
                else False
            )
            care_support_needs_by_index[index] = tuple(
                support_kind
                for support_kind, rate in sorted(conditions.care_support_need_rates.items())
                if rng.random("population", profile.id, "care_support_need", support_kind) < rate
            )
            if (
                pattern_by_index.get(index) is not None
                and pattern_by_index[index].role == "dependent"
            ):
                medical_support_by_index[index] = True
            rhythm_by_index[index] = (
                rhythm_by_id[
                    _draw_weighted_key(
                        rng.random("population", profile.id, "daily_rhythm"), rhythm_choices
                    )
                ]
                if rhythm_choices
                else None
            )
        generated_requirements: list[GeneratedPersonRequirements] = []
        household_pet_count: dict[str, int | None] = {}
        for household_id, member_indexes in sorted(household_members.items()):
            household_pet_count[household_id] = (
                _draw_weighted_integer(
                    rng.random("population", household_id, "pet_count"),
                    conditions.pet_count_weights,
                )
                if conditions.pet_count_weights
                else (
                    int(
                        rng.random("population", household_id, "has_pet")
                        < conditions.pet_household_rate
                    )
                    if conditions.pet_household_rate is not None
                    else None
                )
            )
        for index, profile in enumerate(profile_set.profiles):
            household_id = household_by_index[index]
            age = ages_by_index[index]
            household_size = household_sizes[household_id]
            medical_support = medical_support_by_index[index]
            medication_support = medication_support_by_index[index]
            care_support_needs = tuple(
                (["medication_support"] if medication_support else [])
                + (["medical_assessment"] if medical_support else [])
                + list(care_support_needs_by_index[index])
            )
            extra_access_needs = tuple(
                need_id
                for need_id, probability in conditions.additional_access_need_rates.items()
                if rng.random("population", profile.id, "access_need", need_id) < probability
            )
            support_required = medical_support or medication_support
            if age < 18:
                role = "child"
            elif index in explicit_dependent_indexes or (
                support_required and index in support_caregiver_by_index
            ):
                role = "dependent"
            elif household_size == 1:
                role = "alone"
            else:
                role = "adult"
            profile_values = profile.model_dump(mode="python") | {"group_id": household_id}
            if support_required or care_support_needs or extra_access_needs:
                profile_values["support_need"] = max(profile.support_need, 0.5)
            updated = ProfileSpec.model_validate(profile_values)
            profiles.append(updated)
            assigned_language = _draw_weighted_key(
                rng.random("population", profile.id, "language"),
                normalized_language_weights,
            )
            people[updated.id] = PersonTraits(
                age_years=age,
                household_id=household_id,
                household_role=role,
                languages=(assigned_language,),
                accessibility_needs=tuple(
                    sorted(
                        (
                            {"mobility_support"}
                            if updated.mobility is not Mobility.INDEPENDENT
                            else set()
                        )
                        | set(extra_access_needs)
                    )
                ),
                # PopulationSpec v1 uses this legacy field for household support
                # relationship validation; typed medical/medication distinctions
                # are carried separately by GeneratedPersonRequirements/PersonContext.
                medical_support=support_required,
                source_ids=tuple(sorted(set(source_ids) | set(conditions.source_ids))),
            )
            caregiver_indexes_for_person = child_caregiver_by_index.get(
                index, set()
            ) | support_caregiver_by_index.get(index, set())
            caregivers = tuple(
                profile_set.profiles[item].id for item in sorted(caregiver_indexes_for_person)
            )
            dependent_indexes = {
                dependent_index
                for dependent_index, caregiver_indexes in child_caregiver_by_index.items()
                if index in caregiver_indexes
            } | {
                dependent_index
                for dependent_index, caregiver_indexes in support_caregiver_by_index.items()
                if index in caregiver_indexes
            }
            dependents = tuple(profile_set.profiles[item].id for item in sorted(dependent_indexes))
            information_access = (
                _draw_weighted_key(
                    rng.random("population", profile.id, "information_access"), information_choices
                )
                if information_choices
                else None
            )
            pet_count = household_pet_count[household_id]
            if index != min(household_members[household_id]):
                pet_count = None
            daily_rhythm = rhythm_by_index[index]
            review_reasons: list[str] = []
            if age < 18 and index not in child_caregiver_by_index:
                review_reasons.append("unaccompanied_minor")
            if support_required and not caregivers:
                review_reasons.append("support_need_without_explicit_household_caregiver")
            generated_requirements.append(
                GeneratedPersonRequirements(
                    person_id=updated.id,
                    household_id=household_id,
                    household_role=role,
                    guardian_status=(
                        "not_applicable"
                        if age >= 18
                        else "accompanied"
                        if index in child_caregiver_by_index
                        else "unaccompanied"
                    ),
                    caregiver_ids=caregivers,
                    dependent_ids=dependents,
                    medical_support_required=(
                        medical_support
                        if conditions.medical_support_rate is not None
                        or index in support_required_by_relationship
                        or index in explicit_dependent_indexes
                        else None
                    ),
                    medication_support_required=(
                        medication_support
                        if conditions.medication_support_rate is not None
                        else None
                    ),
                    care_support_needs=care_support_needs,
                    information_access=information_access,
                    pet_count=pet_count,
                    daily_rhythm_id=daily_rhythm.rhythm_id if daily_rhythm else None,
                    sleep_start_minute=daily_rhythm.sleep_start_minute if daily_rhythm else None,
                    wake_minute=daily_rhythm.wake_minute if daily_rhythm else None,
                    requires_review=bool(review_reasons),
                    review_reasons=tuple(review_reasons),
                )
            )

        grouped = GeneratedProfileSet(profiles=tuple(profiles), manifest=profile_set.manifest)
        population = PopulationSpec(people=people, source_ids=source_ids)
        assumptions: list[dict[str, object]] = []
        sources: list[dict[str, object]] = []
        if age_assumed:
            assumptions.append(
                {
                    "id": SYNTHETIC_AGE_ASSUMPTION_ID,
                    "subject": "synthetic_age_band_mix",
                    "value": {"child": 1, "adult": 1, "older_adult": 1},
                    "reason": (
                        "No selected regional age table was supplied; equal band weights are "
                        "only a generic simulation default."
                    ),
                    "source_id": SYNTHETIC_AGE_ASSUMPTION_ID,
                }
            )
            sources.append(
                {
                    "id": SYNTHETIC_AGE_ASSUMPTION_ID,
                    "title": "Generic synthetic age-band assumption",
                    "source_kind": "assumption",
                    "description": (
                        "Not an official statistic and not calibrated to the requested "
                        "municipality."
                    ),
                }
            )
        if household_assumed:
            assumptions.append(
                {
                    "id": SYNTHETIC_HOUSEHOLD_ASSUMPTION_ID,
                    "subject": "synthetic_household_size_mix",
                    "value": {str(size): weight for size, weight in mix.size_weights.items()},
                    "reason": (
                        "No selected regional household table was supplied; weights are a "
                        "generator heuristic."
                    ),
                    "source_id": SYNTHETIC_HOUSEHOLD_ASSUMPTION_ID,
                }
            )
            sources.append(
                {
                    "id": SYNTHETIC_HOUSEHOLD_ASSUMPTION_ID,
                    "title": "Generic synthetic household-size assumption",
                    "source_kind": "assumption",
                    "description": (
                        "Not an official statistic and not calibrated to the requested "
                        "municipality."
                    ),
                }
            )
        if language_assumed:
            assumptions.append(
                {
                    "id": SYNTHETIC_LANGUAGE_ASSUMPTION_ID,
                    "subject": "synthetic_language_mix",
                    "value": normalized_language_weights,
                    "reason": (
                        "No source was supplied for the language assignment weights; one primary "
                        "language is sampled per synthetic profile and the weights are not "
                        "calibrated to the requested region."
                    ),
                    "source_id": SYNTHETIC_LANGUAGE_ASSUMPTION_ID,
                }
            )
            sources.append(
                {
                    "id": SYNTHETIC_LANGUAGE_ASSUMPTION_ID,
                    "title": "Synthetic language assignment assumption",
                    "source_kind": "assumption",
                    "description": (
                        "One primary language is sampled per synthetic profile; not calibrated "
                        "to census, visa, or municipal language data."
                    ),
                }
            )
        if not selection.age_band_multipliers:
            assumptions.append(
                {
                    "id": SYNTHETIC_EVACUATION_SELECTION_ID,
                    "subject": "evacuation_selection_by_age",
                    "value": {band.label: 1.0 for band in bands},
                    "reason": (
                        "No evacuation-selection data was supplied; selection is uniform across "
                        "the observed resident distribution and is not an official evacuation "
                        "rate."
                    ),
                    "source_id": SYNTHETIC_EVACUATION_SELECTION_ID,
                }
            )
            sources.append(
                {
                    "id": SYNTHETIC_EVACUATION_SELECTION_ID,
                    "title": "Uniform synthetic evacuation-selection assumption",
                    "source_kind": "assumption",
                    "description": (
                        "No evacuation-rate data was supplied; the resident distribution is used "
                        "without an age-specific selection adjustment."
                    ),
                }
            )
        assumptions.append(
            {
                "id": SYNTHETIC_HOUSEHOLD_CONSTRUCTION_ID,
                "subject": "household_relationship_policy",
                "value": {
                    "minor_age_threshold": 18,
                    "minimum_parent_age_gap": 16,
                    "relationships_created_from_age_or_co_residence": False,
                    "unlinked_minor_requires_review": True,
                },
                "reason": (
                    "A shared household ID is not evidence of a family relationship. Parent, "
                    "partner, and care links are created only from explicit household templates; "
                    "age checks only reject implausible declared links. Unlinked minors remain "
                    "flagged for operator review."
                ),
                "source_id": SYNTHETIC_HOUSEHOLD_CONSTRUCTION_ID,
            }
        )
        unselected_template_ids = (
            tuple(
                sorted(
                    {item.template_id for item in household_composition.templates}
                    - {item.template_id for item in household_templates.values()}
                )
            )
            if operator_composition
            else ()
        )
        if unselected_template_ids:
            assumptions.append(
                {
                    "id": OPERATOR_POPULATION_SOURCE_ID,
                    "subject": "unselected_household_templates",
                    "value": list(unselected_template_ids),
                    "reason": (
                        "These operator templates did not match a generated household size in "
                        "this seed and were not applied."
                    ),
                    "source_id": OPERATOR_POPULATION_SOURCE_ID,
                }
            )
        if household_composition is not None and household_composition.source_kind == "assumption":
            assumptions.append(
                {
                    "id": household_composition.source_ids[0],
                    "subject": "synthetic_household_composition_templates",
                    "value": {
                        "applied_template_ids": sorted(
                            {item.template_id for item in household_templates.values()}
                        ),
                        "minor_count": sum(
                            1 for person in people.values() if person.age_years < 18
                        ),
                    },
                    "reason": (
                        "No household composition was entered, so generic synthetic templates "
                        "place each generated minor with a linked adult. They describe no real "
                        "family and are not regional statistics."
                    ),
                    "source_id": household_composition.source_ids[0],
                }
            )
            sources.extend(
                {
                    "id": source_id,
                    "title": "Generic synthetic household composition templates",
                    "source_kind": "assumption",
                    "description": (
                        "Template shapes and weights are a generator heuristic; operator-entered "
                        "compositions replace them."
                    ),
                }
                for source_id in household_composition.source_ids
            )
        sources.append(
            {
                "id": SYNTHETIC_HOUSEHOLD_CONSTRUCTION_ID,
                "title": "Synthetic household relationship policy",
                "source_kind": "assumption",
                "description": (
                    "Household membership never creates family or guardian links by itself."
                ),
            }
        )
        assumptions.append(
            {
                "id": SYNTHETIC_TRAIT_ASSUMPTION_ID,
                "subject": "synthetic_person_traits",
                "value": {
                    "medical_support": "unknown unless an explicit rate is supplied",
                    "support_need_rule": (
                        "explicit medical, medication, or additional access need raises "
                        "support_need to at least 0.5"
                    ),
                    "mobility_support_from_profile": True,
                    "household_role_rule": (
                        "child below 18; alone for a one-person adult household; otherwise adult "
                        "or dependent when explicit support is linked"
                    ),
                    "mobility_weights": {
                        "independent": profile_set.manifest.template.independent_weight,
                        "assisted": profile_set.manifest.template.assisted_weight,
                        "wheelchair": profile_set.manifest.template.wheelchair_weight,
                    },
                },
                "reason": (
                    "Medical support, information access, and pet status remain unknown unless "
                    "an explicit input rate or distribution is supplied. Mobility weights and "
                    "caregiver links are generic generation rules, not local data."
                ),
                "source_id": SYNTHETIC_TRAIT_ASSUMPTION_ID,
            }
        )
        sources.append(
            {
                "id": SYNTHETIC_TRAIT_ASSUMPTION_ID,
                "title": "Synthetic person-trait mapping",
                "source_kind": "assumption",
                "description": (
                    "Traits are generated from the documented profile template; mobility, "
                    "medical support, and household relations are not inferred from age or "
                    "identity."
                ),
            }
        )
        if (
            (has_explicit_condition_input or has_explicit_selection)
            and not conditions.source_ids
            and not selection.source_ids
        ):
            sources.append(
                {
                    "id": OPERATOR_POPULATION_SOURCE_ID,
                    "title": "Operator-entered population generation conditions",
                    "source_kind": "operator",
                    "description": (
                        "Input rates and categories were entered by the scenario operator and "
                        "are not regional statistics."
                    ),
                }
            )
        declared_household_source_ids = set()
        if household_composition is not None:
            declared_household_source_ids.update(household_composition.source_ids)
            declared_household_source_ids.update(
                source_id
                for template in household_composition.templates
                for source_id in template.source_ids
            )
            declared_household_source_ids.update(
                relationship.source_id
                for template in household_composition.templates
                for relationship in template.relationships
                if relationship.source_id is not None
            )
            if (
                household_composition.source_kind == "operator"
                and not household_composition.source_ids
            ):
                declared_household_source_ids.add(OPERATOR_POPULATION_SOURCE_ID)
        described_source_ids = {str(item.get("id")) for item in sources}
        for source_id in sorted(declared_household_source_ids - described_source_ids):
            sources.append(
                {
                    "id": source_id,
                    "title": "Operator-declared household composition reference",
                    "source_kind": household_composition.source_kind
                    if household_composition
                    else "operator",
                    "description": (
                        "This is a user-supplied source ID declaration. The generator does "
                        "not verify the citation or its applicability."
                    ),
                }
            )
        return GeneratedPopulation(
            profile_set=grouped,
            population=population,
            household_sizes=household_sizes,
            age_bands=bands,
            resident_age_distribution=resident_age_distribution,
            evacuee_age_distribution=evacuee_age_distribution,
            household_mix=mix,
            evacuation_selection=selection,
            household_relationships=tuple(household_relationships),
            household_template_ids={
                household_id: template.template_id
                for household_id, template in household_templates.items()
            },
            unselected_template_ids=(
                tuple(
                    sorted(
                        {item.template_id for item in household_composition.templates}
                        - {item.template_id for item in household_templates.values()}
                    )
                )
                if household_composition is not None
                else ()
            ),
            household_composition=household_composition,
            person_requirements=tuple(generated_requirements),
            trait_input=conditions,
            assumptions=tuple(assumptions),
            sources=tuple(sources),
        )


def _sample_household_ages(
    *,
    member_indexes: tuple[int, ...],
    profiles: tuple[ProfileSpec, ...],
    template: HouseholdTemplate | None,
    bands: tuple[AgeBand, ...],
    selected_distribution: dict[str, float],
    rng: CounterRNG,
) -> dict[int, int]:
    """Sample age-constrained slots without fabricating links from co-residence."""
    if template is None:
        return {
            index: ResidentPopulationSampler.sample_selected_person(
                rng.random("population", profiles[index].id, "age_band"),
                rng,
                profiles[index].id,
                bands,
                selected_distribution,
            )
            for index in member_indexes
        }

    slot_to_index = dict(enumerate(member_indexes))
    children_by_caregiver: dict[int, set[int]] = {}
    adult_slots: set[int] = set()
    child_slots: set[int] = set()
    for relationship in template.relationships:
        caregiver = relationship.caregiver_member_index
        dependent = relationship.dependent_member_index
        if relationship.relationship in {"parent_child", "caregiver_child"}:
            children_by_caregiver.setdefault(caregiver, set()).add(dependent)
            adult_slots.add(caregiver)
            child_slots.add(dependent)
        elif relationship.relationship == "caregiver_support":
            adult_slots.add(caregiver)
        elif relationship.relationship == "partner":
            adult_slots.update((caregiver, dependent))

    # Child ages are sampled before their caregivers so the explicit 16-year
    # plausibility check can constrain adult ages without a retry loop.
    indegree = [0] * len(template.members)
    dependents_to_caregivers: dict[int, set[int]] = {}
    for caregiver, dependents in children_by_caregiver.items():
        for dependent in dependents:
            dependents_to_caregivers.setdefault(dependent, set()).add(caregiver)
            indegree[caregiver] += 1
    ready = [index for index, degree in enumerate(indegree) if degree == 0]
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for caregiver in sorted(dependents_to_caregivers.get(current, ())):
            indegree[caregiver] -= 1
            if indegree[caregiver] == 0:
                ready.append(caregiver)
    if len(order) != len(template.members):
        raise ValueError("parent/caregiver-child template relationships cannot contain cycles")

    minimum_by_slot: dict[int, int] = {}
    maximum_by_slot: dict[int, int] = {}
    for slot, pattern in enumerate(template.members):
        minimum_age = 18 if pattern.role in {"adult", "caregiver"} or slot in adult_slots else 0
        maximum_age = 17 if pattern.role == "child" or slot in child_slots else 120
        supported_maximum = _maximum_supported_age(
            bands=bands,
            selected_distribution=selected_distribution,
            minimum_age=minimum_age,
            maximum_age=maximum_age,
            age_band_label=pattern.age_band_label,
        )
        if supported_maximum is None:
            raise ValueError(
                "household template has no selected age support for person slot "
                f"{profiles[slot_to_index[slot]].id!r} "
                f"within ages {minimum_age}..{maximum_age}"
            )
        minimum_by_slot[slot] = minimum_age
        maximum_by_slot[slot] = supported_maximum

    # Propagate caregiver age ceilings from older generations toward dependents.
    # This caps a child's draw before sampling, so a feasible template cannot fail
    # only because its seeded child age was too high for the selected adult bands.
    caregivers_by_dependent: dict[int, set[int]] = {}
    for caregiver, dependents in children_by_caregiver.items():
        for dependent in dependents:
            caregivers_by_dependent.setdefault(dependent, set()).add(caregiver)
    remaining_caregivers = [
        len(caregivers_by_dependent.get(slot, ())) for slot in range(len(template.members))
    ]
    forward_ready = [slot for slot, count in enumerate(remaining_caregivers) if count == 0]
    propagated_maximum = dict(maximum_by_slot)
    visited = 0
    while forward_ready:
        caregiver = forward_ready.pop(0)
        visited += 1
        for dependent in sorted(children_by_caregiver.get(caregiver, ())):
            propagated_maximum[dependent] = min(
                propagated_maximum[dependent], propagated_maximum[caregiver] - 16
            )
            remaining_caregivers[dependent] -= 1
            if remaining_caregivers[dependent] == 0:
                forward_ready.append(dependent)
    if visited != len(template.members):
        raise ValueError("parent/caregiver-child template relationships cannot contain cycles")

    ages_by_slot: dict[int, int] = {}
    for slot in order:
        pattern = template.members[slot]
        minimum_age = minimum_by_slot[slot]
        maximum_age = propagated_maximum[slot]
        dependent_ages = [ages_by_slot[index] for index in children_by_caregiver.get(slot, ())]
        if dependent_ages:
            minimum_age = max(minimum_age, max(dependent_ages) + 16, 18)
        ages_by_slot[slot] = _draw_age_with_constraints(
            profile_id=profiles[slot_to_index[slot]].id,
            bands=bands,
            selected_distribution=selected_distribution,
            rng=rng,
            minimum_age=minimum_age,
            maximum_age=maximum_age,
            age_band_label=pattern.age_band_label,
        )
    return {slot_to_index[slot]: age for slot, age in ages_by_slot.items()}


def _maximum_supported_age(
    *,
    bands: tuple[AgeBand, ...],
    selected_distribution: dict[str, float],
    minimum_age: int,
    maximum_age: int,
    age_band_label: str | None,
) -> int | None:
    supported = [
        min(band.age_max, maximum_age)
        for band in bands
        if (age_band_label is None or band.label == age_band_label)
        and selected_distribution.get(band.label, 0.0) > 0
        and max(band.age_min, minimum_age) <= min(band.age_max, maximum_age)
    ]
    return max(supported) if supported else None


def _draw_age_with_constraints(
    *,
    profile_id: str,
    bands: tuple[AgeBand, ...],
    selected_distribution: dict[str, float],
    rng: CounterRNG,
    minimum_age: int,
    maximum_age: int,
    age_band_label: str | None,
) -> int:
    candidates: list[tuple[AgeBand, int, int, float]] = []
    for band in bands:
        lower = max(band.age_min, minimum_age)
        upper = min(band.age_max, maximum_age)
        if age_band_label is not None and band.label != age_band_label:
            continue
        if lower > upper:
            continue
        band_weight = selected_distribution.get(band.label, 0.0)
        if band_weight <= 0:
            continue
        eligible_fraction = (upper - lower + 1) / (band.age_max - band.age_min + 1)
        candidates.append((band, lower, upper, band_weight * eligible_fraction))
    if not candidates:
        raise ValueError(
            f"household template has no selected age support for person slot {profile_id!r} "
            f"within ages {minimum_age}..{maximum_age}"
        )
    chosen = _draw_weighted_key(
        rng.random("population", profile_id, "age_band"),
        _normalize_weights({band.label: weight for band, _low, _high, weight in candidates}),
    )
    _band, lower, upper, _weight = next(item for item in candidates if item[0].label == chosen)
    span = upper - lower + 1
    return lower + min(span - 1, int(rng.random("population", profile_id, "age_years") * span))


def age_bands_from_observations(
    observations: Iterable[StatisticalObservation], *, dimension_id: str | None = None
) -> tuple[AgeBand, ...]:
    """Use e-Stat age rows only when labels and counts form one unambiguous age axis."""
    rows = tuple(observations)
    if not rows:
        return ()
    candidate_dimensions = (
        (dimension_id,)
        if dimension_id is not None
        else tuple(sorted({key for row in rows for key in row.category_labels}))
    )
    valid: list[tuple[AgeBand, ...]] = []
    for candidate in candidate_dimensions:
        bands: list[AgeBand] = []
        seen_ranges: set[tuple[int, int]] = set()
        source_valid = True
        for row in rows:
            label = row.category_labels.get(candidate, "")
            age_range = _parse_age_range(label)
            unit = (row.unit or "").casefold()
            scale = {
                "人": 1,
                "person": 1,
                "persons": 1,
                "people": 1,
                "千人": 1_000,
                "万人": 10_000,
            }.get(unit)
            scaled_count = row.value * scale if scale is not None else None
            if (
                age_range is None
                or scaled_count is None
                or not math.isfinite(scaled_count)
                or abs(scaled_count - round(scaled_count)) > 1e-8
                or scaled_count < 0
            ):
                source_valid = False
                break
            if age_range in seen_ranges:
                # Repeated age bands often mean an unselected sex or other dimension.
                source_valid = False
                break
            seen_ranges.add(age_range)
            bands.append(
                AgeBand(
                    label=label,
                    age_min=age_range[0],
                    age_max=age_range[1],
                    population_count=int(round(scaled_count)),
                    source_ids=(row.source_id,),
                )
            )
        if source_valid and bands:
            valid.append(tuple(sorted(bands, key=lambda item: item.age_min)))
    if len(valid) != 1:
        return ()
    return valid[0] if sum(item.population_count for item in valid[0]) > 0 else ()


def household_mix_from_observations(
    observations: Iterable[StatisticalObservation], *, dimension_id: str | None = None
) -> HouseholdSizeMix | None:
    """Convert an unambiguous count of households by household-size category to weights."""
    rows = tuple(observations)
    if not rows:
        return None
    candidate_dimensions = (
        (dimension_id,)
        if dimension_id is not None
        else tuple(sorted({key for row in rows for key in row.category_labels}))
    )
    valid: list[HouseholdSizeMix] = []
    for candidate in candidate_dimensions:
        counts: dict[int, float] = {}
        source_ids: set[str] = set()
        for row in rows:
            label = row.category_labels.get(candidate, "").strip()
            match = re.fullmatch(r"(\d+)\s*人(?:世帯)?", label)
            unit = (row.unit or "").casefold()
            if (
                match is None
                or unit not in {"世帯", "世帯数", "household", "households", "千世帯", "万世帯"}
                or not math.isfinite(row.value)
                or row.value < 0
            ):
                counts = {}
                break
            size = int(match.group(1))
            if size < 1 or size > 10 or size in counts:
                counts = {}
                break
            unit_scale = {
                "世帯": 1,
                "世帯数": 1,
                "household": 1,
                "households": 1,
                "千世帯": 1_000,
                "万世帯": 10_000,
            }[unit]
            scaled_count = row.value * unit_scale
            if abs(scaled_count - round(scaled_count)) > 1e-8:
                counts = {}
                break
            counts[size] = float(round(scaled_count))
            source_ids.add(row.source_id)
        if counts and sum(counts.values()) > 0:
            valid.append(
                HouseholdSizeMix(
                    size_weights=counts,
                    source_ids=tuple(sorted(source_ids)),
                )
            )
    return valid[0] if len(valid) == 1 else None


def _draw_weighted_size(draw: float, mix: HouseholdSizeMix) -> int:
    total = sum(mix.size_weights.values())
    threshold = draw * total
    for size, weight in sorted(mix.size_weights.items()):
        threshold -= weight
        if threshold < 0:
            return size
    return max(mix.size_weights)


def _normalize_weights(weights: dict[str, float]) -> tuple[tuple[str, float], ...]:
    total = sum(weights.values())
    if not weights or not math.isfinite(total) or total <= 0:
        return ()
    return tuple((key, value / total) for key, value in sorted(weights.items()) if value > 0)


def _draw_weighted_key(draw: float, weights: tuple[tuple[str, float], ...]) -> str:
    if not weights:
        raise ValueError("a weighted choice requires at least one positive option")
    threshold = draw
    for key, weight in weights:
        threshold -= weight
        if threshold < 0:
            return key
    return weights[-1][0]


def _draw_weighted_integer(draw: float, weights: dict[int, float]) -> int:
    total = sum(weights.values())
    if not weights or not math.isfinite(total) or total <= 0:
        raise ValueError("a weighted integer choice requires a positive finite total")
    threshold = draw * total
    for value, weight in sorted(weights.items()):
        threshold -= weight
        if threshold < 0:
            return value
    return max(weights)


def _resident_age_distribution(bands: tuple[AgeBand, ...]) -> dict[str, float]:
    total = sum(band.population_count for band in bands)
    if total <= 0:
        raise ValueError("resident distribution must have a positive population total")
    return {band.label: band.population_count / total for band in bands}


def _evacuee_age_distribution(
    resident_distribution: dict[str, float], selection: EvacuationSelection
) -> dict[str, float]:
    weighted = {
        label: share * selection.age_band_multipliers.get(label, 1.0)
        for label, share in resident_distribution.items()
    }
    total = sum(weighted.values())
    if total <= 0 or not math.isfinite(total):
        raise ValueError(
            "resident distribution and evacuation selection have no positive age-band weight"
        )
    return {label: weight / total for label, weight in weighted.items()}


def _draw_age_from_distribution(
    draw: float,
    rng: CounterRNG,
    profile_id: str,
    bands: tuple[AgeBand, ...],
    distribution: dict[str, float],
) -> int:
    threshold = draw
    selected = next(band for band in reversed(bands) if distribution[band.label] > 0)
    for band in bands:
        weight = distribution[band.label]
        threshold -= weight
        if threshold < 0:
            selected = band
            break
    age_width = selected.age_max - selected.age_min + 1
    offset = int(rng.random("population", profile_id, "age") * age_width)
    return min(selected.age_min + offset, selected.age_max)


def _draw_age(draw: float, rng: CounterRNG, profile_id: str, bands: tuple[AgeBand, ...]) -> int:
    total = sum(band.population_count for band in bands)
    threshold = draw * total
    selected = bands[-1]
    for band in bands:
        threshold -= band.population_count
        if threshold < 0:
            selected = band
            break
    width = selected.age_max - selected.age_min + 1
    offset = int(rng.random("population", profile_id, "age") * width)
    return min(selected.age_min + offset, selected.age_max)


def _parse_age_range(label: str) -> tuple[int, int] | None:
    normalized = label.strip().replace("歳", "")
    under = re.fullmatch(r"(\d{1,3})未満", normalized)
    if under:
        upper = int(under.group(1)) - 1
        return (0, upper) if upper >= 0 else None
    over = re.fullmatch(r"(\d{1,3})以上", normalized)
    if over:
        lower = int(over.group(1))
        return (lower, 120) if lower <= 120 else None
    interval = re.fullmatch(r"(\d{1,3})\s*[～〜~\-－–]\s*(\d{1,3})", normalized)
    if not interval:
        return None
    low, high = int(interval.group(1)), int(interval.group(2))
    return (low, high) if 0 <= low <= high <= 120 else None
