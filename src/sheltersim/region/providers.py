"""Small, auditable clients for public Japanese regional data endpoints."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import socket
import time
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http.client import IncompleteRead
from threading import RLock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RegionProviderError(RuntimeError):
    """A bounded public-data request failed without exposing credentials or payloads."""


class RegionDatasetError(RegionProviderError):
    """A public endpoint responded, but its CSV content failed schema validation."""


_ALLOWED_HOSTS = frozenset({
    "msearch.gsi.go.jp",
    "api.e-stat.go.jp",
    "hinanmap.gsi.go.jp",
})
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_SHELTER_CSV_BYTES = 32_000_000
_TIMEOUT_SECONDS = 8
_CSV_TOTAL_TIMEOUT_SECONDS = 45
_CACHE_TTL_SECONDS = 6 * 60 * 60
_MAX_STALE_CSV_AGE_SECONDS = 24 * 60 * 60
_CACHE_MAX_ENTRIES = 128
_JSON_CACHE: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_JSON_CACHE_LOCK = RLock()
_GSI_CSV_CACHE: OrderedDict[str, tuple[float, "GsiCsvSnapshot"]] = OrderedDict()
_CSV_CACHE_LOCK = RLock()
_GSI_CSV_FETCH_LOCK = RLock()
_GSI_SHELTER_CSV_URL = (
    "https://hinanmap.gsi.go.jp/hinanjocp/defaultFtpData/csv/mergeFromCity_1.csv"
)
_MAX_MUNICIPAL_SHELTERS = 500
_MAX_MUNICIPALITY_CODE_SUGGESTIONS = 30
_MAX_GSI_CSV_ROWS = 250_000
_GSI_SHELTER_REQUIRED_HEADERS = frozenset(
    {
        "共通ID",
        "都道府県名及び市町村名",
        "施設・場所名",
        "住所",
        "受入対象者",
        "緯度",
        "経度",
    }
)


class _SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, url: str) -> Request | None:
        original = urlsplit(request.full_url)
        target = urlsplit(url)
        if (
            original.scheme != "https"
            or target.scheme != "https"
            or original.hostname != target.hostname
            or target.hostname not in _ALLOWED_HOSTS
        ):
            raise RegionProviderError("public-data endpoint returned a disallowed redirect")
        return super().redirect_request(request, response, code, message, headers, url)


def _get_json(url: str) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise RegionProviderError("public-data URL is outside the HTTPS endpoint allowlist")
    cache_query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() != "appid"
        ]
    )
    cache_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, cache_query, "")
    )
    cache_key = hashlib.sha256(cache_url.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(cache_key)
        if cached is not None:
            expires_at, document = cached
            if expires_at > now:
                _JSON_CACHE.move_to_end(cache_key)
                return deepcopy(document)
            del _JSON_CACHE[cache_key]
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "ShelterSim/1"})
    opener = build_opener(_SameHostRedirect())
    try:
        with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RegionProviderError(f"public-data endpoint returned HTTP {response.status}")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RegionProviderError(f"public-data endpoint returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise RegionProviderError("public-data endpoint could not be reached") from exc
    if len(body) > _MAX_RESPONSE_BYTES:
        raise RegionProviderError("public-data response exceeded the 2 MB safety limit")
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegionProviderError("public-data endpoint returned invalid JSON") from exc
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, document)
        _JSON_CACHE.move_to_end(cache_key)
        while len(_JSON_CACHE) > _CACHE_MAX_ENTRIES:
            _JSON_CACHE.popitem(last=False)
    return deepcopy(document)


@dataclass(frozen=True)
class GsiCsvSnapshot:
    sha256: str
    retrieved_at: datetime
    text: str
    etag: str | None = None
    last_modified: str | None = None
    used_stale_cache: bool = False


def clear_region_provider_cache() -> None:
    """Clear this process's bounded public-response cache, for operator or test use."""
    with _JSON_CACHE_LOCK:
        _JSON_CACHE.clear()
    with _CSV_CACHE_LOCK:
        _GSI_CSV_CACHE.clear()


