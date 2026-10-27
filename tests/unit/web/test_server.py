"""The server's static allow-list serves the map level-of-detail helper and the offline
PDF.js renderer and worker.

Page routing over HTTP is checked in tests/integration/test_web_routes.py.
"""

from __future__ import annotations

from pathlib import Path


def test_map_lod_helper_is_served_to_project_maps(tmp_path: Path, bare_request_handler) -> None:
    handler, statuses, headers, output = bare_request_handler(tmp_path)

    handler._static_response("/map-lod.js")

    assert statuses == [200]
    assert headers["Content-Type"].startswith("text/javascript")
    assert b"ShelterSimMapLOD" in output.getvalue()


def test_offline_pdfjs_renderer_and_worker_are_served_from_allowlist(
    tmp_path: Path, bare_request_handler
) -> None:
    for asset in ("pdf.min.mjs", "pdf.worker.min.mjs"):
        handler, statuses, headers, output = bare_request_handler(tmp_path)
        handler._static_response(f"/vendor/pdfjs/{asset}")
        assert statuses == [200]
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert len(output.getvalue()) > 1_000

    handler, statuses, _, output = bare_request_handler(tmp_path)
    handler._json_response = lambda status, document: statuses.append(status)
    handler._static_response("/vendor/pdfjs/../../server.py")
    assert statuses == [404]
