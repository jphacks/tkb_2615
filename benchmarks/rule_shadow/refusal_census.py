"""Count every refusal of the Rust kernels: where raised, why, who caught it, and from what state.

Replaces the reading of refusal texts (docs/rust-port/step4-plan.md) with measurement. Off unless
the variable is set; then, with this directory first on PYTHONPATH, sitecustomize.py installs it in
every process:

    SHELTERSIM_REFUSAL_CENSUS=DIR PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \
        python benchmarks/gate_all_paths.py --output OUT --expect fixtures/gate-all-paths.json
    SHELTERSIM_REFUSAL_CENSUS=DIR PYTHONPATH=... python -m pytest -p refusal_census -n auto ...
    python benchmarks/rule_shadow/refusal_census.py report OUT.json --root . \
        --tests DIR --gate DIR --week DIR

Rust (native/src/core/census.rs) counts each `Unsupported` where it is built, and each fallback of
`or_rule!` and each `__wrapped__` call-back, so the driver's path shows as well as `kernel_first`'s.
A wrapper on every function of `rules` and `state` counts what reaches a Python entry. Nothing here
changes a result: it reads, and the gate and 7-day hashes are checked with it on.

Each count has a bucket, from the state it came from:
  tick_ok        in a tick that finished, and so passed validate_after_tick
  tick_failed    in a tick that raised
  direct_valid, direct_invalid, direct_noworld
                 outside a tick, at a kernel's entry: world.validate() passed, failed, or no
                 WorldState among its arguments (with SHELTERSIM_REFUSAL_CENSUS_PROBE=1)
  outside        outside a tick and not probed

The report's `reach` for a row: run (reached in the gate or the 7 days), tests_tick (only tests, in
a finished tick of a test that doesn't patch or break anything in its own source), tests_direct
(only tests, a direct call on a world that validates, whose arguments the test made), tests_broken
(the rest). A fixture's source isn't read, so the two middle ones are upper bounds, and a row with
`caught` 0 is a check that discards the error rather than a refusal.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import json
import os
import re
import sys
import types
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BUCKETS = ("tick_ok", "tick_failed", "direct_valid", "direct_invalid", "direct_noworld", "outside")


class _Census:
    def __init__(self, output: str, native: Any, probe: bool) -> None:
        self.native = native
        self.probe = probe
        self.target = Path(output) / f"{os.getpid()}.jsonl"
        self.argv = " ".join(sys.argv[:4])
        self.rows: Counter[tuple[Any, ...]] = Counter()
        self.ticks = 0
        self.probed: dict[tuple[Any, ...], str] = {}
        self.world_type: type | None = None
        self.depth = 0

    def scope(self) -> str:
        current = os.environ.get("PYTEST_CURRENT_TEST")
        return current.rsplit(" (", 1)[0] if current else ""

    def collect(self, bucket: str, scope: str | None = None) -> None:
        raised, caught = self.native.census_take()
        if not raised and not caught:
            return
        where = self.scope() if scope is None else scope
        for file, line, reason, n in raised:
            self.rows["raised", where, bucket, file, line, reason, ""] += n
        for by, file, line, reason, n in caught:
            self.rows["caught", where, bucket, file, line, reason, by] += n

    def write(self) -> None:
        self.collect("outside")
        if not self.rows and not self.ticks:
            return
        with self.target.open("a", encoding="utf-8") as output:
            # The driver's own counts since the process began, to check the or_rule rows against.
            counts = dict(self.native.rules.tick.stats()[0])
            head = {"argv": self.argv, "ticks": self.ticks, "driver": counts, "pid": os.getpid()}
            output.write(json.dumps(head, ensure_ascii=False) + "\n")
            for row, n in self.rows.items():
                output.write(json.dumps([*row, n], ensure_ascii=False) + "\n")
        self.rows.clear()
        self.ticks = 0

    def bucket_of_direct(self, label: str, reason: str, values: tuple[Any, ...]) -> str:
        key = (self.scope(), label, reason)
        known = self.probed.get(key)
        if known is not None:
            return known
        world = None
        for value in values:
            for candidate in (value, getattr(value, "world", None)):
                if self.world_type is not None and isinstance(candidate, self.world_type):
                    world = candidate
                    break
            if world is not None:
                break
        if world is None:
            bucket = "direct_noworld"
        else:
            # Whatever the check itself refuses isn't what this refusal came from.
            self.native.census_switch(False)
            try:
                world.validate(validate_event_history=False)
                bucket = "direct_valid"
            except Exception:  # noqa: BLE001 - any failure says the state doesn't hold
                bucket = "direct_invalid"
            finally:
                self.native.census_switch(True)
        self.probed[key] = bucket
        return bucket


CENSUS: Any = None
UNSUPPORTED: Any = None


class _Entry:
    """A kernel's Python entry. Not a function, so a class attribute of it doesn't bind `self`."""

    __slots__ = ("__name__", "__wrapped__", "label")

    def __init__(self, kernel: Any, label: str) -> None:
        self.__wrapped__ = kernel
        self.__name__ = kernel.__name__
        self.label = label

    def __call__(self, *arguments: Any, **options: Any) -> Any:
        try:
            return self.__wrapped__(*arguments, **options)
        except UNSUPPORTED as refusal:
            census = CENSUS
            census.native.census_caught(self.label, str(refusal))
            if census.probe and census.depth == 0:
                values = (*arguments, *options.values())
                # What this call raised, before anything else can take its bucket.
                census.collect(census.bucket_of_direct(self.label, str(refusal), values))
            raise


def install(output: str) -> None:
    """Turn the census on in this process, before the simulator looks up its kernels."""
    global CENSUS, UNSUPPORTED
    if os.environ.get("SHELTERSIM_NATIVE", "1") == "0":
        return
    try:
        import sheltersim_native as native
    except ImportError:
        return
    Path(output).mkdir(parents=True, exist_ok=True)
    census = _Census(output, native, os.environ.get("SHELTERSIM_REFUSAL_CENSUS_PROBE") == "1")
    CENSUS = census
    UNSUPPORTED = native.Unsupported
    for group_name in ("rules", "state"):
        group = getattr(native, group_name)
        for module_name, module in vars(group).items():
            if isinstance(module, types.ModuleType):
                _wrap_module(module, f"{group_name}.{module_name}")
    native.census_switch(True)
    atexit.register(census.write)

    from sheltersim.core.engine import TickEngine
    from sheltersim.core.world import WorldState

    census.world_type = WorldState
    step = TickEngine.step

    def counted_step(self: Any) -> Any:
        census.collect("outside")
        census.depth += 1
        try:
            result = step(self)
        except BaseException:
            census.collect("tick_failed")
            raise
        finally:
            census.depth -= 1
        census.collect("tick_ok")
        census.ticks += 1
        return result

    TickEngine.step = counted_step  # type: ignore[method-assign]


def _wrap_module(module: Any, label: str) -> None:
    for name, value in list(vars(module).items()):
        if isinstance(value, types.BuiltinFunctionType) and name not in {"stats", "install"}:
            setattr(module, name, _Entry(value, f"{label}.{name}"))


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    """A pytest plugin (-p refusal_census): a worker of xdist may not reach atexit."""
    if CENSUS is not None:
        CENSUS.collect("outside", nodeid)
        CENSUS.write()


# ---- The report ----------------------------------------------------------------------------------

_SITE = re.compile(r"\b(?:unsupported|refuse)\s*(?:::<[^>]*>)?\(|\bUnsupported::new_err\(")
_FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?fn\s+(\w+)", re.M)
_TEXT = re.compile(r'\s*(?:format!\(\s*)?"((?:[^"\\]|\\.)*)"')
# What marks a test as one that breaks state or swaps a hook on purpose, in its own source.
_BREAKS = re.compile(
    r"dict\.__setitem__|list\.__setitem__|object\.__setattr__|\b_?taint|\.__dict__\[|corrupt|tamper"
    r"|SimpleNamespace|python_rules|\bnan\b|\binf\b|patch|use_python_entries|python_path_only"
)


def static_sites(root: Path) -> dict[tuple[str, int], dict[str, str]]:
    """Every place in native/src that builds a refusal, with its text and enclosing fn."""
    sites = {}
    for path in sorted((root / "native/src").rglob("*.rs")):
        text = path.read_text("utf-8")
        relative = path.relative_to(root / "native").as_posix()
        functions = [(m.start(), m.group(1)) for m in _FN.finditer(text)]
        macros = [m.start() for m in re.finditer(r"^macro_rules!", text, re.M)]
        for match in _SITE.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            before = text[start : match.start()]
            if "//" in before or re.search(r"\bfn\s+$", before):
                continue
            literal = _TEXT.match(text, match.end())
            line = text.count("\n", 0, match.start()) + 1
            enclosing = "(top)"
            for position, name in functions:
                if position > match.start():
                    break
                enclosing = name
            in_macro = any(
                position < match.start() and text.find("\n}\n", position) > match.start()
                for position in macros
            )
            sites[relative, line] = {
                "reason": literal.group(1) if literal else "(a value)",
                "fn": enclosing,
                "macro": in_macro,
            }
    return sites


def _test_sources(root: Path, ids: set[str]) -> dict[str, bool]:
    """Whether each test's own source breaks state on purpose. A fixture's source is not read."""
    by_file: dict[str, dict[str, str]] = {}
    answer = {}
    for test in ids:
        file, _, rest = test.partition("::")
        name = rest.split("[")[0].split("::")[-1]
        if file not in by_file:
            functions = {}
            path = root / file
            if path.exists():
                source = path.read_text("utf-8")
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.FunctionDef):
                        functions[node.name] = ast.get_source_segment(source, node) or ""
            by_file[file] = functions
        # The tracked-mirror tests run every kernel over containers no Run has.
        answer[test] = "tracked_mirror" in file or bool(_BREAKS.search(by_file[file].get(name, "")))
    return answer


def _read(directory: str | None) -> tuple[list[list[Any]], dict[str, int]]:
    rows: list[list[Any]] = []
    ticks = Counter()
    if directory is None:
        return rows, ticks
    for path in sorted(Path(directory).glob("*.jsonl")):
        last: dict[str, int] = {}
        for line in path.read_text("utf-8").splitlines():
            row = json.loads(line)
            if isinstance(row, dict):
                ticks["steps"] += row["ticks"]
                last = row.get("driver", last)
            else:
                rows.append(row)
        # A process's driver counts are cumulative: its last line has them all.
        ticks.update(last)
    return rows, dict(ticks)


def report(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root)
    sites = static_sites(root)
    runs = {"tests": arguments.tests, "gate": arguments.gate, "week": arguments.week}
    data: dict[Any, dict[str, Any]] = {}
    fallbacks: Counter[tuple[str, str]] = Counter()
    entries: dict[Any, Counter[str]] = defaultdict(Counter)
    ticks = {}

    def record(key: Any) -> dict[str, Any]:
        if key not in data:
            data[key] = {
                "counts": Counter(),
                "tests": defaultdict(Counter),
                "buckets": defaultdict(Counter),
            }
        return data[key]

    for run, directory in runs.items():
        rows, ticks[run] = _read(directory)
        for kind, scope, bucket, file, line, reason, by, n in rows:
            # A file is as rustc was given it, absolute for a module found by build.rs.
            file = file[file.rfind("native/") + len("native/") :] if "native/" in file else file
            if kind == "caught":
                if by.startswith(("or_rule", "__wrapped__")):
                    fallbacks[run, f"{by.split('(')[0].strip()}"] += n
                if file:
                    entries[file, line, reason][by.split("(")[0].strip()] += n
                continue
            item = record((file, line, reason))
            item["counts"][run] += n
            item["buckets"][run][bucket] += n
            if run == "tests":
                item["tests"][scope][bucket] += n
    all_tests = {test for item in data.values() for test in item["tests"] if test}
    breaks = _test_sources(root, all_tests)
    table = []
    for (file, line, reason), item in data.items():
        tests_tick = tests_direct = tests_other = 0
        tick_tests: list[str] = []
        direct_tests: list[str] = []
        for test, buckets in item["tests"].items():
            if breaks.get(test, False):
                tests_other += sum(buckets.values())
                continue
            tests_tick += buckets["tick_ok"]
            tests_direct += buckets["direct_valid"]
            tests_other += sum(buckets.values()) - buckets["tick_ok"] - buckets["direct_valid"]
            if buckets["tick_ok"]:
                tick_tests.append(test)
            if buckets["direct_valid"]:
                direct_tests.append(test)
        counts = item["counts"]
        if counts["gate"] or counts["week"]:
            reach = "run"
        elif tests_tick:
            reach = "tests_tick"
        elif tests_direct:
            reach = "tests_direct"
        else:
            reach = "tests_broken"
        site = sites.get((file, line), {})
        table.append(
            {
                "file": file,
                "line": line,
                "fn": site.get("fn", "(macro or helper)"),
                "reason": reason,
                "tests": counts["tests"],
                "gate": counts["gate"],
                "week": counts["week"],
                "reach": reach,
                "tests_tick": tests_tick,
                "tests_direct": tests_direct,
                "tests_other": tests_other,
                "tests_buckets": dict(sum((b for b in item["tests"].values()), Counter())),
                "test_examples": (
                    sorted(tick_tests) or sorted(direct_tests) or sorted(item["tests"])
                )[:3],
                "entries": entries[file, line, reason].most_common(3),
                # None caught: a check that discards the error, or a class's method's refusal.
                "caught": sum(entries[file, line, reason].values()),
            }
        )
    table.sort(key=lambda row: (-row["week"], -row["gate"], -row["tests"], row["reason"]))
    reached_lines = {(row["file"], row["line"]) for row in table}
    reached_texts = {row["reason"] for row in table if (row["file"], row["line"]) not in sites}
    never = [
        {"file": file, "line": line, **site}
        for (file, line), site in sorted(sites.items())
        if (file, line) not in reached_lines
        and not (site["macro"] and site["reason"] in reached_texts)
        and not (site["reason"] == "(a value)" and (file, line) in reached_lines)
    ]
    document = {
        "ticks": ticks,
        "sites": len(sites),
        "reached_sites": len(reached_lines),
        "never": never,
        "table": table,
        "fallbacks": sorted(
            ([run, by, n] for (run, by), n in fallbacks.items()), key=lambda r: (-r[2], r[0], r[1])
        ),
    }
    Path(arguments.output).write_text(json.dumps(document, ensure_ascii=False, indent=1), "utf-8")
    reaches = Counter(row["reach"] for row in table)
    print(f"ticks and driver counts {ticks}")
    print(f"sites {len(sites)}, reached {len(reached_lines)}, never {len(never)}")
    print(f"rows by reach: {dict(reaches)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("report")
    make.add_argument("output")
    make.add_argument("--root", default=".")
    make.add_argument("--tests")
    make.add_argument("--gate")
    make.add_argument("--week")
    return report(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
