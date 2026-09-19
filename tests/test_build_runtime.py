"""Version parsing, declaration reading, and host-side resolution for `build/runtime.py`.

No toolchain beyond Python and git is needed here: the "installed runtime" candidates in
`test_resolve_*` are tiny `#!/bin/sh` shims on a fake PATH, never a real interpreter.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from hazina_scan.build import runtime


def _shim(path: Path, banner: str) -> None:
    """A tiny, executable stand-in for a runtime that only knows how to print its banner."""
    path.write_text(f"#!/bin/sh\necho '{banner}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --- parse_spec ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,lane,floor,expected",
    [
        # A PEP 440 comma is an intersection, not a union, so both clauses narrow one window.
        (">=3.9,<3.13", "python", False, [((3, 9), (3, 13))]),
        ("^18", "node", False, [((18,), (19,))]),
        ("~=1.4", "python", False, [((1, 4), (1, 5))]),
        ("1.21", "go", True, [((1, 21), None)]),
        # No lane recognises "nightly" as a keyword, so the date inside it is read as a bare
        # pinned version and windowed like any other four-digit release.
        ("nightly-2024-01-01", "rust", False, [((2024,), (2025,))]),
    ],
)
def test_parse_spec_shapes(spec, lane, floor, expected):
    windows = runtime.parse_spec(spec, lane, floor)
    assert [(w.low, w.high) for w in windows] == expected


@pytest.mark.parametrize(
    "lane,spec,floor,probe,expect_satisfied",
    [
        ("node", "18", False, (18, 4, 0), True),
        ("node", "18", False, (17, 9, 0), False),
        ("python", "3.11", False, (3, 11, 4), True),
        ("python", "3.11", False, (3, 12, 0), False),
        ("go", "1.21", True, (1, 22, 0), True),
        ("go", "1.21", True, (1, 20, 0), False),
        ("rust", "1.70", True, (1, 75, 0), True),
        ("ruby", "3.2.2", False, (3, 2, 2), True),
        ("java", "17", False, (17, 0, 2), True),
        # A level above 8 carries no ceiling: nothing currently shipping has dropped it.
        ("java", "17", False, (21, 0, 1), True),
        ("java", "8", False, (25, 0, 1), False),
        ("dotnet", "8.0", False, (8, 0, 100), True),
    ],
)
def test_parse_spec_one_case_per_lane(lane, spec, floor, probe, expect_satisfied):
    windows = runtime.parse_spec(spec, lane, floor)
    assert runtime.satisfies(probe, windows) is expect_satisfied


def test_parse_spec_empty_placeholder_constrains_nothing():
    assert runtime.parse_spec("*", "node") == []
    assert runtime.parse_spec("latest", "python") == []


# --- declarations ------------------------------------------------------------------------


def test_declarations_nvmrc(tmp_path):
    (tmp_path / ".nvmrc").write_text("18.17.0\n")
    records = runtime.declarations(tmp_path)
    assert len(records) == 1
    assert records[0] == {
        "lane": "node",
        "spec": "18.17.0",
        "source": ".nvmrc",
        "floor": False,
        "windows": records[0]["windows"],
        "requested": "18",
    }


def test_declarations_node_version_file(tmp_path):
    (tmp_path / ".node-version").write_text("v20.11.1\n")
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "node"
    assert record["source"] == ".node-version"


def test_declarations_package_json_engines(tmp_path):
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=16 <21"}}')
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "node"
    assert record["source"] == "package.json engines"
    assert runtime.satisfies((18, 0, 0), record["windows"])
    assert not runtime.satisfies((22, 0, 0), record["windows"])


def test_declarations_python_version_file(tmp_path):
    (tmp_path / ".python-version").write_text("3.11.9\n")
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "python"
    assert record["source"] == ".python-version"


def test_declarations_requires_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.9,<3.13"\n'
    )
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "python"
    assert record["source"] == "pyproject requires-python"


def test_declarations_setup_py_python_requires(tmp_path):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='x', python_requires='>=3.8')\n"
    )
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "python"
    assert record["source"] == "setup.py python_requires"


def test_declarations_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    [record] = runtime.declarations(tmp_path)
    assert record == {
        "lane": "go",
        "spec": "1.21",
        "source": "go.mod go",
        "floor": True,
        "windows": record["windows"],
        "requested": "1.21",
    }
    assert record["windows"][0].low == (1, 21)
    assert record["windows"][0].high is None


def test_declarations_rust_toolchain_toml(tmp_path):
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.75.0"\n')
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "rust"
    assert record["source"] == "rust-toolchain"


def test_declarations_rust_toolchain_bare_file(tmp_path):
    (tmp_path / "rust-toolchain").write_text("1.70.0\n")
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "rust"
    assert record["spec"] == "1.70.0"


def test_declarations_ruby_version_file(tmp_path):
    (tmp_path / ".ruby-version").write_text("3.2.2\n")
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "ruby"
    assert record["source"] == ".ruby-version"


def test_declarations_tool_versions(tmp_path):
    (tmp_path / ".tool-versions").write_text("nodejs 18.17.0\npython 3.11.9\ngolang 1.21.5\n")
    records = {r["lane"]: r for r in runtime.declarations(tmp_path)}
    assert set(records) == {"node", "python", "go"}
    assert all(r["source"] == ".tool-versions" for r in records.values())


def test_declarations_pom_xml_compiler_release(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties>"
        "<maven.compiler.release>17</maven.compiler.release>"
        "</properties></project>"
    )
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "java"
    assert record["source"] == "pom.xml compiler release"


def test_declarations_gradle_toolchain(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "java {\n    toolchain {\n        languageVersion = JavaLanguageVersion.of(21)\n    }\n}\n"
    )
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "java"
    assert record["source"] == "gradle toolchain"


def test_declarations_global_json(tmp_path):
    (tmp_path / "global.json").write_text('{"sdk": {"version": "8.0.100"}}')
    [record] = runtime.declarations(tmp_path)
    assert record["lane"] == "dotnet"
    assert record["source"] == "global.json sdk"


def test_declarations_most_specific_source_wins(tmp_path):
    """A pinned version file outranks a manifest range for the same lane."""
    (tmp_path / ".nvmrc").write_text("18.17.0\n")
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=14"}}')
    [record] = runtime.declarations(tmp_path)
    assert record["source"] == ".nvmrc"


def test_declarations_empty_tree_declares_nothing(tmp_path):
    assert runtime.declarations(tmp_path) == []


# --- resolve ---------------------------------------------------------------------------


@pytest.fixture
def no_real_python(monkeypatch):
    """Take this process's own interpreter, and every system search glob, out of the running
    so a resolve() test can reason about a fake PATH's candidates alone.

    Without this, `candidates()` also globs fixed system locations (`_SEARCH_GLOBS`) such as
    Homebrew's `/opt/homebrew/opt/ruby@3.4/bin` -- a real runtime sitting there on the host
    running the test would compete with (and could beat, or unexpectedly satisfy) the fake
    shims a test sets up on its own PATH, making the outcome depend on what happens to be
    installed on whichever machine runs the suite.
    """
    monkeypatch.setattr(sys, "executable", "/nonexistent/hazina-scan-test-python")
    monkeypatch.setattr(runtime, "_SEARCH_GLOBS", {})


def test_resolve_picks_the_candidate_nearest_the_floor_and_skips_a_home_shim(
    tmp_path, monkeypatch, no_real_python
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.9"\n')

    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    _shim(path_bin / "python3.11", "Python 3.11.9")
    _shim(path_bin / "python3.12", "Python 3.12.3")

    fake_home = tmp_path / "fake-home"
    home_bin = fake_home / "bin"
    home_bin.mkdir(parents=True)
    _shim(home_bin / "python3.10", "Python 3.10.1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    env = {"PATH": os.pathsep.join([str(path_bin), str(home_bin)])}
    plan = runtime.resolve(project, env)

    [record] = plan.records
    assert record["lane"] == "python"
    assert record["satisfied"] is True
    # 3.10.1 sits under HOME and is closer to the floor, but it may never be picked without
    # allow_home=True -- so the winner is the nearest ALLOWED candidate, 3.11.
    assert record["used"] == "3.11.9" or record["used"].startswith("3.11")
    assert plan.interpreter["python"] == str(path_bin / "python3.11")


def test_resolve_reports_unsatisfied_lane_with_no_candidate(tmp_path, monkeypatch, no_real_python):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ruby-version").write_text("3.2.2\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-such-home"))

    env = {"PATH": str(tmp_path / "empty-bin")}
    plan = runtime.resolve(project, env, lanes=("ruby",))

    [record] = plan.records
    assert record["lane"] == "ruby"
    assert record["satisfied"] is False
    assert record["used"] is None
    assert plan.unsatisfied == ["ruby"]


def test_resolve_lanes_filter_narrows_the_work(tmp_path, monkeypatch, no_real_python):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".nvmrc").write_text("18\n")
    (project / ".ruby-version").write_text("3.2.2\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-such-home"))

    plan = runtime.resolve(project, {"PATH": ""}, lanes=("node",))
    assert [r["lane"] for r in plan.records] == ["node"]


def test_resolve_never_selects_a_home_runtime_without_allow_home(
    tmp_path, monkeypatch, no_real_python
):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ruby-version").write_text("3.2.2\n")

    fake_home = tmp_path / "fake-home"
    home_bin = fake_home / "bin"
    home_bin.mkdir(parents=True)
    _shim(home_bin / "ruby", "ruby 3.2.2")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    env = {"PATH": str(home_bin)}
    plan = runtime.resolve(project, env, lanes=("ruby",))
    [record] = plan.records
    assert record["satisfied"] is False
    assert plan.overlay == {}


# --- apply_overlay -----------------------------------------------------------------------


def test_apply_overlay_prepends_path():
    merged = runtime.apply_overlay({"PATH": "/usr/bin"}, {"PATH": "/opt/node/bin"})
    assert merged["PATH"] == "/opt/node/bin" + os.pathsep + "/usr/bin"


def test_apply_overlay_empty_overlay_is_a_no_op():
    env = {"PATH": "/usr/bin"}
    assert runtime.apply_overlay(env, {}) is env


# --- summarise -----------------------------------------------------------------------------


def test_summarise_shape_with_mixed_lanes():
    per_project = [
        [
            {
                "lane": "node",
                "requested": "18",
                "requested_source": ".nvmrc",
                "used": "18.17.0",
                "satisfied": True,
                "note": "the tree asks for node 18 and this host has it",
            }
        ],
        [
            {
                "lane": "ruby",
                "requested": "3.2.2",
                "requested_source": ".ruby-version",
                "used": None,
                "satisfied": False,
                "note": "the tree asks for ruby 3.2.2 and no ruby runtime was found on this host",
            }
        ],
    ]
    summary = runtime.summarise(per_project)
    assert summary == {
        "runtime_lanes_declared": ["node", "ruby"],
        "runtime_lanes_unsatisfied": ["ruby"],
        "runtime_requested": ["node@18", "ruby@3.2.2"],
        "runtime_used": ["node@18.17.0"],
        "runtime_resolution_note": summary["runtime_resolution_note"],
    }
    assert "ruby" in summary["runtime_resolution_note"]


def test_summarise_nothing_declared():
    summary = runtime.summarise([[]])
    assert summary["runtime_lanes_declared"] == []
    assert summary["runtime_lanes_unsatisfied"] == []
    assert "no runtime version" in summary["runtime_resolution_note"]


def test_summarise_everything_satisfied():
    per_project = [
        [
            {
                "lane": "go",
                "requested": "1.21",
                "requested_source": "go.mod go",
                "used": "1.22.0",
                "satisfied": True,
                "note": "the tree asks for go 1.21 and this host has it",
            }
        ]
    ]
    summary = runtime.summarise(per_project)
    assert summary["runtime_lanes_unsatisfied"] == []
    assert summary["runtime_resolution_note"] == (
        "this host can supply every runtime lane the tree names"
    )


# --- which -----------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes an extension-less executable is found by name, which POSIX PATH lookup "
    "does but Windows `which` needs a .exe/PATHEXT match for",
)
def test_which_uses_the_supplied_environments_path(tmp_path):
    _shim(tmp_path / "toolctl", "toolctl 1.0")
    found = runtime.which("toolctl", {"PATH": str(tmp_path)})
    assert found == str(tmp_path / "toolctl")


def test_which_misses_when_the_name_is_not_on_that_path(tmp_path):
    assert runtime.which("no-such-executable-anywhere", {"PATH": str(tmp_path)}) is None
