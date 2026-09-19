"""Built child environments. Every subprocess this tool starts gets an environment assembled
here from a fixed list of harmless variables, never a copy of the operator's."""

from __future__ import annotations

import os
import subprocess
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
)


class DeniedVariable(RuntimeError):
    """A denied variable was about to reach a child. Always a caller bug."""


def build_env(passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (*_BASE, *passthrough):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    for key in env:
        if any(key == d or key.startswith(d) for d in _DENIED):
            raise DeniedVariable(f"{key!r} may not be handed to a child process")
    return env


def run_git(repo: Path, *args: str, timeout: int = 600, quotepath_false: bool = True) -> str:
    """Read-only git with a fixed argument list. Empty string on any failure.

    `quotepath_false` decides whether a path with a non-ASCII character in it comes back as
    itself or as git's octal-escaped, quoted form. It is on by default, which is what a
    caller reading paths for their own sake wants. The history collectors in `git.py` turn
    it off, because the escaping decides which patterns a path matches and their counts are
    defined against the spelling git gives a path by default.
    """
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
