from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from github_audit.config import Settings
from github_audit.discovery import discover_all
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.scanner import scan_all

st.set_page_config(page_title="GitHub Audit", page_icon="🔍", layout="wide")

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
                d[k.strip()] = v.strip()
    return d

E = _load_env()

def _bool(key: str, fallback: str = "false") -> bool:
    return E.get(key, fallback).lower() == "true"

# ── session state bootstrap ───────────────────────────────────────────────────
for _k, _v in [
    ("rows", None), ("error", None), ("stats", None),
    ("limitations", []), ("scan_time", None),
]:
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
        project_numbers = st.text_input(
            "Project numbers",
            value=E.get("GITHUB_PROJECT_NUMBERS", E.get("GITHUB_PROJECT_NUMBER", "")),
            help="Comma-separated, e.g. 48,32,55. Remove numbers to narrow the scan.",
        )

    with st.expander("👥 Assignees", expanded=True):
        assignees = st.text_input(
            "Target assignees",
            value=E.get("TARGET_ASSIGNEES", ""),
            help="Comma-separated GitHub usernames. Leave empty to search all.",
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
        required_fields = st.text_input(
            "Fields every item must have set",
            value=E.get(
                "REQUIRED_PROJECT_FIELDS",
                "Estimate,Iteration (sprint),Priority,Difficulty,Status",
            ),
            help="Comma-separated. One finding per missing field per item.",
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

    st.divider()
    scan_btn = st.button("▶ Run Scan", type="primary", use_container_width=True)
    if st.session_state.scan_time:
        st.caption(f"Last scan: {st.session_state.scan_time}")
    if st.session_state.rows is not None and st.button("✕ Clear Results", use_container_width=True):
        for k in ("rows", "error", "stats", "scan_time"):
            st.session_state[k] = None
        st.session_state.limitations = []
        st.rerun()


# ── scan logic ────────────────────────────────────────────────────────────────
def _run_scan() -> None:
    try:
        settings = Settings.model_validate({
            "github_token": token,
            "github_org": org,
            "github_project_numbers_raw": project_numbers,
            "github_project_number": 0,
            "target_assignees_raw": assignees,
            "required_project_fields_raw": required_fields,
            "include_issues": inc_issues,
            "include_pull_requests": inc_prs,
            "include_closed_issues": inc_closed,
            "require_development_link": require_dev,
            "require_linked_pr_or_branch": require_pr_branch,
            "require_project_item": req_board,
            "require_assignee": require_assignee,
            # silence the validator when no assignees are entered
            "require_target_assignee": require_target and bool(assignees.strip()),
            "github_include_all_repositories": inc_all_repos,
            "github_repository_allowlist_raw": "" if inc_all_repos else repo_allowlist,
        })
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

    rows: list[dict[str, object]] = []
    issues = prs = 0
    for r in results:
        issues += r.scanned_issue_count
        prs += r.scanned_pull_request_count
        for f in r.findings:
            rows.append({
                "Project": f.project_number or "",
                "Project Title": f.project_title or "",
                "Repository": f.repository.split("/")[-1],
                "Type": "PR" if f.item_type == "pull_request" else "Issue",
                "#": f.number,
                "Title": f.title,
                "Assignees": ", ".join(f.assignees) if f.assignees else "(none)",
                "Missing Fields": ", ".join(f.missing_fields),
                "URL": f.url,
            })

    limitations = list({lim for r in results for lim in r.limitations})

    st.session_state.scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.session_state.rows = rows
    st.session_state.error = None
    st.session_state.stats = {"issues": issues, "prs": prs, "findings": len(rows)}
    st.session_state.limitations = limitations


if scan_btn:
    if not token.strip():
        st.session_state.error = "GitHub token is required."
    elif not org.strip():
        st.session_state.error = "Organization name is required."
    elif not project_numbers.strip():
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

rows = st.session_state.rows
stats = st.session_state.stats

# ── summary metrics ───────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("Issues scanned", stats["issues"])
c2.metric("PRs scanned", stats["prs"])
c3.metric("Findings", stats["findings"])

if not rows:
    st.success("✅ No findings — everything looks good!")
    st.stop()

df = pd.DataFrame(rows)

# ── filters ───────────────────────────────────────────────────────────────────
st.subheader("Filters")

title_search = st.text_input(
    "title_search", placeholder="🔍 Search by title keyword…", label_visibility="collapsed"
)

fc1, fc2, fc3, fc4, fc5 = st.columns(5)

all_repos = sorted(df["Repository"].unique().tolist())
all_missing = sorted({
    f.strip()
    for cell in df["Missing Fields"].tolist()
    for f in cell.split(",")
    if f.strip()
})
all_assignees = sorted({
    a.strip()
    for cell in df["Assignees"].tolist()
    for a in cell.split(",")
    if a.strip() and a.strip() != "(none)"
})
all_types = sorted(df["Type"].unique().tolist())

# Project filter: "48 - Sprint Board" labels built from already-fetched data
proj_labels: dict[object, str] = {}
for r in rows:
    p = r["Project"]
    if p and p not in proj_labels:
        title = str(r["Project Title"])
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
mask = pd.Series([True] * len(df), dtype=bool)
if title_search:
    mask &= df["Title"].str.contains(title_search, case=False, na=False)
if sel_repos:
    mask &= df["Repository"].isin(sel_repos)
if sel_missing:
    mask &= df["Missing Fields"].apply(lambda m: any(f in m for f in sel_missing))
if sel_assignees:
    mask &= df["Assignees"].apply(lambda a: any(x in a for x in sel_assignees))
if sel_types:
    mask &= df["Type"].isin(sel_types)
if sel_proj_nums:
    mask &= df["Project"].isin(sel_proj_nums)

filtered = df[mask].reset_index(drop=True)
st.caption(f"Showing **{len(filtered)}** of {len(df)} findings")

# ── results table ─────────────────────────────────────────────────────────────
display_cols = ["Project", "Repository", "Type", "#", "Title", "Assignees", "Missing Fields", "URL"]
st.dataframe(
    filtered[display_cols],
    use_container_width=True,
    hide_index=True,
    height=min(600, 100 + len(filtered) * 35),
    column_config={
        "URL": st.column_config.LinkColumn("Link", display_text="Open ↗"),
        "#": st.column_config.NumberColumn("#", format="%d", width="small"),
        "Project": st.column_config.NumberColumn("Project", format="%d", width="small"),
        "Type": st.column_config.TextColumn("Type", width="small"),
    },
)

# ── download ──────────────────────────────────────────────────────────────────
st.download_button(
    "⬇️ Download filtered CSV",
    filtered.to_csv(index=False).encode("utf-8"),
    "findings.csv",
    "text/csv",
)

# ── missing field breakdown ───────────────────────────────────────────────────
with st.expander("📊 Missing field breakdown (all findings)"):
    field_counts = (
        pd.Series([
            f.strip()
            for cell in df["Missing Fields"]
            for f in cell.split(",")
            if f.strip()
        ])
        .value_counts()
        .sort_values()
    )
    st.bar_chart(field_counts, horizontal=True)

# ── limitations ───────────────────────────────────────────────────────────────
if st.session_state.limitations:
    with st.expander(f"⚠️ {len(st.session_state.limitations)} scan limitation(s)"):
        for lim in st.session_state.limitations:
            st.warning(lim, icon="⚠️")
