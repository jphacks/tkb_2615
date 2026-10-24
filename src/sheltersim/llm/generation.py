"""Opus-backed, reviewable generation of normalized synthetic initial needs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.config import LLMMode
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import initial_needs_schema


def generate_initial_needs(
    scenario: ScenarioSpec,
    router: LLMRouter,
) -> ScenarioSpec:
    """Ask Opus for synthetic initial need levels and return a draft ScenarioSpec."""
    if router.mode is LLMMode.OFFLINE:
        raise ValueError("Claude initial-conditions generation requires record or replay mode")
    profile_inputs = [
        {
            "id": profile.id,
            "mobility": profile.mobility.value,
            "support_need": profile.support_need,
            "stress_sensitivity": profile.stress_sensitivity,
        }
        for profile in sorted(scenario.profiles, key=lambda item: item.id)
    ]
    prompt_payload = {
        "task": "Generate plausible but explicitly synthetic normalized initial need values.",
        "rules": [
            "Return each supplied id exactly once and do not add people.",
            "Values are experimental assumptions, not demographic predictions.",
            "Do not infer needs from group membership or identity; use only functional "
            "constraints.",
            "Return numbers from 0 to 1 for toilet_urgency, hunger, and fatigue.",
            "Return only the required JSON object.",
        ],
        "profiles": profile_inputs,
    }
    input_text = json.dumps(
        prompt_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text=input_text,
        state_hash=hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest(),
        prompt_version="initial-needs-v1",
        schema_version=1,
        output_schema=initial_needs_schema(len(scenario.profiles)),
        fallback_text="",
        # About 30 tokens per compact profile entry; leave room for spacing. The router
        # caps this at SHELTERSIM_LLM_MAX_OUTPUT_TOKENS and adds thinking headroom.
        max_output_tokens=max(256, len(scenario.profiles) * 48 + 256),
    )
    output = router.complete(task)
    result: Any = json.loads(output)
    expected_ids = {profile.id for profile in scenario.profiles}
    generated = {item["id"]: item["initial_needs"] for item in result["profiles"]}
    if set(generated) != expected_ids:
        raise ValueError("Opus output must contain each scenario Agent exactly once")

    document = json.loads(scenario.canonical_json())
    usage = router.records[-1]
    for profile in document["profiles"]:
        profile["initial_needs"] = generated[profile["id"]]
        profile["provenance"]["source_kind"] = "llm"
        profile["provenance"]["generator"] = "claude-opus-initial-needs-v1"
    document["provenance"]["llm_initial_needs"] = {
        "model_id": usage.model_id,
        "prompt_version": task.prompt_version,
        "schema_version": task.schema_version,
        "input_hash": task.input_hash,
        "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "estimated_cost_usd": usage.estimated_cost_usd,
        "cache_hit": usage.cache_hit,
        "assumption": "synthetic normalized inputs; not an empirical population model",
    }
    return ScenarioSpec.model_validate(document)
