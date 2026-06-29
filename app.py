from __future__ import annotations

import csv
import io
import re
import shlex
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
    try:
        parts = shlex.split(value)
        return parts[0] if parts else ""
    except ValueError:
        return value.strip()


def _csv(value: str) -> str:
    return ",".join(part.strip() for part in re.split(r"[\n,;]+", value) if part.strip())


def _project_numbers(value: str) -> str:
    numbers: list[str] = []
    for part in re.split(r"[\s,;]+", value.strip()):
        if not part:
            continue
        match = re.search(r"/projects/(\d+)", part) or re.fullmatch(r"#?(\d+)", part)
        if match:
            numbers.append(match.group(1))
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
_MAX_ENV_BYTES = 64 * 1024  # 64 KB
_ENV_PATH = Path(__file__).parent / ".env"


@st.cache_data(show_spinner=False)
def _load_env() -> dict[str, str]:
    d: dict[str, str] = {}
    path = _ENV_PATH
    if path.exists() and path.stat().st_size <= _MAX_ENV_BYTES:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = _env_value(v)
    return d


def _write_env_keys(updates: dict[str, str]) -> None:
    """Safely update or append keys in .env without touching unrelated lines."""
    for v in updates.values():
        if "\n" in v or "\r" in v:
            msg = "env value must not contain newlines"
            raise ValueError(msg)
    path = _ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k in updates:
                new_lines.append(f'{k}="{updates[k]}"')
                written.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in written:
            new_lines.append(f'{k}="{v}"')
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _load_env.clear()


E = _load_env()


def _bool(key: str, fallback: str = "false") -> bool:
    return E.get(key, fallback).strip().lower() == "true"


