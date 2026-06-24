from __future__ import annotations

from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from github_audit.config import load_settings


def test_load_settings_reports_missing_github_token(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="GITHUB_TOKEN is required"):
        load_settings()
