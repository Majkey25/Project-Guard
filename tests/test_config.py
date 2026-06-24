from __future__ import annotations

from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from github_audit.config import Settings, split_csv


def test_split_csv_trims_empty_parts() -> None:
    assert split_csv("a, b,,c ") == ["a", "b", "c"]


def test_settings_parse_scope(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo-one,repo-two",
            "target_assignees_raw": "alice,bob",
        }
    )
    assert settings.repository_allowlist == ["repo-one", "repo-two"]
    assert settings.target_assignees == ["alice", "bob"]
    assert settings.required_project_fields == [
        "Estimate",
        "Iteration (sprint)",
        "Priority",
        "Difficulty",
        "Status",
    ]


def test_settings_requires_repository_scope(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="GITHUB_REPOSITORY_ALLOWLIST"):
        Settings.model_validate(
            {
                "github_token": "token",
                "github_project_number": 1,
                "target_assignees_raw": "alice",
            }
        )


def test_settings_accepts_azure_llm_aliases(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_api_key": "key",
            "llm_base_url": "https://example.openai.azure.com/",
            "llm_model_name": "deployment",
            "llm_api_version": "2024-02-01",
        }
    )
    assert settings.llm_provider_name == "azure"
