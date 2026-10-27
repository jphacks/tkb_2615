"""Bounded, low-confidence raster plan suggestions that require human review."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from io import BytesIO
from typing import Any

from sheltersim.region.plan_import import (
    MAX_PLAN_BYTES,
    PlanAsset,
    PlanExtractionProposal,
    PlanFeatureProposal,
    create_plan_extraction_proposal,
)

RASTER_HEURISTIC_SOURCE_ID = "ASM-PLAN-RASTER-HEURISTIC-V1"
MAX_RENDER_DIMENSION_PX = 2_400
MAX_IMAGE_PIXELS = 80_000_000
MAX_PDF_PAGES = 200
MAX_PROPOSED_FEATURES = 1_000


class PlanExtractionUnavailable(RuntimeError):
    """Raised when optional PDF/image decoding support is not installed."""


def extract_plan_features_from_asset(
    asset: PlanAsset,
    content: bytes,
    *,
    page_index: int = 0,
    max_dimension_px: int = 1_600,
    raster_sink: Callable[[bytes, int, int], None] | None = None,
) -> PlanExtractionProposal:
    """Render one uploaded page and suggest orthogonal room/wall/opening candidates.

    The deterministic raster heuristic is deliberately conservative. Its output is always
    pending review, carries the exact source asset hash, and is never a calibrated plan.
    """
    if asset.status != "manual_trace_required" or asset.media_type not in {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
    }:
        raise ValueError("candidate extraction requires a PDF or image plan asset")
    if not content or len(content) != asset.byte_count or len(content) > MAX_PLAN_BYTES:
        raise ValueError("asset content length does not match the bounded plan metadata")
    if hashlib.sha256(content).hexdigest() != asset.sha256:
        raise ValueError("asset content hash does not match the selected plan")
    if page_index < 0 or page_index > 10_000:
        raise ValueError("plan page index is outside the supported range")
    if not 64 <= max_dimension_px <= MAX_RENDER_DIMENSION_PX:
        raise ValueError(
            f"render dimension must be between 64 and {MAX_RENDER_DIMENSION_PX} pixels"
        )

    pixels, width, height = _render_page(asset.media_type, content, page_index, max_dimension_px)
    features = _candidate_features(pixels, width, height, asset.sha256, page_index)
    if raster_sink is not None:
        # Encode this very image, whose dimensions/coordinates generated features.
        # The callback is supplied by installed application code, never request JSON.
        stream = BytesIO()
        pixels.save(stream, format="PNG")
        raster = stream.getvalue()
        if not raster or len(raster) > MAX_PLAN_BYTES:
            raise ValueError("rendered PNG exceeds the bounded plan asset limit")
        raster_sink(raster, width, height)
    return create_plan_extraction_proposal(
        asset,
        extraction_method="raster_heuristic",
        extraction_source_id=RASTER_HEURISTIC_SOURCE_ID,
        features=features,
        raster_size_px=(width, height),
        notes=(
            "All candidates are low-confidence image heuristics and remain pending human review.",
            "The extractor does not read labels, infer scale, identify facility equipment, "
            "or establish legal/egress compliance.",
            f"Rendered page {page_index}; pixel dimensions are {width} x {height}. Calibrate "
            "scale and correct geometry before mapping.",
        ),
    )


def _render_page(
    media_type: str, content: bytes, page_index: int, max_dimension_px: int
) -> tuple[Any, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise PlanExtractionUnavailable(
            "install the 'plan-import' extra to inspect PDF/image plans"
        ) from exc

    if media_type == "application/pdf":
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise PlanExtractionUnavailable(
                "install the 'plan-import' extra to inspect PDF plans"
            ) from exc
        document = pdfium.PdfDocument(content)
        try:
            page_count = len(document)
            if page_count > MAX_PDF_PAGES:
                raise ValueError(f"PDF page count exceeds the {MAX_PDF_PAGES}-page review limit")
            if page_index >= page_count:
                raise ValueError("selected PDF page does not exist")
            page = document[page_index]
            try:
                page_width, page_height = page.get_size()
                if not all(
                    math.isfinite(value) and 0 < value <= 100_000
                    for value in (page_width, page_height)
                ):
                    raise ValueError("PDF page dimensions are invalid or too large")
                scale = min(max_dimension_px / page_width, max_dimension_px / page_height, 4.0)
                rendered = page.render(scale=scale).to_pil()
                rendered.load()
                gray = rendered.convert("L")
                return gray, gray.width, gray.height
            finally:
                page.close()
        finally:
            document.close()

    if page_index != 0:
        raise ValueError("image assets contain only page index 0")
    try:
        image = Image.open(BytesIO(content))
    except Exception as exc:
        raise ValueError("uploaded image could not be decoded") from exc
    try:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions exceed the bounded extraction limit")
        image.seek(0)
        image.thumbnail((max_dimension_px, max_dimension_px))
        image.load()
        gray = image.convert("L")
        return gray, gray.width, gray.height
    finally:
        image.close()


def _candidate_features(
    pixels: Any, width: int, height: int, asset_sha256: str, page_index: int
) -> tuple[PlanFeatureProposal, ...]:
    if width <= 0 or height <= 0 or width * height > MAX_RENDER_DIMENSION_PX**2:
        raise ValueError("rendered page dimensions exceed the bounded extraction limit")
    data = pixels.load()
    min_line = max(24, round(min(width, height) * 0.035))
    max_runs = 180
    horizontal = _long_runs(
        data, width, height, horizontal=True, min_line=min_line, max_runs=max_runs
    )
    vertical = _long_runs(
        data, width, height, horizontal=False, min_line=min_line, max_runs=max_runs
    )
    features: list[PlanFeatureProposal] = []

    for kind, segments in (("wall", horizontal), ("wall", vertical)):
        for index, (x1, y1, x2, y2, support) in enumerate(segments):
            if len(features) >= MAX_PROPOSED_FEATURES:
                break
            feature_id = f"wall-{len(features):04d}"
            features.append(
                _feature(
                    feature_id=feature_id,
                    asset_sha256=asset_sha256,
                    page_index=page_index,
                    feature_kind="wall",
                    points=((x1, y1), (x2, y2)),
                    confidence=min(0.42, 0.16 + support * 0.02),
                    attributes={
                        "wall_id": feature_id,
                        "orientation_candidate": "horizontal" if y1 == y2 else "vertical",
                    },
                )
            )

    for index, (x1, y1, x2, y2, support) in enumerate(
        _opening_gaps(data, width, height, min_line=min_line)
    ):
        if len(features) >= MAX_PROPOSED_FEATURES:
            break
        feature_id = f"opening-{index:04d}"
        features.append(
            _feature(
                feature_id=feature_id,
                asset_sha256=asset_sha256,
                page_index=page_index,
                feature_kind="opening",
                points=((x1, y1), (x2, y2)),
                confidence=min(0.38, 0.10 + support * 0.02),
                attributes={
                    "opening_id": feature_id,
                    "opening_kind": "doorway_candidate",
                    "host_wall_candidate_id": None,
                    "width_m": None,
                },
            )
        )

    for index, polygon in enumerate(
        _rectangular_room_candidates(horizontal, vertical, width, height)[:100]
    ):
        if len(features) >= MAX_PROPOSED_FEATURES:
            break
        features.append(
            _feature(
                feature_id=f"zone-{index:04d}",
                asset_sha256=asset_sha256,
                page_index=page_index,
                feature_kind="zone",
                points=polygon,
                confidence=0.18,
                attributes={
                    "zone_id": f"review-zone-{index:04d}",
                    "name": "Unlabeled room candidate",
                    "usable_fraction": 0.9,
                },
            )
        )
    return tuple(features)


def _long_runs(
    pixels: Any, width: int, height: int, *, horizontal: bool, min_line: int, max_runs: int
) -> list[tuple[float, float, float, float, int]]:
    scan_count, scan_length = (height, width) if horizontal else (width, height)
    raw: list[tuple[int, int, int]] = []
    for scan in range(scan_count):
        start: int | None = None
        for position in range(scan_length + 1):
            if position < scan_length:
                dark = pixels[position, scan] <= 96 if horizontal else pixels[scan, position] <= 96
            else:
                dark = False
            if dark and start is None:
                start = position
            elif not dark and start is not None:
                if position - start >= min_line:
                    raw.append((scan, start, position - 1))
                start = None
        if len(raw) >= max_runs * 4:
            break

    groups: list[list[tuple[int, int, int]]] = []
    for scan, start, end in raw:
        match = next(
            (
                group
                for group in reversed(groups[-64:])
                if scan - group[-1][0] <= 2
                and min(end, group[-1][2]) - max(start, group[-1][1]) + 1
                >= 0.65 * min(end - start + 1, group[-1][2] - group[-1][1] + 1)
            ),
            None,
        )
        if match is None:
            groups.append([(scan, start, end)])
        else:
            match.append((scan, start, end))
    result = []
    for group in groups:
        cross = sum(item[0] for item in group) / len(group)
        start = min(item[1] for item in group)
        end = max(item[2] for item in group)
        if horizontal:
            result.append((float(start), cross, float(end), cross, len(group)))
        else:
            result.append((cross, float(start), cross, float(end), len(group)))
    result.sort(
        key=lambda item: (-math.hypot(item[2] - item[0], item[3] - item[1]), item[1], item[0])
    )
    return result[:max_runs]


def _opening_gaps(
    pixels: Any, width: int, height: int, *, min_line: int
) -> list[tuple[float, float, float, float, int]]:
    raw: list[tuple[bool, int, int, int]] = []
    min_gap = max(7, round(min(width, height) * 0.006))
    for horizontal in (True, False):
        orientation_candidates: list[tuple[bool, int, int, int]] = []
        scan_count = height if horizontal else width
        for scan in range(scan_count):
            runs = (
                _runs_on_row(pixels, width, scan)
                if horizontal
                else _runs_on_column(pixels, height, scan)
            )
            for (left_start, left_end), (right_start, right_end) in zip(runs, runs[1:]):
                gap = right_start - left_end - 1
                if (
                    left_end - left_start + 1 >= min_line
                    and right_end - right_start + 1 >= min_line
                    and min_gap <= gap <= min_line
                ):
                    orientation_candidates.append((horizontal, scan, left_end, right_start))
                    if len(orientation_candidates) >= 1_000:
                        break
            if len(orientation_candidates) >= 1_000:
                break
        raw.extend(orientation_candidates)

    # Consecutive raster scan lines often observe the same doorway. Group only
    # nearby parallel gaps; keep a low support-derived confidence for review.
    groups: list[list[tuple[bool, int, int, int]]] = []
    for item in raw:
        horizontal, scan, gap_start, gap_end = item
        group = next(
            (
                candidate
                for candidate in reversed(groups[-100:])
                if candidate[-1][0] == horizontal
                and scan - candidate[-1][1] <= 3
                and abs((gap_start + gap_end) - (candidate[-1][2] + candidate[-1][3])) <= 8
            ),
            None,
        )
        if group is None:
            groups.append([item])
        else:
            group.append(item)

    result: list[tuple[float, float, float, float, int]] = []
    for group in groups:
        horizontal = group[0][0]
        scan = sum(item[1] for item in group) / len(group)
        gap_start = sum(item[2] for item in group) / len(group)
        gap_end = sum(item[3] for item in group) / len(group)
        result.append(
            (gap_start, scan, gap_end, scan, len(group))
            if horizontal
            else (scan, gap_start, scan, gap_end, len(group))
        )
    result.sort(key=lambda item: (-item[4], item[1], item[0]))
    return result[:100]


def _runs_on_row(pixels: Any, width: int, y: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width + 1):
        dark = x < width and pixels[x, y] <= 96
        if dark and start is None:
            start = x
        elif not dark and start is not None:
            runs.append((start, x - 1))
            start = None
    return runs


def _runs_on_column(pixels: Any, height: int, x: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for y in range(height + 1):
        dark = y < height and pixels[x, y] <= 96
        if dark and start is None:
            start = y
        elif not dark and start is not None:
            runs.append((start, y - 1))
            start = None
    return runs


def _rectangular_room_candidates(
    horizontal: list[tuple[float, float, float, float, int]],
    vertical: list[tuple[float, float, float, float, int]],
    width: int,
    height: int,
) -> list[tuple[tuple[float, float], ...]]:
    rooms: list[tuple[tuple[float, float], ...]] = []
    seen: set[tuple[float, float, float, float]] = set()
    horizontals = sorted(horizontal[:120], key=lambda item: (item[1], item[0]))
    verticals = vertical[:120]
    minimum_height = max(24, height * 0.05)
    minimum_width = max(24, width * 0.04)
    for top_index, top in enumerate(horizontals):
        for bottom in horizontals[top_index + 1 :]:
            if bottom[1] - top[1] < minimum_height:
                continue
            overlap_left = max(top[0], bottom[0])
            overlap_right = min(top[2], bottom[2])
            crossings = sorted(
                {
                    round(item[0], 3)
                    for item in verticals
                    if item[1] <= top[1] + 6
                    and item[3] >= bottom[1] - 6
                    and overlap_left + 6 <= item[0] <= overlap_right - 6
                }
            )
            for left_x, right_x in zip(crossings, crossings[1:]):
                if right_x - left_x < minimum_width:
                    continue
                key = (left_x, round(top[1], 3), right_x, round(bottom[1], 3))
                if key in seen:
                    continue
                seen.add(key)
                rooms.append(
                    (
                        (left_x, top[1]),
                        (right_x, top[1]),
                        (right_x, bottom[1]),
                        (left_x, bottom[1]),
                    )
                )
                if len(rooms) >= 100:
                    return rooms
    return rooms


def _feature(
    *,
    feature_id: str,
    asset_sha256: str,
    page_index: int,
    feature_kind: str,
    points: tuple[tuple[float, float], ...],
    confidence: float,
    attributes: dict[str, Any] | None = None,
) -> PlanFeatureProposal:
    geometry_kind = {"zone": "polygon", "wall": "line", "opening": "line"}[feature_kind]
    return PlanFeatureProposal(
        feature_id=feature_id,
        asset_sha256=asset_sha256,
        page_index=page_index,
        feature_kind=feature_kind,
        geometry_kind=geometry_kind,
        coordinates_px=points,
        attributes=attributes or {},
        extraction_method="raster_heuristic",
        confidence=confidence,
        extraction_source_id=RASTER_HEURISTIC_SOURCE_ID,
        review_status="pending",
    )
