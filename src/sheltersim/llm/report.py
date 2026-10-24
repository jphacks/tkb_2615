"""Evidence-bound, schema-checked Opus run reviews with no model-authored prose."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMRouter

_ASSUMPTION_LABELS = {
    "ASM-SYNTHETIC-PROFILES-V1": "Synthetic profile distributions are assumptions.",
    "ASM-SIMPLE-PSYCHOLOGY-RATES-V1": "Psychology rates are uncalibrated assumptions.",
    "ASM-H04-CAUSAL-STRESS-V1": "Causal stress coefficients are bounded, uncalibrated assumptions.",
    "ASM-UTILITY-BEHAVIOR-V1": "Behavior thresholds and utilities are rule assumptions.",
    "ASM-PRIORITY-RULE-BEHAVIOR-V1": (
        "Priority-rule action selection and shared utilities are uncalibrated assumptions."
    ),
    "ASM-TOILET-SERVICE-DURATION-V1": "Toilet service duration bounds are assumptions.",
    "ASM-DAY3-TOILET-OUTAGE-V1": "Day 3 outage timing and scale are a synthetic stress test.",
    "ASM-SPACE-AND-STAFF-CAPACITY-V1": "Zone, staff and facility counts are illustrative inputs.",
    "ASM-SOCIAL-PROPAGATION-V1": "Social propagation coefficients are experimental assumptions.",
    "ASM-INFORMATION-SPREAD-V1": (
        "Information contact and inquiry coefficients are uncalibrated experimental assumptions."
    ),
    "ASM-STAFF-SUPPORT-LIFECYCLE-V1": (
        "Staff support durations and cooldowns are uncalibrated assumptions."
    ),
    "ASM-ZONE-ROUTING-V1": (
        "Route selection is deterministic; fixture travel minutes are illustrative."
    ),
    "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1": (
        "Stress trigger, cooldown and daily caps bound optional LLM proposals."
    ),
    "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1": (
        "Typed complaint, cooperation and information-verification effects are uncalibrated."
    ),
    "ASM-SYNTHETIC-ZONE-DISPLAY-V1": (
        "Fallback map coordinates are presentation-only synthetic layout."
    ),
    "ASM-H02-RESOURCE-SERVICE-V1": (
        "Resource service durations, quantities, need effects, thresholds, and utilities "
        "are assumptions."
    ),
    "ASM-H02-TEST-INITIAL-STOCK-V1": (
        "H02 initial stock quantities are synthetic software test inputs."
    ),
    "ASM-H02-QUEUE-STALLS-V1": (
        "Resource service queue stall reasons and retry order are software contracts."
    ),
    "ASM-H03-OPERATIONAL-EVENTS-V1": (
        "Operational event schedules and outages are synthetic, uncalibrated assumptions."
    ),
    "ASM-H05-OPERATIONAL-CAPACITY-V1": (
        "H05 facility, staffing and population capacities are illustrative inputs."
    ),
    "ASM-H05-OPERATIONAL-STOCK-V1": (
        "H05 initial and delivered stock amounts are synthetic scenario quantities."
    ),
    "ASM-H05-OPERATIONAL-SCHEDULE-V1": (
        "H05 event times and schedules are synthetic assumptions, not measured timelines."
    ),
    "ASM-H05-INTERVENTION-V1": (
        "H05 interventions are simulated settings and do not estimate real-world effects."
    ),
}
_HYPOTHESIS_LABELS = {
    "capacity_may_contribute_to_wait": "Capacity may contribute to observed queue or wait values.",
    "staff_load_may_affect_support": "Staff load may affect support throughput.",
    "stress_pattern_needs_validation": "The stress pattern needs validation against suitable data.",
    "event_may_explain_metric_change": "A recorded event may be relevant to a metric difference.",
    "branch_effect_is_descriptive": (
        "The branch difference is descriptive and needs repeated-seed study."
    ),
}
_RECOMMENDATION_LABELS = {
    "compare_capacity_intervention": (
        "Compare a capacity intervention against the same baseline conditions."
    ),
    "inspect_event_chain": "Inspect the cited event chain and its downstream state changes.",
    "repeat_across_seeds": "Repeat the paired comparison across multiple seeds.",
    "calibrate_with_aggregate_data": (
        "Calibrate assumptions only with suitable aggregate operational data."
    ),
    "review_branch_differences": "Review the paired branch differences before drawing conclusions.",
}
_METRIC_IDS = frozenset(MetricSnapshot.__dataclass_fields__) - {"minute_index", "source_ids_json"}
_EVENT_TYPE_ALIASES = {
    "facility_outage": "facility_outage",
    "facility_recovery": "facility_recovery",
    "water_outage": "water_outage",
    "water_restored": "water_restored",
    "resource_delivery": "resource_delivery",
    "delivery_delayed": "delivery_delayed",
    "staff_unavailable": "staff_unavailable",
    "staff_shift_change": "staff_shift_change",
    "power_outage": "power_outage",
    "power_restored": "power_restored",
    "equipment_failure": "equipment_failure",
    "equipment_repaired": "equipment_repaired",
    "extreme_heat": "extreme_heat",
    "extreme_cold": "extreme_cold",
    "social_interaction": "social_interaction",
    "toilet_service_complete": "completion",
    "staff_support_complete": "completion",
    "support_request_timeout": "completion",
    "agent_travel_complete": "completion",
}
_EVENT_LABELS_JA = {
    "facility_outage": "施設停止",
    "facility_recovery": "施設復旧",
    "water_outage": "給水停止",
    "water_restored": "給水再開",
    "resource_delivery": "物資到着",
    "delivery_delayed": "物資配送遅延",
    "staff_unavailable": "スタッフ稼働停止",
    "staff_shift_change": "スタッフ交代",
    "power_outage": "停電",
    "power_restored": "電力復旧",
    "equipment_failure": "設備故障",
    "equipment_repaired": "設備修理",
    "extreme_heat": "高温イベント",
    "extreme_cold": "低温イベント",
    "social_interaction": "社会的相互作用",
    "toilet_service_complete": "トイレ対応完了",
    "staff_support_complete": "スタッフ支援完了",
    "support_request_timeout": "支援要請の期限到達",
}
_METRIC_LABELS_JA = {
    "toilet_queue_length": "トイレ待ち人数",
    "toilet_active_users": "トイレ利用中の人数",
    "toilet_working_units": "稼働中のトイレ数",
    "toilet_wait_count": "待ちが発生した回数",
    "toilet_wait_total_minutes": "トイレの総待ち時間（分）",
    "toilet_wait_mean_minutes": "トイレの平均待ち時間（分）",
    "toilet_wait_max_minutes": "トイレの最大待ち時間（分）",
    "stress_mean": "平均ストレス",
    "stress_high_count": "高ストレス者数",
    "staff_assigned_task_count": "スタッフの担当タスク数",
    "staff_busy_count": "稼働中スタッフ数",
    "staff_load_ratio": "スタッフ稼働率",
    "unresolved_support_request_count": "未解決の支援要請数",
    "support_no_qualified_worker_count": "資格を満たすスタッフがいない支援要請数",
    "support_no_qualified_worker_request_minutes": "資格を満たすスタッフがいない依頼分数",
    "support_fairness_promoted_assignment_count": "公平性ルールで優先された支援要請数",
    "support_wait_staff_busy_request_minutes": "対応中の資格者がいる支援待ち依頼分数",
    "support_wait_staff_off_shift_request_minutes": "勤務時間外の資格者がいる支援待ち依頼分数",
}
_HYPOTHESIS_LABELS_JA = {
    "capacity_may_contribute_to_wait": "処理能力が待ち時間に関係する可能性があります。",
    "staff_load_may_affect_support": "スタッフ負荷が支援の処理量に関係する可能性があります。",
    "stress_pattern_needs_validation": "ストレスの傾向は適切なデータとの照合が必要です。",
    "event_may_explain_metric_change": "記録イベントはKPI差を調べる手掛かりになります。",
    "branch_effect_is_descriptive": "Branch間の差は記述的な結果で、複数seedでの検証が必要です。",
}


@dataclass(frozen=True)
class ReviewEvidence:
    """Allowlisted aggregate evidence and opaque event references for one review."""

    metrics: dict[str, float]
    events: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]
    comparison_deltas: dict[str, float]
    event_id_by_ref: dict[str, str]
    generated_minute: int | None

    def prompt_document(self) -> dict[str, Any]:
        return {
            "task": "Select evidence-bound review codes for one shelter simulation run.",
            "constraints": [
                "Do not make claims about real people or real-world outcomes.",
                "Only reference metric IDs, event references and assumption IDs supplied below.",
                "Observed metric direction must match the supplied values exactly.",
                "Event classifications must match their recorded type and outcome.",
                "Hypotheses must be labeled as hypotheses and include evidence references.",
                "Return at most 8 observed KPIs, 8 event observations, 3 hypotheses, and 3 recommendations.",
                "Prefer the most decision-relevant recorded evidence; do not repeat a metric or event.",
                "Return only the required JSON object. Do not include prose or numbers.",
            ],
            "final_metrics": self.metrics,
            "comparison_deltas": self.comparison_deltas,
            "events": list(self.events),
            "assumption_ids": list(self.assumptions),
            "comparison_available": bool(self.comparison_deltas),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": dict(self.metrics),
            "events": [dict(item) for item in self.events],
            "assumptions": list(self.assumptions),
            "comparison_deltas": dict(self.comparison_deltas),
            "event_id_by_ref": dict(self.event_id_by_ref),
            "generated_minute": self.generated_minute,
        }


@dataclass(frozen=True)
class ReviewResult:
    """Validated machine-readable review plus authoritative evidence facts."""

    schema_version: int
    status: str
    prompt_version: str
    input_hash: str
    evidence: dict[str, Any]
    review: dict[str, Any]
    validation_issue: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NarrativeCard:
    """Short Japanese story built only from recorded events and aggregate KPI evidence."""

    title: str
    observed_facts: tuple[str, ...]
    hypotheses: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    model_limits: tuple[str, ...]
    mode: Literal["opus", "offline"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "observed_facts": list(self.observed_facts),
            "hypotheses": list(self.hypotheses),
            "source_event_ids": list(self.source_event_ids),
            "metric_ids": list(self.metric_ids),
            "model_limits": list(self.model_limits),
            "mode": self.mode,
        }


def build_narrative_card(
    *,
    realized_events: list[dict[str, Any]],
    comparison_report: dict[str, Any],
    review_result: ReviewResult | None = None,
) -> NarrativeCard:
    """Render a bounded Japanese card from exact event/KPI facts and validated review codes."""
    metric_deltas = _comparison_deltas(comparison_report)
    selected_deltas = sorted(
        (
            (metric_id, delta)
            for metric_id, delta in metric_deltas.items()
            if metric_id in _METRIC_LABELS_JA and delta != 0
        ),
        key=lambda item: (-abs(item[1]), item[0]),
    )[:4]
    event_records = [
        record
        for record in realized_events
        if isinstance(record, dict)
        and isinstance(record.get("event"), dict)
        and record["event"].get("event_type") in _EVENT_LABELS_JA
        and isinstance(record["event"].get("event_id"), str)
        and isinstance(record.get("realized_minute"), int)
        and not isinstance(record.get("realized_minute"), bool)
        and record.get("status") in {"applied", "failed", "suppressed"}
    ][-5:]
    event_by_id = {record["event"]["event_id"]: record for record in event_records}
    event_ids = tuple(record["event"]["event_id"] for record in event_records)
    observed: list[str] = []
    first_minute = comparison_report.get("first_divergence_minute")
    if isinstance(first_minute, int) and not isinstance(first_minute, bool) and first_minute >= 0:
        observed.append(f"最初に状態差を記録した時刻はDay {first_minute // 1440 + 1}です。")
    for record in event_records:
        minute = record["realized_minute"]
        event = record["event"]
        clock = f"{minute // 60 % 24:02d}:{minute % 60:02d}"
        event_label = _EVENT_LABELS_JA[event["event_type"]]
        observed.append(
            f"候補BranchではDay {minute // 1440 + 1} {clock}に{event_label}を記録しました "
            f"（event `{event['event_id']}`、結果: {record['status']}）。"
        )
    event_chains: set[tuple[str, str]] = set()
    for record in event_records:
        event = record["event"]
        cause_id = event.get("causation_id")
        event_id = event["event_id"]
        if isinstance(cause_id, str) and cause_id in event_by_id and cause_id != event_id:
            event_chains.add((cause_id, event_id))
    for cause_id, event_id in sorted(event_chains):
        observed.append(
            f"記録上のイベント参照: `{cause_id}` → `{event_id}` （因果関係の実証を意味しません）。"
        )
    for metric_id, delta in selected_deltas:
        observed.append(
            f"終了時の選択案−元シナリオの差は{_METRIC_LABELS_JA[metric_id]} "
            f"{delta:+g}でした（`{metric_id}`）。"
        )

    selected_hypotheses: list[str] = []
    if review_result is not None and review_result.status == "validated":
        evidence = review_result.evidence
        for item in review_result.review.get("observed", []):
            metric_id = item.get("metric_id")
            value = evidence.get("metrics", {}).get(metric_id)
            if (
                metric_id in _METRIC_LABELS_JA
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and metric_id not in {key for key, _ in selected_deltas}
            ):
                observed.append(
                    f"候補Branchの終了時集計は{_METRIC_LABELS_JA[metric_id]} "
                    f"{value:g}でした（`{metric_id}`）。"
                )
        event_aliases = evidence.get("event_id_by_ref", {})
        available_event_ids = set(event_ids)
        available_metric_ids = set(evidence.get("metrics", {}))
        for item in review_result.review.get("hypotheses", []):
            code = item.get("hypothesis")
            cited_events = [
                event_aliases[reference]
                for reference in item.get("event_refs", [])
                if reference in event_aliases and event_aliases[reference] in available_event_ids
            ]
            cited_metrics = [
                metric_id
                for metric_id in item.get("metric_ids", [])
                if metric_id in available_metric_ids
            ]
            if code in _HYPOTHESIS_LABELS_JA and (cited_events or cited_metrics):
                selected_hypotheses.append(
                    f"{_HYPOTHESIS_LABELS_JA[code]}（参照: "
                    + ", ".join(f"`{value}`" for value in (*cited_events, *cited_metrics))
                    + "）"
                )
    if not selected_hypotheses and event_records and selected_deltas:
        selected_hypotheses.append(
            "記録イベントはBranch間のKPI差を調べる手掛かりになります。単一seedでは因果関係を結論できません。"
        )
    if not observed:
        observed.append("表示できるイベント記録またはKPI差はありません。")

    metric_ids = tuple(metric_id for metric_id, _ in selected_deltas)
    if review_result is not None and review_result.status == "validated":
        metric_ids = tuple(
            dict.fromkeys(
                (
                    *metric_ids,
                    *(item["metric_id"] for item in review_result.review.get("observed", [])),
                )
            )
        )
    mode = (
        "opus" if review_result is not None and review_result.status == "validated" else "offline"
    )
    limits = (
        "数値とイベントは合成シミュレーションの記録です。",
        "単一seedの比較は記述的で、現実の結果や因果関係を証明しません。",
        "Agentの発言や個人記録はストーリーに含めません。",
    )
    return NarrativeCard(
        title="イベント記録とBranch間のKPI差",
        observed_facts=tuple(observed),
        hypotheses=tuple(selected_hypotheses),
        source_event_ids=event_ids,
        metric_ids=metric_ids,
        model_limits=limits,
        mode=mode,
    )


def build_review_evidence(
    *,
    metrics: list[dict[str, Any]],
    realized_events: list[dict[str, Any]],
    assumptions: list[str],
    comparison_report: dict[str, Any] | None = None,
    maximum_events: int = 20,
) -> ReviewEvidence:
    """Reduce artifact data to allowlisted metrics, critical events and safe aliases."""
    if maximum_events < 0:
        raise ValueError("maximum_events cannot be negative")
    final_row = metrics[-1] if metrics else {}
    metric_values = {
        key: float(value)
        for key, value in final_row.items()
        if key in _METRIC_IDS and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    minute = final_row.get("minute_index")
    critical = (
        [item for item in realized_events if _critical_event(item)][-maximum_events:]
        if maximum_events
        else []
    )
    event_id_by_ref: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for index, record in enumerate(critical, start=1):
        event = record.get("event")
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        reference = f"E{index:03d}"
        event_id_by_ref[reference] = event_id
        event_type = event.get("event_type")
        status = record.get("status")
        events.append(
            {
                "event_ref": reference,
                "minute": record.get("realized_minute"),
                "event_type": (event_type if event_type in _EVENT_TYPE_ALIASES else "other"),
                "status": status if status in ("applied", "failed", "suppressed") else "other",
                "classification": _event_classification(event_type, status),
            }
        )

    selected_assumptions = tuple(sorted(set(assumptions) & _ASSUMPTION_LABELS.keys()))
    comparison_deltas = _comparison_deltas(comparison_report)
    return ReviewEvidence(
        metrics=metric_values,
        events=tuple(events),
        assumptions=selected_assumptions,
        comparison_deltas=comparison_deltas,
        event_id_by_ref=event_id_by_ref,
        generated_minute=minute if isinstance(minute, int) else None,
    )


def evidence_from_run_artifacts(
    run_directory: str | Path,
    *,
    comparison_report: dict[str, Any] | None = None,
) -> ReviewEvidence:
    """Load only aggregate and allowlisted run evidence; never read decision/profile details."""
    directory = Path(run_directory)
    metrics_path = directory / "metrics.csv"
    with metrics_path.open(encoding="utf-8", newline="") as handle:
        metric_rows = list(csv.DictReader(handle))
    numeric_rows: list[dict[str, Any]] = []
    for row in metric_rows:
        normalized: dict[str, Any] = {"minute_index": _parse_integer(row.get("minute_index"))}
        for metric_id in _METRIC_IDS:
            raw = row.get(metric_id)
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value == value and abs(value) != float("inf"):
                normalized[metric_id] = value
        numeric_rows.append(normalized)
    realized = _read_jsonl(directory / "events_realized.jsonl")
    manifest = _read_json(directory / "manifest.json")
    initial = _read_json(directory / "agents_initial.json")
    if comparison_report is not None:
        _validate_comparison_manifest_scope(manifest, comparison_report)
    profile_records = initial.get("profiles", [])
    assumptions = _scenario_assumption_ids(manifest.get("model_configs", {}), profile_records)
    return build_review_evidence(
        metrics=numeric_rows,
        realized_events=realized,
        assumptions=assumptions,
        comparison_report=comparison_report,
    )


def scenario_assumption_ids(scenario: ScenarioSpec) -> list[str]:
    """Return only known model assumption IDs implicated by this scenario's features."""
    ids: list[str] = []
    if any(profile.provenance.source_kind == "synthetic" for profile in scenario.profiles):
        ids.append("ASM-SYNTHETIC-PROFILES-V1")
    ids.append("ASM-SIMPLE-PSYCHOLOGY-RATES-V1")
    ids.append("ASM-H04-CAUSAL-STRESS-V1")
    behavior_config = scenario.model_configs.get("behavior", {})
    behavior_source = (
        "ASM-PRIORITY-RULE-BEHAVIOR-V1"
        if isinstance(behavior_config, dict) and behavior_config.get("policy") == "priority-rule-v1"
        else "ASM-UTILITY-BEHAVIOR-V1"
    )
    ids.append(behavior_source)
    if any("toilet" in facility.kind.casefold() for facility in scenario.facilities):
        ids.append("ASM-TOILET-SERVICE-DURATION-V1")
    if scenario.zones or scenario.staff or scenario.facilities:
        ids.append("ASM-SPACE-AND-STAFF-CAPACITY-V1")
    if scenario.staff:
        ids.append("ASM-STAFF-SUPPORT-LIFECYCLE-V1")
    if scenario.zones:
        ids.append("ASM-ZONE-ROUTING-V1")
    if "social_propagation" in scenario.model_configs:
        ids.append("ASM-SOCIAL-PROPAGATION-V1")
    information_config = scenario.model_configs.get("information_spread", {})
    if isinstance(information_config, dict) and information_config.get("enabled") is True:
        ids.append("ASM-INFORMATION-SPREAD-V1")
    ids.append("ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1")
    ids.append("ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1")
    if any(
        event.event_type == "facility_outage"
        and (
            event.source_id == "ASM-DAY3-TOILET-OUTAGE-V1"
            or event.provenance.get("source_id") == "ASM-DAY3-TOILET-OUTAGE-V1"
        )
        for event in scenario.scheduled_events
    ):
        ids.append("ASM-DAY3-TOILET-OUTAGE-V1")
    declared = scenario.provenance.get("model_assumption_ids", [])
    if isinstance(declared, list):
        ids.extend(
            value for value in declared if isinstance(value, str) and value in _ASSUMPTION_LABELS
        )
    return ids


