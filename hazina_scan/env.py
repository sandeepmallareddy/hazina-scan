"""Built child environments. Every subprocess this tool starts gets an environment assembled
here from a fixed list of harmless variables, never a copy of the operator's.

There are two trust domains. `static` is for the deterministic readers -- git and the parsers --
which are ours and do nothing but read. `build` is for code that arrives with the repository:
its installers, its compilers, its test suite. That code gets an empty throwaway HOME, a private
TMPDIR beneath it, a PATH with the operator's own directories taken out, and the handful of
variables that make an ecosystem choose its quiet, non-interactive path. It gets no token, no
proxy setting and no agent socket, whatever the operator happens to have exported.

`run()` is the other half. A timeout that only reaps the process we launched is not a timeout at
all once that process has forked a dev server or a package-manager daemon, so children start in
a session of their own and the timeout signals the entire group.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BASE = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
_DENIED = (
    "AWS_",
    "GITHUB_",
    "GH_",
    "GITLAB_",
    "DATABASE_",
    "DOCKER_",
    "NPM_TOKEN",
    "NODE_AUTH_TOKEN",
    "PYPI_",
    "TWINE_",
    "CARGO_REGISTRY_TOKEN",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "GPG_AGENT_INFO",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "VAULT_",
    "OKTA_",
    "STUDIO_",
    "MODAL_",
    "SNOWFLAKE_",
    "AIRTABLE_",
)

STATIC = "static"
BUILD = "build"
DOMAINS = (STATIC, BUILD)

# Settings that turn off progress spinners, funding banners, update checks, telemetry and
# credential prompts across the ecosystems we drive. Every one of them is a published knob.
_QUIET = {
    "CI": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "NPM_CONFIG_FUND": "false",
    "NPM_CONFIG_AUDIT": "false",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "NO_COLOR": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "DO_NOT_TRACK": "1",
}

_MINIMAL_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

# How long we are willing to keep reading a killed child's pipes. Once the group is dead the
# pipes are normally closed already; this ceiling only matters when something slipped the group.
POST_KILL_GRACE_SECONDS = 2.0


class DeniedVariable(RuntimeError):
    """A denied variable was about to reach a child. Always a caller bug."""


def _outside_operator_home(path_value: str) -> str:
    """Return `path_value` without the entries that sit inside the operator's home directory.

    A developer's PATH is full of per-user directories -- language version managers, `~/.local/bin`,
    shim folders. Emptying HOME while still pointing a repository's install hook at binaries under
    the real one would be half a measure, so those entries come out and the rest keep their order.
    A child with an empty PATH would fail for a reason that has nothing to do with the repository,
    so if nothing at all survives we substitute the usual system directories.
    """
    try:
        operator_home = Path.home().resolve()
    except (RuntimeError, OSError):
        return path_value

    kept: list[str] = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except (OSError, ValueError):
            continue
        if resolved == operator_home or operator_home in resolved.parents:
            continue
        kept.append(entry)
    return os.pathsep.join(kept) if kept else os.pathsep.join(_MINIMAL_PATH)


def _reject_denied(env: dict[str, str]) -> dict[str, str]:
    for key in env:
        if any(key == d or key.startswith(d) for d in _DENIED):
            raise DeniedVariable(f"{key!r} may not be handed to a child process")
    return env


@contextmanager
def scratch_home(prefix: str = "hazina-scan-home-") -> Iterator[Path]:
    """Lend one run an empty directory to use as HOME, and delete it afterwards.

    Install and test commands look for `~/.npmrc`, `~/.gitconfig`, `~/.pypirc`, `~/.aws` and a
    long tail of other per-user files. Aiming HOME somewhere empty hides all of them at once,
    with no list to keep up to date.
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    try:
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_env(
    passthrough: tuple[str, ...] | list[str] = (),
    *,
    domain: str = STATIC,
    home: Path | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assemble the environment for one child, starting from an empty mapping.

    passthrough  names of ambient variables to copy across when they are set.
    domain       `static` for our own readers, `build` for the repository's commands.
    home         the directory to advertise as HOME. The build domain insists on one.
    extra        literal pairs, applied after everything else so a caller can override.
    """
    if domain not in DOMAINS:
        raise ValueError(f"{domain!r} is not a trust domain; choose one of {DOMAINS}")

    env: dict[str, str] = {}
    for name in (*_BASE, *passthrough):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    if domain == BUILD:
        if home is None:
            raise ValueError(
                "the build domain needs a home directory of its own; see scratch_home()"
            )
        if env.get("PATH"):
            env["PATH"] = _outside_operator_home(env["PATH"])
        # The operator's TMPDIR is shared with everything else running on this machine, so the
        # build domain gets one nobody else knows about instead of inheriting it.
        tmpdir = Path(home) / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmpdir)
        env.update(_QUIET)
    elif os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]

    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

    if extra:
        env.update(extra)

    return _reject_denied(env)


def run(
    argv: list[str],
    *,
    domain: str,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    capture_output: bool = True,
    home: Path | str | None = None,
) -> subprocess.CompletedProcess:
    """Run a fixed argument list in a trust domain, and take its descendants down with it.

    The child is given a session of its own, so everything it spawns shares one process group id
    and a timeout can signal all of them at once. When the timeout fires the group is killed and
    whatever output had already been captured comes back on the `TimeoutExpired` that is raised,
    after a drain that is itself bounded: the call returns at roughly `timeout` plus
    `POST_KILL_GRACE_SECONDS` no matter what escaped.
    """
    child_env = env if env is not None else build_env(domain=domain, home=home)
    options: dict = {
        "cwd": None if cwd is None else str(cwd),
        "env": child_env,
        "text": True,
        "errors": "replace",
        "shell": False,
        "stdout": subprocess.PIPE if capture_output else None,
        "stderr": subprocess.PIPE if capture_output else None,
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
    }
    if os.name == "posix":
        options["start_new_session"] = True
    else:
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(argv, **options)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = _drain(proc)
        raise subprocess.TimeoutExpired(argv, timeout or 0, output=out, stderr=err) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _decoded(value) -> str | None:
    """A `TimeoutExpired` hands back bytes even when the child was opened in text mode."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _drain(proc: subprocess.Popen) -> tuple[str | None, str | None]:
    """Read what is left on a killed child's pipes, under a deadline.

    Usually there is nothing to wait for: the writers are dead and the pipes are at end of file.
    The case this guards against is a descendant that got away -- it still holds the write end,
    end of file never arrives, and an open-ended read would hang the whole scan. So the wait has
    a ceiling, after which we take the partial text and shut our ends by hand.
    """
    try:
        return proc.communicate(timeout=POST_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as expired:
        out, err = _decoded(expired.output), _decoded(expired.stderr)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=POST_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    return out, err


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill every process in the child's group outright. Never raises.

    There is no grace signal first. A phase that has already blown its budget has had its time,
    and a hung installer is exactly the thing least likely to answer a polite request; waiting on
    one would eat into the ceiling this function exists to keep.
    """
    if os.name != "posix":
        # Windows has no process group to signal, but `taskkill /T` walks the child's tree.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            shell=False,
            check=False,
        )
        proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


#: Where a run parks the ceiling every git call on this thread is held to. Thread-local
#: because a single process measures several repositories at once, each under its own
#: allowance, and a global would let the shortest one gag the others.
_CEILING = threading.local()


@contextmanager
def git_ceiling(seconds: float | None) -> Iterator[None]:
    """Hold every git call made on THIS thread to `seconds`, then put the old ceiling back.

    This is how a wall-clock allowance reaches a collector without every function between
    the two growing a parameter it only passes on. The lane opens the context, the calls
    underneath it shorten to fit, and a lane with nothing left gets a ceiling of zero --
    which is refused outright rather than started and killed a moment later.

    `None` lifts the ceiling, which is what a caller with no allowance to enforce wants.
    """
    previous = getattr(_CEILING, "seconds", None)
    _CEILING.seconds = None if seconds is None else max(0, int(seconds))
    try:
        yield
    finally:
        _CEILING.seconds = previous


def git_ceiling_seconds() -> int | None:
    """The ceiling in force on this thread, or None when nothing is enforcing one."""
    return getattr(_CEILING, "seconds", None)


def run_git(repo: Path, *args: str, timeout: int = 600, quotepath_false: bool = True) -> str:
    """Read-only git with a fixed argument list. Empty string on any failure.

    `timeout` is this one call's own ceiling, and a run-wide ceiling set by `git_ceiling`
    lowers it further. Neither can raise the other: the smaller of the two always wins, so
    a collector that asks for ten minutes inside a one-minute allowance gets the minute.
    A ceiling of zero means the allowance is spent, and the call is refused rather than
    started -- the empty answer a caller reads as "not measured" arrives immediately
    instead of after a pointless wait.

    `quotepath_false` decides whether a path with a non-ASCII character in it comes back as
    itself or as git's octal-escaped, quoted form. It is on by default, which is what a
    caller reading paths for their own sake wants. The history collectors in `git.py` turn
    it off, because the escaping decides which patterns a path matches and their counts are
    defined against the spelling git gives a path by default.
    """
    ceiling = git_ceiling_seconds()
    if ceiling is not None:
        if ceiling <= 0:
            return ""
        timeout = min(timeout, ceiling)
    try:
        proc = subprocess.run(
            [
                "git",
                *(["-c", "core.quotepath=false"] if quotepath_false else []),
                "-C",
                str(repo),
                "--no-pager",
                *args,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            env=build_env(passthrough=("HOME", "USERPROFILE")),
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""
