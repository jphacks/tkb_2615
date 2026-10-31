"""An index file keeps answering whichever way its key-only tables were made.

entities and tokens are made without rowids now. An index made before has them as rowid tables with
an index on the key, and keeps that form: same version, same queries, same answers. A query without
an ORDER BY of its own would read rowid order from the old form and key order from the new one, so
the answers are compared as whole documents, in order, page by page, on the same Run: from an old
file written by the same code, and from one whose rowids run against the keys, where anything
leaning on rowid order shows.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage import history_index
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore, digest

END = 120
FORK_MINUTE = 30
BRANCHES = ("main", "capacity")
KINDS = (
    "events",
    "kpis",
    "social",
    "staff",
    "actions",
    "transitions",
    "dialogue",
    "care",
    "operations",
)
_KEY_ONLY = ("entities", "tokens")


def _old_schema() -> str:
    schema = history_index._SCHEMA
    assert schema.count(") WITHOUT ROWID;") == len(_KEY_ONLY)
    return schema.replace(") WITHOUT ROWID;", ");")


def _sql_of(path: Path, table: str) -> str:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]


def _has_rowids(path: Path) -> bool:
    forms = {"WITHOUT ROWID" not in _sql_of(path, table) for table in _KEY_ONLY}
    assert len(forms) == 1
    return forms.pop()


def _index_of(root: Path) -> HistoryIndex:
    return HistoryIndex(ProjectStore(root), "p", "r")


def _build(root: Path, scenario: ScenarioSpec, saved_timeline, program_of) -> None:
    _store, timeline = saved_timeline(root, scenario)
    timeline.advance("main", END, checkpoint_interval=15)
    program = program_of(2, start_minute=FORK_MINUTE)
    timeline.fork(
        "main",
        FORK_MINUTE,
        "capacity",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    timeline.advance("capacity", END, checkpoint_interval=15)
    index = _index_of(root)
    for number in range(9):
        turn = {
            "turn_id": f"turn-{8 - number}",
            "conversation_id": f"talk-{number % 2}",
            "minute": number,
        }
        index.add_turn("main", turn)


@pytest.fixture(scope="module")
def formats(tmp_path_factory, generated_previews, add_toilets_program, saved_timeline):
    """The same Run with the index in each form: {name: projects root}."""
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    base = tmp_path_factory.mktemp("formats")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        _build(base / "new", scenario, saved_timeline, add_toilets_program)
        # Written by the same code as an old file was: its tables exist when the code comes.
        monkeypatch.setattr(history_index, "_SCHEMA", _old_schema())
        _build(base / "old", scenario, saved_timeline, add_toilets_program)
    # The rows again, with rowids in the opposite order of the keys.
    shutil.copytree(base / "old", base / "against")
    path = _index_of(base / "against").path
    with sqlite3.connect(path) as db:
        for table in _KEY_ONLY:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY 1 DESC,2 DESC,3 DESC,4 DESC,5 DESC")
            rows = rows.fetchall()
            db.execute(f"DELETE FROM {table}")
            marks = ",".join("?" * 5)
            db.executemany(f"INSERT INTO {table} VALUES({marks})", rows)
    return {name: base / name for name in ("new", "old", "against")}


def _pages(index: HistoryIndex, branch: str, anchor: str, **fields: Any) -> list[dict[str, Any]]:
    """Every page of a query as the API returns it, cursors and all, with limit 7."""
    pages = []
    cursor = None
    while True:
        query = HistoryQuery(end=END, limit=7, cursor=cursor, **fields)
        page = index.search(branch, query, anchor)
        pages.append(page)
        cursor = page.get("next_cursor")
        if not cursor:
            return pages


def _values(root: Path, branch: str) -> dict[str, Any]:
    """Filter values that exist in this Run: every text a filter can match, in sorted order."""
    index = _index_of(root)
    with index.connect() as db:
        tokens = [row[0] for row in db.execute("SELECT DISTINCT token FROM tokens ORDER BY token")]
        entities = [
            row[0] for row in db.execute("SELECT DISTINCT entity FROM entities ORDER BY entity")
        ]
        events = [
            row[0]
            for row in db.execute(
                "SELECT event_id FROM records WHERE branch=? AND kind='events' "
                "ORDER BY minute,key LIMIT 400",
                (branch,),
            )
        ]
        staff = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT e.entity FROM entities e JOIN records r ON r.branch=e.branch "
                "AND r.kind=e.kind AND r.key=e.key WHERE json_extract(r.payload,'$.staff_id')"
                "=e.entity ORDER BY 1"
            )
        ]
    return {
        "words": [token for token in tokens if not token.startswith("@event:")][:12],
        "agents": [item for item in entities if item.startswith("A0")][:6],
        "staff": staff[:3],
        "zones": [item for item in entities if "sleeping" in item or "reception" in item][:3],
        "households": [item for item in entities if item.startswith("H0")][:3],
        "events": events[:: max(len(events) // 6, 1)],
    }


def _answers(root: Path, values: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every query on entities and tokens that the index has, on every branch of the Run."""
    index = _index_of(root)
    store = index.store
    answers: dict[str, Any] = {}
    for branch in BRANCHES:
        head = store.branch_reference("p", "r", branch).checkpoint_hash
        known = values[branch]

        def ask(label: str, **fields: Any) -> None:
            answers[f"{branch}/{label}"] = _pages(index, branch, head, **fields)

        for kind in KINDS:
            ask(f"{kind}", kind=kind)
        for agent in known["agents"]:
            for kind in ("events", "actions", "social", "dialogue", "care"):
                ask(f"{kind} agent {agent}", kind=kind, agent_id=agent)
        for staff in known["staff"]:
            ask(f"operations staff {staff}", kind="operations", staff_id=staff)
        for zone in known["zones"]:
            ask(f"actions zone {zone}", kind="actions", zone_id=zone)
        for household in known["households"]:
            ask(f"people {household}", kind="events", household_id=household)
        for word in known["words"]:
            for kind in ("events", "actions"):
                ask(f"{kind} text {word}", kind=kind, text=word)
        for event in known["events"]:
            ask(f"events event {event}", kind="events", event_id=event)
            ask(f"causal {event}", kind="causal", event_id=event)
            ask(f"related {event}", kind="causal", event_id=event, include_related=True)
        answers[f"{branch}/checkpoints"] = index.checkpoints(branch, FORK_MINUTE, END, 100, None)
        answers[f"{branch}/nearest"] = [
            index.nearest(branch, minute, head, END) for minute in (FORK_MINUTE, 44, END)
        ]
    for conversation in ("talk-0", "talk-1", None):
        for limit in (1, 3, 100):
            answers[f"tail {conversation} {limit}"] = index.conversation_tail(
                "main", conversation, limit
            )
    answers["conversations"] = _pages(
        index,
        "main",
        store.branch_reference("p", "r", "main").checkpoint_hash,
        kind="conversations",
        conversation_id="talk-0",
    )
    return answers


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def test_the_two_forms_are_what_the_comparison_needs(formats) -> None:
    assert not _has_rowids(_index_of(formats["new"]).path)
    assert _has_rowids(_index_of(formats["old"]).path)
    assert _has_rowids(_index_of(formats["against"]).path)
    with sqlite3.connect(_index_of(formats["against"]).path) as db:
        first = db.execute("SELECT key FROM entities ORDER BY rowid LIMIT 1").fetchone()[0]
        last = db.execute("SELECT key FROM entities ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert first > last
        assert db.execute("PRAGMA user_version").fetchone()[0] == history_index._SCHEMA_VERSION


@pytest.mark.parametrize("other", ["old", "against"])
def test_every_query_answers_the_same_in_order_from_an_old_file(formats, other: str) -> None:
    values = {branch: _values(formats["new"], branch) for branch in BRANCHES}
    empty = [name for name, found in values["main"].items() if not found]
    assert empty == []
    new = _answers(formats["new"], values)
    answered = _answers(formats[other], values)
    assert new.keys() == answered.keys()
    assert sum(len(page["items"]) for page in new["main/events"]) > 7
    assert any(page["items"] for key, pages in new.items() if " agent " in key for page in pages)
    assert any(page["items"] for key, pages in new.items() if " text " in key for page in pages)
    assert any(
        page["items"]
        for key, pages in new.items()
        if key.startswith("main/causal")
        for page in pages
    )
    assert [turn["turn_id"] for turn in new["tail talk-0 3"]] == ["turn-4", "turn-6", "turn-8"]
    different = [key for key in new if _text(new[key]) != _text(answered[key])]
    assert different == []


def test_each_table_holds_the_same_rows_in_each_form(formats) -> None:
    def rows(name: str) -> dict[str, list[Any]]:
        with sqlite3.connect(_index_of(formats[name]).path) as db:
            return {
                table: sorted(db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
                for table in ("records", *_KEY_ONLY, "heads", "checkpoints", "generations")
            }

    new = rows("new")
    assert new == rows("old") == rows("against")
    assert new["entities"]
    assert new["tokens"]


def test_an_old_file_stays_old_and_takes_writes_and_a_rebuild(formats, tmp_path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(formats["old"], root)
    index = _index_of(root)
    index.add_turn("main", {"turn_id": "turn-9", "conversation_id": "talk-0", "minute": 9})
    assert index.conversation_tail("main", "talk-0", 1)[0]["turn_id"] == "turn-9"
    index.add_turn("capacity", {"turn_id": "turn-9", "conversation_id": "talk-0", "minute": 9})
    with sqlite3.connect(index.path) as db:
        stored = db.execute("SELECT COUNT(*) FROM entities WHERE key='turn-9'").fetchone()[0]
    assert stored > 0
    assert _has_rowids(index.path)

    rebuilt = tmp_path / "rebuilt"
    shutil.copytree(formats["new"], rebuilt)
    old_rebuilt = tmp_path / "old-rebuilt"
    shutil.copytree(formats["old"], old_rebuilt)
    results = []
    for path in (rebuilt, old_rebuilt):
        target = _index_of(path)
        with target.connect(write=True) as db:
            db.execute("UPDATE heads SET hash='stale' WHERE branch='main'")
        target.rebuild("main")
        with sqlite3.connect(target.path) as db:
            results.append(
                [
                    sorted(db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
                    for table in ("records", *_KEY_ONLY)
                ]
            )
    assert results[0] == results[1]
    assert _has_rowids(_index_of(old_rebuilt).path)
    assert not _has_rowids(_index_of(rebuilt).path)
