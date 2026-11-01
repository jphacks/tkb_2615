"""Find where two runs of a scenario first differ: minute, phase, part of the world, value.

Final hashes say only that something differs. This records a digest of each part of the
world after every phase (benchmarks/tick_probe.py) and compares two such streams. To show
values it runs the scenario again up to that phase on each side; no state is stored.

    python benchmarks/first_difference.py --record NAME --output A [--draws]
    python benchmarks/first_difference.py A B
    python benchmarks/first_difference.py --both NAME --output DIR [--draws]

NAME is a gate scenario or generated:MINUTES:EVACUEES. For A and B, change PYTHONPATH or
the environment between the recordings: each stream remembers how to run itself again.
--both records under SHELTERSIM_NATIVE=1 and =0 and compares the two. --draws also logs each
random draw and what it was compared with, which finds a probability that changed without
flipping any comparison. Exit status: 0 no difference, 1 a difference, 2 could not compare.

Run from the repository root with src on PYTHONPATH. Output belongs outside the repository.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

META = "meta.json"
DIGESTS = "digests.jsonl"
DRAWS = "draws.jsonl"
RESULT = "result.json"
# What decides the run besides the code, kept to run it again.
REMEMBERED_ENVIRONMENT = (
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "SHELTERSIM_NATIVE",
    "RAYON_NUM_THREADS",
)
HASHES = ("world_hash", "events_hash", "metrics_hash")


# Recording -------------------------------------------------------------------------------


def _scenario(name: str) -> Any:
    """A generated:MINUTES:EVACUEES[:SEED] run, or None for a gate scenario's name."""
    if not name.startswith("generated:"):
        return None
    from gate_scenarios import GateScenario, generated, single

    minutes, evacuees, *seed = name.split(":")[1:]
    options = {"seed": int(seed[0])} if seed else {}
    return GateScenario(
        name,
        f"{evacuees}人の生成Scenarioを{minutes}分",
        single(lambda: generated(evacuees=int(evacuees), minutes=int(minutes), **options)),
    )


def record(args: argparse.Namespace) -> int:
    import gate_all_paths
    import tick_probe

    output: Path = args.output.resolve()
    output.mkdir(parents=True, exist_ok=bool(args.dump_line is not None))
    dump = None
    if args.dump_line is not None:
        dump = (args.dump_line, json.loads(args.dump_parts), output / "dump.json")
    meta = {
        "scenario": args.record,
        "script": str(Path(__file__).resolve()),
        "python": sys.executable,
        "cwd": os.getcwd(),
        "environment": {key: os.environ.get(key) for key in REMEMBERED_ENVIRONMENT},
        "draws": args.draws,
        "reverse": args.reverse,
        "every_minutes": args.every_minutes,
        "minutes": args.minutes,
        "full_every_minutes": args.full_every_minutes,
    }
    (output / META).write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    minutes = tuple(int(part) for part in args.minutes.split(":")) if args.minutes else None
    stream = tick_probe.DigestStream(
        output / DIGESTS,
        dump=dump,
        every_minutes=args.every_minutes,
        minutes=minutes,
        full_every_minutes=args.full_every_minutes,
    )
    observers: list[Any] = []
    if args.reverse:
        observers.append(tick_probe.ReversedOrder(within_phase=args.reverse == "within_phase"))
    recorder = tick_probe.DrawRecorder(output / DRAWS) if args.draws else None
    if recorder is not None:
        observers.append(recorder)
    observers.append(stream)
    # The Rust side is what a Run does: the driver of rules/tick.rs, watched between its phases.
    tick_probe.install(observers, on_driver=not args.reverse)
    # A run that stops with an error still leaves what it wrote until then.
    try:
        gate_all_paths.run_one(args.record, output, _scenario(args.record))
    finally:
        stream.close()
        if recorder is not None:
            recorder.close()
    return 0


