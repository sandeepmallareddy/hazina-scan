import csv
import json
import time

import pytest

from hazina_scan import orchestrator as orc
from hazina_scan.build import discover, ecosystems

# The build lane is exercised here through the same made-up ecosystems `tests/test_build_probe`
# uses, so a measurement that installs and tests a project can be run on a machine with no
# toolchain on it at all. Each fixture project's manifest is a small JSON document naming one
# of that module's stand-in scripts per phase.
from tests.test_build_probe import FAKE, MANIFEST, _planner


@pytest.fixture
def fake_ecosystems(monkeypatch):
    """Teach discovery and the planner registry about the ecosystems used in this file.

    Deliberately narrower than the namesake in `tests/test_build_probe`: that one also keeps
    the per-project evidence on the block so its assertions can read the phase records, and
    the evidence is exactly what `collect()` drops before anybody sees it. Keeping it here
    would put an undeclared key through the write boundary and stop the run.
    """
    monkeypatch.setattr(
        discover,
        "_MARKERS",
        discover._MARKERS + tuple((eco, (MANIFEST[eco],)) for eco in FAKE),
    )
    for eco in FAKE:
        monkeypatch.setitem(ecosystems.PLANNERS, eco, _planner)


def test_repo_full_name_parsing(monkeypatch):
    cases = {
        "git@github.com:acme/demo.git": "acme/demo",
        "https://gitlab.com/group/sub/name.git": "group/sub/name",
        "file:///home/x/repo": None,
        "/srv/repo": None,
        "C:/src/repo": None,
        "ssh://git@host/a/b": "a/b",
        "": None,
    }
    for url, want in cases.items():
        monkeypatch.setattr(orc, "_remote_url", lambda repo, u=url: u)
        assert orc.repo_full_name(None) == want, url


def test_digest_is_stable_and_ignores_location(repo_builder, tmp_path):
    a = repo_builder({"a.py": "x\n"}, name="a")
    b = repo_builder({"a.py": "x\n"}, name="b")
    assert orc.repo_digest(a) == orc.repo_digest(b) and len(orc.repo_digest(a)) == 64


def test_deadline_slices_what_is_left():
    d = orc.Deadline(100)
    assert d.total == 100
    assert 0 < d.remaining() <= 100 and d.elapsed() >= 0
    assert d.slice(10) == 10  # its own ceiling binds
    assert d.slice(1000, reserve=40) in (59, 60)  # what is left, minus the reserve
    assert orc.Deadline(0).slice(10) == 0


def test_row_status_is_measured_without_a_build():
    assert orc.row_status(None) == ("measured", None)
    assert orc.row_status({"ok": True}) == ("measured", None)
    assert orc.row_status({"ok": False}) == ("partial", "lanes_unavailable")
    assert orc.row_status({"timed_out": True}) == ("partial", "lanes_timed_out")
    assert orc.row_status({"build_skipped": True}) == ("partial", "lanes_timed_out")


def test_a_level_nobody_declared_is_refused(py_repo):
    with pytest.raises(ValueError):
        orc.measure(py_repo, build_level="everything")


# --------------------------------------------------------------------------
# The build lane, driven by projects in an ecosystem that does not exist
# --------------------------------------------------------------------------


def _buildable(repo_builder, spec: dict, name: str = "repo"):
    """A one-commit repository holding one project the fake planner knows how to run."""
    return repo_builder({MANIFEST[FAKE[0]]: json.dumps(spec), "source.txt": "x" * 32}, name=name)


def test_a_discover_level_run_fills_in_the_build_block(fake_ecosystems, repo_builder):
    repo = _buildable(repo_builder, {"install": "ok", "build": "ok", "discover": "collect3"})
    row, measurement = orc.measure(repo, build_level="discover", timeout_build=30, build_budget=120)

    block = measurement["ext_signals"]["build"]
    assert block is not None
    assert block["probe"] == "build"
    assert block["build_level"] == "discover"
    assert block["build_level_requested"] == "discover"
    assert block["build_ok"] is True
    assert block["install_ok"] is True
    assert block["tests_discovered"] is True
    assert block["discover_runnability"] == 3
    # The row carries the same two verdicts, and the suite one is absent at this level.
    assert row["build_ok"] is True
    assert row["testable_at_head"] is None
    assert row["status"] == "measured"


