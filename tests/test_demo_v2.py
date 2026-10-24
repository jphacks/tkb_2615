"""Offline routing and target-resolution regressions for the independent backup UI."""

from io import BytesIO

import pytest

from sheltersim.llm.natural_language import parse_natural_language
from sheltersim.llm.schemas import ParseRequest, ScenarioSummary, ZoneTarget
from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


@pytest.mark.parametrize(
    ("route", "content_type", "marker"),
    [
        ("/demo-v2", "text/html", "避難所IF！".encode()),
        ("/demo-v2.css", "text/css", b"--navy"),
        ("/demo-v2.js", "text/javascript", b"scenario-approve"),
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
        assert b'/demo-v2.js' in body and b'/demo-v2.css' in body
        assert b'src="/project.js"' not in body and b'src="/app.js"' not in body
        assert b'https://cdn' not in body


def parse_target(text):
    return parse_natural_language(ParseRequest(
        text=text,
        branch_id="main",
        selected_minute=5,
        scenario_summary=ScenarioSummary(
            duration_minutes=60,
            zones=[
                ZoneTarget(id="toilet", name="トイレ"),
                ZoneTarget(id="staff", name="スタッフ"),
                ZoneTarget(id="sleeping", name="避難スペース"),
            ],
        ),
    ))


@pytest.mark.parametrize(("text", "target"), [
    ("トイレを2基追加する zone_id: toilet", "toilet"),
    ("スタッフを2人追加する zone_id: staff", "staff"),
    ("避難スペースではなく zone_id: toilet にトイレを2基追加", "toilet"),
])
def test_explicit_existing_zone_overrides_ambiguous_common_words(text, target):
    result = parse_target(text)
    assert result.status == "parsed"
    assert result.draft_plan.effects[0].target_selector.target_id == target
    assert result.draft_plan.effects[0].start_minute == 5


def test_unknown_zone_stays_rejected_and_unqualified_text_requires_choice():
    unknown = parse_target("トイレを2基追加する zone_id: missing")
    assert unknown.status == "rejected"
    assert unknown.issues[0].code == "unknown_target"
    ambiguous = parse_target("トイレを2基追加する")
    assert ambiguous.status == "clarification_required"
