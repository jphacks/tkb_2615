"""Comparison reports name each side's psychology model."""

from __future__ import annotations

from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.experiments.comparison import render_comparison_markdown


def test_comparison_markdown_has_no_warning_for_the_research_model() -> None:
    markdown = render_comparison_markdown(
        {
            "psychology_models": {
                "baseline": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
                "candidate": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
            },
            "final_metric_differences": {},
        }
    )
    assert f"baseline `{INTEGRATED_PSYCHOLOGY_POLICY_VERSION}`" in markdown
    assert "Warning" not in markdown
