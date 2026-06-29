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

## Status

- Repository: public GitHub repository.
- Current package version: `0.1.0`.
- Latest GitHub release: none published yet.
- License: no license file is included yet.
- CI: no GitHub Actions workflow is included yet; run the local checks below before release.

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

## Safety Notes

- Keep real GitHub and LLM tokens in local `.env` or the Streamlit session only.
- `.env`, `.streamlit/secrets.toml`, and local override files are ignored by git.
- `.env.example` contains empty placeholders only.
- `scan`, `discover`, `suggest`, and `browser-scan` are read-only.
- `apply --dry-run` previews changes.
- `apply --yes` can write to GitHub only when `AUTO_APPLY=true`.

## Releases

No GitHub release has been published yet.

Before cutting a release, run:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Then create a GitHub release from a validated commit and tag it with the package version, for example `v0.1.0`.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## License

No license file is currently included. Without a license, the public repository is visible, but reuse rights are not granted by default.
