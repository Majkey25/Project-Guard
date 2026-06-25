from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import streamlit as st
from pydantic import ValidationError

from github_audit.config import Settings
from github_audit.discovery import discover_all
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.scanner import scan_all

st.set_page_config(page_title="GitHub Audit", page_icon="🔍", layout="wide")


class FindingRow(TypedDict):
    project: int
    project_title: str
    repository: str
    item_type: str
    number: int
    updated_at: str
    title: str
    assignees: str
    missing_fields: str
    url: str


class ScanStats(TypedDict):
    issues: int
    prs: int
    findings: int


def _env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _csv(value: str) -> str:
    return ",".join(part.strip() for part in re.split(r"[\n,;]+", value) if part.strip())


def _project_numbers(value: str) -> str:
    numbers: list[str] = []
    for part in re.split(r"[\s,;]+", value.strip()):
        if not part:
            continue
        match = re.search(r"/projects/(\d+)", part) or re.fullmatch(r"#?(\d+)", part)
        numbers.append(match.group(1) if match else part)
    return ",".join(dict.fromkeys(numbers))


def _date_env(value: str) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _date_from_widget(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    msg = "date_input returned unexpected value"
    raise TypeError(msg)


def _date_label(value: str | None) -> str:
    return value[:10] if value else ""


def _csv_bytes(rows: list[FindingRow]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(FindingRow.__annotations__))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


# ── load .env defaults (read once, cached) ────────────────────────────────────
@st.cache_data(show_spinner=False)
def _load_env() -> dict[str, str]:
    d: dict[str, str] = {}
    path = Path(".env")
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = _env_value(v)
    return d


E = _load_env()


def _bool(key: str, fallback: str = "false") -> bool:
    return E.get(key, fallback).strip().lower() == "true"


# ── session state bootstrap ───────────────────────────────────────────────────
session_defaults: dict[str, object | None] = {
    "rows": None,
    "error": None,
    "stats": None,
    "limitations": list[str](),
    "scan_time": None,
}
for _k, _v in session_defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Configuration")

    with st.expander("🔑 GitHub Connection", expanded=True):
        token = st.text_input(
            "Personal Access Token",
            value=E.get("GITHUB_TOKEN", ""),
            type="password",
            help="Classic PAT · scopes: repo, read:org, read:project",
        )
        org = st.text_input("Organization", value=E.get("GITHUB_ORG", ""))
        include_all_projects = st.checkbox(
            "All org projects",
            value=_bool("GITHUB_INCLUDE_ALL_PROJECTS", "false"),
            help="Fetch project numbers from GitHub at scan time.",
        )
        include_closed_projects = st.checkbox(
            "Include closed projects",
            value=_bool("GITHUB_INCLUDE_CLOSED_PROJECTS", "false"),
            disabled=not include_all_projects,
        )
        project_numbers = st.text_area(
            "Project numbers or URLs",
            value=E.get("GITHUB_PROJECT_NUMBERS", E.get("GITHUB_PROJECT_NUMBER", "")),
            disabled=include_all_projects,
            help="Paste numbers or GitHub project URLs. Comma, semicolon, or newline separated.",
        )
        st.caption("Inputs stay in this browser session; the app does not write `.env`.")

    with st.expander("👥 Assignees", expanded=True):
        assignees = st.text_area(
            "Accounts to watch",
            value=E.get("TARGET_ASSIGNEES", ""),
            help=(
                "GitHub usernames, one per line or comma-separated. Empty means project-board "
                "items only; it does not search every unassigned issue in every repo."
            ),
        )
        require_target = st.checkbox(
            "Flag items not assigned to a target assignee",
            value=_bool("REQUIRE_TARGET_ASSIGNEE", "true"),
        )
        require_assignee = st.checkbox(
            "Flag unassigned items",
            value=_bool("REQUIRE_ASSIGNEE", "true"),
        )

    with st.expander("📋 Required Fields", expanded=True):
        required_fields = st.text_area(
            "Fields every item must have set",
            value=E.get(
                "REQUIRED_PROJECT_FIELDS",
                "Estimate,Iteration (sprint),Priority,Difficulty,Status",
            ),
            help="One per line or comma-separated.",
        )

    with st.expander("🔗 Development Links", expanded=False):
        require_dev = st.checkbox(
            "Require a development link (PR or branch)",
            value=_bool("REQUIRE_DEVELOPMENT_LINK", "true"),
        )
        require_pr_branch = st.checkbox(
            "Require a linked PR or branch specifically",
            value=_bool("REQUIRE_LINKED_PR_OR_BRANCH", "true"),
        )

    with st.expander("📂 Scan Scope", expanded=False):
        inc_issues = st.checkbox("Issues", value=_bool("INCLUDE_ISSUES", "true"))
        inc_prs = st.checkbox("Pull Requests", value=_bool("INCLUDE_PULL_REQUESTS", "true"))
        inc_closed = st.checkbox(
            "Include closed issues", value=_bool("INCLUDE_CLOSED_ISSUES", "false")
        )
        req_board = st.checkbox(
            "Must be on project board",
            value=_bool("REQUIRE_PROJECT_ITEM", "true"),
            help="Flag items not added to the project V2 board",
        )

    today = date.today()
    env_updated_from = _date_env(E.get("GITHUB_UPDATED_FROM", ""))
    env_updated_to = _date_env(E.get("GITHUB_UPDATED_TO", ""))
    updated_from: date | None = None
    updated_to: date | None = None
    with st.expander("🕒 Time Range", expanded=True):
        time_mode = st.selectbox(
            "Updated items",
            ("All time", "Last 30 days", "Custom range"),
            index=2 if env_updated_from or env_updated_to else 0,
            help="Filters issues and PRs by GitHub updated date.",
        )
        if time_mode == "Last 30 days":
            updated_from = today - timedelta(days=30)
            updated_to = today
            st.caption(f"{updated_from.isoformat()} → {updated_to.isoformat()}")
        elif time_mode == "Custom range":
            d1, d2 = st.columns(2)
            updated_from = _date_from_widget(
                d1.date_input("From", value=env_updated_from or today - timedelta(days=30))
            )
            updated_to = _date_from_widget(d2.date_input("To", value=env_updated_to or today))

    with st.expander("🗂️ Repository Scope", expanded=False):
        inc_all_repos = st.checkbox(
            "All org repositories",
            value=_bool("GITHUB_INCLUDE_ALL_REPOSITORIES", "false"),
        )
        repo_allowlist = st.text_input(
            "Repository allowlist",
            value=E.get("GITHUB_REPOSITORY_ALLOWLIST", ""),
            disabled=inc_all_repos,
            help="Comma-separated repo names without org prefix.",
        )
        repo_denylist = st.text_input(
            "Repository denylist",
            value=E.get("GITHUB_REPOSITORY_DENYLIST", ""),
            help="Repo names without org prefix to skip.",
        )

    project_scope = (
        "all org projects" if include_all_projects else _project_numbers(project_numbers)
    )
    repo_scope = "all org repositories" if inc_all_repos else _csv(repo_allowlist)
    time_scope = (
        "all time"
        if updated_from is None and updated_to is None
        else f"{updated_from.isoformat() if updated_from else 'start'} → "
        f"{updated_to.isoformat() if updated_to else 'today'}"
    )
    st.caption(f"Scope: projects `{project_scope or 'none'}` · repos `{repo_scope or 'none'}`")
    st.caption(f"Updated: `{time_scope}`")

    st.divider()
    scan_btn = st.button("▶ Run Scan", type="primary", use_container_width=True)
    if st.session_state.scan_time:
        st.caption(f"Last scan: {st.session_state.scan_time}")
    if st.session_state.rows is not None and st.button("✕ Clear Results", use_container_width=True):
        for k in ("rows", "error", "stats", "scan_time"):
            st.session_state[k] = None
        st.session_state.limitations = list[str]()
        st.rerun()


# ── scan logic ────────────────────────────────────────────────────────────────
def _run_scan() -> None:
    try:
        settings = Settings.model_validate(
            {
                "github_token": token,
                "github_org": org,
                "github_project_numbers_raw": ""
                if include_all_projects
                else _project_numbers(project_numbers),
                "github_project_number": 0,
                "github_include_all_projects": include_all_projects,
                "github_include_closed_projects": include_closed_projects,
                "target_assignees_raw": _csv(assignees),
                "required_project_fields_raw": _csv(required_fields),
                "include_issues": inc_issues,
                "include_pull_requests": inc_prs,
                "include_closed_issues": inc_closed,
                "github_updated_from": updated_from,
                "github_updated_to": updated_to,
                "require_development_link": require_dev,
                "require_linked_pr_or_branch": require_pr_branch,
                "require_project_item": req_board,
                "require_assignee": require_assignee,
                # silence the validator when no assignees are entered
                "require_target_assignee": require_target and bool(assignees.strip()),
                "github_include_all_repositories": inc_all_repos,
                "github_repository_allowlist_raw": "" if inc_all_repos else _csv(repo_allowlist),
                "github_repository_denylist_raw": _csv(repo_denylist),
            }
        )
    except (ValidationError, ValueError) as exc:
        st.session_state.error = str(exc)
        return

    try:
        with GitHubClient(settings.github_token) as client:
            discoveries = discover_all(client, settings)
            results = scan_all(client, settings, discoveries)
    except GitHubError as exc:
        st.session_state.error = str(exc)
        return

    rows: list[FindingRow] = []
    issues = prs = 0
    for r in results:
        issues += r.scanned_issue_count
        prs += r.scanned_pull_request_count
        for f in r.findings:
            rows.append(
                {
                    "project": f.project_number or 0,
                    "project_title": f.project_title or "",
                    "repository": f.repository.split("/")[-1],
                    "item_type": "PR" if f.item_type == "pull_request" else "Issue",
                    "number": f.number,
                    "updated_at": _date_label(f.updated_at),
                    "title": f.title,
                    "assignees": ", ".join(f.assignees) if f.assignees else "(none)",
                    "missing_fields": ", ".join(f.missing_fields),
                    "url": f.url,
                }
            )

    limitations = list({lim for r in results for lim in r.limitations})

    st.session_state.scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.session_state.rows = rows
    st.session_state.error = None
    st.session_state.stats = {"issues": issues, "prs": prs, "findings": len(rows)}
    st.session_state.limitations = limitations


if scan_btn:
    for key in ("rows", "stats", "scan_time"):
        st.session_state[key] = None
    st.session_state.limitations = list[str]()
    if not token.strip():
        st.session_state.error = "GitHub token is required."
    elif not org.strip():
        st.session_state.error = "Organization name is required."
    elif not include_all_projects and not project_numbers.strip():
        st.session_state.error = "At least one project number is required."
    elif not inc_all_repos and not repo_allowlist.strip():
        st.session_state.error = (
            "Either enable 'All org repositories' or enter a repository allowlist."
        )
    elif not inc_issues and not inc_prs:
        st.session_state.error = "Enable at least one of Issues or Pull Requests."
    else:
        st.session_state.error = None
        with st.spinner("Connecting to GitHub and scanning — this may take a minute…"):
            _run_scan()


# ── main content ──────────────────────────────────────────────────────────────
st.title("🔍 GitHub Audit")

if st.session_state.error:
    st.error(st.session_state.error)

if st.session_state.rows is None:
    st.info(
        "Configure the settings in the sidebar, then click **▶ Run Scan** to audit "
        "your GitHub Projects for missing fields and workflow gaps."
    )
    with st.expander("What does this tool check?"):
        st.markdown("""
- **Required fields** — Estimate, Priority, Iteration (sprint), Difficulty, Status (configurable)
- **Assignees** — whether items are assigned, and to your chosen target users
- **Development links** — whether issues have a linked PR or branch
- **Project board membership** — whether items are actually tracked on the V2 board
        """)
    st.stop()

rows = cast(list[FindingRow], st.session_state.rows)
stats = cast(ScanStats, st.session_state.stats)

# ── summary metrics ───────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("Issues scanned", stats["issues"])
c2.metric("PRs scanned", stats["prs"])
c3.metric("Findings", stats["findings"])

if not rows:
    st.success("✅ No findings — everything looks good!")
    st.stop()

# ── filters ───────────────────────────────────────────────────────────────────
st.subheader("Filters")

title_search = st.text_input(
    "title_search", placeholder="🔍 Search by title keyword…", label_visibility="collapsed"
)

fc1, fc2, fc3, fc4, fc5 = st.columns(5)

all_repos = sorted({row["repository"] for row in rows})
all_missing = sorted(
    {field.strip() for row in rows for field in row["missing_fields"].split(",") if field.strip()}
)
all_assignees = sorted(
    {
        assignee.strip()
        for row in rows
        for assignee in row["assignees"].split(",")
        if assignee.strip() and assignee.strip() != "(none)"
    }
)
all_types = sorted({row["item_type"] for row in rows})

# Project filter: "48 - Sprint Board" labels built from already-fetched data
proj_labels: dict[int, str] = {}
for row in rows:
    p = row["project"]
    if p and p not in proj_labels:
        title = row["project_title"]
        proj_labels[p] = f"{p} - {title}" if title else str(p)
all_proj_options = [proj_labels[p] for p in sorted(proj_labels)]

with fc1:
    sel_repos = st.multiselect("Repository", all_repos)
with fc2:
    sel_missing = st.multiselect("Missing field", all_missing)
with fc3:
    sel_assignees = st.multiselect("Assignee", all_assignees)
with fc4:
    sel_types = st.multiselect("Type", all_types)
with fc5:
    sel_proj_labels = st.multiselect("Project", all_proj_options)

sel_proj_nums = {p for p, label in proj_labels.items() if label in sel_proj_labels}

# apply all filters
filtered: list[FindingRow] = []
for row in rows:
    if title_search and title_search.lower() not in row["title"].lower():
        continue
    if sel_repos and row["repository"] not in sel_repos:
        continue
    if sel_missing and not any(field in row["missing_fields"] for field in sel_missing):
        continue
    if sel_assignees and not any(assignee in row["assignees"] for assignee in sel_assignees):
        continue
    if sel_types and row["item_type"] not in sel_types:
        continue
    if sel_proj_nums and row["project"] not in sel_proj_nums:
        continue
    filtered.append(row)

st.caption(f"Showing **{len(filtered)}** of {len(rows)} findings")

# ── results table ─────────────────────────────────────────────────────────────
display_rows = [
    {
        "Project": row["project"],
        "Repository": row["repository"],
        "Type": row["item_type"],
        "#": row["number"],
        "Missing": row["missing_fields"],
        "Updated": row["updated_at"],
        "Title": row["title"],
        "Assignees": row["assignees"],
        "URL": row["url"],
    }
    for row in filtered
]
st.dataframe(
    display_rows,
    use_container_width=True,
    hide_index=True,
    height=min(600, 100 + len(filtered) * 35),
    column_config={
        "URL": st.column_config.LinkColumn("Link", display_text="Open ↗"),
        "#": st.column_config.NumberColumn("#", format="%d", width="small"),
        "Project": st.column_config.NumberColumn("Project", format="%d", width="small"),
        "Type": st.column_config.TextColumn("Type", width="small"),
        "Missing": st.column_config.TextColumn("Missing", width="large"),
        "Updated": st.column_config.TextColumn("Updated", width="small"),
    },
)

# ── download ──────────────────────────────────────────────────────────────────
st.download_button(
    "⬇️ Download filtered CSV",
    _csv_bytes(filtered),
    "findings.csv",
    "text/csv",
)

# ── missing field breakdown ───────────────────────────────────────────────────
with st.expander("📊 Missing field breakdown (all findings)"):
    field_counts: dict[str, int] = {}
    for row in rows:
        for field in row["missing_fields"].split(","):
            field = field.strip()
            if field:
                field_counts[field] = field_counts.get(field, 0) + 1
    chart_rows = [
        {"Field": field, "Count": count}
        for field, count in sorted(field_counts.items(), key=lambda item: item[1])
    ]
    st.bar_chart(chart_rows, x="Field", y="Count", horizontal=True)

# ── limitations ───────────────────────────────────────────────────────────────
limitations = cast(list[str], st.session_state.limitations)
if limitations:
    with st.expander(f"⚠️ {len(limitations)} scan limitation(s)"):
        for lim in limitations:
            st.warning(lim, icon="⚠️")
