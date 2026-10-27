"""The independent backup UI (demo-v2) is served locally by the HTTP routes.

Its zone target resolution is checked in tests/unit/llm/test_natural_language.py.
"""

from io import BytesIO

import pytest

from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


@pytest.mark.parametrize(
    ("route", "content_type", "marker"),
    [
        ("/demo-v2", "text/html", "避難所IF！".encode()),
        ("/demo-v2.css", "text/css", b"--navy"),
        ("/demo-v2.js", "text/javascript", b"scenario-approve"),
        ("/demo-v2-map.js", "text/javascript", b"Hinanj"),
        ("/demo-v2-scene.js", "text/javascript", b"HinanjoScene"),
        ("/demo-v2-clock.js", "text/javascript", b"HinanjoReplayClock"),
        ("/demo-v2-replay.js", "text/javascript", b"HinanjoReplayPlayer"),
        ("/demo-v2-sprites.js", "text/javascript", b"HinanjoSprites"),
    ],
)
def test_backup_assets_are_independent_and_served_locally(tmp_path, route, content_type, marker):
    handler_type = _handler_for(InspectorService(tmp_path), DemoService())
    handler = handler_type.__new__(handler_type)
    statuses, headers = [], {}
    handler.send_response = statuses.append
    handler.send_header = lambda key, value: headers.__setitem__(key, value)
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler.path = route
    handler.do_GET()
    body = handler.wfile.getvalue()
    assert statuses == [200]
    assert headers["Content-Type"].startswith(content_type)
    assert marker in body
    if route == "/demo-v2":
        assert b"/demo-v2.js" in body and b"/demo-v2.css" in body
        assert b'src="/project.js"' not in body and b'src="/app.js"' not in body
        assert b"https://cdn" not in body


