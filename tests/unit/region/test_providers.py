"""Public data providers: the JSON cache and the GSI shelter CSV download.

How the regional research combines providers and LLM search is checked in
tests/unit/region/test_research.py.
"""

from __future__ import annotations

import pytest

from sheltersim.region import providers


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
