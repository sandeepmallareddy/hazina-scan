"""Node.js build plan.

Which package manager owns the install is read from whichever lockfile is actually on disk,
never guessed from a `package.json` field, because a lockfile is the one place a project
states "install exactly this" -- everything else is a hint. Which test runner the project
uses is read the same way: from its declared dependencies and its own `"test"` script,
because a Node project can carry more than one test-adjacent package and only one of them is
what `npm test` (or the runner-specific command below) will actually invoke.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["plan"]


def _read_text(path: Path, cap: int = 200_000) -> str:
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def _pkg_scripts(root: Path) -> dict:
    """The `"scripts"` table of `package.json`, or `{}` when there is none to read."""
    pkg = _read_json(root / "package.json")
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _pkg_deps(root: Path) -> dict:
    """Every dependency this project declares, runtime, dev and peer alike, folded into one
    lookup so a caller does not have to check three separate tables."""
    pkg = _read_json(root / "package.json")
    deps: dict = {}
    if isinstance(pkg, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            value = pkg.get(key)
            if isinstance(value, dict):
                deps.update(value)
    return deps


def _node_framework(root: Path) -> str:
    """Which of the three runners this project's own manifest points at, `"unknown"` when
    none of them is named anywhere we look."""
    deps = _pkg_deps(root)
    script = str(_pkg_scripts(root).get("test") or "").lower()
    if "vitest" in deps or "vitest" in script:
        return "vitest"
    if "jest" in deps or "@jest/core" in deps or "react-scripts" in deps or "jest" in script:
        return "jest"
    if "mocha" in deps or "mocha" in script:
        return "mocha"
    return "unknown"


def _real_test_script(root: Path) -> bool:
    """Whether `npm test` would run something the project actually wrote.

    `npm init` leaves a placeholder script that only prints "Error: no test specified" and
    exits non-zero; running it would be neither a repository failure nor a runner failure, so
    it is treated as though no test script exists at all.
    """
    script = str(_pkg_scripts(root).get("test") or "")
    return bool(script) and "no test specified" not in script.lower()


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The install/build/discover/test/coverage commands for one Node project.

    `env`, `timeout` and `restore` are accepted so every ecosystem module answers to the same
    call shape; none of the three changes anything here -- a Node install needs no runtime
    environment overlay baked into its argv, no per-command time slice, and no wrapper
    script's executable bit restored afterwards.
    """
    root = project.root
    cov_dir = scratch / "cov"
    if (root / "pnpm-lock.yaml").is_file():
        tool, toolchain = "pnpm", "node-pnpm"
        locked = ["pnpm", "install", "--frozen-lockfile"]
        relaxed = ["pnpm", "install", "--no-frozen-lockfile"]
    elif (root / "yarn.lock").is_file():
        tool, toolchain = "yarn", "node-yarn"
        berry = (root / ".yarnrc.yml").is_file() or "__metadata:" in _read_text(
            root / "yarn.lock", 4000
        )
        locked = (
            ["yarn", "install", "--immutable"]
            if berry
            else ["yarn", "install", "--frozen-lockfile", "--non-interactive"]
        )
        relaxed = ["yarn", "install"] if berry else ["yarn", "install", "--non-interactive"]
    elif (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
        tool, toolchain = "npm", "node-npm"
        locked = ["npm", "ci", "--no-audit", "--no-fund"]
        relaxed = ["npm", "install", "--no-audit", "--no-fund", "--legacy-peer-deps"]
    else:
        # No lockfile anywhere: `npm install` is the only install this project ever declared,
        # so it stands as the locked command and there is no stricter variant to fall back
        # from.
        tool, toolchain = "npm", "node-npm"
        locked = ["npm", "install", "--no-audit", "--no-fund"]
        relaxed = None

    scripts = _pkg_scripts(root)
    build = ["npm", "run", "build"] if "build" in scripts else None
    framework = _node_framework(root)
    if framework == "jest":
        discover = (["npx", "--no-install", "jest", "--listTests"], "jest_files")
        test = (
            [
                "npx",
                "--no-install",
                "jest",
                "--ci",
                "--runInBand",
                "--passWithNoTests",
                "--coverage",
                "--coverageReporters=json-summary",
                f"--coverageDirectory={cov_dir}",
            ],
            "jest",
        )
        coverage = ("istanbul", cov_dir / "coverage-summary.json")
    elif framework == "vitest":
        discover = (["npx", "--no-install", "vitest", "list"], "jest_files")
        test = (
            [
                "npx",
                "--no-install",
                "vitest",
                "run",
                "--coverage",
                "--coverage.reporter=json-summary",
                f"--coverage.reportsDirectory={cov_dir}",
            ],
            "vitest",
        )
        coverage = ("istanbul", cov_dir / "coverage-summary.json")
    elif framework == "mocha":
        # `--dry-run --reporter min` prints mocha's own "N passing" summary line, which is
        # what the mocha parser is written to read.
        discover = (["npx", "--no-install", "mocha", "--dry-run", "--reporter", "min"], "mocha")
        test = (["npm", "test", "--silent"], "mocha")
        coverage = None
    elif _real_test_script(root):
        discover = None
        test = (["npm", "test", "--silent"], None)
        coverage = None
    else:
        discover = None
        test = None
        coverage = None
    return {
        "toolchain": toolchain,
        "tool": tool,
        "locked": locked,
        "relaxed": relaxed,
        "build": build,
        "discover": discover,
        "test": test,
        "coverage": coverage,
    }
