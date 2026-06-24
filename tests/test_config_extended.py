from __future__ import annotations

from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from github_audit.config import Settings, split_int_csv


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "github_token": "token",
        "github_project_number": 1,
        "github_project_numbers_raw": "",  # don't let .env GITHUB_PROJECT_NUMBERS bleed in
        "github_repository_allowlist_raw": "repo",
        "target_assignees_raw": "alice",
        "llm_provider": "",  # don't inherit LLM_PROVIDER=azure from .env
        "llm_base_url": "",
        "llm_api_version": "",
    }
    base.update(overrides)
    return Settings.model_validate(base)


# ── split_int_csv ─────────────────────────────────────────────────────────────


def test_split_int_csv_valid() -> None:
    assert split_int_csv("1,2,3", "X") == [1, 2, 3]


def test_split_int_csv_rejects_zero() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        split_int_csv("0", "X")


def test_split_int_csv_rejects_non_integer() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        split_int_csv("abc", "X")


# ── project_numbers property ─────────────────────────────────────────────────


def test_project_numbers_from_single_number() -> None:
    settings = _settings()
    assert settings.github_project_numbers == [1]


def test_project_numbers_from_csv() -> None:
    settings = _settings(
        github_project_number=0,
        github_project_numbers_raw="10,20",
    )
    assert settings.github_project_numbers == [10, 20]


def test_project_numbers_csv_overrides_single() -> None:
    settings = _settings(github_project_numbers_raw="5,6")
    assert settings.github_project_numbers == [5, 6]


# ── repository lists ──────────────────────────────────────────────────────────


def test_repository_denylist_parsed() -> None:
    settings = _settings(github_repository_denylist_raw="foo, bar")
    assert settings.repository_denylist == ["foo", "bar"]


def test_optional_project_fields_parsed() -> None:
    settings = _settings(optional_project_fields_raw="Note,Start date")
    assert settings.optional_project_fields == ["Note", "Start date"]


# ── llm_provider_name ─────────────────────────────────────────────────────────


def test_llm_provider_name_defaults_to_openai() -> None:
    settings = _settings()
    assert settings.llm_provider_name == "openai"


def test_llm_provider_name_azure_from_api_version() -> None:
    settings = _settings(llm_api_version="2024-02-01")
    assert settings.llm_provider_name == "azure"


def test_llm_provider_name_azure_from_url() -> None:
    settings = _settings(llm_base_url="https://my.openai.azure.com/")
    assert settings.llm_provider_name == "azure"


def test_llm_provider_name_explicit_override() -> None:
    settings = _settings(llm_provider="openai-compatible")
    assert settings.llm_provider_name == "openai-compatible"


# ── validate_llm ─────────────────────────────────────────────────────────────


def test_validate_llm_raises_when_disabled() -> None:
    settings = _settings(llm_enabled=False)
    with pytest.raises(ValueError, match="LLM_ENABLED=false"):
        settings.validate_llm()


def test_validate_llm_raises_when_no_model() -> None:
    settings = _settings(llm_api_key="key", llm_model_name="")
    with pytest.raises(ValueError, match="LLM_MODEL_NAME is required"):
        settings.validate_llm()


def test_validate_llm_raises_when_no_api_key() -> None:
    settings = _settings(llm_model_name="gpt-4", llm_api_key="")
    with pytest.raises(ValueError, match="LLM_API_KEY is required"):
        settings.validate_llm()


def test_validate_llm_passes_with_all_set() -> None:
    settings = _settings(llm_model_name="gpt-4", llm_api_key="key")
    settings.validate_llm()  # must not raise


# ── validation errors ─────────────────────────────────────────────────────────


def test_settings_requires_project_number(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="GITHUB_PROJECT_NUMBER"):
        Settings.model_validate(
            {
                "github_token": "token",
                "github_repository_allowlist_raw": "repo",
                "target_assignees_raw": "alice",
            }
        )


def test_settings_requires_target_assignees_when_flag_set(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="TARGET_ASSIGNEES"):
        Settings.model_validate(
            {
                "github_token": "token",
                "github_project_number": 1,
                "github_repository_allowlist_raw": "repo",
                "require_target_assignee": True,
                "target_assignees_raw": "",
            }
        )


def test_settings_requires_at_least_one_item_type(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="at least one"):
        Settings.model_validate(
            {
                "github_token": "token",
                "github_project_number": 1,
                "github_repository_allowlist_raw": "repo",
                "target_assignees_raw": "alice",
                "include_issues": False,
                "include_pull_requests": False,
            }
        )


def test_settings_negative_project_number_rejected() -> None:
    with pytest.raises(Exception):
        _settings(github_project_number=-1)


def test_settings_invalid_confidence_rejected() -> None:
    with pytest.raises(Exception):
        _settings(auto_apply_min_confidence=1.5)


def test_settings_invalid_timeout_rejected() -> None:
    with pytest.raises(Exception):
        _settings(llm_timeout_seconds=-1)
