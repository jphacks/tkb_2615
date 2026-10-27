"""Evidence contracts for municipality, hazard and facility research.

These models deliberately keep retrieved observations, source links and assumptions
separate. They are application inputs and do not alter the versioned WorldState.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ACCESSIBLE_ROUTE_MIN_WIDTH_M = 1.8


class RegionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegionResearchRequest(RegionModel):
    municipality_query: str = Field(min_length=1, max_length=160)
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    disaster: str = Field(min_length=1, max_length=80)
    season: str | None = Field(default=None, max_length=40)
    estat_stats_data_id: str | None = Field(default=None, max_length=64)
    estat_area_code: str | None = Field(default=None, max_length=32)
    estat_period_code: str | None = Field(default=None, max_length=32)
    estat_category_codes: dict[str, str] = Field(default_factory=dict)
    selected_municipality_name: str | None = Field(default=None, max_length=160)
    target_date: date | None = None
    weather_area_code: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="Selected six-digit JMA forecast-file code; nested area applicability remains unknown without a separate mapping.",
    )

    @model_validator(mode="after")
    def selected_statistics_are_consistent(self) -> RegionResearchRequest:
        if (
            self.estat_period_code or self.estat_category_codes or self.estat_area_code
        ) and not self.estat_stats_data_id:
            raise ValueError(
                "e-Stat area, period, and category selections require a selected table"
            )
        if any(
            not key or len(key) > 32 or not value or len(value) > 64
            for key, value in self.estat_category_codes.items()
        ):
            raise ValueError("e-Stat category selections require bounded dimension and class codes")
        return self


class DataSource(RegionModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    title: str = Field(min_length=1, max_length=240)
    publisher: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2048)
    source_kind: Literal["official", "municipal", "community", "operator", "assumption"]
    accessed_at: datetime
    published_on: date | None = None
    geographic_scope: str | None = Field(default=None, max_length=240)
    license_note: str | None = Field(default=None, max_length=500)
    freshness: Literal["current", "historical", "unknown", "not_applicable"] = "unknown"
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retrieved_at: datetime | None = None
    etag: str | None = Field(default=None, max_length=256)
    last_modified: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_https(self) -> DataSource:
        if not self.url.startswith("https://"):
            raise ValueError("source URL must use HTTPS")
        if self.accessed_at.tzinfo is None:
            raise ValueError("accessed_at must include a timezone")
        if self.retrieved_at is not None and self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
        return self


class MunicipalityCandidate(RegionModel):
    name: str = Field(min_length=1, max_length=240)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    source_id: str
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    match_kind: Literal["exact", "candidate"] = "candidate"


class StatisticalTableCandidate(RegionModel):
    table_id: str
    title: str
    organization: str | None = None
    survey_date: str | None = None
    publication_date: str | None = None
    source_id: str
    data_kind: Literal["catalog_candidate"] = "catalog_candidate"


class StatisticalChoice(RegionModel):
    code: str
    label: str


class StatisticalDimension(RegionModel):
    dimension_id: str
    label: str
    choices: tuple[StatisticalChoice, ...] = ()


class StatisticalTableMetadata(RegionModel):
    """Explicit table selectors; codes are suggestions until chosen by an operator."""

    table_id: str
    title: str
    source_id: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    areas: tuple[StatisticalChoice, ...] = ()
    periods: tuple[StatisticalChoice, ...] = ()
    dimensions: tuple[StatisticalDimension, ...] = ()
    units: tuple[str, ...] = ()
    geographic_scope: str | None = None


class StatisticalObservation(RegionModel):
    table_id: str
    title: str
    area_code: str
    area_label: str | None = None
    period_code: str | None = None
    category_codes: dict[str, str] = Field(default_factory=dict)
    category_labels: dict[str, str] = Field(default_factory=dict)
    value: float = Field(allow_inf_nan=False)
    unit: str | None = None
    measure_label: str | None = None
    denominator_label: str | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_id: str
    status: Literal["observed"] = "observed"


class MunicipalPlanObservation(RegionModel):
    """Numeric fact transcribed from a cited municipal plan, pending source review."""

    row_number: int = Field(ge=1)
    metric: str = Field(min_length=1, max_length=160)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=80)
    geographic_scope: str = Field(min_length=1, max_length=240)
    period: str = Field(min_length=1, max_length=120)
    applicability: str = Field(min_length=1, max_length=500)
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str
    status: Literal[
        "observed",
        "historical",
        "unknown",
        "conflicting",
        "operator_mapped_pending_source_review",
    ] = "operator_mapped_pending_source_review"
    valid_from: date | None = None
    valid_through: date | None = None
    disaster_tags: tuple[str, ...] = ()
    season_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def observation_period_is_ordered(self) -> MunicipalPlanObservation:
        if self.valid_from and self.valid_through and self.valid_through < self.valid_from:
            raise ValueError("municipal plan observation validity end must follow its start")
        return self


class HazardObservation(RegionModel):
    """Numeric hazard fact returned by an explicitly configured source adapter."""

    hazard: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=160)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=80)
    geographic_scope: str = Field(min_length=1, max_length=240)
    period: str = Field(min_length=1, max_length=120)
    applicability: str = Field(min_length=1, max_length=500)
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str
    status: Literal["observed", "historical", "unknown", "conflicting"] = "unknown"
    valid_from: date | None = None
    valid_through: date | None = None


class WeatherObservation(RegionModel):
    """Dated official observation/forecast; descriptive weather stays categorical."""

    area_code: str = Field(
        pattern=r"^\d{6}$",
        description="Selected JMA forecast-file code used as the source document scope.",
    )
    forecast_area_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    station_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    area_label: str = Field(min_length=1, max_length=160)
    metric: Literal[
        "weather_description", "temperature_c", "precipitation_probability_pct", "wind_description"
    ]
    value: str | float
    unit: str = Field(min_length=1, max_length=40)
    valid_at: datetime
    published_at: datetime
    source_id: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["forecast", "stale_forecast"]
    applicability: Literal["applicable", "out_of_scope", "unknown"] = "unknown"

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> WeatherObservation:
        if self.valid_at.tzinfo is None or self.published_at.tzinfo is None:
            raise ValueError("weather observation timestamps must include a timezone")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("weather observation numeric values must be finite")
        return self


class RegionalProviderStatus(RegionModel):
    data_kind: Literal["municipal_plan", "hazard", "weather"]
    status: Literal[
        "available",
        "partial",
        "not_configured",
        "unavailable",
        "stale",
        "out_of_scope",
        "conflicting",
    ]
    source_id: str | None = None
    record_count: int = Field(default=0, ge=0)
    applicability: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=1_000)


class HazardLayerReference(RegionModel):
    disaster: str
    title: str
    tile_url_template: str
    map_portal_url: str
    source_id: str
    status: Literal["visual_reference_only"] = "visual_reference_only"
    limitation: str


class ShelterCandidate(RegionModel):
    id: str
    name: str
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    capacity: int | None = Field(default=None, ge=0)
    area_m2: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    supported_hazards: tuple[str, ...] | None = None
    source_id: str
    source_kind: Literal["official", "municipal", "historical_national", "operator"]
    freshness: Literal["current", "historical", "unknown"]
    designation_type: Literal["designated_shelter", "emergency_site", "unknown"] = "unknown"
    shelter_subtype: Literal["general", "welfare", "unknown"] = "unknown"
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    municipality_name: str | None = Field(default=None, max_length=240)
    native_record_id: str | None = Field(default=None, max_length=80)
    municipality_update_date: date | None = None
    metadata_source_id: str | None = None
    verified_by_user: bool = False
    notes: tuple[str, ...] = ()


class MunicipalityCodeCandidate(RegionModel):
    municipality_code: str = Field(pattern=r"^\d{5}$")
    municipality_name: str = Field(min_length=1, max_length=240)
    match_kind: Literal["exact_name", "text_suggestion"]


class ShelterRegisterCoverage(RegionModel):
    status: Literal[
        "matched",
        "no_published_records",
        "unresolved_municipality",
        "ambiguous_municipality",
        "result_limit_exceeded",
        "invalid_dataset",
        "unavailable",
    ]
    source_id: str | None = None
    requested_municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    resolved_municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    resolved_municipality_name: str | None = Field(default=None, max_length=240)
    municipality_name_verified: bool = False
    municipality_candidates: tuple[MunicipalityCodeCandidate, ...] = ()
    municipality_candidates_truncated: bool = False
    matched_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    complete: bool = False
    invalid_record_count: int = Field(default=0, ge=0)
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retrieved_at: datetime | None = None
    stale_cache_fallback: bool = False
    municipality_update_metadata_status: Literal["available", "unknown", "unavailable"] = "unknown"


class RegionAssumption(RegionModel):
    id: str
    subject: str
    value: Any = None
    reason: str
    source_id: str


class LLMSearchSourceCandidate(RegionModel):
    """A hosted-search citation that is not an independently fetched observation."""

    title: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=2048)
    cited_text: str = Field(default="", max_length=500)
    accessed_at: datetime
    status: Literal["candidate_not_independently_verified"] = "candidate_not_independently_verified"
    source_kind: Literal["llm_search_candidate"] = "llm_search_candidate"
    license_note: str = "Review the source page, scope, date and reuse terms before relying on it."

    @model_validator(mode="after")
    def official_japanese_domain(self) -> LLMSearchSourceCandidate:
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or not host.endswith((".go.jp", ".lg.jp"))
            or parsed.username
            or parsed.password
        ):
            raise ValueError("LLM search candidate must use a Japanese government HTTPS host")
        if self.accessed_at.tzinfo is None:
            raise ValueError("LLM search candidate time must include a timezone")
        return self


class RegionResearchResult(RegionModel):
    schema_version: Literal[1] = 1
    request: RegionResearchRequest
    researched_at: datetime
    sources: tuple[DataSource, ...] = ()
    municipalities: tuple[MunicipalityCandidate, ...] = ()
    statistical_tables: tuple[StatisticalTableCandidate, ...] = ()
    statistical_table_metadata: StatisticalTableMetadata | None = None
    population_observations: tuple[StatisticalObservation, ...] = ()
    municipal_plan_observations: tuple[MunicipalPlanObservation, ...] = ()
    hazard_observations: tuple[HazardObservation, ...] = ()
    weather_observations: tuple[WeatherObservation, ...] = ()
    provider_statuses: tuple[RegionalProviderStatus, ...] = ()
    hazard_layers: tuple[HazardLayerReference, ...] = ()
    shelters: tuple[ShelterCandidate, ...] = ()
    shelter_register: ShelterRegisterCoverage | None = None
    unknowns: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    assumptions: tuple[RegionAssumption, ...] = ()
    llm_source_candidates: tuple[LLMSearchSourceCandidate, ...] = ()

    @model_validator(mode="after")
    def all_references_resolve(self) -> RegionResearchResult:
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("research results must not contain duplicate source IDs")
        references = [
            *(candidate.source_id for candidate in self.municipalities),
            *(table.source_id for table in self.statistical_tables),
            *(item.source_id for item in self.population_observations),
            *(item.source_id for item in self.municipal_plan_observations),
            *(item.source_id for item in self.hazard_observations),
            *(item.source_id for item in self.weather_observations),
            *(item.source_id for item in self.hazard_layers),
            *(item.source_id for item in self.shelters),
            *(item.source_id for item in self.assumptions),
        ]
        if self.statistical_table_metadata is not None:
            references.append(self.statistical_table_metadata.source_id)
        if self.shelter_register is not None and self.shelter_register.source_id:
            references.append(self.shelter_register.source_id)
        references.extend(
            item.source_id for item in self.provider_statuses if item.source_id is not None
        )
        references.extend(
            item.metadata_source_id for item in self.shelters if item.metadata_source_id is not None
        )
        missing = sorted(set(references) - source_ids)
        if missing:
            raise ValueError(f"research results reference unknown sources: {missing}")
        if self.researched_at.tzinfo is None:
            raise ValueError("researched_at must include a timezone")
        return self
