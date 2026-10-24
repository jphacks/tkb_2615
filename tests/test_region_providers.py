from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sheltersim.region import providers
from sheltersim.region.models import LLMSearchSourceCandidate
from sheltersim.region.research import _official_source_search_task


def test_llm_region_search_is_official_domain_only_and_keeps_candidate_status() -> None:
    from sheltersim.region.models import RegionResearchRequest

    task = _official_source_search_task(RegionResearchRequest(
        municipality_query="墨田区", selected_municipality_name="墨田区",
        disaster="earthquake"))
    assert task.max_web_searches == 1
    assert task.web_search_allowed_domains == ("go.jp", "lg.jp")
    assert "population" in task.input_text
    candidate = LLMSearchSourceCandidate(
        title="Municipal shelter list", url="https://www.city.sumida.lg.jp/shelter",
        cited_text="Public page citation", accessed_at=datetime.now(UTC))
    assert candidate.status == "candidate_not_independently_verified"
    assert candidate.source_kind == "llm_search_candidate"
    with pytest.raises(ValueError, match="government HTTPS host"):
        LLMSearchSourceCandidate(title="unverified", url="https://example.com/page",
            accessed_at=datetime.now(UTC))


def test_region_research_keeps_llm_search_citations_out_of_observations() -> None:
    from sheltersim.llm.cache import ResponseTape
    from sheltersim.llm.config import LLMMode, LLMSettings
    from sheltersim.llm.contracts import ProviderCitation, ProviderResponse
    from sheltersim.region.additional_providers import ConfiguredRegionalProviderResult
    from sheltersim.region.models import RegionalProviderStatus, RegionResearchRequest
    from sheltersim.region.providers import RegionProviderError
    from sheltersim.region.research import RegionResearchService

    class AddressProvider:
        def search(self, _query):
            return []

    class EStatProvider:
        app_id = None

    class ShelterProvider:
        source_url = "https://www.gsi.go.jp/shelter.csv"

        def search_municipality(self, *_args, **_kwargs):
            raise RegionProviderError("offline fixture")

    class RegionalProvider:
        def acquire(self, _request):
            return ConfiguredRegionalProviderResult()

    class WeatherProvider:
        def fetch(self, _request):
            return None, (), RegionalProviderStatus(
                data_kind="weather", status="not_configured", applicability="not selected",
                message="no JMA area")

    class SearchRouter:
        mode = LLMMode.RECORD
        settings = LLMSettings(mode="record", api_key="test")

        def __init__(self):
            self.tape = ResponseTape()
            self.calls = 0

        def complete(self, task):
            self.calls += 1
            response = ProviderResponse(text='{"status":"candidate_sources_found","notes":[]}',
                model_id=self.settings.model_for(task.stage.value), input_tokens=1,
                output_tokens=1, elapsed_ms=1,
                citations=(ProviderCitation(url="https://www.city.sumida.lg.jp/shelter",
                    title="Sumida shelter page", cited_text="Official designation page."),))
            self.tape.put(task.cache_key(response.model_id), response)
            return response.text

    router = SearchRouter()
    service = RegionResearchService(address_provider=AddressProvider(),
        estat_provider=EStatProvider(), shelter_provider=ShelterProvider(),
        regional_data_provider=RegionalProvider(), weather_provider=WeatherProvider())
    result = service.research(RegionResearchRequest(municipality_query="墨田区",
        selected_municipality_name="墨田区", disaster="earthquake"), router=router)

    assert router.calls == 1
    assert len(result.llm_source_candidates) == 1
    assert result.llm_source_candidates[0].status == "candidate_not_independently_verified"
    assert result.population_observations == ()
    assert result.hazard_observations == ()
    assert result.shelters == ()


