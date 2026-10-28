"""The PDF report's problem window counts only observed intervals and reports the missing
minutes instead of inventing them, and an intervention keeps its quantities but no
per-person context.
"""

from __future__ import annotations

from sheltersim.web.pdf_report import intervention_details, problem_window


def test_problem_duration_does_not_invent_missing_intervals():
    rows = [{"minute_index": m, "q": q} for m, q in [(0, 0), (1, 2), (2, 3), (4, 2), (5, 0)]]
    window = problem_window(rows, "q", 0, 5)
    assert window["first_observed_minute"] == 1
    assert window["positive_interval_minutes"] == 2
    assert window["intervals"] == [[1, 2], [4, 5]]
    assert window["missing_minutes"] == 2
    assert problem_window([], "q", 0, 5)["missing_minutes"] == 5


def test_effect_program_interventions_keep_their_top_level_quantities():
    """EffectProgram events put these parameters beside op, not under parameters (#160)."""
    ids = {"program_id": "program-1", "effect_id": "effect-1", "modifier_id": "modifier-1"}
    assert intervention_details({**ids, "op": "add_toilet", "zone_id": "toilet", "count": 2}) == {
        "op": "add_toilet",
        "zone_id": "toilet",
        "count": 2,
    }
    assert intervention_details(
        {"warehouse_id": "warehouse", "resource_id": "water_ml", "quantity": 20000}
    ) == {"resource_id": "water_ml", "quantity": 20000}
    assert intervention_details({"shipment_id": "shipment-1", "to_minute": 180}) == {
        "to_minute": 180
    }
    assert intervention_details({"staff_id": "staff_001", "until_minute": 120}) == {
        "staff_id": "staff_001",
        "until_minute": 120,
    }


def test_arriving_evacuees_report_their_count_without_person_context():
    details = intervention_details(
        {
            "program_id": "program-1",
            "effect_id": "effect-1",
            "op": "add_evacuees",
            "zone_id": "hall",
            "count": 4,
            "household_size": 2,
            "mobility": "assisted",
            "language": "ja",
            "admission_zone_ids": ["hall"],
            "approach_route_id": None,
            "person_context_overrides": [{"age_band": "75+"}],
        }
    )
    assert details == {"op": "add_evacuees", "zone_id": "hall", "count": 4, "household_size": 2}