# ── session state bootstrap ───────────────────────────────────────────────────
session_defaults: dict[str, object | None] = {
    "rows": None,
    "findings": None,
    "error": None,
    "stats": None,
    "limitations": list[str](),
    "scan_time": None,
    "triage_result": None,
    "severity_map": None,
    "nl_filter": None,
    "ai_suggestion": None,
    "ai_explanation": None,
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
            placeholder="Paste GitHub token",
            help="Classic PAT · scopes: repo, read:org, read:project",
        )
        org = st.text_input(
            "Organization",
            value=E.get("GITHUB_ORG", ""),
            placeholder="my-company",
            help="GitHub organization slug (the part after github.com/). E.g. `my-company`.",
        )
        st.caption(
            "📋 **Project** = GitHub Project V2 planning board (sprints, custom fields). "
            "**Repository** = git repo where code lives. Configure each scope separately below."
        )
        include_all_projects = st.checkbox(
            "All org projects",
            value=_bool("GITHUB_INCLUDE_ALL_PROJECTS", "false"),
            help="When checked, the scanner fetches every open project in the org automatically. Uncheck to specify project numbers manually below.",
        )
        include_closed_projects = st.checkbox(
            "Include closed projects",
            value=_bool("GITHUB_INCLUDE_CLOSED_PROJECTS", "false"),
            disabled=not include_all_projects,
            help="Also scan projects whose state is 'closed'. Only relevant when 'All org projects' is enabled.",
        )
        project_numbers = st.text_area(
            "Project numbers or URLs",
            value=E.get("GITHUB_PROJECT_NUMBERS", E.get("GITHUB_PROJECT_NUMBER", "")),
            disabled=include_all_projects,
            placeholder="42\n123\nhttps://github.com/orgs/my-company/projects/5",
            help="Paste numbers or GitHub project URLs. Comma, semicolon, or newline separated.",
        )
        st.caption("Inputs stay in this browser session; the app does not write `.env`.")

    with st.expander("👥 Accounts", expanded=True):
        assignees = st.text_area(
            "Accounts to watch",
            value=E.get("TARGET_ASSIGNEES", ""),
            placeholder="alice\nbob\ncharlie",
            help=(
                "GitHub usernames, one per line or comma-separated. "
                "The scanner searches for issues and PRs assigned to these users. "
                "Leave empty if you only want to scan project board items."
            ),
        )
        inc_unassigned = st.checkbox(
            "Also search unassigned items",
            value=_bool("INCLUDE_UNASSIGNED", "false"),
            help=(
                "In addition to items assigned to the accounts above, also search for issues "
                "and PRs with no assignee at all. Useful to catch work that slipped through "
                "without an owner."
            ),
        )

    with st.expander("✅ Checks to flag", expanded=True):
        req_board = st.checkbox(
            "Items not on selected project board",
            value=_bool("REQUIRE_PROJECT_ITEM", "false"),
            help=(
                "Flag issues and PRs that are not linked to any of the scanned GitHub Project V2 boards. "
                "Turn this off if repo issues/PRs are allowed to exist outside the selected board."
            ),
        )
        require_fields = st.checkbox(
            "Missing required project fields",
            value=bool(_csv(E.get("REQUIRED_PROJECT_FIELDS", ""))),
            help="Flag items that are missing one or more of the required Project V2 custom fields listed below.",
        )
        required_fields = st.text_area(
            "Required Project fields",
            value=E.get("REQUIRED_PROJECT_FIELDS", ""),
            disabled=not require_fields,
            placeholder="Estimate\nIteration (sprint)\nPriority\nDifficulty\nStatus",
            help="Names of GitHub Project V2 custom fields that must be filled in. Use the exact field names as they appear in your project board. One per line or comma-separated.",
        )
        require_assignee = st.checkbox(
            "Unassigned items",
            value=_bool("REQUIRE_ASSIGNEE", "true"),
            help="Flag issues and PRs that have no assignee at all.",
        )
        require_target = st.checkbox(
            "Items not assigned to accounts to watch",
            value=_bool("REQUIRE_TARGET_ASSIGNEE", "true"),
            disabled=not bool(assignees.strip()),
            help="Flag items whose assignees are not in the 'Accounts to watch' list. Requires at least one account to be configured.",
        )
        require_dev = st.checkbox(
            "Missing development link",
            value=_bool("REQUIRE_DEVELOPMENT_LINK", "true"),
            help="Flag issues that have no linked pull request, and PRs that don't reference a closing issue. Uses GitHub's development link feature.",
        )
        require_pr_branch = st.checkbox(
            "Missing linked PR or branch specifically",
            value=_bool("REQUIRE_LINKED_PR_OR_BRANCH", "true"),
            help="Stricter than 'Missing development link': flags items that have no linked PR or branch reference anywhere (including in the PR body).",
        )

    with st.expander("📂 Scan Scope", expanded=False):
        inc_issues = st.checkbox(
            "Issues",
            value=_bool("INCLUDE_ISSUES", "true"),
            help="Include open GitHub Issues in the scan.",
        )
        inc_closed = st.checkbox(
            "Include closed issues",
            value=_bool("INCLUDE_CLOSED_ISSUES", "false"),
            disabled=not inc_issues,
            help="Also scan issues in the 'closed' state. By default only open issues are scanned.",
        )
        inc_prs = st.checkbox(
            "Pull Requests",
            value=_bool("INCLUDE_PULL_REQUESTS", "true"),
            help="Include open Pull Requests in the scan.",
        )
        inc_closed_prs = st.checkbox(
            "Include closed/merged PRs",
            value=_bool("INCLUDE_CLOSED_PULL_REQUESTS", "false"),
            disabled=not inc_prs,
            help="Also scan PRs that are closed or merged. By default only open PRs are scanned.",
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
            help="Filter items by when they were last updated on GitHub. 'All time' imposes no date filter. Use a range to focus on recent activity and speed up the scan.",
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
            help="Scan every repository in the organization. For large orgs this can be slow — use the allowlist below to limit scope.",
        )
        repo_allowlist = st.text_input(
            "Repository allowlist",
            value=E.get("GITHUB_REPOSITORY_ALLOWLIST", ""),
            disabled=inc_all_repos,
            placeholder="frontend,backend,api-service",
            help="Only scan these repositories. Enter repo names without the org prefix (e.g. `my-repo`), comma-separated. Leave empty to use all repos (requires 'All org repositories' to be checked).",
        )
        repo_denylist = st.text_input(
            "Repository denylist",
            value=E.get("GITHUB_REPOSITORY_DENYLIST", ""),
            placeholder="archive-repo,legacy-app",
            help="Always skip these repositories, even when 'All org repositories' is enabled. Enter repo names without the org prefix, comma-separated.",
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

    with st.expander("🧠 AI Assistant (LLM)", expanded=False):
        llm_provider = st.selectbox(
            "Provider",
            ["openai", "azure", "openai-compatible"],
            index=["openai", "azure", "openai-compatible"].index(E.get("LLM_PROVIDER", "openai"))
            if E.get("LLM_PROVIDER", "openai") in ["openai", "azure", "openai-compatible"]
            else 0,
            help="OpenAI (api.openai.com), Azure OpenAI, or any OpenAI-compatible endpoint.",
        )
        llm_api_key = st.text_input(
            "API Key",
            value=E.get("LLM_API_KEY", E.get("AZURE_API_KEY", "")),
            type="password",
            placeholder="Paste provider API key",
            help="Your LLM provider API key. Saved to .env on this machine only.",
        )
        llm_model = st.text_input(
            "Model name",
            value=E.get("LLM_MODEL_NAME", E.get("AZURE_LLM_MODEL_NAME", "")),
            placeholder="gpt-4o",
            help="Model ID as required by your provider (e.g. gpt-4o, gpt-4-turbo).",
        )
        llm_base_url = st.text_input(
            "Base URL (optional)",
            value=E.get("LLM_BASE_URL", E.get("AZURE_API_BASE", "")),
            placeholder="https://my-resource.openai.azure.com/",
            help="Required for Azure and openai-compatible providers. Leave empty for OpenAI.",
        )
        llm_api_version = st.text_input(
            "API Version (Azure only)",
            value=E.get("LLM_API_VERSION", E.get("AZURE_API_VERSION", "")),
            placeholder="2024-02-01",
            help="Azure API version string. Leave empty for non-Azure providers.",
        )
        if st.button("💾 Save LLM settings to .env", use_container_width=True):
            try:
                _write_env_keys({
                    "LLM_PROVIDER": llm_provider,
                    "LLM_API_KEY": llm_api_key,
                    "LLM_MODEL_NAME": llm_model,
                    "LLM_BASE_URL": llm_base_url,
                    "LLM_API_VERSION": llm_api_version,
                    "LLM_ENABLED": "true",
                })
                st.success("Saved. Reload the page to apply.")
            except (ValueError, OSError) as exc:
                st.error(f"Could not save: {exc}")
        llm_ready = bool(llm_api_key.strip() and llm_model.strip())
        if llm_ready:
            st.caption("✅ AI features enabled")
        else:
            st.caption("Enter API key and model to enable AI features.")

    st.divider()
    scan_btn = st.button("▶ Run Scan", type="primary", use_container_width=True)
    if st.session_state.scan_time:
        st.caption(f"Last scan: {st.session_state.scan_time}")
    if st.session_state.rows is not None and st.button("✕ Clear Results", use_container_width=True):
        for k in ("rows", "findings", "error", "stats", "scan_time",
                  "triage_result", "severity_map", "nl_filter", "ai_suggestion", "ai_explanation"):
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
                "required_project_fields_raw": _csv(required_fields) if require_fields else "",
                "include_issues": inc_issues,
                "include_pull_requests": inc_prs,
                "include_closed_issues": inc_closed,
                "include_closed_pull_requests": inc_closed_prs,
                "include_unassigned": inc_unassigned,
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
    except ValidationError as exc:
        msgs = "; ".join(e["msg"] for e in exc.errors())
        st.session_state.error = f"Configuration error: {msgs}"
        return
    except ValueError as exc:
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
    # key → (FindingRow, AuditFinding) — deduplicate across project scans,
    # keeping the version with the fewest missing fields (item may be on project A but not B)
    best: dict[tuple[str, str, int], tuple[FindingRow, object]] = {}
    for r in results:
        issues += r.scanned_issue_count
        prs += r.scanned_pull_request_count
        for f in r.findings:
            row: FindingRow = {
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
            key = (row["repository"], row["item_type"], row["number"])
            existing = best.get(key)
            if existing is None or len(f.missing_fields) < len(existing[0]["missing_fields"].split(",")):
                best[key] = (row, f)

    rows = [v[0] for v in best.values()]
    findings_by_key = {k: v[1] for k, v in best.items()}

    limitations = list({lim for r in results for lim in r.limitations})

    st.session_state.scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.session_state.rows = rows
    st.session_state.findings = findings_by_key
    st.session_state.error = None
    st.session_state.stats = {"issues": issues, "prs": prs, "findings": len(rows)}
    st.session_state.limitations = limitations
    # clear stale AI results from previous scan
    for _ai_key in ("triage_result", "severity_map", "nl_filter", "ai_suggestion", "ai_explanation"):
        st.session_state[_ai_key] = None


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
- **Project board membership** — optional check for items missing from the selected V2 board
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

# ── AI features ───────────────────────────────────────────────────────────────
if llm_ready:
    from github_audit.llm_evaluator import (
        batch_triage,
        explain_finding,
        nl_to_filters,
        score_severities,
        suggest_for_finding,
    )
    from github_audit.models import AuditFinding

    def _llm_settings() -> Settings:
        """Minimal Settings object for LLM calls — scan fields are placeholder-only."""
        return Settings.model_validate({
            "github_token": token or "x",
            "github_org": org or "x",
            "github_project_numbers_raw": "1",
            "github_include_all_repositories": True,
            "require_target_assignee": False,
            "llm_enabled": True,
            "llm_provider": llm_provider,
            "llm_api_key": llm_api_key,
            "llm_model_name": llm_model,
            "llm_base_url": llm_base_url,
            "llm_api_version": llm_api_version,
        })

    st.divider()
    st.subheader("🧠 AI Analysis")

    # ── NL filter ─────────────────────────────────────────────────────────────
    with st.expander("🗣️ Natural Language Filter", expanded=False):
        nl_query = st.text_input(
            "Describe what to find",
            placeholder="urgent PRs missing Priority with no assignee",
            label_visibility="collapsed",
            help="Describe the items you want to see. The AI maps your words to the filter controls.",
        )
        if st.button("Apply AI filter") and nl_query.strip():
            with st.spinner("Parsing…"):
                try:
                    result = nl_to_filters(
                        nl_query,
                        available_repos=all_repos,
                        available_assignees=all_assignees,
                        available_fields=all_missing,
                        settings=_llm_settings(),
                    )
                    st.session_state.nl_filter = {
                        "title_search": result.title_search,
                        "item_types": result.item_types,
                        "missing_fields": result.missing_fields,
                        "assignees": result.assignees,
                        "repositories": result.repositories,
                        "explanation": result.explanation,
                    }
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"AI filter failed: {exc}")

        if st.session_state.nl_filter:
            nf = st.session_state.nl_filter
            st.info(f"🔎 AI filter active: {nf['explanation']}")
            if st.button("Clear AI filter"):
                st.session_state.nl_filter = None
                st.rerun()

    # apply NL filter on top of UI filters
    if st.session_state.nl_filter:
        nf = st.session_state.nl_filter
        ai_filtered: list[FindingRow] = []
        for row in filtered:
            if nf["title_search"] and nf["title_search"].lower() not in row["title"].lower():
                continue
            if nf["item_types"] and row["item_type"] not in nf["item_types"]:
                continue
            if nf["missing_fields"] and not any(f in row["missing_fields"] for f in nf["missing_fields"]):
                continue
            if nf["assignees"] and not any(a in row["assignees"] for a in nf["assignees"]):
                continue
            if nf["repositories"] and row["repository"] not in nf["repositories"]:
                continue
            ai_filtered.append(row)
        if len(ai_filtered) != len(filtered):
            st.caption(f"AI filter narrowed to **{len(ai_filtered)}** of {len(filtered)} shown findings")
            filtered = ai_filtered

    # ── bulk AI actions ────────────────────────────────────────────────────────
    ai_c1, ai_c2 = st.columns(2)
    with ai_c1:
        if st.button("🔬 Triage all findings", use_container_width=True,
                     help="One AI call summarises root causes and top recommendations for all findings."):
            findings_list = list((st.session_state.findings or {}).values())
            if findings_list:
                with st.spinner("Analysing findings…"):
                    try:
                        st.session_state.triage_result = batch_triage(
                            cast(list[AuditFinding], findings_list), _llm_settings()
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Triage failed: {exc}")
    with ai_c2:
        if st.button("⚡ Score severity", use_container_width=True,
                     help="AI scores every finding HIGH/MEDIUM/LOW — adds a Severity column to the table."):
            findings_list = list((st.session_state.findings or {}).values())
            if findings_list:
                with st.spinner("Scoring…"):
                    try:
                        scores = score_severities(
                            cast(list[AuditFinding], findings_list), _llm_settings()
                        )
                        findings_keys = list((st.session_state.findings or {}).keys())
                        st.session_state.severity_map = {
                            findings_keys[i]: {"severity": s.severity, "reason": s.reason}
                            for i, s in enumerate(scores)
                            if i < len(findings_keys)
                        }
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Severity scoring failed: {exc}")

    # show triage result
    if st.session_state.triage_result:
        t = st.session_state.triage_result
        with st.expander("📊 AI Triage Report", expanded=True):
            st.markdown("**Root causes identified:**")
            for cause in t.root_causes:
                st.markdown(f"- {cause}")
            st.markdown(f"**Top priority action:** {t.top_priority_action}")
            st.markdown("**Recommendations:**")
            for rec in t.recommendations:
                st.markdown(f"- {rec}")
            st.info(f"💡 {t.team_process_insight}")

    # show severity in table if scored
    if st.session_state.severity_map:
        smap = cast(dict, st.session_state.severity_map)
        _sev_colour = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
        for row in filtered:
            key = (row["repository"], row["item_type"], row["number"])
            score = smap.get(key)
            if score:
                row["missing_fields"] = (
                    f"{_sev_colour.get(score['severity'], '')} {score['severity']} — {row['missing_fields']}"
                )

    # ── per-finding AI ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Per-finding AI**")

    findings_store = cast(dict, st.session_state.findings or {})
    finding_options = {
        f"#{row['number']} {row['title'][:55]} ({row['repository']})": (
            row["repository"], row["item_type"], row["number"]
        )
        for row in filtered[:100]  # cap selector at 100
    }

    if finding_options:
        sel_label = st.selectbox("Select finding", list(finding_options.keys()),
                                 label_visibility="collapsed")
        sel_key = finding_options[sel_label]
        sel_finding = findings_store.get(sel_key)

        pf_c1, pf_c2 = st.columns(2)
        with pf_c1:
            if st.button("✨ Suggest fixes", use_container_width=True,
                         help="AI suggests values for missing project fields."):
                if sel_finding:
                    with st.spinner("Generating suggestion…"):
                        try:
                            st.session_state.ai_suggestion = suggest_for_finding(
                                cast(AuditFinding, sel_finding), _llm_settings()
                            )
                            st.session_state.ai_explanation = None
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Suggestion failed: {exc}")
        with pf_c2:
            if st.button("❓ Explain this finding", use_container_width=True,
                         help="AI explains why this specific item was flagged and what to do."):
                if sel_finding:
                    with st.spinner("Generating explanation…"):
                        try:
                            finding_obj = cast(AuditFinding, sel_finding)
                            rule = ", ".join(finding_obj.missing_fields) or "unknown rule"
                            st.session_state.ai_explanation = explain_finding(
                                finding_obj, rule, _llm_settings()
                            )
                            st.session_state.ai_suggestion = None
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Explanation failed: {exc}")

        if st.session_state.ai_suggestion:
            s = st.session_state.ai_suggestion
            with st.expander("🧠 AI Suggestion", expanded=True):
                cols = st.columns(3)
                cols[0].metric("Estimate", s.estimated_points or "—")
                cols[1].metric("Priority", s.priority or "—")
                cols[2].metric("Confidence", f"{s.confidence:.0%}")
                if s.difficulty:
                    st.markdown(f"**Difficulty:** {s.difficulty}")
                if s.suggested_iteration:
                    st.markdown(f"**Iteration:** {s.suggested_iteration}")
                st.markdown(f"**Rationale:** {s.rationale}")

        if st.session_state.ai_explanation:
            e = st.session_state.ai_explanation
            with st.expander("💡 AI Explanation", expanded=True):
                st.markdown(f"**Why it matters:** {e.explanation}")
                st.markdown(f"**Impact:** {e.impact}")
                st.markdown(f"**Suggested fix:** {e.suggested_fix}")

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
