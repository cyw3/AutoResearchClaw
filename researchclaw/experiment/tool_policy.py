"""Declarative tool policy for experiment subprocesses.

The **default** policy reproduces the historical validator behaviour: only the
bundled CodeQL binary may be launched. A YAML policy file
(``RESEARCHCLAW_TOOL_POLICY``) can widen this to git/pytest for real-toolchain
experiments without changing the default for every other project.

Matching is by exact executable path. A bare name (``"git"``) only matches when
the policy itself lists it — so the default policy never accepts
``subprocess.run(["codeql", ...])``.

``writable`` is advisory: the AST validator cannot enforce filesystem writes,
container isolation remains the enforcement point. It is recorded so the same
policy file can drive sandbox mounts later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# Kept in sync with researchclaw/experiment/validator.py (_DEFAULT_CODEQL_BIN).
DEFAULT_CODEQL_BIN = (
    "/data/research/AutoResearchClaw/projects/python-taint/tools/codeql/codeql"
)

# Flags whose following token is a value; skipped when locating a subcommand.
VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "-o",
        "--output",
        "--source-root",
        "-l",
        "--language",
        "-j",
        "--threads",
        "--ram",
        "--format",
        "-d",
        "--database",
    }
)


@dataclass(frozen=True)
class Rule:
    name: str
    argv0: tuple[str, ...]
    subcommands: tuple[str, ...] = ()
    value_flags: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()


def subcommand_of(argv: list[str] | tuple[str, ...], extra_value_flags: tuple[str, ...] = ()) -> str:
    """Return the first command-looking token after argv[0], or ``""``."""
    value_flags = VALUE_FLAGS | set(extra_value_flags)
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in value_flags:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


class ToolPolicy:
    """Ordered set of executable rules; first match wins."""

    def __init__(self, rules: tuple[Rule, ...], *, source: str = "default") -> None:
        self.rules = rules
        self.source = source

    def allows(self, argv: list[str] | tuple[str, ...]) -> tuple[bool, str]:
        if not argv:
            return False, "empty argv"
        candidate = str(argv[0])
        for rule in self.rules:
            if candidate not in rule.argv0:
                continue
            if not rule.subcommands:
                return True, f"rule={rule.name}"
            sub = subcommand_of(argv, rule.value_flags)
            if sub in rule.subcommands:
                return True, f"rule={rule.name} sub={sub}"
            return False, f"rule={rule.name} but subcommand {sub!r} is not allowed"
        return False, f"executable {candidate!r} is not in the tool policy"

    def allowed_argv0(self) -> frozenset[str]:
        return frozenset(path for rule in self.rules for path in rule.argv0)

    @classmethod
    def default(cls, extra_argv0: tuple[str, ...] = ()) -> "ToolPolicy":
        bins = tuple(dict.fromkeys((DEFAULT_CODEQL_BIN, *extra_argv0)))
        return cls((Rule(name="codeql", argv0=bins),), source="default")

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        extra_argv0: tuple[str, ...] = (),
    ) -> "ToolPolicy":
        raw_path = str(path or os.environ.get("RESEARCHCLAW_TOOL_POLICY", "")).strip()
        if not raw_path:
            return cls.default(extra_argv0)
        policy_path = Path(raw_path).expanduser()
        if not policy_path.is_file():
            # Fail closed: a configured but missing policy must not silently
            # widen permissions. Fall back to the historical default.
            return cls.default(extra_argv0)
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        rules: list[Rule] = []
        for item in data.get("tools") or []:
            argv0 = tuple(
                str(v).replace("{codeql}", DEFAULT_CODEQL_BIN) for v in (item.get("argv0") or [])
            )
            if not argv0:
                continue
            rules.append(
                Rule(
                    name=str(item.get("name") or argv0[0]),
                    argv0=argv0,
                    subcommands=tuple(str(s) for s in (item.get("subcommands") or [])),
                    value_flags=tuple(str(s) for s in (item.get("value_flags") or [])),
                    writable=tuple(str(s) for s in (item.get("writable") or [])),
                )
            )
        if not rules:  # an empty policy is a misconfiguration: fail closed
            return cls.default(extra_argv0)
        return cls(tuple(rules), source=str(policy_path))


@lru_cache(maxsize=8)
def policy_for(policy_path: str, codeql_bin: str) -> ToolPolicy:
    """Cached policy lookup keyed by (path, extra codeql bin) — env-driven."""
    extra = tuple(filter(None, codeql_bin.split(os.pathsep)))
    return ToolPolicy.load(policy_path or None, extra_argv0=extra)
