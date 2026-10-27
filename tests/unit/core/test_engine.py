"""The execution order key breaks ties without depending on dict or input order.

The tick's phase order, validation and traces are checked by stepping the
TickEngine in tests/integration/test_tick_engine.py.
"""

from __future__ import annotations

from sheltersim.core.engine import DeterministicOrderKey


def test_execution_order_uses_agent_id_as_final_tiebreaker() -> None:
    later_id = DeterministicOrderKey(10, 1, 4, "A002")
    earlier_id = DeterministicOrderKey(10, 1, 4, "A001")
    earlier_sequence = DeterministicOrderKey(10, 1, 3, "A099")
    earlier_minute = DeterministicOrderKey(9, 99, 99, "A999")

    assert sorted((later_id, earlier_id, earlier_sequence, earlier_minute)) == [
        earlier_minute,
        earlier_sequence,
        earlier_id,
        later_id,
    ]
