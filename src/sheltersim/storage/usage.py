"""Durable shared call reservations across preparation, branches and analysis."""

from __future__ import annotations

import math
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from sheltersim.contracts.workspace import UsageLimits
from sheltersim.storage.projects import ProjectStore, StorageError, digest


def money(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        raise StorageError("usage amounts must be finite and nonnegative")
    return int((Decimal(str(value)) * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))


def process_identity(pid: int) -> str | None:
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
        return digest({"boot": boot, "start": start, "pid": pid})
    except (OSError, IndexError):
        return None


class UsageLedger:
    """Pending/uncertain calls retain their full reservation after a crash.

    There is no automatic refund or paid retry. A cache hit never reaches reserve.
    Metadata contains hashes and usage, never prompts, credentials or responses.
    """

    def __init__(self, store: ProjectStore, project_id: str, account_id: str) -> None:
        self.store, self.project_id, self.account_id = store, project_id, account_id
        self.path = store._path(project_id, "usage", f"{account_id}.json")
        # Unfreeze audit lives beside, not inside, the v1 ledger so older readers of a
        # machine-wide ledger keep working and account listings never see it.
        self.audit_path = store._path(project_id, "usage-audit", f"{account_id}.json")

    def create(self, limits: UsageLimits) -> None:
        with self.store.transaction():
            self.store._read(self.store._path(self.project_id, "project.json"))
            if not self.path.exists():
                self.store._write(self.path, {
                    "schema_version": 1, "account_id": self.account_id,
                    "limits": limits.model_dump(mode="json"), "receipts": {},
                    "coverage": "reserved_before_provider", "frozen": False,
                }, immutable=True)

    def summary(self) -> dict[str, Any]:
        document = self.store._read(self.path)
        receipts = document["receipts"].values()
        totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_nusd": 0}
        stages: dict[str, dict[str, int]] = {}
        model_totals: dict[str,dict[str,int]]={}
        stage_totals: dict[str,dict[str,int]]={}
        outcomes: dict[str,int]={}
        errors: dict[str,int]={}
        branches: dict[str | None, dict[str, int]] = {}
        uncertain = 0
        for receipt in receipts:
            bucket = stages.setdefault(receipt["operation"], {key: 0 for key in totals})
            model_bucket=model_totals.setdefault(receipt["model_id"],{key:0 for key in totals})
            stage_bucket=stage_totals.setdefault(receipt["stage"],{key:0 for key in totals})
            outcomes[receipt["status"]]=outcomes.get(receipt["status"],0)+1
            if receipt.get("error_code"):
                errors[receipt["error_code"]] = errors.get(receipt["error_code"], 0) + 1
            branch = branches.setdefault(receipt["branch_id"], {key: 0 for key in totals})
            for key in totals:
                totals[key] += receipt[key]
                bucket[key] += receipt[key]
                model_bucket[key]+=receipt[key]
                stage_bucket[key]+=receipt[key]
                branch[key] += receipt[key]
            uncertain += receipt["status"] in {"pending", "uncertain"}
        return {
            "schema_version": 1, "account_id": self.account_id,
            "limits": document["limits"], "totals": totals, "by_operation": stages,
            "by_stage":stage_totals,"by_model":model_totals,"by_outcome":outcomes,"by_error_code":errors,
            "by_branch": [{"branch_id": key, **value} for key, value in sorted(
                branches.items(), key=lambda item: item[0] or "")],
            "estimated_cost_usd": totals["cost_nusd"] / 1_000_000_000,
            "uncertain_calls": uncertain, "coverage": document["coverage"],
            "frozen": document["frozen"],
            "unfreeze_events": (
                self.store._read(self.audit_path)["events"] if self.audit_path.exists() else []
            ),
            "remaining_allowance": {
                "calls":max(0,document["limits"]["max_calls"]-totals["calls"]),
                "input_tokens":max(0,document["limits"]["max_input_tokens"]-totals["input_tokens"]),
                "output_tokens":max(0,document["limits"]["max_output_tokens"]-totals["output_tokens"]),
                "estimated_usd":max(0,document["limits"]["budget_usd"]-totals["cost_nusd"]/1_000_000_000)},
            "explanation": {
                "cost":"Provider-price estimate, not an invoice. Includes conservative pending/uncertain reservations.",
                "failure":"Timeout/crash may have reached the provider. Its reservation is retained, not refunded automatically.",
                "retry":"SDK retries are disabled. Repeating a reserved request does not authorize a second paid attempt.",
                "history":"Offline/cache/exact replay spends no new allowance. Legacy unrecorded calls are unknown, not zero.",
                "scope":"One preparation-to-Run account, shared by every sibling Branch and analysis stage; Project totals aggregate accounts.",
                "continuation":"Changing environment limits does not enlarge an existing allowance; frozen accounts cannot make paid calls."},
        }

    def reserve(self, *, request_key: str, operation: str, branch_id: str | None,
                stage: str, model_id: str, input_tokens: int, output_tokens: int,
                cost_usd: float, identity: dict[str, Any] | None = None) -> str:
        with self.store.transaction():
            document = self.store._read(self.path)
            limits = UsageLimits.model_validate(document["limits"])
            for receipt in document["receipts"].values():
                if receipt["request_key"] == request_key:
                    raise StorageError("usage_receipt_exists: use the accepted tape or saved result")
            receipts = list(document["receipts"].values())
            for receipt in receipts:
                if receipt["status"] == "pending":
                    try:
                        os.kill(receipt["pid"], 0)
                    except ProcessLookupError:
                        receipt["status"] = "uncertain"
                    except PermissionError:
                        continue
                    current = process_identity(receipt["pid"])
                    if current is not None and receipt.get("process_identity") not in {None, current}:
                        receipt["status"] = "uncertain"
            pending = sum(r["status"] == "pending" for r in receipts)
            amount = money(cost_usd)
            if document["frozen"] or pending >= limits.max_concurrency:
                raise StorageError("usage_busy_or_frozen")
            requested = {
                "calls": (1, limits.max_calls),
                "input_tokens": (input_tokens, limits.max_input_tokens),
                "output_tokens": (output_tokens, limits.max_output_tokens),
                "cost_nusd": (amount, money(limits.budget_usd)),
            }
            if any(sum(r[key] for r in receipts) + value > cap
                   for key, (value, cap) in requested.items()):
                raise StorageError("usage_limit_exceeded")
            receipt_id = uuid.uuid4().hex
            document["receipts"][receipt_id] = {
                "request_key": request_key, "operation": operation,
                "sequence":len(document["receipts"]),"created_at":datetime.now(UTC).isoformat(),
                "branch_id": branch_id, "stage": stage, "model_id": model_id,
                "status": "pending", "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
                "identity": identity or {},
                **{key: value for key, (value, _) in requested.items()},
            }
            self.store._write(self.path, document)
            return receipt_id

    def settle(self, receipt_id: str, *, input_tokens: int, output_tokens: int,
               cost_usd: float, outcome: str, error_code: str | None = None) -> None:
        with self.store.transaction():
            document = self.store._read(self.path)
            receipt = document["receipts"][receipt_id]
            if error_code is not None and error_code not in {
                "http_429", "http_4xx", "http_5xx", "provider_timeout_or_transport",
                "interrupted", "provider_refusal", "thinking_without_text",
                "non_text_response", "empty_text_response",
            }:
                raise StorageError("invalid_usage_error_code")
            if receipt["status"] != "pending":
                raise StorageError("usage receipt already settled")
            if outcome == "uncertain":
                receipt["status"] = "uncertain"
            else:
                actual = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                          "cost_nusd": money(cost_usd)}
                if any(value > receipt[key] for key, value in actual.items()):
                    document["frozen"] = True
                receipt.update(actual)
                receipt["status"] = "settled"
            receipt["settled_at"]=datetime.now(UTC).isoformat()
            if error_code is not None:
                receipt["error_code"] = error_code
            self.store._write(self.path, document)

    def unfreeze(self, *, confirm_account_id: str, reason: str) -> dict[str, Any]:
        """Re-open a frozen account after an operator reviewed why usage exceeded a reservation.

        Recorded spend is kept; only the freeze flag changes, and the event is logged.
        """
        if confirm_account_id != self.account_id:
            raise StorageError("usage_unfreeze_confirmation_mismatch")
        reason = reason.strip()
        if not reason or len(reason) > 200:
            raise StorageError("usage_unfreeze_reason_required")
        with self.store.transaction():
            document = self.store._read(self.path)
            if not document["frozen"]:
                raise StorageError("usage_account_not_frozen")
            if any(receipt["status"] == "pending" for receipt in document["receipts"].values()):
                raise StorageError("usage_unfreeze_pending_reservations")
            event = {
                "unfrozen_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "receipt_count": len(document["receipts"]),
            }
            audit = (
                self.store._read(self.audit_path) if self.audit_path.exists()
                else {"schema_version": 1, "account_id": self.account_id, "events": []}
            )
            audit["events"] = [*audit["events"], event]
            self.store._write(self.audit_path, audit)
            document["frozen"] = False
            self.store._write(self.path, document)
        return event

    def cancel_before_provider(self, receipt_id: str) -> None:
        """Release a reservation only when a later local preflight blocked the call."""
        with self.store.transaction():
            document = self.store._read(self.path)
            receipt = document["receipts"].get(receipt_id)
            if receipt is None or receipt["status"] != "pending":
                raise StorageError("usage reservation cannot be released")
            # This operation is called only before the Provider boundary. Once a
            # request may have left the process, pending/uncertain spend is retained.
            del document["receipts"][receipt_id]
            self.store._write(self.path, document)

    @classmethod
    def for_run(cls, store: ProjectStore, project_id: str, run_id: str,
                limits: UsageLimits, account_id: str | None = None) -> UsageLedger:
        store.run(project_id, run_id)
        binding = store._path(project_id, "runs", run_id, "usage.json")
        if binding.exists():
            recorded = store._read(binding)["account_id"]
            if account_id is not None and recorded != account_id:
                raise StorageError("Run usage account is immutable")
            return cls(store, project_id, recorded)
        ledger = cls(store, project_id, account_id or "run-" + digest(run_id)[:32])
        if account_id is None:
            ledger.create(limits)
            # One-time conservative import for runs created before this service existed.
            known: dict[str, dict[str, Any]] = {}
            for branch in store.branches(project_id, run_id):
                _, checkpoint = store.load_branch(project_id, run_id, branch["branch_id"])
                for record in checkpoint.runtime_state.get("llm", {}).get("records", []):
                    if record["api_request_count"]:
                        known[digest(record)] = record
            analysis = store._path(project_id, "runs", run_id, "analysis.json")
            if analysis.exists():
                for record in store._read(analysis)["router"]["records"]:
                    if record["api_request_count"]:
                        known[digest(record)] = record
            with store.transaction():
                document = store._read(ledger.path)
                for key, record in known.items():
                    document["receipts"].setdefault("legacy-" + key, {
                        "request_key": key, "operation": "legacy_import",
                        "branch_id": None, "stage": record["stage"],
                        "model_id": record["model_id"], "status": "settled", "pid": 0,
                        "calls": record["api_request_count"],
                        "input_tokens": record["input_tokens"],
                        "output_tokens": record["output_tokens"],
                        "cost_nusd": money(record["estimated_cost_usd"]),
                    })
                if known:
                    document["coverage"] = "legacy_checkpoints_then_reservations"
                store._write(ledger.path, document)
        else:
            store._read(ledger.path)
        with store.transaction():
            store._write(binding, {"schema_version": 1, "account_id": ledger.account_id}, immutable=True)
        return ledger

    def receipt_records(self, filters: dict[str,str]) -> tuple[list[dict[str,Any]],str]:
        """Sanitized bounded-account audit data. No provider/raw prompt/host PID."""
        allowed={"branch_id","operation","stage","model_id","status"}
        if set(filters) - allowed:
            raise StorageError("invalid_usage_receipt_filter")
        document=self.store._read(self.path)
        fields={"request_key","operation","branch_id","stage","model_id","status","calls","input_tokens","output_tokens",
                "cost_nusd","identity","sequence","created_at","settled_at","error_code"}
        rows=[]
        for key,receipt in document["receipts"].items():
            if any(receipt.get(name) != value for name, value in filters.items()):
                continue
            rows.append({"receipt_id":key,**{name:receipt[name] for name in fields if name in receipt},
                         "cost_semantics":"conservative_reservation" if receipt["status"] in {"pending","uncertain"} else "reported_token_price_estimate"})
        rows.sort(key=lambda row:(row.get("sequence",-1),row["receipt_id"]))
        return rows,digest(document)


