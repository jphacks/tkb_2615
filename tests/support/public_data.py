"""Fake public data services for the regional research, and the Project API that uses them.

The regional research reads five public services over HTTPS: the GSI address search
and designated-shelter CSV, e-Stat, the JMA forecast, and a regional source the
deployment configures. FakePublicData answers them from synthetic records of a
municipality that does not exist (合成県合成市, code 99901), so nothing leaves the
machine. Its fail() makes one service fail the way a real connection can.
"""

from __future__ import annotations

import copy
import http.client
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest

from support.isolation import _isolate

MUNICIPALITY = "合成県合成市"
MUNICIPALITY_CODE = "99901"
SCHOOL_SHELTER_ID = "GSI-E9990100001111"
ESTAT_TABLE_ID = "0000000001"
ESTAT_PERIOD_CODE = "2020000000"
JMA_AREA_CODE = "999900"
PLAN_SOURCE_ID = "SRC-SYNTHETIC-MUNICIPAL-PLAN"

# GSI common IDs are E + municipality code + five digits + designation (1: shelter)
# + subtype (1: general, 2: welfare) + sequence. 合成県別市 shares the word 合成.
SHELTER_CSV = (
    "共通ID,都道府県名及び市町村名,施設・場所名,住所,受入対象者,緯度,経度\n"
    "E9990100001111,合成県合成市,合成小学校,合成県合成市一丁目1,,35.10,139.10\n"
    "E9990100002121,合成県合成市,合成福祉センター,合成県合成市二丁目2,要配慮者,35.11,139.11\n"
    "E9990200001111,合成県別市,別市中学校,合成県別市三丁目3,,35.20,139.20\n"
)

# The selected table counts 5000 people aged 65 and over and nobody younger.
AGE_CLASSES = [("100", "0～14歳", "0"), ("200", "15～64歳", "0"), ("300", "65歳以上", "5000")]

# A deployment's regional source configuration (SHELTERSIM_REGION_DATA_SOURCES_JSON).
PLAN_SOURCE = {
    "data_kind": "municipal_plan",
    "source_id": PLAN_SOURCE_ID,
    "title": "合成市地域防災計画",
    "publisher": "合成市",
    "url": "https://data.example.jp/plan.json",
    "allowed_hosts": ["data.example.jp"],
    "geographic_scope": "合成市",
    "source_kind": "municipal",
    "license_note": "Synthetic test record.",
    "fields": {
        "metric": "metric",
        "value": "value",
        "unit": "unit",
        "geographic_scope": "scope",
        "period": "period",
        "applicability": "applies",
    },
    "municipality_code_field": "code",
}

FAILURES = (
    "http_503",
    "unreachable",
    "timeout",
    "disconnected",
    "reset_while_reading",
    "truncated",
    "not_json",
    "json_list",
    "json_null",
    "empty",
)

HOSTS = (
    "msearch.gsi.go.jp",
    "hinanmap.gsi.go.jp",
    "api.e-stat.go.jp",
    "www.jma.go.jp",
    "data.example.jp",
)


