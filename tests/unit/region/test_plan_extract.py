"""The raster heuristic proposes walls, doorways and rooms from an uploaded drawing.

Every candidate stays pending human review with a low confidence, and PDF pages are
chosen by index. The drawings are synthetic: the shelter of tests/support/plans.py
and plain rectangles split into rooms by full-height partitions.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from support.plans import rooms_plan_png, shelter_plan_png

from sheltersim.region.plan_extract import extract_plan_features_from_asset
from sheltersim.region.plan_import import inspect_plan_upload


def _extract(content: bytes, media_type: str = "image/png", page_index: int = 0):
    asset = inspect_plan_upload(
        file_name="plan.pdf" if media_type == "application/pdf" else "plan.png",
        media_type=media_type,
        content=content,
        rights_status="operator_permission_asserted",
    )
    return extract_plan_features_from_asset(
        asset, content, page_index=page_index, max_dimension_px=400
    )


def test_walls_and_doorways_of_a_drawn_plan_are_pending_low_confidence_candidates():
    content = shelter_plan_png()

    proposal = _extract(content)

    assert proposal.asset_sha256 == hashlib.sha256(content).hexdigest()
    assert proposal.raster_size_px == (400, 300)
    assert all(item.review_status == "pending" for item in proposal.features)
    assert all(item.confidence < 0.5 for item in proposal.features)
    walls = [item for item in proposal.features if item.feature_kind == "wall"]
    # Three horizontal walls and five vertical: west, east and the partition in three pieces.
    assert len(walls) == 8
    doorways = sorted(
        tuple(round(value) for point in item.coordinates_px for value in point)
        for item in proposal.features
        if item.feature_kind == "opening"
    )
    # The gaps in the partition, where SHELTER_TRACE puts its two doors.
    assert doorways == [(200, 70, 200, 90), (200, 200, 200, 220)]


@pytest.mark.xfail(
    strict=True, reason="#200: a room bounded by an outer wall is never a room candidate"
)
@pytest.mark.parametrize("partitions", [0, 1, 2])
def test_every_room_of_a_drawn_plan_is_a_room_candidate(partitions):
    proposal = _extract(rooms_plan_png(partitions))

    rooms = [item for item in proposal.features if item.feature_kind == "zone"]
    assert len(rooms) == partitions + 1


def test_a_pdf_plan_is_extracted_from_the_chosen_page():
    from PIL import Image

    drawn = Image.open(io.BytesIO(shelter_plan_png())).convert("RGB")
    blank = Image.new("RGB", drawn.size, "white")
    stream = io.BytesIO()
    drawn.save(stream, format="PDF", save_all=True, append_images=[blank])
    content = stream.getvalue()

    first = _extract(content, "application/pdf", page_index=0)
    second = _extract(content, "application/pdf", page_index=1)

    assert {item.page_index for item in first.features} == {0}
    assert len(first.features) == 10
    assert second.features == ()
    with pytest.raises(ValueError, match="selected PDF page does not exist"):
        _extract(content, "application/pdf", page_index=2)
