from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env:
        base.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=base, check=True
    )
    return proc.stdout


def make_repo(
    root: Path,
    files: dict[str, str],
    commits: list[dict] | None = None,
    remote: str | None = None,
    name: str = "repo",
) -> Path:
    """Build a git repository under `root`.

    files    initial working-tree contents, path -> text.
    commits  optional list of {"msg", "files" (path -> text), "name", "email", "date"};
             each becomes one commit. When omitted, `files` is committed once as
             "initial" by Dev One <dev1@example.com> on 2024-01-15.
    remote   optional origin URL.
    name     directory name under `root` to create the repository at (default "repo").
    """
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Dev One")
    _git(repo, "config", "user.email", "dev1@example.com")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if commits is None:
        commits = [
            {
                "msg": "initial",
                "files": {},
                "name": "Dev One",
                "email": "dev1@example.com",
                "date": "2024-01-15T10:00:00+00:00",
            }
        ]
    for c in commits:
        for rel, text in c.get("files", {}).items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        stamp = c.get("date", "2024-01-15T10:00:00+00:00")
        env = {
            "GIT_AUTHOR_NAME": c.get("name", "Dev One"),
            "GIT_AUTHOR_EMAIL": c.get("email", "dev1@example.com"),
            "GIT_COMMITTER_NAME": c.get("name", "Dev One"),
            "GIT_COMMITTER_EMAIL": c.get("email", "dev1@example.com"),
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        _git(repo, "commit", "-q", "--allow-empty", "-m", c["msg"], env=env)
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


@pytest.fixture
def repo_builder(tmp_path):
    def build(files, commits=None, remote=None, name="repo"):
        return make_repo(tmp_path, files, commits, remote, name)

    return build


PY_FILES = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1"\n'
    'dependencies = ["requests"]\n'
    '[project.optional-dependencies]\ndev = ["pytest", "pytest-cov"]\n',
    "src/demo/__init__.py": "",
    "src/demo/core.py": "def add(a, b):\n    if a > b:\n        return a + b\n    return b + a\n",
    "tests/test_core.py": (
        "from demo.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    ),
    "README.md": "# Demo\n\n## Installation\n\npip install demo\n\n## Usage\n\nimport demo\n",
    "LICENSE": "Copyright (c) 2024 Acme Corp\n\nMIT License\n",
    ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n"
    "      - run: pip install -e .\n"
    "      - run: pytest\n",
}


@pytest.fixture
def py_repo(tmp_path):
    return make_repo(tmp_path, PY_FILES, remote="git@github.com:acme/demo.git")
