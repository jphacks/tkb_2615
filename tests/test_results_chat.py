"""Read-only result facts remain grounded when a model claim fails validation."""

from __future__ import annotations

from sheltersim.web.results_chat import _deterministic_exact_field_claim


def test_exact_field_fallback_quotes_only_the_requested_evidence_alias() -> None:
    evidence = [
        {"alias": "S1", "data": {"stress_mean": 0.42}},
        {"alias": "S2", "data": {"stress_mean": 0.11}},
    ]
    sources = [
        {"alias": "S1", "label": "KPI · minute 29"},
        {"alias": "S2", "label": "KPI · minute 12"},
    ]

    claim = _deterministic_exact_field_claim(
        "Return one fact for the exact value in alias S1 field stress_mean.",
        evidence,
        sources,
    )

    assert claim is not None
    assert claim["evidence"] == [{"alias": "S1", "field": "stress_mean", "value": 0.42}]
    assert "0.42" in claim["text"]


def test_exact_field_fallback_refuses_unknown_aliases_and_ambiguous_fields() -> None:
    evidence = [{"alias": "S1", "data": {"stress_mean": 0.42, "trust": 0.5}}]
    sources = [{"alias": "S1", "label": "Selected Agent"}]

    assert _deterministic_exact_field_claim("S2 stress_mean", evidence, sources) is None
    assert (
        _deterministic_exact_field_claim(
            "S1 stress_mean and trust", evidence, sources
        )
        is None
    )
