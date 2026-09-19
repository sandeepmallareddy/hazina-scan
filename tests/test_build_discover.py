"""Unit tests for `hazina_scan.build.discover`: project discovery and the shared time budget.

Every fixture here is built on disk under `tmp_path`; nothing needs git, a package manager or
a language toolchain to run. The discovery walk never consults git at all, so a `.gitignore`d
directory is scanned exactly like any other -- one of the tests below pins that down directly.
`Budget` is exercised with `time.monotonic` monkeypatched to a value this test controls, so the
arithmetic is checked against exact numbers rather than against real wall-clock drift.
"""

from __future__ import annotations

import json

import pytest

from hazina_scan.build import discover

# --- Budget ----------------------------------------------------------------------------


def _clock(monkeypatch, start: float = 0.0) -> dict:
    """Patch `discover.time.monotonic` to a value this test can move at will."""
    state = {"now": start}
    monkeypatch.setattr(discover.time, "monotonic", lambda: state["now"])
    return state


def test_budget_remaining_and_elapsed_follow_the_clock(monkeypatch):
    clock = _clock(monkeypatch)
    budget = discover.Budget(100.0, phase_cap=50)
    assert budget.total == 100.0
    assert budget.remaining() == 100.0
    assert budget.elapsed() == 0.0
    clock["now"] = 40.0
    assert budget.remaining() == 60.0
    assert budget.elapsed() == 40.0


def test_budget_exhausted_flips_once_remaining_drops_below_min_phase_seconds(monkeypatch):
    clock = _clock(monkeypatch)
    budget = discover.Budget(20.0)
    clock["now"] = 5.0  # remaining = 15, exactly at the floor: not yet exhausted
    assert budget.exhausted is False
    clock["now"] = 6.0  # remaining = 14
    assert budget.exhausted is True


def test_budget_remaining_never_goes_negative_after_the_deadline_passes(monkeypatch):
    clock = _clock(monkeypatch)
    budget = discover.Budget(10.0)
    clock["now"] = 500.0
    assert budget.remaining() == 0.0
    assert budget.elapsed() == 500.0
    assert budget.exhausted is True


def test_budget_for_project_splits_what_remains_by_weighted_share(monkeypatch):
    clock = _clock(monkeypatch)
    budget = discover.Budget(300.0, phase_cap=900)
    # This project is 1 of 3 weight-equivalent units still queued: a third of what remains.
    deadline = budget.for_project(3.0)
    assert deadline == pytest.approx(100.0)
    clock["now"] = 100.0
    # A lone remaining project gets everything that is left.
    deadline_alone = budget.for_project(1.0)
    assert deadline_alone == pytest.approx(100.0 + 200.0)


def test_budget_for_project_floors_at_min_project_seconds(monkeypatch):
    _clock(monkeypatch)
    budget = discover.Budget(40.0)
    # A naive 1/10 share (4s) is too thin to attempt anything, so the floor takes over.
    deadline = budget.for_project(10.0)
    assert deadline == pytest.approx(discover.MIN_PROJECT_SECONDS)


def test_budget_for_project_floor_is_itself_capped_by_what_remains(monkeypatch):
    _clock(monkeypatch)
    budget = discover.Budget(10.0)  # less than MIN_PROJECT_SECONDS to begin with
    deadline = budget.for_project(5.0)
    assert deadline == pytest.approx(10.0)


def test_budget_for_phase_is_capped_by_the_phase_ceiling(monkeypatch):
    _clock(monkeypatch)
    budget = discover.Budget(1000.0, phase_cap=60)
    deadline = budget.for_project(1.0)  # a huge project share
    assert budget.for_phase(deadline) == 60


def test_budget_for_phase_is_capped_by_what_is_left_of_the_project_share(monkeypatch):
    clock = _clock(monkeypatch)
    budget = discover.Budget(1000.0, phase_cap=900)
    project_deadline = 40.0
    assert budget.for_phase(project_deadline) == 40
    clock["now"] = 10.0
    assert budget.for_phase(project_deadline) == 30


def test_budget_for_phase_is_capped_by_the_whole_run_even_past_the_project_share(monkeypatch):
    _clock(monkeypatch)
    budget = discover.Budget(50.0, phase_cap=900)
    far_off_deadline = 10_000.0
    assert budget.for_phase(far_off_deadline) == 50


