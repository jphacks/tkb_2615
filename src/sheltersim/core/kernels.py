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

# benchmarks/rule_shadow/text_check.py sets this to compare a kernel's expected outcome
# (EventSuppressed, EventApplicationError: saved texts) with what the Python rule raises for the
# same input. It has before(python_rule, arguments, options) -> token, called before the kernel,
# and after(python_rule, arguments, options, error, token) on a ValueError of the kernel. None:
# nothing is called.
_outcome_check: Any = None
# benchmarks/rule_shadow/error_compare.py sets this, in the same form, to compare the kernels' other
# errors in a Python rule's words (ValueError, StateInvariantError, a contract's: the exits of lane
# L8b in native/src/core/errors.rs) with the rule's. Those come before any change, so its before()
# keeps nothing. benchmarks/rule_shadow/refusal_census.py sets the last to count what the Python
# rule raised after a refusal: fell_back(label, refusal, error). None: nothing is called.
_error_check: Any = None
_fell_back: Any = None


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
        label = f"kernel_first {module}.{name}"

        @wraps(python_rule)
        def run(*arguments: Any, **options: Any) -> Any:
            check = _outcome_check
            other = _error_check
            seen = _fell_back
            if check is None and other is None and seen is None:
                try:
                    return kernel(*arguments, **options)
                except unsupported:
                    return python_rule(*arguments, **options)
            return _checked(
                kernel, python_rule, label, check, other, seen, unsupported, arguments, options
            )

        return run  # type: ignore[return-value]

    return decorate


def _checked(
    kernel: Any,
    python_rule: Any,
    label: str,
    check: Any,
    other: Any,
    seen: Any,
    unsupported: type[Exception],
    arguments: tuple[Any, ...],
    options: dict[str, Any],
) -> Any:
    """`run` of kernel_first while a tool watches it: the same result, and the tools' calls."""
    token = None if check is None else check.before(python_rule, arguments, options)
    mine = None if other is None else other.before(python_rule, arguments, options)
    try:
        return kernel(*arguments, **options)
    except unsupported as refusal:
        if seen is None:
            return python_rule(*arguments, **options)
        try:
            return python_rule(*arguments, **options)
        except Exception as error:
            seen.fell_back(label, refusal, error)
            raise
    except Exception as error:
        if check is not None and isinstance(error, ValueError):
            check.after(python_rule, arguments, options, error, token)
        if other is not None:
            other.after(python_rule, arguments, options, error, mine)
        raise
