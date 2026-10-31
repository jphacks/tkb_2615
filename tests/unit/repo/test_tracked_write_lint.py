"""The tick-side code keeps no plain alias of what it inserts into model state.

The Rust port keeps model_state in tracked containers that forward each write (docs/rust-port.md
and docs/rust-port/lanes/p1-tracked-mirror.md). A plain container is converted when it is
inserted, so a later write through the name it had before the insert reaches neither the tree nor
Rust: `record = relationships[key] = {...}` followed by `record["trust"] = x` drifted at minute 10.
With plain dicts the same code works, so nothing else fails until then.

Write `relationships[key] = {...}` and then `record = relationships[key]`.

This lint reads one function at a time, in line order. It can't see:
- a function that gets the container as an argument and writes to it after its caller inserted it,
  or a caller that inserts what a function returned and the function kept a reference
- an alias kept in an attribute, a global or a closure of another function
- an alias through a local container that is itself inserted later
- loops where the write is on an earlier line than the insert
- `setattr`, `operator.setitem`, `getattr(d, "update")` and writes in native code
tests/support/tracked_mirror.py finds those at run time, on the scenarios it runs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[3] / "src" / "sheltersim"
TICK_SIDE = (
    "contracts",
    "core",
    "domain",
    "events",
    "experiments",
    "policies",
    "llm/runtime.py",
)
MUTATORS = frozenset(
    {
        "append",
        "clear",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)
FRESH_CALLS = frozenset({"dict", "list", "defaultdict", "Counter", "OrderedDict", "deque", "set"})
FRESH_NODES = (ast.Dict, ast.List, ast.DictComp, ast.ListComp, ast.Set, ast.SetComp)
BYPASS_OWNERS = frozenset({"dict", "list"})
HEAP_WRITES = frozenset({"heappush", "heappop", "heapify", "heapreplace", "heappushpop"})

# (file, function, name): why this is not the bug. A list that only shrinks: an entry the lint no
# longer finds fails the test below, as in tests/unit/repo/test_source_manifest.py.
NOT_INSERTED_INTO_STATE: dict[tuple[str, str, str], str] = {
    ("contracts/policies.py", "selected_models", "selection"): "memo of parsed objects",
    ("core/checkpoint_cache.py", "_leaf", "raw"): "encoder memo; bytes",
    ("core/checkpoint_cache.py", "_tree_node", "node"): "encoder memo; a str",
    ("domain/geometry_overlay.py", "_parsed_config", "parsed"): "memo of parsed objects",
    ("domain/movement.py", "plan_in_room_route", "route"): "memo of a route, not world state",
    ("domain/population.py", "queue_arrivals", "admitted"): "an int",
    ("domain/staff.py", "assign_pending", "qualified"): "local memo of StaffState lists",
    ("domain/staff.py", "qualified_for", "qualified"): "local memo of StaffState lists",
    ("domain/staff.py", "assign_pending", "person"): "a StaffState put in a local list",
    ("domain/staff_operations.py", "order_staff_operation", "task_id"): "a str",
    ("events/queue.py", "__deepcopy__", "copied"): "deepcopy's memo",
    ("experiments/metrics.py", "capture", "snapshot"): "a MetricSnapshot in MetricsRecorder",
    ("experiments/runner.py", "_physically_closed_edges", "closed"): "memo of the controller",
    ("policies/behavior.py", "_zone_environment", "view"): "memo of a parsed view",
}
# Not state either: a validation memo in __dict__, a local memo of StaffState lists, and memos of
# parsed views and of people's fields.
CHAINS_ON_MEMOS = frozenset(
    {
        "core/world.py memo = self.__dict__['_tick_memo'] = TickValidationMemo()",
        "domain/staff.py qualified = qualified_by_kind[support_kind] = [staff for sta",
        "policies/behavior.py view = views[zone_id] = zone_environment_view(world, zone_id",
        "policies/behavior.py entry = cache[identity] = (*entry, _fields_of_person(context",
    }
)


@dataclass(frozen=True)
class Finding:
    file: str
    function: str
    name: str
    insert_line: int
    use_line: int
    use: str

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.insert_line} {self.function}: {self.name} was inserted, "
            f"then {self.use} at line {self.use_line}"
        )


def _root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _names_in(node: ast.AST) -> set[str]:
    """The bare names an inserted value holds: the value itself, or inside a literal."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Dict):
        return {name for value in node.values for name in _names_in(value)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for value in node.elts for name in _names_in(value)}
    return set()


