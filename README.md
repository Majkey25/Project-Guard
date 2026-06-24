# github-audit

Audits GitHub Issues and Pull Requests against required GitHub Project V2 metadata fields. Connects via the GitHub GraphQL API using a personal access token, then reports which items are missing fields like Estimate, Priority, or Status.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A GitHub personal access token (classic) with `repo`, `read:org`, `read:project` scopes

## Installation

```sh
uv sync
cp .env.example .env
# fill in GITHUB_TOKEN, GITHUB_ORG, GITHUB_PROJECT_NUMBER, TARGET_ASSIGNEES
```

## Usage

```sh
# Discover repositories and project structure
uv run github-audit discover

# Audit issues and PRs for missing fields
uv run github-audit scan

# Export results
uv run github-audit scan --markdown report.md
uv run github-audit scan --csv report.csv
uv run github-audit scan --json

# Get LLM suggestions for missing fields
uv run github-audit suggest

# Preview what apply would change (read-only)
uv run github-audit apply --dry-run

# Write suggested field values back to GitHub Projects
uv run github-audit apply --yes

# Scrape the project table via browser (no token required)
uv run github-audit browser-scan
```

## Configuration

All settings are read from `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Personal access token |
| `GITHUB_ORG` | Organization name |
| `GITHUB_PROJECT_NUMBER` | Project number from the URL |
| `GITHUB_PROJECT_NUMBERS` | Comma-separated list for multiple projects |
| `TARGET_ASSIGNEES` | GitHub usernames to audit |
| `REQUIRED_PROJECT_FIELDS` | Fields that must be set (comma-separated) |
| `REQUIRE_DEVELOPMENT_LINK` | Require a linked PR or branch |
| `LLM_PROVIDER` | `openai`, `openai-compatible`, or `azure` |
| `LLM_MODEL_NAME` | Model name passed to the provider |
| `LLM_API_KEY` | API key for the LLM provider |
| `AUTO_APPLY` | Set `true` to allow `apply --yes` to write |
| `AUTO_APPLY_MIN_CONFIDENCE` | Minimum LLM confidence to auto-apply (0–1) |

## Token scopes

Read-only (`discover`, `scan`, `suggest`, `apply --dry-run`): `repo`, `read:org`, `read:project`

Write (`apply --yes`): `repo`, `read:org`, `project`

If the organization requires SAML SSO, authorize the token for the org after generating it.

## Browser mode

`browser-scan` opens a temporary local browser profile, navigates to the project, and scrapes the visible table rows. No token needed. Sign in when the browser opens, navigate to the Project table view, then press Enter in the terminal.

Browser mode is read-only and limited to rows rendered on screen. Use token mode for complete paginated data.

## Safety

- `discover`, `scan`, `suggest`, and `apply --dry-run` are read-only.
- `apply --yes` only writes when `AUTO_APPLY=true` and the LLM confidence meets `AUTO_APPLY_MIN_CONFIDENCE`.
- Existing field values are never overwritten.
- LLM suggestions do not determine whether a field is missing — only the GraphQL data does.

## Development

```sh
uv run pytest
uv run pyright
uv run ruff check
```
