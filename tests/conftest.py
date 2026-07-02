from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_llm_agent_cache() -> None:  # pyright: ignore[reportUnusedFunction]
    """Each test that patches pydantic_ai.Agent expects its own fresh Agent instance."""
    from github_audit.llm_evaluator import reset_agent_cache

    reset_agent_cache()
