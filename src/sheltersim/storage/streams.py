"""Disk-backed trace ordering and bounded-memory artifact emission."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator


class TraceSpool:
    """Append sanitized trace documents; sort decisions without retaining proposals."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS records (sequence INTEGER PRIMARY KEY, stream TEXT "
            "NOT NULL, minute INTEGER, agent TEXT, policy TEXT, trigger TEXT, choice REAL, "
            "payload TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS ordered_records ON records(stream, minute, agent, "
            "policy, trigger, choice, sequence)"
        )
        self.counts: dict[str, int] = {}
        self.pending = 0

    def append(self, stream: str, record: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO records(stream,minute,agent,policy,trigger,choice,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                stream,
                record.get("minute_index", 0),
                record.get("agent_id", ""),
                record.get("policy_version", ""),
                record.get("trigger", ""),
                record.get("choice_value", 0),
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            ),
        )
        self.counts[stream] = self.counts.get(stream, 0) + 1
        self.pending += 1
        if self.pending >= 128:
            self.connection.commit()
            self.pending = 0

    def records(self, stream: str, *, ordered: bool = False) -> Iterator[dict[str, Any]]:
        order = "minute,agent,policy,trigger,choice,sequence" if ordered else "sequence"
        for row in self.connection.execute(
            f"SELECT payload FROM records WHERE stream=? ORDER BY {order}", (stream,)
        ):
            yield json.loads(row[0])

    def close(self, *, remove: bool = False) -> None:
        self.connection.commit()
        self.connection.close()
        if remove:
            self.path.unlink(missing_ok=True)
