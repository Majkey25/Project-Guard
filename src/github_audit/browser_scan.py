from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from typing import cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from github_audit.config import split_required_fields
from github_audit.models import BrowserProjectFinding, BrowserScanResult


class BrowserSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    github_org: str = Field(default="OKsystem", validation_alias="GITHUB_ORG")
    github_project_number: int = Field(default=0, validation_alias="GITHUB_PROJECT_NUMBER")
    required_project_fields_raw: str = Field(
        default="Estimate,Iteration (sprint),Priority,Difficulty,Status",
        validation_alias="REQUIRED_PROJECT_FIELDS",
    )

    @property
    def required_project_fields(self) -> list[str]:
        return split_required_fields(self.required_project_fields_raw)


RAW_SCAN_SCRIPT = """
() => {
  const userLogin = document.querySelector("meta[name='user-login']")?.content || "";
  const rowNodes = Array.from(document.querySelectorAll("tr, [role='row']"));
  const rows = rowNodes
    .map((row) => Array.from(row.querySelectorAll(
      "th, td, [role='columnheader'], [role='gridcell']"
    ))
      .map((cell) => cell.innerText.trim()).filter(Boolean))
    .filter((cells) => cells.length > 1);
  const headers = rows.length ? rows[0] : [];
  const dataRows = rows.slice(1);
  return {
    url: location.href,
    title: document.title,
    signed_in: Boolean(userLogin),
    headers,
    rows: dataRows,
  };
}
"""

SCROLL_SCRIPT = """
() => {
  const scrollables = Array.from(document.querySelectorAll("*"))
    .filter((el) => el.scrollHeight > el.clientHeight)
    .sort((a, b) => b.scrollHeight - a.scrollHeight);
  const target = scrollables[0] || document.scrollingElement || document.documentElement;
  const before = target.scrollTop;
  target.scrollTop = before + target.clientHeight;
  return target.scrollTop !== before;
}
"""


def browser_start_url(settings: BrowserSettings, project_url: str | None) -> str:
    if project_url:
        return project_url
    if settings.github_project_number > 0:
        return (
            f"https://github.com/orgs/{settings.github_org}/projects/"
            f"{settings.github_project_number}/views/1"
        )
    return f"https://github.com/orgs/{settings.github_org}/projects?query=is%3Aopen"


def run_browser_scan(
    settings: BrowserSettings,
    *,
    project_url: str | None,
    wait_for_user: Callable[[str], str] = input,
) -> BrowserScanResult:
    start_url = browser_start_url(settings, project_url)
    with (
        tempfile.TemporaryDirectory(prefix="github-audit-browser-") as user_data_dir,
        sync_playwright() as playwright,
    ):
        context = None
        last_error: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel=channel,
                    headless=False,
                )
                break
            except PlaywrightError as exc:
                last_error = exc
        if context is None:
            msg = (
                "Could not launch Chrome/Edge/Chromium. "
                "Install browser support with: uv run python -m playwright install chromium"
            )
            raise RuntimeError(msg) from last_error
        # Close the browser on every path: an orphaned Chromium process keeps file
        # locks on user_data_dir and TemporaryDirectory cleanup fails on Windows.
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(start_url)
            wait_for_user("Sign in, open the GitHub Project table view, then press Enter here...")
            return collect_browser_scan(page, settings.required_project_fields)
        finally:
            context.close()


def collect_browser_scan(page: Page, required_fields: Sequence[str]) -> BrowserScanResult:
    raw_rows: list[list[str]] = []
    raw_headers: list[str] = []
    raw_url = ""
    raw_title = ""
    signed_in = False
    for _ in range(25):
        raw = cast(dict[str, object], page.evaluate(RAW_SCAN_SCRIPT))
        raw_url = as_str(raw.get("url"))
        raw_title = as_str(raw.get("title"))
        signed_in = bool(raw.get("signed_in"))
        raw_headers = as_str_list(raw.get("headers"))
        for row in as_rows(raw.get("rows")):
            if row not in raw_rows:
                raw_rows.append(row)
        moved = bool(page.evaluate(SCROLL_SCRIPT))
        if not moved:
            break
        page.wait_for_timeout(250)
    return extract_browser_findings(
        url=raw_url,
        title=raw_title,
        signed_in=signed_in,
        headers=raw_headers,
        rows=raw_rows,
        required_fields=required_fields,
    )


def extract_browser_findings(
    *,
    url: str,
    title: str,
    signed_in: bool,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    required_fields: Sequence[str],
) -> BrowserScanResult:
    header_indexes = {normalize(header): index for index, header in enumerate(headers)}
    required_indexes = {
        field: header_indexes[normalize(field)]
        for field in required_fields
        if normalize(field) in header_indexes
    }
    missing_headers = [field for field in required_fields if field not in required_indexes]
    findings: list[BrowserProjectFinding] = []
    for row in rows:
        missing = [
            field
            for field, index in required_indexes.items()
            if index >= len(row) or is_empty_cell(row[index])
        ]
        if missing:
            findings.append(
                BrowserProjectFinding(
                    row_label=first_label(row),
                    missing_fields=missing,
                    cells=list(row),
                )
            )
    limitations = [
        "Browser mode only sees rows and columns rendered in the GitHub web table.",
        "Use GraphQL token mode for complete paginated Project V2 data.",
    ]
    if not signed_in:
        limitations.append("Browser is not signed in to GitHub.")
    if missing_headers:
        limitations.append("Required columns not visible: " + ", ".join(missing_headers))
    return BrowserScanResult(
        url=url,
        title=title,
        signed_in=signed_in,
        headers=list(headers),
        findings=findings,
        missing_headers=missing_headers,
        limitations=limitations,
    )


def normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def is_empty_cell(value: str) -> bool:
    return normalize(value) in {"", "-", "—", "none", "no value", "empty"}


def first_label(row: Sequence[str]) -> str:
    for cell in row:
        if cell.strip():
            return cell.strip()
    return "unknown row"


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def as_rows(value: object) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    rows: list[list[str]] = []
    for item in cast(list[object], value):
        row = as_str_list(item)
        if row:
            rows.append(row)
    return rows
