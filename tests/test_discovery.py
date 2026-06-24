from __future__ import annotations

from github_audit.project_fields import parse_field_nodes, parse_field_value_nodes


def test_parse_project_fields_options_and_iterations() -> None:
    fields = parse_field_nodes(
        [
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": "field-priority",
                "name": "Priority",
                "dataType": "SINGLE_SELECT",
                "options": [{"id": "p1", "name": "P1"}],
            },
            {
                "__typename": "ProjectV2IterationField",
                "id": "field-iteration",
                "name": "Iteration (sprint)",
                "dataType": "ITERATION",
                "configuration": {
                    "iterations": [{"id": "i1", "title": "Sprint 1"}],
                    "completedIterations": [],
                },
            },
        ]
    )
    assert fields[0].options == {"P1": "p1"}
    assert fields[1].iterations == {"Sprint 1": "i1"}


def test_parse_field_values_skips_unsupported_nodes_without_field() -> None:
    values = parse_field_value_nodes(
        [
            {"__typename": "ProjectV2ItemFieldRepositoryValue"},
            {
                "__typename": "ProjectV2ItemFieldNumberValue",
                "number": 3,
                "field": {
                    "__typename": "ProjectV2Field",
                    "id": "estimate",
                    "name": "Estimate",
                    "dataType": "NUMBER",
                },
            },
        ]
    )
    assert values["Estimate"].value == 3
