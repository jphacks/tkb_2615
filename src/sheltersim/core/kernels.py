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
# same input: check(python_rule, arguments, options, error). None, nothing is called.
Check = Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any], Exception], None]
_outcome_check: Check | None = None
# benchmarks/rule_shadow/error_compare.py sets this to compare any other error a kernel raised in a
# Python rule's words (native/src/core/errors.rs) with the rule's: check(python_rule, arguments,
# options, error). benchmarks/rule_shadow/refusal_census.py sets the second to count what the
# Python rule raised after a refusal: seen(label, refusal, error). None, nothing is called.
_error_check: Check | None = None
_fell_back: Callable[[str, Exception, Exception], None] | None = None


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
            try:
                return kernel(*arguments, **options)
            except unsupported as refusal:
                try:
                    return python_rule(*arguments, **options)
                except Exception as error:
                    seen = _fell_back
                    if seen is not None:
                        seen(label, refusal, error)
                    raise
            except Exception as error:
                if isinstance(error, ValueError):
                    check = _outcome_check
                    if check is not None:
                        check(python_rule, arguments, options, error)
                other = _error_check
                if other is not None:
                    other(python_rule, arguments, options, error)
                raise

        return run  # type: ignore[return-value]

    return decorate
