"""Run one ProjectAPI computation or fork in its own process (#48).

The server process keeps answering HTTP requests without sharing the GIL with the
simulation, and the memory a job used returns to the OS when it ends.
"""

from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from multiprocessing.synchronize import Event
from typing import Any

from sheltersim.storage.projects import ProjectStore, StorageError

# Spawn starts a clean interpreter on every platform; forking a process that runs
# server threads could copy locks held by those threads.
_CONTEXT = multiprocessing.get_context("spawn")
# Starting the interpreter and importing the simulation takes well under a second;
# the bound only ends a start that hangs.
_START_TIMEOUT_SECONDS = 120.0


def start_worker(
    store: ProjectStore, job: dict[str, Any]
) -> tuple[SpawnProcess, Event, Connection]:
    """Start a worker and return it once it owns the Run's writer lease.

    job is {"kind": "advance", ...} or {"kind": "fork", ...} with the arguments of
    ProjectAPI._advance or ProjectAPI.fork. The worker claims the lease itself, so a
    server that stops abnormally never leaves a job writing without one. A Run
    another process owns is refused with the same error as before. The returned
    connection receives the job's outcome; it reaches end of file when the worker
    ends.
    """
    cancel = _CONTEXT.Event()
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=run_worker,
        args=(str(store.root), store.max_bytes, job, cancel, sender),
        name=f"sheltersim-{job['kind']}-{job['run_id']}",
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(_START_TIMEOUT_SECONDS):
            process.kill()
            process.join()
            raise StorageError("run_worker_start_timeout")
        try:
            state, detail = receiver.recv()
        except EOFError:
            process.join()
            raise StorageError("run_worker_start_failed") from None
    except BaseException:
        receiver.close()
        raise
    if state != "running":
        receiver.close()
        process.join()
        raise StorageError(detail)
    return process, cancel, receiver


def run_worker(
    root: str, max_bytes: int, job: dict[str, Any], cancel: Event, outcome: Connection
) -> None:
    from sheltersim.web.projects import ProjectAPI

    project_id, run_id = job["project_id"], job["run_id"]
    store = ProjectStore(root, max_bytes=max_bytes)
    try:
        lease = store.claim_run(project_id, run_id)
    except StorageError as exc:
        outcome.send(("refused", str(exc)))
        return
    try:
        previous = store.run(project_id, run_id)["status"]
        store.set_status(project_id, run_id, "running")
    except BaseException as exc:
        store.release_run(lease)
        outcome.send(
            ("refused", str(exc) if isinstance(exc, StorageError) else "run_worker_failed")
        )
        return
    outcome.send(("running", None))
    parent: Any = multiprocessing.parent_process()

    def stopped() -> bool:
        # A server that is gone can no longer cancel; stop at the next boundary.
        return cancel.is_set() or (parent is not None and not parent.is_alive())

    api = ProjectAPI(store, recover=False)
    try:
        if job["kind"] == "advance":
            api._advance(
                project_id,
                run_id,
                job["branch_id"],
                job["target_minute"],
                stopped,
                lease,
                job["measure_performance"],
                own_process=True,
            )
            return
        try:
            result = api.fork(
                project_id,
                run_id,
                job["branch_id"],
                job["document"],
                expected_state_hash=job["expected_state_hash"],
            )
        except StorageError as exc:
            outcome.send(("fork_failed", {"error": str(exc), "code": exc.code}))
        except ValueError:
            outcome.send(("fork_failed", {"error": "invalid project request", "code": None}))
        except Exception:
            outcome.send(("fork_failed", {"error": "project operation failed", "code": None}))
        else:
            outcome.send(("forked", result))
        finally:
            # A fork leaves the Run as it found it. The server restores the status
            # after it records the outcome, so a client that sees the Run leave
            # running also sees why; without a server, the worker restores it.
            try:
                if parent is None or not parent.is_alive():
                    store.set_status(project_id, run_id, previous)
            finally:
                store.release_run(lease)
    finally:
        outcome.close()
        api.close()
