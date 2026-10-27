"""The admin page (index.html) has keyboard tabs, and its interactive controls are for
the demo only.
"""

from __future__ import annotations

from pathlib import Path


def test_admin_shell_has_keyboard_tabs_and_interactive_controls_are_demo_only() -> None:
    root = Path(__file__).parents[3]
    html = (root / "src/sheltersim/web/static/index.html").read_text(encoding="utf-8")
    css = (root / "src/sheltersim/web/static/style.css").read_text(encoding="utf-8")

    assert 'role="tablist"' in html
    assert 'data-admin-tab="overview"' in html
    assert 'data-admin-panel="decisions"' in html
    assert 'id="admin-panel-world" role="tabpanel" aria-labelledby="tab-world" hidden' in html
    assert 'id="admin-branch-snapshot"' in html
    assert 'class="card demo-card demo-only"' in html
    assert 'body[data-mode="admin"] .demo-only { display: none !important; }' in css
