# GitHub Audit

Python CLI that audits GitHub Issues and Pull Requests for required GitHub Project V2 metadata.

## Setup

```powershell
uv sync
Copy-Item .env.example .env
```

Fill `.env`. Do not commit it.

GitHub token mode needs a personal access token. Browser mode is available, but it only sees rendered table rows and is not a complete Project V2 audit.
Azure LLM variables are supported through either `LLM_*` names or existing `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION`, and `AZURE_LLM_MODEL_NAME` names.

## GitHub Token

For complete `discover`, `scan`, `suggest`, and `apply --dry-run`, create a GitHub personal access token (classic):

1. Open GitHub.
2. Go to profile picture -> Settings -> Developer settings.
3. Open Personal access tokens -> Tokens (classic).
4. Click Generate new token -> Generate new token (classic).
5. Set an expiration.
6. Select scopes.
7. Generate token and copy it once.
8. If OKsystem requires SAML SSO, authorize the token for the organization.

Required classic scopes for read-only audit:

- `repo` -> access private repositories, issues, and pull requests.
- `read:org` -> read organization context.
- `read:project` -> read GitHub Projects V2 through GraphQL.

Required classic scopes for `apply --yes`:

- `repo`
- `read:org`
- `project` instead of `read:project`

Put these values in `.env`:

```env
GITHUB_TOKEN=ghp_...
GITHUB_ORG=OKsystem
GITHUB_PROJECT_NUMBER=123
# Or multiple projects:
GITHUB_PROJECT_NUMBERS=123,456
```

`GITHUB_PROJECT_NUMBER` is the number in the project URL. Example: `https://github.com/orgs/OKsystem/projects/123`.
Use `GITHUB_INCLUDE_ALL_REPOSITORIES=true` for every non-archived repository in the org. Do not put `all` in `GITHUB_REPOSITORY_ALLOWLIST`.

Docs:

- GitHub PAT creation: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub Projects API scopes: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects

## Commands

```powershell
uv run github-audit discover
uv run github-audit discover --markdown discovery.md
uv run github-audit scan
uv run github-audit scan --json
uv run github-audit scan --markdown report.md
uv run github-audit scan --csv report.csv
uv run github-audit suggest
uv run github-audit apply --dry-run
uv run github-audit apply --yes
uv run github-audit browser-scan
```

## Safety

- `discover`, `scan`, and `suggest` are read-only.
- `apply --dry-run` is read-only.
- `apply --yes` writes only when `AUTO_APPLY=true` and suggestion confidence is at least `AUTO_APPLY_MIN_CONFIDENCE`.
- Existing Project V2 field values are not overwritten.
- Field IDs, single-select option IDs, and iteration IDs are discovered dynamically.
- LLM suggestions never decide whether a field is missing.

## Browser Login Mode

Use this when you do not want to put a GitHub token in `.env`:

```powershell
uv run github-audit browser-scan
```

The CLI opens a local browser window. Sign in to GitHub, open the Project table view, then press Enter in the terminal. Browser mode uses a temporary local browser profile and deletes it when the command exits. No GitHub cookie, token, or browser state is written to this repository.

Browser mode is read-only and only scrapes rows/columns visible in the GitHub web table. Use token mode with `read:project` for complete paginated Project V2 data.

## Development Links

GitHub documents issue and pull request linking through closing keywords and the Development sidebar. The CLI uses GraphQL closing references where available. Branch links are reported as a discovery limitation unless the API proves reliable access in the current token/project context.

Docs checked:

- GitHub Projects GraphQL API: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
- GitHub Project V2 GraphQL reference: https://docs.github.com/en/graphql/reference/projects
- GitHub issue and PR linking: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
- Pydantic AI structured output: https://pydantic.dev/docs/ai/core-concepts/output/
