"""Page routing and navigation context for the Demo/Admin web modes."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


def _request_handler(tmp_path: Path):
    handler_type = _handler_for(InspectorService(tmp_path), DemoService())
    handler = handler_type.__new__(handler_type)
    statuses: list[int] = []
    headers: dict[str, str] = {}
    output = BytesIO()
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler.wfile = output
    return handler, statuses, headers, output


def test_root_redirects_to_demo_without_dropping_run_context(tmp_path: Path) -> None:
    handler, statuses, headers, _ = _request_handler(tmp_path)
    handler.path = "/?run_id=run-one&branch_id=baseline&minute=1440"

    handler.do_GET()

    assert statuses == [302]
    expected_location = "/demo?run_id=run-one&branch_id=baseline&minute=1440"
    assert headers["Location"] == expected_location
    assert headers["Cache-Control"] == "no-store"


def test_demo_and_admin_routes_set_mode_and_switch_link(tmp_path: Path) -> None:
    for route in ("/demo", "/admin"):
        handler, statuses, headers, output = _request_handler(tmp_path)
        handler.path = f"{route}?run_id=run-one&branch_id=baseline&minute=1440"

        handler.do_GET()

        assert statuses == [200]
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        expected_mode = b'data-mode="' + route[1:].encode() + b'"'
        assert expected_mode in output.getvalue()
        assert b"mode-switch" in output.getvalue()
        assert b'id="ws-intervention-preview" type="button" disabled' in output.getvalue()
        assert b'id="ws-fork" type="button" disabled' in output.getvalue()


def test_map_lod_helper_is_served_to_project_maps(tmp_path: Path) -> None:
    handler, statuses, headers, output = _request_handler(tmp_path)

    handler._static_response("/map-lod.js")

    assert statuses == [200]
    assert headers["Content-Type"].startswith("text/javascript")
    assert b"ShelterSimMapLOD" in output.getvalue()


def test_offline_pdfjs_renderer_and_worker_are_served_from_allowlist(
    tmp_path: Path,
) -> None:
    for asset in ("pdf.min.mjs", "pdf.worker.min.mjs"):
        handler, statuses, headers, output = _request_handler(tmp_path)
        handler._static_response(f"/vendor/pdfjs/{asset}")
        assert statuses == [200]
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert len(output.getvalue()) > 1_000

    handler, statuses, _, output = _request_handler(tmp_path)
    handler._json_response = lambda status, document: statuses.append(status)
    handler._static_response("/vendor/pdfjs/../../server.py")
    assert statuses == [404]
