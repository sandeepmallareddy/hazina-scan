"""The build trust domain and the process-group runner.

The child programs here are `python -c` one-liners on purpose: these tests have to pass on a
machine with nothing installed but Python and git.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hazina_scan import env

CI_MARKERS = {
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

SPAWNER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def test_scratch_home_exists_then_goes_away():
    with env.scratch_home() as home:
        assert home.is_dir()
        seen = home
    assert not seen.exists()


def test_build_domain_relocates_home_and_tmpdir():
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
    assert built["HOME"] == str(home)
    assert built["USERPROFILE"] == str(home)
    assert Path(built["TMPDIR"]).parent == home


def test_build_domain_creates_its_tmpdir():
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
        assert Path(built["TMPDIR"]).is_dir()


def test_build_domain_sets_the_non_interactive_markers():
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
    for name, value in CI_MARKERS.items():
        assert built[name] == value


def test_build_domain_path_leaves_the_operator_home_behind():
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
    operator = Path.home().resolve()
    for entry in built["PATH"].split(os.pathsep):
        resolved = Path(entry).resolve()
        assert resolved != operator and operator not in resolved.parents


def test_build_domain_path_falls_back_when_nothing_survives(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path.home() / "bin"), str(Path.home())]))
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
    assert built["PATH"] == os.pathsep.join(
        ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
    )


@pytest.mark.parametrize(
    "name", ["AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "HTTPS_PROXY", "SNOWFLAKE_ACCOUNT"]
)
def test_build_domain_carries_no_credential(monkeypatch, name):
    monkeypatch.setenv(name, "decoy")
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home)
    assert name not in built


@pytest.mark.parametrize("prefix", ["STUDIO_", "MODAL_", "SNOWFLAKE_", "AIRTABLE_"])
def test_new_denied_prefixes_are_refused_as_passthrough(monkeypatch, prefix):
    monkeypatch.setenv(f"{prefix}KEY", "decoy")
    with pytest.raises(env.DeniedVariable):
        env.build_env(passthrough=(f"{prefix}KEY",))


def test_build_domain_refuses_a_denied_extra():
    with env.scratch_home() as home:
        with pytest.raises(env.DeniedVariable):
            env.build_env(domain="build", home=home, extra={"MODAL_TOKEN_ID": "decoy"})


def test_build_domain_applies_extra_last():
    with env.scratch_home() as home:
        built = env.build_env(domain="build", home=home, extra={"CI": "0", "MY_FLAG": "on"})
    assert built["CI"] == "0" and built["MY_FLAG"] == "on"


def test_build_domain_demands_a_home():
    with pytest.raises(ValueError):
        env.build_env(domain="build", home=None)


def test_unknown_domain_is_refused():
    with pytest.raises(ValueError):
        env.build_env(domain="nonsense")


def test_static_domain_is_unchanged(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp/somewhere")
    built = env.build_env()
    assert "CI" not in built
    assert "HOME" not in built
    assert built["TMPDIR"] == "/tmp/somewhere"
    assert built == env.build_env(domain="static")


def test_run_captures_stdout_and_the_built_environment():
    with env.scratch_home() as home:
        proc = env.run(
            [sys.executable, "-c", "import os; print(os.environ.get('CI'))"],
            domain="build",
            home=home,
            timeout=60,
        )
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1"


def test_run_reports_a_failing_exit_code():
    with env.scratch_home() as home:
        proc = env.run(
            [sys.executable, "-c", "raise SystemExit(3)"], domain="build", home=home, timeout=60
        )
    assert proc.returncode == 3


def test_run_feeds_input_text_to_stdin():
    with env.scratch_home() as home:
        proc = env.run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            domain="build",
            home=home,
            timeout=60,
            input_text="hello",
        )
    assert proc.stdout == "HELLO"


def test_run_honours_cwd(tmp_path):
    with env.scratch_home() as home:
        proc = env.run(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            domain="build",
            home=home,
            cwd=tmp_path,
            timeout=60,
        )
    assert Path(proc.stdout.strip()).resolve() == tmp_path.resolve()


@pytest.mark.skipif(os.name != "posix", reason="process groups are checked with os.kill here")
def test_run_timeout_kills_the_whole_group():
    started = time.monotonic()
    with env.scratch_home() as home:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            env.run([sys.executable, "-c", SPAWNER], domain="build", home=home, timeout=1)
    elapsed = time.monotonic() - started
    assert elapsed < 8, f"the call took {elapsed:.1f}s to come back"

    output = caught.value.output or ""
    grandchild = int(output.strip().splitlines()[0])

    deadline = time.monotonic() + 3
    while True:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, f"pid {grandchild} outlived its process group"
        time.sleep(0.05)
