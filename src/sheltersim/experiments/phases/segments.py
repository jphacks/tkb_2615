"""Rust segments that replace a step of a phase, so a lane doesn't edit the phase's steps.

A phase's `_run_*_phase` lists its steps, each a method. A lane adds one line to SEGMENTS, and the
controller's step tries the kernel first. A kernel gets the controller and the step's arguments,
and returns what the step returns. Unsupported, raised before it has changed anything, runs the
Python step, so it says what the kernel doesn't handle: a trace, a tainted namespace, a policy it
doesn't know. Without the Rust kernels, or with nothing listed, the steps stay the plain methods
and a tick has nothing to pay. See docs/rust-port/lanes/p4-world-core.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

from sheltersim import native

# Name of a step's method -> (the module of native/src/rules/, the name of its kernel).
SEGMENTS: dict[str, tuple[str, str]] = {
    "_update_long_term_psychology": ("psychology", "update_long_term"),
    "_social_propagation": ("social_propagation", "update_social_propagation"),
}


def install_segments(
    controller: type, segments: Mapping[str, tuple[str, str] | Callable[..., Any]] = SEGMENTS
) -> None:
    """Replace each listed step of this class. Its bases keep the Python step."""
    if not native.available():
        return
    for step, kernel in segments.items():
        if not callable(getattr(controller, step, None)):
            raise ValueError(f"the controller has no step {step}")
        if not callable(kernel):
            kernel = getattr(native.rules(kernel[0]), kernel[1])
        setattr(controller, step, _with_kernel(controller, step, kernel))


def _with_kernel(controller: type, step: str, kernel: Callable[..., Any]) -> Callable[..., Any]:
    python_step = getattr(controller, step)

    @wraps(python_step)
    def run(self: Any, *arguments: Any, **options: Any) -> Any:
        try:
            return kernel(self, *arguments, **options)
        except native.Unsupported:
            # Found on the bases at each call, so a test's patch of the Python step applies.
            return getattr(super(controller, self), step)(*arguments, **options)

    return run