def validate_comparison_scope(
    scenario: ScenarioSpec,
    comparison_report: dict[str, Any] | None,
) -> None:
    """Reject branch/run comparison evidence that belongs to another scenario or seed."""
    if comparison_report is None:
        return
    current_hash = _digest(scenario.canonical_json())
    if "scenario_hash" in comparison_report and comparison_report["scenario_hash"] != current_hash:
        raise ValueError("comparison report scenario hash does not match this run")
    scenario_id = comparison_report.get("scenario_id")
    if scenario_id is not None and scenario_id != scenario.id:
        raise ValueError("comparison report scenario ID does not match this run")
    seed = comparison_report.get("seed")
    if seed is not None and seed != scenario.seed:
        raise ValueError("comparison report seed does not match this run")
    definition_hash = comparison_report.get("scenario_definition_hash")
    if definition_hash is not None:
        scenario_document = scenario.model_dump(mode="json")
        scenario_document.pop("seed", None)
        expected = hashlib.sha256(
            json.dumps(
                scenario_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if definition_hash != expected:
            raise ValueError("comparison report scenario definition does not match this run")


def review_saved_run(
    run_directory: str | Path,
    router: LLMRouter,
    *,
    comparison_report: dict[str, Any] | None = None,
) -> ReviewResult:
    """Review a completed run offline or with Opus, updating only review/usage artifacts."""
    from sheltersim.inspection.traces import _llm_usage_summary

    directory = Path(run_directory)
    evidence = evidence_from_run_artifacts(directory, comparison_report=comparison_report)
    starting_record_count = len(router.records)
    result = generate_evidence_review(evidence, router)
    _write_atomic(directory / "review.md", render_review_markdown(result))
    _write_atomic(directory / "review.json", _pretty_json(result.to_dict()))
    call_path = directory / "llm_calls.jsonl"
    current_calls = _read_jsonl(call_path) if call_path.exists() else []
    current_calls.extend(record.to_dict() for record in router.records[starting_record_count:])
    _write_atomic(
        call_path,
        ""
        if not current_calls
        else "\n".join(_compact_json(item) for item in current_calls) + "\n",
    )
    manifest = _read_json(directory / "manifest.json")
    manifest["llm_usage"] = _llm_usage_summary(current_calls)
    manifest["review_status"] = result.status
    manifest["review_input_hash"] = result.input_hash
    _write_atomic(directory / "manifest.json", _pretty_json(manifest) + "\n")
    return result


def generate_evidence_review(
    evidence: ReviewEvidence,
    router: LLMRouter,
) -> ReviewResult:
    """Ask Opus for structured review codes and reject unsupported references/patterns."""
    prompt = evidence.prompt_document()
    input_text = json.dumps(
        prompt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    schema = _review_schema(evidence)
    fallback = _empty_review()
    task = LLMTask(
        stage=LLMStage.REVIEW,
        input_text=input_text,
        state_hash=_digest(json.dumps(evidence.to_dict(), sort_keys=True, separators=(",", ":"))),
        prompt_version="evidence-review-v1",
        schema_version=1,
        output_schema=schema,
        fallback_text=json.dumps(fallback, separators=(",", ":"), sort_keys=True),
        max_output_tokens=2048,
    )
    response = router.complete(task)
    record = router.records[-1]
    result = json.loads(response)
    validation_issue = _validate_review(result, evidence)
    if validation_issue is not None:
        return ReviewResult(
            schema_version=1,
            status="rejected",
            prompt_version=task.prompt_version,
            input_hash=record.input_hash,
            evidence=evidence.to_dict(),
            review=_empty_review(),
            validation_issue=validation_issue,
        )
    return ReviewResult(
        schema_version=1,
        status="rule_fallback" if record.fallback_used else "validated",
        prompt_version=task.prompt_version,
        input_hash=record.input_hash,
        evidence=evidence.to_dict(),
        review=result,
    )


def render_review_markdown(result: ReviewResult) -> str:
    """Render fixed vocabulary and numeric facts from authoritative run artifacts."""
    evidence = result.evidence
    review = result.review
    metrics = evidence["metrics"]
    event_by_ref = {item["event_ref"]: item for item in evidence["events"]}
    alias_map = evidence["event_id_by_ref"]
    lines = ["# Evidence-bound Opus review", "", f"- Status: `{result.status}`"]
    if result.status == "rejected":
        lines.extend(
            [
                "",
                "The structured review was rejected because it did not match "
                "the supplied evidence.",
                f"Validation issue: `{result.validation_issue}`",
            ]
        )
    elif result.status == "rule_fallback":
        lines.extend(
            [
                "",
                "Opus review was unavailable; the recorded run evidence below "
                "remains authoritative.",
            ]
        )

    lines.extend(["", "## Observed evidence", ""])
    observations = review.get("observed", [])
    if not observations:
        lines.append("No structured observations were returned.")
    for item in observations:
        metric_id = item["metric_id"]
        value = metrics[metric_id]
        if item["direction"] in ("increase", "decrease", "unchanged"):
            delta = evidence["comparison_deltas"][metric_id]
            lines.append(
                f"- `{metric_id}` changed {item['direction']} versus its paired baseline "
                f"(delta {delta:g}; candidate final value {value:g})."
            )
        else:
            lines.append(
                f"- `{metric_id}` is {item['direction']} in the final snapshot ({value:g})."
            )
    for item in review.get("event_observations", []):
        event_ref = item["event_ref"]
        event = event_by_ref[event_ref]
        real_id = alias_map[event_ref]
        lines.append(
            f"- `{real_id}` at minute {event['minute']} is recorded as "
            f"`{event['classification']}` ({event['status']})."
        )

    lines.extend(["", "## Assumptions", ""])
    if not review.get("assumptions"):
        lines.append("No assumption references were selected.")
    for assumption_id in review.get("assumptions", []):
        lines.append(f"- `{assumption_id}`: {_ASSUMPTION_LABELS[assumption_id]}")

    lines.extend(["", "## Hypotheses", ""])
    if not review.get("hypotheses"):
        lines.append("No hypotheses were selected.")
    for item in review.get("hypotheses", []):
        text = _HYPOTHESIS_LABELS[item["hypothesis"]]
        citations = _render_references(item, alias_map)
        lines.append(f"- {text}{citations}")

    lines.extend(["", "## Recommendations", ""])
    if not review.get("recommendations"):
        lines.append("No recommendations were selected.")
    for item in review.get("recommendations", []):
        text = _RECOMMENDATION_LABELS[item["action"]]
        citations = _render_references(item, alias_map)
        lines.append(f"- {text}{citations}")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "This review summarizes recorded synthetic simulation outputs. It does not establish "
            "real-world safety, behavior, or causal effects.",
            "",
        ]
    )
    return "\n".join(lines)


def _review_schema(evidence: ReviewEvidence) -> dict[str, Any]:
    metric_ids = sorted(evidence.metrics)
    event_refs = [item["event_ref"] for item in evidence.events]
    item_refs_schema = {"type": "array", "items": {"type": "string", "enum": event_refs}}
    metric_refs_schema = {
        "type": "array",
        "items": {"type": "string", "enum": metric_ids},
    }
    assumption_refs_schema = {
        "type": "array",
        "items": {"type": "string", "enum": list(evidence.assumptions)},
    }
    observed_directions = ["zero", "nonzero", "increase", "decrease", "unchanged"]
    return {
        "type": "object",
        "properties": {
            "observed": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "metric_id": {"type": "string", "enum": metric_ids},
                        "direction": {"type": "string", "enum": observed_directions},
                    },
                    "required": ["metric_id", "direction"],
                    "additionalProperties": False,
                },
            },
            "event_observations": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "event_ref": {"type": "string", "enum": event_refs},
                        "classification": {
                            "type": "string",
                            "enum": sorted(
                                {"failure", "suppressed", "other", *_EVENT_TYPE_ALIASES.values()}
                            ),
                        },
                    },
                    "required": ["event_ref", "classification"],
                    "additionalProperties": False,
                },
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string", "enum": list(evidence.assumptions)},
            },
            "hypotheses": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string", "enum": sorted(_HYPOTHESIS_LABELS)},
                        "metric_ids": metric_refs_schema,
                        "event_refs": item_refs_schema,
                        "assumption_ids": assumption_refs_schema,
                    },
                    "required": ["hypothesis", "metric_ids", "event_refs", "assumption_ids"],
                    "additionalProperties": False,
                },
            },
            "recommendations": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": sorted(_RECOMMENDATION_LABELS)},
                        "metric_ids": metric_refs_schema,
                        "event_refs": item_refs_schema,
                        "assumption_ids": assumption_refs_schema,
                    },
                    "required": ["action", "metric_ids", "event_refs", "assumption_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "observed",
            "event_observations",
            "assumptions",
            "hypotheses",
            "recommendations",
        ],
        "additionalProperties": False,
    }


