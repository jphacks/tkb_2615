"""The arithmetic and wording of benchmarks/compare_behaviour.py on made-up results."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from support.paths import BENCHMARKS


@pytest.fixture(scope="module")
def compare():
    spec = importlib.util.spec_from_file_location(
        "compare_behaviour", BENCHMARKS / "compare_behaviour.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def _set(values: dict[int, float], **hashes: str) -> dict[int, dict]:
    return {
        seed: {
            "seed": seed,
            "minutes": 60,
            "evacuees": 5,
            "scenario_hash": f"scenario-{seed}",
            "world_hash": hashes.get("world_hash", f"world-{seed}-{value}"),
            "metrics_hash": hashes.get("metrics_hash", f"metrics-{seed}-{value}"),
            "kpis": {"stress_mean": value},
        }
        for seed, value in values.items()
    }


def test_independent_sets_use_the_standard_error_of_both_spreads(compare) -> None:
    row = compare.compare_kpi([1.0, 3.0], [2.0, 6.0], paired=False)

    assert row["mean_a"] == 2.0
    assert row["mean_b"] == 4.0
    assert row["difference"] == 2.0
    assert row["relative"] == 1.0
    assert row["standard_error"] == pytest.approx((2.0 / 2 + 8.0 / 2) ** 0.5)
    assert row["beyond_noise"] is False


def test_paired_sets_compare_each_seed_with_itself(compare) -> None:
    row = compare.compare_kpi([1.0, 5.0, 9.0], [2.0, 6.0, 10.0], paired=True)

    # The spread between seeds is large, but every seed moved by exactly one.
    assert row["difference"] == 1.0
    assert row["standard_error"] == 0.0
    assert row["beyond_noise"] is True


def test_no_baseline_gives_no_relative_change(compare) -> None:
    assert compare.compare_kpi([0.0, 0.0], [0.0, 1.0], paired=False)["relative"] is None


def test_a_report_names_a_kpi_beyond_seed_noise(compare) -> None:
    a = _set({1: 0.10, 2: 0.11, 3: 0.12})
    b = _set({1: 0.50, 2: 0.51, 3: 0.53})

    report, moved = compare.render(Path("a"), Path("b"), a, b)

    assert moved == ["stress_mean"]
    assert "**揺れより大きい**" in report
    assert "結論：1個のKPIが" in report
    assert "平均ストレス" in report


def test_different_conditions_are_flagged_in_the_report(compare) -> None:
    a = _set({1: 0.1, 2: 0.2})
    b = _set({1: 0.1, 2: 0.2})
    b[1]["scenario_hash"] = "another"

    report, _ = compare.render(Path("a"), Path("b"), a, b)

    assert "注意：AとBで入力" in report


def test_same_hashes_on_every_seed_say_nothing_changed(compare) -> None:
    a = _set({1: 0.1, 2: 0.2}, world_hash="w", metrics_hash="m")
    b = _set({1: 0.1, 2: 0.2}, world_hash="w", metrics_hash="m")

    report, moved = compare.render(Path("a"), Path("b"), a, b)

    assert moved == []
    assert "振る舞いは変わっていません" in report
