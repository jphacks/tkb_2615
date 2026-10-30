"""Typed in-memory domain state used by the simulation controller."""

from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


class Activity(StrEnum):
    """Mutually exclusive primary activity for an Agent."""

    IDLE = "idle"
    MOVING = "moving"
    QUEUED = "queued"
    IN_SERVICE = "in_service"
    SEEKING_HELP = "seeking_help"
    SLEEPING = "sleeping"
    RESTING = "resting"


class Mobility(StrEnum):
    """Functional mobility categories, not demographic labels."""

    INDEPENDENT = "independent"
    ASSISTED = "assisted"
    WHEELCHAIR = "wheelchair"


class SupportRequestStatus(StrEnum):
    """Lifecycle state of one Agent request for staff assistance."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class InformationTruthStatus(StrEnum):
    """Simulation-side assessment of an information claim, not Agent knowledge."""

    UNKNOWN = "unknown"
    TRUE = "true"
    FALSE = "false"


class InformationBeliefStatus(StrEnum):
    """A synthetic Agent's bounded knowledge state for one typed claim."""

    UNKNOWN = "unknown"
    HEARD = "heard"
    VERIFIED = "verified"
    REJECTED = "rejected"


_INFORMATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _validate_information_id(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not _INFORMATION_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a stable ID")


@dataclass(frozen=True)
class TypedMessage:
    """Claim metadata; its truth status is hidden from Agents until corrected."""

    id: str
    truth_status: InformationTruthStatus
    source_id: str
    zone_id: str
    time: int
    origin_event_id: str
    origin_agent_id: str

    def __post_init__(self) -> None:
        for name in ("id", "source_id", "zone_id", "origin_event_id", "origin_agent_id"):
            _validate_information_id(name, getattr(self, name))
        object.__setattr__(self, "truth_status", InformationTruthStatus(self.truth_status))
        if not isinstance(self.time, int) or isinstance(self.time, bool) or self.time < 0:
            raise ValueError("message time must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "truth_status": self.truth_status.value,
            "source_id": self.source_id,
            "zone_id": self.zone_id,
            "time": self.time,
            "origin_event_id": self.origin_event_id,
            "origin_agent_id": self.origin_agent_id,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> TypedMessage:
        expected = {
            "id",
            "truth_status",
            "source_id",
            "zone_id",
            "time",
            "origin_event_id",
            "origin_agent_id",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("TypedMessage fields do not match the supported schema")
        return cls(**document)


@dataclass(frozen=True)
class AgentInformationState:
    """Exposure, subjective belief, sharing tendency, and correction record for one claim."""

    message_id: str
    belief: InformationBeliefStatus
    source: str
    last_contact: int
    last_event_id: str
    exposed: bool = True
    belief_strength: float = 0.5
    share_propensity: float = 0.5
    correction_exposed: bool = False
    correction_accepted: bool = False
    ever_misbelieved: bool = False
    ever_forgotten: bool = False
    last_correction_accepted: bool = False

    def __post_init__(self) -> None:
        for name in ("message_id", "source", "last_event_id"):
            _validate_information_id(name, getattr(self, name))
        object.__setattr__(self, "belief", InformationBeliefStatus(self.belief))
        if (
            not isinstance(self.last_contact, int)
            or isinstance(self.last_contact, bool)
            or self.last_contact < 0
        ):
            raise ValueError("last_contact must be a non-negative integer")
        if not isinstance(self.exposed, bool):
            raise ValueError("information exposure state must be boolean")
        if not isinstance(self.correction_exposed, bool) or not isinstance(
            self.correction_accepted, bool
        ):
            raise ValueError("correction exposure and acceptance must be boolean")
        if not isinstance(self.ever_misbelieved, bool) or not isinstance(self.ever_forgotten, bool):
            raise ValueError("historical information flags must be boolean")
        if not isinstance(self.last_correction_accepted, bool):
            raise ValueError("last correction outcome must be boolean")
        if self.correction_accepted and not self.correction_exposed:
            raise ValueError("a correction cannot be accepted before exposure")
        if self.last_correction_accepted and not self.correction_exposed:
            raise ValueError("a correction outcome requires correction exposure")
        validate_unit_interval("belief_strength", self.belief_strength)
        validate_unit_interval("share_propensity", self.share_propensity)
        if not self.exposed:
            raise ValueError("stored information states must identify an exposure")
        if self.belief is InformationBeliefStatus.UNKNOWN and self.share_propensity != 0.0:
            raise ValueError("unknown or forgotten information must have zero share propensity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "belief": self.belief.value,
            "source": self.source,
            "last_contact": self.last_contact,
            "last_event_id": self.last_event_id,
            "exposed": self.exposed,
            "belief_strength": self.belief_strength,
            "share_propensity": self.share_propensity,
            "correction_exposed": self.correction_exposed,
            "correction_accepted": self.correction_accepted,
            "ever_misbelieved": self.ever_misbelieved,
            "ever_forgotten": self.ever_forgotten,
            "last_correction_accepted": self.last_correction_accepted,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> AgentInformationState:
        legacy_fields = {"message_id", "belief", "source", "last_contact", "last_event_id"}
        current_fields = legacy_fields | {
            "exposed",
            "belief_strength",
            "share_propensity",
            "correction_exposed",
            "correction_accepted",
        }
        historical_fields = current_fields | {"ever_misbelieved", "ever_forgotten"}
        extended_fields = historical_fields | {"last_correction_accepted"}
        if not isinstance(document, dict) or set(document) not in (
            legacy_fields,
            current_fields,
            historical_fields,
            extended_fields,
        ):
            raise ValueError("AgentInformationState fields do not match the supported schema")
        values = dict(document)
        if set(document) == legacy_fields:
            belief = InformationBeliefStatus(document["belief"])
            corrected = belief in (
                InformationBeliefStatus.VERIFIED,
                InformationBeliefStatus.REJECTED,
            )
            values.update(
                exposed=True,
                belief_strength=(
                    1.0
                    if belief is InformationBeliefStatus.VERIFIED
                    else 0.0
                    if belief is InformationBeliefStatus.REJECTED
                    else 0.5
                ),
                share_propensity=(0.0 if belief is InformationBeliefStatus.UNKNOWN else 0.5),
                correction_exposed=corrected,
                correction_accepted=corrected,
                ever_misbelieved=False,
                ever_forgotten=belief is InformationBeliefStatus.UNKNOWN,
                last_correction_accepted=corrected,
            )
        elif set(document) == current_fields:
            values.update(
                ever_misbelieved=False,
                ever_forgotten=document["belief"] == InformationBeliefStatus.UNKNOWN.value,
                last_correction_accepted=document["correction_accepted"],
            )
        elif set(document) == historical_fields:
            values["last_correction_accepted"] = document["correction_accepted"]
        return cls(**values)


@dataclass(frozen=True)
class InformationSpreadConfig:
    """Versioned, opt-in information diffusion and inquiry assumptions."""

    enabled: bool = False
    max_contacts_per_agent: int = 2
    minimum_contact_minutes: int = 2
    contact_duration_scale_minutes: int = 5
    base_transmission_probability: float = 0.2
    correction_transmission_multiplier: float = 1.25
    trust_sensitivity: float = 0.5
    family_contact_multiplier: float = 1.5
    adjacent_zone_contact_multiplier: float = 0.5
    query_pressure_per_claim: float = 0.5
    query_pressure_decay_per_minute: float = 0.002
    query_pressure_relief_per_correction: float = 0.5
    query_threshold: float = 0.45
    query_utility_weight: float = 0.8
    correction_acceptance_probability: float = 0.7
    correction_trust_sensitivity: float = 0.2
    correction_source_sensitivity: float = 0.2
    rumor_belief_probability: float = 0.5
    rumor_trust_sensitivity: float = 0.2
    rumor_source_sensitivity: float = 0.2
    default_source_reliability: float = 0.7
    correction_source_reliability_by_id: dict[str, float] = field(default_factory=dict)
    distress_sharing_sensitivity: float = 0.5
    share_propensity_decay_per_minute: float = 0.002
    forgetting_rate_per_minute: float = 0.0
    schema_version: int = 1
    policy_version: str = "sparse-information-contact-v1"
    source_type: str = "assumption"
    source_id: str = "ASM-INFORMATION-SPREAD-V1"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("information_spread enabled must be boolean")
        for name, value, minimum, maximum in (
            ("max_contacts_per_agent", self.max_contacts_per_agent, 0, 16),
            ("minimum_contact_minutes", self.minimum_contact_minutes, 1, 10_080),
            ("contact_duration_scale_minutes", self.contact_duration_scale_minutes, 1, 10_080),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ValueError(
                    f"information_spread {name} must be between {minimum} and {maximum}"
                )
        for name in (
            "base_transmission_probability",
            "correction_transmission_multiplier",
            "trust_sensitivity",
            "family_contact_multiplier",
            "adjacent_zone_contact_multiplier",
            "query_pressure_per_claim",
            "query_pressure_decay_per_minute",
            "query_pressure_relief_per_correction",
            "query_threshold",
            "query_utility_weight",
            "correction_acceptance_probability",
            "correction_trust_sensitivity",
            "correction_source_sensitivity",
            "rumor_belief_probability",
            "rumor_trust_sensitivity",
            "rumor_source_sensitivity",
            "default_source_reliability",
            "distress_sharing_sensitivity",
            "share_propensity_decay_per_minute",
            "forgetting_rate_per_minute",
        ):
            value = getattr(self, name)
            maximum = (
                2.0
                if name
                in {
                    "correction_transmission_multiplier",
                    "family_contact_multiplier",
                    "adjacent_zone_contact_multiplier",
                }
                else 4.0
                if name == "query_utility_weight"
                else 1.0
                if name
                in {
                    "correction_acceptance_probability",
                    "correction_trust_sensitivity",
                    "correction_source_sensitivity",
                    "rumor_belief_probability",
                    "rumor_trust_sensitivity",
                    "rumor_source_sensitivity",
                    "default_source_reliability",
                    "distress_sharing_sensitivity",
                    "share_propensity_decay_per_minute",
                    "forgetting_rate_per_minute",
                }
                else 1.0
            )
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= maximum
            ):
                raise ValueError(
                    f"information_spread {name} must be finite and between 0 and {maximum}"
                )
        if len(self.correction_source_reliability_by_id) > 100 or any(
            not isinstance(source_id, str)
            or not _INFORMATION_ID.fullmatch(source_id)
            or type(value) not in (int, float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
            for source_id, value in self.correction_source_reliability_by_id.items()
        ):
            raise ValueError(
                "correction source reliability must map up to 100 source IDs to [0, 1]"
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
            or self.policy_version != "sparse-information-contact-v1"
        ):
            raise ValueError("unsupported information_spread schema or policy version")
        if self.source_type != "assumption" or self.source_id != "ASM-INFORMATION-SPREAD-V1":
            raise ValueError("information_spread provenance must use ASM-INFORMATION-SPREAD-V1")

    @classmethod
    def from_mapping(cls, document: Any = None) -> InformationSpreadConfig:
        if document is None:
            return cls()
        if not isinstance(document, dict):
            raise ValueError("information_spread model config must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(document) - allowed
        if unknown:
            raise ValueError(f"unknown information_spread settings: {sorted(unknown)}")
        try:
            return cls(**document)
        except TypeError as exc:
            raise ValueError(f"invalid information_spread settings: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_contacts_per_agent": self.max_contacts_per_agent,
            "minimum_contact_minutes": self.minimum_contact_minutes,
            "contact_duration_scale_minutes": self.contact_duration_scale_minutes,
            "base_transmission_probability": self.base_transmission_probability,
            "correction_transmission_multiplier": self.correction_transmission_multiplier,
            "trust_sensitivity": self.trust_sensitivity,
            "family_contact_multiplier": self.family_contact_multiplier,
            "adjacent_zone_contact_multiplier": self.adjacent_zone_contact_multiplier,
            "query_pressure_per_claim": self.query_pressure_per_claim,
            "query_pressure_decay_per_minute": self.query_pressure_decay_per_minute,
            "query_pressure_relief_per_correction": self.query_pressure_relief_per_correction,
            "query_threshold": self.query_threshold,
            "query_utility_weight": self.query_utility_weight,
            "correction_acceptance_probability": self.correction_acceptance_probability,
            "correction_trust_sensitivity": self.correction_trust_sensitivity,
            "correction_source_sensitivity": self.correction_source_sensitivity,
            "rumor_belief_probability": self.rumor_belief_probability,
            "rumor_trust_sensitivity": self.rumor_trust_sensitivity,
            "rumor_source_sensitivity": self.rumor_source_sensitivity,
            "default_source_reliability": self.default_source_reliability,
            "correction_source_reliability_by_id": dict(
                sorted(self.correction_source_reliability_by_id.items())
            ),
            "distress_sharing_sensitivity": self.distress_sharing_sensitivity,
            "share_propensity_decay_per_minute": self.share_propensity_decay_per_minute,
            "forgetting_rate_per_minute": self.forgetting_rate_per_minute,
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "source_type": self.source_type,
            "source_id": self.source_id,
        }

    def provenance(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "policy_version": self.policy_version,
            "values": self.to_dict(),
            "calibration_status": "uncalibrated experimental assumptions",
        }


@dataclass(frozen=True)
class InitialNeedProfile:
    """Generated or manually supplied normalized starting need levels."""

    toilet_urgency: float = 0.0
    hunger: float = 0.0
    fatigue: float = 0.0
    hydration_need: float = 0.0

    def __post_init__(self) -> None:
        _validate_need_levels(self)


@dataclass
class NeedState:
    """Mutable normalized need levels used by simulation policies."""

    toilet_urgency: float = 0.0
    hunger: float = 0.0
    fatigue: float = 0.0
    hydration_need: float = 0.0

    def validate(self) -> None:
        """Reject values outside the declared 0..1 simulation scale."""
        _validate_need_levels(self)


@functools.lru_cache(maxsize=65_536)
def _text_pair_key(first: str, second: str) -> str:
    return json.dumps([first, second], separators=(",", ":"))


def ordered_pair_key(first: Any, second: Any) -> str:
    """The JSON key of an ordered pair of IDs, which a tick makes about 40 times."""
    if type(first) is str and type(second) is str:
        return _text_pair_key(first, second)
    return json.dumps([first, second], separators=(",", ":"))


def validate_unit_interval(name: str, value: float) -> None:
    """Validate a finite normalized state value, including mutable psychology fields."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_need_levels(needs: InitialNeedProfile | NeedState) -> None:
    for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
        value = getattr(needs, name)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise ValueError(f"{name} must be finite and between 0 and 1")


@dataclass(frozen=True)
class AgentProfile:
    """Stable, synthetic or operator-provided Agent profile fields."""

    id: str
    group_id: str | None
    initial_zone_id: str
    mobility: Mobility
    support_need: float
    stress_sensitivity: float
    rng_key: str
    initial_needs: InitialNeedProfile = field(default_factory=InitialNeedProfile)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    """Mutable simulation state for one Agent."""

    profile: AgentProfile
    zone_id: str
    activity: Activity = Activity.IDLE
    target_id: str | None = None
    needs: NeedState = field(default_factory=NeedState)
    stress: float = 0.0
    trust: float = 0.5
    next_decision_at: int = 0
    support_request_id: str | None = None
    support_cooldown_until: int = 0

    @property
    def id(self) -> str:
        """Return the stable profile ID used as the world's mapping key."""
        return self.profile.id

    @property
    def acute_distress(self) -> float:
        """Compatibility name for the single acute state historically called stress."""
        return self.stress

    @acute_distress.setter
    def acute_distress(self, value: float) -> None:
        validate_unit_interval("acute_distress", value)
        self.stress = value

    def __post_init__(self) -> None:
        if isinstance(self.activity, (list, tuple, set, frozenset)):
            actions = tuple(self.activity)
            if len(actions) > 1:
                raise ValueError("an Agent cannot have multiple exclusive activities")
            self.activity = Activity(actions[0]) if actions else Activity.IDLE
        else:
            self.activity = Activity(self.activity)
        validate_unit_interval("stress", self.stress)
        validate_unit_interval("trust", self.trust)
        if (
            not isinstance(self.next_decision_at, int)
            or isinstance(self.next_decision_at, bool)
            or self.next_decision_at < 0
        ):
            raise ValueError("next_decision_at must be a non-negative integer")
        if (
            not isinstance(self.support_cooldown_until, int)
            or isinstance(self.support_cooldown_until, bool)
            or self.support_cooldown_until < 0
        ):
            raise ValueError("support_cooldown_until must be a non-negative integer")


@dataclass
class ZoneState:
    """Simulation-owned attributes for a zone."""

    id: str
    name: str
    capacity: int | None
    display_x: float | None = None
    display_y: float | None = None


@dataclass
class ZoneEdgeState:
    """Traversable connection between two zones."""

    id: str
    from_zone_id: str
    to_zone_id: str
    travel_minutes: int
    closed: bool = False


@dataclass
class FacilityState:
    """Facility capacity and current reservations."""

    id: str
    kind: str
    zone_id: str
    capacity: int
    working_units: int
    active_agent_ids: list[str] = field(default_factory=list)
    queue_agent_ids: list[str] = field(default_factory=list)
    resource_active_agent_ids: list[str] = field(default_factory=list)
    resource_queue_agent_ids: list[str] = field(default_factory=list)


@dataclass
class StaffState:
    """Staff assignment and pending work identifiers."""

    id: str
    zone_id: str
    skills: tuple[str, ...]
    work_queue: list[str] = field(default_factory=list)
    busy_until: int = 0
    active_task_id: str | None = None


@dataclass
class SupportRequestState:
    """Persisted request, assignment and outcome for staff assistance."""

    id: str
    agent_id: str
    requested_minute: int
    timeout_minute: int
    status: SupportRequestStatus = SupportRequestStatus.PENDING
    assigned_staff_id: str | None = None
    task_id: str | None = None
    resolved_minute: int | None = None
    cooldown_until: int = 0
    last_assignment_attempt_minute: int | None = None
    last_assignment_wait_reason_code: str | None = None
    # Where to report writes; see report_writes_to. Not state, so not a field.
    _write_listener: ClassVar[list[Any] | None] = None

    def __post_init__(self) -> None:
        self.status = SupportRequestStatus(self.status)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        listener = self._write_listener
        if listener is not None:
            listener.append(self)

    def report_writes_to(self, listener: list[Any] | None) -> None:
        """Append this request to listener whenever a field is assigned from now on.

        The end-of-tick check does not read settled requests again until one reports
        a write (core/tick_validation.py).
        """
        object.__setattr__(self, "_write_listener", listener)


@dataclass
class ActiveTask:
    """A task already in progress, such as travel or service."""

    id: str
    kind: str
    agent_id: str | None
    resource_id: str | None
    start_minute: int
    due_minute: int
    staff_id: str | None = None