def _validate_review(review: dict[str, Any], evidence: ReviewEvidence) -> str | None:
    for item in review["observed"]:
        metric = item["metric_id"]
        value = evidence.metrics[metric]
        direction = item["direction"]
        if direction == "zero" and value != 0.0:
            return f"metric direction mismatch for {metric}"
        if direction == "nonzero" and value == 0.0:
            return f"metric direction mismatch for {metric}"
        if direction in ("increase", "decrease", "unchanged"):
            delta = evidence.comparison_deltas.get(metric)
            if delta is None:
                return f"metric {metric} has no paired comparison fact"
            if direction == "increase" and delta <= 0:
                return f"comparison direction mismatch for {metric}"
            if direction == "decrease" and delta >= 0:
                return f"comparison direction mismatch for {metric}"
            if direction == "unchanged" and delta != 0:
                return f"comparison direction mismatch for {metric}"
    event_records = {item["event_ref"]: item for item in evidence.events}
    for item in review["event_observations"]:
        record = event_records[item["event_ref"]]
        if item["classification"] != record["classification"]:
            return f"event classification mismatch for {item['event_ref']}"
    for item in (*review["hypotheses"], *review["recommendations"]):
        if not (item["metric_ids"] or item["event_refs"] or item["assumption_ids"]):
            return "hypothesis or recommendation has no cited evidence"
    return None


