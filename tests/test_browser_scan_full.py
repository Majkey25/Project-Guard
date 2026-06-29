from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from github_audit.browser_scan import BrowserSettings, collect_browser_scan, run_browser_scan
from github_audit.models import BrowserScanResult


def _page_mock(
    *,
    url: str = "https://github.com/orgs/OKsystem/projects/1",
    title: str = "Project",
    signed_in: bool = True,
    headers: list[str] | None = None,
    rows: list[list[str]] | None = None,
    scroll_moves: bool = False,
) -> MagicMock:
    page = MagicMock()
    scan_result = {
        "url": url,
        "title": title,
        "signed_in": signed_in,
        "headers": headers if headers is not None else ["Title", "Estimate", "Priority"],
        "rows": rows if rows is not None else [["Issue 1", "3", "P1"]],
    }
    page.evaluate.side_effect = [scan_result, scroll_moves]
    return page


def _browser_settings() -> BrowserSettings:
    return BrowserSettings.model_validate({"github_org": "OKsystem", "github_project_number": 1})


def _scan_result() -> BrowserScanResult:
    return BrowserScanResult(
        url="u",
        title="t",
        signed_in=True,
        headers=[],
        findings=[],
        missing_headers=[],
    )


# ── collect_browser_scan ──────────────────────────────────────────────────────


def test_collect_returns_signed_in_state() -> None:
    page = _page_mock(signed_in=True)
    result = collect_browser_scan(page, ["Estimate", "Priority"])  # type: ignore[arg-type]
    assert result.signed_in is True


def test_collect_detects_missing_field() -> None:
    page = _page_mock(rows=[["Issue 1", "", "P1"]])
    result = collect_browser_scan(page, ["Estimate", "Priority"])  # type: ignore[arg-type]
    assert len(result.findings) == 1
    assert "Estimate" in result.findings[0].missing_fields


def test_collect_no_findings_when_all_fields_present() -> None:
    page = _page_mock(rows=[["Issue 1", "3", "P1"]])
    result = collect_browser_scan(page, ["Estimate", "Priority"])  # type: ignore[arg-type]
    assert result.findings == []


def test_collect_records_url_and_title() -> None:
    page = _page_mock(url="https://github.com/orgs/OKsystem/projects/42", title="Sprint Board")
    result = collect_browser_scan(page, [])  # type: ignore[arg-type]
    assert result.url == "https://github.com/orgs/OKsystem/projects/42"
    assert result.title == "Sprint Board"


def test_collect_reports_missing_column_as_limitation() -> None:
    page = _page_mock(headers=["Title"], rows=[])
    result = collect_browser_scan(page, ["Estimate"])  # type: ignore[arg-type]
    assert "Estimate" in result.missing_headers
    assert any("Estimate" in lim for lim in result.limitations)


def test_collect_deduplicates_rows_across_scrolls() -> None:
    page = MagicMock()
    row = ["Issue 1", "3", "P1"]
    page.evaluate.side_effect = [
        {
            "url": "u",
            "title": "t",
            "signed_in": True,
            "headers": ["Title", "Estimate", "Priority"],
            "rows": [row],
        },
        True,
        {
            "url": "u",
            "title": "t",
            "signed_in": True,
            "headers": ["Title", "Estimate", "Priority"],
            "rows": [row],
        },
        False,
    ]
    result = collect_browser_scan(page, [])  # type: ignore[arg-type]
    assert len(result.headers) == 3


def test_collect_limitation_when_not_signed_in() -> None:
    page = _page_mock(signed_in=False)
    result = collect_browser_scan(page, [])  # type: ignore[arg-type]
    assert any("not signed in" in lim.lower() for lim in result.limitations)


# ── run_browser_scan ──────────────────────────────────────────────────────────


def test_run_browser_scan_returns_result() -> None:
    expected = _scan_result()
    context = MagicMock()
    context.pages = [MagicMock()]
    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = context

    with (
        patch("github_audit.browser_scan.sync_playwright") as mock_playwright,
        patch("github_audit.browser_scan.tempfile.TemporaryDirectory") as mock_tmp,
        patch("github_audit.browser_scan.collect_browser_scan", return_value=expected),
    ):
        mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
        mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
        mock_playwright.return_value.__enter__ = MagicMock(return_value=pw)
        mock_playwright.return_value.__exit__ = MagicMock(return_value=False)

        result = run_browser_scan(_browser_settings(), project_url=None, wait_for_user=lambda _: "")

    assert result is expected


def test_run_browser_scan_tries_channels_in_order() -> None:
    from playwright.sync_api import Error as PlaywrightError

    expected = _scan_result()
    context = MagicMock()
    context.pages = [MagicMock()]
    pw = MagicMock()
    call_log: list[str | None] = []

    def launch_side_effect(
        *_args: object,
        channel: str | None = None,
        **_kwargs: object,
    ) -> MagicMock:
        call_log.append(channel)
        if channel == "chrome":
            raise PlaywrightError("chrome not installed")
        return context

    pw.chromium.launch_persistent_context.side_effect = launch_side_effect

    with (
        patch("github_audit.browser_scan.sync_playwright") as mock_playwright,
        patch("github_audit.browser_scan.tempfile.TemporaryDirectory") as mock_tmp,
        patch("github_audit.browser_scan.collect_browser_scan", return_value=expected),
    ):
        mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
        mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
        mock_playwright.return_value.__enter__ = MagicMock(return_value=pw)
        mock_playwright.return_value.__exit__ = MagicMock(return_value=False)

        run_browser_scan(_browser_settings(), project_url=None, wait_for_user=lambda _: "")

    assert call_log[0] == "chrome"
    assert call_log[1] == "msedge"


def test_run_browser_scan_raises_when_no_browser() -> None:
    from playwright.sync_api import Error as PlaywrightError

    pw = MagicMock()
    pw.chromium.launch_persistent_context.side_effect = PlaywrightError("not installed")

    with (
        patch("github_audit.browser_scan.sync_playwright") as mock_playwright,
        patch("github_audit.browser_scan.tempfile.TemporaryDirectory") as mock_tmp,
    ):
        mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
        mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
        mock_playwright.return_value.__enter__ = MagicMock(return_value=pw)
        mock_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(RuntimeError, match="Could not launch"):
            run_browser_scan(_browser_settings(), project_url=None, wait_for_user=lambda _: "")


def test_run_browser_scan_uses_new_page_when_no_pages() -> None:
    expected = _scan_result()
    context = MagicMock()
    context.pages = []
    context.new_page.return_value = MagicMock()
    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = context

    with (
        patch("github_audit.browser_scan.sync_playwright") as mock_playwright,
        patch("github_audit.browser_scan.tempfile.TemporaryDirectory") as mock_tmp,
        patch("github_audit.browser_scan.collect_browser_scan", return_value=expected),
    ):
        mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
        mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
        mock_playwright.return_value.__enter__ = MagicMock(return_value=pw)
        mock_playwright.return_value.__exit__ = MagicMock(return_value=False)

        run_browser_scan(_browser_settings(), project_url=None, wait_for_user=lambda _: "")

    context.new_page.assert_called_once()
