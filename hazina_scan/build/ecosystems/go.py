"""Go build plan.

The Go toolchain already owns enumeration, execution and coverage measurement through one
family of subcommands, so this plan is the shortest of the nine: there is no package-manager
choice to make and no separate coverage tool to reach for.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["plan"]


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The download/build/discover/test/coverage commands for one Go module.

    `env`, `timeout` and `restore` are accepted so every ecosystem module answers to the
    same call shape; a Go module needs none of the three baked into its plan.
    """
    profile = scratch / "cover.out"
    return {
        "toolchain": "go-modules",
        "tool": "go",
        "locked": ["go", "mod", "download"],
        "relaxed": None,
        "build": ["go", "build", "./..."],
        "discover": (["go", "test", "-list", ".*", "./..."], "go_list"),
        "test": (
            [
                "go",
                "test",
                "./...",
                "-count=1",
                "-json",
                "-covermode=set",
                f"-coverprofile={profile}",
            ],
            "go",
        ),
        "coverage": ("go", profile),
    }
