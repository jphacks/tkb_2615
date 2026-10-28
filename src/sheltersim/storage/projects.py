"""Immutable scenario/run inputs with atomic branch pointers and restart recovery."""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX lock for macOS/Linux.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None
    import msvcrt

from sheltersim.contracts.api import BranchReference, EvidenceReference, RunReference
from sheltersim.contracts.policies import model_signatures
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CanonicalEncoder
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.checkpoints import CheckpointArchive

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PRIVATE = re.compile(
    r"api.?key|secret|password|credential|authorization|raw.?prompt|full.?name|email|phone|address",
    re.I,
)
# The top-level domain is letters, so a value with a decimal after "@", such as a
# model signature, is not taken for an address (#187).
EMAIL_ADDRESS = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\W\d_]{2,}\b")
_SECRET = re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{12,}|Bearer\s+\S+|" + EMAIL_ADDRESS.pattern, re.I)
# Three separated digit groups, so DOIs and dates in provenance are not phone
# numbers (#190). Run artifacts and the saved-Run inspector use the same definition.
PHONE_NUMBER = re.compile(r"(?<!\w)(?:\+\d{1,3}[ -])?\d{2,4}[- ]\d{2,4}[- ]\d{3,4}(?!\w)")
_TERMINAL = {"completed", "cancelled", "failed", "interrupted"}


