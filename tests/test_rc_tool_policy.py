"""Tests for the declarative tool policy (L2 execution layer)."""

from __future__ import annotations

import textwrap

import pytest

from researchclaw.experiment.tool_policy import (
    DEFAULT_CODEQL_BIN,
    ToolPolicy,
    subcommand_of,
)
from researchclaw.experiment.validator import validate_security


def test_default_policy_matches_historical_behaviour():
    policy = ToolPolicy.default()
    assert policy.allows([DEFAULT_CODEQL_BIN, "version"])[0] is True
    assert policy.allows(["codeql", "version"])[0] is False
    assert policy.allows(["pytest", "-q"])[0] is False
    assert policy.allows([])[0] is False


def test_subcommand_skips_flag_values():
    assert subcommand_of(["git", "-C", "/repo", "worktree", "add"]) == "worktree"
    assert subcommand_of(["/bin/git", "--no-pager", "diff"]) == "diff"
    assert subcommand_of(["codeql"]) == ""


def test_yaml_policy_allows_only_listed_subcommands(tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        textwrap.dedent(
            """
            version: 1
            default: deny
            tools:
              - name: git
                argv0: ["git", "/usr/bin/git"]
                subcommands: ["worktree", "diff"]
                value_flags: ["-C"]
              - name: codeql
                argv0: ["{codeql}"]
                subcommands: ["database", "analyze"]
            """
        ),
        encoding="utf-8",
    )
    policy = ToolPolicy.load(policy_file)

    assert policy.allows(["git", "-C", "/repo", "worktree", "add", "/dest"])[0] is True
    assert policy.allows(["/usr/bin/git", "diff", "--name-only"])[0] is True
    assert policy.allows(["git", "push", "origin", "main"])[0] is False
    assert policy.allows([DEFAULT_CODEQL_BIN, "database", "create"])[0] is True
    assert policy.allows([DEFAULT_CODEQL_BIN, "bqrs", "decode"])[0] is False
    assert policy.allows(["rm", "-rf", "/"])[0] is False


def test_missing_policy_fails_closed(tmp_path):
    policy = ToolPolicy.load(tmp_path / "does-not-exist.yaml")
    assert policy.source == "default"
    assert policy.allows(["pytest"])[0] is False
    assert policy.allows([DEFAULT_CODEQL_BIN, "version"])[0] is True


def test_validator_widens_only_when_a_policy_is_configured(tmp_path, monkeypatch):
    code = "import subprocess\nsubprocess.run(['git', 'worktree', 'list'], shell=False)\n"
    monkeypatch.delenv("RESEARCHCLAW_TOOL_POLICY", raising=False)
    assert validate_security(code).ok is False

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        textwrap.dedent(
            """
            version: 1
            tools:
              - name: git
                argv0: ["git"]
                subcommands: ["worktree"]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RESEARCHCLAW_TOOL_POLICY", str(policy_file))
    assert validate_security(code).ok is True

    rejected = "import subprocess\nsubprocess.run(['git', 'push', 'origin', 'main'], shell=False)\n"
    assert validate_security(rejected).ok is False


def test_validator_still_requires_shell_false(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        'version: 1\ntools:\n  - name: git\n    argv0: ["git"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("RESEARCHCLAW_TOOL_POLICY", str(policy_file))
    code = "import subprocess\nsubprocess.run(['git', 'worktree', 'list'], shell=True)\n"
    assert validate_security(code).ok is False


def test_non_literal_argv_stays_rejected(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        'version: 1\ntools:\n  - name: git\n    argv0: ["git"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("RESEARCHCLAW_TOOL_POLICY", str(policy_file))
    code = "import subprocess\ncmd = ['git']\nsubprocess.run(cmd, shell=False)\n"
    assert validate_security(code).ok is False
