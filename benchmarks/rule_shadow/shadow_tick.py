"""The Rust driver (rules/tick.rs) under the shadows, and how many ticks it drove in a plain run.

A shadow replaces a kernel or a step at its Python entry, and the driver calls neither, so a
shadowed run goes through `phase_handler`: `prepare` says so. The driver itself is compared with
the Python rules by the hashes, by benchmarks/first_difference.py --both, and by
tests/integration/test_tick_driver.py. `count_ticks` is for the plain run of
SHELTERSIM_DISPATCH_COUNT_ONLY=1.
"""

from __future__ import annotations

import atexit

from shadow_kit import Recorder


def prepare(_output: str) -> None:
    from sheltersim import native

    if native.available():
        native.rules("tick").python_path_only(True)


def count_ticks(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "tick")
    module = native.rules("tick")

    def report_counts() -> None:
        counts, reasons = module.stats()
        recorder.seen.update(counts)
        recorder.reasons.update(reasons)

    # After Recorder's own: atexit runs the last registered first.
    atexit.register(report_counts)
