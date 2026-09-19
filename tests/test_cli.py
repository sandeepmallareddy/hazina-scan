"""The command line: one repository, many repositories, and what a run says on the way.

Every test here runs the module as a subprocess, because the exit code and the two
streams ARE the interface -- calling `cli.main([...])` in-process would test the same
function while skipping the part an operator actually uses.

`--no-build` is passed almost everywhere, because the check executes the measured
repository's own install and test commands and none of the tests below are about that.
The few that ARE about it say so, and ask for the cheaper level.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from hazina_scan import cli


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "hazina_scan", *args], capture_output=True, text=True
    )


def tiny_repo(path: Path) -> Path:
    """A one-commit git repository at `path`, parents created."""
    path.mkdir(parents=True, exist_ok=True)
    ident = ["-c", "user.name=d", "-c", "user.email=d@e.com"]
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), *ident, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), *ident, "commit", "-q", "-m", "i"], check=True)
    return path


# --------------------------------------------------------------------------
# The brief's acceptance tests
# --------------------------------------------------------------------------


def test_single_repo(py_repo, tmp_path):
    out = tmp_path / "out"
    proc = run(str(py_repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert (out / "repo" / "measurement.json").exists()
    assert "acme/demo" in proc.stdout and "Python" in proc.stdout


def test_build_discover_runs_and_says_which_level_ran(py_repo, tmp_path):
    """`--build discover` on a real Python project. What the project's own commands make of
    this host is not the point -- that the check RAN, said so, and did not take the run down
    with it is."""
    proc = run(str(py_repo), "--build", "discover", "--out", str(tmp_path / "o"))
    assert proc.returncode == 0, proc.stderr
    assert "[build] ran at level discover" in proc.stderr
    block = json.loads((tmp_path / "o" / "repo" / "measurement.json").read_text())
    assert block["ext_signals"]["build"]["build_level"] == "discover"


def test_the_warning_about_executing_the_repository_is_printed_once(tmp_path):
    """Two repositories, one warning. It describes the command, not a repository, and a
    warning repeated per repository is a warning people learn to scroll past."""
    first = tiny_repo(tmp_path / "a" / "one")
    second = tiny_repo(tmp_path / "b" / "two")
    proc = run(
        str(first),
        str(second),
        "--build",
        "discover",
        "--out",
        str(tmp_path / "out"),
        "--no-zip",
    )
    assert proc.returncode == 0, proc.stderr
    warnings = [ln for ln in proc.stderr.splitlines() if "MODIFIES THE CHECKOUT" in ln]
    assert len(warnings) == 1, warnings
    assert "disposable clone" in warnings[0]


def test_no_build_executes_nothing_and_says_nothing_about_it(py_repo, tmp_path):
    proc = run(str(py_repo), "--no-build", "--out", str(tmp_path / "o"))
    assert proc.returncode == 0, proc.stderr
    assert "MODIFIES THE CHECKOUT" not in proc.stderr
    assert "[build] ran at level" not in proc.stderr
    doc = json.loads((tmp_path / "o" / "repo" / "measurement.json").read_text())
    assert doc["ext_signals"]["build"] is None
    assert "build: not run" in proc.stdout


def test_all_dir_and_zip(repo_builder, tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    for n in ("one", "two"):
        (root / n).mkdir()
        subprocess.run(["git", "-C", str(root / n), "init", "-q"], check=True)
        (root / n / "a.py").write_text("x\n")
        subprocess.run(
            [
                "git",
                "-C",
                str(root / n),
                "-c",
                "user.name=d",
                "-c",
                "user.email=d@e.com",
                "add",
                "-A",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root / n),
                "-c",
                "user.name=d",
                "-c",
                "user.email=d@e.com",
                "commit",
                "-q",
                "-m",
                "i",
            ],
            check=True,
        )
    (root / "notrepo").mkdir()
    out = tmp_path / "out"
    proc = run("--all", str(root), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert (out / "one" / "codebase_repos.csv").exists() and (
        out / "two" / "codebase_repos.csv"
    ).exists()
    names = zipfile.ZipFile(tmp_path / "hazina-out.zip").namelist()
    assert any(n.endswith("one/measurement.json") for n in names)


def test_usage_errors(tmp_path):
    assert run(str(tmp_path / "missing"), "--no-build").returncode == 2
    assert run("--all", str(tmp_path), "--no-build").returncode == 2


# --------------------------------------------------------------------------
# Path validation, which happens before any measuring
# --------------------------------------------------------------------------


def test_directory_without_git_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    proc = run(str(plain), "--no-build", "--out", str(tmp_path / "out"))
    assert proc.returncode == 2
    assert "not a git repository" in proc.stderr and str(plain) in proc.stderr
    assert not (tmp_path / "out").exists()  # refused before anything was written


def test_no_repositories_given(tmp_path):
    proc = run("--no-build", "--out", str(tmp_path / "out"))
    assert proc.returncode == 2
    assert "no repositor" in proc.stderr.lower()


# --------------------------------------------------------------------------
# Output folder naming
# --------------------------------------------------------------------------


def test_duplicate_dirnames_get_a_suffix(tmp_path):
    first = tiny_repo(tmp_path / "a" / "dup")
    second = tiny_repo(tmp_path / "b" / "dup")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out), "--no-zip")
    assert proc.returncode == 0, proc.stderr
    assert (out / "dup" / "measurement.json").exists()
    assert (out / "dup-2" / "measurement.json").exists()


# --------------------------------------------------------------------------
# Zipping, review and concurrency
# --------------------------------------------------------------------------


def test_no_zip_leaves_no_archive(tmp_path):
    first = tiny_repo(tmp_path / "a" / "one")
    second = tiny_repo(tmp_path / "b" / "two")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out), "--no-zip")
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "hazina-out.zip").exists()
    assert not list(tmp_path.glob("*.zip"))


def test_review_prints_every_field(py_repo, tmp_path):
    short = run(str(py_repo), "--no-build", "--out", str(tmp_path / "a"))
    full = run(str(py_repo), "--no-build", "--out", str(tmp_path / "b"), "--review")
    assert short.returncode == 0 and full.returncode == 0, full.stderr
    assert "readme_loc" not in short.stdout
    assert "readme_loc" in full.stdout


def test_jobs_one_serialises(tmp_path):
    first = tiny_repo(tmp_path / "a" / "one")
    second = tiny_repo(tmp_path / "b" / "two")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out), "--jobs", "1")
    assert proc.returncode == 0, proc.stderr
    assert (out / "one" / "measurement.json").exists()
    assert (out / "two" / "measurement.json").exists()
    assert "one: " in proc.stdout and "two: " in proc.stdout


# --------------------------------------------------------------------------
# A repository that raises: reported, and the others still run
# --------------------------------------------------------------------------


def test_one_repo_raising_exits_1_and_the_rest_still_run(tmp_path, monkeypatch, capsys):
    good = tiny_repo(tmp_path / "a" / "good")
    bad = tiny_repo(tmp_path / "b" / "bad")
    real = cli.orchestrator.measure

    def fake(repo, **kw):
        if Path(repo).name == "bad":
            raise RuntimeError("collector exploded")
        return real(repo, **kw)

    monkeypatch.setattr(cli.orchestrator, "measure", fake)
    out = tmp_path / "out"
    code = cli.main([str(good), str(bad), "--no-build", "--out", str(out), "--no-zip"])
    captured = capsys.readouterr()
    assert code == 1
    assert (out / "good" / "measurement.json").exists()
    assert not (out / "bad").exists()
    assert "bad: FAILED: RuntimeError: collector exploded" in captured.err.splitlines()


# --------------------------------------------------------------------------
# report.py on its own
# --------------------------------------------------------------------------


def test_summary_line_shape():
    from hazina_scan import report

    row = {
        "real_repo_name": None,
        "fake_repo_name": "repo-abc123",
        "primary_language": "Python",
        "loc": 1373,
        "commit_count": 677,
        "author_count": 41,
        "test_framework": ["pytest"],
        "has_ci": True,
    }
    line = report.summary_line(row, {"real_repo_name": "acme/demo"})
    assert line == (
        "acme/demo  Python  1,373 LOC  677 commits  41 authors  "
        "tests: pytest  ci: yes  build: not run"
    )


def test_summary_line_nulls_print_as_question_marks():
    from hazina_scan import report

    row = {
        "real_repo_name": None,
        "fake_repo_name": "repo-abc123",
        "primary_language": None,
        "loc": None,
        "commit_count": None,
        "author_count": None,
        "test_framework": [],
        "has_ci": None,
    }
    line = report.summary_line(row, {})
    assert line == (
        "repo-abc123  ?  ? LOC  ? commits  ? authors  tests: none  ci: ?  build: not run"
    )


def test_plan_lines_name_the_roots_and_the_size_of_the_walk(py_repo):
    from hazina_scan import report

    (py_repo / "node_modules").mkdir()
    (py_repo / "node_modules" / "huge.js").write_text("x\n")
    lines = report.plan_lines(py_repo, "none", 9000)
    assert lines[0].startswith("[plan] 1 project root (python),")
    assert "files in" in lines[0] and "directories scanned" in lines[0]
    assert "node_modules" not in "\n".join(lines)
    assert "budget" in lines[1] and "minute" in lines[1]
    # Nothing would run, so the split names the reading lanes alone.
    assert "deterministic collectors" in lines[1] and "build probe" not in lines[1]


def test_plan_lines_price_the_build_check_when_one_was_asked_for(py_repo):
    from hazina_scan import report

    lines = report.plan_lines(py_repo, "full", 9000)
    assert "build probe" in lines[1]
    # A budget the estimate does not fit inside is said out loud, with what will be cut.
    short = report.plan_lines(py_repo, "full", 10)
    assert any("EXCEEDS the budget" in line for line in short)
    assert any("the build check" in line for line in short)


def test_plan_lines_warn_about_roots_past_the_cap(py_repo):
    from hazina_scan import report

    for n in range(4):
        part = py_repo / f"part{n}"
        part.mkdir()
        (part / "go.mod").write_text(f"module example.test/part{n}\n\ngo 1.21\n")
    lines = report.plan_lines(py_repo, "discover", 9000, max_build_projects=2)
    assert any("past the cap and will be reported skipped" in line for line in lines)
    assert any("--max-build-projects" in line for line in lines)


def test_a_subdirectory_of_a_repository_is_refused(py_repo, tmp_path):
    proc = run(str(py_repo / "src"), "--no-build", "--out", str(tmp_path / "out"))
    assert proc.returncode == 2
    assert "not a git repository" in proc.stderr


def test_the_same_repository_named_twice_is_measured_once(py_repo, tmp_path):
    out = tmp_path / "out"
    proc = run(str(py_repo), str(py_repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert (out / "repo" / "measurement.json").exists()
    assert not (out / "repo-2").exists()


def test_every_lane_line_names_its_repository(tmp_path):
    """Two repositories share one stderr. An unlabelled `[lane] tree started` in that
    stream tells an operator nothing, so every one of them carries the folder name."""
    tiny_repo(tmp_path / "a" / "one")
    tiny_repo(tmp_path / "b" / "two")
    out = tmp_path / "out"
    proc = run(
        str(tmp_path / "a" / "one"),
        str(tmp_path / "b" / "two"),
        "--no-build",
        "--out",
        str(out),
        "--no-zip",
    )
    assert proc.returncode == 0, proc.stderr
    lane_lines = [ln for ln in proc.stderr.splitlines() if "[lane]" in ln]
    assert lane_lines
    assert all(ln.startswith(("one: ", "two: ")) for ln in lane_lines), lane_lines


def test_a_failure_after_measuring_is_reported_and_the_run_finishes(tmp_path, monkeypatch, capsys):
    """A write that fails is as much a per-repository failure as a collector that raises.
    It must not take the other repositories, the status lines or the exit code with it."""
    good = tiny_repo(tmp_path / "a" / "good")
    bad = tiny_repo(tmp_path / "b" / "bad")
    real = cli.orchestrator.write_outputs

    def fake(out_dir, row, measurement):
        if Path(out_dir).name == "bad":
            raise OSError("disk went away")
        return real(out_dir, row, measurement)

    monkeypatch.setattr(cli.orchestrator, "write_outputs", fake)
    out = tmp_path / "out"
    code = cli.main([str(good), str(bad), "--no-build", "--out", str(out), "--jobs", "1"])
    captured = capsys.readouterr()
    assert code == 1
    assert (out / "good" / "measurement.json").exists()
    assert "bad: FAILED: OSError: disk went away" in captured.err.splitlines()
    assert "good: measured" in captured.out
    assert "bad: FAILED -- OSError: disk went away" in captured.out
    assert (tmp_path / "hazina-out.zip").exists()  # the zip step still ran


# --------------------------------------------------------------------------
# --out must not land inside a measured repository
# --------------------------------------------------------------------------


def test_an_out_directory_inside_the_repository_is_refused(py_repo):
    """Writing into the tree changes what the next run measures, so it is refused."""
    proc = run(str(py_repo), "--no-build", "--out", str(py_repo / "hazina-out"))
    assert proc.returncode == 2
    assert "would write inside the repository being measured" in proc.stderr
    assert not (py_repo / "hazina-out").exists()


def test_the_repository_itself_as_the_out_directory_is_refused(py_repo):
    proc = run(str(py_repo), "--no-build", "--out", str(py_repo))
    assert proc.returncode == 2
    assert "would write inside the repository being measured" in proc.stderr


def test_an_out_directory_beside_the_repository_is_allowed(py_repo, tmp_path):
    out = tmp_path / "beside"
    proc = run(str(py_repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert (out / "repo" / "measurement.json").exists()


def test_an_out_directory_whose_subfolder_lands_on_the_repository_is_refused(tmp_path):
    """`--out .` from a repository's parent aims `<out>/myrepo` at `myrepo` itself."""
    repo = tiny_repo(tmp_path / "myrepo")
    proc = run(str(repo), "--no-build", "--out", str(tmp_path))
    assert proc.returncode == 2
    assert "would write inside the repository being measured" in proc.stderr
    assert not (repo / "measurement.json").exists()