def _get_gsi_shelter_snapshot(url: str) -> GsiCsvSnapshot:
    # Keep concurrent cache misses from downloading multiple copies of the national file.
    with _GSI_CSV_FETCH_LOCK:
        return _get_gsi_shelter_snapshot_locked(url)


def _get_gsi_shelter_snapshot_locked(url: str) -> GsiCsvSnapshot:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hinanmap.gsi.go.jp"
        or parsed.path != "/hinanjocp/defaultFtpData/csv/mergeFromCity_1.csv"
        or parsed.query
    ):
        raise RegionProviderError("GSI shelter CSV URL is outside the verified endpoint allowlist")
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    stale_snapshot: GsiCsvSnapshot | None = None
    with _CSV_CACHE_LOCK:
        cached = _GSI_CSV_CACHE.get(cache_key)
        if cached is not None:
            expires_at, stale_snapshot = cached
            if expires_at > time.monotonic():
                _GSI_CSV_CACHE.move_to_end(cache_key)
                return stale_snapshot

    error: RegionProviderError | None = None
    try:
        request = Request(url, headers={"Accept": "text/csv", "User-Agent": "ShelterSim/1"})
        opener = build_opener(_SameHostRedirect())
        deadline = time.monotonic() + _CSV_TOTAL_TIMEOUT_SECONDS
        chunks = bytearray()
        with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RegionProviderError(
                    f"public-data endpoint returned HTTP {response.status}"
                )
            content_length = response.headers.get("Content-Length")
            expected_content_length: int | None = None
            if content_length:
                try:
                    expected_content_length = int(content_length)
                    if expected_content_length > _MAX_SHELTER_CSV_BYTES:
                        raise RegionProviderError("GSI shelter CSV exceeded the 32 MB safety limit")
                except ValueError:
                    raise RegionProviderError("GSI shelter CSV had an invalid content length")
            elif response.headers.get("Transfer-Encoding", "").casefold() != "chunked":
                raise RegionProviderError("GSI shelter CSV response length could not be verified")
            while True:
                # http.client closes the response as soon as the whole body has been
                # read, so a closed response is the end of the download. Setting the
                # read timeout on it used to fail and every download was discarded.
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RegionProviderError("GSI shelter CSV download exceeded its time limit")
                if not _set_response_read_timeout(response, min(_TIMEOUT_SECONDS, remaining)):
                    raise RegionProviderError(
                        "GSI shelter CSV read timeout could not be bounded on this runtime"
                    )
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > _MAX_SHELTER_CSV_BYTES:
                    raise RegionProviderError("GSI shelter CSV exceeded the 32 MB safety limit")
            if expected_content_length is not None and len(chunks) != expected_content_length:
                raise RegionProviderError("GSI shelter CSV download was truncated or had extra bytes")
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
        try:
            text = bytes(chunks).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RegionDatasetError("GSI shelter CSV was not UTF-8") from exc
        _validate_gsi_shelter_csv(text)
    except RegionProviderError as exc:
        error = exc
    except HTTPError as exc:
        error = RegionProviderError(f"public-data endpoint returned HTTP {exc.code}")
    except csv.Error:
        error = RegionDatasetError("GSI shelter CSV contains malformed CSV")
    except (URLError, TimeoutError, socket.timeout, OSError, IncompleteRead):
        error = RegionProviderError("GSI shelter CSV could not be downloaded or parsed")

    if error is not None:
        if (
            stale_snapshot is not None
            and (datetime.now(UTC) - stale_snapshot.retrieved_at).total_seconds()
            <= _MAX_STALE_CSV_AGE_SECONDS
        ):
            return replace(stale_snapshot, used_stale_cache=True)
        raise error

    snapshot = GsiCsvSnapshot(
        sha256=hashlib.sha256(chunks).hexdigest(),
        retrieved_at=datetime.now(UTC),
        text=text,
        etag=etag,
        last_modified=last_modified,
    )
    with _CSV_CACHE_LOCK:
        _GSI_CSV_CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, snapshot)
        _GSI_CSV_CACHE.move_to_end(cache_key)
        while len(_GSI_CSV_CACHE) > 1:
            _GSI_CSV_CACHE.popitem(last=False)
    return snapshot