def test_budget_for_phase_returns_zero_rather_than_a_command_that_cannot_finish(monkeypatch):
    _clock(monkeypatch)
    budget = discover.Budget(10.0, phase_cap=900)  # remaining is already below MIN_PHASE_SECONDS
    assert budget.for_phase(100.0) == 0


# --- is_ancillary ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,expected",
    [
        (".", False),
        ("src", False),
        ("docs", True),
        ("docs/guide", True),
        ("packages/examples", True),
        ("tools/docsgen", False),  # a whole segment must match, not a substring
        ("DOCS", True),
    ],
)
def test_is_ancillary_matches_whole_path_segments(rel, expected):
    assert discover.is_ancillary(rel) is expected


# --- scan / peek_ecosystems ----------------------------------------------------------------


def test_scan_finds_markers_and_counts_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "README.md").write_text("hi\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")

    found, report = discover.scan(tmp_path)
    assert found[tmp_path] == {"python"}
    assert report["files_scanned"] == 3  # pyproject.toml + README.md at root, a.py under src
    assert report["dirs_scanned"] >= 2


def test_scan_ignores_vendor_and_node_modules_at_any_depth(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    nested_vendor = tmp_path / "vendor" / "somelib"
    nested_vendor.mkdir(parents=True)
    (nested_vendor / "package.json").write_text("{}")
    nested_modules = tmp_path / "a" / "node_modules" / "pkg"
    nested_modules.mkdir(parents=True)
    (nested_modules / "package.json").write_text("{}")

    found, _report = discover.scan(tmp_path)
    assert set(found) == {tmp_path}


def test_peek_ecosystems_reads_one_directory_without_descending(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    assert discover.peek_ecosystems(tmp_path) == ["go", "rust"]
    assert discover.peek_ecosystems(tmp_path / "missing") == []


def test_scan_records_depth_limit_with_peeked_ecosystems(tmp_path):
    deep = tmp_path
    for _ in range(discover.MAX_DEPTH + 2):
        deep = deep / "d"
        deep.mkdir()
    (deep / "go.mod").write_text("module example.com/deep\n\ngo 1.21\n")

    found, report = discover.scan(tmp_path)
    assert deep not in found
    depth_gaps = [o for o in report["omitted"] if o["reason_code"] == "depth_limit"]
    assert depth_gaps  # at least the first directory past MAX_DEPTH was recorded


# --- node_workspace_globs / cargo_workspace -------------------------------------------------


def test_node_workspace_globs_array_form(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["packages/*", "apps/*"]}))
    assert discover.node_workspace_globs(tmp_path) == ["packages/*", "apps/*"]


