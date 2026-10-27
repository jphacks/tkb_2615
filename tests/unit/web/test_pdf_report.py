"""The PDF report's problem window counts only observed intervals and reports the missing
minutes instead of inventing them.
"""

from __future__ import annotations

from sheltersim.web.pdf_report import problem_window


def test_problem_duration_does_not_invent_missing_intervals():
    rows = [{"minute_index": m, "q": q} for m, q in [(0, 0), (1, 2), (2, 3), (4, 2), (5, 0)]]
    window = problem_window(rows, "q", 0, 5)
    assert window["first_observed_minute"] == 1
    assert window["positive_interval_minutes"] == 2
    assert window["intervals"] == [[1, 2], [4, 5]]
    assert window["missing_minutes"] == 2
    assert problem_window([], "q", 0, 5)["missing_minutes"] == 5
