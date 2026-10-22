from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from github_audit.models import IssueCommentPlan

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.mark.parametrize("label", ["Personal Access Token", "Organization"])
def test_configuration_change_drops_stale_pending_writes(label: str) -> None:
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    app.session_state["agent_pending_writes"] = [
        IssueCommentPlan(
            subject_id="I_1",
            repository="org/repo",
            item_type="issue",
            number=1,
            body="reviewed under old credentials",
        )
    ]
    app.run()
    assert app.session_state["agent_pending_writes"]
    next(widget for widget in app.text_input if widget.label == label).set_value("changed")
    with patch("github_audit.github_client.GitHubClient") as client:
        app.run()
    assert not app.exception
    assert app.session_state["agent_pending_writes"] == []
    assert app.session_state["findings"] is None
    client.assert_not_called()


@pytest.mark.parametrize("issues,prs", [(0, 0), (1, 0), (0, 1)])
def test_empty_findings_only_report_success_for_audited_items(issues: int, prs: int) -> None:
    app = AppTest.from_file(str(APP))
    app.session_state["rows"] = []
    app.session_state["stats"] = {"issues": issues, "prs": prs, "findings": 0}
    app.run()
    assert not app.exception
    if issues + prs:
        assert any("No findings" in message.value for message in app.success)
    else:
        assert not app.success
        assert any(
            "No issues or pull requests were audited" in message.value for message in app.info
        )


def test_scan_without_discovered_projects_reports_no_scope() -> None:
    app = AppTest.from_file(str(APP)).run()
    for widget in app.text_input:
        if widget.label == "Personal Access Token":
            widget.set_value("synthetic-token")
        elif widget.label == "Organization":
            widget.set_value("fixture")
    for checkbox in app.checkbox:
        if checkbox.label in {"All org projects", "All org repositories"}:
            checkbox.check()
        elif checkbox.label == "Branches":
            checkbox.uncheck()
    with (
        patch("github_audit.github_client.GitHubClient"),
        patch("github_audit.discovery.discover_repositories", return_value=["fixture/repo"]),
        patch("github_audit.project_fields.search_items", return_value=[]),
        patch("github_audit.discovery.discover_all", return_value=[]),
    ):
        next(button for button in app.button if button.label == "▶ Run Scan").click().run()
    assert not app.exception
    assert any("No projects discovered" in message.value for message in app.error), [
        message.value for message in app.error
    ]
    assert not any("No findings" in message.value for message in app.success)
    assert app.session_state["rows"] is None