def _spawn_record(
    meta: dict[str, Any], output: Path, environment: dict[str, str | None], *extra: str
) -> subprocess.CompletedProcess[str]:
    """Record in a process of its own; a failing run is shown by its missing result."""
    command = [meta["python"], meta["script"], "--record", meta["scenario"], "--output"]
    command.append(str(output))
    if meta.get("draws"):
        command.append("--draws")
    if meta.get("reverse"):
        command += ["--reverse", meta["reverse"]]
    if meta.get("every_minutes", 1) != 1:
        command += ["--every-minutes", str(meta["every_minutes"])]
    if meta.get("minutes"):
        command += ["--minutes", meta["minutes"]]
    command += ["--full-every-minutes", str(meta["full_every_minutes"])]
    env = dict(os.environ)
    for key, value in environment.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [*command, *extra],
        cwd=meta["cwd"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


# Values ----------------------------------------------------------------------------------


def _label(item: Any) -> str | None:
    """What names a list item across runs, when it has an ID."""
    if not isinstance(item, dict):
        return None
    for field in ("id", "event_id", "agent_id", "request_id"):
        value = item.get(field)
        if isinstance(value, str):
            return f"{field}={value}"
    profile = item.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("id"), str):
        return f"id={profile['id']}"
    event = item.get("event")
    if isinstance(event, dict) and isinstance(event.get("event_id"), str):
        return f"event_id={event['event_id']}"
    return None


def _by_label(items: list[Any]) -> dict[str, Any] | None:
    labelled: dict[str, Any] = {}
    for item in items:
        label = _label(item)
        if label is None or label in labelled:
            return None
        labelled[label] = item
    return labelled


def value_text(value: Any) -> str:
    """A value, with the bits of a float."""
    if isinstance(value, float):
        return f"{value!r} (float {value.hex()})"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value!r} (int)"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 160 else f"{text[:160]}... ({len(text)} characters)"


MISSING = object()


def differences(first: Any, second: Any, path: str) -> Iterator[tuple[str, Any, Any]]:
    """Each leaf that differs, as (JSON path, value, value), in document order."""
    if isinstance(first, dict) and isinstance(second, dict):
        for key in sorted(first.keys() | second.keys()):
            yield from differences(
                first.get(key, MISSING), second.get(key, MISSING), f"{path}.{key}"
            )
        return
    if isinstance(first, list) and isinstance(second, list):
        labelled = (_by_label(first), _by_label(second))
        if labelled[0] is not None and labelled[1] is not None:
            for label in sorted(labelled[0].keys() | labelled[1].keys()):
                yield from differences(
                    labelled[0].get(label, MISSING),
                    labelled[1].get(label, MISSING),
                    f"{path}[{label}]",
                )
            return
        for index, (a, b) in enumerate(itertools.zip_longest(first, second, fillvalue=MISSING)):
            yield from differences(a, b, f"{path}[{index}]")
        return
    if first is MISSING or second is MISSING or type(first) is not type(second):
        yield path, first, second
    elif first != second and not (first != first and second != second):
        yield path, first, second


