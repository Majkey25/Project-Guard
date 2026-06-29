# GitHub Audit

[![Repository](https://img.shields.io/badge/repository-public-brightgreen)](https://github.com/Majkey25/Project-Guard)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![uv](https://img.shields.io/badge/package%20manager-uv-black)](https://docs.astral.sh/uv/)
[![Streamlit](https://img.shields.io/badge/ui-Streamlit-ff4b4b)](https://streamlit.io/)
[![Ruff](https://img.shields.io/badge/lint-Ruff-46aef7)](https://docs.astral.sh/ruff/)
[![Pyright](https://img.shields.io/badge/types-Pyright%20strict-yellow)](https://github.com/microsoft/pyright)
[![Tests](https://img.shields.io/badge/tests-pytest-blueviolet)](https://pytest.org/)
[![Release](https://img.shields.io/github/v/release/Majkey25/Project-Guard?label=release)](https://github.com/Majkey25/Project-Guard/releases)
[![License](https://img.shields.io/badge/license-not%20specified-lightgrey)](#license)

Local tool for checking GitHub Issues and Pull Requests against your GitHub Project V2 workflow.

It answers one practical question:

> Which items are missing the fields or links my team expects?

## Privacy

This tool runs **100% locally** on your machine. No telemetry. No accounts.

Outbound connections are only ever made to:

1. **GitHub API** — to scan your issues and PRs (uses `GITHUB_TOKEN`)
2. **Your LLM endpoint** — only when you open the AI assistant and send a message

API keys are stored in `.env` on your machine only. They are never logged or sent anywhere other than the endpoint you configure.

## What It Checks

- Missing Project V2 fields — `Estimate`, `Priority`, `Status`, `Iteration`, etc.
- Missing assignees.
- Items not linked to a project board (optional).
- Issues or PRs without a development link.
- Optional date range — last 30 days or custom `from / to`.
- One project, many projects, or all projects in an organization.
- One repo, many repos, or all organization repos.

## Quick Start

```sh
uv sync
copy .env.example .env
uv run streamlit run app.py
```

Open the Streamlit page, fill in the sidebar, then click **💾 Save settings to .env** to persist everything locally. Settings survive restarts — no login, no cookies.

## Sidebar Settings

| Section | Setting | What it does |
|---|---|---|
| GitHub Connection | Token | Classic PAT — scopes: `repo`, `read:org`, `read:project` |
| GitHub Connection | Organization | GitHub org slug, e.g. `my-company` |
| GitHub Connection | Project numbers | Project numbers or URLs; or enable "All org projects" |
| Checks to flag | Required project fields | Custom V2 field names that must be filled |
| Checks to flag | All rule toggles | Turn each check on/off individually |
| Scan Scope | Issues / PRs | Include or exclude each item type |
| Scan Scope | Include closed | Optionally scan closed issues/PRs |
| Time Range | Updated items | All time, last 30 days, or custom date range |
| Repository Scope | Allowlist / denylist | Limit or exclude specific repos |
| AI Assistant | Provider | `openai`, `azure`, `openai-compatible`, or `ollama` (local) |
| AI Assistant | Model / API key | Provider-specific model name and key |

Click **💾 Save settings to .env** (above Run Scan) to write all current sidebar values to `.env`. They load automatically on the next start.

## AI Assistant

The AI panel opens via the **AI** button in the top-right corner of the page.

### Local — no API key needed

```
LLM_PROVIDER=ollama
LLM_MODEL_NAME=llama3.2
```

Requires [Ollama](https://ollama.com) running locally (`ollama serve`). Data never leaves your machine.

### Cloud providers

| Provider | Key env var | Notes |
|---|---|---|
| `openai` | `LLM_API_KEY` | e.g. `gpt-4o` |
| `azure` | `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_API_VERSION` | Azure OpenAI deployment |
| `openai-compatible` | `LLM_API_KEY`, `LLM_BASE_URL` | Any OpenAI-compatible endpoint |

Configure in the sidebar and save, or set values in `.env` directly.

## GitHub Token

Create a classic GitHub personal access token.

Read-only scans need:

- `repo`
- `read:org`
- `read:project`

Writing suggested values back needs:

- `repo`
- `read:org`
- `project`

If your organization uses SAML SSO, authorize the token for the organization after creating it.

## Configuration Reference

All values can be set in `.env` or via the sidebar **Save** button.

| Variable | Meaning | Default |
|---|---|---|
| `GITHUB_TOKEN` | GitHub PAT | required |
| `GITHUB_ORG` | Organization name | required |
| `GITHUB_PROJECT_NUMBERS` | Comma-separated project numbers | — |
| `GITHUB_INCLUDE_ALL_PROJECTS` | Scan every org project | `false` |
| `GITHUB_INCLUDE_CLOSED_PROJECTS` | Include closed boards | `false` |
| `GITHUB_REPOSITORY_ALLOWLIST` | Repos to scan (comma-separated) | — |
| `GITHUB_INCLUDE_ALL_REPOSITORIES` | Scan every org repo | `false` |
| `GITHUB_REPOSITORY_DENYLIST` | Repos to always skip | — |
| `TARGET_ASSIGNEES` | Usernames to watch for target-assignee check | — |
| `REQUIRED_PROJECT_FIELDS` | Required V2 field names | — |
| `REQUIRE_PROJECT_ITEM` | Flag items missing from board | `false` |
| `REQUIRE_ASSIGNEE` | Flag unassigned items | `true` |
| `REQUIRE_DEVELOPMENT_LINK` | Flag items with no dev link | `true` |
| `REQUIRE_LINKED_PR_OR_BRANCH` | Stricter dev-link check | `true` |
| `INCLUDE_ISSUES` | Scan open issues | `true` |
| `INCLUDE_CLOSED_ISSUES` | Also scan closed issues | `false` |
| `INCLUDE_PULL_REQUESTS` | Scan open PRs | `true` |
| `INCLUDE_CLOSED_PULL_REQUESTS` | Also scan closed/merged PRs | `false` |
| `GITHUB_UPDATED_FROM` | `YYYY-MM-DD` lower bound | — |
| `GITHUB_UPDATED_TO` | `YYYY-MM-DD` upper bound | — |
| `LLM_ENABLED` | Enable AI features | `true` |
| `LLM_PROVIDER` | `openai`, `azure`, `openai-compatible`, `ollama` | `openai` |
| `LLM_MODEL_NAME` | Model ID | — |
| `LLM_API_KEY` | API key (not needed for Ollama) | — |
| `LLM_BASE_URL` | Custom endpoint URL | — |
| `LLM_API_VERSION` | Azure API version string | — |

## CLI

Streamlit is the easiest way to use the tool. CLI commands are still available:

```sh
uv run github-audit discover
uv run github-audit scan
uv run github-audit scan --markdown report.md
uv run github-audit scan --csv report.csv
uv run github-audit scan --json
uv run github-audit my-work
```

LLM-assisted suggestions:

```sh
uv run github-audit suggest
uv run github-audit apply --dry-run
uv run github-audit apply --yes
```

`my-work` shows all open issues and PRs assigned to `TARGET_ASSIGNEES` with project status — no project number required.

`apply --yes` writes only when `AUTO_APPLY=true` and confidence is high enough.

## Browser Mode

```sh
uv run github-audit browser-scan
```

Opens a temporary browser profile and scrapes the visible GitHub Project table. Use it when you cannot use a token. Read-only and limited to visible rows; token mode is required for complete paginated audits.

## Safety Notes

- Keep GitHub and LLM tokens in `.env` only — never commit them.
- `.env`, `.streamlit/secrets.toml`, and local override files are git-ignored.
- `.env.example` contains empty placeholders only.
- `scan`, `discover`, `suggest`, and `browser-scan` are read-only.
- `apply --dry-run` previews changes without writing.
- `apply --yes` writes to GitHub only when `AUTO_APPLY=true`.
- LLM base URLs are validated; cloud metadata endpoints (`169.254.x`) are blocked.

## Releases

No GitHub release has been published yet.

Before cutting a release, run:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Then create a GitHub release from a validated commit and tag it with the package version, e.g. `v0.1.0`.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## License

No license file is currently included. Without a license, the public repository is visible, but reuse rights are not granted by default.
