import csv
import json
import time

import pytest

from hazina_scan import orchestrator as orc


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


def test_other_build_levels_are_refused(py_repo):
    with pytest.raises(NotImplementedError):
        orc.measure(py_repo, build_level="full")


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
