"""How much did behaviour change? Compare the KPIs of two sets of multi-seed Runs.

Collect one set with each version of the code, then compare them:

    python benchmarks/compare_behaviour.py --collect A_DIR
    python benchmarks/compare_behaviour.py --collect B_DIR    (with the other code)
    python benchmarks/compare_behaviour.py A_DIR B_DIR

The result is a Markdown table for people who do not read the code. Different seeds of the
same code also differ, so each difference is shown next to that seed noise. To see what
"nothing changed" looks like, compare two sets of other seeds from the same code:

    python benchmarks/compare_behaviour.py --collect A --first-seed 2026
    python benchmarks/compare_behaviour.py --collect B --first-seed 3026
    python benchmarks/compare_behaviour.py A B

Run from the repository root with src on PYTHONPATH. Output belongs outside the repository.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SEEDS = 10
DEFAULT_MINUTES = 2880
DEFAULT_EVACUEES = 100
# A difference counts as larger than seed noise beyond this many standard errors. With about
# 30 KPIs, one or two pass it by chance even when nothing changed.
NOISE_THRESHOLD = 2.0


@dataclass(frozen=True)
class Kpi:
    """A number each Run ends with: over the whole Run, from one column of its KPI files."""

    name: str
    column: str
    how: str  # "mean" over the minutes, "last" value, or "max" over the minutes
    label: str | None = None  # None: take the label the results screen uses


KPIS = (
    Kpi("stress_mean", "stress_mean", "mean"),
    Kpi("stress_high_count", "stress_high_count", "mean"),
    Kpi("toilet_urgency_mean", "toilet_urgency_mean", "mean", "平均トイレニーズ"),
    Kpi("hunger_need_mean", "hunger_need_mean", "mean"),
    Kpi("hydration_need_mean", "hydration_need_mean", "mean"),
    Kpi("toilet_queue_length", "toilet_queue_length", "mean"),
    Kpi("toilet_active_users", "toilet_active_users", "mean"),
    Kpi("toilet_wait_count", "toilet_wait_count", "last", "トイレ待ちの回数"),
    Kpi("toilet_wait_mean_minutes", "toilet_wait_mean_minutes", "last"),
    Kpi("toilet_wait_max_minutes", "toilet_wait_max_minutes", "last", "最長トイレ待ち時間"),
    Kpi("water_queue_length", "water_queue_length", "mean"),
    Kpi("meal_queue_length", "meal_queue_length", "mean"),
    Kpi("water_service_wait_count", "water_service_wait_count", "last", "給水待ちの回数"),
    Kpi(
        "water_service_wait_mean_minutes",
        "water_service_wait_mean_minutes",
        "last",
        "平均給水待ち時間",
    ),
    Kpi(
        "water_service_wait_max_minutes",
        "water_service_wait_max_minutes",
        "last",
        "最長給水待ち時間",
    ),
    Kpi("meal_service_wait_count", "meal_service_wait_count", "last", "食事待ちの回数"),
    Kpi(
        "meal_service_wait_mean_minutes",
        "meal_service_wait_mean_minutes",
        "last",
        "平均食事待ち時間",
    ),
    Kpi(
        "meal_service_wait_max_minutes", "meal_service_wait_max_minutes", "last", "最長食事待ち時間"
    ),
    Kpi("water_outage_stall_count", "water_outage_stall_count", "last", "断水で止まった給水の数"),
    Kpi("resource_stock_stall_count", "resource_stock_stall_count", "last", "在庫切れで止まった数"),
    Kpi("water_ml_remaining", "water_ml_remaining", "last"),
    Kpi("food_meal_units_remaining", "food_meal_units_remaining", "last"),
    Kpi("staff_busy_count", "staff_busy_count", "mean"),
    Kpi("staff_load_ratio", "staff_load_ratio", "mean"),
    Kpi("unresolved_support_request_count", "unresolved_support_request_count", "last"),
    Kpi("support_no_qualified_worker_count", "support_no_qualified_worker_count", "last"),
    Kpi(
        "support_no_qualified_worker_request_minutes",
        "support_no_qualified_worker_request_minutes",
        "last",
    ),
    Kpi(
        "support_fairness_promoted_assignment_count",
        "support_fairness_promoted_assignment_count",
        "last",
    ),
    Kpi(
        "support_wait_staff_busy_request_minutes", "support_wait_staff_busy_request_minutes", "last"
    ),
    Kpi(
        "support_wait_staff_off_shift_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
        "last",
    ),
)
HOW_LABELS = {"mean": "期間平均", "last": "終了時", "max": "期間最大"}


def _labels() -> dict[str, str]:
    from sheltersim.web.results_chat import _FIELD_LABELS

    return {kpi.name: kpi.label or _FIELD_LABELS.get(kpi.column) or kpi.name for kpi in KPIS}


def _kpis_of_run(directory: Path) -> dict[str, float]:
    """The summary numbers of one saved Run directory."""
    columns: dict[str, list[float]] = {}
    for name in ("metrics.csv", "operational_kpis.csv"):
        with (directory / name).open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                for key, value in row.items():
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    columns.setdefault(key, []).append(number)
    found = {}
    for kpi in KPIS:
        values = columns.get(kpi.column)
        if not values:
            continue
        if kpi.how == "mean":
            found[kpi.name] = sum(values) / len(values)
        elif kpi.how == "max":
            found[kpi.name] = max(values)
        else:
            found[kpi.name] = values[-1]
    return found


def run_one_seed(seed: int, minutes: int, evacuees: int, output: Path) -> None:
    """In a process of its own: run one seed and write only its summary."""
    os.environ["SHELTERSIM_LLM_MODE"] = "offline"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gate_scenarios import generated, hashes_of, run_library

    from sheltersim.storage.projects import digest

    scenario = generated(evacuees=evacuees, minutes=minutes, seed=seed)
    with tempfile.TemporaryDirectory() as directory:
        world, metrics = run_library(scenario, Path(directory) / "run")
        kpis = _kpis_of_run(Path(directory) / "run")
    result = {
        "seed": seed,
        "minutes": minutes,
        "evacuees": evacuees,
        "scenario_hash": digest(json.loads(scenario.canonical_json())),
        **hashes_of(world, metrics),
        "kpis": kpis,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def collect(
    output: Path, *, seeds: int, first_seed: int, minutes: int, evacuees: int, jobs: int
) -> None:
    output.mkdir(parents=True, exist_ok=False)

    def one(seed: int) -> int:
        with (output / f"seed-{seed}.log").open("w", encoding="utf-8") as log:
            code = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--run-seed",
                    str(seed),
                    "--minutes",
                    str(minutes),
                    "--evacuees",
                    str(evacuees),
                    "--seed-output",
                    str(output / f"seed-{seed}.json"),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        if code == 0:
            (output / f"seed-{seed}.log").unlink()
        return code

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        codes = list(pool.map(one, range(first_seed, first_seed + seeds)))
    (output / "environment.json").write_text(
        json.dumps({"platform": platform.platform(), "python": platform.python_version()}) + "\n",
        encoding="utf-8",
    )
    failed = [first_seed + index for index, code in enumerate(codes) if code]
    if failed:
        raise SystemExit(f"these seeds failed; see their logs in {output}: {failed}")


def load_set(directory: Path) -> dict[int, dict[str, Any]]:
    runs = {}
    for path in sorted(directory.glob("seed-*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        runs[document["seed"]] = document
    if not runs:
        raise SystemExit(f"no seed-*.json in {directory}")
    return runs


def _number(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.3g}"


def _signed(value: float) -> str:
    return "0" if value == 0 else ("+" if value > 0 else "") + _number(value)


def _spread(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def compare_kpi(a: list[float], b: list[float], *, paired: bool) -> dict[str, Any]:
    """Means, seed-to-seed spread, and the difference in units of its standard error.

    With the same seeds on both sides each seed is compared with itself; otherwise the two
    sets are independent samples (Welch's standard error).
    """
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    difference = mean_b - mean_a
    if paired:
        steps = [y - x for x, y in zip(a, b, strict=True)]
        error = _spread(steps) / math.sqrt(len(steps)) if len(steps) > 1 else 0.0
    else:
        error = math.sqrt(_spread(a) ** 2 / len(a) + _spread(b) ** 2 / len(b))
    if difference == 0:
        beyond = False
    elif error == 0:
        beyond = True
    else:
        beyond = abs(difference) > NOISE_THRESHOLD * error
    return {
        "mean_a": mean_a,
        "spread_a": _spread(a),
        "mean_b": mean_b,
        "spread_b": _spread(b),
        "difference": difference,
        "standard_error": error,
        "relative": difference / abs(mean_a) if mean_a else None,
        "beyond_noise": beyond,
    }


def render(a_dir: Path, b_dir: Path, a: dict[int, Any], b: dict[int, Any]) -> tuple[str, list[str]]:
    """The Markdown report, and the names of the KPIs that moved beyond seed noise."""
    paired = set(a) == set(b)
    labels = _labels()
    identical = paired and all(
        a[seed]["world_hash"] == b[seed]["world_hash"]
        and a[seed]["metrics_hash"] == b[seed]["metrics_hash"]
        for seed in a
    )
    lines = [
        "# 振る舞いの比較",
        "",
        f"- A：`{a_dir}`（{len(a)}個のseed）",
        f"- B：`{b_dir}`（{len(b)}個のseed）",
    ]
    conditions = {(run["minutes"], run["evacuees"]) for run in (*a.values(), *b.values())}
    # The people are generated from the seed, so only the same seed can have the same input.
    same_input = not paired or all(
        a[seed]["scenario_hash"] == b[seed]["scenario_hash"] for seed in a
    )
    for minutes, evacuees in sorted(conditions):
        lines.append(f"- 条件：{evacuees}人、{minutes}分（{minutes / 1440:g}日）")
    if len(conditions) > 1 or not same_input:
        lines.append(
            "- **注意：AとBで入力（Scenario）が違います。差はモデルの差だけではありません。**"
        )
    lines.append(
        "- 比べ方："
        + (
            "同じseedどうしを比べました。"
            if paired
            else "seedが違うので、2つの集団として比べました。"
        )
    )
    lines += [
        "",
        "| KPI | 集計 | A 平均 | A ばらつき | B 平均 | B ばらつき | 差（B−A） | 差の割合 | 判定 |",
    ]
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    moved = []
    for kpi in KPIS:
        if not all(kpi.name in run["kpis"] for run in (*a.values(), *b.values())):
            continue
        ordered_a = [a[seed]["kpis"][kpi.name] for seed in sorted(a)]
        ordered_b = [b[seed]["kpis"][kpi.name] for seed in sorted(b)]
        row = compare_kpi(ordered_a, ordered_b, paired=paired)
        if row["difference"] == 0:
            verdict = "同じ"
        elif row["beyond_noise"]:
            verdict = "**揺れより大きい**"
            moved.append(kpi.name)
        else:
            verdict = "揺れの範囲"
        relative = "—" if row["relative"] is None else f"{100 * row['relative']:+.1f}%"
        lines.append(
            f"| {labels[kpi.name]} | {HOW_LABELS[kpi.how]} | {_number(row['mean_a'])} | "
            f"{_number(row['spread_a'])} | {_number(row['mean_b'])} | {_number(row['spread_b'])} | "
            f"{_signed(row['difference'])} | {relative} | {verdict} |"
        )
    lines += [
        "",
        "読み方：",
        "",
        "- 「ばらつき」は、同じコードでseed（乱数の種）だけを変えたときの標準偏差です。",
        f"- 「揺れより大きい」は、差が標準誤差の{NOISE_THRESHOLD:g}倍を超えたKPIです。"
        "何も変えていなくても、20個に1個ほどは偶然ここに入ります。",
        "- 差の大きさの良し悪しは、この表では決まりません。"
        "モデルの係数の多くは実測で校正されていません。",
        "",
    ]
    if identical:
        lines.append(
            "結論：**すべてのseedで、世界とKPIのハッシュまで同じです。振る舞いは変わっていません。**"
        )
    else:
        lines.append(
            f"結論：{len(moved)}個のKPIが、seedの揺れより大きく動きました"
            f"（{'、'.join(labels[name] for name in moved)}）。"
            if moved
            else "結論：すべてのKPIの差は、seedの揺れの範囲です。"
        )
    return "\n".join(lines) + "\n", moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("sets", nargs="*", type=Path, metavar="DIR", help="A_DIR B_DIR to compare")
    parser.add_argument("--collect", type=Path, metavar="DIR", help="run the seeds into DIR")
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    parser.add_argument("--first-seed", type=int, default=2026)
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    parser.add_argument("--evacuees", type=int, default=DEFAULT_EVACUEES)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--output", type=Path, help="also write the report to this file")
    parser.add_argument("--run-seed", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--seed-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_seed is not None:
        run_one_seed(args.run_seed, args.minutes, args.evacuees, args.seed_output)
        return 0
    if args.collect:
        collect(
            args.collect,
            seeds=args.seeds,
            first_seed=args.first_seed,
            minutes=args.minutes,
            evacuees=args.evacuees,
            jobs=args.jobs,
        )
        return 0
    if len(args.sets) != 2:
        parser.error("give A_DIR and B_DIR, or --collect DIR")
    report, _ = render(*args.sets, load_set(args.sets[0]), load_set(args.sets[1]))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
