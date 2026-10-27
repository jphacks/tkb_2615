"""Regional research: LLM search stays a candidate, and warnings follow the provider results.

The providers themselves are checked in tests/unit/region/test_providers.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sheltersim.region import providers
from sheltersim.region.models import LLMSearchSourceCandidate
from sheltersim.region.research import _official_source_search_task


def test_llm_region_search_is_official_domain_only_and_keeps_candidate_status() -> None:
    from sheltersim.region.models import RegionResearchRequest

    task = _official_source_search_task(
        RegionResearchRequest(
            municipality_query="墨田区", selected_municipality_name="墨田区", disaster="earthquake"
        )
    )
    assert task.max_web_searches == 1
    assert task.web_search_allowed_domains == ("go.jp", "lg.jp")
    assert "population" in task.input_text
    candidate = LLMSearchSourceCandidate(
        title="Municipal shelter list",
        url="https://www.city.sumida.lg.jp/shelter",
        cited_text="Public page citation",
        accessed_at=datetime.now(UTC),
    )
    assert candidate.status == "candidate_not_independently_verified"
    assert candidate.source_kind == "llm_search_candidate"
    with pytest.raises(ValueError, match="government HTTPS host"):
        LLMSearchSourceCandidate(
            title="unverified", url="https://example.com/page", accessed_at=datetime.now(UTC)
        )


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
            return (
                None,
                (),
                RegionalProviderStatus(
                    data_kind="weather",
                    status="not_configured",
                    applicability="not selected",
                    message="no JMA area",
                ),
            )

    class SearchRouter:
        mode = LLMMode.RECORD
        settings = LLMSettings(mode="record", api_key="test")

        def __init__(self):
            self.tape = ResponseTape()
            self.calls = 0

        def complete(self, task):
            self.calls += 1
            response = ProviderResponse(
                text='{"status":"candidate_sources_found","notes":[]}',
                model_id=self.settings.model_for(task.stage.value),
                input_tokens=1,
                output_tokens=1,
                elapsed_ms=1,
                citations=(
                    ProviderCitation(
                        url="https://www.city.sumida.lg.jp/shelter",
                        title="Sumida shelter page",
                        cited_text="Official designation page.",
                    ),
                ),
            )
            self.tape.put(task.cache_key(response.model_id), response)
            return response.text

    router = SearchRouter()
    service = RegionResearchService(
        address_provider=AddressProvider(),
        estat_provider=EStatProvider(),
        shelter_provider=ShelterProvider(),
        regional_data_provider=RegionalProvider(),
        weather_provider=WeatherProvider(),
    )
    result = service.research(
        RegionResearchRequest(
            municipality_query="墨田区", selected_municipality_name="墨田区", disaster="earthquake"
        ),
        router=router,
    )

    assert router.calls == 1
    assert len(result.llm_source_candidates) == 1
    assert result.llm_source_candidates[0].status == "candidate_not_independently_verified"
    assert result.population_observations == ()
    assert result.hazard_observations == ()
    assert result.shelters == ()


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
                municipality=None,
                status="unresolved_municipality",
                complete=False,
                invalid_record_count=invalid_rows,
            )

    class RegionalProvider:
        def acquire(self, _request):
            return ConfiguredRegionalProviderResult()

    class WeatherProvider:
        def fetch(self, _request):
            return (
                None,
                (),
                RegionalProviderStatus(
                    data_kind="weather",
                    status="not_configured",
                    applicability="not selected",
                    message="no JMA area",
                ),
            )

    service = RegionResearchService(
        address_provider=AddressProvider(),
        estat_provider=EStatProvider(),
        shelter_provider=ShelterProvider(),
        regional_data_provider=RegionalProvider(),
        weather_provider=WeatherProvider(),
    )
    result = service.research(
        RegionResearchRequest(
            municipality_query="墨田区", selected_municipality_name="墨田区", disaster="earthquake"
        )
    )

    assert any("解釈できない行" in warning for warning in result.warnings) is warned
