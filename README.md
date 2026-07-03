# GitHub Audit

[![Repository](https://img.shields.io/badge/repository-public-brightgreen)](https://github.com/Majkey25/Project-Guard)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![uv](https://img.shields.io/badge/package%20manager-uv-black)](https://docs.astral.sh/uv/)
[![Streamlit](https://img.shields.io/badge/ui-Streamlit-ff4b4b)](https://streamlit.io/)
[![Ruff](https://img.shields.io/badge/lint-Ruff-46aef7)](https://docs.astral.sh/ruff/)
[![Pyright](https://img.shields.io/badge/types-Pyright%20strict-yellow)](https://github.com/microsoft/pyright)
[![Tests](https://img.shields.io/badge/tests-pytest-blueviolet)](https://pytest.org/)
[![CI](https://github.com/Majkey25/Project-Guard/actions/workflows/ci.yml/badge.svg)](https://github.com/Majkey25/Project-Guard/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Majkey25/Project-Guard?label=release)](https://github.com/Majkey25/Project-Guard/releases)
[![License](https://img.shields.io/badge/license-PolyForm%20Strict%201.0.0-blue)](LICENSE)

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

## Setup Guide — Step by Step

You don't need any programming experience. The whole setup is: install one helper
program, download this app, start it, and create a GitHub token so the app can
read your projects. About 10 minutes.

### Step 1 — Install `uv` (one time only)

`uv` is a small helper that downloads everything the app needs (including Python).

1. Press the **Windows key**, type `powershell`, press **Enter**. A blue/black text window opens.
2. Copy this line, paste it into that window (right-click pastes), press **Enter**:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. When it finishes, **close the window** (the next step needs a fresh one).

On macOS or Linux use: `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Step 2 — Download the app

No git needed:

1. Open <https://github.com/Majkey25/Project-Guard> in your browser.
2. Click the green **Code** button → **Download ZIP**.
3. Find the downloaded `Project-Guard-main.zip`, right-click it → **Extract All…**
   and extract it into your `Documents` folder.

(If you know git: `git clone https://github.com/Majkey25/Project-Guard.git` works too —
then your folder is called `Project-Guard` instead of `Project-Guard-main`.)

### Step 3 — Start the app

1. Open a **new** PowerShell window (Windows key → `powershell` → Enter).
2. Paste these two lines and press **Enter**:

   ```powershell
   cd $HOME\Documents\Project-Guard-main
   uv run streamlit run app.py
   ```

3. The **first start takes a minute or two** — `uv` downloads Python and all
   dependencies automatically. After that, your browser opens the app at
   <http://localhost:8501>.

Keep the PowerShell window open while you use the app — closing it stops the app.
Next time, only Step 3 is needed, and it starts in seconds.

### Step 4 — Create your GitHub token (PAT)

The app needs a "personal access token" — a kind of password that lets it read
your GitHub projects. You create it once on the GitHub website:

1. Go to <https://github.com/settings/tokens> (sign in if asked).
   *(That's: your profile picture → **Settings** → **Developer settings** →
   **Personal access tokens** → **Tokens (classic)**.)*
2. Click **Generate new token** → **Generate new token (classic)**.
3. **Note**: type something like `GitHub Audit`. **Expiration**: e.g. 90 days.
4. Tick these checkboxes ("scopes"):
   - `repo`
   - `read:org`
   - `project` *(needed if you want the AI assistant to write field values;
     for read-only scanning `read:project` is enough)*
5. Click **Generate token** at the bottom and **copy the token immediately**
   (it looks like `ghp_...` and is shown only once). Treat it like a password.
6. If your organization uses SSO/SAML sign-in: on the token list page click
   **Configure SSO** next to the new token and authorize it for your organization.

### Step 5 — First scan

Back in the app in your browser:

1. In the left sidebar, open **🔑 GitHub Connection** and paste your token.
2. Fill in your **Organization** name (the part after `github.com/` in your org's URL).
3. Either tick **All org projects**, or enter your project numbers/URLs.
4. Click **💾 Save settings to .env** — settings are stored locally and survive restarts.
5. Click **▶ Run Scan**. Results appear as a table you can filter and download as Excel.

### HTTP API (optional, for developers)

```sh
uv run github-audit-api --host 127.0.0.1 --port 8010
```

API URL at `http://127.0.0.1:8010/chat`. The endpoint accepts `POST /chat?stream=true` with `prompt` or `message`, streams `data: {"delta": ...}` chunks for general chat, sends a final JSON payload, then `data: [DONE]`. `GET /status` and `GET /context` are also available.

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
| AI Assistant | Allow GitHub writes | Lets the assistant apply queued changes (`AUTO_APPLY`) |

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

### What the assistant can write

Select an issue or PR in the AI panel dropdown — or just reference it in your message,
e.g. `add estimate 5 to #123`. The assistant can queue: adding the item to the project
board, Project V2 field updates, new comments, title/body edits, label add/remove,
assignee add/remove, close/reopen (with an optional reason for issues), setting or
clearing the milestone, merging a pull request, and requesting PR reviewers. If the item
isn't on the board yet, ask for the field changes anyway — it queues the board add first
and the field updates run right after it. Every write is a **preview only** — nothing touches GitHub until
you confirm with `apply it`, and **Allow GitHub writes (AUTO_APPLY)** must be enabled in
the sidebar (⚙️ Config → 🧠 AI Assistant). Not yet supported: creating new issues/PRs,
editing or deleting existing comments, changing a PR's base branch, and
draft/ready-for-review toggling.

## GitHub Token Scopes

Creation walkthrough: see [Step 4 above](#step-4--create-your-github-token-pat).

- Read-only scans: `repo`, `read:org`, `read:project`
- Writing values back (AI assistant / `apply`): `repo`, `read:org`, `project`

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
| `REQUIRE_PROJECT_ITEM` | Flag issues missing from board | `true` |
| `REQUIRE_PROJECT_ITEM_PULL_REQUESTS` | Also require a board item for PRs | `false` |
| `REQUIRE_ASSIGNEE` | Flag unassigned items | `true` |
| `REQUIRE_DEVELOPMENT_LINK` | Flag items with no dev link | `true` |
| `REQUIRE_LINKED_PR_OR_BRANCH` | Stricter dev-link check | `true` |
| `INCLUDE_ISSUES` | Scan open issues | `true` |
| `INCLUDE_CLOSED_ISSUES` | Also scan closed issues | `false` |
| `INCLUDE_PULL_REQUESTS` | Scan open PRs | `true` |
| `INCLUDE_CLOSED_PULL_REQUESTS` | Also scan closed/merged PRs | `false` |
| `GITHUB_UPDATED_FROM` | `YYYY-MM-DD` lower bound | — |
| `GITHUB_UPDATED_TO` | `YYYY-MM-DD` upper bound | — |
| `AUTO_APPLY` | Allow the AI assistant and `apply` to write to GitHub | `false` |
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

[PolyForm Strict License 1.0.0](LICENSE) — free for noncommercial use.
Commercial use, redistribution, and selling are not permitted.
