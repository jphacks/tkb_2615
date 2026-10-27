"""Versioned inputs shared by persistent services and independent UI workers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Finite, Identifier


class UsageLimits(ExtensionModel):
    schema_version: Literal[1] = 1
    max_calls: int = Field(strict=True, gt=0, le=100000)
    max_input_tokens: int = Field(strict=True, gt=0)
    max_output_tokens: int = Field(strict=True, gt=0)
    budget_usd: Finite = Field(strict=True, ge=0)
    max_concurrency: int = Field(default=1, strict=True, ge=1, le=16)


class DraftInput(ExtensionModel):
    schema_version: Literal[1] = 1
    kind: Literal["scenario_input", "scenario", "intervention"] = "scenario_input"
    content: dict[str, Any]
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class AssetBegin(ExtensionModel):
    schema_version: Literal[1] = 1
    media_type: Literal[
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/json",
        "application/geo+json",
    ]
    byte_count: int = Field(strict=True, gt=0, le=15 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    permission_confirmed: Literal[True]
    sensitive_building: bool = True
    license_note: str = Field(default="Operator-provided; local project use only", max_length=500)
    file_name: str | None = Field(default=None, min_length=1, max_length=240)
    source_kind: Literal["operator_upload", "public_reference", "synthetic"] = "operator_upload"
    coordinate_system: Literal["unknown", "raster_pixel", "local_metre", "geographic"] = "unknown"

    @model_validator(mode="after")
    def display_name_only(self) -> AssetBegin:
        if self.file_name is not None and any(
            char in self.file_name for char in ("/", "\\", "\x00")
        ):
            raise ValueError("asset display name must not contain path separators or NUL")
        return self


class AssetChunk(ExtensionModel):
    index: int = Field(strict=True, ge=0, lt=64)
    data_base64: str = Field(min_length=1, max_length=349528)


class HistoryQuery(ExtensionModel):
    schema_version: Literal[1] = 1
    kind: Literal[
        "people",
        "households",
        "events",
        "causal",
        "kpis",
        "conversations",
        "social",
        "staff",
        "actions",
        "transitions",
        "dialogue",
        "care",
        "operations",
    ]
    start: int = Field(default=0, strict=True, ge=0)
    end: int = Field(strict=True, ge=0)
    step: int = Field(default=1, strict=True, ge=1)
    limit: int = Field(default=25, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    agent_id: Identifier | None = None
    household_id: Identifier | None = None
    event_id: Identifier | None = None
    staff_id: Identifier | None = None
    conversation_id: Identifier | None = None
    zone_id: Identifier | None = None
    event_type: Identifier | None = None
    source_id: Identifier | None = None
    status: Identifier | None = None
    text: str | None = Field(default=None, min_length=1, max_length=160)
    include_related: bool = False

    @model_validator(mode="after")
    def bounded_query(self) -> HistoryQuery:
        for selector, allowed in (
            (
                "agent_id",
                {
                    "people",
                    "events",
                    "causal",
                    "social",
                    "actions",
                    "transitions",
                    "dialogue",
                    "care",
                },
            ),
            (
                "household_id",
                {"households", "events", "actions", "transitions", "dialogue", "care"},
            ),
            ("staff_id", {"staff", "actions", "dialogue", "operations"}),
            ("conversation_id", {"conversations"}),
            (
                "event_id",
                {
                    "causal",
                    "events",
                    "social",
                    "staff",
                    "actions",
                    "transitions",
                    "dialogue",
                    "care",
                    "operations",
                },
            ),
        ):
            if getattr(self, selector) is not None and self.kind not in allowed:
                raise ValueError(f"{selector} is not supported for {self.kind} history")
        if self.include_related and self.kind != "causal":
            raise ValueError("include_related applies only to causal history")
        if self.start > self.end:
            raise ValueError("history start must not exceed end")
        if self.kind == "people" and self.agent_id is None:
            raise ValueError("people history requires an agent_id")
        if self.kind == "households" and self.household_id is None:
            raise ValueError("household history requires a household_id")
        if self.kind == "causal" and self.event_id is None:
            raise ValueError("causal history requires an event_id")
        return self


class ComparisonTarget(ExtensionModel):
    run_id: Identifier
    branch_id: Identifier


class ComparisonQuery(ExtensionModel):
    schema_version: Literal[1] = 1
    targets: tuple[ComparisonTarget, ...] = Field(min_length=2, max_length=8)
    minute: int = Field(strict=True, ge=0)


class ComparisonDifferenceQuery(ComparisonQuery):
    limit: int = Field(default=25, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)


class StateDescriptor(ExtensionModel):
    """Installed code declares this; scenario JSON cannot authorize Python code."""

    namespace: Identifier
    implementation_id: Identifier
    implementation_version: Identifier
    schema_version: int = Field(strict=True, ge=1)
    fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source_ids: tuple[Identifier, ...] = ()
    entity_kind: Literal["agent", "staff"] = "agent"
    entity_collection: Identifier | None = None
    policy_domain: Identifier | None = None


class DatasetLink(ExtensionModel):
    model_id: Identifier
    parameter_path: str = Field(pattern=r"^[A-Za-z0-9_]+\.[A-Za-z0-9_.]+$", max_length=128)


class DatasetInput(ExtensionModel):
    source_id: Identifier
    url: str = Field(pattern=r"^https://", max_length=2048)
    title: str = Field(min_length=1, max_length=240)
    unit: str = Field(min_length=1, max_length=80)
    lower: Finite
    upper: Finite
    applicability: dict[str, Any]
    model_parameter_links: tuple[DatasetLink, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def range_is_ordered(self) -> DatasetInput:
        if self.lower > self.upper:
            raise ValueError("dataset lower bound exceeds upper bound")
        import re
        from urllib.parse import parse_qsl, urlsplit

        url = urlsplit(self.url)
        if (
            not url.netloc
            or url.username is not None
            or url.password is not None
            or any(
                re.search(r"key|token|secret|password|appid", key, re.I)
                for key, _ in parse_qsl(url.query)
            )
        ):
            raise ValueError("dataset requires a public source URL without credentials")
        return self


class DivergenceQuery(ExtensionModel):
    schema_version: Literal[1] = 1
    targets: tuple[ComparisonTarget, ...] = Field(min_length=2, max_length=8)
    start: int = Field(default=0, strict=True, ge=0)
    end: int = Field(strict=True, ge=0)
    mode: Literal["kpis", "world", "person"] = "kpis"
    agent_id: Identifier | None = None
    metrics: tuple[Identifier, ...] = Field(default=(), max_length=20)
    scan_limit: int = Field(default=20, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def coherent_window(self) -> DivergenceQuery:
        if self.start > self.end:
            raise ValueError("divergence start exceeds end")
        if self.mode == "person" and self.agent_id is None:
            raise ValueError("person divergence requires agent_id")
        if self.mode != "person" and self.agent_id is not None:
            raise ValueError("agent_id applies only to person divergence")
        if self.mode != "kpis" and self.metrics:
            raise ValueError("metrics apply only to KPI divergence")
        if len({(t.run_id, t.branch_id) for t in self.targets}) != len(self.targets):
            raise ValueError("divergence targets must differ")
        return self
