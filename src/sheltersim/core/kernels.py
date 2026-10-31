"""Run a domain rule in Rust first, for rules that other modules call as plain functions.

A step of a phase's list goes in experiments/phases/segments.py. A function such as
staff_dynamics_on_shift is called from many modules and from tests, so it wraps itself where it is
defined, and every caller gets the kernel. See docs/rust-port/lanes/s-staff-dynamics.md.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from sheltersim import native

Rule = TypeVar("Rule", bound=Callable[..., Any])


def kernel_first(module: str, name: str) -> Callable[[Rule], Rule]:
    """Try native/src/rules/<module>.rs's kernel, and on Unsupported run the decorated Python rule.

    A kernel says Unsupported before it changes anything, so the Python rule then runs as if the
    kernel weren't there and raises its own text. Without the kernels the rule is returned as it
    is and a call has nothing to pay. `__wrapped__` is the Python rule.

    The kernel is looked up once, here, so benchmarks/rule_shadow replaces it before this runs.
    """

    def decorate(python_rule: Rule) -> Rule:
        if not native.available():
            return python_rule
        kernel = getattr(native.rules(module), name)
        unsupported = native.Unsupported

        @wraps(python_rule)
        def run(*arguments: Any, **options: Any) -> Any:
            try:
                return kernel(*arguments, **options)
            except unsupported:
                return python_rule(*arguments, **options)

        return run  # type: ignore[return-value]

    return decorate
