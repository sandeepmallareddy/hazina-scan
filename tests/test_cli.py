"""The command line: one repository, many repositories, and what a run says on the way.

Every test here runs the module as a subprocess, because the exit code and the two
streams ARE the interface -- calling `cli.main([...])` in-process would test the same
function while skipping the part an operator actually uses.

`--no-build` is passed almost everywhere, because the check executes the measured
repository's own install and test commands and none of the tests below are about that.
The few that ARE about it say so, and ask for the cheaper level.

Output folders ON DISK are named after each repository's own local directory name --
friendly, so an operator can find `<out>/myrepo/` without a lookup table. Inside
`hazina-out.zip`, the very same folder is packed under its anonymous, content-derived
handle (`repo-<hex>`, from the row's `fake_repo_name`) instead, so no local directory
name ever leaves the machine. `INDEX.local.txt` is the one file that records both names
side by side, and it never travels with the zip.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from hazina_scan import cli, orchestrator


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "hazina_scan", *args], capture_output=True, text=True
    )


def tiny_repo(path: Path, marker: str | None = None) -> Path:
    """A one-commit git repository at `path`, parents created.

    `marker` (default: the directory's own name) goes into the one tracked file, so two
    calls at two different paths produce two different content handles unless a test asks
    otherwise by passing the same marker twice.
    """
    path.mkdir(parents=True, exist_ok=True)
    ident = ["-c", "user.name=d", "-c", "user.email=d@e.com"]
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "a.py").write_text(f"x = 1  # {marker if marker is not None else path.name}\n")
    subprocess.run(["git", "-C", str(path), *ident, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), *ident, "commit", "-q", "-m", "i"], check=True)
    return path


def handle_of(repo: Path) -> str:
    return f"repo-{orchestrator.repo_digest(repo)[:12]}"


def read_index(out: Path) -> dict[str, tuple[str, str, str]]:
    """`{local name: (handle, repo path, status)}` parsed back out of `INDEX.local.txt`."""
    lines = (out / "INDEX.local.txt").read_text(encoding="utf-8").splitlines()
    rows = [ln.split("\t") for ln in lines if ln and not ln.startswith("#")]
    assert all(len(r) == 4 for r in rows), rows  # local, handle, repo path, status
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


# --------------------------------------------------------------------------
# The brief's acceptance tests
# --------------------------------------------------------------------------


def test_single_repo(py_repo, tmp_path):
    out = tmp_path / "out"
    proc = run(str(py_repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr

    # On disk: the repository's own local directory name.
    assert (out / py_repo.name / "measurement.json").exists()
    row = json.loads((out / py_repo.name / "codebase_repos.json").read_text())
    handle = handle_of(py_repo)
    assert row["fake_repo_name"] == handle
    assert "acme/demo" in proc.stdout and "Python" in proc.stdout

    # In the zip: the anonymous handle, and only those three files.
    zpath = tmp_path / "hazina-out.zip"
    assert zpath.exists()
    names = sorted(zipfile.ZipFile(zpath).namelist())
    assert names == sorted(
        f"out/{handle}/{fname}"
        for fname in ("codebase_repos.json", "codebase_repos.csv", "measurement.json")
    )

    index_path = out / "INDEX.local.txt"
    assert index_path.exists()
    assert str(py_repo) in index_path.read_text(encoding="utf-8")
    assert "INDEX.local.txt" not in [Path(n).name for n in names]

    idx = read_index(out)
    assert idx[py_repo.name] == (handle, str(py_repo), "measured")


def test_build_discover_runs_and_says_which_level_ran(py_repo, tmp_path):
    """`--build discover` on a real Python project. What the project's own commands make of
    this host is not the point -- that the check RAN, said so, and did not take the run down
    with it is."""
    proc = run(str(py_repo), "--build", "discover", "--out", str(tmp_path / "o"))
    assert proc.returncode == 0, proc.stderr
    assert "[build] ran at level discover" in proc.stderr
    block = json.loads((tmp_path / "o" / py_repo.name / "measurement.json").read_text())
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
    doc = json.loads((tmp_path / "o" / py_repo.name / "measurement.json").read_text())
    assert doc["ext_signals"]["build"] is None
    assert "build: not run" in proc.stdout


def test_all_dir_and_zip(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    for n in ("one", "two"):
        (root / n).mkdir()
        subprocess.run(["git", "-C", str(root / n), "init", "-q"], check=True)
        (root / n / "a.py").write_text(f"x = 1  # {n}\n")
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

    handles = {n: handle_of(root / n) for n in ("one", "two")}
    for n in ("one", "two"):
        assert (out / n / "codebase_repos.csv").exists()

    names = zipfile.ZipFile(tmp_path / "hazina-out.zip").namelist()
    assert len(names) == 6  # two handle folders, three files each, nothing more
    for h in handles.values():
        assert any(n.endswith(f"{h}/measurement.json") for n in names)
    assert not any("INDEX.local.txt" in n for n in names)
    assert not any("one" in n or "two" in n for n in names)  # no local dirname in the zip

    for n, h in handles.items():
        assert f"{n}  ->  {h}: measured" in proc.stdout
    assert f"index (local only, not in the zip): {out / 'INDEX.local.txt'}" in proc.stdout

    idx = read_index(out)
    for n, h in handles.items():
        assert idx[n] == (h, str(root / n), "measured")


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
# Output folder naming: friendly on disk, anonymous in the zip
# --------------------------------------------------------------------------


def test_identical_trees_under_different_dirnames_get_two_local_folders_and_a_suffixed_handle(
    repo_builder, tmp_path
):
    """Two repositories with byte-for-byte identical trees, checked out under different
    local directory names, get two distinct local folders and, since they share a
    content handle, a suffixed handle inside the zip."""
    files = {"a.py": "x = 1\n", "README.md": "hi\n"}
    first = repo_builder(files, name="one")
    second = repo_builder(files, name="two")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr

    assert (out / "one" / "measurement.json").exists()
    assert (out / "two" / "measurement.json").exists()

    base = handle_of(first)
    assert base == handle_of(second)  # identical trees really do share a base handle

    names = zipfile.ZipFile(tmp_path / "hazina-out.zip").namelist()
    assert any(n.endswith(f"{base}/measurement.json") for n in names)
    assert any(n.endswith(f"{base}-2/measurement.json") for n in names)
    assert not any("/one/" in n or "/two/" in n for n in names)

    idx = read_index(out)
    got_handles = {idx["one"][0], idx["two"][0]}
    assert got_handles == {base, f"{base}-2"}
    assert idx["one"][1] == str(first)
    assert idx["two"][1] == str(second)


def test_same_dirname_in_different_parents_gets_local_name_and_dash_two(tmp_path):
    """Two repositories share a directory name on this machine but not any content.
    Their local folders must not collide, so the second is suffixed."""
    first = tiny_repo(tmp_path / "a" / "dup", marker="a")
    second = tiny_repo(tmp_path / "b" / "dup", marker="b")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out), "--no-zip")
    assert proc.returncode == 0, proc.stderr
    assert (out / "dup" / "measurement.json").exists()
    assert (out / "dup-2" / "measurement.json").exists()
    assert any(ln.startswith("dup  ->  ") and ": measured" in ln for ln in proc.stdout.splitlines())
    assert any(
        ln.startswith("dup-2  ->  ") and ": measured" in ln for ln in proc.stdout.splitlines()
    )


def test_the_local_directory_name_never_appears_in_the_zip(repo_builder, tmp_path):
    files = {"a.py": "x = 1\n"}
    repo = repo_builder(files, name="acme-secret-billing")
    out = tmp_path / "out"
    proc = run(str(repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    # On disk, the friendly name is right there.
    assert (out / "acme-secret-billing" / "measurement.json").exists()
    with zipfile.ZipFile(tmp_path / "hazina-out.zip") as zf:
        assert not any(b"acme-secret-billing" in n.encode() for n in zf.namelist())
        for member in zf.namelist():
            assert b"acme-secret-billing" not in zf.read(member)


# --------------------------------------------------------------------------
# Zipping, the local index, review and concurrency
# --------------------------------------------------------------------------


def test_no_zip_leaves_no_archive_but_still_writes_the_index(tmp_path):
    first = tiny_repo(tmp_path / "a" / "one")
    second = tiny_repo(tmp_path / "b" / "two")
    out = tmp_path / "out"
    proc = run(str(first), str(second), "--no-build", "--out", str(out), "--no-zip")
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "hazina-out.zip").exists()
    assert not list(tmp_path.glob("*.zip"))
    assert (out / "INDEX.local.txt").exists()


def test_stale_content_under_out_is_not_swept_into_the_zip(py_repo, tmp_path):
    """`--out` pointing at a directory that already holds unrelated files -- think
    `--out .` -- must not turn those files into zip entries."""
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "junk.txt").write_text("leftover\n")
    (out / "old").mkdir()
    (out / "old" / "x.json").write_text("{}\n")
    proc = run(str(py_repo), "--no-build", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    names = zipfile.ZipFile(tmp_path / "hazina-out.zip").namelist()
    assert not any("junk.txt" in n for n in names)
    assert not any("old" in n for n in names)


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
    assert "one  -> " in proc.stdout and "two  -> " in proc.stdout


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
    assert "bad: FAILED -- RuntimeError: collector exploded" in captured.out.splitlines()
    assert f"index (local only, not in the zip): {out / 'INDEX.local.txt'}" in captured.out
    idx = read_index(out)
    assert idx["good"][1:] == (str(good), "measured")
    assert idx["bad"][1:] == (str(bad), "FAILED")


def test_a_failure_after_measuring_is_reported_and_the_run_finishes(tmp_path, monkeypatch, capsys):
    """A write that fails is as much a per-repository failure as a collector that raises.
    It must not take the other repositories, the status lines or the exit code with it.
    Only the good repository's folder is zipped."""
    good = tiny_repo(tmp_path / "a" / "good")
    bad = tiny_repo(tmp_path / "b" / "bad")
    bad_digest = orchestrator.repo_digest(bad)
    real = cli.orchestrator.write_outputs

    def fake(out_dir, row, measurement):
        if row.get("repo_digest") == bad_digest:
            raise OSError("disk went away")
        return real(out_dir, row, measurement)

    monkeypatch.setattr(cli.orchestrator, "write_outputs", fake)
    out = tmp_path / "out"
    code = cli.main([str(good), str(bad), "--no-build", "--out", str(out), "--jobs", "1"])
    captured = capsys.readouterr()
    assert code == 1
    good_handle = handle_of(good)
    assert (out / "good" / "measurement.json").exists()
    assert "bad: FAILED: OSError: disk went away" in captured.err.splitlines()
    assert any(
        ln.startswith("good  ->  ") and ln.endswith(": measured")
        for ln in captured.out.splitlines()
    )
    assert "bad: FAILED -- OSError: disk went away" in captured.out.splitlines()
    assert (tmp_path / "hazina-out.zip").exists()  # the zip step still ran
    names = zipfile.ZipFile(tmp_path / "hazina-out.zip").namelist()
    assert any(n.endswith(f"{good_handle}/measurement.json") for n in names)
    assert len(names) == 3  # only the good repository's three files


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
    assert (out / py_repo.name / "measurement.json").exists()


def test_out_dot_from_the_repositorys_own_parent_is_refused(tmp_path):
    """`--out .` run from a repository's own parent puts the repository's output folder
    on top of the repository itself, now that output folders are named after each
    repository's local directory name again. Caught before any repository is measured."""
    parent = tmp_path / "workspace"
    repo = tiny_repo(parent / "myrepo")
    proc = subprocess.run(
        [sys.executable, "-m", "hazina_scan", "myrepo", "--no-build", "--out", "."],
        capture_output=True,
        text=True,
        cwd=str(parent),
    )
    assert proc.returncode == 2, proc.stderr
    assert "would write" in proc.stderr
    assert "repository being measured" in proc.stderr
    assert not (repo / "measurement.json").exists()
    assert not (repo / "codebase_repos.json").exists()


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
    assert (out / py_repo.name / "measurement.json").exists()
    assert not (out / f"{py_repo.name}-2").exists()


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