def test_public_json_cache_strips_app_id_and_accepts_query_pairs(
    monkeypatch,
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b"[]"

    class Opener:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def open(self, request, *, timeout):
            assert timeout == providers._TIMEOUT_SECONDS
            self.urls.append(request.full_url)
            return Response()

    opener = Opener()
    monkeypatch.setattr(providers, "build_opener", lambda *_args: opener)
    providers.clear_region_provider_cache()
    url = (
        "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList"
        "?appId=secret-a&searchWord=%E4%BB%99%E5%8F%B0%E5%B8%82"
    )

    assert providers._get_json(url) == []
    assert providers._get_json(url.replace("secret-a", "secret-b")) == []
    assert len(opener.urls) == 1
    assert "appId=secret-a" in opener.urls[0]
    providers.clear_region_provider_cache()


_SUMIDA_SHELTER_CSV = (
    "共通ID,都道府県名及び市町村名,施設・場所名,住所,受入対象者,緯度,経度\n"
    "E1310700001111,東京都墨田区,押上小学校,東京都墨田区押上3-46-17,,35.712,139.819\n"
).encode("utf-8")


class _HttpLikeResponse:
    """Mimic http.client: the body is read in pieces and fp closes once it is all read."""

    status = 200

    def __init__(self, body: bytes, *, socket_timeout_supported: bool = True) -> None:
        self._body = body
        self.headers = {"Content-Length": str(len(body))}
        self.timeouts: list[float] = []
        sock = type("Sock", (), {"settimeout": lambda _self, value: self.timeouts.append(value)})()
        raw = type("Raw", (), {})()
        if socket_timeout_supported:
            raw._sock = sock
        self.fp = type("Fp", (), {"raw": raw})()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def isclosed(self) -> bool:
        return self.fp is None

    def read1(self, size: int) -> bytes:
        chunk, self._body = self._body[:size], self._body[size:]
        if not self._body:
            self.fp = None
        return chunk


def _serve_csv(monkeypatch, response: _HttpLikeResponse) -> None:
    class Opener:
        def open(self, _request, *, timeout):
            assert timeout == providers._TIMEOUT_SECONDS
            return response

    monkeypatch.setattr(providers, "build_opener", lambda *_args: Opener())
    providers.clear_region_provider_cache()


def test_gsi_shelter_csv_download_completes_when_the_response_closes(monkeypatch) -> None:
    # Reading the last bytes closes an http.client response; setting the read timeout
    # on it afterwards failed, so every complete download used to be discarded.
    response = _HttpLikeResponse(_SUMIDA_SHELTER_CSV)
    _serve_csv(monkeypatch, response)

    lookup = providers.GsiShelterProvider().search_municipality("東京都墨田区", "13107")

    assert lookup.status == "matched"
    assert lookup.complete
    assert [row["施設・場所名"] for row in lookup.shelters] == ["押上小学校"]
    assert response.timeouts  # every read while open was bounded
    providers.clear_region_provider_cache()


def test_gsi_shelter_csv_still_rejects_an_open_response_without_a_read_timeout(
    monkeypatch,
) -> None:
    _serve_csv(monkeypatch, _HttpLikeResponse(_SUMIDA_SHELTER_CSV, socket_timeout_supported=False))

    with pytest.raises(providers.RegionProviderError, match="could not be bounded"):
        providers.GsiShelterProvider().search_municipality("東京都墨田区", "13107")
    providers.clear_region_provider_cache()


@pytest.mark.parametrize(("invalid_rows", "warned"), [(0, False), (2, True)])
def test_unreadable_row_warning_needs_unreadable_rows(invalid_rows: int, warned: bool) -> None:
    from sheltersim.region.additional_providers import ConfiguredRegionalProviderResult
    from sheltersim.region.models import RegionalProviderStatus, RegionResearchRequest
    from sheltersim.region.research import RegionResearchService

    class AddressProvider:
        def search(self, _query):
            return []

    class EStatProvider:
        app_id = None

    class ShelterProvider:
        source_url = "https://hinanmap.gsi.go.jp/shelter.csv"

        def search_municipality(self, *_args, **_kwargs):
            # An unresolved municipality is incomplete whether or not rows were unreadable.
            return providers.GsiShelterLookup(
                municipality=None, status="unresolved_municipality", complete=False,
                invalid_record_count=invalid_rows,
            )

    class RegionalProvider:
        def acquire(self, _request):
            return ConfiguredRegionalProviderResult()

    class WeatherProvider:
        def fetch(self, _request):
            return None, (), RegionalProviderStatus(
                data_kind="weather", status="not_configured", applicability="not selected",
                message="no JMA area")

    service = RegionResearchService(address_provider=AddressProvider(),
        estat_provider=EStatProvider(), shelter_provider=ShelterProvider(),
        regional_data_provider=RegionalProvider(), weather_provider=WeatherProvider())
    result = service.research(RegionResearchRequest(municipality_query="墨田区",
        selected_municipality_name="墨田区", disaster="earthquake"))

    assert any("解釈できない行" in warning for warning in result.warnings) is warned