def _validate_comparison_manifest_scope(
    manifest: dict[str, Any], comparison_report: dict[str, Any]
) -> None:
    for field in ("scenario_hash", "scenario_definition_hash", "scenario_id", "seed"):
        expected = manifest.get(field)
        value = comparison_report.get(field)
        if value is not None and expected is not None and value != expected:
            raise ValueError(f"comparison report {field} does not match the saved run")


def _empty_review() -> dict[str, Any]:
    return {
        "observed": [],
        "event_observations": [],
        "assumptions": [],
        "hypotheses": [],
        "recommendations": [],
    }


def _critical_event(record: dict[str, Any]) -> bool:
    event = record.get("event")
    if not isinstance(event, dict):
        return False
    event_type = event.get("event_type")
    status = record.get("status")
    return status in ("failed", "suppressed") or event_type in (
        set(_EVENT_TYPE_ALIASES)
        - {
            "toilet_service_complete",
            "staff_support_complete",
            "support_request_timeout",
            "agent_travel_complete",
        }
    )


def _event_classification(event_type: Any, status: Any) -> str:
    if status == "failed":
        return "failure"
    if status == "suppressed":
        return "suppressed"
    return _EVENT_TYPE_ALIASES.get(event_type, "other")


def _comparison_deltas(report: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(report, dict):
        return {}
    raw = report.get("final_metric_differences")
    if not isinstance(raw, dict):
        raw = report.get("paired_delta_summary")
    if not isinstance(raw, dict):
        return {}
    deltas: dict[str, float] = {}
    for metric_id, value in raw.items():
        if metric_id not in _METRIC_IDS or not isinstance(value, dict):
            continue
        delta = value.get("delta", value.get("mean"))
        if isinstance(delta, (int, float)) and not isinstance(delta, bool) and _finite(delta):
            deltas[metric_id] = float(delta)
    return deltas


def _scenario_assumption_ids(model_configs: Any, profiles: Any) -> list[str]:
    ids: set[str] = set()
    if isinstance(profiles, list) and profiles:
        if all(
            isinstance(item, dict)
            and isinstance(item.get("provenance"), dict)
            and item["provenance"].get("source_kind") in ("synthetic", "llm", "assumption")
            for item in profiles
        ):
            ids.add("ASM-SYNTHETIC-PROFILES-V1")
    if isinstance(model_configs, dict):
        if "behavior" in model_configs:
            behavior = model_configs["behavior"]
            ids.add(
                "ASM-PRIORITY-RULE-BEHAVIOR-V1"
                if isinstance(behavior, dict) and behavior.get("policy") == "priority-rule-v1"
                else "ASM-UTILITY-BEHAVIOR-V1"
            )
        if "toilet_service" in model_configs or "staff" in model_configs:
            ids.add("ASM-TOILET-SERVICE-DURATION-V1")
        information_config = model_configs.get("information_spread")
        if isinstance(information_config, dict) and information_config.get("enabled") is True:
            ids.add("ASM-INFORMATION-SPREAD-V1")
        if model_configs:
            ids.add("ASM-SPACE-AND-STAFF-CAPACITY-V1")
    ids.add("ASM-SIMPLE-PSYCHOLOGY-RATES-V1")
    ids.add("ASM-H04-CAUSAL-STRESS-V1")
    return sorted(ids & _ASSUMPTION_LABELS.keys())


def _render_references(item: dict[str, Any], alias_map: dict[str, str]) -> str:
    refs = [
        alias_map[reference] for reference in item.get("event_refs", []) if reference in alias_map
    ]
    metrics = item.get("metric_ids", [])
    assumptions = item.get("assumption_ids", [])
    citations = [f"`{metric}`" for metric in metrics]
    citations.extend(f"event `{event}`" for event in refs)
    citations.extend(f"assumption `{assumption}`" for assumption in assumptions)
    return " Evidence: " + ", ".join(citations) + "." if citations else ""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_atomic(path: Path, contents: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(contents, encoding="utf-8", newline="")
    temporary.replace(path)


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite(value: float) -> bool:
    return value == value and abs(value) != float("inf")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
