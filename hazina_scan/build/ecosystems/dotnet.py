"""The .NET build plan.

The `dotnet` CLI covers restore, build, test enumeration and test execution through one
family of subcommands, and coverage is deliberately left unset: coverlet only produces a
report when the project itself already references it, and inventing that reference here
would measure this tool's own addition rather than the repository.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["plan"]


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The restore/build/discover/test commands for one .NET project or solution.

    `scratch`, `env`, `timeout` and `restore` are accepted so every ecosystem module answers
    to the same call shape; the dotnet CLI needs none of the four baked into its plan.
    """
    return {
        "toolchain": "dotnet",
        "tool": "dotnet",
        "locked": ["dotnet", "restore", "--locked-mode"],
        "relaxed": ["dotnet", "restore"],
        "build": ["dotnet", "build", "--no-restore"],
        "discover": (["dotnet", "test", "--no-build", "--list-tests"], "dotnet_list"),
        "test": (["dotnet", "test", "--no-build", "--nologo"], "dotnet"),
        "coverage": None,
        "coverage_reason": (
            "coverlet only reports coverage when the project already references it itself"
        ),
    }
