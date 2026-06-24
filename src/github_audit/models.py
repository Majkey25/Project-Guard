from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ItemType = Literal["issue", "pull_request"]
FieldKind = Literal["field", "single_select", "iteration"]
ProjectContentType = ItemType | Literal["draft_issue", "redacted", "unknown"]


class ProjectFieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    data_type: str
    kind: FieldKind
    options: dict[str, str] = Field(default_factory=dict)
    iterations: dict[str, str] = Field(default_factory=dict)


class ProjectFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    field_name: str
    value: str | int | float | bool
    option_id: str | None = None
    iteration_id: str | None = None


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content_id: str | None
    content_type: ProjectContentType
    repository: str | None
    number: int | None
    title: str
    url: str | None
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    milestone: str | None = None
    field_values: dict[str, ProjectFieldValue] = Field(default_factory=dict)
    linked_pull_requests_count: int = 0
    closing_issues_count: int = 0


class GitHubIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repository: str
    number: int
    title: str
    url: str
    state: str
    body: str
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    milestone: str | None = None
    linked_pull_requests_count: int = 0


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repository: str
    number: int
    title: str
    url: str
    state: str
    body: str
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    milestone: str | None = None
    closing_issues_count: int = 0


GitHubContent = GitHubIssue | GitHubPullRequest


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: str
    project_id: str
    project_number: int
    project_title: str
    project_url: str
    repositories: list[str]
    fields: list[ProjectFieldDefinition]
    required_fields_missing: list[str]
    issue_sample_count: int
    pull_request_sample_count: int
    project_item_sample_count: int
    content_types: list[str]
    development_strategy: str
    development_limitations: list[str]


class LLMSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_points: int | None = Field(default=None, ge=0)
    difficulty: str | None = None
    priority: str | None = None
    suggested_iteration: str | None = None
    missing_fields_summary: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    should_auto_apply: bool


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_number: int | None = None
    project_title: str | None = None
    repository: str
    item_type: ItemType
    number: int
    title: str
    url: str
    assignees: list[str]
    missing_fields: list[str]
    current_project_fields: dict[str, str] = Field(default_factory=dict)
    development_status: str
    project_item_id: str | None = None
    llm_suggestion: LLMSuggestion | None = None
    apply_status: str = "not_planned"


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: str
    project_number: int | None = None
    project_title: str | None = None
    repositories: list[str]
    findings: list[AuditFinding]
    scanned_issue_count: int
    scanned_pull_request_count: int
    limitations: list[str] = Field(default_factory=list)


class ApplyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    item_type: ItemType
    number: int
    project_item_id: str
    field_name: str
    value: str | int | float | bool
    option_id: str | None = None
    iteration_id: str | None = None


class ApplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[ApplyChange]
    skipped: list[str] = Field(default_factory=list)


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    applied: list[ApplyChange]
    skipped: list[str] = Field(default_factory=list)


class BrowserProjectFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_label: str
    missing_fields: list[str]
    cells: list[str]


class BrowserScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    signed_in: bool
    headers: list[str]
    findings: list[BrowserProjectFinding]
    missing_headers: list[str]
    limitations: list[str] = Field(default_factory=list)
