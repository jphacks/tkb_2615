"""Read-only report projection and cached PDF export for saved Project runs."""

from __future__ import annotations

import base64
import math
import threading
from dataclasses import replace
from typing import Any

from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.pdf_analysis import PROMPT_VERSION, generate_analysis
from sheltersim.llm.report import scenario_assumption_ids
from sheltersim.storage.projects import StorageError, digest, safe_document

REPORT_VERSION = "pdf-report-ja-v1"
# key, Japanese label, displayed unit, scale, lower-is-better (None = descriptive).
METRICS = (
    ("stress_mean", "平均ストレス", "/100", 100, True),
    ("toilet_wait_max_minutes", "最大トイレ待ち時間", "分", 1, True),
    ("staff_load_ratio", "スタッフ稼働率", "%", 100, None),
    ("unresolved_support_request_count", "未解決の支援要請", "件", 1, True),
    ("toilet_queue_length", "トイレ待ち人数", "人", 1, True),
    ("stress_high_count", "高ストレス者数", "人", 1, True),
)


def number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def problem_window(rows: list[dict[str, Any]], key: str, start: int, end: int) -> dict[str, Any]:
    """Count positive one-minute intervals only; never interpolate gaps or an end sample."""
    indexed = {
        row["minute_index"]: row.get(key)
        for row in rows
        if type(row.get("minute_index")) is int and start <= row["minute_index"] <= end
    }
    positive = [
        m
        for m in range(start, end)
        if number(indexed.get(m)) and number(indexed.get(m + 1)) and indexed[m] > 0
    ]
    covered = sum(number(indexed.get(m)) and number(indexed.get(m + 1)) for m in range(start, end))
    intervals: list[list[int]] = []
    for minute in positive:
        if intervals and intervals[-1][1] == minute:
            intervals[-1][1] = minute + 1
        else:
            intervals.append([minute, minute + 1])
    return {
        "first_observed_minute": min(
            (m for m, value in indexed.items() if number(value) and value > 0), default=None
        ),
        "positive_interval_minutes": len(positive),
        "covered_minutes": covered,
        "missing_minutes": end - start - covered,
        "intervals": intervals,
        "criterion": f"{key} > 0",
    }


_INTERVENTION_DETAIL_KEYS = (
    "op",
    "parameters",
    "target_id",
    "facility_id",
    "zone_id",
    "capacity",
    "working_units",
    "end_minute",
    "delta",
    "kind",
    # EffectProgram events keep some parameters at the top level of the payload (#160).
    "count",
    "quantity",
    "resource_id",
    "household_size",
    "include_initial",
    "to_minute",
    "staff_id",
    "until_minute",
)


