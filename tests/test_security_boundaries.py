from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import httpx
import pytest
from markdown_it import MarkdownIt
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from github_audit.api.access import MAX_CONCURRENT_REQUESTS, ApiAccess
from github_audit.config import Settings, write_env_keys
from github_audit.models import AuditResult
from github_audit.project_fields import parse_content
from github_audit.report import write_markdown
from github_audit.rules import evaluate_item


def test_scan_keeps_reviewed_pr_head_and_markdown_is_inert(tmp_path: Path) -> None:
    content = parse_content(
        {
            "__typename": "PullRequest",
            "id": "PR_1",
            "number": 1,
            "repository": {"nameWithOwner": "org/repo"},
            "title": '![Český obrázek](https://attacker.example/a) <img src="https://evil.example">',
            "url": "https://github.com/org/repo/pull/1",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "assignees": {"nodes": []},
            "labels": {"nodes": []},
            "closingIssuesReferences": {"totalCount": 0},
        }
    )
    assert content is not None
    settings = Settings.model_validate(
        {
            "github_token": "synthetic-token",
            "github_project_number": 1,
            "github_include_all_repositories": True,
            "require_target_assignee": False,
        }
    )
    finding = evaluate_item(content, None, settings)
    assert finding is not None and finding.head_oid == "a" * 40
    report = tmp_path / "report.md"
    write_markdown(
        report,
        AuditResult(
            organization="org",
            repositories=["org/repo"],
            findings=[finding],
            scanned_issue_count=0,
            scanned_pull_request_count=1,
        ),
    )
    rendered = MarkdownIt().render(report.read_text(encoding="utf-8"))
    assert "Český obrázek" in rendered
    assert "<img" not in rendered and "<a " not in rendered


def test_env_save_preserves_other_keys_and_restricts_posix_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('# retained\nOTHER="original"\nTOKEN="old"\n', encoding="utf-8")
    path.chmod(0o666)
    write_env_keys(path, {"TOKEN": "synthetic-token"})
    assert path.read_text(encoding="utf-8") == (
        '# retained\nOTHER="original"\nTOKEN="synthetic-token"\n'
    )
    assert list(tmp_path.glob(".env-*")) == []
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_env_save_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    path = tmp_path / ".env"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks requires a local OS privilege")
    with pytest.raises(ValueError, match="symlink"):
        write_env_keys(path, {"TOKEN": "synthetic-token"})
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_api_concurrency_limit_covers_whole_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_GUARD_API_TOKEN", raising=False)

    async def scenario() -> None:
        release = asyncio.Event()
        full = asyncio.Event()
        admitted = 0

        async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal admitted
            admitted += 1
            if admitted == MAX_CONCURRENT_REQUESTS:
                full.set()
            await release.wait()
            await PlainTextResponse("done")(scope, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ApiAccess(slow_app)), base_url="http://127.0.0.1"
        ) as client:
            pending = [
                asyncio.create_task(client.get("/chat")) for _ in range(MAX_CONCURRENT_REQUESTS)
            ]
            await asyncio.wait_for(full.wait(), timeout=5)
            assert (await client.get("/chat")).status_code == 429
            release.set()
            assert all(response.status_code == 200 for response in await asyncio.gather(*pending))
            assert (await client.get("/chat")).status_code == 200

    asyncio.run(scenario())
