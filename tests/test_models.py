from __future__ import annotations

import pytest
from pydantic import ValidationError

from github_audit.models import LLMSuggestion


def test_llm_suggestion_validates_confidence() -> None:
    with pytest.raises(ValidationError):
        LLMSuggestion(
            estimated_points=1,
            difficulty="M",
            priority="P2",
            missing_fields_summary=[],
            confidence=2,
            rationale="bad",
            should_auto_apply=False,
        )
