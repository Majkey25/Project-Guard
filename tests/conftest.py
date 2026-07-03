from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_llm_agent_cache() -> None:  # pyright: ignore[reportUnusedFunction]
    """Each test that patches pydantic_ai.Agent expects its own fresh Agent instance."""
    from github_audit.llm_evaluator import reset_agent_cache

    reset_agent_cache()


@pytest.fixture(autouse=True)
def _isolate_dotenv(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings loads ./.env relative to CWD; keep tests independent of the repo's live .env."""
    monkeypatch.chdir(tmp_path)