def test_the_build_lane_has_the_tree_to_itself(fake_ecosystems, repo_builder):
    """The exclusivity rule, read off the recorded spans rather than off the control flow."""
    repo = _buildable(repo_builder, {"install": "ok", "discover": "collect3"})
    clock = orc.LaneClock()
    orc.measure(repo, build_level="discover", clock=clock, timeout_build=30, build_budget=120)

    assert clock.seconds("build") is not None  # the lane really ran
    assert clock.overlaps("build") == []
    # And the readers still overlapped each other, so the emptiness above is not an artefact
    # of everything having been serialised.
    assert clock.overlaps("tree") != []


def test_a_probe_the_clock_never_reached_makes_the_row_partial(fake_ecosystems, repo_builder):
    repo = _buildable(repo_builder, {"install": "ok"})
    # Under the smallest phase a probe will start, so however fast the readers are there is
    # nothing left to start one with.
    budget = orc.build_probe.MIN_PHASE_SECONDS - 1
    row, measurement = orc.measure(repo, build_level="discover", deadline=orc.Deadline(budget))
    # Nothing is left for the lane that executes, so it is recorded as skipped rather than
    # run: every runnability field is null and the row says the run has a hole in it.
    block = measurement["ext_signals"]["build"]
    assert block["build_skipped"] is True
    # The stub carries no measurement at all rather than a false one, so there is no
    # `build_ok` on the block to read and the row's column is null.
    assert "build_ok" not in block
    assert "seconds left" in block["note"]
    assert row["build_ok"] is None
    assert (row["status"], row["skip_reason"]) == ("partial", "lanes_timed_out")


def test_a_full_attempt_that_runs_out_is_finished_at_the_cheaper_level(
    fake_ecosystems, repo_builder, capsys
):
    """The fallback. The suite cannot finish, so the measurement is completed at `discover`
    and both levels are recorded -- the one that ran and the one that was asked for.

    TWO roots, not one. The fallback fires only when the check's own clock stopped it, and
    what proves that is a root the clock never reached; a single root that merely hangs uses
    up the allowance without ever leaving one unvisited.
    """
    repo = _buildable(repo_builder, {"install": "hang"})
    second = repo / "part2"
    second.mkdir()
    (second / MANIFEST[FAKE[1]]).write_text(json.dumps({"install": "hang"}))
    (second / "source.txt").write_text("x" * 32)
    started = time.monotonic()
    row, measurement = orc.measure(
        repo,
        build_level="full",
        build_budget=36,
        full_attempt_seconds=16,
        timeout_build=900,
        deadline=orc.Deadline(300),
    )
    elapsed = time.monotonic() - started

    block = measurement["ext_signals"]["build"]
    assert block["build_level"] == "discover"
    assert block["build_level_requested"] == "full"
    assert "cheaper level" in block["build_level_fallback_reason"]
    assert block["run_budget_exhausted"] is True
    assert row["build_ok"] is None
    # Both calls together stayed inside the reserve they were given.
    assert elapsed < 36 + 25, f"the two attempts overran the build reserve: {elapsed:.1f}s"
    assert "ran at level discover (asked for full)" in capsys.readouterr().err


def test_a_twenty_second_budget_stops_the_run_and_reports_nulls(fake_ecosystems, repo_builder):
    """The budget is enforced, not recorded. Nothing here can finish, so the only thing that
    can end the run is the clock -- and what it did not reach is null with a reason."""
    repo = _buildable(repo_builder, {"install": "hang"})
    started = time.monotonic()
    row, measurement = orc.measure(repo, build_level="full", deadline=orc.Deadline(20))
    elapsed = time.monotonic() - started

    assert elapsed < 35, f"the run overran its budget by {elapsed - 20:.1f}s"
    block = measurement["ext_signals"]["build"]
    assert block["build_ok"] is None
    assert block["observed_runnability"] is None
    assert block["observed_runnability_reason"]
    assert block["timed_out"] is True
    assert row["build_ok"] is None and row["testable_at_head"] is None
    assert (row["status"], row["skip_reason"]) == ("partial", "lanes_timed_out")


def test_measure_writes_three_files(py_repo, tmp_path):
    row, measurement = orc.measure(py_repo)
    written = orc.write_outputs(tmp_path / "out", row, measurement)
    assert sorted(p.name for p in written) == [
        "codebase_repos.csv",
        "codebase_repos.json",
        "measurement.json",
    ]
    assert row["status"] == "measured" and row["real_repo_name"] is None
    assert measurement["real_repo_name"] == "acme/demo"
    assert "material" not in measurement and measurement["ext_signals"]["build"] is None
    assert list(row) == orc.CSV_COLUMNS
    text = (tmp_path / "out" / "measurement.json").read_text()
    assert "src/" not in text and "dev1@example.com" not in text
    # The row is the same document, written twice.
    assert json.loads((tmp_path / "out" / "codebase_repos.json").read_text()) == row


