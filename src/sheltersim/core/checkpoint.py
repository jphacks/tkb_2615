"""Versioned, integrity-checked checkpoints independent of WorldState schema versions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sheltersim.contracts.policies import model_signatures
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.experiments.metrics import MetricsRecorder


class CheckpointError(ValueError):
    """Raised when a checkpoint is corrupt or incompatible with this runtime."""


@dataclass(frozen=True)
class Checkpoint:
    """Detached save image of world state plus application-owned continuation state."""

    schema_version: int
    world_state_schema_version: int
    scenario_id: str
    simulation_minute: int
    state_hash: str
    model_versions: dict[str, str]
    world_state: dict[str, Any]
    metrics_state: dict[str, Any]
    runtime_state: dict[str, Any]
    checkpoint_hash: str

    CURRENT_SCHEMA_VERSION = 1

    @classmethod
    def capture(
        cls,
        world: WorldState,
        *,
        metrics: MetricsRecorder | None = None,
        runtime_state: dict[str, Any] | None = None,
        model_versions: dict[str, str] | None = None,
    ) -> Checkpoint:
        """Capture every serialized WorldState field and optional controller state."""
        world.validate()
        world_document = world.to_dict()
        metric_document = (
            metrics.to_dict() if metrics is not None else {"schema_version": 3, "snapshots": []}
        )
        versions = _model_versions(world)
        if model_versions is not None:
            versions.update(deepcopy(model_versions))
        body = {
            "schema_version": cls.CURRENT_SCHEMA_VERSION,
            "world_state_schema_version": world.schema_version,
            "scenario_id": world.scenario_id,
            "simulation_minute": world.minute_index,
            "state_hash": _digest(world_document),
            "model_versions": versions,
            "world_state": world_document,
            "metrics_state": metric_document,
            "runtime_state": deepcopy(runtime_state or {}),
        }
        try:
            checkpoint_hash = _digest(body)
        except (TypeError, ValueError) as exc:
            raise CheckpointError(
                f"checkpoint state must contain finite JSON values: {exc}"
            ) from exc
        checkpoint = cls(**body, checkpoint_hash=checkpoint_hash)
        checkpoint._validate()
        return checkpoint

    def restore_world(
        self,
        *,
        expected_model_versions: dict[str, str] | None = None,
    ) -> WorldState:
        """Recreate an independent world and optionally enforce runtime model versions."""
        self._validate()
        if expected_model_versions is not None and expected_model_versions != self.model_versions:
            raise CheckpointError("checkpoint model versions do not match the active runtime")
        try:
            return WorldState.from_dict(self._world_document_for_restore())
        except StateInvariantError as exc:
            raise CheckpointError(f"checkpoint WorldState cannot be restored: {exc}") from exc

    def _legacy_queue_migration_required(self) -> bool:
        installed = model_signatures(self.world_state.get("model_configs", {}))
        recorded = {
            key: value for key, value in self.model_versions.items()
            if key.startswith(("policy:", "simulator:", "state:", "operator:"))
            or key == "model_suite"
        }
        if recorded == installed:
            return False
        queue_signature = "simulator:facility_queues_v1"
        configs = self.world_state.get("model_configs", {})
        state = self.world_state.get("model_state", {})
        compatible = (
            queue_signature in installed
            and queue_signature not in recorded
            and "facility_queues_v1" not in state
            and "facility_geometry" in configs
            and {key: value for key, value in recorded.items() if key != queue_signature}
            == {key: value for key, value in installed.items() if key != queue_signature}
        )
        if not compatible and (
            recorded or "state_contracts" in configs
            or any(key.startswith("operator:") for key in installed)
        ):
            raise CheckpointError(
                "installed model implementation/state versions differ; explicit migration required"
            )
        return compatible

    def _world_document_for_restore(self) -> dict[str, Any]:
        world_document = deepcopy(self.world_state)
        if self._legacy_queue_migration_required():
            from sheltersim.domain.facility_queues import (
                FACILITY_QUEUES_LEGACY_MODE,
                FACILITY_QUEUES_LEGACY_MODE_KEY,
                independent_queues_required,
            )
            if not independent_queues_required(world_document.get("model_configs", {})):
                raise CheckpointError("legacy queue migration was requested for an unrelated scenario")
            world_document["model_state"][FACILITY_QUEUES_LEGACY_MODE_KEY] = dict(
                FACILITY_QUEUES_LEGACY_MODE
            )
        return world_document

    def restore_metrics(self) -> MetricsRecorder:
        """Restore KPI samples and accumulation history captured with this checkpoint."""
        self._validate()
        return MetricsRecorder.from_dict(deepcopy(self.metrics_state))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached checkpoint document."""
        self._validate()
        return {
            "schema_version": self.schema_version,
            "world_state_schema_version": self.world_state_schema_version,
            "scenario_id": self.scenario_id,
            "simulation_minute": self.simulation_minute,
            "state_hash": self.state_hash,
            "model_versions": deepcopy(self.model_versions),
            "world_state": deepcopy(self.world_state),
            "metrics_state": deepcopy(self.metrics_state),
            "runtime_state": deepcopy(self.runtime_state),
            "checkpoint_hash": self.checkpoint_hash,
        }

    def to_json(self) -> str:
        """Serialize canonically for storage or deterministic comparison."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> Checkpoint:
        """Load a checkpoint only when both independent schemas and hashes match."""
        expected_fields = {
            "schema_version",
            "world_state_schema_version",
            "scenario_id",
            "simulation_minute",
            "state_hash",
            "model_versions",
            "world_state",
            "metrics_state",
            "runtime_state",
            "checkpoint_hash",
        }
        if not isinstance(document, dict) or set(document) != expected_fields:
            raise CheckpointError("checkpoint fields do not match the supported schema")
        if document.get("schema_version") != cls.CURRENT_SCHEMA_VERSION:
            raise CheckpointError(
                f"unsupported checkpoint schema version: {document.get('schema_version')!r}"
            )
        checkpoint = cls(**deepcopy(document))
        checkpoint._validate()
        return checkpoint

    @classmethod
    def from_json(cls, value: str) -> Checkpoint:
        """Parse a JSON checkpoint document and fail closed on malformed input."""
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"invalid checkpoint JSON: {exc}") from exc
        return cls.from_dict(document)

    def save(self, path: str | Path) -> None:
        """Atomically write this checkpoint as UTF-8 JSON."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(self.to_json() + "\n", encoding="utf-8")
        temporary.replace(destination)

    @classmethod
    def load(cls, path: str | Path) -> Checkpoint:
        """Load and validate a checkpoint from disk."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def _validate(self) -> None:
        if self.schema_version != self.CURRENT_SCHEMA_VERSION:
            raise CheckpointError(f"unsupported checkpoint schema version: {self.schema_version}")
        if self.world_state_schema_version not in (5, 6):
            raise CheckpointError(
                f"unsupported WorldState schema version in checkpoint (expected v5 or v6): "
                f"{self.world_state_schema_version}"
            )
        if not isinstance(self.model_versions, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.model_versions.items()
        ):
            raise CheckpointError("checkpoint model_versions must map names to strings")
        if not isinstance(self.world_state, dict):
            raise CheckpointError("checkpoint world_state must be an object")
        if not isinstance(self.metrics_state, dict) or not isinstance(self.runtime_state, dict):
            raise CheckpointError("checkpoint runtime and metric state must be objects")
        if self.world_state.get("schema_version") != self.world_state_schema_version:
            raise CheckpointError("checkpoint WorldState schema marker does not match its payload")
        if self.world_state.get("scenario_id") != self.scenario_id:
            raise CheckpointError("checkpoint scenario ID does not match its WorldState")
        if self.world_state.get("minute_index") != self.simulation_minute:
            raise CheckpointError("checkpoint minute does not match its WorldState")
        if _digest(self.world_state) != self.state_hash:
            raise CheckpointError("checkpoint WorldState integrity hash mismatch")
        try:
            WorldState.from_dict(self._world_document_for_restore())
            MetricsRecorder.from_dict(deepcopy(self.metrics_state))
        except (StateInvariantError, ValueError, TypeError) as exc:
            raise CheckpointError(f"checkpoint payload is invalid: {exc}") from exc
        body = self._body_dict()
        if _digest(body) != self.checkpoint_hash:
            raise CheckpointError("checkpoint integrity hash mismatch")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "world_state_schema_version": self.world_state_schema_version,
            "scenario_id": self.scenario_id,
            "simulation_minute": self.simulation_minute,
            "state_hash": self.state_hash,
            "model_versions": self.model_versions,
            "world_state": self.world_state,
            "metrics_state": self.metrics_state,
            "runtime_state": self.runtime_state,
        }


@dataclass(frozen=True)
class CheckpointSchedule:
    """Configurable cadence for checkpoint snapshots, defaulting to thirty minutes."""

    interval_minutes: int = 30

    def __post_init__(self) -> None:
        if (
            not isinstance(self.interval_minutes, int)
            or isinstance(self.interval_minutes, bool)
            or self.interval_minutes <= 0
        ):
            raise ValueError("checkpoint interval_minutes must be a positive integer")

    def should_capture(self, minute_index: int) -> bool:
        """Whether a state at this simulation minute falls on the cadence boundary."""
        if not isinstance(minute_index, int) or isinstance(minute_index, bool) or minute_index < 0:
            raise ValueError("minute_index must be a non-negative integer")
        return minute_index % self.interval_minutes == 0


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_versions(world: WorldState) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name, config in sorted(world.model_configs.items()):
        declared = config.get("model_version", config.get("model_id", config.get("version")))
        versions[name] = str(declared) if declared is not None else _digest(config)
    effect_state = world.model_state.get("effect_program_v1")
    if effect_state is not None:
        versions["effect_program_interpreter"] = str(effect_state.get("schema_version", "invalid"))
    versions.update(model_signatures(world.model_configs))
    return versions
