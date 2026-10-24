"""Opt-in, bounded telemetry for saved Project Runs and browser rendering."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from sheltersim.storage.projects import ProjectStore, StorageError, safe_document


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise StorageError(f"invalid telemetry value: {name}")
    return float(value)


class RunTelemetryStore:
    """Persist bounded measurements without placing observational data in checkpoints."""

    def __init__(self, store: ProjectStore, project_id: str, run_id: str) -> None:
        self.store, self.project_id, self.run_id = store, project_id, run_id

    def _path(self) -> Any:
        return self.store._path(self.project_id, "runs", self.run_id, "telemetry.json")

    def read(self) -> dict[str, Any]:
        path = self._path()
        if not path.exists():
            return {"schema_version": 1, "execution_count": 0,
                    "totals": {
                        "wall_seconds": 0.0,
                        "thread_cpu_seconds": 0.0,
                        "project_storage_growth_bytes": 0,
                        "checkpoint_write_bytes": 0,
                        "process_peak_rss_bytes": 0,
                    },
                    "executions": [], "render_reports": []}
        record = self.store._read(path)
        if not isinstance(record.get("executions"), list) or not isinstance(record.get("render_reports"), list):
            raise StorageError("run_telemetry_corrupt")
        record.setdefault("totals", {
            "wall_seconds": 0.0,
            "thread_cpu_seconds": 0.0,
            "project_storage_growth_bytes": 0,
            "checkpoint_write_bytes": 0,
            "process_peak_rss_bytes": 0,
        })
        return record

    def record_execution(self, record: dict[str, Any]) -> dict[str, Any]:
        required = {
            "branch_id", "from_minute", "target_minute", "computed_until", "status",
            "wall_seconds", "thread_cpu_seconds", "process_peak_rss_bytes",
            "project_storage_before_bytes", "project_storage_after_bytes", "project_storage_limit_bytes",
            "checkpoint_write_bytes",
        }
        memory_fields = {"run_peak_memory_bytes", "memory_attribution_status", "memory_attribution_reason"}
        if set(record) - required - {"failure_code", *memory_fields} or required - set(record):
            raise StorageError("telemetry_execution_fields_invalid")
        present_memory_fields = memory_fields & set(record)
        if present_memory_fields and present_memory_fields != memory_fields:
            raise StorageError("telemetry_memory_attribution_fields_invalid")
        for field in (
            "from_minute", "target_minute", "computed_until", "process_peak_rss_bytes",
            "project_storage_before_bytes", "project_storage_after_bytes",
            "project_storage_limit_bytes", "checkpoint_write_bytes",
        ):
            if type(record[field]) is not int or record[field] < 0:
                raise StorageError(f"invalid telemetry value: {field}")
        for field in ("wall_seconds", "thread_cpu_seconds"):
            _number(record[field], field)
        if record["status"] not in {"completed", "paused", "failed"}:
            raise StorageError("invalid telemetry execution status")
        if record.get("failure_code") is not None and (
            not isinstance(record["failure_code"], str) or len(record["failure_code"]) > 80
        ):
            raise StorageError("invalid telemetry failure code")
        if present_memory_fields:
            peak_memory = record["run_peak_memory_bytes"]
            status = record["memory_attribution_status"]
            reason = record["memory_attribution_reason"]
            if peak_memory is not None and (type(peak_memory) is not int or peak_memory < 0):
                raise StorageError("invalid telemetry value: run_peak_memory_bytes")
            if status not in {"measured", "unavailable"}:
                raise StorageError("invalid memory attribution status")
            if not isinstance(reason, str) or not reason or len(reason) > 500:
                raise StorageError("invalid memory attribution reason")
            if (status == "measured") != (peak_memory is not None):
                raise StorageError("memory attribution status and value disagree")
        stored = {"recorded_at": datetime.now(UTC).isoformat(), **record}
        with self.store.transaction():
            current = self.read()
            executions = [*current["executions"], stored][-100:]
            execution_count = current["execution_count"] + 1
            totals = {
                **current["totals"],
                "wall_seconds": current["totals"]["wall_seconds"] + float(record["wall_seconds"]),
                "thread_cpu_seconds": current["totals"]["thread_cpu_seconds"] + float(record["thread_cpu_seconds"]),
                "project_storage_growth_bytes": current["totals"]["project_storage_growth_bytes"]
                + max(0, record["project_storage_after_bytes"] - record["project_storage_before_bytes"]),
                "checkpoint_write_bytes": current["totals"]["checkpoint_write_bytes"] + record["checkpoint_write_bytes"],
                "process_peak_rss_bytes": max(
                    current["totals"]["process_peak_rss_bytes"], record["process_peak_rss_bytes"]
                ),
            }
            value = {"schema_version": 1, "execution_count": execution_count,
                     "totals": totals, "executions": executions, "render_reports": current["render_reports"]}
            self.store._write(self._path(), safe_document(value))
        return value

    def record_render(self, body: dict[str, Any]) -> dict[str, Any]:
        if (
            set(body) != {"kind", "branch_id", "samples", "fps", "p95_frame_ms", "visibility"}
            or body.get("kind") != "render"
        ):
            raise StorageError("render_telemetry_fields_invalid")
        if not isinstance(body["branch_id"], str) or not body["branch_id"] or len(body["branch_id"]) > 64:
            raise StorageError("invalid telemetry branch")
        if type(body["samples"]) is not int or not 0 <= body["samples"] <= 600:
            raise StorageError("invalid render sample count")
        if body["fps"] is not None and _number(body["fps"], "fps") > 1000:
            raise StorageError("invalid render fps")
        if body["p95_frame_ms"] is not None and _number(body["p95_frame_ms"], "p95_frame_ms") > 60_000:
            raise StorageError("invalid render frame duration")
        if not isinstance(body["visibility"], str) or body["visibility"] not in {
            "visible", "hidden", "prerender", "unloaded"
        }:
            raise StorageError("invalid render visibility")
        report = {"recorded_at": datetime.now(UTC).isoformat(), **body}
        with self.store.transaction():
            current = self.read()
            reports = [*current["render_reports"], report][-100:]
            value = {"schema_version": 1, "execution_count": current["execution_count"], "totals": current["totals"],
                     "executions": current["executions"], "render_reports": reports}
            self.store._write(self._path(), safe_document(value))
        return value
