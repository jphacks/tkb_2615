"""Exact boundary-minute reconstruction from durable checkpoints and recorded calls."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
from sheltersim.core.collection import fewer_collections
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

        self.policy_sources = {
            key: value["assumption_source_id"] for key, value in model_catalog().items()
        }

    def record_decision(self, proposal: Any, *, observed_state: Any = None) -> None:
        agent = self.world.agents.get(proposal.agent_id)
        observed = dict(observed_state) if observed_state is not None else None
        omitted = (
            observed is not None and len(json.dumps(observed, ensure_ascii=False).encode()) > 16384
        )
        candidates = [
            {
                "action": c.action.value,
                "eligible": c.eligible,
                "trigger": c.trigger,
                "utility": c.utility,
                "probability": c.probability,
                "urgency_priority": c.urgency_priority,
            }
            for c in proposal.candidates[:32]
        ]
        self.observations.append(
            {
                "record_kind": "actions",
                "minute": proposal.minute_index,
                "agent_id": proposal.agent_id,
                "household_id": current_household_id(self.world, proposal.agent_id)
                if agent
                else None,
                "zone_id": agent.zone_id if agent else None,
                "action": proposal.action.value,
                "target_id": proposal.target_id,
                "trigger": proposal.trigger,
                "policy_version": proposal.policy_version,
                "source_id": self.policy_sources.get(proposal.policy_version),
                "observed_state_hash": proposal.observed_state_hash,
                "observed_state": None if omitted else observed,
                "observed_state_status": "omitted_size_limit" if omitted else "recorded",
                "candidates": candidates,
                "candidate_count": len(proposal.candidates),
                "candidates_truncated": len(proposal.candidates) > 32,
                "next_decision_at": proposal.next_decision_at,
                "status": "selected_proposal",
                "execution_evidence": "consult event history; selection is not proof of completion",
            }
        )

    def record_social_decision(self, decision: Any) -> None:
        # Keep structured effects/evidence, not provider-authored dialogue/prose.
        row = {
            key: value
            for key, value in dict(decision).items()
            if key
            in {
                "minute",
                "minute_index",
                "agent_id",
                "actor_ids",
                "event_id",
                "causation_id",
                "source_id",
                "topic_id",
                "status",
                "kind",
                "accepted",
                "reason",
            }
        }
        row["minute"] = row.get("minute", row.get("minute_index", self.world.minute_index))
        row["record_kind"] = "dialogue"
        row["speech_status"] = "structured_interaction_only_no_recorded_utterance"
        self.observations.append(row)


class RecordedRouter(LLMRouter):
    """Reproduce original call records and outcomes, including recorded fallbacks.

    No network adapter is constructed. Cache-hit metadata alone is insufficient:
    social policies also persist the original costs/outcomes in WorldState.
    """

    def __init__(
        self, start: dict[str, Any], complete: dict[str, Any], settings: dict[str, Any]
    ) -> None:
        replay_settings = settings_from_record(settings, mode=LLMMode.REPLAY, api_key=None)
        super().__init__(replay_settings, provider=OfflineProvider(), tape=ResponseTape())
        self.restore_snapshot(complete)
        self._future_records = list(self.records)
        self.records = self.records[: len(start["records"])]
        if [r.to_dict() for r in self.records] != start["records"]:
            raise TimelineError("recorded response history does not match checkpoint prefix")
        self.api_request_count = start["api_request_count"]
        self.spent_usd = start["spent_usd"]
        self.mode = LLMMode(start["mode"])

    def history_records(self) -> tuple[Any, ...]:
        # The full recorded history, including calls after the replay position.
        return (*self._future_records, *self.records[len(self._future_records) :])

    def complete(self, task: LLMTask) -> str:
        if len(self.records) >= len(self._future_records):
            raise TimelineError("missing recorded LLM decision; history cannot call a provider")
        record = self._future_records[len(self.records)]
        expected = (
            task.stage.value,
            task.input_hash,
            task.state_hash,
            task.prompt_version,
            task.schema_version,
        )
        actual = (
            record.stage,
            record.input_hash,
            record.state_hash,
            record.prompt_version,
            record.schema_version,
        )
        if actual != expected:
            raise TimelineError(
                "recorded LLM input/state/version does not match reconstructed history"
            )
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

    def __init__(
        self,
        start: dict[str, Any],
        complete: dict[str, Any],
        settings: dict[str, Any],
        live: LLMRouter,
    ) -> None:
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
    result = {
        key: getattr(settings, key)
        for key in (
            "opus_model",
            "sonnet_model",
            "haiku_model",
            "prepare_model_family",
            "intervention_model_family",
            "review_model_family",
            "max_api_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_concurrency",
            "timeout_seconds",
            "budget_usd",
        )
    }
    result.update({"settings_schema_version": 3, "error_policy": settings.error_policy.value})
    for name in ("opus_price", "sonnet_price", "haiku_price"):
        price = getattr(settings, name)
        result[name] = {
            "input_usd_per_million": price.input_usd_per_million,
            "output_usd_per_million": price.output_usd_per_million,
        }
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
    if any(
        getattr(router.settings, name) != "opus"
        for name in ("prepare_model_family", "intervention_model_family", "review_model_family")
    ):
        return False
    if version == 2:
        return all(
            current.get(key) == value
            for key, value in recorded.items()
            if key != "settings_schema_version"
        )
    required = {
        "opus_model",
        "haiku_model",
        "max_api_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_concurrency",
        "timeout_seconds",
        "budget_usd",
    }
    defaults = LLMSettings(
        mode=LLMMode.OFFLINE, opus_model="claude-opus-5", opus_price=ModelPrice(5.0, 25.0)
    )
    if not required <= set(recorded):
        return False
    if (
        router.settings.opus_price != defaults.opus_price
        or router.settings.haiku_price != defaults.haiku_price
        or router.settings.error_policy is not LLMErrorPolicy.FALLBACK
    ):
        return False
    return all(current.get(key) == value for key, value in recorded.items())


# A computation with at least this many saves indexes them in another process
# (storage/index_writer.py); a shorter one indexes each save before computing on.
BACKGROUND_INDEX_MIN_SAVES = 8


class _SaveThread:
    """Writes one computation's saves on one thread, in order, while the computation goes on.

    A save's document is detached once captured, and nothing here shortens or reorders
    what a save does: its files are written, synced and published as without the thread.
    The computation only stops waiting for the disk, which needs no GIL. Everything that
    needs the GIL for long (checks, hashes, compression) stays on the computation's
    thread, since sharing the GIL would slow both. Each save starts after the one before
    it has finished, and a save that failed stops the computation at its next save or at
    its end. Until then the head on disk is the last save finished.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint-save")
        self._last: Future[None] | None = None

    def wait(self) -> None:
        """Wait for the last save, and raise its error if it failed."""
        last, self._last = self._last, None
        if last is not None:
            last.result()

    def submit(self, write: Any) -> None:
        self.wait()
        self._last = self._pool.submit(write)

    def close(self) -> None:
        """Wait for the last save and end the thread; raise the save's error if it failed."""
        try:
            self.wait()
        finally:
            self._pool.shutdown()

    def abandon(self) -> None:
        """close() for a computation that is failing already: its error is the one to report."""
        try:
            self.close()
        except Exception:  # noqa: BLE001 - reported by the computation's own error
            pass