def _set_response_read_timeout(response: Any, timeout: float) -> bool:
    """Apply the remaining overall deadline to the underlying response socket."""
    try:
        response.fp.raw._sock.settimeout(timeout)
        return True
    except AttributeError:
        return False


def _validate_gsi_shelter_csv(text: str) -> None:
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if not reader.fieldnames or not _GSI_SHELTER_REQUIRED_HEADERS.issubset(reader.fieldnames):
        raise RegionDatasetError("GSI designated-shelter CSV schema is not recognized")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise RegionDatasetError("GSI designated-shelter CSV contains duplicate headers")
    try:
        seen_common_ids: dict[str, tuple[tuple[str, str], ...]] = {}
        row_count = 0
        for row_number, row in enumerate(reader, start=2):
            row_count += 1
            if row_number > _MAX_GSI_CSV_ROWS + 1:
                raise RegionDatasetError("GSI designated-shelter CSV exceeded the row safety limit")
            if None in row or None in row.values():
                raise RegionDatasetError("GSI designated-shelter CSV row has an invalid column count")
            common_id = (row.get("共通ID") or "").strip()
            if common_id:
                signature = tuple(
                    (field, row.get(field) or "")
                    for field in reader.fieldnames
                    if field != "NO"
                )
                previous = seen_common_ids.get(common_id)
                if previous is not None and previous != signature:
                    raise RegionDatasetError(
                        "GSI designated-shelter CSV contains conflicting duplicate common IDs"
                    )
                seen_common_ids[common_id] = signature
        if row_count == 0:
            raise RegionDatasetError("GSI designated-shelter CSV contains no records")
    except csv.Error as exc:
        raise RegionDatasetError("GSI designated-shelter CSV contains malformed CSV") from exc


@dataclass(frozen=True)
class GsiShelterLookup:
    municipality: str | None
    municipality_code: str | None = None
    shelters: tuple[dict[str, str], ...] = ()
    municipality_candidates: tuple[tuple[str, str], ...] = ()
    municipality_candidates_truncated: bool = False
    municipality_name_verified: bool = False
    status: str = "unresolved_municipality"
    complete: bool = False
    matched_count: int = 0
    invalid_record_count: int = 0
    snapshot: GsiCsvSnapshot | None = None


