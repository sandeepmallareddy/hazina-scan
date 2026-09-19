"""Python build plan.

Every later command in this plan bakes an absolute interpreter path into its argv, so the
virtualenv the plan runs against has to exist before any of those commands can be written
down -- this module creates it as part of planning rather than deferring that to whoever
runs the plan later. The interpreter itself is whichever one runtime resolution matched
against the project's own declared version; falling back to this process's own interpreter
only happens when the tree declared nothing for the host to match against.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from hazina_scan import env as env_mod

__all__ = ["plan"]

_IS_WIN = sys.platform.startswith("win")

#: Optional-dependency and dependency-group names that plausibly mean "what this project
#: needs to run its own tests", read from `pyproject.toml`.
_TEST_GROUP_NAMES = ("test", "tests", "testing", "dev", "develop", "development")


def _venv_python(venv: Path) -> Path:
    """The interpreter that lives inside a virtualenv; the layout differs by platform, so
    this is the one place that has to know it."""
    return venv / ("Scripts" if _IS_WIN else "bin") / ("python.exe" if _IS_WIN else "python")


def _read_text(path: Path, cap: int = 200_000) -> str:
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _toml_section(body: str, header: str) -> str:
    """The raw text of one TOML table, from its `[header]` line up to the next `[...]`
    header. There is no TOML parser here on purpose, the same way the rest of this planner
    reads manifests by pattern rather than by fully understanding their grammar."""
    match = re.search(rf"^\[{re.escape(header)}\]\s*$", body, re.M)
    if not match:
        return ""
    rest = body[match.end() :]
    nxt = re.search(r"^\[", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _python_test_deps(root: Path) -> tuple[list[str], list[str]]:
    """The optional-dependency extras and PEP 735 dependency groups a project names for
    running its own tests, so `pip install .` alone -- which only installs the package and
    its runtime dependencies -- is not the only thing this plan ever installs."""
    body = _read_text(root / "pyproject.toml", 200_000)
    if not body:
        return [], []
    found = []
    for header, out in (("project.optional-dependencies", []), ("dependency-groups", [])):
        section = _toml_section(body, header)
        for key in re.findall(r"^\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*=", section, re.M):
            if key.lower() in _TEST_GROUP_NAMES:
                out.append(key)
        found.append(out)
    return found[0], found[1]


def _python_harness(py: str, root: Path) -> list[list[str]]:
    """List the pip installs that give this project's suite the best chance of actually running.

    `pytest` and `pytest-cov` are installed first and unconditionally, so even a project whose
    own declared test extras fail to install still ends up with a runner available; whatever
    the project itself names via `_python_test_deps` is installed afterward. Nothing this
    function returns is allowed to influence whether resolve passed -- a project simply never
    declaring pytest as a dependency is not a resolve failure caused by this virtualenv.
    """
    commands = [[py, "-m", "pip", "install", "-q", "pytest", "pytest-cov"]]
    extras, groups = _python_test_deps(root)
    if extras:
        commands.append([py, "-m", "pip", "install", "-q", f".[{','.join(sorted(set(extras)))}]"])
    for group in sorted(set(groups)):
        commands.append([py, "-m", "pip", "install", "-q", "--group", group])
    return commands


def _which(cmd: str, env: dict | None) -> str | None:
    """Resolve one argv[0] against the CHILD's own PATH rather than this process's, so the
    absolute path handed to the sandboxed runner is one that environment can actually see."""
    return shutil.which(cmd, path=None if env is None else env.get("PATH"))


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int) -> tuple[int, str, bool]:
    """Run one command for its side effect and fold every outcome into one shape.

    A plan-time command either finishes with a return code, times out, or the host cannot
    start it at all (a missing interpreter, most often); a caller building a plan wants one
    tuple back rather than three different exceptions to catch.
    """
    argv = list(cmd)
    resolved = _which(argv[0], env)
    if resolved:
        argv[0] = resolved
    try:
        proc = env_mod.run(argv, domain=env_mod.BUILD, cwd=cwd, env=env, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.output
            if isinstance(exc.output, str)
            else (exc.output or b"").decode("utf-8", "replace")
        )
        err = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode("utf-8", "replace")
        )
        return 124, out + err, True
    except OSError as exc:
        return 127, f"command not found: {cmd[0]} ({type(exc).__name__})", False


def plan(
    project,
    scratch: Path,
    env: dict,
    timeout: int,
    restore: list,
    base_python: str | None = None,
) -> dict:
    """The venv/install/harness/discover/test/coverage commands for one Python project.

    `restore` is accepted so every ecosystem module answers to the same call shape; nothing
    here rewrites a checked-in file's permission bit. `base_python`, when given, is the
    interpreter runtime resolution matched for this tree; without one this falls back to
    whichever interpreter is running this process, exactly as measuring an unpinned project
    with the interpreter at hand has always meant.

    A virtualenv is created here, synchronously, because it is what every command below is
    built around: each one bakes the venv's own interpreter path into its argv rather than
    depending on activation or a PATH lookup a later step could resolve differently.
    """
    root = project.root
    venv = scratch / "venv"
    interpreter = base_python or sys.executable
    rc, log, _timed_out = _run(
        [interpreter, "-m", "venv", str(venv)], scratch, env, min(timeout, 300)
    )
    if rc != 0 or not _venv_python(venv).exists():
        # A host that cannot create a virtualenv is a host problem, not a repository one.
        return {
            "toolchain": "python",
            "tool": None,
            "preflight": log or "ensurepip is not available",
        }
    py = str(_venv_python(venv))
    cov_json = scratch / "coverage.json"
    if (root / "requirements.txt").is_file():
        locked = [py, "-m", "pip", "install", "-r", "requirements.txt"]
        relaxed = None
    elif any((root / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg")):
        locked = [py, "-m", "pip", "install", "."]
        relaxed = [py, "-m", "pip", "install", "--no-build-isolation", "."]
    else:
        for extra in (
            "requirements-dev.txt",
            "dev-requirements.txt",
            "requirements/base.txt",
            "requirements/dev.txt",
        ):
            if (root / extra).is_file():
                locked, relaxed = [py, "-m", "pip", "install", "-r", extra], None
                break
        else:
            # Something in this directory matched a Python signal (tox.ini, noxfile.py, or
            # similar), yet none of the files this function knows how to install from are
            # present. Making up an install command anyway would score the project against a
            # guess rather than against what it actually declares.
            return {
                "toolchain": "python-venv",
                "tool": None,
                "preflight": "probe: no installable manifest present",
            }
    return {
        "toolchain": "python-venv",
        "tool": py,
        "locked": locked,
        "relaxed": relaxed,
        "build": None,
        "harness": _python_harness(py, project.root),
        "discover": (
            [py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            "pytest",
        ),
        "test": (
            [
                py,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--cov=.",
                f"--cov-report=json:{cov_json}",
                "--cov-report=",
            ],
            "pytest",
        ),
        "coverage": ("coveragepy", cov_json),
        "test_fallback": ([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "pytest"),
    }
