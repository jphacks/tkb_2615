"""Bounded observational frame batches for smooth browser playback."""

from copy import deepcopy
from typing import Any

from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.inspection.frames import world_frame
from sheltersim.storage.projects import ProjectStore, StorageError


def comparison_projection(run_id, minute, common_range, frames, receipts, paired):
    """Identical comparison contract for individual and batched read projections."""
    base = frames[0]["kpis"]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "minute": minute,
        "common_range": common_range,
        "frames": frames,
        "origin_receipts": receipts,
        "shared_origin": paired,
        "differences": [
            {
                "branch_id": frame["branch_id"],
                "metrics": {
                    key: frame["kpis"][key] - value
                    for key, value in base.items()
                    if type(value) in (int, float) and type(frame["kpis"].get(key)) in (int, float)
                },
            }
            for frame in frames[1:]
        ],
        "interpretation": "Same Run/seed/time descriptive model comparison. "
        "Shared lineage does not establish real-world causation.",
    }


def _branch_frames(
    timeline: TimelineStore, run_id: str, ref: Any, start: int, end: int
) -> list[dict[str, Any]]:
    # Each replayed minute's state hash is world.state_digest(). The history cache of
    # Checkpoint saves (#44) gives the same bytes and converts only the history added
    # since the previous minute instead of encoding the whole world each minute.
    history = CheckpointHistoryCache()
    frames = []
    for world in timeline.iter_worlds(ref.branch_id, start, end):
        world.validate()
        document, _, encoder = history.documents(world, None)
        frames.append(
            deepcopy(
                world_frame(
                    world,
                    run_id=run_id,
                    branch_id=ref.branch_id,
                    computed_until=ref.computed_until,
                    state_hash=encoder.digest(document),
                )
            )
        )
    return frames


def replay_frames(
    store: ProjectStore,
    workspace: Any,
    project_id: str,
    run_id: str,
    branch_ids: list[str],
    start: int,
    end: int,
) -> dict[str, Any]:
    """Read at most eight computed boundaries for one or two branches.

    iter_worlds reuses mutable local replay worlds; copy each projection before the
    next tick. This never advances a persisted Branch or calls a live Provider.
    """
    if not 1 <= len(branch_ids) <= 2 or len(set(branch_ids)) != len(branch_ids):
        raise StorageError("replay requires one or two distinct branches")
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < start + 8:
        raise StorageError("replay interval must contain 1–8 integer boundaries")
    refs = [store.branch_reference(project_id, run_id, key) for key in branch_ids]
    if start < max(ref.fork_minute for ref in refs) or end > min(
        ref.computed_until for ref in refs
    ):
        raise StorageError("replay interval is outside the common computed range")
    # The comparison of the first minute without its frames, which are rendered below.
    template = (
        workspace.comparison_context(project_id, run_id, branch_ids, start)
        if len(refs) == 2
        else None
    )
    timeline = TimelineStore(store, project_id, run_id)
    batches = [_branch_frames(timeline, run_id, ref, start, end) for ref in refs]
    if any(
        store.branch_reference(project_id, run_id, ref.branch_id).checkpoint_hash
        != ref.checkpoint_hash
        for ref in refs
    ):
        raise StorageError("replay heads changed while reading; retry the request")
    samples = []
    for index, minute in enumerate(range(start, end + 1)):
        frames = [batch[index] for batch in batches]
        if template is None:
            samples.append(frames[0])
            continue
        common_range, receipts, paired = template
        samples.append(
            comparison_projection(run_id, minute, common_range, frames, receipts, paired)
        )
    return {
        "schema_version": 1,
        "start": start,
        "end": end,
        "branch_ids": branch_ids,
        "samples": samples,
    }