class GsiShelterProvider:
    """Read GSI's designated-shelter register without treating it as exhaustive."""

    source_url = _GSI_SHELTER_CSV_URL

    def search_municipality(
        self, name: str, municipality_code: str | None = None
    ) -> GsiShelterLookup:
        normalized_name = _normalize_municipality(name)
        code = municipality_code.strip() if municipality_code is not None else None
        code_was_selected = code is not None
        if code is not None and not re.fullmatch(r"\d{5}", code):
            raise ValueError("municipality code must be the five-digit code in the GSI common ID")
        if code is None and len(normalized_name) < 2:
            raise ValueError("municipality must be at least two characters")

        snapshot = _get_gsi_shelter_snapshot(self.source_url)
        reader = csv.DictReader(io.StringIO(snapshot.text, newline=""), strict=True)
        names_by_code: dict[str, dict[str, str]] = {}
        invalid_records = 0
        exact_code_candidates: set[str] = set()
        rows_by_code: list[tuple[str, dict[str, str]]] = []
        seen_common_ids: set[str] = set()
        requested_exact_name = normalized_name
        for row in reader:
            common_id = (row.get("共通ID") or "").strip()
            parsed_id = _parse_gsi_common_id(common_id)
            municipality_name = (row.get("都道府県名及び市町村名") or "").strip()
            if (
                parsed_id is None
                or not municipality_name
                or not (row.get("施設・場所名") or "").strip()
            ):
                invalid_records += 1
                continue
            if common_id in seen_common_ids:
                continue
            seen_common_ids.add(common_id)
            record_code, designation, subtype = parsed_id
            if designation != "1":
                # Emergency sites are not overnight-shelter candidates.
                invalid_records += 1
                continue
            normalized_municipality = _normalize_municipality(municipality_name)
            names_by_code.setdefault(record_code, {})[normalized_municipality] = municipality_name
            if normalized_municipality == requested_exact_name:
                exact_code_candidates.add(record_code)
            if code is not None and record_code == code:
                rows_by_code.append((subtype, row))

        if code is None:
            if len(exact_code_candidates) == 1:
                code = next(iter(exact_code_candidates))
            elif len(exact_code_candidates) > 1:
                options = tuple(
                    sorted(
                        (candidate_code, candidate_name)
                        for candidate_code in exact_code_candidates
                        for candidate_name in names_by_code.get(candidate_code, {}).values()
                    )
                )
                return GsiShelterLookup(
                    municipality=None,
                    municipality_candidates=options,
                    municipality_name_verified=False,
                    status="ambiguous_municipality",
                    complete=False,
                    invalid_record_count=invalid_records,
                    snapshot=snapshot,
                )
            else:
                suggestions = tuple(
                    sorted(
                        (candidate_code, candidate_name)
                        for candidate_code, names in names_by_code.items()
                        for normalized_candidate, candidate_name in names.items()
                        if requested_exact_name in normalized_candidate
                    )
                )
                return GsiShelterLookup(
                    municipality=None,
                    municipality_candidates=suggestions[:_MAX_MUNICIPALITY_CODE_SUGGESTIONS],
                    municipality_candidates_truncated=(
                        len(suggestions) > _MAX_MUNICIPALITY_CODE_SUGGESTIONS
                    ),
                    status="unresolved_municipality",
                    municipality_name_verified=False,
                    complete=False,
                    invalid_record_count=invalid_records,
                    snapshot=snapshot,
                )

        if code_was_selected and exact_code_candidates and code not in exact_code_candidates:
            options = tuple(
                sorted(
                    (candidate_code, candidate_name)
                    for candidate_code in exact_code_candidates
                    for candidate_name in names_by_code.get(candidate_code, {}).values()
                )
            )
            return GsiShelterLookup(
                municipality=None,
                municipality_code=code,
                municipality_candidates=options,
                municipality_name_verified=False,
                status="ambiguous_municipality",
                complete=False,
                invalid_record_count=invalid_records,
                snapshot=snapshot,
            )

        # If exact full-name selection resolved the code, the matching is still done
        # by that verified code; partial, suffix, and geospatial matches are never used.
        if not rows_by_code:
            reader = csv.DictReader(io.StringIO(snapshot.text, newline=""), strict=True)
            seen_common_ids = set()
            for row in reader:
                common_id = (row.get("共通ID") or "").strip()
                parsed_id = _parse_gsi_common_id(common_id)
                if (
                    parsed_id is not None
                    and parsed_id[0] == code
                    and parsed_id[1] == "1"
                    and (row.get("施設・場所名") or "").strip()
                    and common_id not in seen_common_ids
                ):
                    seen_common_ids.add(common_id)
                    rows_by_code.append((parsed_id[2], row))

        candidate_names = names_by_code.get(code, {})
        if not candidate_names and code_was_selected:
            return GsiShelterLookup(
                municipality=None,
                municipality_code=code,
                municipality_name_verified=False,
                status="no_published_records",
                complete=invalid_records == 0,
                invalid_record_count=invalid_records,
                snapshot=snapshot,
            )
        if len(candidate_names) != 1:
            options = tuple(
                sorted((code, candidate_name) for candidate_name in candidate_names.values())
            )
            return GsiShelterLookup(
                municipality=None,
                municipality_code=code,
                municipality_candidates=options,
                municipality_name_verified=requested_exact_name in candidate_names,
                status="ambiguous_municipality",
                complete=False,
                invalid_record_count=invalid_records,
                snapshot=snapshot,
            )
        municipality_name_verified = requested_exact_name in candidate_names
        municipality = next(iter(candidate_names.values()))
        if not rows_by_code:
            return GsiShelterLookup(
                municipality=municipality,
                municipality_code=code,
                municipality_name_verified=municipality_name_verified,
                status="no_published_records",
                complete=invalid_records == 0,
                invalid_record_count=invalid_records,
                snapshot=snapshot,
            )
        if len(rows_by_code) > _MAX_MUNICIPAL_SHELTERS:
            return GsiShelterLookup(
                municipality=municipality,
                municipality_code=code,
                municipality_name_verified=municipality_name_verified,
                status="result_limit_exceeded",
                complete=False,
                matched_count=len(rows_by_code),
                invalid_record_count=invalid_records,
                snapshot=snapshot,
            )
        return GsiShelterLookup(
            municipality=municipality,
            municipality_code=code,
            municipality_name_verified=municipality_name_verified,
            shelters=tuple(
                dict(row, __shelter_subtype=subtype)
                for subtype, row in rows_by_code
            ),
            status="matched",
            complete=invalid_records == 0,
            matched_count=len(rows_by_code),
            invalid_record_count=invalid_records,
            snapshot=snapshot,
        )


