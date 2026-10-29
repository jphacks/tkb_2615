"""Run one ProjectAPI computation in its own process (#48).

The server process keeps answering HTTP requests without sharing the GIL with the
simulation, and the memory a computation used returns to the OS when it ends.
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
    store: ProjectStore,
    project_id: str,
    run_id: str,
    branch_id: str,
    target_minute: int,
    measure_performance: bool,
) -> tuple[SpawnProcess, Event]:
    """Start a worker and return it once it owns the Run's writer lease.

    The worker claims the lease itself, so a server that stops abnormally never
    leaves a computation writing without one. A Run another process owns is
    refused with the same error as before.
    """
    cancel = _CONTEXT.Event()
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=run_worker,
        args=(
            str(store.root),
            store.max_bytes,
            project_id,
            run_id,
            branch_id,
            target_minute,
            measure_performance,
            cancel,
            sender,
        ),
        name=f"sheltersim-run-{run_id}",
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
    finally:
        receiver.close()
    if state != "running":
        process.join()
        raise StorageError(detail)
    return process, cancel


def run_worker(
    root: str,
    max_bytes: int,
    project_id: str,
    run_id: str,
    branch_id: str,
    target_minute: int,
    measure_performance: bool,
    cancel: Event,
    ready: Connection,
) -> None:
    from sheltersim.web.projects import ProjectAPI

    store = ProjectStore(root, max_bytes=max_bytes)
    try:
        lease = store.claim_run(project_id, run_id)
    except StorageError as exc:
        ready.send(("refused", str(exc)))
        return
    try:
        store.set_status(project_id, run_id, "running")
    except BaseException as exc:
        store.release_run(lease)
        ready.send(("refused", str(exc) if isinstance(exc, StorageError) else "run_worker_failed"))
        return
    ready.send(("running", None))
    ready.close()
    parent: Any = multiprocessing.parent_process()

    def stopped() -> bool:
        # A server that is gone can no longer cancel; stop at the next boundary.
        return cancel.is_set() or (parent is not None and not parent.is_alive())

    api = ProjectAPI(store, recover=False)
    try:
        api._advance(
            project_id,
            run_id,
            branch_id,
            target_minute,
            stopped,
            lease,
            measure_performance,
            own_process=True,
        )
    finally:
        api.close()
