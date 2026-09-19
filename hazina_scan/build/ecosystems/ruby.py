"""Ruby build plan.

Bundler owns dependency resolution, and rspec's own `--dry-run` stands in for a native
"list the tests" command Ruby does not otherwise offer. Coverage is left unset: simplecov
only produces a report when the project's own spec helper already configures it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["plan"]


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The install/discover/test commands for one Bundler-managed Ruby project.

    `scratch`, `env`, `timeout` and `restore` are accepted so every ecosystem module answers
    to the same call shape; a bundler-driven project needs none of the four baked into its
    plan.
    """
    return {
        "toolchain": "bundler",
        "tool": "bundle",
        "locked": ["bundle", "install", "--deployment"],
        "relaxed": ["bundle", "install"],
        "build": None,
        "discover": (["bundle", "exec", "rspec", "--dry-run", "--no-color"], "rspec_dry"),
        "test": (["bundle", "exec", "rspec", "--no-color"], "rspec"),
        "coverage": None,
        "coverage_reason": (
            "simplecov only reports coverage when the project's own spec helper already "
            "configures it"
        ),
    }
