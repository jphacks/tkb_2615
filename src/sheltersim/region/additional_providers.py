"""Configured municipal/hazard feeds and dated JMA forecasts with bounded provenance."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from threading import RLock
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import Field, model_validator

from sheltersim.region.models import (
    DataSource,
    HazardObservation,
    MunicipalPlanObservation,
    RegionalProviderStatus,
    RegionModel,
    RegionResearchRequest,
    WeatherObservation,
)
from sheltersim.region.providers import RegionDatasetError, RegionProviderError

_MAX_JSON_BYTES = 2_000_000
_MAX_SOURCE_RECORDS = 5_000
_TIMEOUT_SECONDS = 8
_CACHE_TTL_SECONDS = 6 * 60 * 60
_MAX_STALE_SECONDS = 24 * 60 * 60
_CACHE_MAX_ENTRIES = 64
_JMA_HOST = "www.jma.go.jp"
_JMA_SOURCE_PREFIX = "SRC-JMA-FORECAST-"
_CREDENTIAL_QUERY_KEYS = {
    "key",
    "api_key",
    "apikey",
    "x_api_key",
    "access_key",
    "appid",
    "token",
    "access_token",
    "auth",
    "authorization",
    "password",
    "secret",
    "client_secret",
    "signature",
    "sig",
    "subscription_key",
}
_JSON_CACHE: OrderedDict[str, tuple[float, "JsonSnapshot"]] = OrderedDict()
_JSON_CACHE_LOCK = RLock()


class ConfiguredRegionalSource(RegionModel):
    """Trusted deployment configuration for one public JSON source, never request input."""

    data_kind: Literal["municipal_plan", "hazard"]
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    title: str = Field(min_length=1, max_length=240)
    publisher: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2048)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=20)
    geographic_scope: str = Field(min_length=1, max_length=240)
    source_kind: Literal["official", "municipal"]
    license_note: str = Field(min_length=1, max_length=500)
    published_on: date | None = None
    records_path: tuple[str, ...] = Field(default=("records",), max_length=8)
    fields: dict[str, str] = Field(min_length=5, max_length=16)
    municipality_code_field: str | None = None
    municipality_name_field: str | None = None
    disaster_field: str | None = None
    season_field: str | None = None
    valid_from_field: str | None = None
    valid_through_field: str | None = None
    max_age_days: int = Field(default=3_650, ge=0, le=36_500)

    @model_validator(mode="after")
    def validate_public_feed(self) -> ConfiguredRegionalSource:
        parsed = urlsplit(self.url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        _validate_public_host(host)
        allowed = {item.casefold().rstrip(".") for item in self.allowed_hosts}
        if host not in allowed:
            raise ValueError(
                "configured regional source host must appear in its exact host allowlist"
            )
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("configured regional source must be a credential-free HTTPS URL")
        if _contains_embedded_credentials(self.url):
            raise ValueError("configured regional source URLs cannot embed credentials")
        required = {"metric", "value", "unit", "geographic_scope", "period", "applicability"}
        if self.data_kind == "hazard":
            required.add("hazard")
        if not required <= set(self.fields):
            raise ValueError(
                "configured regional source fields are missing canonical observation columns"
            )
        if any(not path or len(path) > 120 for path in (*self.records_path, *self.fields.values())):
            raise ValueError(
                "configured source record selectors must be bounded non-empty field names"
            )
        for optional_field in (
            self.municipality_code_field,
            self.municipality_name_field,
            self.disaster_field,
            self.season_field,
            self.valid_from_field,
            self.valid_through_field,
        ):
            if optional_field is not None and (not optional_field or len(optional_field) > 120):
                raise ValueError("configured source applicability field names must be bounded")
        return self


class ConfiguredRegionalProviderResult(RegionModel):
    sources: tuple[DataSource, ...] = ()
    municipal_plan_observations: tuple[MunicipalPlanObservation, ...] = ()
    hazard_observations: tuple[HazardObservation, ...] = ()
    statuses: tuple[RegionalProviderStatus, ...] = ()
    warnings: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


@dataclass(frozen=True)
class JsonSnapshot:
    document: Any
    content_sha256: str
    retrieved_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    stale_cache_fallback: bool = False


class _SingleHostRedirect(HTTPRedirectHandler):
    def __init__(self, hostname: str) -> None:
        super().__init__()
        self.hostname = hostname

    def redirect_request(
        self, request: Request, response: Any, code: int, message: str, headers: Any, url: str
    ) -> Request | None:
        original = urlsplit(request.full_url)
        target = urlsplit(url)
        if (
            original.scheme != "https"
            or target.scheme != "https"
            or (target.hostname or "").casefold().rstrip(".") != self.hostname
            or target.port not in (None, 443)
        ):
            raise RegionProviderError("public-data endpoint returned a disallowed redirect")
        return super().redirect_request(request, response, code, message, headers, url)


def _validate_public_host(host: str) -> None:
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("regional source hosts must be public DNS names")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("regional source host cannot be a private or reserved IP address")
    raise ValueError("regional source configuration must use a DNS name, not an IP address")


def _contains_embedded_credentials(url: str) -> bool:
    return any(
        key.casefold().replace("-", "_") in _CREDENTIAL_QUERY_KEYS
        or key.casefold().replace("-", "_").endswith(("_token", "_secret", "_key"))
        for key, _ in parse_qsl(urlsplit(url).query)
    )


def _download_json(url: str, allowed_hosts: tuple[str, ...]) -> JsonSnapshot:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = {item.casefold().rstrip(".") for item in allowed_hosts}
    _validate_public_host(host)
    if (
        parsed.scheme != "https"
        or host not in allowed
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise RegionProviderError("public-data URL is outside the configured HTTPS host allowlist")
    if _contains_embedded_credentials(url):
        raise RegionProviderError("public-data URL cannot contain embedded credentials")
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    now_monotonic = time.monotonic()
    stale: JsonSnapshot | None = None
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(cache_key)
        if cached is not None:
            expires_at, snapshot = cached
            if expires_at > now_monotonic:
                _JSON_CACHE.move_to_end(cache_key)
                return snapshot
            stale = snapshot
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "ShelterSim/1"})
        opener = build_opener(_SingleHostRedirect(host))
        with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RegionProviderError(f"public-data endpoint returned HTTP {response.status}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > _MAX_JSON_BYTES:
                        raise RegionProviderError(
                            "public-data response exceeded the 2 MB safety limit"
                        )
                except ValueError as exc:
                    raise RegionProviderError(
                        "public-data endpoint returned an invalid content length"
                    ) from exc
            body = response.read(_MAX_JSON_BYTES + 1)
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
    except HTTPError as exc:
        failure: RegionProviderError = RegionProviderError(
            f"public-data endpoint returned HTTP {exc.code}"
        )
    except (URLError, TimeoutError, socket.timeout, OSError):
        failure = RegionProviderError("public-data endpoint could not be reached")
    else:
        if len(body) > _MAX_JSON_BYTES:
            failure = RegionProviderError("public-data response exceeded the 2 MB safety limit")
        else:
            try:
                document = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                failure = RegionDatasetError("public-data endpoint returned invalid JSON")
            else:
                snapshot = JsonSnapshot(
                    document=document,
                    content_sha256=hashlib.sha256(body).hexdigest(),
                    retrieved_at=datetime.now(UTC),
                    etag=etag,
                    last_modified=last_modified,
                )
                with _JSON_CACHE_LOCK:
                    _JSON_CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, snapshot)
                    _JSON_CACHE.move_to_end(cache_key)
                    while len(_JSON_CACHE) > _CACHE_MAX_ENTRIES:
                        _JSON_CACHE.popitem(last=False)
                return snapshot
    if (
        stale is not None
        and (datetime.now(UTC) - stale.retrieved_at).total_seconds() <= _MAX_STALE_SECONDS
    ):
        return replace(stale, stale_cache_fallback=True)
    raise failure


def _records(document: Any, path: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    node = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise RegionDatasetError(
                "configured regional JSON did not contain the selected records path"
            )
        node = node[part]
    if not isinstance(node, list) or len(node) > _MAX_SOURCE_RECORDS:
        raise RegionDatasetError("configured regional JSON records were not a bounded array")
    if any(not isinstance(item, dict) for item in node):
        raise RegionDatasetError("configured regional JSON records must be objects")
    return tuple(node)


def _value(record: dict[str, Any], field: str | None) -> Any:
    return record.get(field) if field else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


class ConfiguredRegionalDataProvider:
    """Fetch deployment-configured public municipal/hazard feeds; no source means unknown."""

    def __init__(self, sources: tuple[ConfiguredRegionalSource, ...] = ()) -> None:
        if len(sources) > 32:
            raise ValueError("at most 32 regional public-data sources can be configured")
        if len({item.source_id for item in sources}) != len(sources):
            raise ValueError("configured regional source IDs must be unique")
        if any(
            item.source_id.startswith(("SRC-GSI", "SRC-ESTAT", "SRC-MLIT", _JMA_SOURCE_PREFIX))
            for item in sources
        ):
            raise ValueError(
                "configured regional sources cannot reuse reserved built-in provider IDs"
            )
        self.sources = tuple(sources)

    @classmethod
    def from_environment(cls) -> ConfiguredRegionalDataProvider:
        raw = os.environ.get("SHELTERSIM_REGION_DATA_SOURCES_JSON", "").strip()
        if not raw:
            return cls()
        if len(raw) > 100_000:
            raise ValueError("regional source configuration exceeded its 100 KB limit")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("regional source configuration must be valid JSON") from exc
        if not isinstance(document, list) or len(document) > 32:
            raise ValueError(
                "regional source configuration must be a list of at most 32 source entries"
            )
        return cls(tuple(ConfiguredRegionalSource.model_validate(item) for item in document))

    def acquire(self, request: RegionResearchRequest) -> ConfiguredRegionalProviderResult:
        sources: list[DataSource] = []
        plans: list[MunicipalPlanObservation] = []
        hazards: list[HazardObservation] = []
        statuses: list[RegionalProviderStatus] = []
        warnings: list[str] = []
        unknowns: list[str] = []
        kinds_seen = {item.data_kind for item in self.sources}
        for kind in ("municipal_plan", "hazard"):
            if kind not in kinds_seen:
                statuses.append(
                    RegionalProviderStatus(
                        data_kind=kind,
                        status="not_configured",
                        applicability=(
                            "No deployment-configured endpoint for the requested data class."
                        ),
                        message="自治体計画・災害数値の取得Providerが構成されていません。",
                    )
                )
                unknowns.append(
                    "自治体の地域防災計画Providerが未構成です。"
                    if kind == "municipal_plan"
                    else "数値ハザードデータProviderが未構成です。"
                )
        for config in self.sources:
            now = datetime.now(UTC)
            try:
                snapshot = _download_json(config.url, config.allowed_hosts)
                rows = _records(snapshot.document, config.records_path)
                sources.append(
                    DataSource(
                        id=config.source_id,
                        title=config.title,
                        publisher=config.publisher,
                        url=config.url,
                        source_kind=config.source_kind,
                        accessed_at=now,
                        retrieved_at=snapshot.retrieved_at,
                        published_on=config.published_on,
                        geographic_scope=config.geographic_scope,
                        license_note=config.license_note,
                        freshness="historical" if snapshot.stale_cache_fallback else "unknown",
                        content_sha256=snapshot.content_sha256,
                        etag=snapshot.etag,
                        last_modified=snapshot.last_modified,
                    )
                )
            except (RegionProviderError, RegionDatasetError, ValueError) as exc:
                sources.append(
                    DataSource(
                        id=config.source_id,
                        title=config.title,
                        publisher=config.publisher,
                        url=config.url,
                        source_kind=config.source_kind,
                        accessed_at=now,
                        published_on=config.published_on,
                        geographic_scope=config.geographic_scope,
                        license_note=config.license_note,
                        freshness="unknown",
                    )
                )
                statuses.append(
                    RegionalProviderStatus(
                        data_kind=config.data_kind,
                        status="unavailable",
                        source_id=config.source_id,
                        applicability=config.geographic_scope,
                        message=str(exc)[:1_000],
                    )
                )
                unknowns.append(f"{config.title}の取得結果を適用できませんでした。")
                continue

            selected_rows: list[tuple[int, dict[str, Any], str, date | None, date | None, str]] = []
            requested_name = request.selected_municipality_name or request.municipality_query
            for row_number, record in enumerate(rows, start=1):
                code = _text(_value(record, config.municipality_code_field))
                name = _text(_value(record, config.municipality_name_field))
                if request.municipality_code and code and code != request.municipality_code:
                    continue
                if (
                    request.municipality_code
                    and not code
                    and name
                    and _normalize(name) != _normalize(requested_name)
                ):
                    continue
                if (
                    not request.municipality_code
                    and name
                    and _normalize(name) != _normalize(requested_name)
                ):
                    continue
                if (
                    name is None
                    and code is None
                    and _normalize(config.geographic_scope) != _normalize(requested_name)
                ):
                    continue
                disaster_value = _text(_value(record, config.disaster_field))
                if disaster_value and _normalize(disaster_value) not in _normalize(
                    request.disaster
                ):
                    continue
                season_value = _text(_value(record, config.season_field))
                if (
                    request.season
                    and season_value
                    and _normalize(season_value) != _normalize(request.season)
                ):
                    continue
                valid_from = _as_date(_value(record, config.valid_from_field))
                valid_through = _as_date(_value(record, config.valid_through_field))
                applicability_bits = [f"geographic_scope={name or code or config.geographic_scope}"]
                status: str = "unknown"
                municipality_verified = bool(
                    config.municipality_code_field
                    and request.municipality_code
                    and code == request.municipality_code
                )
                if municipality_verified:
                    applicability_bits.append("municipality_code=matched")
                elif name and _normalize(name) == _normalize(requested_name):
                    applicability_bits.append("municipality_name=exact_match_without_verified_code")
                else:
                    applicability_bits.append("municipality=configuration_scope_only")
                if request.season:
                    applicability_bits.append(
                        "season=matched"
                        if config.season_field and season_value
                        else "season=unknown"
                    )
                date_verified = False
                if request.target_date:
                    if valid_from is None and valid_through is None:
                        applicability_bits.append("target_date=unknown")
                    elif (valid_from is None or valid_from <= request.target_date) and (
                        valid_through is None or request.target_date <= valid_through
                    ):
                        date_verified = True
                        applicability_bits.append("target_date=within_source_validity")
                    elif valid_through and request.target_date > valid_through:
                        status = "historical"
                        applicability_bits.append("target_date=after_source_validity")
                    else:
                        applicability_bits.append("target_date=before_source_validity")
                elif valid_from is not None or valid_through is not None:
                    applicability_bits.append("target_date=not_selected")
                else:
                    applicability_bits.append("source_validity=not_recorded")
                disaster_verified = bool(config.disaster_field and disaster_value)
                season_verified = not request.season or bool(config.season_field and season_value)
                if disaster_verified:
                    applicability_bits.append("disaster=matched")
                elif request.disaster:
                    applicability_bits.append("disaster=unknown")
                if snapshot.stale_cache_fallback:
                    status = "unknown"
                    applicability_bits.append("retrieval=stale_cache")
                elif (
                    config.published_on
                    and (datetime.now(UTC).date() - config.published_on).days > config.max_age_days
                ):
                    status = "historical"
                    applicability_bits.append("publication=max_age_exceeded")
                elif status != "historical" and (
                    municipality_verified
                    and disaster_verified
                    and season_verified
                    and date_verified
                ):
                    status = "observed"
                selected_rows.append(
                    (
                        row_number,
                        record,
                        status,
                        valid_from,
                        valid_through,
                        "; ".join(applicability_bits),
                    )
                )

            local_count = 0
            for (
                row_number,
                record,
                status,
                valid_from,
                valid_through,
                row_applicability,
            ) in selected_rows:
                mapped: dict[str, Any] = {}
                for canonical, field in config.fields.items():
                    mapped[canonical] = record.get(field)
                metric = _text(mapped.get("metric"))
                unit = _text(mapped.get("unit"))
                period = _text(mapped.get("period"))
                applicability = _text(mapped.get("applicability")) or row_applicability
                geographic_scope = _text(mapped.get("geographic_scope"))
                value = _as_number(mapped.get("value"))
                if not metric or not unit or not period or not geographic_scope or value is None:
                    warnings.append(
                        f"{config.title}の{row_number}行目は必須の数値項目・単位・適用範囲を満たしません。"
                    )
                    continue
                effective_status = status
                try:
                    if config.data_kind == "municipal_plan":
                        observation = MunicipalPlanObservation(
                            row_number=row_number,
                            metric=metric,
                            value=value,
                            unit=unit,
                            geographic_scope=geographic_scope,
                            period=period,
                            applicability=applicability,
                            municipality_code=request.municipality_code,
                            content_sha256=snapshot.content_sha256,
                            source_id=config.source_id,
                            status=effective_status,
                            valid_from=valid_from,
                            valid_through=valid_through,
                            disaster_tags=(_text(_value(record, config.disaster_field)),)
                            if _text(_value(record, config.disaster_field))
                            else (),
                            season_tags=(_text(_value(record, config.season_field)),)
                            if _text(_value(record, config.season_field))
                            else (),
                        )
                    else:
                        hazard = _text(mapped.get("hazard"))
                        if not hazard:
                            warnings.append(
                                f"{config.title}の{row_number}行目に災害分類がありません。"
                            )
                            continue
                        observation = HazardObservation(
                            hazard=hazard,
                            metric=metric,
                            value=value,
                            unit=unit,
                            geographic_scope=geographic_scope,
                            period=period,
                            applicability=applicability,
                            municipality_code=request.municipality_code,
                            content_sha256=snapshot.content_sha256,
                            source_id=config.source_id,
                            status=effective_status,
                            valid_from=valid_from,
                            valid_through=valid_through,
                        )
                except (TypeError, ValueError) as exc:
                    warnings.append(
                        f"{config.title}の{row_number}行目は観測スキーマを満たしません: "
                        f"{str(exc)[:240]}"
                    )
                    continue
                if config.data_kind == "municipal_plan":
                    plans.append(observation)
                else:
                    hazards.append(observation)
                local_count += 1
            result_status = (
                "stale"
                if snapshot.stale_cache_fallback
                else "available"
                if local_count
                else "out_of_scope"
            )
            statuses.append(
                RegionalProviderStatus(
                    data_kind=config.data_kind,
                    status=result_status,
                    source_id=config.source_id,
                    record_count=local_count,
                    applicability=(
                        f"municipality={requested_name}; disaster={request.disaster}; "
                        f"season={request.season or 'not specified'}; "
                        f"target_date={request.target_date or 'not specified'}"
                    ),
                    message=(
                        "古いキャッシュのため観測値は適用不可です。"
                        if snapshot.stale_cache_fallback
                        else (
                            "構成Providerから一致する数値レコードを取得しました。"
                            "適用条件はレコードごとに確認してください。"
                        )
                        if local_count
                        else "取得はできましたが、自治体・災害・期間に一致する行がありません。"
                    ),
                )
            )
            if snapshot.stale_cache_fallback:
                warnings.append(
                    f"{config.title}の再取得に失敗し、24時間以内の古いキャッシュを返しました。"
                )
            elif not local_count:
                unknowns.append(
                    f"{config.title}には入力地域・災害・期間に適用できるレコードがありません。"
                )

        plans = list(_mark_conflicts(plans))
        hazards = list(_mark_conflicts(hazards))
        if any(item.status == "conflicting" for item in plans):
            statuses.append(
                RegionalProviderStatus(
                    data_kind="municipal_plan",
                    status="conflicting",
                    applicability=(
                        "Same metric/scope/period produced different values in configured sources."
                    ),
                    message="同じ適用範囲の複数の自治体資料に矛盾する数値があるため、自動採用しません。",
                )
            )
        if any(item.status == "conflicting" for item in hazards):
            statuses.append(
                RegionalProviderStatus(
                    data_kind="hazard",
                    status="conflicting",
                    applicability=(
                        "Same metric/scope/period produced different values in configured sources."
                    ),
                    message="同じ適用範囲の複数のハザード資料に矛盾する数値があるため、自動採用しません。",
                )
            )
        return ConfiguredRegionalProviderResult(
            sources=tuple(sources),
            municipal_plan_observations=tuple(plans),
            hazard_observations=tuple(hazards),
            statuses=tuple(statuses),
            warnings=tuple(warnings),
            unknowns=tuple(unknowns),
        )


def _mark_conflicts(items: list[Any]) -> tuple[Any, ...]:
    def key_for(item: Any) -> tuple[Any, ...]:
        # Every observed row has already passed the current request's municipality,
        # disaster, season, and validity checks. Group on normalized measurement
        # dimensions so capitalization/spacing differences in source labels cannot
        # hide conflicting values for that same selected applicability.
        return (
            _normalize(item.metric),
            _normalize(item.unit),
            _normalize(item.geographic_scope),
            _normalize(item.period),
            _normalize(getattr(item, "hazard", "") or ""),
            item.municipality_code,
        )

    grouped: dict[tuple[Any, ...], set[float]] = {}
    for item in items:
        if item.status == "observed":
            grouped.setdefault(key_for(item), set()).add(item.value)
    conflicting = {key for key, values in grouped.items() if len(values) > 1}
    if not conflicting:
        return tuple(items)
    return tuple(
        item.model_copy(update={"status": "conflicting"})
        if item.status == "observed" and key_for(item) in conflicting
        else item
        for item in items
    )


class JmaForecastProvider:
    """Read dated seven-day JMA forecasts only when an operator selected a forecast area."""

    def fetch(
        self, request: RegionResearchRequest
    ) -> tuple[DataSource | None, tuple[WeatherObservation, ...], RegionalProviderStatus]:
        area_code = request.weather_area_code
        if not area_code:
            return (
                None,
                (),
                RegionalProviderStatus(
                    data_kind="weather",
                    status="not_configured",
                    applicability=(
                        "A six-digit JMA forecast area was not selected for this municipality."
                    ),
                    message="気象庁予報区域コードが選択されていないため、予報を地域条件へ結び付けていません。",
                ),
            )
        url = f"https://{_JMA_HOST}/bosai/forecast/data/forecast/{area_code}.json"
        source_id = _JMA_SOURCE_PREFIX + area_code
        now = datetime.now(UTC)
        try:
            snapshot = _download_json(url, (_JMA_HOST,))
            document = snapshot.document
            if not isinstance(document, list) or not document or len(document) > 4:
                raise RegionDatasetError("JMA forecast JSON top-level shape was not recognized")
            observations: list[WeatherObservation] = []
            publication_times: list[datetime] = []
            valid_dates: set[date] = set()
            for report in document:
                if not isinstance(report, dict) or not isinstance(report.get("timeSeries"), list):
                    continue
                published = _parse_aware_datetime(report.get("reportDatetime"))
                if published:
                    publication_times.append(published)
                for series in report["timeSeries"][:8]:
                    if not isinstance(series, dict):
                        continue
                    defines = series.get("timeDefines")
                    areas = series.get("areas")
                    if not isinstance(defines, list) or not isinstance(areas, list):
                        continue
                    for area in areas[:100]:
                        if not isinstance(area, dict) or not isinstance(area.get("area"), dict):
                            continue
                        metadata = area["area"]
                        nested_code = str(metadata.get("code", ""))
                        if re.fullmatch(r"\d{6}", nested_code):
                            forecast_area_code = nested_code
                            station_code = None
                        elif re.fullmatch(r"\d{5}", nested_code):
                            forecast_area_code = None
                            station_code = nested_code
                        else:
                            continue
                        label = _text(metadata.get("name")) or nested_code
                        for key, metric, unit in (
                            ("weathers", "weather_description", "description"),
                            ("winds", "wind_description", "description"),
                            ("temps", "temperature_c", "°C"),
                            ("pops", "precipitation_probability_pct", "%"),
                        ):
                            values = area.get(key)
                            if not isinstance(values, list):
                                continue
                            for index, (time_value, value) in enumerate(zip(defines, values)):
                                valid_at = _parse_aware_datetime(time_value)
                                if valid_at is None:
                                    continue
                                valid_dates.add(valid_at.date())
                                if request.target_date and valid_at.date() != request.target_date:
                                    continue
                                parsed_value: str | float | None
                                if metric in {"temperature_c", "precipitation_probability_pct"}:
                                    parsed_value = _as_number(value)
                                    if parsed_value is None:
                                        continue
                                else:
                                    parsed_value = _text(value)
                                    if parsed_value is None:
                                        continue
                                stale = (
                                    snapshot.stale_cache_fallback
                                    or not publication_times
                                    or now - max(publication_times) > timedelta(hours=24)
                                )
                                observations.append(
                                    WeatherObservation(
                                        area_code=area_code,
                                        forecast_area_code=forecast_area_code,
                                        station_code=station_code,
                                        area_label=label,
                                        metric=metric,
                                        value=parsed_value,
                                        unit=unit,
                                        valid_at=valid_at,
                                        published_at=max(publication_times)
                                        if publication_times
                                        else now,
                                        source_id=source_id,
                                        content_sha256=snapshot.content_sha256,
                                        status="stale_forecast" if stale else "forecast",
                                        applicability="unknown",
                                    )
                                )
            if not publication_times:
                raise RegionDatasetError(
                    "JMA forecast JSON did not include a publication timestamp"
                )
            source = DataSource(
                id=source_id,
                title="Japan Meteorological Agency area forecast",
                publisher="Japan Meteorological Agency",
                url=url,
                source_kind="official",
                accessed_at=now,
                retrieved_at=snapshot.retrieved_at,
                geographic_scope=f"JMA forecast area {area_code}",
                license_note=(
                    "Official forecast description only. It is not a shelter indoor temperature, "
                    "hazard impact, or long-range climate projection."
                ),
                freshness="historical" if snapshot.stale_cache_fallback else "unknown",
                content_sha256=snapshot.content_sha256,
                etag=snapshot.etag,
                last_modified=snapshot.last_modified,
            )
            if request.target_date and request.target_date not in valid_dates:
                return (
                    source,
                    (),
                    RegionalProviderStatus(
                        data_kind="weather",
                        status="out_of_scope",
                        source_id=source_id,
                        applicability=(
                            "JMA forecast valid dates: "
                            f"{', '.join(sorted(item.isoformat() for item in valid_dates)) or 'none'}"
                        ),
                        message="指定日の気象庁予報は提供範囲外です。長期の気象条件は不明として扱います。",
                    ),
                )
            status = (
                "stale"
                if snapshot.stale_cache_fallback
                or any(item.status == "stale_forecast" for item in observations)
                else "available"
                if observations
                else "out_of_scope"
            )
            return (
                source,
                tuple(observations),
                RegionalProviderStatus(
                    data_kind="weather",
                    status=status,
                    source_id=source_id,
                    record_count=len(observations),
                    applicability=(
                        f"JMA forecast-file area={area_code}; nested forecast areas are "
                        "returned with their own codes; "
                        f"target_date={request.target_date or 'not selected'}; "
                        f"valid_dates={', '.join(sorted(item.isoformat() for item in valid_dates))}"
                    ),
                    message=(
                        "気象庁予報を候補として取得しました。自治体と細分区域を結ぶ選択・対応が未確定のため全観測をunknownのまま返します。予報期間外・屋内条件・将来の災害影響には適用しません。"
                        if observations
                        else "気象庁予報区域に一致する日時別値がありません。"
                    ),
                ),
            )
        except (RegionProviderError, RegionDatasetError, ValueError) as exc:
            source = DataSource(
                id=source_id,
                title="Japan Meteorological Agency area forecast",
                publisher="Japan Meteorological Agency",
                url=url,
                source_kind="official",
                accessed_at=now,
                geographic_scope=f"JMA forecast area {area_code}",
                license_note=(
                    "Configured public endpoint; an unavailable response is not evidence of zero "
                    "weather risk."
                ),
                freshness="unknown",
            )
            return (
                source,
                (),
                RegionalProviderStatus(
                    data_kind="weather",
                    status="unavailable",
                    source_id=source_id,
                    applicability=(
                        f"JMA area={area_code}; target_date={request.target_date or 'not selected'}"
                    ),
                    message=str(exc)[:1_000],
                ),
            )


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
