"""A test that patches a function a Rust kernel replaced carries `@pytest.mark.python_rules`.

The patch would otherwise reach nothing: the kernel runs, the patched function is never called, and
a test that only asserts what the patch didn't see can pass. tests/support/python_rules.py has the
names, and the mark. This reads each test file's AST. What patches a name counts: `setattr` of
`monkeypatch` or the builtin, `patch`, and `patch.object`, with a string for the name. It can't see
a name in a variable, except a `for` over a tuple of strings directly around the patch, or an
assignment to a class's attribute.

A patch in a helper or fixture needs the mark on every test that reaches it by name, or on the file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support.python_rules import PORTED_METHODS, PORTED_NAMES, segment_steps

TESTS = Path(__file__).parents[2]
SOURCE = Path(__file__).parents[3] / "src"


def _owner_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _names_of(node: ast.expr, loops: dict[str, list[str]]) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return loops.get(node.id, [])
    return []


def _patches(call: ast.Call, loops: dict[str, list[str]]) -> list[tuple[str | None, str]]:
    """(the class or module, the name) of each thing this call may patch."""
    function = call.func
    name = (
        function.attr
        if isinstance(function, ast.Attribute)
        else function.id
        if isinstance(function, ast.Name)
        else None
    )
    if name not in {"setattr", "patch", "object", "delattr"} or not call.args:
        return []
    if name == "object" and _owner_name(function.value) != "patch":  # type: ignore[union-attr]
        return []
    first = call.args[0]
    # A dotted string: monkeypatch.setattr("a.b.c", value), patch("a.b.c").
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        parts = first.value.split(".")
        return [(parts[-2] if len(parts) > 1 else None, parts[-1])]
    if len(call.args) < 2:
        return []
    return [(_owner_name(first), text) for text in _names_of(call.args[1], loops)]


def _is_ported(owner: str | None, name: str, steps: frozenset[str]) -> bool:
    # A step patched on the controller replaces the kernel too, so that patch does reach something.
    if name in steps:
        return owner != "OfflineSimulationController"
    return name in PORTED_NAMES or (owner, name) in PORTED_METHODS


def _has_mark(decorators: list[ast.expr]) -> bool:
    return any("python_rules" in ast.unparse(decorator) for decorator in decorators)


def _ported_patches(function: ast.AST, steps: frozenset[str]) -> list[tuple[int, str]]:
    found = []
    loops: dict[str, list[str]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, (ast.Tuple, ast.List)):
                loops[node.target.id] = [
                    item.value
                    for item in node.iter.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            for owner, name in _patches(node, loops):
                if _is_ported(owner, name, steps):
                    found.append((node.lineno, name))
    return found


def violations(source: str, steps: frozenset[str] = frozenset()) -> list[str]:
    """Each patch of a ported name that some test reaches without the mark."""
    tree = ast.parse(source)
    if any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        )
        and "python_rules" in ast.unparse(node.value)
        for node in tree.body
    ):
        return []
    # Each function with whether it or its class has the mark.
    functions: dict[str, tuple[ast.AST, bool]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = (node, _has_mark(node.decorator_list))
        elif isinstance(node, ast.ClassDef):
            marked = _has_mark(node.decorator_list)
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    mark = marked or _has_mark(item.decorator_list)
                    functions[f"{node.name}.{item.name}"] = (item, mark)

    def used_names(function: ast.AST) -> set[str]:
        names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
        if isinstance(function, ast.FunctionDef):
            names |= {argument.arg for argument in function.args.args}
        return names

    uses = {
        name: used_names(function) & functions.keys() for name, (function, _) in functions.items()
    }

    def is_test(name: str) -> bool:
        return name.rsplit(".", 1)[-1].startswith("test_")

    def tests_reaching(target: str) -> set[str]:
        found: set[str] = set()
        pending = [target]
        visited = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if is_test(current):
                found.add(current)
            pending.extend(name for name, used in uses.items() if current in used)
        return found

    problems = []
    for name, (function, _) in functions.items():
        for line, patched in _ported_patches(function, steps):
            reaching = tests_reaching(name)
            unmarked = sorted(test for test in reaching if not functions[test][1])
            if unmarked:
                problems.append(f"line {line}: {patched} in {name}, reached by {unmarked}")
    return problems


def test_no_test_patches_a_ported_name_without_the_mark() -> None:
    steps = segment_steps()
    problems = []
    for path in sorted(TESTS.rglob("*.py")):
        if path == Path(__file__):
            continue
        for problem in violations(path.read_text(encoding="utf-8"), steps):
            problems.append(f"{path.relative_to(TESTS)}: {problem}")
    assert not problems, "\n".join(problems)


def test_the_listed_names_exist() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SOURCE.rglob("*.py")))
    names = PORTED_NAMES | {name for _, name in PORTED_METHODS}
    assert not [name for name in sorted(names) if f"def {name}(" not in text]


def _lint(text: str) -> list[str]:
    return violations(text, frozenset({"_some_step"}))


def test_the_lint_finds_a_patch_without_the_mark() -> None:
    assert _lint("def test_a(monkeypatch):\n    monkeypatch.setattr(WorldView, 'from_world', f)\n")
    assert _lint("def test_a(monkeypatch):\n    monkeypatch.setattr(x, '_view_state_hash', f)\n")
    assert _lint(
        "def test_a(monkeypatch):\n    monkeypatch.setattr(UpdatePhase, '_some_step', f)\n"
    )
    assert _lint("def test_a():\n    patch('a.WorldView.from_world')\n")
    assert _lint("def test_a():\n    patch.object(a.WorldView, 'after_state_update')\n")
    assert _lint("def test_a(monkeypatch):\n    monkeypatch.setattr('a.b._view_state_hash', f)\n")
    assert _lint(
        "def test_a(monkeypatch):\n"
        "    for step in ('_some_step', 'other'):\n"
        "        monkeypatch.setattr(UpdatePhase, step, f)\n"
    )


def test_the_lint_follows_a_helper_or_fixture_to_its_tests() -> None:
    helper = "def _helper(monkeypatch):\n    monkeypatch.setattr(WorldView, 'from_world', f)\n"
    assert _lint(helper + "def test_a(monkeypatch):\n    _helper(monkeypatch)\n")
    assert _lint(helper + "def test_a(_helper):\n    pass\n")
    assert not _lint(
        helper
        + "@pytest.mark.python_rules\n"
        + "def test_a(monkeypatch):\n    _helper(monkeypatch)\n"
    )


def test_the_lint_takes_the_mark_on_a_test_a_class_or_the_file() -> None:
    patch = "    monkeypatch.setattr(WorldView, 'from_world', f)\n"
    assert not _lint(f"@pytest.mark.python_rules\ndef test_a(monkeypatch):\n{patch}")
    assert not _lint(f"pytestmark = pytest.mark.python_rules\ndef test_a(monkeypatch):\n{patch}")
    method = (
        "    def test_a(self, monkeypatch):\n"
        "        monkeypatch.setattr(WorldView, 'from_world', f)\n"
    )
    assert not _lint(f"@pytest.mark.python_rules\nclass TestA:\n{method}")


def test_the_lint_leaves_other_names_alone() -> None:
    assert not _lint("def test_a(monkeypatch):\n    monkeypatch.setattr(x, 'other', f)\n")
    assert not _lint(
        "def test_a(monkeypatch):\n"
        "    monkeypatch.setattr(OfflineSimulationController, '_some_step', f)\n"
    )
    assert not _lint(
        "def test_a(monkeypatch):\n    monkeypatch.setattr(BranchRuntime, 'from_world', f)\n"
    )