TASK_CAMPAIGN_ACCOUNT_ID = "task-global-v1"
TASK_CAMPAIGN_BUDGET_USD = 50.0


def task_campaign_store() -> ProjectStore:
    """Resolve one account-data directory independently of Project/Run locations."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ShelterSim" / "state"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/ShelterSim/state"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "sheltersim"
    return ProjectStore(root)


def task_campaign_ledger(_store: ProjectStore | None = None) -> UsageLedger:
    """Return the immutable, process-wide cap shared by every Project in this store.

    Its platform user-data path is independent of Project roots, so a different project
    directory, CLI invocation, Web worker or process still sees the same allowance.
    The cap is code-owned, not an environment setting, so Run/account changes cannot
    increase the task-wide ceiling. `_store` remains accepted for caller compatibility.
    """
    del _store
    store = task_campaign_store()
    store.create_project("runtime-accounts", _internal=True)
    ledger = UsageLedger(store, "runtime-accounts", TASK_CAMPAIGN_ACCOUNT_ID)
    ledger.create(UsageLimits(
        max_calls=100_000,
        max_input_tokens=10_000_000_000,
        max_output_tokens=10_000_000_000,
        budget_usd=TASK_CAMPAIGN_BUDGET_USD,
        max_concurrency=16,
    ))
    summary = ledger.summary()
    if summary["limits"]["budget_usd"] != TASK_CAMPAIGN_BUDGET_USD:
        raise StorageError("campaign_usage_cap_mismatch")
    return ledger


def task_campaign_summary() -> dict[str, Any]:
    """Report cumulative task spend without creating an empty paid-use account."""
    store = task_campaign_store()
    project_path = store._path("runtime-accounts", "project.json")
    if not project_path.exists():
        return {"status": "not_created", "budget_usd": TASK_CAMPAIGN_BUDGET_USD,
                "calls": 0, "reserved_or_spent_usd": 0.0,
                "explanation": "No paid provider reservation has been recorded."}
    ledger = UsageLedger(store, "runtime-accounts", TASK_CAMPAIGN_ACCOUNT_ID)
    summary = ledger.summary()
    totals = summary["totals"]
    receipts, anchor = ledger.receipt_records({})
    return {"status": "active", "budget_usd": TASK_CAMPAIGN_BUDGET_USD,
            "calls": totals["calls"],
            "reserved_or_spent_usd": summary["estimated_cost_usd"],
            "uncertain_calls": summary["uncertain_calls"],
            "remaining_usd": summary["remaining_allowance"]["estimated_usd"],
            "summary": summary,
            "recent_receipts": receipts[-20:],
            "receipt_count": len(receipts),
            "receipts_truncated": len(receipts) > 20,
            "receipts_anchor": anchor,
            "storage_path_scope": "configured_user_state_directory"}
