# GitHub Audit

Local tool for checking GitHub Issues and Pull Requests against your GitHub Project V2 workflow.

It answers one practical question:

> Which items are missing the fields or links my team expects?

## What It Checks

- Missing Project V2 fields, for example `Estimate`, `Priority`, `Status`, `Iteration`.
- Missing assignees or missing target assignees.
- Items missing from the selected project board, if you enable that check.
- Issues or PRs without a development link.
- Optional date range, for example last 30 days or custom `from` / `to`.
- One project, many projects, or all projects in an organization.
- One repo, many repos, or all organization repos.

## Quick Start

```sh
uv sync
copy .env.example .env
uv run streamlit run app.py
```

Then open the Streamlit page and fill in the sidebar form.

Most users only need:

| Setting | What to enter |
|---|---|
| GitHub token | Classic PAT with read scopes |
| Organization | GitHub org name, for example `OKsystem` |
| Projects | Project numbers/URLs, or enable all org projects |
| Repositories | Repo allowlist, or enable all org repositories |
| Accounts to watch | GitHub usernames to audit |
| Required fields | Project fields that must be filled |
| Checks to flag | Turn each rule on/off |
| Time range | All time, last 30 days, or custom range |

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

## Configuration

The app reads `.env` for defaults, but Streamlit form changes stay local to that browser session.

Useful `.env` values:

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT |
| `GITHUB_ORG` | Organization name |
| `GITHUB_PROJECT_NUMBER` | One project number |
| `GITHUB_PROJECT_NUMBERS` | Multiple project numbers |
| `GITHUB_INCLUDE_ALL_PROJECTS` | `true` scans all org Project V2 boards |
| `GITHUB_INCLUDE_CLOSED_PROJECTS` | `true` includes closed boards when scanning all projects |
| `GITHUB_REPOSITORY_ALLOWLIST` | Comma-separated repo names |
| `GITHUB_INCLUDE_ALL_REPOSITORIES` | `true` scans all non-archived org repos |
| `GITHUB_REPOSITORY_DENYLIST` | Repo names to skip |
| `TARGET_ASSIGNEES` | GitHub usernames to watch |
| `REQUIRED_PROJECT_FIELDS` | Required Project V2 fields |
| `REQUIRE_PROJECT_ITEM` | `true` flags repo items missing from selected project board |
| `GITHUB_UPDATED_FROM` | Optional `YYYY-MM-DD` lower bound |
| `GITHUB_UPDATED_TO` | Optional `YYYY-MM-DD` upper bound |

## CLI

Streamlit is the easiest way to use the tool. CLI commands are still available:

```sh
uv run github-audit discover
uv run github-audit scan
uv run github-audit scan --markdown report.md
uv run github-audit scan --csv report.csv
uv run github-audit scan --json
```

LLM-assisted suggestions:

```sh
uv run github-audit suggest
uv run github-audit apply --dry-run
uv run github-audit apply --yes
```

`apply --yes` writes only when `AUTO_APPLY=true` and confidence is high enough.

## Browser Mode

```sh
uv run github-audit browser-scan
```

This opens a temporary browser profile and scrapes the visible GitHub Project table.

Use it when you cannot use a token. It is read-only, but limited to visible rows. Token mode is required for complete paginated audits.

## Development

```sh
uv run ruff check .
uv run pyright
uv run pytest
```
