import subprocess
import sys

from hazina_scan import __version__


def test_version_flag():
    out = subprocess.run(
        [sys.executable, "-m", "hazina_scan", "--version"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert __version__ in out.stdout


def test_fixture_builder_makes_a_git_repo(py_repo):
    assert (py_repo / ".git").is_dir()
    log = subprocess.run(
        ["git", "-C", str(py_repo), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert log.count("\n") == 1