def _values_at(
    directory: Path, line: int, parts: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the recorded run again up to a line of its stream, and return that document."""
    meta = json.loads((directory / META).read_text(encoding="utf-8"))
    again = directory / f"values-{line}"
    if again.exists():
        shutil.rmtree(again)
    done = _spawn_record(
        meta,
        again,
        meta["environment"],
        "--dump-line",
        str(line),
        "--dump-parts",
        json.dumps(parts),
    )
    dump = again / "dump.json"
    if not dump.exists():
        return None, f"the run ended before that line: {done.stderr[-500:]}"
    document = json.loads(dump.read_text(encoding="utf-8"))
    shutil.rmtree(again)
    return document, None


# Comparing -------------------------------------------------------------------------------


def _first_differing_line(
    first: Path, second: Path
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None] | int:
    """The line where two streams first differ, or how many lines they share."""
    shared = 0
    with first.open(encoding="utf-8") as a, second.open(encoding="utf-8") as b:
        for x, y in itertools.zip_longest(a, b):
            if x != y:
                return (
                    shared,
                    json.loads(x) if x is not None else None,
                    json.loads(y) if y is not None else None,
                )
            shared += 1
    return shared


def _where(row: dict[str, Any]) -> str:
    return f"engine {row['e']}, minute {row['m']}, phase {row['p']}"


def compare_digests(first: Path, second: Path, *, max_values: int, values: bool) -> bool:
    """Print the first difference of two streams; whether there is one."""
    found = _first_differing_line(first / DIGESTS, second / DIGESTS)
    if isinstance(found, int):
        print(f"digests: the same at all {found} recorded phases")
        return False
    line, a, b = found
    print(f"digests: first difference at line {line} of the streams")
    if a is None or b is None:
        ended, row = ("A", b) if a is None else ("B", a)
        assert row is not None
        print(f"  stream {ended} ends here; the other goes on with {_where(row)}")
        return True
    print(f"  A: {_where(a)}")
    if (a["e"], a["m"], a["p"]) != (b["e"], b["m"], b["p"]):
        print(f"  B: {_where(b)}")
        print("  the runs reach different points, so their phases do not line up")
        return True
    parts = sorted(
        name for name in a["d"].keys() | b["d"].keys() if a["d"].get(name) != b["d"].get(name)
    )
    print(f"  parts that differ ({len(parts)}): {', '.join(parts) or a.get('w', '') or '?'}")
    if not values or not parts:
        return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        dumps = list(pool.map(lambda side: _values_at(side, line, parts), (first, second)))
    for side, (_, problem) in zip("AB", dumps, strict=True):
        if problem:
            print(f"  no values from {side}: {problem}")
    if dumps[0][0] is None or dumps[1][0] is None:
        return True
    for side, directory, (document, _), row in zip(
        "AB", (first, second), dumps, (a, b), strict=True
    ):
        for name in parts:
            if name in row["d"] and document["digests"][name] != row["d"][name]:
                print(f"  warning: {side} ({directory}) gave another {name} when run again")
    for name in parts:
        found_values = list(
            differences(
                dumps[0][0]["values"].get(name, MISSING),
                dumps[1][0]["values"].get(name, MISSING),
                name,
            )
        )
        print(f"  {name}: {len(found_values)} values differ")
        if name == "event_log" and not found_values:
            print("    (those appended in this phase are equal: an earlier record was edited)")
        for path, x, y in found_values[:max_values]:
            print(f"    {path}")
            print(f"      A: {'(absent)' if x is MISSING else value_text(x)}")
            print(f"      B: {'(absent)' if y is MISSING else value_text(y)}")
        if len(found_values) > max_values:
            print(f"    ... and {len(found_values) - max_values} more")
    return True


def _draw_groups(path: Path) -> Iterator[tuple[tuple[Any, ...], dict[str, list[Any]]]]:
    """The draws of each phase, by seed and key, in the order of the file."""
    label: tuple[Any, ...] | None = None
    group: dict[str, list[Any]] = {}
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            current = (row["e"], row["m"], row["p"])
            if current != label:
                if label is not None:
                    yield label, group
                label, group = current, {}
            key = json.dumps([row["s"], row["k"]], ensure_ascii=False, separators=(",", ":"))
            group.setdefault(key, []).append([row["d"], row["u"]])
    if label is not None:
        yield label, group


def _flipped(first: list[Any], second: list[Any]) -> bool:
    return any(
        len(x) == 3 and len(y) == 3 and x[2] != y[2]
        for a, b in zip(first, second, strict=False)
        for x, y in zip(a[1], b[1], strict=False)
    )


def compare_draws(first: Path, second: Path, *, one_side_allowed: bool, max_values: int) -> bool:
    """Print the first draw whose value or uses differ; whether a draw differs.

    A draw on one side only is a difference unless ``one_side_allowed``: Rust's draws are not
    logged, so under --both the Python side has draws the Rust side made in Rust.
    """
    a_groups = dict(_draw_groups(first / DRAWS))
    b_groups = dict(_draw_groups(second / DRAWS))
    common = different = flips = 0
    one_side: dict[str, int] = {"A": 0, "B": 0}
    examples: list[str] = []
    one_side_example: str | None = None
    first_place: str | None = None
    for label in itertools.chain(a_groups, (k for k in b_groups if k not in a_groups)):
        a, b = a_groups.get(label, {}), b_groups.get(label, {})
        for key in itertools.chain(a, (k for k in b if k not in a)):
            if key not in a or key not in b:
                side = "A" if key in a else "B"
                one_side[side] += 1
                if one_side_example is None:
                    one_side_example = f"{key} at {label}, only in {side}"
                continue
            common += 1
            if a[key] == b[key]:
                continue
            different += 1
            flipped = _flipped(a[key], b[key])
            flips += flipped
            if first_place is None:
                first_place = f"engine {label[0]}, minute {label[1]}, phase {label[2]}"
            if len(examples) < max_values:
                examples.append(
                    f"    {key}{' (the comparison flips)' if flipped else ''}\n"
                    f"      at engine {label[0]}, minute {label[1]}, phase {label[2]}\n"
                    f"      A: {json.dumps(a[key])}\n"
                    f"      B: {json.dumps(b[key])}"
                )
    print(
        f"draws: {common} on both sides; {different} differ in value or use, "
        f"{flips} of them flip a comparison; only in A {one_side['A']}, only in B {one_side['B']}"
    )
    if one_side_example:
        print(f"  first draw on one side: {one_side_example}")
    if first_place:
        print(f"  first difference: {first_place}")
        print("  each entry is [draw, [[operation, the other number (hex), result], ...]]")
        print("\n".join(examples))
    return bool(different or (not one_side_allowed and any(one_side.values())))


def compare_results(first: Path, second: Path) -> bool:
    """Print whether the final hashes agree; whether they differ."""
    if not (first / RESULT).exists() or not (second / RESULT).exists():
        print("final hashes: a run did not finish")
        return True
    a = json.loads((first / RESULT).read_text(encoding="utf-8"))
    b = json.loads((second / RESULT).read_text(encoding="utf-8"))
    different = [
        f"{run}: {key}"
        for run in sorted(a["runs"].keys() | b["runs"].keys())
        for key in HASHES
        if a["runs"].get(run, {}).get(key) != b["runs"].get(run, {}).get(key)
    ]
    if a.get("refused") != b.get("refused"):
        different.append("the refused interventions")
    print(f"final hashes: {'differ in ' + ', '.join(different) if different else 'the same'}")
    return bool(different)


def compare(
    first: Path,
    second: Path,
    *,
    max_values: int,
    values: bool = True,
    one_side_allowed: bool = False,
) -> int:
    if not (first / DIGESTS).exists() or not (second / DIGESTS).exists():
        print(f"no stream in {first} or {second}", file=sys.stderr)
        return 2
    print(f"A: {first}\nB: {second}")
    found = compare_digests(first, second, max_values=max_values, values=values)
    if (first / DRAWS).exists() and (second / DRAWS).exists():
        found = (
            compare_draws(first, second, one_side_allowed=one_side_allowed, max_values=max_values)
            or found
        )
    else:
        print("draws: not recorded on both sides")
    if compare_results(first, second):
        found = True
    return 1 if found else 0


def both(args: argparse.Namespace) -> int:
    """Record a scenario with the Rust kernels and without, and compare."""
    output: Path = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    meta = {
        "scenario": args.both,
        "script": str(Path(__file__).resolve()),
        "python": sys.executable,
        "cwd": os.getcwd(),
        "draws": args.draws,
        "reverse": args.reverse,
        "every_minutes": args.every_minutes,
        "minutes": args.minutes,
        "full_every_minutes": args.full_every_minutes,
    }
    sides = {"native": "1", "python": "0"}

    def one(side: str) -> tuple[str, subprocess.CompletedProcess[str], float]:
        started = time.perf_counter()
        done = _spawn_record(
            meta,
            output / side,
            {"SHELTERSIM_NATIVE": sides[side], "PYTHONHASHSEED": "1"},
        )
        return side, done, time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=2) as pool:
        for side, done, seconds in pool.map(one, sides):
            print(f"{side}: exit {done.returncode} in {seconds:.1f}s")
            if done.returncode != 0:
                print(done.stderr[-2000:], file=sys.stderr)
    first, second = output / "native", output / "python"
    if (first / RESULT).exists():
        result = json.loads((first / RESULT).read_text(encoding="utf-8"))
        if not result["native_kernels"]:
            print("the extension is not installed: both sides ran Python", file=sys.stderr)
            return 2
    return compare(first, second, max_values=args.max_values, one_side_allowed=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("streams", nargs="*", type=Path, metavar="STREAM", help="A and B")
    parser.add_argument("--record", help="record this scenario into --output")
    parser.add_argument("--both", help="record with and without the Rust kernels, and compare")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--draws", action="store_true", help="also log random draws and uses")
    parser.add_argument(
        "--reverse",
        choices=("phase", "within_phase"),
        help="reverse the order of dicts, to place a finding of the gate's --reversed-order",
    )
    parser.add_argument(
        "--every-minutes",
        type=int,
        default=1,
        help="one line per this many minutes, for a long run; then record again with --minutes",
    )
    parser.add_argument("--minutes", help="only record these minutes, as FROM:TO")
    parser.add_argument(
        "--full-every-minutes",
        type=int,
        default=60,
        help="how often old history items are hashed again, to see an edit of one (1: each minute)",
    )
    parser.add_argument("--max-values", type=int, default=12, help="values shown per part")
    parser.add_argument("--no-values", action="store_true", help="do not run again for values")
    parser.add_argument("--dump-line", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--dump-parts", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.record or args.both:
        if args.output is None or args.streams:
            parser.error("--record and --both take --output and no streams")
        return record(args) if args.record else both(args)
    if len(args.streams) != 2:
        parser.error("give two streams, or --record or --both")
    return compare(
        args.streams[0].resolve(),
        args.streams[1].resolve(),
        max_values=args.max_values,
        values=not args.no_values,
    )


if __name__ == "__main__":
    raise SystemExit(main())