def intervention_details(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload values a report shows for one intervention.

    Per-person context, free text and internal IDs stay out of the report.
    """
    return {key: payload[key] for key in _INTERVENTION_DETAIL_KEYS if key in payload}


def collect_report(
    api: Any, project: str, run_id: str, ids: list[str], minute: int
) -> dict[str, Any]:
    """Capture only exact saved history and aggregate conditions, without provider calls."""
    store = api.store
    run = store.run(project, run_id)
    scenario = store.scenario(project, run["scenario_id"], run["scenario_version"])
    refs = [store.branch_reference(project, run_id, key) for key in ids]
    start = max(ref.fork_minute for ref in refs) if len(ids) > 1 else 0
    if any(not ref.fork_minute <= minute <= ref.computed_until for ref in refs):
        raise StorageError("pdf_uncomputed_range: 指定時刻は共通の計算済み範囲外です。")
    # Single child reports start at its own fork, rather than claiming unrecorded history.
    start = max(start, refs[0].fork_minute)
    branches = []
    for key in ids:
        runtime = TimelineStore(store, project, run_id).seek(key, minute)
        final = MetricSnapshot.from_world(runtime.world).csv_row()
        series = [
            {
                "minute_index": row.minute_index,
                **{metric[0]: getattr(row, metric[0]) for metric in METRICS},
            }
            for row in runtime.metrics.snapshots
            if start <= row.minute_index <= minute
        ]
        # The exact selected boundary is authoritative even if the recorder ended earlier.
        series = [row for row in series if row["minute_index"] != minute]
        series.append({"minute_index": minute, **{m[0]: final[m[0]] for m in METRICS}})
        series.sort(key=lambda row: row["minute_index"])
        events = []
        for record in runtime.world.event_log.records:
            event = record.event
            if start <= record.realized_minute <= minute and event.kind.value in {
                "exogenous",
                "intervention",
            }:
                events.append(
                    {
                        "event_id": event.event_id,
                        "minute": record.realized_minute,
                        "type": event.event_type,
                        "status": record.status.value,
                        "kind": event.kind.value,
                        "source_id": event.source_id,
                        "causation_id": event.causation_id,
                    }
                )
        interventions = []
        for event, realized, status in [
            (record.event, record.realized_minute, record.status.value)
            for record in runtime.world.event_log.records
        ] + [(event, None, "scheduled") for event in runtime.world.event_queue.pending()]:
            if event.kind.value != "intervention":
                continue
            payload = event.to_dict()["payload"]
            marker = event.to_dict().get("provenance", {}).get("effect_program_v1", {})
            interventions.append(
                {
                    "event_id": event.event_id,
                    "type": event.event_type,
                    "scheduled_minute": event.due_minute,
                    "realized_minute": realized,
                    "status": status,
                    "operator": marker.get("op"),
                    "details": intervention_details(payload),
                }
            )
        # Local report includes typed intervention records, never individual profiles or prompts.
        commands = safe_document(runtime.command_log)
        branches.append(
            {
                "branch_id": key,
                "minute": minute,
                "population": len(runtime.world.agents),
                "state_hash": digest(runtime.world.to_dict()),
                "final": {m[0]: final.get(m[0]) for m in METRICS},
                "series": series,
                "events": events,
                "commands": commands,
                "interventions": safe_document(interventions),
                "origin": runtime.runtime_state.get("fork_origin"),
                "problems": {
                    key: problem_window(series, key, start, minute)
                    for key in ("toilet_queue_length", "unresolved_support_request_count")
                },
            }
        )
    differences = []
    for branch in branches[1:]:
        values = {}
        for key, _, _, _, lower_better in METRICS:
            a, b = branches[0]["final"].get(key), branch["final"].get(key)
            delta = b - a if number(a) and number(b) else None
            direction = (
                "未取得"
                if delta is None
                else "変化なし"
                if delta == 0
                else "方向評価なし"
                if lower_better is None
                else "改善"
                if (delta < 0) == lower_better
                else "悪化"
            )
            values[key] = {"baseline": a, "candidate": b, "delta": delta, "assessment": direction}
        differences.append({"branch_id": branch["branch_id"], "metrics": values})
    # Reuse the existing comparison contract for pairing and common-range semantics.
    comparison = api.workspace.comparison(project, run_id, ids, minute) if len(ids) > 1 else None
    conditions = {
        "population": len(scenario.profiles),
        "duration_minutes": scenario.duration_minutes,
        "zones": [item.model_dump(mode="json") for item in scenario.zones],
        "facilities": [item.model_dump(mode="json") for item in scenario.facilities],
        "staff_count": len(scenario.staff),
        "staff_skills": sorted({skill for item in scenario.staff for skill in item.skills}),
        "initial_stock": scenario.initial_stock.model_dump(mode="json")
        if scenario.initial_stock
        else None,
        "assumption_ids": scenario_assumption_ids(scenario),
        "source_ids": sorted(
            set(_source_ids(scenario.provenance)) | set(_source_ids(scenario.model_configs))
        ),
    }
    return {
        "schema_version": 1,
        "report_version": REPORT_VERSION,
        "project_id": project,
        "run_id": run_id,
        "scenario_id": scenario.id,
        "scenario_hash": run["scenario_hash"],
        "scenario_version": run["scenario_version"],
        "seed": scenario.seed,
        "model_versions": run["model_versions"],
        "range": [start, minute],
        "partial": minute < scenario.duration_minutes,
        "conditions": conditions,
        "branches": branches,
        "differences": differences,
        "paired": comparison["shared_origin"] if comparison else False,
        "heads": [ref.model_dump(mode="json") for ref in refs],
    }


def _source_ids(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_id" and isinstance(item, str):
                found.append(item)
            elif key.endswith("source_ids") and isinstance(item, (list, tuple)):
                found.extend(part for part in item if isinstance(part, str))
            else:
                found.extend(_source_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_source_ids(item))
    return found


def analysis_facts(data: dict[str, Any]) -> dict[str, Any]:
    """Opaque aliases keep user labels, locations, IDs and arbitrary strings off the API."""
    facts: dict[str, Any] = {
        "S": {
            key: data["conditions"][key]
            for key in ("population", "duration_minutes", "staff_count")
        },
        "L": {
            "calibration": "実測校正状況は未取得。モデル係数に仮定を含み、予測精度は未検証。",
            "partial": data["partial"],
            "range": data["range"],
            "comparison": bool(data["differences"]),
            "paired": data["paired"],
            "map": "実建物図面は評価対象外",
        },
    }
    for index, branch in enumerate(data["branches"]):
        alias = f"B{index}"
        for key, label, unit, factor, _ in METRICS:
            raw = branch["final"].get(key)
            facts[f"{alias}.{key}"] = {
                "branch": alias,
                "metric": key,
                "label": label,
                "value": raw * factor if number(raw) else None,
                "unit": unit,
                "minute": branch["minute"],
            }
        for key, window in branch["problems"].items():
            facts[f"{alias}.window.{key}"] = {k: v for k, v in window.items() if k != "intervals"}
        facts[f"{alias}.interventions"] = {
            "recorded_commands": len(branch["commands"]),
            "recorded_intervention_events": sum(
                e["kind"] == "intervention" for e in branch["events"]
            ),
        }
        # Bounded representative temporal evidence; all observations remain in the local PDF data.
        for key, label, unit, factor, _ in METRICS:
            observed = [row for row in branch["series"] if number(row.get(key))]
            if observed:
                peak = max(observed, key=lambda row: row[key])
                facts[f"{alias}.peak.{key}"] = {
                    "metric": key,
                    "value": peak[key] * factor,
                    "label": label,
                    "unit": unit,
                    "minute": peak["minute_index"],
                }
        known_operators = {
            "add_facility",
            "add_toilet",
            "add_staff",
            "add_resource",
            "deliver_resource",
            "set_facility_availability",
            "facility_outage",
            "set_staff_availability",
            "set_ambient_noise",
            "set_ventilation",
            "set_privacy",
            "set_temperature",
            "set_lighting",
            "modify_demand_rate",
            "move_facility",
            "set_route_access",
            "add_evacuees",
            "depart_evacuees",
            "transfer_equipment",
        }
        for intervention_index, item in enumerate(branch["interventions"][:12]):
            operator = item["operator"] or item["type"].removeprefix("branch-").replace("-", "_")
            parameters = item["details"].get("parameters", item["details"])
            facts[f"{alias}.I{intervention_index}"] = {
                "operator": operator if operator in known_operators else "recorded_intervention",
                "parameters": {
                    k: parameters[k]
                    for k in (
                        "capacity",
                        "working_units",
                        "count",
                        "quantity",
                        "water_ml",
                        "food_meal_units",
                        "available",
                        "multiplier",
                        "ratio",
                    )
                    if k in parameters and (number(parameters[k]) or type(parameters[k]) is bool)
                },
                "scheduled_minute": item["scheduled_minute"],
                "realized_minute": item["realized_minute"],
                "status": item["status"],
            }
        known_types = {
            "facility_outage",
            "facility_recovery",
            "water_outage",
            "water_restored",
            "resource_delivery",
            "staff_unavailable",
            "power_outage",
            "power_restored",
            "equipment_failure",
            "equipment_repaired",
            "extreme_heat",
            "extreme_cold",
        }
        for event_index, event in enumerate(branch["events"][:12]):
            facts[f"{alias}.E{event_index}"] = {
                "type": event["type"] if event["type"] in known_types else "recorded_event",
                "minute": event["minute"],
                "status": event["status"],
                "kind": event["kind"],
            }
    for index, delta in enumerate(data["differences"], 1):
        for key, label, unit, factor, _ in METRICS:
            values = delta["metrics"][key]
            facts[f"D{index}.{key}"] = {
                "metric": key,
                "label": label,
                "unit": "pt" if factor == 100 else unit,
                **{
                    field: values[field] * factor if number(values[field]) else None
                    for field in ("baseline", "candidate", "delta")
                },
                "assessment": values["assessment"],
            }
    return facts


class PDFReportService:
    """Serialize duplicate exports; persist a versioned artifact using existing atomic storage."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self._lock = threading.Lock()

    def export(self, project: str, run_id: str, body: dict[str, Any]) -> bytes:
        from sheltersim.web.pdf_render import render_pdf
        from sheltersim.web.projects import fields

        fields(body, {"branch_ids", "minute"})
        ids, minute = body["branch_ids"], body["minute"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 2
            or any(not isinstance(key, str) for key in ids)
            or len(set(ids)) != len(ids)
            or type(minute) is not int
            or minute < 0
        ):
            raise StorageError(
                "pdf_invalid_scope: 単一BranchまたはA/Bの2分岐と整数分を指定してください。"
            )
        with self._lock:
            store = self.api.store
            run = store.run(project, run_id)
            refs = [store.branch_reference(project, run_id, key) for key in ids]
            if any(not ref.fork_minute <= minute <= ref.computed_until for ref in refs):
                raise StorageError("pdf_uncomputed_range: 指定時刻は共通の計算済み範囲外です。")
            settings = replace(LLMSettings.from_env(), review_model_family="opus")
            identity = {
                "heads": [ref.model_dump(mode="json") for ref in refs],
                "scenario_hash": run["scenario_hash"],
                "minute": minute,
                "version": REPORT_VERSION,
                "prompt_version": PROMPT_VERSION,
                "model": settings.opus_model,
            }
            key = digest(identity)
            path = store._path(project, "runs", run_id, "pdf-reports", f"{key}.json")
            if path.exists():
                saved = store._read(path)
                if saved.get("identity") == identity:
                    try:
                        pdf = base64.b64decode(saved["pdf_base64"], validate=True)
                        if pdf.startswith(b"%PDF-") and digest(saved["pdf_base64"]) == saved.get(
                            "pdf_hash"
                        ):
                            return pdf
                    except (ValueError, KeyError):
                        pass  # Rebuild a damaged PDF from the accepted response tape.
            data = collect_report(self.api, project, run_id, ids, minute)
            facts = analysis_facts(data)
            router = self.api.workspace.analysis_router(
                project, run_id, operation="pdf_report", branch_id=ids[-1], settings=settings
            )
            attempt_path = store._path(project, "runs", run_id, "pdf-attempts", f"{key}.json")
            attempt = store._read(attempt_path)["next_attempt"] if attempt_path.exists() else 0
            try:
                analysis = generate_analysis(facts, router, attempt=attempt)
            except ValueError:
                # An explicit retry after an attempted/invalid answer is a new, accounted
                # request. Keep uncertain spend and every original receipt intact.
                if router.records and (
                    router.records[-1].api_request_count or router.records[-1].cache_hit
                ):
                    with store.transaction():
                        store._write(
                            attempt_path, {"schema_version": 1, "next_attempt": attempt + 1}
                        )
                raise
            finally:
                self.api.workspace.save_analysis(project, run_id, router)
            try:
                pdf = render_pdf(data, analysis, facts)
            except Exception as exc:
                raise StorageError(
                    "pdf_render_failed: PDFの組版に失敗しました。再試行してください。"
                ) from exc
            if any(
                store.branch_reference(project, run_id, ref.branch_id).checkpoint_hash
                != ref.checkpoint_hash
                for ref in refs
            ):
                raise StorageError(
                    "pdf_scope_changed: 計算結果が更新されました。再試行してください。"
                )
            encoded = base64.b64encode(pdf).decode("ascii")
            artifact = {
                "schema_version": 1,
                "identity": identity,
                "data": data,
                "facts": facts,
                "analysis": analysis,
                "pdf_hash": digest(encoded),
                "pdf_base64": encoded,
            }
            with store.transaction():
                store._write(path, safe_document(artifact))
            return pdf
