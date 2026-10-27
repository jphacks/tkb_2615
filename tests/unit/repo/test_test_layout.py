"""Test files stay in the scope directories described in docs/testing.md."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).parents[2]

# docs/testing.md "テストの置き場所と名前": tests/unit/<owner>/ uses these owners.
UNIT_OWNERS = frozenset(
    {
        "core",
        "domain",
        "events",
        "policies",
        "experiments",
        "region",
        "llm",
        "storage",
        "web",
        "cli",
        "repo",
    }
)
SCOPE_DIRECTORIES = ("integration", "e2e", "live")

# Files written before the layout existed. Move one when you change it and
# remove it here; never add to this list.
UNSORTED = frozenset(
    {
        "test_nl_demo_ui.py",
        "test_pdf_report.py",
        "test_resume_checkpoint_loads.py",
        "test_web_demo.py",
        "test_web_inspector.py",
        "test_web_routes.py",
    }
)


def _test_files() -> list[Path]:
    return sorted(TESTS.rglob("test_*.py"))


def _uses_live_marker(path: Path) -> bool:
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "live_api"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


def test_no_new_test_file_is_added_to_the_top_level() -> None:
    top_level = {path.name for path in TESTS.glob("test_*.py")}

    assert top_level - UNSORTED == set(), (
        "put new tests in tests/unit/<owner>/, tests/integration/ or tests/e2e/"
    )


def test_the_unsorted_list_names_only_files_still_at_the_top_level() -> None:
    top_level = {path.name for path in TESTS.glob("test_*.py")}

    assert UNSORTED - top_level == set(), "remove moved files from UNSORTED"


def test_test_files_live_only_in_the_scope_directories() -> None:
    misplaced = []
    for path in _test_files():
        parts = path.relative_to(TESTS).parts
        if len(parts) == 1:
            continue
        if len(parts) == 3 and parts[0] == "unit" and parts[1] in UNIT_OWNERS:
            continue
        if len(parts) == 2 and parts[0] in SCOPE_DIRECTORIES:
            continue
        misplaced.append("/".join(parts))

    assert misplaced == []


def test_paid_live_tests_are_exactly_the_files_in_tests_live() -> None:
    # The API-key gate in sheltersim.llm.config opens only for tests/live/test_live_api.py.
    marked = {
        "/".join(path.relative_to(TESTS).parts) for path in _test_files() if _uses_live_marker(path)
    }
    in_live = {
        "/".join(path.relative_to(TESTS).parts) for path in (TESTS / "live").glob("test_*.py")
    }

    assert marked == in_live == {"live/test_live_api.py"}
