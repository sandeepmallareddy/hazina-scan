"""Rust build plan.

`--locked` is only ever asked of cargo when a `Cargo.lock` actually exists to honour, the
same rule the Node planner applies to `npm ci`: a crate that has simply not committed its
lockfile is not the same failure as one whose lockfile does not match its manifest, and
demanding strictness against a file that is not there would conflate the two.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["plan"]


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The fetch/build/discover/test commands for one Cargo crate.

    `scratch`, `env`, `timeout` and `restore` are accepted so every ecosystem module answers
    to the same call shape; none of them changes what this plan runs.
    """
    has_lock = (project.root / "Cargo.lock").is_file()
    return {
        "toolchain": "cargo",
        "tool": "cargo",
        "locked": ["cargo", "fetch", "--locked"] if has_lock else ["cargo", "fetch"],
        "relaxed": ["cargo", "fetch"] if has_lock else None,
        "build": ["cargo", "build", "--locked"] if has_lock else ["cargo", "build"],
        "build_relaxed": ["cargo", "build"] if has_lock else None,
        "discover": (["cargo", "test", "--", "--list"], "cargo_list"),
        "test": (["cargo", "test", "--no-fail-fast"], "cargo"),
        "coverage": None,
        "coverage_reason": (
            "cargo ships no line-coverage reporter of its own, and a third-party one such as "
            "cargo-llvm-cov is not assumed to be installed on the host running the probe"
        ),
    }