def _is_fresh(value: ast.AST) -> bool:
    if isinstance(value, FRESH_NODES):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in FRESH_CALLS
    )


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """The nodes of a function, with nested functions in, in source order."""
    nodes = [node for node in ast.walk(function) if hasattr(node, "lineno")]
    return sorted(nodes, key=lambda node: (node.lineno, node.col_offset))


def _rebinds(node: ast.AST) -> set[str]:
    found: set[str] = set()
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        # In `a = b[k] = c` the name stays the plain value: that form is the first finding.
        if not any(isinstance(item, (ast.Subscript, ast.Attribute)) for item in node.targets):
            targets = list(node.targets)
    elif isinstance(node, (ast.For, ast.comprehension)):
        targets = [node.target]
    elif isinstance(node, ast.NamedExpr):
        targets = [node.target]
    for target in targets:
        found.update(item.id for item in ast.walk(target) if isinstance(item, ast.Name))
    return found


def _inserted_names(node: ast.AST, local: set[str]) -> list[tuple[str, str]]:
    """(name, what it was inserted into) for an insert into a container not known to be local."""
    rows: list[tuple[str, ast.AST]] = []
    if isinstance(node, ast.Assign):
        subscripts = [item for item in node.targets if isinstance(item, ast.Subscript)]
        names = [item.id for item in node.targets if isinstance(item, ast.Name)]
        if subscripts:
            rows.extend((name, subscripts[0]) for name in _names_in(node.value))
            if not isinstance(node.value, ast.Constant):
                rows.extend((name, subscripts[0]) for name in names)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method in {"append", "insert", "setdefault", "extend"} and node.args:
            rows.extend((name, node.func) for name in _names_in(node.args[-1]))
        elif method == "update":
            for argument in [*node.args, *(item.value for item in node.keywords)]:
                rows.extend((name, node.func) for name in _names_in(argument))
    return [
        (name, ast.unparse(target))
        for name, target in rows
        if _root(target) is not None and _root(target) not in local
    ]


def _use_of(node: ast.AST) -> tuple[str, str] | None:
    """(name, what happens to it) if node writes through a name or hands it out."""
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    for target in targets:
        if isinstance(target, ast.Subscript):
            name = _root(target)
            if name is not None:
                return name, f"written as {ast.unparse(target)}"
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return node.target.id, f"changed by {ast.unparse(node.op.__class__())}=".replace("  ", " ")
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in MUTATORS
    ):
        name = _root(node.func.value)
        if name is not None:
            return name, f"changed by {ast.unparse(node.func)}()"
    if isinstance(node, (ast.Return, ast.Yield)) and isinstance(node.value, ast.Name):
        return node.value.id, "returned"
    return None


def _scan_function(file: str, function: ast.AST) -> list[Finding]:
    findings = []
    local: set[str] = set()
    inserted: dict[str, tuple[int, str]] = {}
    aliases: dict[str, set[str]] = {}
    for node in _own_nodes(function):
        use = _use_of(node)
        if use is not None and use[0] in inserted:
            line, _ = inserted.pop(use[0])
            findings.append(Finding(file, function.name, use[0], line, node.lineno, use[1]))
        for name, _target in _inserted_names(node, local):
            for held in {name, *aliases.get(name, ())}:
                inserted.setdefault(held, (node.lineno, _target))
        for name in _rebinds(node):
            inserted.pop(name, None)
            local.discard(name)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if _is_fresh(node.value):
                        local.add(target.id)
                    held = _names_in(node.value)
                    if held:
                        aliases[target.id] = held
    return findings


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _files() -> list[Path]:
    found: list[Path] = []
    for part in TICK_SIDE:
        path = ROOT / part
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def scan_source(file: str, source: str) -> list[Finding]:
    tree = ast.parse(source)
    seen: dict[tuple[str, str, int], Finding] = {}
    for function in _functions(tree):
        for finding in _scan_function(file, function):
            seen.setdefault((finding.name, finding.function, finding.insert_line), finding)
    return sorted(seen.values(), key=lambda item: (item.file, item.insert_line))


def scan_chains(file: str, source: str) -> list[str]:
    """`name = container[key] = value`, where the name stays the plain value."""
    rows = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(item, ast.Name) for item in node.targets)
            and any(isinstance(item, ast.Subscript) for item in node.targets)
            and not isinstance(node.value, ast.Constant)
        ):
            rows.append(f"{file} {ast.unparse(node)[:60]}")
    return rows