def test_csv_cells_carry_the_row(py_repo, tmp_path):
    """The CSV is the spelling most readers actually load, so its cells are pinned.

    A null must arrive as an empty cell -- never the text `None`, which reads as a value,
    and never `0`, which reads as a measurement of none.
    """
    row, measurement = orc.measure(py_repo)
    orc.write_outputs(tmp_path / "out", row, measurement)
    path = tmp_path / "out" / "codebase_repos.csv"

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == orc.CSV_COLUMNS
        rows = list(reader)
    assert len(rows) == 1
    cells = rows[0]

    # null -> empty, and nothing anywhere spells a null as a word or as a zero.
    assert row["zip_bytes"] is None and cells["zip_bytes"] == ""
    assert "None" not in path.read_text(encoding="utf-8")

    # booleans are lower-case words, not Python's capitalised ones.
    assert isinstance(row["ci_present"], bool)
    assert cells["ci_present"] == ("true" if row["ci_present"] else "false")

    # containers are compact, key-sorted JSON that reads back as what the row holds.
    assert json.loads(cells["commits_by_month"]) == row["commits_by_month"]
    assert json.loads(cells["languages"]) == row["languages"]
    assert cells["languages"] == json.dumps(row["languages"], sort_keys=True, separators=(",", ":"))
    # and a plain number is its own digits.
    assert cells["loc"] == str(row["loc"])


def test_build_probe_never_overlaps_readers(py_repo):
    clock = orc.LaneClock()
    orc.measure(py_repo, clock=clock)
    assert clock.overlaps("build") == []
    assert clock.seconds("tree") is not None
    assert "lane wall clock" in clock.table() and "lane wall clock" in clock.note()
    # Every reader ran at the same time as at least one other, which is the whole point.
    assert clock.overlaps("tree") != []
    # Classification reads the tree lane's output, so it waits for it.
    assert "classify" not in clock.overlaps("tree")


def test_a_labelled_clock_names_the_repository_on_every_line(capsys):
    """Several repositories measured at once share one stderr, so the lane lines have to
    say WHICH repository they are about or they are noise."""
    clock = orc.LaneClock(label="api")
    with clock.lane("tree"):
        pass
    err = capsys.readouterr().err.splitlines()
    assert err == ["api: [lane] tree started", err[1]]
    assert err[1].startswith("api: [lane] tree finished after ")


def test_an_unlabelled_clock_prints_exactly_as_before(capsys):
    clock = orc.LaneClock()
    with clock.lane("tree"):
        pass
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "[lane] tree started"
    assert err[1].startswith("[lane] tree finished after ")


def test_a_labelled_heartbeat_names_the_repository_too(capsys):
    clock = orc.LaneClock(interval=1, label="api")
    with clock.lane("tree"):
        clock.start_heartbeat()
        deadline = time.monotonic() + 5
        while "[alive]" not in capsys.readouterr().err and time.monotonic() < deadline:
            time.sleep(0.1)
        clock.stop_heartbeat()
    # The pulse above was consumed by the poll, so run one more and read it whole.
    clock2 = orc.LaneClock(interval=1, label="api")
    with clock2.lane("tree"):
        clock2.start_heartbeat()
        time.sleep(1.4)
        clock2.stop_heartbeat()
    lines = capsys.readouterr().err.splitlines()
    alive = [line for line in lines if "[alive]" in line]
    assert alive and all(line.startswith("api: [alive] ") for line in alive)


def test_two_runs_over_an_unchanged_tree_agree_on_the_digest(py_repo):
    """The digest is what ties a measurement to a tree, so measuring must not disturb it.

    Running `measure` twice against a repository nobody touched in between has to produce
    the same `repo_digest`. It would not if the run wrote anything into the tree, which is
    why the CLI refuses an `--out` inside a measured repository.
    """
    first, first_measurement = orc.measure(py_repo, build_level="none")
    second, second_measurement = orc.measure(py_repo, build_level="none")
    assert first["repo_digest"] == second["repo_digest"]
    assert first_measurement["repo_digest"] == first["repo_digest"]
    assert first["fake_repo_name"] == second["fake_repo_name"]