def forecast_dates() -> list[str]:
    """The three days of the fake JMA forecast, from today, at midnight UTC."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today + timedelta(days=day)).isoformat() for day in range(3)]


class _Response:
    """Mimic http.client: read in pieces, with a socket whose read timeout can be set."""

    status = 200

    def __init__(self, body: bytes, failure: str | None = None) -> None:
        self._body, self._failure = body, failure
        self.headers = {"Content-Length": str(len(body))}
        raw = type("Raw", (), {})()
        raw._sock = type("Sock", (), {"settimeout": lambda _self, _value: None})()
        self.fp = type("Fp", (), {"raw": raw})()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._failure == "truncated":
            raise http.client.IncompleteRead(self._body[:1], len(self._body) - 1)
        if self._failure == "reset_while_reading":
            raise ConnectionResetError("connection reset by peer")
        size = len(self._body) if size < 0 else size
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def read1(self, size: int) -> bytes:
        chunk = self.read(size)
        if not self._body:
            self.fp = None
        return chunk

    def isclosed(self) -> bool:
        return self.fp is None


class FakePublicData:
    """Answers the research's HTTPS requests; records every URL it was asked for."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.age_classes = list(AGE_CLASSES)
        self._failures: dict[str, str] = {}

    def fail(self, host: str, failure: str) -> None:
        """Make every request to host fail in one of the FAILURES ways."""
        self._failures[host] = failure

    def open(self, request: Any, *, timeout: float) -> _Response:
        url = request.full_url
        self.urls.append(url)
        parts = urlsplit(url)
        failure = self._failures.get(parts.hostname or "")
        if failure == "http_503":
            raise HTTPError(url, 503, "Service Unavailable", {}, None)
        if failure == "unreachable":
            raise URLError("name or service not known")
        if failure == "timeout":
            raise TimeoutError("timed out")
        if failure == "disconnected":
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        body = {
            "not_json": b"<html>maintenance</html>",
            "json_list": b"[]",
            "json_null": b"null",
            "empty": b"",
        }.get(failure or "")
        if body is None:
            body = self._body(parts.hostname or "", parts.path)
        return _Response(body, failure)

    def _body(self, host: str, path: str) -> bytes:
        if host == "hinanmap.gsi.go.jp":
            return SHELTER_CSV.encode("utf-8")
        if host == "msearch.gsi.go.jp":
            document: Any = [
                {"geometry": {"coordinates": [139.1, 35.1]}, "properties": {"title": MUNICIPALITY}}
            ]
        elif host == "api.e-stat.go.jp":
            document = self._estat(path.rsplit("/", 1)[-1])
        elif host == "www.jma.go.jp":
            days = forecast_dates()
            document = [
                {
                    "reportDatetime": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "timeSeries": [
                        {
                            "timeDefines": days,
                            "areas": [
                                {
                                    "area": {"name": "合成地方", "code": "999910"},
                                    "weathers": ["晴れ", "くもり", "雨"],
                                    "pops": ["0", "10", "80"],
                                }
                            ],
                        },
                        {
                            "timeDefines": days,
                            "areas": [
                                {
                                    "area": {"name": "合成", "code": "99991"},
                                    "temps": ["5", "12", "3"],
                                }
                            ],
                        },
                    ],
                }
            ]
        elif host == "data.example.jp":
            document = {
                "records": [
                    {
                        "metric": "備蓄水",
                        "value": 3000,
                        "unit": "L",
                        "scope": "合成市",
                        "period": "2024",
                        "applies": "市全体",
                        "code": MUNICIPALITY_CODE,
                    }
                ]
            }
        else:
            raise AssertionError(f"unexpected public data request: {host}{path}")
        return json.dumps(document, ensure_ascii=False).encode("utf-8")

    def _estat(self, endpoint: str) -> dict[str, Any]:
        if endpoint == "getStatsList":
            table = {
                "@id": ESTAT_TABLE_ID,
                "TITLE": {"$": "年齢別人口"},
                "GOV_ORG": {"$": "合成省"},
            }
            return {"GET_STATS_LIST": {"DATALIST_INF": {"TABLE_INF": [table]}}}
        classes = {
            "CLASS_OBJ": [
                {"@id": "area", "CLASS": [{"@code": MUNICIPALITY_CODE, "@name": "合成市"}]},
                {"@id": "time", "CLASS": [{"@code": ESTAT_PERIOD_CODE, "@name": "2020年"}]},
                {
                    "@id": "cat01",
                    "@name": "年齢",
                    "CLASS": [
                        {"@code": code, "@name": label} for code, label, _ in self.age_classes
                    ],
                },
            ]
        }
        if endpoint == "getMetaInfo":
            table_info = {"@id": ESTAT_TABLE_ID, "TITLE": {"$": "年齢別人口"}, "AREA": "合成市"}
            return {
                "GET_META_INFO": {"METADATA_INF": {"TABLE_INF": table_info, "CLASS_INF": classes}}
            }
        values = [
            {
                "@area": MUNICIPALITY_CODE,
                "@time": ESTAT_PERIOD_CODE,
                "@cat01": code,
                "@unit": "人",
                "@value": value,
            }
            for code, _, value in self.age_classes
        ]
        data = {
            "TABLE_INF": {"TITLE": {"$": "年齢別人口"}},
            "CLASS_INF": classes,
            "DATA_INF": {"VALUE": values},
        }
        return {"GET_STATS_DATA": {"STATISTICAL_DATA": data}}


def _clear_caches() -> None:
    from sheltersim.region import additional_providers, providers

    providers.clear_region_provider_cache()
    with additional_providers._JSON_CACHE_LOCK:
        additional_providers._JSON_CACHE.clear()


@pytest.fixture
def public_data(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePublicData]:
    """Answer the regional research's public data requests with FakePublicData.

    The e-Stat app ID is a placeholder and the deployment configures PLAN_SOURCE.
    """
    from sheltersim.region import additional_providers, providers

    fake = FakePublicData()
    monkeypatch.setattr(providers, "build_opener", lambda *_args: fake)
    monkeypatch.setattr(additional_providers, "build_opener", lambda *_args: fake)
    monkeypatch.setenv("ESTAT_APP_ID", "placeholder-app-id")
    monkeypatch.setenv("SHELTERSIM_REGION_DATA_SOURCES_JSON", json.dumps([PLAN_SOURCE]))
    _clear_caches()
    yield fake
    _clear_caches()


@pytest.fixture
def researching_project_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public_data: FakePublicData
) -> Iterator[Any]:
    """The Project screen's API with the real regional research over public_data."""
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = ProjectStore(tmp_path / "store")
    store.create_project("project")
    api = ProjectAPI(store)
    try:
        yield api
    finally:
        api.close()


@pytest.fixture
def research_request() -> Callable[..., Any]:
    """A RegionResearchRequest for MUNICIPALITY with every public service selected."""

    def build(**fields: Any) -> Any:
        from sheltersim.region.models import RegionResearchRequest

        values = {
            "municipality_query": MUNICIPALITY,
            "municipality_code": MUNICIPALITY_CODE,
            "disaster": "地震",
            "estat_stats_data_id": ESTAT_TABLE_ID,
            "estat_area_code": MUNICIPALITY_CODE,
            "estat_period_code": ESTAT_PERIOD_CODE,
            "weather_area_code": JMA_AREA_CODE,
            "target_date": forecast_dates()[1][:10],
        }
        return RegionResearchRequest.model_validate(copy.deepcopy(values | fields))

    return build
