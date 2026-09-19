"""PHP build plan.

Composer owns dependency resolution, and the test command reaches for phpunit under the
scratch vendor directory Composer's own install populates rather than a global install this
host may or may not carry. Coverage is left unset: no PHP coverage driver (Xdebug, PCOV) is
assumed to be present on the host running the probe.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["plan"]

_IS_WIN = sys.platform.startswith("win")


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The install/discover/test commands for one Composer-managed PHP project.

    `env`, `timeout` and `restore` are accepted so every ecosystem module answers to the
    same call shape; none of the three changes what this plan runs.
    """
    phpunit = scratch / "vendor" / "bin" / ("phpunit.bat" if _IS_WIN else "phpunit")
    return {
        "toolchain": "composer",
        "tool": "composer",
        "locked": ["composer", "install", "--no-interaction", "--no-progress"],
        "relaxed": [
            "composer",
            "install",
            "--no-interaction",
            "--no-progress",
            "--ignore-platform-reqs",
        ],
        "build": None,
        "discover": ([str(phpunit), "--list-tests"], "phpunit_list"),
        "test": ([str(phpunit), "--do-not-cache-result"], "phpunit"),
        "coverage": None,
        "coverage_reason": "no PHP coverage driver is assumed to be installed on this host",
    }
