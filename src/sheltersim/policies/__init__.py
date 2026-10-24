"""Replaceable simulation policies, imported lazily to keep domain modules acyclic."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ActionCandidate": ".behavior",
    "ActionKind": ".behavior",
    "ActionProposal": ".behavior",
    "BehaviorDecisionRunner": ".behavior",
    "BehaviorPolicy": ".behavior",
    "UtilityBehaviorPolicy": ".behavior",
    "WorldView": ".behavior",
    "AgentStateDelta": ".psychology",
    "PsychologyPolicy": ".psychology",
    "SimplePsychologyPolicy": ".psychology",
    "apply_state_deltas": ".psychology",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
