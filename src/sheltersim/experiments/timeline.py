"""Exact boundary-minute reconstruction from durable checkpoints and recorded calls."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMErrorPolicy, LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMContractError, LLMTask, validate_task_output
from sheltersim.llm.provider import LLMRouter, OfflineProvider
from sheltersim.storage.projects import ProjectStore, StorageError, digest


class TimelineError(StorageError):
    """Unavailable minute, incompatible continuation or missing historical decision."""


class _MetricSink:
    trace_level = TraceLevel.OFF

    def __init__(self, metrics: MetricsRecorder) -> None:
        self.metrics = metrics

    def capture_metric(self, world: Any, *, minute: int | None = None) -> None:
        self.metrics.capture(world, minute=minute)

    def record_decision(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_social_decision(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_llm_call(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _IndexedMetricSink(_MetricSink):
    def __init__(self, metrics: MetricsRecorder, world: Any) -> None:
        super().__init__(metrics)
        self.world = world
        self.observations: list[dict[str, Any]] = []
        from sheltersim.contracts.policies import model_catalog
        self.policy_sources={key:value["assumption_source_id"] for key,value in model_catalog().items()}

    def record_decision(self, proposal: Any, *, observed_state: Any = None) -> None:
        agent = self.world.agents.get(proposal.agent_id)
        observed=dict(observed_state) if observed_state is not None else None
        omitted=observed is not None and len(json.dumps(observed,ensure_ascii=False).encode())>16384
        candidates=[{"action":c.action.value,"eligible":c.eligible,"trigger":c.trigger,
                     "utility":c.utility,"probability":c.probability,"urgency_priority":c.urgency_priority}
                    for c in proposal.candidates[:32]]
        self.observations.append({"record_kind": "actions", "minute": proposal.minute_index,
            "agent_id": proposal.agent_id, "household_id": current_household_id(self.world, proposal.agent_id) if agent else None,
            "zone_id": agent.zone_id if agent else None, "action": proposal.action.value,
            "target_id": proposal.target_id, "trigger": proposal.trigger,
            "policy_version": proposal.policy_version,"source_id":self.policy_sources.get(proposal.policy_version),
            "observed_state_hash": proposal.observed_state_hash,
            "observed_state":None if omitted else observed,"observed_state_status":"omitted_size_limit" if omitted else "recorded",
            "candidates":candidates,"candidate_count":len(proposal.candidates),"candidates_truncated":len(proposal.candidates)>32,
            "next_decision_at": proposal.next_decision_at,
            "status": "selected_proposal", "execution_evidence": "consult event history; selection is not proof of completion"})

    def record_social_decision(self, decision: Any) -> None:
        # Keep structured effects/evidence, not provider-authored dialogue/prose.
        row = {key:value for key,value in dict(decision).items() if key in {
            "minute", "minute_index", "agent_id", "actor_ids", "event_id", "causation_id",
            "source_id", "topic_id", "status", "kind", "accepted", "reason"}}
        row["minute"] = row.get("minute", row.get("minute_index", self.world.minute_index))
        row["record_kind"] = "dialogue"
        row["speech_status"] = "structured_interaction_only_no_recorded_utterance"
        self.observations.append(row)


class RecordedRouter(LLMRouter):
    """Reproduce original call records and outcomes, including recorded fallbacks.

    No network adapter is constructed. Cache-hit metadata alone is insufficient:
    social policies also persist the original costs/outcomes in WorldState.
    """

    def __init__(self, start: dict[str, Any], complete: dict[str, Any], settings: dict[str, Any]) -> None:
        replay_settings = settings_from_record(settings, mode=LLMMode.REPLAY, api_key=None)
        super().__init__(replay_settings, provider=OfflineProvider(), tape=ResponseTape())
        self.restore_snapshot(complete)
        self._future_records = list(self.records)
        self.records = self.records[:len(start["records"])]
        if [r.to_dict() for r in self.records] != start["records"]:
            raise TimelineError("recorded response history does not match checkpoint prefix")
        self.api_request_count = start["api_request_count"]
        self.spent_usd = start["spent_usd"]
        self.mode = LLMMode(start["mode"])

    def complete(self, task: LLMTask) -> str:
        if len(self.records) >= len(self._future_records):
            raise TimelineError("missing recorded LLM decision; history cannot call a provider")
        record = self._future_records[len(self.records)]
        expected = (task.stage.value, task.input_hash, task.state_hash, task.prompt_version, task.schema_version)
        actual = (record.stage, record.input_hash, record.state_hash, record.prompt_version, record.schema_version)
        if actual != expected:
            raise TimelineError("recorded LLM input/state/version does not match reconstructed history")
        response = self.tape.get(task.cache_key(self.settings.model_for(task.stage.value)))
        if record.fallback_used:
            text = task.fallback_text
        elif response is not None:
            text = response.text
        else:
            raise TimelineError("missing accepted response tape entry")
        try:
            validate_task_output(task, text)
        except LLMContractError as exc:
            raise TimelineError("historical LLM response failed structured validation") from exc
        self.records.append(deepcopy(record))
        self.api_request_count += record.api_request_count
        self.spent_usd += record.estimated_cost_usd
        return text


class ContinuingRouter(RecordedRouter):
    """Use recorded parent decisions first, then the explicitly supplied live router."""

    def __init__(self, start: dict[str, Any], complete: dict[str, Any], settings: dict[str, Any], live: LLMRouter) -> None:
        super().__init__(start, complete, settings)
        if live.mode is not self.mode:
            raise TimelineError("continuation router mode differs from saved history")
        self._live = live

    def complete(self, task: LLMTask) -> str:
        if len(self.records) < len(self._future_records):
            return super().complete(task)
        self._live.restore_snapshot(self.snapshot_state())
        response = self._live.complete(task)
        self.records = list(self._live.records)
        self.tape = self._live.tape
        self.api_request_count = self._live.api_request_count
        self.spent_usd = self._live.spent_usd
        return response


def public_router_settings(router: LLMRouter) -> dict[str, Any]:
    settings = router.settings
    result = {key: getattr(settings, key) for key in (
        "opus_model", "sonnet_model", "haiku_model", "prepare_model_family",
        "intervention_model_family", "review_model_family", "max_api_calls",
        "max_input_tokens", "max_output_tokens", "max_concurrency", "timeout_seconds", "budget_usd",
    )}
    result.update({"settings_schema_version": 3, "error_policy": settings.error_policy.value})
    for name in ("opus_price", "sonnet_price", "haiku_price"):
        price = getattr(settings, name)
        result[name] = {"input_usd_per_million": price.input_usd_per_million,
                        "output_usd_per_million": price.output_usd_per_million}
    return result


def settings_from_record(record: dict[str, Any], **overrides: Any) -> LLMSettings:
    values = dict(record)
    version = values.pop("settings_schema_version", 1)
    if type(version) is not int or version not in (1, 2, 3):
        raise TimelineError("unsupported recorded LLM settings version")
    for name in ("opus_price", "sonnet_price", "haiku_price"):
        if name in values:
            values[name] = ModelPrice(**values[name])
    return LLMSettings(**{**values, **overrides})


def compatible_router_settings(router: LLMRouter, recorded: dict[str, Any]) -> bool:
    current = public_router_settings(router)
    version = recorded.get("settings_schema_version", 1)
    if version == 3:
        return current == recorded
    # v1/v2 snapshots predate Sonnet and explicit stage-family routing. Preserve their
    # saved Opus/Haiku interpretation and reject attempts to continue with new routing.
    if any(getattr(router.settings, name) != "opus" for name in (
        "prepare_model_family", "intervention_model_family", "review_model_family")):
        return False
    if version == 2:
        return all(current.get(key) == value for key, value in recorded.items()
                   if key != "settings_schema_version")
    required = {
        "opus_model", "haiku_model", "max_api_calls", "max_input_tokens",
        "max_output_tokens", "max_concurrency", "timeout_seconds", "budget_usd",
    }
    defaults = LLMSettings(mode=LLMMode.OFFLINE, opus_model="claude-opus-5",
                           opus_price=ModelPrice(5.0, 25.0))
    if not required <= set(recorded):
        return False
    if (router.settings.opus_price != defaults.opus_price
            or router.settings.haiku_price != defaults.haiku_price
            or router.settings.error_policy is not LLMErrorPolicy.FALLBACK):
        return False
    return all(current.get(key) == value for key, value in recorded.items())


class TimelineStore:
    """One persisted Run's checkpoint index and exact branch reconstruction API."""

    def __init__(self, store: ProjectStore, project_id: str, run_id: str) -> None:
        self.store, self.project_id, self.run_id = store, project_id, run_id
        self.store.run(project_id, run_id)

    def _index_path(self, branch_id: str) -> Any:
        return self.store._path(self.project_id, "runs", self.run_id, "branches", branch_id, "timeline.json")

    def _index(self, branch_id: str) -> dict[str, Any]:
        path = self._index_path(branch_id)
        return self.store._read(path) if path.exists() else {"schema_version": 1, "checkpoints": {}}

    def record(self, runtime: BranchRuntime, *, fork_minute: int = 0, router: LLMRouter | None = None, observations: list[dict[str, Any]] | None = None) -> Checkpoint:
        if router is not None:
            runtime.runtime_state["llm"] = router.snapshot_state()
            if "replay_source" in runtime.runtime_state and len(router.records) > len(runtime.runtime_state["replay_source"]["records"]):
                runtime.runtime_state["replay_source"] = router.snapshot_state()
            runtime.runtime_state["llm_settings"] = public_router_settings(router)
        checkpoint = runtime.checkpoint()
        try:
            prior = self.store.branch_reference(self.project_id, self.run_id, runtime.branch_id)
        except StorageError:
            # Only a genuinely new branch is creatable; corrupt existing data fails closed.
            if self.store._path(self.project_id, "runs", self.run_id, "branches", runtime.branch_id, "branch.json").exists():
                raise
            prior = None
        reference = BranchReference(run_id=self.run_id, branch_id=runtime.branch_id, parent_branch_id=runtime.parent_branch_id, fork_minute=prior.fork_minute if prior else fork_minute, computed_until=checkpoint.simulation_minute)
        self.store.save_branch(self.project_id, reference, checkpoint, expected_checkpoint_hash=prior.checkpoint_hash if prior else None)
        from sheltersim.storage.history_index import HistoryIndex
        HistoryIndex(self.store, self.project_id, self.run_id).ingest(runtime.branch_id, checkpoint, observations=observations)
        return checkpoint

    def seek(self, branch_id: str, minute: int) -> BranchRuntime:
        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        if type(minute) is not int or not reference.fork_minute <= minute <= reference.computed_until:
            raise TimelineError("minute is outside this branch's computed range")
        from sheltersim.storage.history_index import HistoryIndex
        index = HistoryIndex(self.store, self.project_id, self.run_id)
        try:
            nearest = index.nearest(branch_id, minute, reference.checkpoint_hash)
        except StorageError as exc:
            if exc.code not in {"history_index_required", "history_index_stale"}:
                raise
            # Legacy reads remain compatible. Their explicit rebuild installs the
            # indexed route; only this compatibility path reads timeline.json.
            entries = self._index(branch_id)["checkpoints"]
            entries[str(reference.computed_until)] = reference.checkpoint_hash
            nearest = max(((int(key), value) for key,value in entries.items() if int(key)<=minute), default=None)
        if nearest is None:
            raise TimelineError("no checkpoint exists before requested minute")
        _, checkpoint_hash = nearest
        checkpoint = self.store.load_checkpoint(self.project_id, self.run_id, checkpoint_hash)
        if checkpoint.checkpoint_hash != checkpoint_hash:
            raise TimelineError("timeline checkpoint hash mismatch")
        runtime = BranchRuntime.from_checkpoint(checkpoint, branch_id=branch_id, parent_branch_id=reference.parent_branch_id)
        if minute == checkpoint.simulation_minute:
            return runtime
        start = checkpoint.runtime_state.get("llm")
        head_fields=self.store.checkpoint_fields(self.project_id,self.run_id,reference.checkpoint_hash,("runtime_state",))
        complete = head_fields["runtime_state"].get("llm")
        router = None
        if start is not None:
            if complete is None:
                raise TimelineError("continuation response tape is missing")
            router = RecordedRouter(start, complete, checkpoint.runtime_state.get("llm_settings", {}))
        elif complete is not None:
            raise TimelineError("LLM routing changed within history without a checkpoint")
        controller = OfflineSimulationController(runtime.world, _MetricSink(runtime.metrics), llm_router=router)
        TickEngine(runtime.world, phase_handler=controller).run(steps=minute - runtime.world.minute_index)
        if router is not None:
            runtime.runtime_state["llm"] = router.snapshot_state()
        return runtime

    def iter_worlds(self, branch_id: str, start: int, end: int) -> Any:
        """Sequential observational replay. Yielded worlds are reused; consume immediately."""
        reference=self.store.branch_reference(self.project_id,self.run_id,branch_id)
        if not reference.fork_minute<=start<=end<=reference.computed_until:
            raise TimelineError("replay interval outside computed range")
        runtime=self.seek(branch_id,start)
        router=None
        saved=runtime.runtime_state.get("llm")
        if saved is not None:
            fields=self.store.checkpoint_fields(self.project_id,self.run_id,reference.checkpoint_hash,("runtime_state",))
            complete=fields["runtime_state"].get("llm")
            if complete is None:
                raise TimelineError("missing recorded response history")
            router=RecordedRouter(saved,complete,runtime.runtime_state.get("llm_settings",{}))
        controller=OfflineSimulationController(runtime.world,_MetricSink(runtime.metrics),llm_router=router)
        engine=TickEngine(runtime.world,phase_handler=controller)
        while True:
            yield runtime.world
            if runtime.world.minute_index == end:
                break
            engine.step()

    def advance(self, branch_id: str, target_minute: int, *, router: LLMRouter | None = None, checkpoint_interval: int = 30, cancelled: Any = None) -> BranchRuntime:
        lease = self.store.claim_run(self.project_id, self.run_id)
        try:
            return self._advance_locked(branch_id, target_minute, router=router,
                                        checkpoint_interval=checkpoint_interval, cancelled=cancelled)
        finally:
            self.store.release_run(lease)

    def _advance_locked(self, branch_id: str, target_minute: int, *, router: LLMRouter | None = None, checkpoint_interval: int = 30, cancelled: Any = None) -> BranchRuntime:
        """Caller must hold the Run writer lease (Project jobs or advance())."""
        reference, head = self.store.load_branch(self.project_id, self.run_id, branch_id)
        if type(target_minute) is not int or not head.simulation_minute <= target_minute <= head.world_state["duration_minutes"]:
            raise TimelineError("advance target is outside the remaining simulation")
        if type(checkpoint_interval) is not int or checkpoint_interval <= 0:
            raise TimelineError("checkpoint interval must be positive")
        runtime = BranchRuntime.from_checkpoint(head, branch_id=branch_id, parent_branch_id=reference.parent_branch_id)
        if target_minute == head.simulation_minute:
            return runtime
        saved_llm = runtime.runtime_state.get("llm")
        if saved_llm is not None:
            if router is not None:
                if not compatible_router_settings(router, runtime.runtime_state.get("llm_settings", {})):
                    raise TimelineError("LLM settings changed; create a new Run")
                if "replay_source" in runtime.runtime_state:
                    router = ContinuingRouter(saved_llm, runtime.runtime_state["replay_source"], runtime.runtime_state.get("llm_settings", {}), router)
                else:
                    router.restore_snapshot(saved_llm)
            elif saved_llm["mode"] == "offline":
                router = LLMRouter(settings_from_record(runtime.runtime_state.get("llm_settings", {}), mode=LLMMode.OFFLINE), provider=OfflineProvider())
                router.restore_snapshot(saved_llm)
            elif "replay_source" in runtime.runtime_state:
                router = RecordedRouter(saved_llm, runtime.runtime_state["replay_source"], runtime.runtime_state.get("llm_settings", {}))
            else:
                raise TimelineError("live continuation needs the configured router; offline history cannot change mode")
        elif router is not None and head.simulation_minute > 0:
            raise TimelineError("cannot add a provider to an existing offline history")
        sink = _IndexedMetricSink(runtime.metrics, runtime.world)
        controller = OfflineSimulationController(runtime.world, sink, llm_router=router)
        engine = TickEngine(runtime.world, phase_handler=controller)
        while runtime.world.minute_index < target_minute:
            if cancelled is not None and cancelled():
                break
            engine.step()
            if runtime.world.minute_index % checkpoint_interval == 0:
                self.record(runtime, fork_minute=reference.fork_minute, router=router, observations=sink.observations)
                sink.observations.clear()
        if runtime.world.minute_index == head.simulation_minute:
            return BranchRuntime.from_checkpoint(head, branch_id=branch_id, parent_branch_id=reference.parent_branch_id)
        self.record(runtime, fork_minute=reference.fork_minute, router=router, observations=sink.observations)
        return runtime

    def fork(self, branch_id: str, minute: int, child_id: str, *, program: Any = None, approved_program_hash: str | None = None) -> BranchRuntime:
        # A destination identity must be new, even if its content would match.
        if self.store._path(self.project_id, "runs", self.run_id, "branches", child_id, "branch.json").exists():
            raise TimelineError("child branch already exists")
        source = self.seek(branch_id, minute)
        child = source.fork(child_id)
        child.runtime_state["fork_origin"] = {"branch_id": branch_id, "minute": minute, "state_hash": digest(source.world.to_dict()), "checkpoint_hash": source.checkpoint().checkpoint_hash}
        if program is not None:
            if approved_program_hash != digest(program.model_dump(mode="json")):
                raise TimelineError("EffectProgram approval hash does not match preview")
            child.runtime_state.pop("replay_source", None)
            child.apply_effect_program(program)
        elif "llm" in source.runtime_state:
            _, head = self.store.load_branch(self.project_id, self.run_id, branch_id)
            child.runtime_state["replay_source"] = deepcopy(head.runtime_state["llm"])
        self.record(child, fork_minute=minute)
        return child

    def frame(self, branch_id: str, minute: int) -> dict[str, Any]:
        runtime = self.seek(branch_id, minute)
        from sheltersim.inspection.frames import world_frame
        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        return world_frame(runtime.world, run_id=self.run_id, branch_id=branch_id, computed_until=reference.computed_until)