def _parse_gsi_common_id(value: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"E(\d{5})\d{5}([12])([012])([0-9A-Za-z])", value)
    if match is None:
        return None
    municipality_code, designation, subtype, _sequence = match.groups()
    if (designation == "1" and subtype not in {"1", "2"}) or (
        designation == "2" and subtype != "0"
    ):
        return None
    return municipality_code, designation, subtype


def _normalize_municipality(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


class GsiAddressProvider:
    """GSI address search. Results are geocoding candidates, not legal boundaries."""

    source_url = "https://msearch.gsi.go.jp/address-search/AddressSearch"

    def search(self, query: str, *, limit: int = 8) -> tuple[dict[str, Any], ...]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query must be non-empty and limit must be between 1 and 20")
        document = _get_json(f"{self.source_url}?{urlencode({'q': query})}")
        if not isinstance(document, list):
            raise RegionProviderError("GSI address search response must be a list")
        candidates: list[dict[str, Any]] = []
        for item in document[:limit]:
            if not isinstance(item, dict):
                continue
            geometry = item.get("geometry")
            properties = item.get("properties")
            if not isinstance(geometry, dict) or not isinstance(properties, dict):
                continue
            coordinates = geometry.get("coordinates")
            if not isinstance(coordinates, list) or len(coordinates) < 2:
                continue
            try:
                longitude, latitude = float(coordinates[0]), float(coordinates[1])
            except (TypeError, ValueError):
                continue
            candidates.append(
                {
                    "name": str(properties.get("title") or properties.get("address") or query),
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
        return tuple(candidates)


class EStatProvider:
    """e-Stat API v3 client; it never invents an app ID or selects a table silently."""

    base_url = "https://api.e-stat.go.jp/rest/3.0/app/json"
    source_url = "https://www.e-stat.go.jp/api/api-info/api-guide"

    def __init__(self, app_id: str | None = None) -> None:
        self.app_id = app_id if app_id is not None else os.environ.get("ESTAT_APP_ID")

    def search_tables(self, query: str, *, limit: int = 10) -> tuple[dict[str, Any], ...]:
        if not self.app_id:
            return ()
        if not query.strip() or not 1 <= limit <= 50:
            raise ValueError("query must be non-empty and limit must be between 1 and 50")
        url = f"{self.base_url}/getStatsList?{urlencode({'appId': self.app_id, 'searchWord': query, 'limit': limit})}"
        root = _get_json(url).get("GET_STATS_LIST", {})
        if not isinstance(root, dict):
            return ()
        data = root.get("DATALIST_INF", {})
        records = data.get("TABLE_INF", []) if isinstance(data, dict) else []
        if isinstance(records, dict):
            records = [records]
        candidates = []
        for row in records[:limit] if isinstance(records, list) else ():
            if not isinstance(row, dict):
                continue
            table_id = row.get("@id")
            title = row.get("TITLE")
            if isinstance(title, dict):
                title = title.get("$")
            if table_id and title:
                candidates.append(
                    {
                        "table_id": str(table_id),
                        "title": str(title),
                        "organization": _nested_text(row.get("GOV_ORG")),
                        "survey_date": str(row.get("SURVEY_DATE") or "") or None,
                        "publication_date": str(row.get("OPEN_DATE") or "") or None,
                    }
                )
        return tuple(candidates)

    def describe_table(self, table_id: str) -> dict[str, Any]:
        """Return e-Stat area, period, and dimension choices without selecting them."""
        if not self.app_id:
            return {}
        if not table_id.strip():
            raise ValueError("an e-Stat table ID must be selected")
        document = _get_json(
            f"{self.base_url}/getMetaInfo?{urlencode({'appId': self.app_id, 'statsDataId': table_id})}"
        )
        root = document.get("GET_META_INFO", {})
        metadata = root.get("METADATA_INF", {}) if isinstance(root, dict) else {}
        if not isinstance(metadata, dict):
            return {}
        table_info = metadata.get("TABLE_INF", {})
        if not isinstance(table_info, dict):
            table_info = {}
        class_info = metadata.get("CLASS_INF", {})
        objects = class_info.get("CLASS_OBJ", []) if isinstance(class_info, dict) else []
        if isinstance(objects, dict):
            objects = [objects]
        areas: list[dict[str, str]] = []
        periods: list[dict[str, str]] = []
        dimensions: list[dict[str, Any]] = []
        units: set[str] = set()
        for raw in objects if isinstance(objects, list) else ():
            if not isinstance(raw, dict):
                continue
            dimension_id = str(raw.get("@id") or "")
            dimension_label = _nested_text(raw.get("@name")) or dimension_id
            classes = raw.get("CLASS", [])
            if isinstance(classes, dict):
                classes = [classes]
            choices = []
            for item in classes if isinstance(classes, list) else ():
                if not isinstance(item, dict) or item.get("@code") is None:
                    continue
                label = _nested_text(item.get("@name")) or _nested_text(item.get("NAME"))
                if not label:
                    continue
                choice = {"code": str(item["@code"]), "label": label}
                choices.append(choice)
                if item.get("@unit"):
                    units.add(str(item["@unit"]))
            if dimension_id == "area":
                areas.extend(choices)
            elif dimension_id == "time":
                periods.extend(choices)
            elif dimension_id:
                dimensions.append(
                    {"dimension_id": dimension_id, "label": dimension_label, "choices": choices}
                )
        table_id_value = _nested_text(table_info.get("@id")) or table_id
        title = _nested_text(table_info.get("TITLE")) or table_id
        raw_unit = table_info.get("UNIT")
        unit_text = _nested_text(raw_unit)
        if unit_text:
            units.add(unit_text)
        return {
            "table_id": table_id_value,
            "title": title,
            "geographic_scope": _nested_text(table_info.get("AREA")),
            "content_sha256": hashlib.sha256(
                json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "areas": areas,
            "periods": periods,
            "dimensions": dimensions,
            "units": sorted(units),
        }

    def fetch_table(
        self,
        table_id: str,
        area_code: str,
        *,
        period_code: str | None = None,
        category_codes: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch only the table, area, period, and dimensions explicitly selected by the operator."""
        if not self.app_id:
            return ()
        if not table_id.strip() or not area_code.strip():
            raise ValueError("both an e-Stat table ID and area code must be selected")
        params = {
            "appId": self.app_id,
            "statsDataId": table_id,
            "cdArea": area_code,
            "metaGetFlg": "Y",
            "cntGetFlg": "N",
        }
        if period_code:
            params["cdTime"] = period_code
        for dimension_id, code in (category_codes or {}).items():
            match = re.fullmatch(r"cat(\d{2})", dimension_id)
            if match is None or not code:
                raise ValueError("e-Stat category selections must use a catNN dimension and a class code")
            params[f"cdCat{match.group(1)}"] = code
        response_document = _get_json(f"{self.base_url}/getStatsData?{urlencode(params)}")
        response_sha256 = hashlib.sha256(
            json.dumps(response_document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        root = response_document.get("GET_STATS_DATA", {})
        if not isinstance(root, dict):
            return ()
        table = root.get("STATISTICAL_DATA", {})
        if not isinstance(table, dict):
            return ()
        meta = table.get("TABLE_INF", {})
        title = _nested_text(meta.get("TITLE")) if isinstance(meta, dict) else None
        area_labels, category_labels = _class_label_maps(table.get("CLASS_INF", {}))
        data_inf = table.get("DATA_INF", {})
        values = data_inf.get("VALUE", []) if isinstance(data_inf, dict) else []
        if isinstance(values, dict):
            values = [values]
        observations: list[dict[str, Any]] = []
        for item in values if isinstance(values, list) else ():
            if not isinstance(item, dict) or "@value" not in item:
                continue
            try:
                numeric_value = float(item["@value"])
            except (TypeError, ValueError):
                continue
            observations.append(
                {
                    "table_id": table_id,
                    "title": str(title or table_id),
                    "area_code": str(item.get("@area") or area_code),
                    "area_label": area_labels.get(str(item.get("@area") or area_code)),
                    "period_code": str(item.get("@time")) if item.get("@time") is not None else None,
                    "category_codes": {
                        key[1:]: str(value)
                        for key, value in item.items()
                        if key.startswith("@cat") and value is not None
                    },
                    "category_labels": {
                        key[1:]: category_labels.get(key[1:], {}).get(str(value), "")
                        for key, value in item.items()
                        if key.startswith("@cat") and value is not None
                    },
                    "value": numeric_value,
                    "unit": str(item.get("@unit")) if item.get("@unit") else None,
                    "content_sha256": response_sha256,
                }
            )
        return tuple(observations)


def _nested_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("$", value.get("@name"))
    return str(value) if value not in (None, "") else None


def _class_label_maps(class_info: Any) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    if not isinstance(class_info, dict):
        return {}, {}
    objects = class_info.get("CLASS_OBJ", [])
    if isinstance(objects, dict):
        objects = [objects]
    area_labels: dict[str, str] = {}
    category_labels: dict[str, dict[str, str]] = {}
    for class_object in objects if isinstance(objects, list) else ():
        if not isinstance(class_object, dict):
            continue
        dimension_id = str(class_object.get("@id") or "")
        classes = class_object.get("CLASS", [])
        if isinstance(classes, dict):
            classes = [classes]
        labels: dict[str, str] = {}
        for item in classes if isinstance(classes, list) else ():
            if isinstance(item, dict) and item.get("@code") is not None:
                name = _nested_text(item.get("@name")) or _nested_text(item.get("NAME"))
                if name:
                    labels[str(item["@code"])] = name
        if dimension_id == "area":
            area_labels = labels
        elif dimension_id:
            category_labels[dimension_id] = labels
    return area_labels, category_labels

