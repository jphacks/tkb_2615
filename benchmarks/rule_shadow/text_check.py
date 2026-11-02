"""Compare each saved outcome text a kernel makes with its Python rule's for the same input.

`EventSuppressed` and `EventApplicationError` texts go to the EventLog as `detail`: they are saved
data. A kernel raises them through native/src/core/errors.rs. While the Python rules exist, this
runs the Python rule on the same input each time a kernel raises one, and compares the class and
the text. Both must raise before any change, so the second call sees what the first saw.
docs/rust-port/lanes/l8-texts-events.md.

    SHELTERSIM_TEXT_CHECK=DIR PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \
        python benchmarks/gate_all_paths.py --output OUT --expect fixtures/gate-all-paths.json
    SHELTERSIM_TEXT_CHECK=DIR PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \
        python -m pytest -p text_check -n auto ...
    python benchmarks/rule_shadow/text_check.py report OUT.json --root . --tests DIR --gate DIR ...

Two places reach a kernel's outcome: `kernel_first` (core/kernels.py `_outcome_check`) and the
dispatch loop's direct calls (`dispatch.texts_checker`), which return a text instead of raising.
With SHELTERSIM_TEXT_CHECK_STRICT=1 a difference raises AssertionError at once, so a test fails.

It also counts where Python raised one of the two (a frame on a `raise` line of src/): with the
kernels on, a site moved to Rust is then reached only inside a comparison.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import json
import linecache
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

CHECK: Any = None
CLASS_NAMES = ("EventSuppressed", "EventApplicationError")


class _Check:
    def __init__(self, output: str, native: Any, strict: bool) -> None:
        self.native = native
        self.strict = strict
        self.target = Path(output) / f"{os.getpid()}.jsonl"
        self.argv = " ".join(sys.argv[:4])
        self.rows: Counter[tuple[Any, ...]] = Counter()
        self.depth = 0
        self.last_python: tuple[str, int] | None = None
        self.classes: tuple[type, ...] = ()
        self.world_type: Any = None

    def scope(self) -> str:
        current = os.environ.get("PYTEST_CURRENT_TEST")
        return current.rsplit(" (", 1)[0] if current else ""

    # ---- The comparison --------------------------------------------------------------------------

    def compare(self, label: str, rust_class: str, rust_text: str, python_call: Any) -> None:
        last = self.native.texts_last()
        site = (
            (last[0], last[1])
            if last and last[2] == rust_class and last[3] == rust_text
            else ("", 0)
        )
        self.depth += 1
        self.last_python = None
        python_class = "returned"
        python_text = ""
        try:
            python_call()
        except ValueError as error:
            python_class = type(error).__name__
            python_text = str(error)
        finally:
            self.depth -= 1
        same = python_class == rust_class and python_text == rust_text
        python_site = self.last_python or ("", 0)
        self.rows["compared", site[0], site[1], rust_class, same, *python_site] += 1
        if not same:
            detail = {
                "label": label,
                "rust": [rust_class, rust_text],
                "python": [python_class, python_text],
                "site": list(site),
                "python_site": list(python_site),
                "scope": self.scope(),
            }
            self.rows["mismatch", json.dumps(detail, ensure_ascii=False, sort_keys=True)] += 1
            if self.strict:
                raise AssertionError(f"outcome text differs from the Python rule's: {detail}")

    # The Python rules that may raise after they changed the world: a deferral schedules its retry
    # on the world, then suppresses. Their comparison runs on a copy made before the kernel.
    AFTER_CHANGE = frozenset(
        {
            "EffectProgramPopulationHandler.apply",
            "apply_population_presence_command",
            "_apply_presence_action",
            "queue_arrivals",
            "queue_departures",
            "apply_arrival_approach_completion",
            "AgentTravelCompletionHandler.apply",
        }
    )

    def _world_of(self, arguments: tuple, options: dict) -> tuple[int, Any] | None:
        for index, value in enumerate(arguments):
            if isinstance(value, self.world_type):
                return index, value
        return None

    def _fingerprint(self, world: Any) -> tuple[Any, ...]:
        return (
            id(world.event_queue),
            id(world.model_state),
            tuple(sorted(world.id_counters.items())),
        )

    def before(self, python_rule: Any, arguments: tuple, options: dict) -> Any:
        if self.depth:
            return None
        found = self._world_of(arguments, options)
        if found is None:
            return None
        index, world = found
        if getattr(python_rule, "__qualname__", "") in self.AFTER_CHANGE:
            return index, self._fingerprint(world), self.world_type.from_dict(world.to_dict())
        return index, self._fingerprint(world), None

    def after(
        self, python_rule: Any, arguments: tuple, options: dict, error: ValueError, token: Any
    ) -> None:
        name = type(error).__name__
        if self.depth or name not in CLASS_NAMES or not isinstance(error, self.classes):
            return
        label = getattr(python_rule, "__qualname__", repr(python_rule))
        if token is not None:
            index, fingerprint, copy = token
            if copy is not None:
                arguments = (*arguments[:index], copy, *arguments[index + 1 :])
            elif self._fingerprint(arguments[index]) != fingerprint:
                # Raised after a change, and no copy was made: nothing to compare against.
                self.rows["changed", label, name, str(error)] += 1
                return
        self.compare(label, name, str(error), lambda: python_rule(*arguments, **options))

    def check_direct(self, handler: Any, world: Any, event: Any, status: str, text: str) -> None:
        if self.depth:
            return
        name = {"suppressed": "EventSuppressed", "failed": "EventApplicationError"}[status]
        apply = type(handler).apply
        python_rule = getattr(apply, "__wrapped__", apply)
        label = f"direct {type(handler).__name__}"
        self.compare(label, name, text, lambda: python_rule(handler, world, event))

    # ---- Where Python raised ---------------------------------------------------------------------

    def python_raised(self) -> None:
        frame = sys._getframe(2)
        file = frame.f_code.co_filename
        if "sheltersim" not in file:
            return
        line = linecache.getline(file, frame.f_lineno)
        if not any(f"{name}(" in line for name in CLASS_NAMES):
            return
        relative = file[file.rfind("src/sheltersim/") :] if "src/sheltersim/" in file else file
        site = (relative, frame.f_lineno)
        self.last_python = site
        self.rows["python", *site, "check" if self.depth else "run"] += 1

    def write(self) -> None:
        for row in self.native.texts_take():
            file, line, name, text, n = row
            self.rows["made", file, line, name, text] += n
        if not self.rows:
            return
        with self.target.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"argv": self.argv, "pid": os.getpid()}, ensure_ascii=False) + "\n"
            )
            for row, n in self.rows.items():
                output.write(json.dumps([*row, n], ensure_ascii=False) + "\n")
        self.rows.clear()


def install(output: str, strict: bool = False) -> None:
    """Turn the comparison on in this process."""
    global CHECK
    if os.environ.get("SHELTERSIM_NATIVE", "1") == "0":
        return
    from sheltersim import native

    if not native.available():
        return
    import sheltersim_native

    from sheltersim.core import kernels
    from sheltersim.core.world import WorldState
    from sheltersim.events import models

    Path(output).mkdir(parents=True, exist_ok=True)
    check = _Check(output, sheltersim_native, strict)
    check.classes = (models.EventSuppressed, models.EventApplicationError)
    check.world_type = WorldState
    CHECK = check
    for cls in check.classes:

        def init(self: Any, *arguments: Any, _base: Any = ValueError.__init__) -> None:
            check.python_raised()
            _base(self, *arguments)

        cls.__init__ = init  # type: ignore[misc]
    sheltersim_native.texts_switch(True)
    kernels._outcome_check = check
    native.rules("dispatch").texts_checker(check.check_direct)
    atexit.register(check.write)


def pytest_configure(config: Any) -> None:
    """A pytest plugin (-p text_check): on with SHELTERSIM_TEXT_CHECK, as sitecustomize does."""
    output = os.environ.get("SHELTERSIM_TEXT_CHECK")
    if output and CHECK is None:
        install(output, os.environ.get("SHELTERSIM_TEXT_CHECK_STRICT") == "1")


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    """A worker of xdist may not reach atexit."""
    if CHECK is not None:
        CHECK.write()


# ---- The report ----------------------------------------------------------------------------------

_EXIT = re.compile(r"\berrors::(suppressed|failed|error|text|noted)\s*(?:::<[^>]*>)?\(")


def rust_sites(root: Path) -> dict[tuple[str, int], str]:
    """Every call of an exit of core/errors.rs in native/src, with its file."""
    sites = {}
    for path in sorted((root / "native/src").rglob("*.rs")):
        if path.name == "errors.rs":
            continue
        text = path.read_text("utf-8")
        relative = path.relative_to(root / "native").as_posix()
        for match in _EXIT.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            if "//" in text[start : match.start()]:
                continue
            sites[relative, text.count("\n", 0, match.start()) + 1] = match.group(1)
    return sites


def python_sites(root: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Each `raise EventSuppressed(...)` and `raise EventApplicationError(...)` in src/."""
    sites = {}
    for path in sorted((root / "src/sheltersim").rglob("*.py")):
        source = path.read_text("utf-8")
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(source)
        scopes: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scopes.append((node.lineno, node.end_lineno or node.lineno, node.name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in CLASS_NAMES:
                continue
            enclosing = [s for s in scopes if s[0] <= node.lineno <= s[1]]
            enclosing.sort(key=lambda s: s[0])
            sites[relative, node.exc.lineno] = {
                "class": name,
                "end": node.end_lineno,
                "scope": ".".join(s[2] for s in enclosing),
                "text": ast.get_source_segment(source, node.exc.args[0]) if node.exc.args else "",
            }
    return sites


def _python_key(sites: dict[tuple[str, int], dict[str, Any]], file: str, line: int) -> Any:
    if (file, line) in sites:
        return file, line
    for (site_file, site_line), site in sites.items():
        if site_file == file and site_line <= line <= site["end"]:
            return site_file, site_line
    return file, line


def report(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root)
    exits = rust_sites(root)
    raises = python_sites(root)
    runs = {"tests": arguments.tests, "gate": arguments.gate, "week": arguments.week}
    made: dict[Any, Counter[str]] = defaultdict(Counter)
    compared: dict[Any, Counter[str]] = defaultdict(Counter)
    python: dict[Any, Counter[str]] = defaultdict(Counter)
    mismatches: list[dict[str, Any]] = []
    changed: Counter[str] = Counter()
    for run, directory in runs.items():
        if not directory:
            continue
        for path in sorted(Path(directory).glob("*.jsonl")):
            for line in path.read_text("utf-8").splitlines():
                row = json.loads(line)
                if isinstance(row, dict):
                    continue
                kind, *rest, n = row
                if kind == "made":
                    file, number = rest[0], rest[1]
                    file = (
                        file[file.rfind("native/") + len("native/") :]
                        if "native/" in file
                        else file
                    )
                    made[file, number][run] += n
                elif kind == "compared":
                    file, number, _name, same, *_python = rest
                    file = (
                        file[file.rfind("native/") + len("native/") :]
                        if "native/" in file
                        else file
                    )
                    compared[file, number][f"{run} {'same' if same else 'differ'}"] += n
                elif kind == "python":
                    file, number, phase = rest
                    python[_python_key(raises, file, number)][f"{run} {phase}"] += n
                elif kind == "changed":
                    changed[f"{rest[0]}: {rest[2]}"] += n
                elif kind == "mismatch":
                    mismatches.append({"run": run, "n": n, **json.loads(rest[0])})
    rust_rows = []
    for (file, line), exit_name in sorted(exits.items()):
        rust_rows.append(
            {
                "file": file,
                "line": line,
                "exit": exit_name,
                "made": dict(made.get((file, line), {})),
                "compared": dict(compared.get((file, line), {})),
            }
        )
    python_rows = []
    for (file, line), site in sorted(raises.items()):
        python_rows.append(
            {"file": file, "line": line, **site, "reached": dict(python.get((file, line), {}))}
        )
    unattributed = {f"{k[0]}:{k[1]}": dict(v) for k, v in compared.items() if k not in exits}
    document = {
        "rust_sites": rust_rows,
        "python_sites": python_rows,
        "compared_elsewhere": unattributed,
        "not_compared_after_a_change": dict(changed),
        "mismatches": mismatches,
    }
    Path(arguments.output).write_text(json.dumps(document, ensure_ascii=False, indent=1), "utf-8")
    compared_sites = sum(1 for row in rust_rows if any("same" in k for k in row["compared"]))
    reached_run = sum(1 for row in python_rows if any(k.endswith(" run") for k in row["reached"]))
    print(f"Rust exits {len(rust_rows)}, compared same at least once {compared_sites}")
    print(f"Python raise sites {len(python_rows)}, raised outside a comparison {reached_run}")
    print(f"mismatches {sum(m['n'] for m in mismatches)}")
    print(f"not compared, raised after a change: {sum(changed.values())}")
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
