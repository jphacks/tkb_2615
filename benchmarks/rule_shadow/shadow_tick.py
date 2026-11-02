"""The Rust driver (rules/tick.rs) under the shadows, and how many ticks it drove in a plain run.

A shadow replaces a kernel or a step at its Python entry, and the driver calls neither, so a
shadowed run goes through `phase_handler`: `prepare` says so. The driver itself is compared with
the Python rules by the hashes, by benchmarks/first_difference.py --both, and by
tests/integration/test_tick_driver.py. `count_ticks` is for the plain run of
SHELTERSIM_DISPATCH_COUNT_ONLY=1.
"""

from __future__ import annotations

import atexit
import sys
from typing import Any

from shadow_kit import Recorder


def prepare(_output: str) -> None:
    from sheltersim import native

    if native.available():
        native.rules("tick").python_path_only(True)


def _driving_frame() -> bool:
    """Whether Python's caller is Rust, not a phase's method: no Python frame of a phase above."""
    frame = sys._getframe(2)
    return not frame.f_code.co_filename.endswith(
        ("arbitrate.py", "sample.py", "metrics.py", "update.py")
    )


def count_ticks(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "tick")
    module = native.rules("tick")
    # The Python parts a driven tick can reach: a patch of these leaves it driven.
    from sheltersim.experiments.runner import OfflineSimulationController

    for part in (
        "_replan_journeys",
        "_record_assigned_support",
        "_finalize_actions_at_end",
        "_consider_due_complaints",
    ):
        original = getattr(OfflineSimulationController, part)

        def counted(*values: Any, _part: str = part, _original: Any = original) -> Any:
            recorder.seen[f"part.{_part}.{'driven' if _driving_frame() else 'phases'}"] += 1
            return _original(*values)

        setattr(OfflineSimulationController, part, counted)

    def report_counts() -> None:
        counts, reasons = module.stats()
        recorder.seen.update(counts)
        recorder.reasons.update(reasons)
        checks, refusals = native.rules("after_tick").stats()
        recorder.seen["after_tick_ran"] += checks
        recorder.reasons.update({f"after_tick: {key}": n for key, n in refusals.items()})

    # After Recorder's own: atexit runs the last registered first.
    atexit.register(report_counts)
