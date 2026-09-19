import subprocess
import sys

import pytest

from hazina_scan import env


def test_build_env_is_minimal(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    monkeypatch.setenv("LANG", "C.UTF-8")
    built = env.build_env()
    assert "PATH" in built and built["LANG"] == "C.UTF-8"
    assert "AWS_SECRET_ACCESS_KEY" not in built and "GITHUB_TOKEN" not in built


def test_build_env_refuses_denied_passthrough(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    with pytest.raises(env.DeniedVariable):
        env.build_env(passthrough=("GITHUB_TOKEN",))


def test_run_git_returns_stdout(py_repo):
    assert env.run_git(py_repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_run_git_returns_empty_on_failure(tmp_path):
    assert env.run_git(tmp_path, "rev-parse", "HEAD") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="git on Windows does not octal-escape paths")
def test_run_git_quotepath_flag_is_honoured(repo_builder):
    """Off, a non-ASCII path comes back escaped -- which is what a tool we have to
    produce the same numbers as sees, because it sets nothing."""
    repo = repo_builder({"src/café.py": "x\n"})
    assert "src/café.py" in env.run_git(repo, "ls-files")
    escaped = env.run_git(repo, "ls-files", quotepath_false=False)
    assert escaped.strip() == '"src/caf\\303\\251.py"'


def test_run_git_quotepath_flag_reaches_the_argv(tmp_path, monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    env.run_git(tmp_path, "status")
    env.run_git(tmp_path, "status", quotepath_false=False)
    assert seen[0][:3] == ["git", "-c", "core.quotepath=false"]
    assert seen[1][:2] == ["git", "-C"]
    assert "core.quotepath=false" not in seen[1]
