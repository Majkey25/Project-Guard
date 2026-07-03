from __future__ import annotations

import re
from datetime import date
from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}$")
_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def split_int_csv(value: str, label: str) -> list[int]:
    numbers: list[int] = []
    for part in split_csv(value):
        try:
            number = int(part)
        except ValueError as exc:
            msg = f"{label} must contain positive integers"
            raise ValueError(msg) from exc
        if number <= 0:
            msg = f"{label} must contain positive integers"
            raise ValueError(msg)
        numbers.append(number)
    return numbers


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    github_token: str = Field(default="", validation_alias="GITHUB_TOKEN")
    github_org: str = Field(default="OKsystem", validation_alias="GITHUB_ORG")
    github_project_number: int = Field(default=0, validation_alias="GITHUB_PROJECT_NUMBER")
    github_project_numbers_raw: str = Field(default="", validation_alias="GITHUB_PROJECT_NUMBERS")
    github_include_all_projects: bool = Field(
        default=False, validation_alias="GITHUB_INCLUDE_ALL_PROJECTS"
    )
    github_include_closed_projects: bool = Field(
        default=False, validation_alias="GITHUB_INCLUDE_CLOSED_PROJECTS"
    )
    github_project_owner_type: Literal["org"] = Field(
        default="org", validation_alias="GITHUB_PROJECT_OWNER_TYPE"
    )
    github_repository_allowlist_raw: str = Field(
        default="", validation_alias="GITHUB_REPOSITORY_ALLOWLIST"
    )
    github_repository_denylist_raw: str = Field(
        default="", validation_alias="GITHUB_REPOSITORY_DENYLIST"
    )
    github_include_all_repositories: bool = Field(
        default=False, validation_alias="GITHUB_INCLUDE_ALL_REPOSITORIES"
    )
    target_assignees_raw: str = Field(default="", validation_alias="TARGET_ASSIGNEES")

    required_project_fields_raw: str = Field(
        default="Estimate,Iteration (sprint),Priority,Difficulty,Status",
        validation_alias="REQUIRED_PROJECT_FIELDS",
    )
    optional_project_fields_raw: str = Field(
        default="Note,Start date,End date", validation_alias="OPTIONAL_PROJECT_FIELDS"
    )

    require_assignee: bool = Field(default=True, validation_alias="REQUIRE_ASSIGNEE")
    require_target_assignee: bool = Field(default=True, validation_alias="REQUIRE_TARGET_ASSIGNEE")
    require_project_item: bool = Field(default=True, validation_alias="REQUIRE_PROJECT_ITEM")
    require_project_item_pull_requests: bool = Field(
        default=False, validation_alias="REQUIRE_PROJECT_ITEM_PULL_REQUESTS"
    )
    require_development_link: bool = Field(
        default=True, validation_alias="REQUIRE_DEVELOPMENT_LINK"
    )
    require_linked_pr_or_branch: bool = Field(
        default=True, validation_alias="REQUIRE_LINKED_PR_OR_BRANCH"
    )

    include_closed_issues: bool = Field(default=False, validation_alias="INCLUDE_CLOSED_ISSUES")
    include_closed_pull_requests: bool = Field(
        default=False, validation_alias="INCLUDE_CLOSED_PULL_REQUESTS"
    )
    include_pull_requests: bool = Field(default=True, validation_alias="INCLUDE_PULL_REQUESTS")
    include_issues: bool = Field(default=True, validation_alias="INCLUDE_ISSUES")
    include_unassigned: bool = Field(default=False, validation_alias="INCLUDE_UNASSIGNED")
    github_updated_from: date | None = Field(default=None, validation_alias="GITHUB_UPDATED_FROM")
    github_updated_to: date | None = Field(default=None, validation_alias="GITHUB_UPDATED_TO")

    my_work_mode: bool = Field(default=False, validation_alias="MY_WORK_MODE")
    auto_apply: bool = Field(default=False, validation_alias="AUTO_APPLY")
    auto_apply_min_confidence: float = Field(
        default=0.85, validation_alias="AUTO_APPLY_MIN_CONFIDENCE"
    )

    llm_enabled: bool = Field(default=True, validation_alias="LLM_ENABLED")
    llm_provider: str = Field(default="", validation_alias="LLM_PROVIDER")
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_KEY", "AZURE_API_KEY")
    )
    llm_base_url: str = Field(
        default="", validation_alias=AliasChoices("LLM_BASE_URL", "AZURE_API_BASE")
    )
    llm_model_name: str = Field(
        default="", validation_alias=AliasChoices("LLM_MODEL_NAME", "AZURE_LLM_MODEL_NAME")
    )
    llm_api_version: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_VERSION", "AZURE_API_VERSION")
    )
    llm_timeout_seconds: int = Field(default=30, validation_alias="LLM_TIMEOUT_SECONDS")

    @field_validator("github_token", "github_org")
    @classmethod
    def not_blank(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            labels: dict[str, str] = {
                "github_token": "GITHUB_TOKEN is required",
                "github_org": "GITHUB_ORG is required",
            }
            msg = labels.get(info.field_name or "", "value must not be empty")
            raise ValueError(msg)
        return value.strip()

    @field_validator("llm_api_key", "llm_model_name", "llm_api_version")
    @classmethod
    def strip_llm_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            msg = "LLM_BASE_URL is not a valid URL"
            raise ValueError(msg) from exc
        if parsed.scheme not in {"https", "http"}:
            msg = "LLM_BASE_URL must use http or https scheme"
            raise ValueError(msg)
        host = parsed.hostname or ""
        # Block cloud metadata endpoints and RFC 1918 ranges to prevent SSRF.
        _SSRF_BLOCKED = ("169.254.", "::1", "0.0.0.0")
        _SSRF_BLOCKED_HOSTS = {"metadata.google.internal"}
        if any(host.startswith(p) for p in _SSRF_BLOCKED) or host in _SSRF_BLOCKED_HOSTS:
            msg = f"LLM_BASE_URL host {host!r} is not permitted"
            raise ValueError(msg)
        return value

    @field_validator("llm_model_name")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        value = value.strip()
        if value and not re.match(r"^[a-zA-Z0-9._:\-/]{1,100}$", value):
            msg = "LLM_MODEL_NAME contains invalid characters"
            raise ValueError(msg)
        return value

    @field_validator("target_assignees_raw")
    @classmethod
    def validate_assignees(cls, value: str) -> str:
        for part in split_csv(value):
            if not _USERNAME_RE.match(part):
                msg = f"Invalid GitHub username: {part!r} (only letters, digits, hyphens allowed)"
                raise ValueError(msg)
        if len(split_csv(value)) > 50:
            msg = "TARGET_ASSIGNEES: maximum 50 accounts"
            raise ValueError(msg)
        return value

    @field_validator("github_repository_allowlist_raw", "github_repository_denylist_raw")
    @classmethod
    def validate_repo_names(cls, value: str) -> str:
        for part in split_csv(value):
            name = part.split("/")[-1]  # accept bare name or org/name
            if not _REPO_NAME_RE.match(name):
                msg = f"Invalid repository name: {part!r}"
                raise ValueError(msg)
        return value

    @field_validator("github_project_number")
    @classmethod
    def non_negative_project_number(cls, value: int) -> int:
        if value < 0:
            msg = "GITHUB_PROJECT_NUMBER must be positive"
            raise ValueError(msg)
        return value

    @field_validator("auto_apply_min_confidence")
    @classmethod
    def confidence_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            msg = "AUTO_APPLY_MIN_CONFIDENCE must be between 0 and 1"
            raise ValueError(msg)
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: int) -> int:
        if value <= 0:
            msg = "LLM_TIMEOUT_SECONDS must be positive"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_repository_scope(self) -> Settings:
        if (
            not self.my_work_mode
            and not self.github_include_all_projects
            and not self.github_project_numbers
        ):
            msg = "GITHUB_PROJECT_NUMBER or GITHUB_PROJECT_NUMBERS is required"
            raise ValueError(msg)
        if not self.github_include_all_repositories and not self.repository_allowlist:
            msg = "set GITHUB_REPOSITORY_ALLOWLIST or GITHUB_INCLUDE_ALL_REPOSITORIES=true"
            raise ValueError(msg)
        if self.require_target_assignee and not self.target_assignees:
            msg = "TARGET_ASSIGNEES is required when REQUIRE_TARGET_ASSIGNEE=true"
            raise ValueError(msg)
        if not self.include_issues and not self.include_pull_requests:
            msg = "at least one of INCLUDE_ISSUES or INCLUDE_PULL_REQUESTS must be true"
            raise ValueError(msg)
        if (
            self.github_updated_from is not None
            and self.github_updated_to is not None
            and self.github_updated_from > self.github_updated_to
        ):
            msg = "GITHUB_UPDATED_FROM must be before or equal to GITHUB_UPDATED_TO"
            raise ValueError(msg)
        return self

    @property
    def repository_allowlist(self) -> list[str]:
        return split_csv(self.github_repository_allowlist_raw)

    @property
    def repository_denylist(self) -> list[str]:
        return split_csv(self.github_repository_denylist_raw)

    @property
    def github_project_numbers(self) -> list[int]:
        numbers = split_int_csv(self.github_project_numbers_raw, "GITHUB_PROJECT_NUMBERS")
        if numbers:
            return numbers
        return [self.github_project_number] if self.github_project_number > 0 else []

    @property
    def target_assignees(self) -> list[str]:
        return split_csv(self.target_assignees_raw)

    @property
    def required_project_fields(self) -> list[str]:
        return split_csv(self.required_project_fields_raw)

    @property
    def optional_project_fields(self) -> list[str]:
        return split_csv(self.optional_project_fields_raw)

    @property
    def llm_provider_name(self) -> str:
        provider = self.llm_provider.strip().lower()
        if provider:
            return provider
        if self.llm_api_version or "azure" in self.llm_base_url.lower():
            return "azure"
        return "openai"

    def validate_llm(self) -> None:
        if not self.llm_enabled:
            msg = "LLM_ENABLED=false"
            raise ValueError(msg)
        if not self.llm_model_name.strip():
            msg = "LLM_MODEL_NAME is required for suggest/apply suggestions"
            raise ValueError(msg)
        # Local providers (ollama) don't need an API key
        _LOCAL_PROVIDERS = {"ollama"}
        if self.llm_provider_name not in _LOCAL_PROVIDERS and not self.llm_api_key.strip():
            msg = "LLM_API_KEY is required for suggest/apply suggestions"
            raise ValueError(msg)


def load_settings(*, my_work_mode: bool = False) -> Settings:
    import os

    if my_work_mode:
        os.environ["MY_WORK_MODE"] = "true"
    try:
        return Settings()
    except ValidationError as exc:
        errors = "; ".join(error["msg"] for error in exc.errors())
        msg = f"Invalid configuration: {errors}"
        raise ValueError(msg) from exc
    finally:
        if my_work_mode:
            os.environ.pop("MY_WORK_MODE", None)
