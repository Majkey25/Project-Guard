from __future__ import annotations

import pytest

from github_audit.offline_export import OfflineColumn, render_offline_html

_COLUMNS = [
    OfflineColumn("repo", "Repository", filterable=True),
    OfflineColumn("state", "State", "badge", filterable=True),
    OfflineColumn("age", "Age", "number", min_filter=True),
    OfflineColumn("url", "Link", "link"),
]


def test_render_embeds_rows_and_columns() -> None:
    html = render_offline_html(
        "Title <x>",
        "Sub & title",
        _COLUMNS,
        [{"repo": "r1", "state": "Open", "age": 3, "url": "https://github.com/o/r"}],
    )
    assert "Title &lt;x&gt;" in html
    assert "Sub &amp; title" in html
    assert '"repo": "r1"' in html
    assert '"min_filter": true' in html


def test_render_neutralizes_script_terminator_in_data() -> None:
    html = render_offline_html(
        "T",
        "S",
        _COLUMNS,
        [{"repo": "</script><script>alert(1)</script>", "state": "", "age": 0, "url": ""}],
    )
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html


def test_render_rejects_row_missing_column() -> None:
    with pytest.raises(ValueError, match="missing column"):
        render_offline_html("T", "S", _COLUMNS, [{"repo": "r1"}])
