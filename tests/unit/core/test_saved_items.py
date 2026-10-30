"""A saved list kept as item bytes reads like the list it was saved from (docs/rust-port.md, B1)."""

from __future__ import annotations

import copy
import json
import pickle

from sheltersim.core.checkpoint_cache import canonical_bytes
from sheltersim.core.saved_items import SavedItems

VALUES = [{"b": [1, 2.5], "a": "避難所"}, {"x": None}, {"y": {"z": True}}]


def _saved() -> SavedItems:
    return SavedItems([canonical_bytes(value) for value in VALUES])


def test_saved_items_read_as_the_values_they_were_saved_from() -> None:
    saved = _saved()
    assert len(saved) == 3
    assert saved[0] == VALUES[0] and saved[-1] == VALUES[-1]
    assert saved[1:] == VALUES[1:]
    assert list(saved) == VALUES
    assert saved == VALUES and saved == _saved()
    assert saved != VALUES[:2] and saved != [*VALUES[:2], {"x": 1}]


def test_every_read_parses_anew_so_no_reader_changes_another() -> None:
    saved = _saved()
    saved[0]["b"].append(3)
    next(iter(saved))["a"] = "changed"
    saved[:1][0].clear()
    assert saved == VALUES


def test_copies_share_the_immutable_bytes_and_pickle_round_trips() -> None:
    saved = _saved()
    copied = copy.deepcopy(saved)
    assert copied is not saved and copied.raws is not saved.raws
    assert all(map(lambda a, b: a is b, copied.raws, saved.raws))
    assert pickle.loads(pickle.dumps(saved)) == saved


def test_json_of_a_document_holding_saved_items_is_that_of_the_list() -> None:
    document = {"event_log": _saved(), "minute": 3}
    assert canonical_bytes(document) == canonical_bytes({"event_log": VALUES, "minute": 3})
    assert json.loads(canonical_bytes(document))["event_log"] == VALUES
