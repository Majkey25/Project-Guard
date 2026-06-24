from __future__ import annotations

from argparse import Namespace

from github_audit.cli import build_parser


def test_apply_dry_run_parse() -> None:
    args = build_parser().parse_args(["apply", "--dry-run"])
    assert isinstance(args, Namespace)
    assert args.command == "apply"
    assert args.dry_run is True
