"""Sections that collect cycles less often restore the thresholds when the last one ends."""

from __future__ import annotations

import gc

from sheltersim.core.collection import fewer_collections


def test_nested_sections_raise_the_threshold_and_restore_it_once() -> None:
    before = gc.get_threshold()
    with fewer_collections():
        raised = gc.get_threshold()
        assert raised[0] > before[0]
        with fewer_collections():
            assert gc.get_threshold() == raised
        assert gc.get_threshold() == raised
    assert gc.get_threshold() == before


def test_a_failing_section_restores_the_threshold() -> None:
    before = gc.get_threshold()
    try:
        with fewer_collections():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert gc.get_threshold() == before
