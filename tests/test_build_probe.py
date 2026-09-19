"""`build/probe.py`, driven end to end without any toolchain but Python.

Every fixture below is a "project" in an ecosystem that does not exist. A test-only planner is
registered under that ecosystem name and a test-only manifest filename is added to discovery's
marker table, so `collect()` walks the tree, finds the roots, plans them and runs them exactly
as it would a real Node or Go project -- except that every command it runs is a short
`python -c` script this file wrote. That is what lets the phase sequencing, the classification,
the budget arithmetic and the snapshot guarantee be tested on a machine with nothing installed.

Two expectations in the task this file implements disagree with what the probe's own rules
produce, and the rules win. Both are noted at the test that covers them.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from hazina_scan.build import discover, ecosystems, probe

# --- the fake ecosystems -----------------------------------------------------------------

#: Eight ecosystem names that exist only here, each with a manifest filename of its own, so a
#: fixture tree can hold roots of several "ecosystems" the way a real polyglot repository does.
FAKE = tuple(f"probefake{n}" for n in range(1, 9))
MANIFEST = {eco: f"{eco}.manifest.json" for eco in FAKE}

#: A script that exits cleanly and says nothing.
QUIET_OK = "pass"
#: A script that never returns, so the phase cap has to stop it -- and has to stop the process
#: group, since this one also leaves a grandchild behind.
HANG = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    "time.sleep(600)"
)
#: A package index refusing the credentials it was offered.
AUTH_REFUSED = (
    "import sys; sys.stderr.write("
    "'npm ERR! code E401\\n"
    "npm ERR! 401 Unauthorized - GET https://packages.example-internal.test/-/widget\\n'); "
    "sys.exit(1)"
)
#: A package index that could not be reached at all, which is a statement about this host.
NO_NETWORK = (
    "import sys; sys.stderr.write("
    "'npm ERR! code ENOTFOUND\\n"
    "npm ERR! getaddrinfo ENOTFOUND registry.npmjs.org\\n'); "
    "sys.exit(1)"
)


def _cmd(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def _rewrites_lockfile(root: Path) -> str:
    target = root / "package-lock.json"
    return f"open({str(target)!r}, 'w').write('{{\"lockfileVersion\": 99}}')"


def _writes_coverage(target: Path, summary: str) -> str:
    payload = json.dumps(
        {"totals": {"percent_covered": 87.5, "covered_lines": 7, "num_statements": 8}}
    )
    return f"open({str(target)!r}, 'w').write({payload!r}); print({summary!r})"


def _planner(project, scratch: Path, env: dict, timeout: int, restore: list) -> dict:
    """Build a plan for one fake project out of the JSON its manifest carries.

    The manifest is the whole test interface: each key names one phase and the value picks
    which of the scripts above that phase runs. A key left out means the plan simply has no
    command for that phase, which is itself a case the probe has to handle.
    """
    spec = json.loads((project.root / MANIFEST[project.ecosystem]).read_text())
    coverage_file = scratch / "coverage.json"
    scripts = {
        "ok": QUIET_OK,
        "hang": HANG,
        "auth": AUTH_REFUSED,
        "offline": NO_NETWORK,
        "rewrite_lock": _rewrites_lockfile(project.root),
        "collect3": "print('collected 3 items')",
        "collect0": "print('collected 0 items')",
        "pass3": "print('===== 3 passed in 0.10s =====')",
        "pass3_cov": _writes_coverage(coverage_file, "===== 3 passed in 0.10s ====="),
    }
    plan: dict = {
        "toolchain": "node",
        "tool": sys.executable,
        "locked": _cmd(scripts[spec.get("install", "ok")]),
        "relaxed": None,
        "harness": None,
        "build": _cmd(scripts[spec["build"]]) if spec.get("build") else None,
        "discover": (_cmd(scripts[spec["discover"]]), "pytest") if spec.get("discover") else None,
        "test": (_cmd(scripts[spec["test"]]), "pytest") if spec.get("test") else None,
        "coverage": ("coveragepy", coverage_file) if spec.get("coverage") else None,
        "coverage_reason": None if spec.get("coverage") else "this ecosystem reports no coverage",
    }
    return plan


@pytest.fixture
def fake_ecosystems(monkeypatch):
    """Teach discovery and the planner registry about the eight ecosystems used here."""
    monkeypatch.setattr(
        discover,
        "_MARKERS",
        discover._MARKERS + tuple((eco, (MANIFEST[eco],)) for eco in FAKE),
    )
    for eco in FAKE:
        monkeypatch.setitem(ecosystems.PLANNERS, eco, _planner)
    # Keep the per-project evidence on the returned block so the tests can read the phase
    # records. `collect()` drops it by default because it is evidence rather than measurement.
    monkeypatch.setattr(probe, "_DETAIL_KEYS", ())


def _project(repo: Path, name: str, spec: dict, eco: str = FAKE[0]) -> Path:
    root = repo / name if name != "." else repo
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST[eco]).write_text(json.dumps(spec))
    # A little filler so the root has a weight the allocator can order by.
    (root / "source.txt").write_text("x" * 32)
    return root


def _records(block: dict) -> list[dict]:
    return block["build_projects"]["projects"]


def _phase(record: dict, name: str) -> dict:
    return next(p for p in record["phases"] if p["phase"] == name)


# --- the success path --------------------------------------------------------------------


def test_a_project_that_passes_every_phase_reports_four_executed_terms(fake_ecosystems, tmp_path):
    _project(
        tmp_path,
        ".",
        {
            "install": "ok",
            "build": "ok",
            "discover": "collect3",
            "test": "pass3_cov",
            "coverage": True,
        },
    )
    block = probe.collect(tmp_path, level="full", timeout=30, budget_seconds=120)

    assert block["install_ok"] is True
    assert block["build_ok"] is True
    assert block["tests_discovered"] is True
    assert block["build_and_tests_ran"] is True
    assert block["failure_class"] == "NONE"
    assert block["repo_intrinsic_failure"] is False
    assert block["timed_out"] is False
    assert block["coverage_pct"] == 87.5
    assert block["coverage_method"] == "coverage.py via pytest-cov"
    assert block["coverage_unsupported_reason"] is None
    assert block["observed_runnability"] == 4
    assert block["observed_runnability_reason"] is None
    assert block["discover_runnability"] == 3
    assert block["build_remediation_effort"] == "none"
    assert block["run_budget_exhausted"] is False
    assert block["build_level"] == "full"

    record = _records(block)[0]
    assert [p["status"] for p in record["phases"]] == ["passed"] * 5
    assert record["n_tests_collected"] == 3
    assert record["n_passed"] == 3
    assert record["n_failed"] == 0


def test_the_discover_level_stops_before_the_suite(fake_ecosystems, tmp_path):
    _project(
        tmp_path,
        ".",
        {
            "install": "ok",
            "build": "ok",
            "discover": "collect3",
            "test": "pass3_cov",
            "coverage": True,
        },
    )
    block = probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)

    assert block["build_level"] == "discover"
    assert block["install_ok"] is True
    assert block["build_ok"] is True
    assert block["tests_discovered"] is True
    # The suite was never attempted, so the executed index is absent rather than low.
    assert block["build_and_tests_ran"] is None
    assert block["observed_runnability"] is None
    assert "build level" in block["observed_runnability_reason"]
    # The discover-level index is fully executed even so, which is the point of having it.
    assert block["discover_runnability"] == 3
    assert block["coverage_pct"] is None

    record = _records(block)[0]
    assert _phase(record, "test")["status"] == "skipped_level"
    assert _phase(record, "test")["reason_code"] == "level_discover_only"


def test_a_runner_that_collects_nothing_is_a_measurement_not_a_failure(fake_ecosystems, tmp_path):
    _project(tmp_path, ".", {"install": "ok", "discover": "collect0", "test": "pass3"})
    block = probe.collect(tmp_path, level="full", timeout=30, budget_seconds=120)

    record = _records(block)[0]
    assert _phase(record, "discover")["status"] == "no_tests"
    assert _phase(record, "discover")["attribution"] == "repository"
    assert _phase(record, "test")["status"] == "no_tests"
    assert block["tests_discovered"] is False
    assert block["build_ok"] is True
    # Three of the four terms were observed and one of them is honestly zero.
    assert block["observed_runnability"] == 1
    assert block["discover_runnability"] == 2


# --- a credential failure ------------------------------------------------------------------


def test_a_rejected_credential_with_a_private_index_in_the_tree_is_the_repositorys(
    fake_ecosystems, tmp_path
):
    """The task sheet for this module predicted `attribution: runner` and a null `build_ok`.

    The rules produce neither, and the rules are the specification. A tree that checks in an
    index configuration naming a non-public host is the side that asked for the private index,
    so the coarse verdict is repository-intrinsic; the finer class is a credential one, so the
    phase is attributed to `credentials` -- which the roll-up counts with the repository, not
    with the runner. `install_ok` is therefore a real False and `build_ok` follows it down.
    The companion case below covers what a credential failure looks like without that file,
    and it is still not the runner.
    """
    root = _project(tmp_path, ".", {"install": "auth"})
    (root / ".npmrc").write_text(
        "registry=https://packages.example-internal.test/\n"
        "//packages.example-internal.test/:_authToken=not-a-real-token\n"
    )
    block = probe.collect(tmp_path, level="full", timeout=30, budget_seconds=120)

    record = _records(block)[0]
    assert _phase(record, "resolve")["status"] == "failed"
    assert _phase(record, "resolve")["reason_code"] == "private_registry"
    assert record["attribution"] == "credentials"
    assert record["failure_class"] == "REPO_INTRINSIC"
    assert block["install_ok"] is False
    assert block["build_ok"] is False
    assert block["repo_intrinsic_failure"] is True
    assert block["failure_class"] == "REPO_INTRINSIC"
    # The remediation signatures are matched against the failure CLASS, not the raw log, so a
    # credential class whose name carries an underscore matches none of them. Unknown effort is
    # the honest answer there, and this field is reported rather than scored either way.
    assert block["build_remediation_effort"] == "unknown"
    # Everything downstream is blocked, and inherits the owner of the failure that blocked it.
    assert [p["status"] for p in record["phases"][1:]] == ["blocked"] * 4
    assert {p["attribution"] for p in record["phases"][1:]} == {"credentials"}


def test_a_rejected_credential_with_no_index_config_in_the_tree_is_not_intrinsic(
    fake_ecosystems, tmp_path
):
    _project(tmp_path, ".", {"install": "auth"})
    block = probe.collect(tmp_path, level="full", timeout=30, budget_seconds=120)

    record = _records(block)[0]
    # Nothing in the tree asked for a private index, so the credentials that were refused were
    # this host's: the coarse verdict moves off the repository even though the finer class does
    # not change.
    assert record["failure_class"] == "ENVIRONMENT"
    assert record["build_error_class"] == "private_registry"
    assert block["repo_intrinsic_failure"] is False


def test_an_unreachable_index_leaves_the_build_verdict_null(fake_ecosystems, tmp_path):
    """The runner-owned failure the task sheet was reaching for: absent, not false."""
    _project(tmp_path, ".", {"install": "offline"})
    block = probe.collect(tmp_path, level="full", timeout=30, budget_seconds=120)

    record = _records(block)[0]
    assert record["build_error_class"] == "no_network"
    assert record["attribution"] == "external_service"
    assert block["install_ok"] is None
    assert block["build_ok"] is None
    assert block["observed_runnability"] is None
    # An absent build verdict is reported before anything is said about the suite: an index
    # summing a term that was never observed is the failure this whole branch exists to avoid.
    assert "never actually observed" in block["observed_runnability_reason"]
    assert block["discover_runnability"] is None
    assert block["repo_intrinsic_failure"] is False


# --- a phase that hangs ----------------------------------------------------------------------


def test_a_hanging_suite_is_killed_inside_the_phase_cap_and_nulls_the_index(
    fake_ecosystems, tmp_path
):
    _project(tmp_path, ".", {"install": "ok", "discover": "collect3", "test": "hang"})
    started = time.monotonic()
    block = probe.collect(
        tmp_path, level="full", timeout=probe.MIN_PHASE_SECONDS, budget_seconds=120
    )
    elapsed = time.monotonic() - started

    # The phase cap is the ceiling, and the drain after the group is killed is bounded too.
    assert elapsed < probe.MIN_PHASE_SECONDS + 15
    assert block["timed_out"] is True
    record = _records(block)[0]
    assert _phase(record, "test")["status"] == "timed_out"
    assert _phase(record, "test")["attribution"] == "runner"
    assert _phase(record, "coverage")["status"] == "blocked"
    # The build did happen and the tests were found; only the execution terms are missing.
    assert block["build_ok"] is True
    assert block["tests_discovered"] is True
    assert block["discover_runnability"] == 3
    assert block["build_and_tests_ran"] is None
    assert block["observed_runnability"] is None
    assert "this runner" in block["observed_runnability_reason"]


# --- the budget ------------------------------------------------------------------------------


def test_eleven_hanging_roots_return_inside_the_budget_with_the_rest_unmeasured(
    fake_ecosystems, tmp_path
):
    """The budget test. Nothing here can finish, so the only thing that can stop the run is
    the clock -- and it has to stop it, report which roots it never reached, and say so."""
    for n in range(11):
        _project(tmp_path, f"part{n:02d}", {"install": "hang"}, eco=FAKE[n % len(FAKE)])

    budget_seconds = 40
    started = time.monotonic()
    block = probe.collect(
        tmp_path,
        level="full",
        timeout=probe.MIN_PHASE_SECONDS,
        budget_seconds=budget_seconds,
        max_projects=probe.MAX_PROBED_PROJECTS,
    )
    elapsed = time.monotonic() - started

    assert elapsed < budget_seconds + 10, f"probe overran its budget by {elapsed:.1f}s"
    assert block["run_budget_exhausted"] is True
    assert block["timed_out"] is True

    records = _records(block)
    assert len(records) == 11
    # Eight roots were eligible to be probed and three were past the cap; whichever the clock
    # did not reach is recorded as skipped, with every one of its phases skipped and every
    # measurement null.
    skipped = [r for r in records if r["skipped_reason"]]
    assert len(skipped) >= 4
    for record in skipped:
        assert {p["status"] for p in record["phases"]} == {"skipped_budget"}
        assert {p["attribution"] for p in record["phases"]} == {"runner"}
        assert record["n_tests_collected"] is None
        assert record["coverage_pct"] is None
    assert sum(1 for r in records if r["skipped_reason"] == "project_cap_reached") == 3
    assert any(r["skipped_reason"] == "run_budget_exhausted" for r in records)

    # Half a tree is not a verdict about the tree.
    assert block["build_ok"] is None
    assert block["observed_runnability"] is None
    assert block["discover_runnability"] is None
    assert "unmeasured" in block["note"]
    assert "null, not zero" in block["note"]


def test_a_budget_too_small_to_start_returns_the_stub(tmp_path):
    block = probe.collect(tmp_path, level="full", budget_seconds=0)
    assert block == probe.skipped_budget(0.0)
    assert block["build_skipped"] is True
    assert block["ok"] is True
    assert "null" in block["note"]


def test_the_skipped_budget_stub_names_the_seconds_that_were_left():
    stub = probe.skipped_budget(12.7)
    assert stub["probe"] == "build"
    assert stub["build_skipped"] is True
    assert stub["ok"] is True
    assert "12 seconds" in stub["note"]
    assert set(stub) == {"probe", "build_skipped", "ok", "note"}


def test_the_none_level_runs_nothing(tmp_path):
    block = probe.collect(tmp_path, level="none")
    assert block["build_skipped"] is True
    # Nothing was measured, so the stub carries no measurement at all -- not a false one.
    assert set(block) == {"probe", "build_skipped", "ok", "note"}


def test_an_unknown_level_is_refused(tmp_path):
    with pytest.raises(ValueError):
        probe.collect(tmp_path, level="everything")


# --- putting the tree back -------------------------------------------------------------------


def test_a_lockfile_the_install_rewrote_is_restored(fake_ecosystems, tmp_path):
    root = _project(tmp_path, ".", {"install": "rewrite_lock"})
    original = '{"lockfileVersion": 1, "name": "as-checked-in"}'
    (root / "package-lock.json").write_text(original)

    block = probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)

    assert block["install_ok"] is True
    assert (root / "package-lock.json").read_text() == original


def test_a_lockfile_the_install_created_is_removed_again(fake_ecosystems, tmp_path):
    root = _project(tmp_path, ".", {"install": "rewrite_lock"})
    assert not (root / "package-lock.json").exists()

    probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)

    assert not (root / "package-lock.json").exists()


def test_an_artefact_directory_the_repository_ships_is_left_alone(fake_ecosystems, tmp_path):
    root = _project(tmp_path, ".", {"install": "ok"})
    shipped = root / "build"
    shipped.mkdir()
    (shipped / "kept.txt").write_text("this was here first")

    probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)

    assert (shipped / "kept.txt").read_text() == "this was here first"


# --- the shape of the emitted block ------------------------------------------------------------

#: Every key `collect()` puts on the block. This is the task sheet's list, plus
#: `build_commands_tried`, which the sheet omits but which the block has always carried and the
#: emitted-field table declares.
EXPECTED_KEYS = {
    "probe",
    "ok",
    "error",
    "note",
    "build_level",
    "run_budget_exhausted",
    "build_probe_mode",
    "agentic_fallback_reason",
    "toolchain",
    "build_attempted",
    "build_commands_tried",
    "install_ok",
    "build_ok",
    "observed_runnability",
    "observed_runnability_reason",
    "discover_runnability",
    "tests_discovered",
    "build_and_tests_ran",
    "failure_class",
    "repo_intrinsic_failure",
    "timed_out",
    "coverage_pct",
    "coverage_method",
    "coverage_unsupported_reason",
    "build_remediation_effort",
    "build_remediation_notes",
    "runtime_lanes_declared",
    "runtime_lanes_unsatisfied",
    "runtime_requested",
    "runtime_used",
    "runtime_resolution_note",
    "repair_offered",
    "repair_attempted_n",
    "repair_succeeded_n",
    "repair_refused_n",
    "repair_rejected_source_edit_n",
    "repair_seconds",
}


@pytest.mark.parametrize("level", ["discover", "full"])
def test_the_emitted_key_set_is_exactly_the_declared_one(monkeypatch, tmp_path, level):
    monkeypatch.setattr(
        discover,
        "_MARKERS",
        discover._MARKERS + tuple((eco, (MANIFEST[eco],)) for eco in FAKE),
    )
    monkeypatch.setitem(ecosystems.PLANNERS, FAKE[0], _planner)
    _project(
        tmp_path,
        ".",
        {
            "install": "ok",
            "build": "ok",
            "discover": "collect3",
            "test": "pass3_cov",
            "coverage": True,
        },
    )
    block = probe.collect(tmp_path, level=level, timeout=30, budget_seconds=120)
    assert set(block) == EXPECTED_KEYS


def test_an_empty_tree_reports_no_build_definition(tmp_path):
    block = probe.collect(tmp_path, level="full", budget_seconds=60)
    assert set(block) == EXPECTED_KEYS
    assert block["build_attempted"] is False
    assert block["toolchain"] is None
    assert block["install_ok"] is False
    assert block["build_ok"] is False
    assert block["observed_runnability"] == 0
    assert block["discover_runnability"] == 0
    assert block["failure_class"] == "NONE"
    assert block["build_remediation_effort"] == "unknown"


def test_the_repair_fields_report_a_pass_this_tool_does_not_offer(fake_ecosystems, tmp_path):
    _project(tmp_path, ".", {"install": "ok"})
    block = probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)
    assert block["repair_offered"] is False
    assert all(
        block[key] is None
        for key in EXPECTED_KEYS
        if key.startswith("repair_") and key != "repair_offered"
    )
    # Only offered when repair actually ran: a pass this tool never enters has no candidates
    # to count, and the count is absent rather than null so the two facts stay distinguishable.
    assert "repair_candidates_n" not in block


def test_the_probe_names_itself_and_the_mode_it_ran_in(fake_ecosystems, tmp_path):
    _project(tmp_path, ".", {"install": "ok"})
    block = probe.collect(tmp_path, level="discover", timeout=30, budget_seconds=120)
    assert block["probe"] == "build"
    assert block["build_probe_mode"] == "deterministic"
    # Only an agentic run names a model: a deterministic one has no field to leave null.
    assert "build_probe_model" not in block
    assert block["agentic_fallback_reason"] is None
    assert block["ok"] is True


# --- the pieces, on their own ------------------------------------------------------------------


def test_a_checked_in_index_config_is_only_private_when_it_names_a_private_host(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (public / ".npmrc").write_text("registry=https://registry.npmjs.org/\n")
    assert probe.declares_private_registry(public) is False

    private = tmp_path / "private"
    private.mkdir()
    (private / ".npmrc").write_text("registry=https://npm.example-internal.test/\n")
    assert probe.declares_private_registry(private) is True

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / ".npmrc").write_text("_authToken=opaque\n")
    # A token with no host named beside it still says the tree wants a private index.
    assert probe.declares_private_registry(bare) is True

    assert probe.declares_private_registry(tmp_path / "missing") is False


def test_an_ambiguous_failure_is_never_charged_to_the_repository():
    assert probe.classify("something nobody has a pattern for", False) == "UNCLASSIFIED"
    assert probe.attribution("UNCLASSIFIED", "unclassified") == "runner"
    # A class that is positive evidence about this machine overturns a repository verdict.
    assert probe.reconcile("REPO_INTRINSIC", "wrong_runtime", None) == "ENVIRONMENT"
    # One that merely looks repository-flavoured does not move toward the repository.
    assert probe.reconcile("UNCLASSIFIED", "compile_error", None) == "UNCLASSIFIED"


def test_a_timeout_outranks_whatever_the_log_happened_to_say():
    assert probe.classify("cannot find symbol", True) == "TIMEOUT"
    assert probe.error_class("cannot find symbol", True) == "timeout"
    assert probe.attribution("TIMEOUT", "timeout") == "runner"


def test_an_empty_log_is_named_rather_than_left_unclassified():
    assert probe.error_class("   \n", False) == "unknown_no_output"


def test_the_snapshot_restores_created_modified_and_untouched_files(tmp_path):
    kept = tmp_path / "go.sum"
    kept.write_text("original bytes\n")
    absent = tmp_path / "Cargo.lock"

    snap = probe.snapshot([tmp_path])
    kept.write_text("rewritten by an installer\n")
    absent.write_text("generated by an installer\n")
    probe.restore_snapshot(snap)

    assert kept.read_text() == "original bytes\n"
    assert not absent.exists()


def test_scratch_cleanup_removes_a_read_only_cache(tmp_path):
    """Go's module cache -- and pip's, and cargo's -- leaves its directories read-only so
    nothing edits a cached download by accident, which is exactly what used to make
    `/tmp/hazina-build-*` survive `collect()`: a plain `ignore_errors=True` rmtree gives up
    silently on the first permission it cannot change rather than restoring it and retrying.
    """
    scratch = tmp_path / "scratch"
    cache = scratch / "mod"
    cache.mkdir(parents=True)
    cached_file = cache / "example.com@v1.0.0.info"
    cached_file.write_text("cached\n")
    cached_file.chmod(0o444)
    cache.chmod(0o555)

    probe._rmtree(scratch)

    assert not scratch.exists()


# --- coverage rounding, per reporter ------------------------------------------------------------

#: 62.8437 rounds differently at one and two decimal places (62.8 vs 62.84), which is exactly
#: the distinction a regression in either direction would blur.
_COVERAGE_SAMPLE = 62.8437


def test_coveragepy_coverage_keeps_two_decimal_places(tmp_path):
    target = tmp_path / "coverage.json"
    target.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": _COVERAGE_SAMPLE,
                    "num_statements": 1000,
                    "covered_lines": 628,
                }
            }
        )
    )
    pct, covered, total, method = probe._read_coverage("coveragepy", target, {}, tmp_path, 30)
    assert pct == 62.84
    assert covered == 628
    assert total == 1000
    assert method == "coverage.py via pytest-cov"


def test_istanbul_coverage_keeps_two_decimal_places(tmp_path):
    target = tmp_path / "coverage-summary.json"
    target.write_text(
        json.dumps({"total": {"lines": {"pct": _COVERAGE_SAMPLE, "covered": 628, "total": 1000}}})
    )
    pct, covered, total, method = probe._read_coverage("istanbul", target, {}, tmp_path, 30)
    assert pct == 62.84
    assert covered == 628
    assert total == 1000
    assert method == "istanbul json-summary"


def test_go_coverage_is_read_at_full_reporter_precision(monkeypatch, tmp_path):
    target = tmp_path / "cover.out"
    target.write_text("mode: set\n")

    def _fake_run(argv, cwd, env, timeout):
        return 0, f"total:\t(statements)\t{_COVERAGE_SAMPLE}%\n", False

    monkeypatch.setattr(probe, "_run", _fake_run)
    pct, covered, total, method = probe._read_coverage("go", target, {}, tmp_path, 30)
    # `go tool cover` prints whatever precision it chose; this reporter has never rounded that
    # figure, unlike the JSON-based readers above, and a real reporter never emits six decimals.
    assert pct == _COVERAGE_SAMPLE
    assert covered is None
    assert total is None
    assert method == "go test -coverprofile"
