"""Classify recorded LLM call outcomes so rule fallbacks are visible in Run summaries."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from sheltersim.llm.contracts import LLMCallRecord

# Version of the block returned by summarize_call_outcomes. Manifests written before
# this block existed have no "outcomes" entry and an undercounting "failures" field.
LLM_OUTCOME_SUMMARY_SCHEMA_VERSION = 1

ACCEPTED_OUTCOMES = frozenset({"recorded", "cache_hit"})
OFFLINE_RULE_OUTCOME = "offline"


def summarize_call_outcomes(records: Iterable[LLMCallRecord | Mapping[str, Any]]) -> dict[str, Any]:
    """Count accepted, rule-fallback and offline-rule calls without reading model text.

    Status values:
    - not_requested: no LLM task was issued.
    - offline_rules: every task used the configured rule because the mode was offline.
    - all_accepted: every requested task used a validated provider or tape response.
    - degraded: some requested tasks fell back to the rule.
    - all_fallback: no requested task used a provider or tape response.
    """
    accepted = offline_rules = 0
    by_reason: Counter[str] = Counter()
    by_error_kind: Counter[str] = Counter()
    by_prompt_version: Counter[str] = Counter()
    first_fallback_call_index: int | None = None
    call_count = 0
    for raw in records:
        record = raw.to_dict() if isinstance(raw, LLMCallRecord) else raw
        call_count += 1
        outcome = str(record.get("outcome", "unknown"))
        if outcome == OFFLINE_RULE_OUTCOME:
            offline_rules += 1
            continue
        if outcome in ACCEPTED_OUTCOMES and not record.get("fallback_used"):
            accepted += 1
            continue
        by_reason[outcome] += 1
        by_error_kind[str(record.get("error_kind") or outcome)] += 1
        by_prompt_version[str(record.get("prompt_version", "unknown"))] += 1
        if first_fallback_call_index is None:
            index = record.get("call_index")
            first_fallback_call_index = index if isinstance(index, int) else call_count - 1
    fallback = sum(by_reason.values())
    if call_count == 0:
        status = "not_requested"
    elif accepted + fallback == 0:
        status = "offline_rules"
    elif fallback == 0:
        status = "all_accepted"
    elif accepted == 0:
        status = "all_fallback"
    else:
        status = "degraded"
    return {
        "summary_schema_version": LLM_OUTCOME_SUMMARY_SCHEMA_VERSION,
        "status": status,
        "call_count": call_count,
        "accepted_count": accepted,
        "fallback_count": fallback,
        "offline_rule_count": offline_rules,
        "fallback_by_reason": dict(sorted(by_reason.items())),
        "fallback_by_error_kind": dict(sorted(by_error_kind.items())),
        "fallback_by_prompt_version": dict(sorted(by_prompt_version.items())),
        "first_fallback_call_index": first_fallback_call_index,
    }


def fallback_warning(summary: Mapping[str, Any]) -> str | None:
    """Return a one-line operator warning when requested tasks fell back to rules."""
    if summary.get("status") not in {"degraded", "all_fallback"}:
        return None
    requested = summary["accepted_count"] + summary["fallback_count"]
    reasons = ", ".join(
        f"{reason}: {count}" for reason, count in summary["fallback_by_reason"].items()
    )
    return (
        f"LLM fallback: {summary['fallback_count']} of {requested} requested tasks used the "
        f"rule fallback instead of a Claude response ({reasons}). Results for those tasks "
        "match offline rules."
    )
