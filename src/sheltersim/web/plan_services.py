"""Project/asset adapter for the region owner's validated plan functions."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from typing import Any

from pydantic import ValidationError

from sheltersim.contracts.workspace import AssetBegin, AssetChunk, DraftInput
from sheltersim.region.plan_import import (
    ImportedPlanGeometry,
    ManualPlanTrace,
    PlanAsset,
    PlanExtractionProposal,
    PlanFeatureProposal,
    PlanFeatureReview,
    create_plan_extraction_proposal,
    import_metric_geojson,
    inspect_plan_upload,
    manual_trace_from_reviewed_features,
    review_plan_extraction,
    revise_imported_plan_geometry,
    trace_uploaded_plan,
)
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.projects import ProjectStore, StorageError, digest, safe_document


class PlanServices:
    def __init__(self, store: ProjectStore) -> None:
        self.store, self.artifacts = store, ArtifactStore(store)

    def preview(self, project: str, asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        operation = body.get("operation")
        allowed = {
            "extract": {"page_index", "max_dimension_px"},
            "reviewed_trace": {
                "reviewed_preview_id",
                "building_width_m",
                "building_height_m",
                "operator_id",
                "plan_pixel_left",
                "plan_pixel_top",
                "plan_pixel_width",
                "plan_pixel_height",
            },
            "trace": {"trace", "reviewed_preview_id", "page_index", "raster_preview_id"},
            "geojson": set(),
            "proposal": {"features", "extraction_method", "extraction_source_id", "raster_size_px"},
            "review": {"proposal_preview_id", "reviews"},
            "geometry_revision": {
                "base_geometry_id",
                "base_geometry_hash",
                "geometry",
                "editable_geometry",
            },
        }
        if operation not in allowed:
            raise StorageError("unsupported_plan_preview_operation")
        required = {
            "extract": set(),
            "geojson": set(),
            "proposal": {"features"},
            "review": {"proposal_preview_id", "reviews"},
            "trace": {"trace"},
            "reviewed_trace": {"reviewed_preview_id", "building_width_m", "building_height_m"},
            "geometry_revision": {"base_geometry_id", "base_geometry_hash", "geometry"},
        }
        fields(body, {"operation", *required[operation]}, allowed[operation] - required[operation])
        manifest = self.artifacts.asset(project, asset_id, include_content=True)
        content = base64.b64decode(manifest["data_base64"], validate=True)
        extensions = {
            "application/pdf": "pdf",
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "application/json": "json",
            "application/geo+json": "geojson",
        }
        asset = inspect_plan_upload(
            file_name=f"asset-{asset_id[:16]}.{extensions[manifest['media_type']]}",
            media_type=manifest["media_type"],
            content=content,
            rights_status="operator_permission_asserted",
            use_restriction_note=manifest["license_note"],
        )
        try:
            result, raster_preview = self._operation_result(
                project, asset_id, body, manifest, asset, content
            )
        except StorageError:
            raise
        except ValueError as exc:
            # Plans are drawn by people; give them the reason, not a bare 400 (#204).
            raise StorageError(f"plan_{operation}_invalid: {plan_rejection_reason(exc)}") from exc
        if isinstance(result, ImportedPlanGeometry) and raster_preview is not None:
            result = self._bind_raster_provenance(project, result, raster_preview)
        kind = "geometry" if isinstance(result, ImportedPlanGeometry) else "proposal"
        value = result.model_dump(mode="json")
        if safe_document(value) != value:
            raise StorageError("plan_contains_sensitive_fields")
        preview_id = "plan-" + uuid.uuid4().hex
        record = {
            "record_type": "plan_preview",
            "asset_id": asset_id,
            "kind": kind,
            "value": value,
            "value_hash": digest(value),
            "validation": "region_structural_rules_only",
            "runtime_verified": False,
        }
        if raster_preview is not None:
            record["raster_preview"] = raster_preview
        self.artifacts.save_draft(project, preview_id, DraftInput(content=record))
        return {
            "schema_version": 1,
            "preview_id": preview_id,
            **record,
            "limits": (
                "Raster extraction, when requested, is an untrusted bounded heuristic. Geometry "
                "requires human review/calibration. No provider call is made by this adapter."
            ),
        }

    def _operation_result(
        self,
        project: str,
        asset_id: str,
        body: dict[str, Any],
        manifest: dict[str, Any],
        asset: PlanAsset,
        content: bytes,
    ) -> tuple[ImportedPlanGeometry | PlanExtractionProposal, dict[str, Any] | None]:
        """Run one plan preview operation and return its result and raster preview."""
        operation = body["operation"]
        raster_preview = None
        if operation == "extract":
            from sheltersim.region.plan_extract import (
                PlanExtractionUnavailable,
                extract_plan_features_from_asset,
            )

            page = body.get("page_index", 0)
            dimension = body.get("max_dimension_px", 1600)
            if (
                type(page) is not int
                or not 0 <= page < 200
                or type(dimension) is not int
                or not 64 <= dimension <= 2400
            ):
                raise StorageError("invalid_plan_extraction_page_or_size")

            def store_raster(png: bytes, width: int, height: int) -> None:
                nonlocal raster_preview
                derivative_hash = hashlib.sha256(png).hexdigest()
                upload_id = "raster-" + uuid.uuid4().hex
                request = AssetBegin(
                    media_type="image/png",
                    byte_count=len(png),
                    sha256=derivative_hash,
                    permission_confirmed=manifest["permission_confirmed"],
                    sensitive_building=manifest["sensitive_building"],
                    license_note=manifest["license_note"],
                    file_name=f"preview-{asset_id[:16]}-page-{page}.png",
                    source_kind=manifest.get("source_kind", "operator_upload"),
                    coordinate_system="raster_pixel",
                )
                self.artifacts.begin(project, upload_id, request)
                try:
                    for index, offset in enumerate(range(0, len(png), 262144)):
                        self.artifacts.chunk(
                            project,
                            upload_id,
                            AssetChunk(
                                index=index,
                                data_base64=base64.b64encode(png[offset : offset + 262144]).decode(
                                    "ascii"
                                ),
                            ),
                        )
                    stored = self.artifacts.finish(project, upload_id)
                finally:
                    # Clean only this operation's random upload directory. Finished
                    # immutable content lives separately in the existing Asset vault.
                    self.artifacts.delete_upload(project, upload_id)
                raster_preview = {
                    "asset_id": stored["asset_id"],
                    "media_type": "image/png",
                    "width_px": width,
                    "height_px": height,
                    "page_index": page,
                    "source_asset_id": asset_id,
                }

            try:
                result = extract_plan_features_from_asset(
                    asset,
                    content,
                    page_index=page,
                    max_dimension_px=dimension,
                    raster_sink=store_raster,
                )
            except PlanExtractionUnavailable as exc:
                raise StorageError(
                    "plan_extraction_unavailable: optional plan-import decoder is required"
                ) from exc
        elif operation == "reviewed_trace":
            saved = self._preview(project, body["reviewed_preview_id"], asset_id, "proposal")
            raster_preview = saved.get("raster_preview")
            reviewed = PlanExtractionProposal.model_validate(saved["value"])
            calibration = {
                key: value
                for key, value in body.items()
                if key not in {"operation", "reviewed_preview_id"}
            }
            calibration.setdefault("operator_id", "operator")
            trace = manual_trace_from_reviewed_features(reviewed, **calibration)
            if raster_preview is not None and (
                raster_preview.get("page_index") != trace.source_page_index
                or raster_preview.get("width_px") != trace.raster_pixel_width
                or raster_preview.get("height_px") != trace.raster_pixel_height
            ):
                raise StorageError("plan_raster_preview_trace_calibration_mismatch")
            result = trace_uploaded_plan(asset, trace, reviewed_proposal=reviewed)
        elif operation == "geojson":
            result = import_metric_geojson(asset, content)
        elif operation == "proposal":
            # External extracted suggestions only; this is not an OCR/vision call.
            result = create_plan_extraction_proposal(
                asset,
                extraction_method=body.get("extraction_method", "operator_trace"),
                extraction_source_id=body.get("extraction_source_id", asset.source_id),
                features=tuple(
                    PlanFeatureProposal.model_validate(row) for row in body.get("features", [])
                ),
                raster_size_px=body.get("raster_size_px"),
            )
        elif operation == "review":
            proposal = self._preview(project, body["proposal_preview_id"], asset_id, "proposal")
            raster_preview = proposal.get("raster_preview")
            result = review_plan_extraction(
                PlanExtractionProposal.model_validate(proposal["value"]),
                tuple(PlanFeatureReview.model_validate(row) for row in body["reviews"]),
            )
        elif operation == "geometry_revision":
            base_record = self.geometry(project, body["base_geometry_id"])
            raster_preview = base_record.get("raster_preview")
            if base_record.get("geometry_hash") != body["base_geometry_hash"]:
                raise StorageError("plan_geometry_revision_stale_base")
            base_geometry = ImportedPlanGeometry.model_validate(base_record["geometry"])
            if (
                base_geometry.asset.sha256,
                base_geometry.asset.byte_count,
                base_geometry.asset.media_type,
            ) != (asset.sha256, asset.byte_count, asset.media_type):
                raise StorageError("plan_geometry_revision_asset_mismatch")
            try:
                result = revise_imported_plan_geometry(
                    base_geometry,
                    geometry=body["geometry"],
                    editable_geometry=body.get("editable_geometry"),
                )
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"plan_geometry_revision_invalid: {plan_rejection_reason(exc)}"
                ) from exc
        else:
            reviewed = None
            if body.get("reviewed_preview_id"):
                saved = self._preview(project, body["reviewed_preview_id"], asset_id, "proposal")
                raster_preview = saved.get("raster_preview")
                reviewed = PlanExtractionProposal.model_validate(saved["value"])
            if body.get("raster_preview_id"):
                raster_record = self._preview(
                    project, body["raster_preview_id"], asset_id, "proposal"
                )
                referenced_raster = raster_record.get("raster_preview")
                if referenced_raster is None:
                    raise StorageError("plan_raster_preview_missing")
                if raster_preview is not None and raster_preview != referenced_raster:
                    raise StorageError("plan_raster_preview_reference_conflict")
                raster_preview = referenced_raster
            if not isinstance(body["trace"], dict):
                raise StorageError("invalid_plan_trace")
            trace_document = dict(body["trace"])
            supplied_page = body.get("page_index", trace_document.get("source_page_index"))
            if supplied_page is None and raster_preview is not None:
                supplied_page = raster_preview.get("page_index")
            if supplied_page is None and asset.media_type != "application/pdf":
                supplied_page = 0
            if supplied_page is not None and (
                type(supplied_page) is not int or not 0 <= supplied_page <= 10_000
            ):
                raise StorageError("invalid_plan_trace_page_index")
            if (
                "source_page_index" in trace_document
                and supplied_page is not None
                and trace_document["source_page_index"] != supplied_page
            ):
                raise StorageError("plan_trace_page_reference_mismatch")
            if supplied_page is not None:
                trace_document["source_page_index"] = supplied_page
            trace = ManualPlanTrace.model_validate(trace_document)
            if raster_preview is not None and (
                raster_preview.get("page_index") != trace.source_page_index
                or raster_preview.get("width_px") != trace.raster_pixel_width
                or raster_preview.get("height_px") != trace.raster_pixel_height
            ):
                raise StorageError("plan_raster_preview_trace_calibration_mismatch")
            result = trace_uploaded_plan(asset, trace, reviewed_proposal=reviewed)
        return result, raster_preview

    def _preview(self, project: str, preview_id: str, asset: str, kind: str) -> dict[str, Any]:
        record = self.artifacts.draft(project, preview_id)["content"]
        if (
            record.get("record_type") != "plan_preview"
            or record.get("asset_id") != asset
            or record.get("kind") != kind
            or digest(record.get("value")) != record.get("value_hash")
        ):
            raise StorageError("plan_preview_context_or_hash_mismatch")
        raster = record.get("raster_preview")
        if raster is not None:
            if (
                not isinstance(raster, dict)
                or raster.get("source_asset_id") != asset
                or raster.get("media_type") != "image/png"
                or not isinstance(raster.get("asset_id"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", raster["asset_id"])
                or any(
                    type(raster.get(key)) is not int or not 1 <= raster[key] <= 2400
                    for key in ("width_px", "height_px")
                )
                or type(raster.get("page_index")) is not int
                or not 0 <= raster["page_index"] < 200
            ):
                raise StorageError("plan_raster_preview_context_mismatch")
            manifest = self.artifacts.asset(project, raster["asset_id"])
            if manifest["media_type"] != "image/png" or manifest["sha256"] != raster["asset_id"]:
                raise StorageError("plan_raster_preview_media_mismatch")
        return record

    def _bind_raster_provenance(
        self, project: str, geometry: ImportedPlanGeometry, raster: dict[str, Any]
    ) -> ImportedPlanGeometry:
        provenance = geometry.source_provenance
        if provenance is None or not isinstance(raster.get("asset_id"), str):
            raise StorageError("plan_raster_provenance_missing")
        manifest = self.artifacts.asset(project, raster["asset_id"])
        if (
            manifest.get("media_type") != "image/png"
            or manifest.get("sha256") != raster["asset_id"]
            or raster.get("source_asset_id") != geometry.asset.sha256
            or raster.get("page_index") != provenance.source_page_index
            or (raster.get("width_px"), raster.get("height_px")) != provenance.raster_size_px
        ):
            raise StorageError("plan_raster_provenance_mismatch")
        bound = provenance.model_copy(update={"raster_asset_sha256": manifest["sha256"]})
        return geometry.model_copy(update={"source_provenance": bound})

    def _validate_approved_raster_binding(
        self,
        project: str,
        asset_id: str,
        geometry: ImportedPlanGeometry,
        raster: dict[str, Any] | None,
    ) -> None:
        provenance = geometry.source_provenance
        if raster is None:
            if provenance is not None and provenance.raster_asset_sha256 is not None:
                raise StorageError("plan_geometry_raster_provenance_missing")
            return
        if (
            not isinstance(raster, dict)
            or raster.get("source_asset_id") != asset_id
            or raster.get("media_type") != "image/png"
            or not isinstance(raster.get("asset_id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", raster["asset_id"])
            or type(raster.get("page_index")) is not int
            or not 0 <= raster["page_index"] < 200
            or any(
                type(raster.get(key)) is not int or not 1 <= raster[key] <= 2400
                for key in ("width_px", "height_px")
            )
        ):
            raise StorageError("plan_geometry_raster_context_invalid")
        if provenance is None:
            # Older approved Geometry records may have a raster sidecar but predate
            # the versioned provenance field. Keep them readable without upgrading.
            pass
        elif (
            raster.get("asset_id") != provenance.raster_asset_sha256
            or raster.get("page_index") != provenance.source_page_index
            or (raster.get("width_px"), raster.get("height_px")) != provenance.raster_size_px
        ):
            raise StorageError("plan_geometry_raster_provenance_mismatch")
        manifest = self.artifacts.asset(project, raster["asset_id"])
        if (
            manifest.get("media_type") != "image/png"
            or manifest.get("sha256") != raster["asset_id"]
            or (provenance is not None and manifest["sha256"] != provenance.raster_asset_sha256)
        ):
            raise StorageError("plan_geometry_raster_asset_binding_mismatch")

    def approve(self, project: str, geometry_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        fields(body, {"preview_id", "asset_id", "approved_hash", "approved"})
        if body["approved"] is not True:
            raise StorageError("geometry_requires_explicit_approval")
        preview = self._preview(project, body["preview_id"], body["asset_id"], "geometry")
        if preview["value_hash"] != body["approved_hash"]:
            raise StorageError("geometry_approval_hash_mismatch")
        value = ImportedPlanGeometry.model_validate(preview["value"]).model_dump(mode="json")
        manifest = self.artifacts.asset(project, body["asset_id"])
        if (
            value["asset"]["sha256"],
            value["asset"]["byte_count"],
            value["asset"]["media_type"],
        ) != (manifest["sha256"], manifest["byte_count"], manifest["media_type"]):
            raise StorageError("plan_asset_manifest_mismatch")
        geometry = ImportedPlanGeometry.model_validate(value)
        self._validate_approved_raster_binding(
            project, body["asset_id"], geometry, preview.get("raster_preview")
        )
        record = {
            "schema_version": 1,
            "geometry_id": geometry_id,
            "asset_id": body["asset_id"],
            "geometry_hash": digest(value),
            "geometry": value,
            "approved": True,
            "validation": "region_structural_rules_only",
            "runtime_verified": False,
        }
        if preview.get("raster_preview") is not None:
            record["raster_preview"] = preview["raster_preview"]
        with self.store.transaction():
            self.store._write(
                self.store._path(project, "geometries", f"{geometry_id}.json"),
                record,
                immutable=True,
            )
        return record

    def geometry(self, project: str, identity: str) -> dict[str, Any]:
        record = self.store._read(self.store._path(project, "geometries", f"{identity}.json"))
        if (
            record.get("approved") is not True
            or digest(record["geometry"]) != record["geometry_hash"]
        ):
            raise StorageError("saved_geometry_integrity_mismatch")
        geometry = ImportedPlanGeometry.model_validate(record["geometry"])
        if geometry.asset.sha256 != record.get("asset_id"):
            raise StorageError("saved_geometry_asset_binding_mismatch")
        self._validate_approved_raster_binding(
            project, record["asset_id"], geometry, record.get("raster_preview")
        )
        return record


def plan_rejection_reason(exc: Exception) -> str:
    """Why a plan input was refused, naming contract fields without echoing their values."""
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_input=False)
        parts = [
            f"{'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"
            for error in errors[:5]
        ]
        if len(errors) > 5:
            parts.append(f"and {len(errors) - 5} more")
        return "; ".join(parts)
    return str(exc)[:600]
