"""Regional research: LLM search stays a candidate, and warnings follow the provider results.

The research also reads every public service for a selected municipality, and a service
that fails becomes an unknown instead of stopping the research. Those services are the
fakes of tests/support/public_data.py. The providers themselves are checked in
tests/unit/region/test_providers.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from support.public_data import FAILURES, HOSTS, PLAN_SOURCE_ID

from sheltersim.region import providers
from sheltersim.region.models import LLMSearchSourceCandidate
from sheltersim.region.research import RegionResearchService, _official_source_search_task


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


def test_research_reads_every_public_service_for_the_selected_municipality(
    public_data, research_request
) -> None:
    result = RegionResearchService().research(research_request())

    assert [item.name for item in result.municipalities] == ["合成県合成市"]
    assert result.shelter_register.status == "matched" and result.shelter_register.complete
    assert [(item.name, item.shelter_subtype) for item in result.shelters] == [
        ("合成小学校", "general"),
        ("合成福祉センター", "welfare"),
    ]
    assert [item.table_id for item in result.statistical_tables] == ["0000000001"]
    assert {
        item.category_labels["cat01"]: item.value for item in result.population_observations
    } == {
        "0～14歳": 0,
        "15～64歳": 0,
        "65歳以上": 5000,
    }
    # The selected day only: weather and rain chance for the area, temperature for its station.
    assert sorted(item.metric for item in result.weather_observations) == [
        "precipitation_probability_pct",
        "temperature_c",
        "weather_description",
    ]
    assert [(item.metric, item.value) for item in result.municipal_plan_observations] == [
        ("備蓄水", 3000)
    ]
    statuses = {item.data_kind: item.status for item in result.provider_statuses}
    assert statuses == {
        "hazard": "not_configured",
        "municipal_plan": "available",
        "weather": "available",
    }
    assert PLAN_SOURCE_ID in {item.id for item in result.sources}
    assert "placeholder-app-id" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("host", "failure"),
    [(host, failure) for host in HOSTS for failure in FAILURES],
)
def test_a_failing_public_service_becomes_an_unknown(
    public_data, research_request, host, failure
) -> None:
    public_data.fail(host, failure)

    result = RegionResearchService().research(research_request())

    # The other services still answer.
    assert bool(result.shelters) == (host != "hinanmap.gsi.go.jp")
    assert bool(result.population_observations) == (host != "api.e-stat.go.jp")
    assert bool(result.weather_observations) == (host != "www.jma.go.jp")
    assert bool(result.municipal_plan_observations) == (host != "data.example.jp")
    assert result.unknowns
