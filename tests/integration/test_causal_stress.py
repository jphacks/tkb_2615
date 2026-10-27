"""Causal stress contributions replay the same at every trace level, with and without
social propagation (H04).

The contributions one tick at a time are checked in tests/unit/policies/test_psychology.py,
and support gating in tests/unit/policies/test_behavior.py.
"""

from __future__ import annotations

import json

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario


def test_trace_levels_and_social_policy_replay_deterministically(
    tmp_path, causal_stress_scenario
) -> None:
    scenario = causal_stress_scenario(duration=12, with_service=False)
    worlds = {}
    for level in (TraceLevel.OFF, TraceLevel.SUMMARY, TraceLevel.FULL):
        output = tmp_path / level.value
        worlds[level] = run_scenario(scenario, output, trace_level=level)
        trace_path = output / "psychology_contributions.jsonl"
        if level is TraceLevel.OFF:
            assert not trace_path.exists()
        else:
            records = [json.loads(line) for line in trace_path.read_text().splitlines()]
            assert records
            assert all(record["social_propagation_is_separate"] for record in records)
    final_hashes = {world.canonical_json() for world in worlds.values()}
    assert len(final_hashes) == 1
    assert all(len(world.support_requests) <= len(world.agents) for world in worlds.values())

    social_scenario = causal_stress_scenario(duration=12, social_enabled=True, with_service=False)
    first = run_scenario(social_scenario, tmp_path / "social-1", trace_level=TraceLevel.FULL)
    second = run_scenario(social_scenario, tmp_path / "social-2", trace_level=TraceLevel.FULL)
    assert first.canonical_json() == second.canonical_json()
    contacts = (tmp_path / "social-1" / "social_contacts.jsonl").read_text(encoding="utf-8")
    assert contacts == (tmp_path / "social-2" / "social_contacts.jsonl").read_text(encoding="utf-8")
