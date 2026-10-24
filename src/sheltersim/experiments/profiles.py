"""Offline deterministic generation and pre-run handling of synthetic profiles."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.core.rng import CounterRNG
from sheltersim.domain.models import Mobility
from sheltersim.experiments.scenario import (
    ContractModel,
    InitialNeedsSpec,
    ProfileProvenance,
    ProfileSpec,
    ScenarioSpec,
    StableId,
)


class ProfileTemplate(ContractModel):
    """Small synthetic generation template; weights are explicit assumptions."""

    template_id: StableId = "synthetic-balanced-v1"
    agent_id_prefix: StableId = "A"
    group_id_prefix: StableId = "G"
    id_width: int = Field(default=3, ge=1, le=8)
    group_size: int = Field(default=4, gt=0)
    initial_zone_ids: tuple[StableId, ...] = ()
    independent_weight: float = Field(default=0.80, ge=0.0, allow_inf_nan=False)
    assisted_weight: float = Field(default=0.15, ge=0.0, allow_inf_nan=False)
    wheelchair_weight: float = Field(default=0.05, ge=0.0, allow_inf_nan=False)
    support_need_min: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    support_need_max: float = Field(default=0.6, ge=0.0, le=1.0, allow_inf_nan=False)
    stress_sensitivity_min: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    stress_sensitivity_max: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    toilet_urgency_min: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    toilet_urgency_max: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    hunger_min: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    hunger_max: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    fatigue_min: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    fatigue_max: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_template(self) -> ProfileTemplate:
        weights = (self.independent_weight, self.assisted_weight, self.wheelchair_weight)
        if sum(weights) <= 0:
            raise ValueError("at least one mobility weight must be positive")
        if self.support_need_min > self.support_need_max:
            raise ValueError("support_need_min cannot exceed support_need_max")
        for name in ("stress_sensitivity", "toilet_urgency", "hunger", "fatigue"):
            if getattr(self, f"{name}_min") > getattr(self, f"{name}_max"):
                raise ValueError(f"{name}_min cannot exceed {name}_max")
        if len(self.initial_zone_ids) != len(set(self.initial_zone_ids)):
            raise ValueError("template initial_zone_ids must be unique")
        return self


@dataclass(frozen=True)
class RepresentativeProfileSelection:
    """A synthetic profile selected by a declared display criterion."""

    criterion_id: str
    label: str
    selection_reason: str
    profile: ProfileSpec


def select_representative_profiles(
    profiles: Iterable[ProfileSpec],
) -> tuple[RepresentativeProfileSelection, ...]:
    """Select bounded synthetic examples by explicit need, mobility and group criteria."""
    ordered = tuple(sorted(profiles, key=lambda item: item.id))
    if not ordered:
        return ()

    used: set[str] = set()
    selected: list[RepresentativeProfileSelection] = []

    def need_score(profile: ProfileSpec) -> float:
        needs = profile.initial_needs
        return max(needs.toilet_urgency, needs.hunger, needs.fatigue)

    def choose(
        candidates: Iterable[ProfileSpec],
        *,
        criterion_id: str,
        label: str,
        reason: str,
    ) -> None:
        available = [profile for profile in candidates if profile.id not in used]
        if not available:
            return
        profile = min(
            available,
            key=lambda item: (-item.support_need, -need_score(item), item.id),
        )
        used.add(profile.id)
        selected.append(
            RepresentativeProfileSelection(
                criterion_id=criterion_id,
                label=label,
                selection_reason=(
                    f"{reason}; support_need={profile.support_need:.3f}; "
                    f"highest initial need={need_score(profile):.3f}; tie-break=Agent ID"
                ),
                profile=profile,
            )
        )

    choose(
        ordered,
        criterion_id="support_need",
        label="支援ニーズが高いプロフィール",
        reason="support_needを優先し、同値なら初期need最大値で選定",
    )
    choose(
        (profile for profile in ordered if profile.mobility is not Mobility.INDEPENDENT),
        criterion_id="mobility_support",
        label="移動支援フラグのあるプロフィール",
        reason="独立移動以外のmobilityとsupport_need、初期needで選定",
    )
    groups: dict[str, list[ProfileSpec]] = {}
    for profile in ordered:
        if profile.group_id is not None:
            groups.setdefault(profile.group_id, []).append(profile)
    family_groups = [members for members in groups.values() if len(members) > 1]
    if family_groups:
        largest_group = min(
            family_groups,
            key=lambda members: (-len(members), members[0].group_id or ""),
        )
        choose(
            largest_group,
            criterion_id="family_context",
            label="同じGroupに属するプロフィール",
            reason=(
                f"members={len(largest_group)}のGroupを選び、その中をsupport_need・初期needで選定"
            ),
        )
    return tuple(selected)


class ManualOverrideRecord(ContractModel):
    """Audit metadata for a profile edited after synthetic generation."""

    agent_id: StableId
    fields: tuple[str, ...]


class ProfileGenerationManifest(ContractModel):
    """Reproducibility metadata stored beside the generated profiles."""

    schema_version: Literal[2] = 2
    generator: str = "counter-rng-profile-generator-v1"
    rng_algorithm: Literal["sha256-keyed-v1"] = CounterRNG.algorithm_id
    seed: int = Field(ge=0)
    template: ProfileTemplate
    profile_count: int = Field(gt=0)
    zone_ids: tuple[StableId, ...]
    manual_overrides: tuple[ManualOverrideRecord, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> ProfileGenerationManifest:
        if not self.zone_ids:
            raise ValueError("profile manifest must list at least one zone")
        if len(self.zone_ids) != len(set(self.zone_ids)):
            raise ValueError("profile manifest zone IDs must be unique")
        if len({record.agent_id for record in self.manual_overrides}) != len(self.manual_overrides):
            raise ValueError("profile manifest has duplicate manual override records")
        return self


@dataclass(frozen=True)
class GeneratedProfileSet:
    """Versioned, serializable result that can be previewed before a run."""

    profiles: tuple[ProfileSpec, ...]
    manifest: ProfileGenerationManifest

    def __post_init__(self) -> None:
        identifiers = [profile.id for profile in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("generated profile IDs must be unique")
        if len(self.profiles) != self.manifest.profile_count:
            raise ValueError("manifest profile_count must match generated profiles")
        unknown_zones = {profile.initial_zone_id for profile in self.profiles} - set(
            self.manifest.zone_ids
        )
        if unknown_zones:
            raise ValueError(f"generated profiles reference unknown zones: {sorted(unknown_zones)}")
        profile_ids = set(identifiers)
        unknown_override_ids = {
            record.agent_id for record in self.manifest.manual_overrides
        } - profile_ids
        if unknown_override_ids:
            raise ValueError(
                f"profile manifest overrides unknown Agents: {sorted(unknown_override_ids)}"
            )

    def canonical_json(self) -> str:
        """Return the complete stable artifact for preview or disk output."""
        document = {
            "schema_version": self.manifest.schema_version,
            "manifest": self.manifest.model_dump(mode="json"),
            "profiles": [
                profile.model_dump(mode="json")
                for profile in sorted(self.profiles, key=lambda item: item.id)
            ],
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )

    def save(self, path: str | Path) -> None:
        """Write every generated profile and its manifest as readable JSON."""
        Path(path).write_text(self.preview() + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, value: str) -> GeneratedProfileSet:
        """Load and validate a generated profile artifact."""
        document = json.loads(value)
        if document.get("schema_version") != 2 or isinstance(document.get("schema_version"), bool):
            raise ValueError("unsupported generated profile schema version")
        manifest = ProfileGenerationManifest.model_validate(document["manifest"])
        if manifest.schema_version != document["schema_version"]:
            raise ValueError("profile artifact and manifest schema versions do not match")
        return cls(
            profiles=tuple(ProfileSpec.model_validate(item) for item in document["profiles"]),
            manifest=manifest,
        )

    @classmethod
    def load(cls, path: str | Path) -> GeneratedProfileSet:
        """Read a generated profile artifact from disk."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def preview(self) -> str:
        """Return the complete readable artifact without truncating its profile list."""
        return json.dumps(
            json.loads(self.canonical_json()),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )

    def apply_manual_overrides(
        self,
        overrides: Mapping[str, Mapping[str, Any]],
    ) -> GeneratedProfileSet:
        """Apply validated field overrides and retain a compact audit manifest."""
        allowed_fields = {
            "group_id",
            "initial_zone_id",
            "mobility",
            "support_need",
            "stress_sensitivity",
            "initial_needs",
        }
        profiles_by_id = {profile.id: profile for profile in self.profiles}
        if set(overrides) - profiles_by_id.keys():
            unknown_ids = sorted(set(overrides) - profiles_by_id.keys())
            raise ValueError(f"manual overrides reference unknown Agent IDs: {unknown_ids}")

        records = {record.agent_id: record for record in self.manifest.manual_overrides}
        for agent_id in sorted(overrides):
            updates = dict(overrides[agent_id])
            if not updates:
                raise ValueError(f"manual override for {agent_id!r} is empty")
            unknown_fields = set(updates) - allowed_fields
            if unknown_fields:
                raise ValueError(
                    f"unsupported manual profile fields: {', '.join(sorted(unknown_fields))}"
                )
            current = profiles_by_id[agent_id]
            data = current.model_dump(mode="python")
            data.update(updates)
            target_zone = data.get("initial_zone_id")
            if target_zone not in self.manifest.zone_ids:
                raise ValueError(
                    f"manual override for {agent_id!r} references unknown zone {target_zone!r}"
                )
            data["provenance"] = ProfileProvenance(
                source_kind="human",
                generator="manual-override",
                seed=current.provenance.seed,
            )
            profiles_by_id[agent_id] = ProfileSpec.model_validate(data)
            previous = set(records[agent_id].fields) if agent_id in records else set()
            records[agent_id] = ManualOverrideRecord(
                agent_id=agent_id,
                fields=tuple(sorted(previous.union(updates))),
            )

        manifest = self.manifest.model_copy(
            update={"manual_overrides": tuple(records[key] for key in sorted(records))}
        )
        return GeneratedProfileSet(
            profiles=tuple(profiles_by_id[key] for key in sorted(profiles_by_id)),
            manifest=manifest,
        )