def scan_bypasses(file: str, source: str) -> list[str]:
    """Calls that write past a dict or list subclass's own methods."""
    rows = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Name)
            and owner.id in BYPASS_OWNERS
            and (node.func.attr in MUTATORS or node.func.attr.startswith("__"))
            and node.func.attr not in {"__getitem__", "__contains__", "__len__", "__iter__"}
        ):
            rows.append(f"{file}:{node.lineno} {ast.unparse(node.func)}")
        if (
            isinstance(owner, ast.Name)
            and owner.id == "heapq"
            and node.func.attr in HEAP_WRITES
            and node.args
            and isinstance(node.args[0], (ast.Subscript, ast.Attribute))
        ):
            rows.append(f"{file}:{node.lineno} {ast.unparse(node.func)}")
    return rows


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _all_findings() -> list[Finding]:
    found = []
    for path in _files():
        found.extend(scan_source(_relative(path), path.read_text(encoding="utf-8")))
    return found


def test_tick_side_code_keeps_no_plain_alias_of_what_it_inserts() -> None:
    new = [
        str(item)
        for item in _all_findings()
        if (item.file, item.function, item.name) not in NOT_INSERTED_INTO_STATE
    ]

    assert new == [], "read the name again from the container after the insert"


def test_the_list_of_exceptions_only_names_what_the_lint_still_finds() -> None:
    found = {(item.file, item.function, item.name) for item in _all_findings()}

    assert sorted(set(NOT_INSERTED_INTO_STATE) - found) == []


def test_tick_side_code_assigns_no_name_and_container_in_one_statement() -> None:
    rows = {
        row
        for path in _files()
        for row in scan_chains(_relative(path), path.read_text(encoding="utf-8"))
    }

    assert sorted(rows - CHAINS_ON_MEMOS) == []
    assert sorted(CHAINS_ON_MEMOS - rows) == []


def test_tick_side_code_writes_past_no_container_methods() -> None:
    rows = [
        row
        for path in _files()
        for row in scan_bypasses(_relative(path), path.read_text(encoding="utf-8"))
    ]

    assert rows == []


def _names_found(source: str) -> list[str]:
    return [item.name for item in scan_source("x.py", source)]


def test_the_lint_finds_a_write_after_an_insert() -> None:
    assert _names_found("def f(s):\n    r = {}\n    s['a'] = r\n    r['b'] = 1\n") == ["r"]
    assert _names_found("def f(s, r):\n    s.append(r)\n    r['b'] = 1\n") == ["r"]
    assert _names_found("def f(s, r):\n    s.setdefault('a', r)\n    r.update(b=1)\n") == ["r"]
    assert _names_found("def f(s, r):\n    s['a'] = r\n    r['b']['c'].append(1)\n") == ["r"]
    assert _names_found("def f(s, r):\n    s['a'] = {'x': r}\n    r.append(1)\n") == ["r"]
    held_by_a_local = "def f(s):\n    r = {}\n    t = {'x': r}\n    s['a'] = t\n    r['b'] = 1\n"
    assert _names_found(held_by_a_local) == ["r"]


def test_the_lint_finds_a_returned_alias() -> None:
    assert _names_found("def f(s):\n    r = {}\n    s['a'] = r\n    return r\n") == ["r"]


def test_the_lint_accepts_reading_the_name_again() -> None:
    assert _names_found("def f(s):\n    s['a'] = {}\n    r = s['a']\n    r['b'] = 1\n") == []
    read_again = "def f(s):\n    r = {}\n    s['a'] = r\n    r = s['a']\n    r['b'] = 1\n"
    assert _names_found(read_again) == []


def test_the_lint_accepts_writing_before_the_insert_and_local_containers() -> None:
    assert _names_found("def f(s):\n    r = {}\n    r['b'] = 1\n    s['a'] = r\n") == []
    assert _names_found("def f():\n    l = {}\n    r = {}\n    l['a'] = r\n    r['b'] = 1\n") == []


def test_the_chain_lint_finds_the_form_that_drifted() -> None:
    assert scan_chains("x.py", "r = s['a'] = {}\n") != []
    assert scan_chains("x.py", "s['a'] = r = 0\n") == []
    assert scan_chains("x.py", "s['a'] = {}\nr = s['a']\n") == []


def test_the_bypass_lint_finds_direct_writes() -> None:
    assert scan_bypasses("x.py", "dict.__setitem__(d, 'a', 1)\n") != []
    assert scan_bypasses("x.py", "dict.update(d, {})\n") != []
    assert scan_bypasses("x.py", "list.append(d, 1)\n") != []
    assert scan_bypasses("x.py", "heapq.heappush(state['heap'], 1)\n") != []
    assert scan_bypasses("x.py", "heapq.heappush(local_heap, 1)\n") == []
    assert scan_bypasses("x.py", "dict.fromkeys(d)\n") == []
