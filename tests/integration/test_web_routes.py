"""Page routing and navigation context for the Demo/Admin web modes.

The static helpers served from the allow-list are checked in
tests/unit/web/test_server.py.
"""

from __future__ import annotations

from pathlib import Path


def test_root_redirects_to_demo_without_dropping_run_context(
    tmp_path: Path, bare_request_handler
) -> None:
    handler, statuses, headers, _ = bare_request_handler(tmp_path)
    handler.path = "/?run_id=run-one&branch_id=baseline&minute=1440"

    handler.do_GET()

    assert statuses == [302]
    expected_location = "/demo?run_id=run-one&branch_id=baseline&minute=1440"
    assert headers["Location"] == expected_location
    assert headers["Cache-Control"] == "no-store"


def test_demo_and_admin_routes_set_mode_and_switch_link(
    tmp_path: Path, bare_request_handler
) -> None:
    for route in ("/demo", "/admin"):
        handler, statuses, headers, output = bare_request_handler(tmp_path)
        handler.path = f"{route}?run_id=run-one&branch_id=baseline&minute=1440"

        handler.do_GET()

        assert statuses == [200]
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        expected_mode = b'data-mode="' + route[1:].encode() + b'"'
        assert expected_mode in output.getvalue()
        assert b"mode-switch" in output.getvalue()
        assert b'id="ws-intervention-preview" type="button" disabled' in output.getvalue()
        assert b'id="ws-fork" type="button" disabled' in output.getvalue()