class StorageError(ValueError):
    """Missing, incompatible, conflicting or unsafe saved artifacts."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        candidate = message.split(":", 1)[0]
        self.code = code or (
            candidate if re.fullmatch(r"[a-z][a-z0-9_]{1,80}", candidate) else "storage_conflict"
        )


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@functools.lru_cache(maxsize=4096)
def _private_field(key: str) -> bool:
    # Saved records reuse a small set of field names; match each name once (#47).
    return _PRIVATE.search(key) is not None


def _redact(value: str) -> str:
    return PHONE_NUMBER.sub("[redacted]", _SECRET.sub("[redacted]", value))


# Saved records repeat the same IDs, labels and short texts; redact each one once.
_redact_short = functools.lru_cache(maxsize=16_384)(_redact)


def safe_document(value: Any) -> Any:
    """Reject credential-bearing fields; redact conventional secrets/contact text."""
    if type(value) is str:
        return _redact_short(value) if len(value) <= 128 else _redact(value)
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str) or _private_field(key):
                raise StorageError("sensitive fields cannot be persisted")
        return {key: safe_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_document(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    if value is None or type(value) is bool:
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        # Built-in ints and finite floats already satisfy the JSON scalar contract.
        # Avoid constructing a JSON encoder for every coefficient/KPI/history value.
        return value
    if type(value) is float:
        canonical(value)  # Raises for NaN and infinities.
        return value
    raise StorageError("saved data must be finite JSON")


def _reject_sensitive_checkpoint_content(value: Any) -> None:
    if safe_document(value) != value:
        raise StorageError("checkpoint contains sensitive data")


class ProjectStore:
    """Local cloud-disk store; callers provide opaque IDs, never filesystem paths."""

    def __init__(self, root: str | Path, *, max_bytes: int = 10_000_000_000) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise StorageError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._mutex = threading.RLock()
        self._checkpoint_write_bytes: dict[tuple[str, str], int] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._mutex, (self.root / ".lock").open("a+b") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:  # Windows msvcrt locks a byte range and needs a byte in the file.
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                else:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _path(self, *parts: str) -> Path:
        if any(
            not isinstance(part, str)
            or not (_ID.fullmatch(part) or (part.endswith(".json") and _ID.fullmatch(part[:-5])))
            for part in parts
        ):
            raise StorageError("invalid artifact identifier")
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root) or any(
            p.is_symlink() for p in [path, *path.parents] if p != self.root.parent
        ):
            raise StorageError("artifact path cannot follow symlinks")
        return path

    def _read(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise StorageError("unsafe artifact path")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise StorageError("saved artifact is missing or corrupt") from exc
        # Conversation v2 adds scalar proof/scope records. Other artifact envelopes
        # remain v1; never accept an unrelated future checkpoint/status format.
        conversation = (
            path.parent.name == "conversations"
            and len(path.parents) > 2
            and path.parents[2].name == "branches"
        )
        versions = {1, 2} if conversation else {1}
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] not in versions
        ):
            raise StorageError("unsupported saved artifact version; migration required")
        return value

    def _write(self, path: Path, value: dict[str, Any], *, immutable: bool = False) -> None:
        encoded = (canonical(value) + "\n").encode()
        if immutable and path.exists():
            if path.read_bytes() != encoded:
                raise StorageError("immutable saved artifact already exists")
            return
        previous_size = path.stat().st_size if path.exists() else 0
        if self.usage()["bytes"] + len(encoded) - previous_size > self.max_bytes:
            raise StorageError("storage capacity exceeded; export then delete saved runs")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if immutable:
                try:
                    os.link(name, path)
                except FileExistsError as exc:
                    if path.read_bytes() != encoded:
                        raise StorageError("immutable saved artifact already exists") from exc
            else:
                os.replace(name, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    def usage(self) -> dict[str, int]:
        """Measure the regular files on disk now, including other processes' writes.

        Symlinks are neither counted nor followed. Directory entries carry their type,
        so each file costs one stat (#47); a file or directory removed while it is
        measured holds no bytes.
        """
        total = files = 0
        directories = [os.fspath(self.root)]
        while directories:
            try:
                entries = list(os.scandir(directories.pop()))
            except (FileNotFoundError, PermissionError):
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except FileNotFoundError:
                    continue
        return {"bytes": total, "files": files, "limit_bytes": self.max_bytes}

    def create_project(self, project_id: str, *, _internal: bool = False) -> dict[str, Any]:
        if project_id == "runtime-accounts" and not _internal:
            raise StorageError("project_id_reserved_for_usage_accounts")
        with self.transaction():
            document = {"schema_version": 1, "project_id": project_id, "archived": False}
            self._write(self._path(project_id, "project.json"), document, immutable=True)
            return document

    def projects(self) -> list[dict[str, Any]]:
        # Internal non-Project usage accounts share ProjectStore durability but are
        # not user-created study Projects.
        return [
            self._read(p)
            for p in sorted(self.root.glob("*/project.json"))
            if p.parent.name != "runtime-accounts"
        ]

    def project(self, project_id: str) -> dict[str, Any]:
        project = self._read(self._path(project_id, "project.json"))
        return {**project, "scenarios": self.scenarios(project_id), "runs": self.runs(project_id)}

    def save_scenario(
        self, project_id: str, scenario: ScenarioSpec, *, approved_hash: str
    ) -> dict[str, Any]:
        """Immutable revision; edited/approved content never overwrites an old run."""
        document = json.loads(scenario.canonical_json())
        if safe_document(document) != document:
            raise StorageError("scenario contains sensitive text; remove it before approval")
        scenario_hash = digest(document)
        if approved_hash != scenario_hash:
            raise StorageError("scenario approval hash does not match content")
        with self.transaction():
            self._read(self._path(project_id, "project.json"))
            prior = [r for r in self.scenarios(project_id) if r["scenario_id"] == scenario.id]
            revision = max((r["scenario_version"] for r in prior), default=0) + 1
            record = {
                "schema_version": 1,
                "scenario_id": scenario.id,
                "scenario_version": revision,
                "scenario_hash": scenario_hash,
                "approved": True,
                "scenario": document,
            }
            self._write(
                self._path(project_id, "scenarios", scenario.id, f"v{revision}.json"),
                record,
                immutable=True,
            )
            return record

    def scenarios(self, project_id: str) -> list[dict[str, Any]]:
        directory = self._path(project_id, "scenarios")
        return [self._read(p) for p in sorted(directory.glob("*/v*.json"))]

    def scenario(self, project_id: str, scenario_id: str, revision: int) -> ScenarioSpec:
        if type(revision) is not int or revision < 1:
            raise StorageError("invalid scenario revision")
        record = self._read(self._path(project_id, "scenarios", scenario_id, f"v{revision}.json"))
        if record["scenario_hash"] != digest(record["scenario"]):
            raise StorageError("saved scenario integrity mismatch")
        return ScenarioSpec.from_json(canonical(record["scenario"]))

    def create_run(
        self, project_id: str, run_id: str, scenario_id: str, revision: int
    ) -> dict[str, Any]:
        with self.transaction():
            scenario = self.scenario(project_id, scenario_id, revision)
            ref = RunReference(
                project_id=project_id,
                scenario_id=scenario_id,
                scenario_version=revision,
                scenario_hash=digest(json.loads(scenario.canonical_json())),
                run_id=run_id,
                seed=scenario.seed,
                model_versions=model_signatures(scenario.model_configs),
            )
            record = {**ref.model_dump(mode="json"), "status": "queued", "archived": False}
            self._write(self._path(project_id, "runs", run_id, "run.json"), record, immutable=True)
            self._write(
                self._path(project_id, "runs", run_id, "status.json"),
                {"schema_version": 1, "status": "queued", "archived": False},
            )
            return record

    def run(self, project_id: str, run_id: str) -> dict[str, Any]:
        record = self._read(self._path(project_id, "runs", run_id, "run.json"))
        status = self._read(self._path(project_id, "runs", run_id, "status.json"))
        return {**record, **status, "branches": self.branches(project_id, run_id)}

    def runs(self, project_id: str) -> list[dict[str, Any]]:
        return [
            self.run(project_id, p.parent.name)
            for p in sorted(self._path(project_id, "runs").glob("*/run.json"))
        ]

    def set_status(self, project_id: str, run_id: str, status: str) -> None:
        if status not in _TERMINAL | {"queued", "running", "paused"}:
            raise StorageError("unsupported run status")
        with self.transaction():
            self._read(self._path(project_id, "runs", run_id, "run.json"))
            path = self._path(project_id, "runs", run_id, "status.json")
            self._write(path, {**self._read(path), "status": status})

    def claim_run(self, project_id: str, run_id: str) -> int:
        """Cross-process writer lease. Closing the FD/crashing releases ownership.

        The lock file survives restarts, but the lease does not. Never infer stale
        ownership from a reused PID or interrupt a live worker at server startup.
        """
        with self.transaction():
            self.run(project_id, run_id)
            path = self._path(project_id, "runs", run_id, "worker-lock")
            # O_NOFOLLOW/O_BINARY exist only on some platforms; Windows has no symlink flag here.
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            descriptor = os.open(path, flags, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    # Windows: lock the first byte without waiting; the lock ends when the
                    # FD closes.
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        raise BlockingIOError(str(exc)) from exc
            except BlockingIOError as exc:
                os.close(descriptor)
                raise StorageError("run_worker_busy: another process owns this Run") from exc
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor

    @staticmethod
    def release_run(descriptor: int) -> None:
        os.close(descriptor)

    def recover_interrupted(self) -> list[tuple[str, str]]:
        recovered = []
        for project in self.projects():
            for run in self.runs(project["project_id"]):
                if run["status"] not in {"queued", "running"}:
                    continue
                try:
                    lease = self.claim_run(project["project_id"], run["run_id"])
                except StorageError as exc:
                    if exc.code == "run_worker_busy":
                        continue
                    raise
                try:
                    current = self.run(project["project_id"], run["run_id"])
                    if current["status"] in {"queued", "running"}:
                        self.set_status(project["project_id"], run["run_id"], "interrupted")
                        recovered.append((project["project_id"], run["run_id"]))
                finally:
                    self.release_run(lease)
        return recovered

    def save_branch(
        self,
        project_id: str,
        reference: BranchReference,
        checkpoint: Checkpoint,
        *,
        expected_checkpoint_hash: str | None = None,
    ) -> None:
        """Write content first, then atomically move a compare-and-swap branch pointer."""
        # to_dict validates both hashes, WorldState/model compatibility and
        # metrics, and returns an owned snapshot. Reuse it throughout this
        # locked save instead of restoring/copying the growing history again.
        self._save_branch(
            project_id,
            reference,
            checkpoint,
            checkpoint.to_dict,
            expected_checkpoint_hash=expected_checkpoint_hash,
        )

    def save_captured_branch(
        self,
        project_id: str,
        reference: BranchReference,
        checkpoint: Checkpoint,
        *,
        expected_checkpoint_hash: str | None = None,
    ) -> None:
        """Save a checkpoint the caller just created with Checkpoint.capture.

        capture() already validated the hashes, WorldState/model compatibility
        and metrics. Only pass an object that has not been shared or mutated
        since; anything else must use save_branch, which validates again.
        """
        self._save_branch(
            project_id,
            reference,
            checkpoint,
            checkpoint._document_view,
            expected_checkpoint_hash=expected_checkpoint_hash,
            encoder=checkpoint._encoder,
        )

    def _save_branch(
        self,
        project_id: str,
        reference: BranchReference,
        checkpoint: Checkpoint,
        document_source: Any,
        *,
        expected_checkpoint_hash: str | None,
        encoder: CanonicalEncoder | None = None,
    ) -> None:
        with self.transaction():
            run = self.run(project_id, reference.run_id)
            scenario = self.scenario(project_id, run["scenario_id"], run["scenario_version"])
            document = document_source()
            world = document["world_state"]
            if (
                world["scenario_id"] != scenario.id
                or world["seed"] != scenario.seed
                or world["model_configs"] != scenario.model_configs
            ):
                raise StorageError("checkpoint does not match immutable run inputs")
            if world["duration_minutes"] != scenario.duration_minutes:
                raise StorageError("checkpoint duration does not match run inputs")
            if reference.fork_minute > reference.computed_until:
                raise StorageError("fork minute cannot exceed computed history")
            if reference.computed_until != checkpoint.simulation_minute:
                raise StorageError("branch pointer minute does not match checkpoint")
            path = self._path(
                project_id, "runs", reference.run_id, "branches", reference.branch_id, "branch.json"
            )
            if path.exists():
                prior = self._read(path)
                if (
                    reference.parent_branch_id != prior["parent_branch_id"]
                    or reference.fork_minute != prior["fork_minute"]
                ):
                    raise StorageError("branch lineage is immutable")
                if (
                    reference.computed_until == prior["computed_until"]
                    and checkpoint.checkpoint_hash != prior["checkpoint_hash"]
                ):
                    raise StorageError("same-minute history is immutable; fork first")
                if expected_checkpoint_hash != prior["checkpoint_hash"]:
                    raise StorageError("branch changed; reload before saving")
                if reference.computed_until < prior["computed_until"]:
                    raise StorageError("cannot overwrite computed history with an earlier state")
            elif expected_checkpoint_hash is not None:
                raise StorageError("branch does not exist")
            elif reference.parent_branch_id is not None:
                # Only the parent's lineage pointer is needed, not its history.
                parent = self.branch_reference(
                    project_id, reference.run_id, reference.parent_branch_id
                )
                if not parent.fork_minute <= reference.fork_minute <= parent.computed_until:
                    raise StorageError("fork point is outside parent history")
            checkpoint_path = self._path(
                project_id,
                "runs",
                reference.run_id,
                "checkpoints",
                f"{checkpoint.checkpoint_hash}.json",
            )
            checkpoint_bytes_before = self.checkpoint_write_bytes(project_id, reference.run_id)
            # The archive applies the privacy check to every chunk that is not
            # already stored (stored chunks passed it when first written) and
            # stages new chunks, so a rejected checkpoint writes nothing.
            archive = CheckpointArchive(
                checkpoint_path.parent,
                max_new_bytes=max(0, self.max_bytes - self.usage()["bytes"] - 65_536),
                content_check=_reject_sensitive_checkpoint_content,
                encoder=encoder,
            )
            encoded_archive = archive._prepare_validated_document(
                document,
                checkpoint_hash=checkpoint.checkpoint_hash,
            )
            pending_ledger_path = self._path(
                project_id, "runs", reference.run_id, "checkpoint-write-pending.json"
            )
            self._write(
                pending_ledger_path,
                {
                    "schema_version": 1,
                    "run_id": reference.run_id,
                    "status": "checkpoint_archive_write_in_progress",
                },
            )
            archive._write_prepared()
            new_manifest = not checkpoint_path.exists()
            self._write(checkpoint_path, encoded_archive, immutable=True)
            manifest_bytes = checkpoint_path.stat().st_size if new_manifest else 0
            key = (project_id, reference.run_id)
            written_bytes = checkpoint_bytes_before + archive.new_bytes + manifest_bytes
            self._write(
                self._path(project_id, "runs", reference.run_id, "checkpoint-write-ledger.json"),
                {
                    "schema_version": 1,
                    "run_id": reference.run_id,
                    "unique_checkpoint_bytes_written": written_bytes,
                    "accounting": (
                        "unique content-addressed chunk bytes plus immutable checkpoint manifest "
                        "bytes"
                    ),
                },
            )
            self._checkpoint_write_bytes[key] = written_bytes
            pending_ledger_path.unlink(missing_ok=True)
            self._write(
                path,
                {
                    **reference.model_dump(mode="json"),
                    "checkpoint_hash": checkpoint.checkpoint_hash,
                },
            )

    def checkpoint_write_bytes(self, project_id: str, run_id: str) -> int:
        """Return durable unique checkpoint archive bytes, reconstructing legacy ledgers once."""
        with self._mutex:
            key = (project_id, run_id)
            pending_path = self._path(project_id, "runs", run_id, "checkpoint-write-pending.json")
            if pending_path.exists():
                value = self._checkpoint_archive_bytes(project_id, run_id)
                self._write(
                    self._path(project_id, "runs", run_id, "checkpoint-write-ledger.json"),
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "unique_checkpoint_bytes_written": value,
                        "accounting": (
                            "unique content-addressed chunk bytes plus immutable checkpoint "
                            "manifest bytes"
                        ),
                    },
                )
                pending_path.unlink(missing_ok=True)
                self._checkpoint_write_bytes[key] = value
                return value
            if key in self._checkpoint_write_bytes:
                return self._checkpoint_write_bytes[key]
            ledger_path = self._path(project_id, "runs", run_id, "checkpoint-write-ledger.json")
            if ledger_path.exists():
                record = self._read(ledger_path)
                value = record.get("unique_checkpoint_bytes_written")
                if (
                    record.get("run_id") != run_id
                    or type(value) is not int
                    or value < 0
                    or record.get("accounting")
                    != (
                        "unique content-addressed chunk bytes plus immutable checkpoint manifest "
                        "bytes"
                    )
                ):
                    raise StorageError("checkpoint_write_ledger_corrupt")
            else:
                value = self._checkpoint_archive_bytes(project_id, run_id)
            self._checkpoint_write_bytes[key] = value
            return value

    def _checkpoint_archive_bytes(self, project_id: str, run_id: str) -> int:
        """Count immutable archive payload/manifest files without following symlinks."""
        directory = self._path(project_id, "runs", run_id, "checkpoints")
        if not directory.exists():
            return 0
        if directory.is_symlink():
            raise StorageError("unsafe_checkpoint_archive_directory")
        chunks = directory / "chunks"
        if chunks.is_symlink():
            raise StorageError("unsafe_checkpoint_archive_directory")
        paths = [*directory.glob("*.json"), *chunks.glob("*.json.gz")]
        if any(
            path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())
            for path in paths
        ):
            raise StorageError("unsafe_checkpoint_archive_file")
        return sum(path.stat().st_size for path in paths if path.is_file())

    def branches(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        return [
            self._read(p)
            for p in sorted(
                self._path(project_id, "runs", run_id, "branches").glob("*/branch.json")
            )
        ]

    def branch_reference(self, project_id: str, run_id: str, branch_id: str) -> BranchReference:
        """Read the atomic pointer without decoding its potentially large checkpoint."""
        return BranchReference.model_validate(
            self._read(self._path(project_id, "runs", run_id, "branches", branch_id, "branch.json"))
        )

    def load_branch(
        self, project_id: str, run_id: str, branch_id: str
    ) -> tuple[BranchReference, Checkpoint]:
        reference, checkpoint, _ = self.load_restored_branch(project_id, run_id, branch_id)
        # load_checkpoint already validated hashes, model compatibility and the
        # WorldState/metrics restore; callers restore again only if they need objects.
        return reference, checkpoint

    def load_restored_branch(
        self, project_id: str, run_id: str, branch_id: str
    ) -> tuple[BranchReference, Checkpoint, tuple[WorldState, MetricsRecorder]]:
        """load_branch(), and the world and metrics restored from its head."""
        reference = BranchReference.model_validate(
            self._read(self._path(project_id, "runs", run_id, "branches", branch_id, "branch.json"))
        )
        checkpoint, restored = self.load_restored_checkpoint(
            project_id, run_id, reference.checkpoint_hash
        )
        if (
            checkpoint.checkpoint_hash != reference.checkpoint_hash
            or checkpoint.simulation_minute != reference.computed_until
        ):
            raise StorageError("branch checkpoint pointer is corrupt")
        return reference, checkpoint, restored

    def load_checkpoint(self, project_id: str, run_id: str, checkpoint_hash: str) -> Checkpoint:
        return self.load_restored_checkpoint(project_id, run_id, checkpoint_hash)[0]

    def load_restored_checkpoint(
        self, project_id: str, run_id: str, checkpoint_hash: str
    ) -> tuple[Checkpoint, tuple[WorldState, MetricsRecorder]]:
        """load_checkpoint(), and the world and metrics its validation restored.

        They are what restore_state() returns, without restoring the Checkpoint again.
        """
        path = self._path(project_id, "runs", run_id, "checkpoints", f"{checkpoint_hash}.json")
        document = self._read(path)
        try:
            if document.get("storage_type") in {"checkpoint_archive_v1", "checkpoint_archive_v2"}:
                return CheckpointArchive(path.parent).decode_restored(document)
            # _read parsed the file just now, so the document is not shared.
            return Checkpoint._load(document)
        except (CheckpointError, ValueError, TypeError, KeyError) as exc:
            raise StorageError("checkpoint_incompatible_or_corrupt") from exc

    def checkpoint_document(
        self, project_id: str, run_id: str, checkpoint_hash: str
    ) -> dict[str, Any]:
        path = self._path(project_id, "runs", run_id, "checkpoints", f"{checkpoint_hash}.json")
        record = self._read(path)
        if record.get("storage_type") in {"checkpoint_archive_v1", "checkpoint_archive_v2"}:
            try:
                document = CheckpointArchive(path.parent).document(record)
            except (CheckpointError, ValueError, TypeError, KeyError) as exc:
                raise StorageError("checkpoint_recorded_document_integrity_mismatch") from exc
        else:
            document = record
        fields = {
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
        if (
            not isinstance(document, dict)
            or set(document) != fields
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["checkpoint_hash"] != checkpoint_hash
            or digest({k: v for k, v in document.items() if k != "checkpoint_hash"})
            != checkpoint_hash
            or digest(document["world_state"]) != document["state_hash"]
            or safe_document(document) != document
        ):
            raise StorageError("checkpoint_recorded_document_integrity_mismatch")
        if (
            type(document["simulation_minute"]) is not int
            or document["simulation_minute"] < 0
            or not isinstance(document["metrics_state"], dict)
            or not isinstance(document["world_state"], dict)
            or not isinstance(document["metrics_state"].get("snapshots"), list)
            or not isinstance(document["world_state"].get("event_log"), list)
        ):
            raise StorageError("checkpoint_recorded_document_structure_invalid")
        return document

    def checkpoint_fields(
        self, project_id: str, run_id: str, checkpoint_hash: str, fields: tuple[str, ...]
    ) -> dict[str, Any]:
        """Verified archive fields, without constructing a WorldState or provider."""
        path = self._path(project_id, "runs", run_id, "checkpoints", f"{checkpoint_hash}.json")
        record = self._read(path)
        if record.get("checkpoint_hash") != checkpoint_hash:
            raise StorageError("checkpoint_pointer_corrupt")
        if record.get("storage_type") in {"checkpoint_archive_v1", "checkpoint_archive_v2"}:
            archive = CheckpointArchive(path.parent)
            return {field: archive.project(record, (field,)) for field in fields}
        checkpoint = Checkpoint.from_dict(record)
        return {field: checkpoint.to_dict()[field] for field in fields}

    def save_turn(
        self, project_id: str, run_id: str, branch_id: str, turn_id: str, turn: dict[str, Any]
    ) -> None:
        reference = self.branch_reference(project_id, run_id, branch_id)
        allowed = {
            "question",
            "answer",
            "evidence",
            "limitations",
            "usage",
            "model_version",
            "minute",
            "conversation_id",
            "claims",
            "context",
            "sources",
        }
        if set(turn) - allowed:
            raise StorageError("unsupported conversation fields")
        evidence = [EvidenceReference.model_validate(item) for item in turn.get("evidence", [])]
        for item in evidence:
            cited_reference = (
                reference
                if item.branch_id == branch_id
                else self.branch_reference(project_id, run_id, item.branch_id)
            )
            if (
                item.run_id != run_id
                or not cited_reference.fork_minute <= item.minute <= cited_reference.computed_until
            ):
                raise StorageError(
                    "conversation evidence context does not match saved branch history"
                )
        claims = turn.get("claims", [])
        sources = turn.get("sources", [])
        if (
            not isinstance(claims, list)
            or len(claims) > 32
            or not isinstance(sources, list)
            or len(sources) > 20
        ):
            raise StorageError("invalid_conversation_proofs")
        aliases = {}
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("alias"), str):
                raise StorageError("invalid_conversation_source")
            item = EvidenceReference.model_validate(source.get("reference"))
            if item not in evidence or source["alias"] in aliases:
                raise StorageError("conversation_proof_reference_mismatch")
            aliases[source["alias"]] = item
        for claim in claims:
            if (
                not isinstance(claim, dict)
                or set(claim) != {"text", "evidence"}
                or not isinstance(claim["text"], str)
            ):
                raise StorageError("invalid_conversation_claim")
            proofs = claim["evidence"]
            if not isinstance(proofs, list) or not 1 <= len(proofs) <= 4:
                raise StorageError("invalid_conversation_claim")
            for proof in proofs:
                if (
                    not isinstance(proof, dict)
                    or set(proof) != {"alias", "field", "value"}
                    or proof["alias"] not in aliases
                    or not isinstance(proof["field"], str)
                    or not 1 <= len(proof["field"]) <= 120
                    or type(proof["value"]) not in (str, int, float, bool, type(None))
                ):
                    raise StorageError("invalid_conversation_proof")
        context = turn.get("context")
        if context is not None and (
            not isinstance(context, dict)
            or context.get("run_id") != run_id
            or context.get("branch_id") != branch_id
            or context.get("captured_minute") != turn.get("minute")
        ):
            raise StorageError("conversation_scope_mismatch")
        document = {
            "schema_version": 2 if "claims" in turn else 1,
            "turn_id": turn_id,
            **safe_document(turn),
        }
        with self.transaction():
            self._write(
                self._path(
                    project_id,
                    "runs",
                    run_id,
                    "branches",
                    branch_id,
                    "conversations",
                    f"{turn_id}.json",
                ),
                document,
                immutable=True,
            )
            from sheltersim.storage.history_index import HistoryIndex

            HistoryIndex(self, project_id, run_id).add_turn(branch_id, document)

    def conversations(self, project_id: str, run_id: str, branch_id: str) -> list[dict[str, Any]]:
        return [
            self._read(p)
            for p in sorted(
                self._path(project_id, "runs", run_id, "branches", branch_id, "conversations").glob(
                    "*.json"
                )
            )
        ]

    def archive(self, project_id: str, run_id: str | None = None) -> None:
        with self.transaction():
            path = (
                self._path(project_id, "project.json")
                if run_id is None
                else self._path(project_id, "runs", run_id, "status.json")
            )
            self._write(path, {**self._read(path), "archived": True})

    def delete_run(self, project_id: str, run_id: str, *, confirmation: str) -> None:
        """An explicit run-ID confirmation is required; no cascading project deletion."""
        import shutil

        lease = self.claim_run(project_id, run_id)
        try:
            with self.transaction():
                run = self.run(project_id, run_id)
                if confirmation != run_id or run["status"] not in _TERMINAL | {"paused", "queued"}:
                    raise StorageError("deletion needs run ID confirmation and an inactive run")
                shutil.rmtree(self._path(project_id, "runs", run_id))
                self._checkpoint_write_bytes.pop((project_id, run_id), None)
        finally:
            self.release_run(lease)