def test_node_workspace_globs_object_form(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": {"packages": ["libs/*"]}}))
    assert discover.node_workspace_globs(tmp_path) == ["libs/*"]


def test_node_workspace_globs_pnpm_yaml(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "root"}))
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n  - 'tools/*'\n")
    assert discover.node_workspace_globs(tmp_path) == ["packages/*", "tools/*"]


def test_node_workspace_globs_no_declaration_is_empty(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "root"}))
    assert discover.node_workspace_globs(tmp_path) == []


def test_cargo_workspace_none_without_workspace_section(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    assert discover.cargo_workspace(tmp_path) is None


def test_cargo_workspace_members_and_exclude(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/skip"]\n'
    )
    members, exclude = discover.cargo_workspace(tmp_path)
    assert members == ["crates/*"]
    assert exclude == ["crates/skip"]


# --- authoritative_roots -------------------------------------------------------------------


def test_authoritative_roots_covers_a_node_workspace_root(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}))
    found = {tmp_path: {"node"}}
    covers = discover.authoritative_roots(tmp_path, found)
    assert covers["node"][tmp_path] == ["packages/*"]


# --- discover_projects: fixtures from the brief ---------------------------------------------


def test_discover_single_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")

    projects, report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    project = projects[0]
    assert project.ecosystem == "python"
    assert project.rel == "."
    assert project.required is True
    assert project.workspace is False
    assert report["n_projects"] == 1
    assert report["ecosystems"] == ["python"]
    assert report["no_manifest"] is False


def test_discover_npm_workspaces_monorepo_with_three_packages(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["packages/*"]})
    )
    packages = tmp_path / "packages"
    for name in ("a", "b", "c"):
        pkg = packages / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": name}))
        (pkg / "index.js").write_text("module.exports = {};\n")

    projects, report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    root_project = projects[0]
    assert root_project.rel == "."
    assert root_project.ecosystem == "node"
    assert root_project.workspace is True
    assert sorted(root_project.members) == ["packages/a", "packages/b", "packages/c"]
    assert report["n_projects"] == 1


def test_discover_cargo_workspace(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    crates = tmp_path / "crates"
    for name in ("core", "cli"):
        crate = crates / name
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n')
        (crate / "src").mkdir()
        (crate / "src" / "lib.rs").write_text("pub fn go() {}\n")

    projects, report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    root_project = projects[0]
    assert root_project.ecosystem == "rust"
    assert root_project.workspace is True
    assert sorted(root_project.members) == ["crates/cli", "crates/core"]
    assert report["n_projects"] == 1


def test_discover_ignores_nested_vendor_and_node_modules(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    vendored = tmp_path / "vendor" / "somelib"
    vendored.mkdir(parents=True)
    (vendored / "package.json").write_text("{}")
    dep = tmp_path / "node_modules" / "somepkg"
    dep.mkdir(parents=True)
    (dep / "package.json").write_text("{}")

    projects, _report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    assert projects[0].ecosystem == "python"


def test_discover_caps_at_max_projects_keeping_the_largest(tmp_path):
    # Names sort the same way their size does: "p00" is both alphabetically first and the
    # biggest, so the cap's real rule (scan order) and "largest kept" line up on purpose.
    for i in range(30):
        root = tmp_path / f"p{i:02d}"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        for j in range(30 - i):
            (root / f"file{j}.py").write_text("x = 1\n")

    projects, report = discover.discover_projects(tmp_path)
    assert len(projects) == discover.MAX_PROJECTS == 24
    kept = {p.rel for p in projects}
    assert kept == {f"p{i:02d}" for i in range(24)}
    weights = [p.weight for p in projects]
    assert weights == sorted(weights, reverse=True)
    assert report["truncated"] is True
    omitted = {o["root"] for o in report["omitted_roots"]}
    assert omitted == {f"p{i:02d}" for i in range(24, 30)}


def test_discover_still_scans_a_gitignored_directory(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored/\n")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "go.mod").write_text("module example.com/ignored\n\ngo 1.21\n")

    projects, _report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    assert projects[0].rel == "ignored"
    assert projects[0].ecosystem == "go"


def test_discover_lifts_ancillary_when_it_is_all_there_is(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    projects, _report = discover.discover_projects(tmp_path)
    assert len(projects) == 1
    assert projects[0].required is True


def test_discover_keeps_ancillary_demoted_when_a_real_project_exists(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    projects, _report = discover.discover_projects(tmp_path)
    by_rel = {p.rel: p for p in projects}
    assert by_rel["."].required is True
    assert by_rel["docs"].required is False


# --- survey / estimate_seconds --------------------------------------------------------------


def test_survey_keys_and_values(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")

    result = discover.survey(tmp_path)
    assert set(result) == {
        "n_projects",
        "n_projects_probed",
        "n_projects_over_cap",
        "ecosystems",
        "files_scanned",
        "dirs_scanned",
    }
    assert result["n_projects"] == 1
    assert result["n_projects_probed"] == 1
    assert result["n_projects_over_cap"] == 0
    assert result["ecosystems"] == ["go"]


def test_survey_respects_max_projects_over_cap(tmp_path):
    for i in range(3):
        root = tmp_path / f"proj{i}"
        root.mkdir()
        (root / "go.mod").write_text(f"module example.com/x{i}\n\ngo 1.21\n")

    result = discover.survey(tmp_path, max_projects=2)
    assert result["n_projects"] == 3
    assert result["n_projects_probed"] == 2
    assert result["n_projects_over_cap"] == 1


@pytest.mark.parametrize("level", ["discover", "full"])
def test_estimate_seconds_scales_with_projects_and_files(level):
    survey_report = {"n_projects_probed": 2, "files_scanned": 5000}
    got = discover.estimate_seconds(survey_report, level)
    per_project = {"discover": 30.0, "full": 90.0}[level]
    per_kfile = {"discover": 4.0, "full": 20.0}[level]
    assert got == pytest.approx((per_project * 2) + (per_kfile * 5.0))


def test_estimate_seconds_zero_outside_discover_and_full():
    survey_report = {"n_projects_probed": 5, "files_scanned": 5000}
    assert discover.estimate_seconds(survey_report, "none") == 0.0
    assert discover.estimate_seconds(survey_report, "not-a-level") == 0.0
