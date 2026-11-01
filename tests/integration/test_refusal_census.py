"""The refusal census counts where each Unsupported is built and who fell back. Off by default.

benchmarks/rule_shadow/refusal_census.py; docs/rust-port/step4-plan.md is what it measures for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest
from support.paths import BENCHMARKS, FIXTURES

from sheltersim import native

# Under a census of the tests themselves, its switch is not these tests' to turn.
pytestmark = pytest.mark.skipif(
    not native.available() or "SHELTERSIM_REFUSAL_CENSUS" in os.environ,
    reason="needs the Rust kernels, with no census running",
)

_ROOT = FIXTURES.parent
_SHADOW_DIR = BENCHMARKS / "rule_shadow"


def _refuse_once() -> None:
    import sheltersim_native

    with pytest.raises(sheltersim_native.Unsupported, match="not a plain JSON value"):
        sheltersim_native.canonical_json(object())


def test_nothing_is_counted_while_it_is_off() -> None:
    import sheltersim_native

    sheltersim_native.census_take()
    _refuse_once()
    assert sheltersim_native.census_take() == ([], [])


def test_a_refusal_is_counted_at_the_line_that_built_it() -> None:
    import sheltersim_native

    sheltersim_native.census_switch(True)
    try:
        _refuse_once()
        sheltersim_native.census_caught("somewhere", "value is not a plain JSON value")
        raised, caught = sheltersim_native.census_take()
    finally:
        sheltersim_native.census_switch(False)
    [(file, line, reason, times)] = raised
    assert file.endswith("lib.rs") and line > 0
    assert (reason, times) == ("value is not a plain JSON value", 1)
    assert caught == [("somewhere", file, line, reason, 1)]


def test_the_exception_is_the_one_python_catches() -> None:
    import sheltersim_native

    assert sheltersim_native.Unsupported.__name__ == "Unsupported"
    assert sheltersim_native.Unsupported.__module__ == "sheltersim_native"
    assert native.Unsupported is sheltersim_native.Unsupported


def test_without_the_variable_a_kernel_is_not_wrapped() -> None:
    assert isinstance(native.rules("tick").run_tick, types.BuiltinFunctionType)


def test_the_static_list_finds_sites_and_skips_definitions(tmp_path) -> None:
    sys.path.insert(0, str(_SHADOW_DIR))
    try:
        from refusal_census import static_sites
    finally:
        sys.path.remove(str(_SHADOW_DIR))
    source = tmp_path / "native" / "src"
    source.mkdir(parents=True)
    (source / "a.rs").write_text(
        "fn unsupported<T>(reason: &'static str) -> PyResult<T> {\n"
        "    Err(Unsupported::new_err(reason))\n"
        "}\n"
        "pub(crate) fn kernel() -> PyResult<()> {\n"
        '    // unsupported("a comment")\n'
        '    unsupported(\n        "a reason",\n    )\n'
        "}\n",
        "utf-8",
    )
    sites = static_sites(tmp_path)
    assert set(sites) == {("src/a.rs", 2), ("src/a.rs", 6)}
    assert sites["src/a.rs", 6]["reason"] == "a reason"
    assert sites["src/a.rs", 6]["fn"] == "kernel"


def test_a_run_of_the_driver_shows_its_fallbacks(tmp_path) -> None:
    """The first tick of a generated Run falls back in the driver, and so counts as or_rule."""
    census = tmp_path / "census"
    environment = {
        **os.environ,
        "SHELTERSIM_REFUSAL_CENSUS": str(census),
        "SHELTERSIM_LLM_MODE": "offline",
        "PYTHONPATH": os.pathsep.join(
            [str(_SHADOW_DIR), str(_ROOT / "src"), str(_ROOT / "native" / "pylib")]
        ),
    }
    environment.pop("PYTEST_CURRENT_TEST", None)
    subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "gate_all_paths.py"),
            "--output",
            str(tmp_path / "out"),
            "--only",
            "generated_project",
        ],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
    )
    rows = []
    ticks = 0
    for path in census.glob("*.jsonl"):
        for line in path.read_text("utf-8").splitlines():
            row = json.loads(line)
            if isinstance(row, dict):
                ticks += row["ticks"]
            else:
                rows.append(row)
    assert ticks > 0
    fallbacks = {row[6] for row in rows if row[0] == "caught"}
    assert any(by.startswith("or_rule ") for by in fallbacks)
    assert any(row[0] == "raised" and row[2] == "tick_ok" for row in rows)
