"""Versioned, explicit configuration for synthetic facility knowledge."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

FACILITY_AWARENESS_POLICY_VERSION = "entry-orientation-and-arrival-discovery-v1"
FACILITY_AWARENESS_SOURCE_ID = "ASM-FACILITY-ENTRY-ORIENTATION-V1"
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class FacilityAwarenessConfig(BaseModel):
    """Initial entry guidance plus deterministic discovery on physical arrival."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    initial_known_facility_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=1000)
    discover_co_located_facilities_on_arrival: StrictBool = True
    source_type: Literal["assumption"] = "assumption"
    source_id: Literal["ASM-FACILITY-ENTRY-ORIENTATION-V1"] = FACILITY_AWARENESS_SOURCE_ID

    @field_validator("initial_known_facility_ids")
    @classmethod
    def stable_unique_facility_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STABLE_ID.fullmatch(value) for value in values):
            raise ValueError("initial_known_facility_ids must contain stable facility IDs")
        if len(values) != len(set(values)):
            raise ValueError("initial_known_facility_ids must be unique")
        return tuple(sorted(values))


def canonical_facility_awareness_config(document: Any) -> dict[str, Any]:
    """Validate and return the stable JSON form used by Scenarios and Checkpoints."""
    return FacilityAwarenessConfig.model_validate(document).model_dump(mode="json")
