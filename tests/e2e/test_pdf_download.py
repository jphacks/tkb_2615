"""A PDF report is downloaded over HTTP as an attachment, and a request from another origin
is refused.

The report itself is checked in tests/integration/test_pdf_report.py.
"""

from __future__ import annotations

import json
from io import BytesIO

from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


def test_pdf_http_attachment_and_existing_origin_guard(
    report_saved_run, monkeypatch, tmp_path, install_report_router
):
    api = report_saved_run
    install_report_router(api, monkeypatch)
    handler_type = _handler_for(InspectorService(tmp_path), projects=api)

    def request(origin="http://localhost:8765"):
        handler = handler_type.__new__(handler_type)
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda key, value: headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()
        body = json.dumps({"branch_ids": ["main"], "minute": 40}).encode()
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": origin,
            "Host": "localhost:8765",
        }
        handler.path = "/api/v1/projects/p/runs/r/pdf-report"
        handler.do_POST()
        return statuses, headers, handler.wfile.getvalue()

    statuses, headers, body = request()
    assert statuses == [200] and headers["Content-Type"] == "application/pdf"
    assert "attachment" in headers["Content-Disposition"] and body.startswith(b"%PDF-")
    assert int(headers["Content-Length"]) == len(body)
    assert request("https://untrusted.example")[0] == [403]