class SyntheticProfileGenerator:
    """Generate reproducible functional profiles without personal information."""

    generator_id = "counter-rng-profile-generator-v1"

    def generate(
        self,
        *,
        count: int = 100,
        seed: int,
        zone_ids: Iterable[str],
        template: ProfileTemplate | None = None,
    ) -> GeneratedProfileSet:
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("profile count must be a positive integer")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("profile generation seed cannot be negative")
        available_zones = tuple(sorted(set(zone_ids)))
        if not available_zones:
            raise ValueError("profile generation requires at least one valid zone ID")
        selected_template = template or ProfileTemplate()
        target_zones = selected_template.initial_zone_ids or available_zones
        unknown_zones = set(target_zones) - set(available_zones)
        if unknown_zones:
            raise ValueError(f"profile template references unknown zones: {sorted(unknown_zones)}")

        rng = CounterRNG(seed)
        profiles = tuple(
            self._generate_one(
                number=number,
                seed=seed,
                target_zones=target_zones,
                template=selected_template,
                rng=rng,
            )
            for number in range(1, count + 1)
        )
        manifest = ProfileGenerationManifest(
            seed=seed,
            template=selected_template,
            profile_count=count,
            zone_ids=available_zones,
        )
        return GeneratedProfileSet(profiles=profiles, manifest=manifest)

    @staticmethod
    def apply_to_scenario(scenario: ScenarioSpec, profiles: GeneratedProfileSet) -> ScenarioSpec:
        """Replace scenario profiles and embed the generation manifest in provenance."""
        scenario_zone_ids = {zone.id for zone in scenario.zones}
        if set(profiles.manifest.zone_ids) - scenario_zone_ids:
            raise ValueError("generated profile manifest references zones absent from the scenario")
        document = scenario.model_dump(mode="json")
        document["profiles"] = [profile.model_dump(mode="json") for profile in profiles.profiles]
        provenance = dict(document.get("provenance", {}))
        provenance["profile_generation"] = profiles.manifest.model_dump(mode="json")
        document["provenance"] = provenance
        return ScenarioSpec.model_validate(document)

    def _generate_one(
        self,
        *,
        number: int,
        seed: int,
        target_zones: tuple[str, ...],
        template: ProfileTemplate,
        rng: CounterRNG,
    ) -> ProfileSpec:
        agent_id = f"{template.agent_id_prefix}{number:0{template.id_width}d}"
        group_number = (number - 1) // template.group_size + 1
        group_id = f"{template.group_id_prefix}{group_number:0{template.id_width}d}"
        mobility = self._sample_mobility(agent_id, template, rng)
        support_draw = rng.random("profile", agent_id, "support_need")
        stress_draw = rng.random("profile", agent_id, "stress_sensitivity")
        initial_needs = InitialNeedsSpec(
            toilet_urgency=self._sample_range(
                rng.random("profile", agent_id, "toilet_urgency"),
                template.toilet_urgency_min,
                template.toilet_urgency_max,
            ),
            hunger=self._sample_range(
                rng.random("profile", agent_id, "hunger"),
                template.hunger_min,
                template.hunger_max,
            ),
            fatigue=self._sample_range(
                rng.random("profile", agent_id, "fatigue"),
                template.fatigue_min,
                template.fatigue_max,
            ),
        )
        zone_index = int(rng.random("profile", agent_id, "initial_zone") * len(target_zones))
        zone_index = min(zone_index, len(target_zones) - 1)
        return ProfileSpec(
            id=agent_id,
            group_id=group_id,
            initial_zone_id=target_zones[zone_index],
            mobility=mobility,
            support_need=template.support_need_min
            + support_draw * (template.support_need_max - template.support_need_min),
            stress_sensitivity=template.stress_sensitivity_min
            + stress_draw * (template.stress_sensitivity_max - template.stress_sensitivity_min),
            initial_needs=initial_needs,
            rng_key=f"profile:{agent_id}",
            provenance=ProfileProvenance(
                source_kind="synthetic",
                generator=template.template_id,
                seed=seed,
            ),
        )

    @staticmethod
    def _sample_mobility(agent_id: str, template: ProfileTemplate, rng: CounterRNG) -> Mobility:
        choices = (
            (Mobility.INDEPENDENT, template.independent_weight),
            (Mobility.ASSISTED, template.assisted_weight),
            (Mobility.WHEELCHAIR, template.wheelchair_weight),
        )
        threshold = rng.random("profile", agent_id, "mobility") * sum(
            weight for _, weight in choices
        )
        for mobility, weight in choices:
            threshold -= weight
            if threshold < 0:
                return mobility
        return Mobility.WHEELCHAIR

    @staticmethod
    def _sample_range(draw: float, minimum: float, maximum: float) -> float:
        return minimum + draw * (maximum - minimum)
