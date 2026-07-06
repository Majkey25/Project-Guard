from __future__ import annotations

import io
import re
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import streamlit as st
import xlsxwriter  # pyright: ignore[reportMissingTypeStubs]  # typed inline, py.typed missing
from pydantic import ValidationError

from github_audit.agent_chat import parse_agent_command, should_apply_now, summarize_findings
from github_audit.applier import (
    PartialApplyError,
    apply_pending_write,
    describe_pending_write,
    resolve_created_item_ids,
)
from github_audit.config import Settings
from github_audit.discovery import discover_all, discover_repositories
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.models import (
    AddToProjectPlan,
    ApplyPlan,
    AuditFinding,
    PendingWrite,
    ProjectFieldDefinition,
)
from github_audit.project_fields import (
    fetch_assignable_users,
    fetch_repo_labels,
    fetch_repo_milestones,
    search_items,
)
from github_audit.scanner import scan_all

st.set_page_config(page_title="GitHub Audit", page_icon="🔍", layout="wide")

st.html("""<style>
[data-testid="stPopover"] > div[data-testid="stPopoverBody"] {
    width: 600px !important;
    max-height: 80vh !important;
    overflow-y: auto !important;
}
</style>""")


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


type ScanStats = dict[str, int]


class AgentMessage(TypedDict):
    role: str
    content: str


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


def _date_from_widget(value: object) -> date | None:
    # st.date_input returns None when the user clears the field
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    msg = "date_input returned unexpected value"
    raise TypeError(msg)


def _date_label(value: str | None) -> str:
    return value[:10] if value else ""


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_XLSX_HEADERS = {
    "project": "Project",
    "project_title": "Project title",
    "repository": "Repository",
    "item_type": "Type",
    "number": "#",
    "updated_at": "Updated",
    "title": "Title",
    "assignees": "Assignees",
    "missing_fields": "Missing fields",
    "url": "URL",
}


def _xlsx_bytes(rows: list[FindingRow]) -> bytes:
    fields = list(FindingRow.__annotations__)
    buf = io.BytesIO()
    # strings_to_formulas=False: issue titles are repo-controlled text; a title like
    # =HYPERLINK(...) must land as a string, not an executable formula.
    with xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False}) as book:
        sheet = book.add_worksheet("Findings")  # pyright: ignore[reportUnknownMemberType]
        # Excel Table = per-column filter dropdowns + banded rows out of the box.
        # Table range needs at least one data row, even when there are no findings.
        sheet.add_table(
            0,
            0,
            max(len(rows), 1),
            len(fields) - 1,
            {
                "data": [[dict(row)[f] for f in fields] for row in rows],
                "columns": [{"header": _XLSX_HEADERS[f]} for f in fields],
                "style": "Table Style Medium 9",
            },
        )
        sheet.freeze_panes(1, 0)
        sheet.autofit(300)
    return buf.getvalue()


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
        if '"' in v or "\\" in v:
            msg = "env value must not contain quotes or backslashes"
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
    "project_ids_by_number": None,
    "project_fields_by_number": None,
    "agent_messages": [],
    "chat_message_history": [],
    "agent_pending_writes": [],
    "agent_pending_project_id": None,
    "agent_pending_fields": None,
    "agent_pending_content_id": None,
    "agent_pending_scan": False,
    "agent_pending_control_updates": None,
    "scan_include_issues": _bool("INCLUDE_ISSUES", "true"),
    "scan_include_closed_issues": _bool("INCLUDE_CLOSED_ISSUES", "false"),
    "scan_include_pull_requests": _bool("INCLUDE_PULL_REQUESTS", "true"),
    "scan_include_closed_pull_requests": _bool("INCLUDE_CLOSED_PULL_REQUESTS", "false"),
}
for _k, _v in session_defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

_pending_control_updates = cast(
    dict[str, bool] | None,
    st.session_state.agent_pending_control_updates,
)
if _pending_control_updates:
    for _control_name, _control_value in _pending_control_updates.items():
        st.session_state[f"scan_{_control_name}"] = _control_value
    st.session_state.agent_pending_control_updates = None

# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Configuration")

    with st.expander("🔑 GitHub Connection", expanded=True):
        token = st.text_input(
            "Personal Access Token",
            value=E.get("GITHUB_TOKEN", ""),
            type="password",
            autocomplete="off",
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
            help=(
                "When checked, the scanner fetches every open project in the org "
                "automatically. Uncheck to specify project numbers manually below."
            ),
        )
        include_closed_projects = st.checkbox(
            "Include closed projects",
            value=_bool("GITHUB_INCLUDE_CLOSED_PROJECTS", "false"),
            disabled=not include_all_projects,
            help=(
                "Also scan projects whose state is 'closed'. Only relevant when "
                "'All org projects' is enabled."
            ),
        )
        project_numbers = st.text_area(
            "Project numbers or URLs",
            value=E.get("GITHUB_PROJECT_NUMBERS", E.get("GITHUB_PROJECT_NUMBER", "")),
            disabled=include_all_projects,
            placeholder="42\n123\nhttps://github.com/orgs/my-company/projects/5",
            help="Paste numbers or GitHub project URLs. Comma, semicolon, or newline separated.",
        )
        st.caption("Click **💾 Save settings** below to persist all values to `.env`.")

    assignees = E.get("TARGET_ASSIGNEES", "")
    inc_unassigned = _bool("INCLUDE_UNASSIGNED", "false")

    with st.expander("✅ Checks to flag", expanded=True):
        req_board = st.checkbox(
            "Issues not on selected project board",
            value=_bool("REQUIRE_PROJECT_ITEM", "false"),
            help=(
                "Flag issues that are not linked to any of the scanned "
                "GitHub Project V2 boards. "
                "Turn this off if repo issues are allowed to exist outside the selected board."
            ),
        )
        req_board_prs = st.checkbox(
            "…also require board item for pull requests",
            value=_bool("REQUIRE_PROJECT_ITEM_PULL_REQUESTS", "false"),
            disabled=not req_board,
            help="Off (default): only issues must be on the board — PRs are exempt.",
        )
        # "none" is the explicit off-sentinel ("" would fall back to the config default)
        _required_env = E.get("REQUIRED_PROJECT_FIELDS", "")
        if _required_env.strip().casefold() == "none":
            _required_env = ""
        require_fields = st.checkbox(
            "Missing required project fields",
            value=bool(_csv(_required_env)),
            help=(
                "Flag items that are missing one or more of the required Project V2 "
                "custom fields listed below."
            ),
        )
        required_fields = st.text_area(
            "Required Project fields",
            value=_required_env,
            disabled=not require_fields,
            placeholder="Estimate\nIteration (sprint)\nPriority\nDifficulty\nStatus",
            help=(
                "Names of GitHub Project V2 custom fields that must be filled in. "
                "Use exact field names from your project board. "
                "One per line or comma-separated."
            ),
        )
        require_assignee = st.checkbox(
            "Unassigned items",
            value=_bool("REQUIRE_ASSIGNEE", "true"),
            help="Flag issues and PRs that have no assignee at all.",
        )
        require_target = _bool("REQUIRE_TARGET_ASSIGNEE", "true")
        require_dev = st.checkbox(
            "Missing development link",
            value=_bool("REQUIRE_DEVELOPMENT_LINK", "true"),
            help=(
                "Flag issues that have no linked pull request, and PRs that do not "
                "reference a closing issue. Uses GitHub's development link feature."
            ),
        )
        require_pr_branch = st.checkbox(
            "Missing linked PR or branch specifically",
            value=_bool("REQUIRE_LINKED_PR_OR_BRANCH", "true"),
            help=(
                "Stricter than 'Missing development link': flags items that have no "
                "linked PR or branch reference anywhere, including in the PR body."
            ),
        )

    with st.expander("📂 Scan Scope", expanded=False):
        inc_issues = st.checkbox(
            "Issues",
            value=_bool("INCLUDE_ISSUES", "true"),
            key="scan_include_issues",
            help="Include open GitHub Issues in the scan.",
        )
        inc_closed = st.checkbox(
            "Include closed issues",
            value=_bool("INCLUDE_CLOSED_ISSUES", "false"),
            key="scan_include_closed_issues",
            disabled=not inc_issues,
            help="Also scan issues in the 'closed' state. By default only open issues are scanned.",
        )
        inc_prs = st.checkbox(
            "Pull Requests",
            value=_bool("INCLUDE_PULL_REQUESTS", "true"),
            key="scan_include_pull_requests",
            help="Include open Pull Requests in the scan.",
        )
        inc_closed_prs = st.checkbox(
            "Include closed/merged PRs",
            value=_bool("INCLUDE_CLOSED_PULL_REQUESTS", "false"),
            key="scan_include_closed_pull_requests",
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
            help=(
                "Filter items by last GitHub update. 'All time' imposes no date "
                "filter. Use a range to focus on recent activity and speed up the scan."
            ),
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
            help=(
                "Scan every repository in the organization. For large orgs this can "
                "be slow; use the allowlist below to limit scope."
            ),
        )
        repo_allowlist = st.text_input(
            "Repository allowlist",
            value=E.get("GITHUB_REPOSITORY_ALLOWLIST", ""),
            disabled=inc_all_repos,
            placeholder="frontend,backend,api-service",
            help=(
                "Only scan these repositories. Enter repo names without the org prefix, "
                "comma-separated. Leave empty to use all repos."
            ),
        )
        repo_denylist = st.text_input(
            "Repository denylist",
            value=E.get("GITHUB_REPOSITORY_DENYLIST", ""),
            placeholder="archive-repo,legacy-app",
            help=(
                "Always skip these repositories, even when 'All org repositories' is "
                "enabled. Enter repo names without the org prefix, comma-separated."
            ),
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
        _PROVIDERS = ["openai", "azure", "openai-compatible", "ollama"]
        _env_provider = E.get("LLM_PROVIDER", "openai")
        llm_provider = st.selectbox(
            "Provider",
            _PROVIDERS,
            index=_PROVIDERS.index(_env_provider) if _env_provider in _PROVIDERS else 0,
            help=(
                "Use **ollama** for a local model with no API key. "
                "Use **openai** or **azure** for hosted APIs. "
                "Use **openai-compatible** for a local or remote endpoint "
                "that exposes the OpenAI chat completions API."
            ),
        )
        _ollama = llm_provider == "ollama"
        llm_api_key = st.text_input(
            "API Key",
            value="" if _ollama else E.get("LLM_API_KEY", E.get("AZURE_API_KEY", "")),
            type="password",
            autocomplete="off",
            placeholder="Not required for local Ollama" if _ollama else "Paste provider API key",
            disabled=_ollama,
            help=(
                "Stored in .env on this machine only. Sent only to the selected "
                "LLM endpoint when the provider needs a key."
            ),
        )
        _model_hints = {
            "ollama": "llama3.2, qwen3, mistral, codellama",
            "openai": "gpt-4o, gpt-4o-mini",
            "azure": "your-deployment-name",
            "openai-compatible": "model name as required by the endpoint",
        }
        llm_model = st.text_input(
            "Model name",
            value=E.get("LLM_MODEL_NAME", E.get("AZURE_LLM_MODEL_NAME", "")),
            placeholder=_model_hints.get(llm_provider, "model-name"),
            help="Model ID as required by your provider.",
        )
        _url_placeholder = {
            "azure": "https://my-resource.openai.azure.com/",
            "openai-compatible": "http://localhost:1234/v1",
            "ollama": "http://localhost:11434/v1",
        }.get(llm_provider, "Leave empty for default")
        llm_base_url = st.text_input(
            "Ollama Base URL (optional)" if _ollama else "Base URL (optional)",
            value=E.get("LLM_BASE_URL", E.get("AZURE_API_BASE", "")),
            placeholder=_url_placeholder,
            help="Leave empty to use the provider default.",
        )
        llm_api_version = st.text_input(
            "API Version (Azure only)",
            value=E.get("LLM_API_VERSION", E.get("AZURE_API_VERSION", "")),
            placeholder="2024-08-01-preview",
            disabled=llm_provider != "azure",
            help="Azure API version string. Leave empty for non-Azure providers.",
        )
        if _ollama:
            st.info("Local mode: Ollama runs on this machine. No API key is required.")
        llm_ready = bool(llm_model.strip() and (llm_api_key.strip() or _ollama))
        if llm_ready:
            st.caption(f"✅ AI features enabled ({llm_provider})")
        else:
            st.caption("Enter model name and API key when required to enable AI features.")
        auto_apply_enabled = st.checkbox(
            "Allow GitHub writes (AUTO_APPLY)",
            value=_bool("AUTO_APPLY"),
            help=(
                "Lets the AI assistant write queued changes (fields, comments, labels, ...) "
                "to GitHub after you confirm with `apply it`. Off = previews only."
            ),
        )

    st.divider()
    if st.button("💾 Save settings to .env", width="stretch"):
        try:
            _env_save: dict[str, str] = {
                "GITHUB_TOKEN": token,
                "GITHUB_ORG": org,
                "GITHUB_INCLUDE_ALL_PROJECTS": "true" if include_all_projects else "false",
                "GITHUB_INCLUDE_CLOSED_PROJECTS": "true" if include_closed_projects else "false",
                "GITHUB_PROJECT_NUMBERS": _project_numbers(project_numbers),
                "REQUIRE_PROJECT_ITEM": "true" if req_board else "false",
                "REQUIRE_PROJECT_ITEM_PULL_REQUESTS": "true" if req_board_prs else "false",
                # "none" because env_ignore_empty would turn "" back into the default
                "REQUIRED_PROJECT_FIELDS": (_csv(required_fields) if require_fields else "")
                or "none",
                "REQUIRE_ASSIGNEE": "true" if require_assignee else "false",
                "REQUIRE_DEVELOPMENT_LINK": "true" if require_dev else "false",
                "REQUIRE_LINKED_PR_OR_BRANCH": "true" if require_pr_branch else "false",
                "INCLUDE_ISSUES": "true" if inc_issues else "false",
                "INCLUDE_CLOSED_ISSUES": "true" if inc_closed else "false",
                "INCLUDE_PULL_REQUESTS": "true" if inc_prs else "false",
                "INCLUDE_CLOSED_PULL_REQUESTS": "true" if inc_closed_prs else "false",
                "GITHUB_INCLUDE_ALL_REPOSITORIES": "true" if inc_all_repos else "false",
                "GITHUB_REPOSITORY_ALLOWLIST": repo_allowlist,
                "GITHUB_REPOSITORY_DENYLIST": repo_denylist,
                "GITHUB_UPDATED_FROM": updated_from.isoformat() if updated_from else "",
                "GITHUB_UPDATED_TO": updated_to.isoformat() if updated_to else "",
                "LLM_PROVIDER": llm_provider,
                "LLM_MODEL_NAME": llm_model,
                "LLM_BASE_URL": llm_base_url,
                "LLM_API_VERSION": llm_api_version,
                "LLM_ENABLED": "true",
                "AUTO_APPLY": "true" if auto_apply_enabled else "false",
            }
            if not _ollama:
                _env_save["LLM_API_KEY"] = llm_api_key
            _write_env_keys(_env_save)
            st.success("✅ Saved to .env — settings persist after restart.")
        except (ValueError, OSError) as exc:
            st.error(f"Could not save: {exc}")
    scan_btn = st.button("▶ Run Scan", type="primary", width="stretch")
    if st.session_state.scan_time:
        st.caption(f"Last scan: {st.session_state.scan_time}")
    if st.session_state.rows is not None and st.button("✕ Clear Results", width="stretch"):
        for k in (
            "rows",
            "findings",
            "error",
            "stats",
            "scan_time",
            "project_ids_by_number",
            "project_fields_by_number",
            "agent_pending_project_id",
            "agent_pending_fields",
            "agent_pending_content_id",
        ):
            st.session_state[k] = None
        st.session_state.agent_pending_writes = []
        st.session_state.agent_pending_scan = False
        st.session_state.agent_pending_control_updates = None
        st.session_state.limitations = list[str]()
        st.rerun()


# ── scan logic ────────────────────────────────────────────────────────────────
def _clear_scan_results() -> None:
    """Drop scan-derived state so a failed rescan can't leave stale findings
    addressable by the write agent."""
    for key in ("rows", "stats", "scan_time", "findings"):
        st.session_state[key] = None
    st.session_state.project_ids_by_number = None
    st.session_state.project_fields_by_number = None


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
                "require_project_item_pull_requests": req_board_prs,
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
        _clear_scan_results()
        return
    except ValueError as exc:
        st.session_state.error = str(exc)
        _clear_scan_results()
        return

    try:
        with GitHubClient(settings.github_token) as client:
            repositories = discover_repositories(client, settings)
            searched_items = search_items(
                client,
                repositories,
                settings.target_assignees,
                include_issues=settings.include_issues,
                include_pull_requests=settings.include_pull_requests,
                include_closed_issues=settings.include_closed_issues,
                include_closed_pull_requests=settings.include_closed_pull_requests,
                include_unassigned=settings.include_unassigned,
            )
            discoveries = discover_all(
                client, settings, repositories=repositories, searched_items=searched_items
            )
            results = scan_all(client, settings, discoveries, searched_items)
    except GitHubError as exc:
        st.session_state.error = str(exc)
        # stale findings must not stay addressable by the write agent after a failed scan
        _clear_scan_results()
        return

    rows: list[FindingRow] = []
    # each per-project scan iterates the same search results, so summing the
    # per-scan counts would count every item once per project
    issues = max((r.scanned_issue_count for r in results), default=0)
    prs = max((r.scanned_pull_request_count for r in results), default=0)
    # key → (FindingRow, AuditFinding) — deduplicate across project scans,
    # preferring the scan of a board the item is actually on, then fewest missing
    # fields (item may be on project A but not B)
    best: dict[tuple[str, str, int], tuple[FindingRow, AuditFinding]] = {}
    for r in results:
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
            rank = (f.project_item_id is None, len(f.missing_fields))
            if existing is None or rank < (
                existing[1].project_item_id is None,
                len(existing[1].missing_fields),
            ):
                best[key] = (row, f)

    rows = [v[0] for v in best.values()]
    findings_by_key = {k: v[1] for k, v in best.items()}

    limitations = list({lim for r in results for lim in r.limitations})

    st.session_state.scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.session_state.rows = rows
    st.session_state.findings = findings_by_key
    st.session_state.project_ids_by_number = {
        discovery.project_number: discovery.project_id for discovery in discoveries
    }
    st.session_state.project_fields_by_number = {
        discovery.project_number: discovery.fields for discovery in discoveries
    }
    st.session_state.error = None
    st.session_state.stats = {"issues": issues, "prs": prs, "findings": len(rows)}
    st.session_state.limitations = limitations
    st.session_state.agent_pending_writes = []
    st.session_state.agent_pending_project_id = None
    st.session_state.agent_pending_fields = None
    st.session_state.agent_pending_content_id = None


def _scan_request_error() -> str | None:
    if not token.strip():
        return "GitHub token is required."
    if not org.strip():
        return "Organization name is required."
    if not include_all_projects and not project_numbers.strip():
        return "At least one project number is required."
    if not inc_all_repos and not repo_allowlist.strip():
        return "Either enable 'All org repositories' or enter a repository allowlist."
    if not inc_issues and not inc_prs:
        return "Enable at least one of Issues or Pull Requests."
    return None


if scan_btn:
    _clear_scan_results()
    st.session_state.limitations = list[str]()
    scan_error = _scan_request_error()
    if scan_error:
        st.session_state.error = scan_error
    else:
        st.session_state.error = None
        with st.spinner("Connecting to GitHub and scanning — this may take a minute…"):
            _run_scan()

if st.session_state.agent_pending_scan:
    st.session_state.agent_pending_scan = False
    scan_error = _scan_request_error()
    if scan_error:
        st.session_state.error = scan_error
    else:
        st.session_state.error = None
        with st.spinner("AI assistant rerunning the scan…"):
            _run_scan()


def _llm_settings() -> Settings:
    """Minimal Settings object for LLM calls — scan fields are placeholder-only."""
    return Settings.model_validate(
        {
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
        }
    )


def _agent_messages() -> list[AgentMessage]:
    messages = st.session_state.agent_messages
    if isinstance(messages, list):
        return cast(list[AgentMessage], messages)
    st.session_state.agent_messages = []
    return []


def _add_agent_message(role: str, content: str) -> None:
    messages = _agent_messages()
    messages.append({"role": role, "content": content})
    st.session_state.agent_messages = messages[-30:]


def _trim_partial_apply(
    write: PendingWrite, exc: PartialApplyError, rest: list[PendingWrite]
) -> list[PendingWrite]:
    """Rebuild the failed ApplyPlan write with only its unapplied changes, keeping later writes."""
    assert isinstance(write, ApplyPlan)
    remaining_changes = [change for change in write.changes if change not in exc.applied]
    if not remaining_changes:
        return rest
    trimmed = ApplyPlan(changes=remaining_changes, skipped=exc.skipped)
    return [trimmed, *rest]


def _pending_apply_reply() -> str:
    pending_writes = cast(list[PendingWrite], st.session_state.agent_pending_writes or [])
    project_id = cast(str | None, st.session_state.agent_pending_project_id)
    fields = cast(list[ProjectFieldDefinition] | None, st.session_state.agent_pending_fields)
    if not pending_writes:
        return "No pending write. Select a finding and ask for a field change, comment, or edit."
    if not auto_apply_enabled:
        return (
            "Write blocked: enable **Allow GitHub writes (AUTO_APPLY)** in the sidebar "
            "(⚙️ Config → 🧠 AI Assistant), then say `apply it` again."
        )
    if not token.strip():
        return "Write blocked: GitHub token is missing. Add it in the sidebar or `.env`."
    # Board adds must run first — later field updates resolve their project item
    # id from the add's result via created_item_ids.
    pending_writes = [w for w in pending_writes if isinstance(w, AddToProjectPlan)] + [
        w for w in pending_writes if not isinstance(w, AddToProjectPlan)
    ]
    created_item_ids: dict[str, str] = {}
    applied = 0
    with GitHubClient(token) as client:
        for index, write in enumerate(pending_writes):
            try:
                apply_pending_write(
                    client,
                    write,
                    project_id=project_id,
                    fields=fields,
                    created_item_ids=created_item_ids,
                )
            except PartialApplyError as exc:
                remaining = _trim_partial_apply(write, exc, pending_writes[index + 1 :])
                # keep the retry queue self-contained: adds already consumed can no
                # longer resolve empty project item ids on the next run
                resolve_created_item_ids(remaining, created_item_ids)
                st.session_state.agent_pending_writes = remaining
                return (
                    f"Applied {applied} write(s) fully, then failed on write {index + 1}"
                    f" after {len(exc.applied)} field change(s) went through: {exc}."
                    f" {len(remaining)} write(s) remain queued - say `apply it` to retry."
                )
            except (GitHubError, ValueError) as exc:
                remaining = pending_writes[index:]
                resolve_created_item_ids(remaining, created_item_ids)
                st.session_state.agent_pending_writes = remaining
                return (
                    f"Applied {applied} write(s), then failed on write {index + 1}: {exc}."
                    f" {len(remaining)} write(s) remain queued - say `apply it` to retry."
                )
            applied += 1
    st.session_state.agent_pending_writes = []
    st.session_state.agent_pending_project_id = None
    st.session_state.agent_pending_fields = None
    st.session_state.agent_pending_content_id = None
    st.session_state.agent_pending_scan = True
    return f"Applied {applied} write(s). Rerunning scan."


def _finding_from_prompt(
    prompt: str,
    findings_store: dict[tuple[str, str, int], AuditFinding],
) -> AuditFinding | None:
    """Resolve a '#123' / 'issue 123' reference to a uniquely numbered scanned finding."""
    refs = {
        int(match.group(1))
        for match in re.finditer(r"(?:#|\b(?:issue|pr)\s+#?)(\d+)", prompt, re.IGNORECASE)
    }
    if len(refs) != 1:
        return None
    number = refs.pop()
    matches = [finding for (_, _, num), finding in findings_store.items() if num == number]
    return matches[0] if len(matches) == 1 else None


def _agent_reply(
    prompt: str,
    selected_key: tuple[str, str, int] | None,
    rows_for_summary: list[FindingRow],
    filtered_for_summary: list[FindingRow],
    stats_for_summary: ScanStats | None,
) -> str:
    command = parse_agent_command(prompt)
    replies: list[str] = []

    findings_store = cast(
        dict[tuple[str, str, int], AuditFinding],
        st.session_state.findings or {},
    )
    has_pending = bool(st.session_state.agent_pending_writes)
    if should_apply_now(prompt):
        # The queued writes belong to one item; confirming while a different item
        # is selected must never write to the previously selected one.
        selected = findings_store.get(selected_key) if selected_key else None
        pending_content_id = cast(str | None, st.session_state.agent_pending_content_id)
        if has_pending and selected is not None and selected.content_id != pending_content_id:
            count = len(cast(list[PendingWrite], st.session_state.agent_pending_writes))
            st.session_state.agent_pending_writes = []
            st.session_state.agent_pending_project_id = None
            st.session_state.agent_pending_fields = None
            st.session_state.agent_pending_content_id = None
            return (
                f"Discarded {count} queued write(s) because you switched items."
                " Nothing was applied. Ask again on the currently selected item."
            )
        return _pending_apply_reply()

    pending_controls = cast(
        dict[str, bool],
        st.session_state.agent_pending_control_updates or {},
    )
    for update in command.control_updates:
        pending_controls[update.name] = update.value
        replies.append(f"Set `{update.name}` -> `{update.value}`.")
    if pending_controls:
        st.session_state.agent_pending_control_updates = pending_controls

    if command.run_scan:
        st.session_state.agent_pending_scan = True
        replies.append("Scan queued.")

    selected_finding = findings_store.get(selected_key) if selected_key else None
    if selected_finding is None:
        # Let "add estimate 5 to #123"-style prompts work without the dropdown.
        selected_finding = _finding_from_prompt(prompt, findings_store)

    if command.explain:
        if selected_finding is not None:
            if llm_ready:
                try:
                    from github_audit.llm_evaluator import explain_finding

                    rule = ", ".join(selected_finding.missing_fields) or "unknown rule"
                    explanation = explain_finding(selected_finding, rule, _llm_settings())
                    replies.append(explanation.explanation)
                    replies.append(f"Impact: {explanation.impact}")
                    replies.append(f"Suggested fix: {explanation.suggested_fix}")
                except Exception as exc:
                    replies.append(
                        f"AI call failed: {exc}\n\n"
                        "Check your API key in **⚙️ Config → 🧠 AI Assistant** "
                        "or add `LLM_API_KEY` to `.env`. "
                        "For Ollama (local/free): set `LLM_PROVIDER=ollama` — no key needed."
                    )
            else:
                missing = ", ".join(selected_finding.missing_fields)
                replies.append(
                    f"{selected_finding.item_type} #{selected_finding.number} "
                    f"is missing: {missing}.\n\n"
                    "To get AI explanations: add `LLM_API_KEY` + `LLM_MODEL_NAME` to `.env`, "
                    "or configure them in **⚙️ Config → 🧠 AI Assistant**. "
                    "For local/free: set `LLM_PROVIDER=ollama` — no key needed."
                )
        elif findings_store:
            if llm_ready:
                try:
                    from github_audit.llm_evaluator import batch_triage

                    result = batch_triage(list(findings_store.values()), _llm_settings())
                    replies.append(f"**Top priority action:** {result.top_priority_action}")
                    if result.root_causes:
                        replies.append(
                            "**Root causes:**\n" + "\n".join(f"- {c}" for c in result.root_causes)
                        )
                    if result.recommendations:
                        replies.append(
                            "**Recommendations:**\n"
                            + "\n".join(f"- {r}" for r in result.recommendations)
                        )
                    if result.team_process_insight:
                        replies.append(f"**Team insight:** {result.team_process_insight}")
                except Exception as exc:
                    replies.append(f"AI call failed: {exc}")
            else:
                replies.append(
                    f"{len(findings_store)} findings loaded. "
                    "Enable AI (LLM_MODEL_NAME + LLM_API_KEY or Ollama) to get a batch summary."
                )
        else:
            replies.append("No findings to explain. Run a scan first.")

    if not replies:
        if llm_ready:
            try:
                from pydantic_ai.messages import ModelMessage

                from github_audit.llm_evaluator import (
                    general_chat,
                    project_agent_chat,
                    trim_message_history,
                )

                ctx_parts = [
                    summarize_findings(
                        len(rows_for_summary), len(filtered_for_summary), stats_for_summary
                    )
                ]
                stored_history: list[ModelMessage] = cast(
                    list[ModelMessage],
                    st.session_state.get("chat_message_history") or [],
                )
                if selected_finding:
                    project_ids = cast(
                        dict[int, str] | None,
                        st.session_state.project_ids_by_number,
                    )
                    fields_by_project = cast(
                        dict[int, list[ProjectFieldDefinition]] | None,
                        st.session_state.project_fields_by_number,
                    )
                    project_id = (
                        project_ids.get(selected_finding.project_number)
                        if project_ids and selected_finding.project_number is not None
                        else None
                    )
                    fields = (
                        fields_by_project.get(selected_finding.project_number, [])
                        if fields_by_project and selected_finding.project_number is not None
                        else []
                    )

                    discarded_notice = ""
                    pending_writes = cast(
                        list[PendingWrite], st.session_state.agent_pending_writes or []
                    )
                    pending_content_id = cast(str | None, st.session_state.agent_pending_content_id)
                    if pending_writes and pending_content_id != selected_finding.content_id:
                        discarded_notice = (
                            f"\n\n(Note: {len(pending_writes)} unapplied write(s) queued for a"
                            " different item were discarded because you switched items.)"
                        )
                        pending_writes = []
                        st.session_state.agent_pending_writes = []
                        st.session_state.agent_pending_project_id = None
                        st.session_state.agent_pending_fields = None
                        st.session_state.agent_pending_content_id = None

                    with GitHubClient(token) as client:
                        labels = fetch_repo_labels(client, selected_finding.repository)
                        milestones = fetch_repo_milestones(client, selected_finding.repository)
                        assignable_users = fetch_assignable_users(
                            client, selected_finding.repository
                        )

                    agent_result = project_agent_chat(
                        prompt,
                        "\n\n".join(ctx_parts),
                        selected_finding,
                        fields,
                        project_id,
                        _llm_settings(),
                        labels=labels,
                        milestones=milestones,
                        assignable_users=assignable_users,
                        existing_writes=pending_writes,
                        message_history=stored_history,
                    )
                    st.session_state.chat_message_history = trim_message_history(
                        stored_history + agent_result.new_messages, 20
                    )
                    replies.append(agent_result.reply + discarded_notice)
                    preview: list[str] = []
                    if agent_result.pending_writes:
                        st.session_state.agent_pending_writes = agent_result.pending_writes
                        st.session_state.agent_pending_project_id = agent_result.project_id
                        st.session_state.agent_pending_fields = agent_result.fields
                        st.session_state.agent_pending_content_id = selected_finding.content_id
                        for write in agent_result.pending_writes:
                            preview.extend(describe_pending_write(write))
                    if preview:
                        replies.append("Prepared write preview:")
                        replies.extend(preview)
                        if auto_apply_enabled:
                            replies.append("Say `apply it` to write.")
                        else:
                            replies.append(
                                "Say `apply it` to write — but first enable "
                                "**Allow GitHub writes (AUTO_APPLY)** in the sidebar "
                                "(⚙️ Config → 🧠 AI Assistant)."
                            )
                    return "\n\n".join(replies)

                reply, new_msgs = general_chat(
                    prompt,
                    "\n\n".join(ctx_parts),
                    _llm_settings(),
                    message_history=stored_history,
                )
                # Keep last ~20 messages to bound context size (trimmed at run
                # boundaries so tool calls stay paired with their returns).
                st.session_state.chat_message_history = trim_message_history(
                    stored_history + new_msgs, 20
                )
                return reply
            except Exception as exc:
                replies.append(f"AI error: {exc}")
        else:
            replies.append(
                summarize_findings(
                    len(rows_for_summary), len(filtered_for_summary), stats_for_summary
                )
            )
            replies.append(
                "Configure an LLM (sidebar → 🧠 AI Assistant) to ask anything. "
                "Without LLM, only scan control commands are available."
            )

    return "\n\n".join(replies)


def _render_agent_assistant(visible_rows: list[FindingRow]) -> None:
    st.markdown("**🧠 AI Assistant**")

    all_rows = cast(list[FindingRow], st.session_state.rows or [])
    findings_store = cast(
        dict[tuple[str, str, int], AuditFinding],
        st.session_state.findings or {},
    )
    stats = cast(ScanStats | None, st.session_state.stats)

    _ALL_LABEL = "🔍 All findings"
    # Mirror the table: only rows passing the current filters are offered.
    target_options = {
        f"#{row['number']} {row['title'][:40]}": (
            row["repository"],
            row["item_type"],
            row["number"],
        )
        for row in visible_rows[:200]
    }
    selected_key: tuple[str, str, int] | None = None
    if all_rows:
        options = [_ALL_LABEL, *target_options]
        if st.session_state.get("agent_target_finding") not in options:
            st.session_state.agent_target_finding = _ALL_LABEL
        selected_label = st.selectbox(
            "Finding",
            options,
            key="agent_target_finding",
            label_visibility="collapsed",
        )
        if selected_label != _ALL_LABEL:
            selected_key = target_options[selected_label]
            selected = findings_store.get(selected_key)
            if selected and selected.missing_fields:
                st.caption(f"Missing: {', '.join(selected.missing_fields)}")
        else:
            st.caption(
                f"{len(visible_rows)} of {len(all_rows)} findings (table filters apply) — "
                "say `explain` for a batch summary."
            )

    if not _agent_messages():
        if selected_key:
            st.info("Ask for a Project field update or comment — then confirm with `apply it`.")
        else:
            st.info(
                "Say `explain` for a batch summary, pick a finding above, or reference one "
                "directly — e.g. `add estimate 5 to #123`."
            )

    for message in _agent_messages():
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    with st.form(key="agent_form", clear_on_submit=True):
        prompt = st.text_input("Message", placeholder="Ask...", label_visibility="collapsed")
        submitted = st.form_submit_button("Send", icon=":material/send:", use_container_width=True)

    if submitted and prompt:
        _add_agent_message("user", prompt)
        reply = _agent_reply(prompt, selected_key, all_rows, visible_rows, stats)
        _add_agent_message("assistant", reply)
        st.rerun()


# ── main content ──────────────────────────────────────────────────────────────
_h1, _h2 = st.columns([7, 1])
with _h1:
    st.title("🔍 GitHub Audit")
with _h2:
    st.write("")
    # Filled after the table filters run so the AI dropdown mirrors the table.
    _ai_popover = st.popover("AI", use_container_width=True, help="Open AI assistant")

if st.session_state.error:
    st.error(st.session_state.error)

if st.session_state.rows is None:
    with _ai_popover:
        _render_agent_assistant([])
    st.info(
        "Configure settings in the sidebar, then click **▶ Run Scan** to audit "
        "your GitHub Projects for missing fields and workflow gaps."
    )
    with st.expander("What does this tool check?"):
        st.markdown("""
- **Required fields** — Estimate, Priority, Iteration (sprint), Difficulty, Status (configurable)
- **Assignees** — whether items are assigned and to your target users
- **Development links** — whether issues have a linked PR or branch
- **Project board membership** — optional check for items missing from the V2 board
        """)
    st.stop()

rows = cast(list[FindingRow], st.session_state.rows)
stats = cast(ScanStats, st.session_state.stats)

c1, c2, c3 = st.columns(3)
c1.metric("Issues scanned", stats["issues"])
c2.metric("PRs scanned", stats["prs"])
c3.metric("Findings", stats["findings"])

if not rows:
    with _ai_popover:
        _render_agent_assistant([])
    st.success("✅ No findings — everything looks good!")
    st.stop()

# ── filters ───────────────────────────────────────────────────────────────────
st.subheader("Filters")
fc1, fc2, fc3, fc4, fc5, fc6 = st.columns([1, 1, 1, 1, 1, 0.22])

all_repos = sorted({row["repository"] for row in rows})
all_missing = sorted(
    {f.strip() for row in rows for f in row["missing_fields"].split(",") if f.strip()}
)
all_assignees = sorted(
    {
        a.strip()
        for row in rows
        for a in row["assignees"].split(",")
        if a.strip() and a.strip() != "(none)"
    }
)
all_types = sorted({row["item_type"] for row in rows})
proj_labels: dict[int, str] = {}
for row in rows:
    p = row["project"]
    if p and p not in proj_labels:
        t = row["project_title"]
        proj_labels[p] = f"{p} - {t}" if t else str(p)
all_proj_options = [proj_labels[p] for p in sorted(proj_labels)]

_date_active = bool(
    st.session_state.get("filter_date_from") or st.session_state.get("filter_date_to")
)
_date_btn_label = "📅 ●" if _date_active else "📅"

# Explicit keys keep selections alive across rescans; prune values that no
# longer exist in the fresh options (a stale value would raise otherwise).
for _fk, _fopts in (
    ("filter_repos", all_repos),
    ("filter_missing", all_missing),
    ("filter_assignees", all_assignees),
    ("filter_types", all_types),
    ("filter_projects", all_proj_options),
):
    if _fk in st.session_state:
        st.session_state[_fk] = [v for v in st.session_state[_fk] if v in _fopts]

with fc1:
    sel_repos = st.multiselect("Repository", all_repos, key="filter_repos")
with fc2:
    sel_missing = st.multiselect("Missing field", all_missing, key="filter_missing")
with fc3:
    sel_assignees = st.multiselect("Assignee", all_assignees, key="filter_assignees")
with fc4:
    sel_types = st.multiselect("Type", all_types, key="filter_types")
with fc5:
    sel_proj_labels = st.multiselect("Project", all_proj_options, key="filter_projects")
with fc6:
    st.markdown('<div style="height:27px"></div>', unsafe_allow_html=True)
    with st.popover(_date_btn_label, use_container_width=True, help="Filter by last updated date"):
        _dc1, _dc2 = st.columns(2)
        with _dc1:
            date_from = st.date_input("From", value=None, key="filter_date_from")
        with _dc2:
            date_to = st.date_input("To", value=None, key="filter_date_to")

sel_proj_nums = {p for p, lbl in proj_labels.items() if lbl in sel_proj_labels}

filtered: list[FindingRow] = []
for row in rows:
    if sel_repos and row["repository"] not in sel_repos:
        continue
    # exact membership, not substring: "assignee" must not match "target assignee",
    # login "jan" must not match "janedoe"
    row_missing = {part.strip() for part in row["missing_fields"].split(",")}
    if sel_missing and not any(f in row_missing for f in sel_missing):
        continue
    row_assignees = {part.strip() for part in row["assignees"].split(",")}
    if sel_assignees and not any(a in row_assignees for a in sel_assignees):
        continue
    if sel_types and row["item_type"] not in sel_types:
        continue
    if sel_proj_nums and row["project"] not in sel_proj_nums:
        continue
    if date_from or date_to:
        raw = row["updated_at"]
        try:
            row_date = date.fromisoformat(raw[:10]) if raw else None
        except ValueError:
            row_date = None
        if row_date is None:
            continue
        if date_from and row_date < date_from:
            continue
        if date_to and row_date > date_to:
            continue
    filtered.append(row)

st.caption(f"Showing **{len(filtered)}** of {len(rows)} findings")

with _ai_popover:
    _render_agent_assistant(filtered)

# ── results table ─────────────────────────────────────────────────────────────
st.dataframe(
    [
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
    ],
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

st.download_button(
    "⬇️ Download filtered Excel",
    _xlsx_bytes(filtered),
    "findings.xlsx",
    _XLSX_MIME,
    on_click="ignore",
)

with st.expander("📊 Missing field breakdown"):
    field_counts: dict[str, int] = {}
    for row in rows:
        for _f in row["missing_fields"].split(","):
            _f = _f.strip()
            if _f:
                field_counts[_f] = field_counts.get(_f, 0) + 1
    st.bar_chart(
        [{"Field": _f, "Count": c} for _f, c in sorted(field_counts.items(), key=lambda x: x[1])],
        x="Field",
        y="Count",
        horizontal=True,
    )

limitations = cast(list[str], st.session_state.limitations)
if limitations:
    with st.expander(f"⚠️ {len(limitations)} scan limitation(s)"):
        for lim in limitations:
            st.warning(lim, icon="⚠️")