class TimelineStore:
    """One persisted Run's checkpoint index and exact branch reconstruction API."""

    def __init__(self, store: ProjectStore, project_id: str, run_id: str) -> None:
        self.store, self.project_id, self.run_id = store, project_id, run_id
        self.store.run(project_id, run_id)

    def _index_path(self, branch_id: str) -> Any:
        return self.store._path(
            self.project_id, "runs", self.run_id, "branches", branch_id, "timeline.json"
        )

    def _index(self, branch_id: str) -> dict[str, Any]:
        path = self._index_path(branch_id)
        return self.store._read(path) if path.exists() else {"schema_version": 1, "checkpoints": {}}

    def record(
        self,
        runtime: BranchRuntime,
        *,
        fork_minute: int = 0,
        router: LLMRouter | None = None,
        observations: list[dict[str, Any]] | None = None,
        history_cache: CheckpointHistoryCache | None = None,
        inherit_index_from: str | None = None,
        indexer: Any = None,
        saver: _SaveThread | None = None,
    ) -> Checkpoint:
        """Capture the runtime and save it; with a saver, on the saver's thread.

        The returned Checkpoint is durable only without a saver.
        """
        if saver is not None:
            # The head read below, and what a capture reuses, are the last save's.
            saver.wait()
        if router is not None:
            runtime.runtime_state["llm"] = router.snapshot_state()
            if "replay_source" in runtime.runtime_state and len(router.records) > len(
                runtime.runtime_state["replay_source"]["records"]
            ):
                runtime.runtime_state["replay_source"] = router.snapshot_state()
            runtime.runtime_state["llm_settings"] = public_router_settings(router)
        checkpoint = runtime.checkpoint(history_cache=history_cache)
        try:
            prior = self.store.branch_reference(self.project_id, self.run_id, runtime.branch_id)
        except StorageError:
            # Only a genuinely new branch is creatable; corrupt existing data fails closed.
            if self.store._path(
                self.project_id, "runs", self.run_id, "branches", runtime.branch_id, "branch.json"
            ).exists():
                raise
            prior = None
        reference = BranchReference(
            run_id=self.run_id,
            branch_id=runtime.branch_id,
            parent_branch_id=runtime.parent_branch_id,
            fork_minute=prior.fork_minute if prior else fork_minute,
            computed_until=checkpoint.simulation_minute,
        )
        # runtime.checkpoint() captured and validated this object just now and it is not
        # shared, so the store need not validate it again. Whatever fails here stops the
        # computation now; only the writing may run on the saver's thread.
        write_save = self.store.save_captured_branch(
            self.project_id,
            reference,
            checkpoint,
            expected_checkpoint_hash=prior.checkpoint_hash if prior else None,
            deferred=saver is not None,
        )
        # The world goes on after this method, so its frame is rendered here.
        frame = self._frame(runtime, checkpoint)
        # The caller may clear its list as soon as this method returns.
        observed = None if observations is None else list(observations)

        def finish() -> None:
            used_bytes = None if write_save is None else write_save()
            if indexer is not None:
                # A long computation's saves are indexed in another process, in order.
                indexer.submit(checkpoint, observed or [])
            else:
                from sheltersim.storage.history_index import HistoryIndex

                HistoryIndex(self.store, self.project_id, self.run_id).ingest(
                    runtime.branch_id,
                    checkpoint,
                    observations=observed,
                    inherit_from=inherit_index_from,
                )
            self._cache_frame(frame, checkpoint, used_bytes)

        if saver is None:
            finish()
        else:
            saver.submit(finish)
        return checkpoint

    def _frame(self, runtime: BranchRuntime, checkpoint: Checkpoint) -> dict[str, Any] | None:
        """The saved boundary rendered from the writer's in-memory world for later reads."""
        from sheltersim.inspection.frames import world_frame

        try:
            return world_frame(
                runtime.world,
                run_id=self.run_id,
                branch_id=runtime.branch_id,
                computed_until=checkpoint.simulation_minute,
                state_hash=checkpoint.state_hash,
            )
        except (OSError, StorageError):
            return None

    def _cache_frame(
        self, frame: dict[str, Any] | None, checkpoint: Checkpoint, used_bytes: int | None
    ) -> None:
        """Keep a frame of a durable Checkpoint; a failure only means readers reconstruct it."""
        from sheltersim.storage.frames import FrameCache

        if frame is None:
            return
        try:
            FrameCache(self.store, self.project_id, self.run_id).put(
                checkpoint.checkpoint_hash, frame, used_bytes=used_bytes
            )
        except (OSError, StorageError):
            pass

    def _nearest_checkpoint(
        self,
        branch_id: str,
        minute: int,
        reference: BranchReference,
    ) -> tuple[int, str]:
        from sheltersim.storage.history_index import HistoryIndex

        index = HistoryIndex(self.store, self.project_id, self.run_id)
        try:
            nearest = index.nearest(
                branch_id, minute, reference.checkpoint_hash, reference.computed_until
            )
        except StorageError as exc:
            if exc.code not in {"history_index_required", "history_index_stale"}:
                raise
            # Legacy reads remain compatible. Their explicit rebuild installs the
            # indexed route; only this compatibility path reads timeline.json.
            entries = self._index(branch_id)["checkpoints"]
            entries[str(reference.computed_until)] = reference.checkpoint_hash
            nearest = max(
                ((int(key), value) for key, value in entries.items() if int(key) <= minute),
                default=None,
            )
        if nearest is None:
            raise TimelineError("no checkpoint exists before requested minute")
        return nearest

    def seek(self, branch_id: str, minute: int) -> BranchRuntime:
        return self._seek(branch_id, minute)[0]

    def _seek(
        self, branch_id: str, minute: int, *, keep: bool = True
    ) -> tuple[BranchRuntime, Checkpoint]:
        """seek(), and the saved Checkpoint its runtime was restored from.

        With keep=False the store keeps no copy of the restored world for later reads.
        """
        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        if (
            type(minute) is not int
            or not reference.fork_minute <= minute <= reference.computed_until
        ):
            raise TimelineError("minute is outside this branch's computed range")
        _, checkpoint_hash = self._nearest_checkpoint(branch_id, minute, reference)
        checkpoint, restored = self.store.load_restored_checkpoint(
            self.project_id, self.run_id, checkpoint_hash, keep=keep
        )
        if checkpoint.checkpoint_hash != checkpoint_hash:
            raise TimelineError("timeline checkpoint hash mismatch")
        runtime = BranchRuntime.from_checkpoint(
            checkpoint,
            branch_id=branch_id,
            parent_branch_id=reference.parent_branch_id,
            _restored=restored,
        )
        if minute == checkpoint.simulation_minute:
            return runtime, checkpoint
        start = checkpoint.runtime_state.get("llm")
        head_fields = self.store.checkpoint_fields(
            self.project_id, self.run_id, reference.checkpoint_hash, ("runtime_state",)
        )
        complete = head_fields["runtime_state"].get("llm")
        router = None
        if start is not None:
            if complete is None:
                raise TimelineError("continuation response tape is missing")
            router = RecordedRouter(
                start, complete, checkpoint.runtime_state.get("llm_settings", {})
            )
        elif complete is not None:
            raise TimelineError("LLM routing changed within history without a checkpoint")
        controller = OfflineSimulationController(
            runtime.world, _MetricSink(runtime.metrics), llm_router=router
        )
        TickEngine(runtime.world, phase_handler=controller).run(
            steps=minute - runtime.world.minute_index
        )
        if router is not None:
            runtime.runtime_state["llm"] = router.snapshot_state()
        return runtime, checkpoint

    def iter_worlds(
        self,
        branch_id: str,
        start: int,
        end: int,
        *,
        history_cache: CheckpointHistoryCache | None = None,
    ) -> Any:
        """Sequential observational replay. Yielded worlds are reused; consume immediately.

        A history_cache is seeded with the EventLog of the Checkpoint the replay
        starts from, for a caller that captures documents of the yielded worlds.
        """
        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        if not reference.fork_minute <= start <= end <= reference.computed_until:
            raise TimelineError("replay interval outside computed range")
        runtime, checkpoint = self._seek(branch_id, start)
        if history_cache is not None:
            history_cache.seed_events(runtime.world.event_log, checkpoint.world_state["event_log"])
        # Observe the exact saved boundary before controller initialization can
        # install any missing optional state. A one-frame read needs no controller.
        yield runtime.world
        if runtime.world.minute_index == end:
            return
        router = None
        saved = runtime.runtime_state.get("llm")
        if saved is not None:
            fields = self.store.checkpoint_fields(
                self.project_id, self.run_id, reference.checkpoint_hash, ("runtime_state",)
            )
            complete = fields["runtime_state"].get("llm")
            if complete is None:
                raise TimelineError("missing recorded response history")
            router = RecordedRouter(saved, complete, runtime.runtime_state.get("llm_settings", {}))
        controller = OfflineSimulationController(
            runtime.world, _MetricSink(runtime.metrics), llm_router=router
        )
        engine = TickEngine(runtime.world, phase_handler=controller)
        while runtime.world.minute_index < end:
            engine.step()
            yield runtime.world

    def advance(
        self,
        branch_id: str,
        target_minute: int,
        *,
        router: LLMRouter | None = None,
        checkpoint_interval: int = 30,
        cancelled: Any = None,
    ) -> BranchRuntime:
        lease = self.store.claim_branch(self.project_id, self.run_id, branch_id)
        try:
            return self._advance_locked(
                branch_id,
                target_minute,
                router=router,
                checkpoint_interval=checkpoint_interval,
                cancelled=cancelled,
            )
        finally:
            self.store.release_run(lease)

    def _advance_locked(
        self,
        branch_id: str,
        target_minute: int,
        *,
        router: LLMRouter | None = None,
        checkpoint_interval: int = 30,
        cancelled: Any = None,
    ) -> BranchRuntime:
        """Caller must hold the Run writer lease (Project jobs or advance())."""
        with fewer_collections():
            return self._advance_collecting_less(
                branch_id,
                target_minute,
                router=router,
                checkpoint_interval=checkpoint_interval,
                cancelled=cancelled,
            )

    def _advance_collecting_less(
        self,
        branch_id: str,
        target_minute: int,
        *,
        router: LLMRouter | None,
        checkpoint_interval: int,
        cancelled: Any,
    ) -> BranchRuntime:
        # The head is replaced as the computation saves, so it is seldom read again;
        # keeping a copy of its world would hold memory for the whole computation.
        reference, head, restored = self.store.load_restored_branch(
            self.project_id, self.run_id, branch_id, keep=False
        )
        if (
            type(target_minute) is not int
            or not head.simulation_minute <= target_minute <= head.world_state["duration_minutes"]
        ):
            raise TimelineError("advance target is outside the remaining simulation")
        if type(checkpoint_interval) is not int or checkpoint_interval <= 0:
            raise TimelineError("checkpoint interval must be positive")
        runtime = BranchRuntime.from_checkpoint(
            head,
            branch_id=branch_id,
            parent_branch_id=reference.parent_branch_id,
            _restored=restored,
        )
        if target_minute == head.simulation_minute:
            return runtime
        saved_llm = runtime.runtime_state.get("llm")
        if saved_llm is not None:
            if router is not None:
                if not compatible_router_settings(
                    router, runtime.runtime_state.get("llm_settings", {})
                ):
                    raise TimelineError("LLM settings changed; create a new Run")
                if "replay_source" in runtime.runtime_state:
                    router = ContinuingRouter(
                        saved_llm,
                        runtime.runtime_state["replay_source"],
                        runtime.runtime_state.get("llm_settings", {}),
                        router,
                    )
                else:
                    router.restore_snapshot(saved_llm)
            elif saved_llm["mode"] == "offline":
                router = LLMRouter(
                    settings_from_record(
                        runtime.runtime_state.get("llm_settings", {}), mode=LLMMode.OFFLINE
                    ),
                    provider=OfflineProvider(),
                )
                router.restore_snapshot(saved_llm)
            elif "replay_source" in runtime.runtime_state:
                router = RecordedRouter(
                    saved_llm,
                    runtime.runtime_state["replay_source"],
                    runtime.runtime_state.get("llm_settings", {}),
                )
            else:
                raise TimelineError(
                    "live continuation needs the configured router; offline history cannot "
                    "change mode"
                )
        elif router is not None and head.simulation_minute > 0:
            raise TimelineError("cannot add a provider to an existing offline history")
        sink = _IndexedMetricSink(runtime.metrics, runtime.world)
        controller = OfflineSimulationController(runtime.world, sink, llm_router=router)
        engine = TickEngine(runtime.world, phase_handler=controller)
        last_recorded_minute = head.simulation_minute
        # Each save reuses the history this loop saved before; the first one reuses
        # the EventLog of the head it continues from.
        history_cache = CheckpointHistoryCache()
        history_cache.seed_events(runtime.world.event_log, head.world_state["event_log"])
        indexer = None
        if (target_minute - head.simulation_minute) // checkpoint_interval >= (
            BACKGROUND_INDEX_MIN_SAVES
        ):
            from sheltersim.storage.index_writer import BackgroundIndexer

            indexer = BackgroundIndexer(self.store, self.project_id, self.run_id, branch_id)
        saver = _SaveThread()
        try:
            while runtime.world.minute_index < target_minute:
                if cancelled is not None and cancelled():
                    break
                engine.step()
                if runtime.world.minute_index % checkpoint_interval == 0:
                    self.record(
                        runtime,
                        fork_minute=reference.fork_minute,
                        router=router,
                        observations=sink.observations,
                        history_cache=history_cache,
                        indexer=indexer,
                        saver=saver,
                    )
                    sink.observations.clear()
                    last_recorded_minute = runtime.world.minute_index
            if runtime.world.minute_index == head.simulation_minute:
                saver.close()
                if indexer is not None:
                    indexer.close()
                return BranchRuntime.from_checkpoint(
                    head, branch_id=branch_id, parent_branch_id=reference.parent_branch_id
                )
            # A boundary that ended on the interval is already saved; record only an
            # unsaved remainder (odd target or cancellation between boundaries).
            if runtime.world.minute_index != last_recorded_minute:
                self.record(
                    runtime,
                    fork_minute=reference.fork_minute,
                    router=router,
                    observations=sink.observations,
                    history_cache=history_cache,
                    indexer=indexer,
                    saver=saver,
                )
            # The computation ends only once its saves are durable.
            saver.close()
        except BaseException:
            saver.abandon()
            if indexer is not None:
                try:
                    indexer.close()
                except StorageError:
                    pass  # The computation's own error is the one to report.
            raise
        if indexer is not None:
            # The computation ends only once its saves are indexed.
            indexer.close()
        return runtime

    def fork(
        self,
        branch_id: str,
        minute: int,
        child_id: str,
        *,
        program: Any = None,
        approved_program_hash: str | None = None,
        expected_state_hash: str | None = None,
    ) -> BranchRuntime:
        """expected_state_hash, when given, is the source state an approved preview saw."""
        # A destination identity must be new, even if its content would match.
        if self.store._path(
            self.project_id, "runs", self.run_id, "branches", child_id, "branch.json"
        ).exists():
            raise TimelineError("child branch already exists")
        # A fork reads its source once; a kept copy would serve no later read.
        source, saved = self._seek(branch_id, minute, keep=False)
        # The child is taken from this Checkpoint, which also holds both hashes of
        # the source.
        if (
            minute == saved.simulation_minute
            and source._checkpoint_runtime_state() == saved.runtime_state
        ):
            # The source is a saved Checkpoint restored and not advanced; capturing it
            # again gives the same Checkpoint, so the child takes it and the source's
            # world, which nothing else uses.
            checkpoint = saved
            child = BranchRuntime.from_checkpoint(
                saved,
                branch_id=child_id,
                parent_branch_id=source.branch_id,
                _restored=(source.world, source.metrics),
            )
        else:
            child, checkpoint = source.fork_with_checkpoint(child_id)
        if expected_state_hash is not None:
            # A preview hashes the world under the current rule; a Checkpoint of an older
            # version holds its hash under its own rule.
            current = (
                checkpoint.state_hash
                if checkpoint.schema_version == Checkpoint.CURRENT_SCHEMA_VERSION
                else child.world.state_digest()
            )
            if current != expected_state_hash:
                raise TimelineError("source state changed; preview the intervention again")
        child.runtime_state["fork_origin"] = {
            "branch_id": branch_id,
            "minute": minute,
            "state_hash": checkpoint.state_hash,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
        if program is not None:
            if approved_program_hash != digest(program.model_dump(mode="json")):
                raise TimelineError("EffectProgram approval hash does not match preview")
            child.runtime_state.pop("replay_source", None)
            child.apply_effect_program(program)
        elif "llm" in source.runtime_state:
            # Only the head's response tape is needed, not its restored world.
            reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
            head = self.store.checkpoint_fields(
                self.project_id, self.run_id, reference.checkpoint_hash, ("runtime_state",)
            )
            child.runtime_state["replay_source"] = deepcopy(head["runtime_state"]["llm"])
        # The child's world was restored from this Checkpoint, so its first save
        # reuses the Checkpoint's EventLog.
        history_cache = CheckpointHistoryCache()
        history_cache.seed_events(child.world.event_log, checkpoint.world_state["event_log"])
        try:
            self.record(
                child,
                fork_minute=minute,
                history_cache=history_cache,
                inherit_index_from=branch_id,
            )
        except BaseException:
            # The child is new (checked above); a save that failed after writing its
            # pointer, while indexing, must not leave a branch the index lacks (#288).
            self.store.forget_unindexed_branch(self.project_id, self.run_id, child_id)
            raise
        return child

    def frame(self, branch_id: str, minute: int) -> dict[str, Any]:
        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        if type(minute) is int and reference.fork_minute <= minute <= reference.computed_until:
            # A saved boundary renders the same frame the writer cached when it was
            # recorded; only computed_until moves as the branch grows.
            from sheltersim.storage.frames import FrameCache

            saved_minute, checkpoint_hash = self._nearest_checkpoint(branch_id, minute, reference)
            cache = FrameCache(self.store, self.project_id, self.run_id)
            cached = cache.get(checkpoint_hash) if saved_minute == minute else None
            identity = (self.run_id, branch_id, minute)
            if (
                cached is not None
                and (cached.get("run_id"), cached.get("branch_id"), cached.get("minute"))
                == identity
            ):
                return {**cached, "computed_until": reference.computed_until}
        runtime = self.seek(branch_id, minute)
        from sheltersim.inspection.frames import world_frame

        reference = self.store.branch_reference(self.project_id, self.run_id, branch_id)
        return world_frame(
            runtime.world,
            run_id=self.run_id,
            branch_id=branch_id,
            computed_until=reference.computed_until,
        )
