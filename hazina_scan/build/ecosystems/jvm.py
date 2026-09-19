"""JVM build plans: Maven and Gradle.

Both prefer a checked-in wrapper script (`mvnw`, `gradlew`) over a bare `mvn`/`gradle` on
PATH whenever the project ships one, because the wrapper is the version the project actually
tested against and a bare command on PATH is whatever happens to be installed on this host.
Neither ecosystem has a native "enumerate the tests" command worth trusting -- Maven's
closest equivalent matches nothing on purpose -- so both read what really ran from the JUnit
XML the test phase itself leaves behind, which is why `discover` is `None` for Maven and a
best-effort dry run for Gradle.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["plan"]

_IS_WIN = sys.platform.startswith("win")


def _ensure_executable(path: Path, restore: list[tuple[Path, int]]) -> None:
    """Give a checked-in wrapper script its executable bit if the checkout lost it,
    remembering the original mode so the caller can put it back once the probe is done.

    Windows carries no such bit, so there is nothing for this to do there.
    """
    if _IS_WIN or not path.is_file() or os.access(path, os.X_OK):
        return
    try:
        mode = path.stat().st_mode
        restore.append((path, mode))
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _plan_maven(project, scratch: Path, restore: list) -> dict:
    wrapper = project.root / ("mvnw.cmd" if _IS_WIN else "mvnw")
    if wrapper.is_file():
        _ensure_executable(wrapper, restore)
        exe = [str(wrapper)]
    else:
        exe = ["mvn"]
    base = exe + ["-B", "-ntp", f"-Dmaven.repo.local={scratch / 'm2'}"]
    return {
        "toolchain": "maven",
        "tool": exe[0],
        # Deliberately no `-o` flag: `-Dmaven.repo.local` redirects Maven at a local repo this
        # run just created and left empty, so an offline resolve there would fail for any
        # project with even one dependency. Fetching those dependencies fresh into a scratch
        # directory is exactly what the other ecosystems' lockfile-driven installs already do.
        "locked": base + ["dependency:go-offline"],
        "relaxed": base + ["-U", "dependency:go-offline"],
        "build": base + ["-DskipTests", "test-compile"],
        "discover": None,
        "test": (base + ["-Dmaven.test.failure.ignore=true", "test"], None),
        "test_junit": ("**/target/surefire-reports/*.xml", "**/target/failsafe-reports/*.xml"),
        "coverage": None,
        "coverage_reason": (
            "jacoco only writes a coverage report when the project already applies the plugin "
            "itself"
        ),
    }


def _plan_gradle(project, scratch: Path, restore: list) -> dict:
    wrapper = project.root / ("gradlew.bat" if _IS_WIN else "gradlew")
    if wrapper.is_file():
        _ensure_executable(wrapper, restore)
        exe = [str(wrapper)]
    else:
        exe = ["gradle"]
    base = exe + ["--no-daemon", "--console=plain", "-g", str(scratch / "gradle")]
    return {
        "toolchain": "gradle",
        "tool": exe[0],
        # `dependencies` resolves the graph without compiling anything, the closest Gradle
        # comes to a locked-install phase. `--write-locks` is deliberately absent: it would
        # edit a tracked lockfile, and this plan only ever reads the checkout.
        "locked": base + ["dependencies"],
        "relaxed": None,
        "build": base + ["testClasses"],
        "discover": (base + ["test", "--dry-run"], "gradle_dry"),
        "test": (base + ["--continue", "test"], None),
        "test_junit": ("**/build/test-results/**/*.xml",),
        "coverage": None,
        "coverage_reason": (
            "jacoco only writes a coverage report when the project already applies the plugin "
            "itself"
        ),
    }


def plan(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """The dependency/build/discover/test commands for one Maven or Gradle module.

    Dispatches purely on `project.ecosystem`: a directory discovery found by its `pom.xml`
    is never also found by a Gradle marker, so the two branches never both apply to the same
    project. `env` and `timeout` are accepted so every ecosystem module answers to the same
    call shape; neither changes what a JVM build command looks like.
    """
    if project.ecosystem == "gradle":
        return _plan_gradle(project, scratch, restore)
    return _plan_maven(project, scratch, restore)
