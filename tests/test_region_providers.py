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
