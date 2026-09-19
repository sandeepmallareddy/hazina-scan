"""Unit tests for `hazina_scan.build.ecosystems`: one plan per discovered project.

Every fixture is a bare directory of manifest files under `tmp_path`; a `discover.Project` is
built by hand rather than run through the full discovery walk, so each test controls exactly
which ecosystem, lockfile and framework combination it is checking. The Python planner is the
one module that does real work at plan time -- it creates a virtualenv -- so its tests are the
slower ones here; everything else is pure string and path assembly and needs no toolchain at
all beyond Python and git, per this suite's own rule.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hazina_scan import env as env_mod
from hazina_scan.build import discover, runtime
from hazina_scan.build.ecosystems import dotnet, go, jvm, node, php, plan_for, python, ruby, rust


def _project(root: Path, ecosystem: str, **overrides) -> discover.Project:
    fields = {
        "project_id": ecosystem,
        "root": root,
        "rel": ".",
        "ecosystem": ecosystem,
        "members": [],
        "workspace": False,
        "required": True,
        "weight": 1,
    }
    fields.update(overrides)
    return discover.Project(**fields)


def _build_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return env_mod.build_env(domain="build", home=home)


# --- node ------------------------------------------------------------------------------


def test_node_jest_with_pnpm_lock(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n")
    (root / "package.json").write_text(
        json.dumps({"devDependencies": {"jest": "^29.0.0"}, "scripts": {"test": "jest"}})
    )
    project = _project(root, "node")
    scratch = tmp_path / "scratch"
    result = node.plan(project, scratch, {}, 900, [])

    assert result["toolchain"] == "node-pnpm"
    assert result["tool"] == "pnpm"
    assert result["locked"] == ["pnpm", "install", "--frozen-lockfile"]
    assert result["relaxed"] == ["pnpm", "install", "--no-frozen-lockfile"]
    assert result["build"] is None
    assert result["discover"] == (["npx", "--no-install", "jest", "--listTests"], "jest_files")
    cov_dir = scratch / "cov"
    assert result["test"] == (
        [
            "npx",
            "--no-install",
            "jest",
            "--ci",
            "--runInBand",
            "--passWithNoTests",
            "--coverage",
            "--coverageReporters=json-summary",
            f"--coverageDirectory={cov_dir}",
        ],
        "jest",
    )
    assert result["coverage"] == ("istanbul", cov_dir / "coverage-summary.json")


def test_node_vitest_with_yarn_classic_lock(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "yarn.lock").write_text("# yarn lockfile v1\n")
    (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^1.0.0"}}))
    project = _project(root, "node")
    scratch = tmp_path / "scratch"
    result = node.plan(project, scratch, {}, 900, [])

    assert result["toolchain"] == "node-yarn"
    assert result["locked"] == ["yarn", "install", "--frozen-lockfile", "--non-interactive"]
    assert result["relaxed"] == ["yarn", "install", "--non-interactive"]
    cov_dir = scratch / "cov"
    assert result["discover"] == (["npx", "--no-install", "vitest", "list"], "jest_files")
    assert result["coverage"] == ("istanbul", cov_dir / "coverage-summary.json")


def test_node_vitest_with_yarn_berry_lock(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "yarn.lock").write_text("__metadata:\n  version: 6\n")
    (root / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^1.0.0"}}))
    project = _project(root, "node")
    result = node.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["locked"] == ["yarn", "install", "--immutable"]
    assert result["relaxed"] == ["yarn", "install"]


def test_node_mocha_with_no_lockfile(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"devDependencies": {"mocha": "^10.0.0"}, "scripts": {"build": "tsc"}})
    )
    project = _project(root, "node")
    result = node.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["toolchain"] == "node-npm"
    assert result["locked"] == ["npm", "install", "--no-audit", "--no-fund"]
    assert result["relaxed"] is None
    assert result["build"] == ["npm", "run", "build"]
    assert result["discover"] == (
        ["npx", "--no-install", "mocha", "--dry-run", "--reporter", "min"],
        "mocha",
    )
    assert result["test"] == (["npm", "test", "--silent"], "mocha")
    assert result["coverage"] is None


def test_node_unknown_framework_with_real_test_script(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "package-lock.json").write_text("{}")
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "node run-tests.js"}}))
    project = _project(root, "node")
    result = node.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["toolchain"] == "node-npm"
    assert result["locked"] == ["npm", "ci", "--no-audit", "--no-fund"]
    assert result["discover"] is None
    assert result["test"] == (["npm", "test", "--silent"], None)


def test_node_placeholder_test_script_plans_no_test(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
    )
    project = _project(root, "node")
    result = node.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["discover"] is None
    assert result["test"] is None
    assert result["coverage"] is None


# --- python ------------------------------------------------------------------------------


def _python_project(tmp_path: Path) -> tuple[discover.Project, Path, dict]:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    project = _project(root, "python")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = _build_env(tmp_path)
    return project, scratch, env


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes the venv's POSIX bin/python layout, not Windows' Scripts\\python.exe",
)
def test_python_pyproject_with_dev_extras(tmp_path):
    project, scratch, env = _python_project(tmp_path)
    (project.root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n"
        "[project.optional-dependencies]\n"
        "dev = ['pytest', 'pytest-cov']\n"
    )
    result = python.plan(project, scratch, env, 300, [], base_python=sys.executable)

    py = str(scratch / "venv" / "bin" / "python")
    assert result["toolchain"] == "python-venv"
    assert result["tool"] == py
    assert result["locked"] == [py, "-m", "pip", "install", "."]
    assert result["relaxed"] == [py, "-m", "pip", "install", "--no-build-isolation", "."]
    assert result["build"] is None
    assert result["harness"][0] == [py, "-m", "pip", "install", "-q", "pytest", "pytest-cov"]
    assert result["harness"][1] == [py, "-m", "pip", "install", "-q", ".[dev]"]
    assert result["discover"] == (
        [py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        "pytest",
    )
    cov_json = scratch / "coverage.json"
    assert result["test"] == (
        [
            py,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--cov=.",
            f"--cov-report=json:{cov_json}",
            "--cov-report=",
        ],
        "pytest",
    )
    assert result["coverage"] == ("coveragepy", cov_json)
    assert result["test_fallback"] == (
        [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        "pytest",
    )


def test_python_requirements_txt(tmp_path):
    project, scratch, env = _python_project(tmp_path)
    (project.root / "requirements.txt").write_text("requests\n")
    result = python.plan(project, scratch, env, 300, [], base_python=sys.executable)

    py = result["tool"]
    assert result["locked"] == [py, "-m", "pip", "install", "-r", "requirements.txt"]
    assert result["relaxed"] is None


def test_python_setup_cfg(tmp_path):
    project, scratch, env = _python_project(tmp_path)
    (project.root / "setup.cfg").write_text("[metadata]\nname = demo\n")
    result = python.plan(project, scratch, env, 300, [], base_python=sys.executable)

    py = result["tool"]
    assert result["locked"] == [py, "-m", "pip", "install", "."]
    assert result["relaxed"] == [py, "-m", "pip", "install", "--no-build-isolation", "."]


def test_python_tox_only_has_no_installable_manifest(tmp_path):
    project, scratch, env = _python_project(tmp_path)
    (project.root / "tox.ini").write_text("[tox]\nenvlist = py311\n")
    result = python.plan(project, scratch, env, 300, [], base_python=sys.executable)

    assert result == {
        "toolchain": "python-venv",
        "tool": None,
        "preflight": "probe: no installable manifest present",
    }


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes the venv's POSIX bin/python layout, not Windows' Scripts\\python.exe",
)
def test_python_falls_back_to_sys_executable_without_base_python(tmp_path):
    project, scratch, env = _python_project(tmp_path)
    (project.root / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    result = python.plan(project, scratch, env, 300, [])

    assert result["tool"] == str(scratch / "venv" / "bin" / "python")


# --- go --------------------------------------------------------------------------------


def test_go_plan(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.21\n")
    project = _project(root, "go")
    scratch = tmp_path / "scratch"
    result = go.plan(project, scratch, {}, 900, [])

    profile = scratch / "cover.out"
    assert result["toolchain"] == "go-modules"
    assert result["locked"] == ["go", "mod", "download"]
    assert result["build"] == ["go", "build", "./..."]
    assert result["discover"] == (["go", "test", "-list", ".*", "./..."], "go_list")
    assert result["test"] == (
        ["go", "test", "./...", "-count=1", "-json", "-covermode=set", f"-coverprofile={profile}"],
        "go",
    )
    assert result["coverage"] == ("go", profile)


# --- rust ------------------------------------------------------------------------------


def test_rust_with_lockfile(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\nname = 'demo'\nversion = '0.1.0'\n")
    (root / "Cargo.lock").write_text("# generated\n")
    project = _project(root, "rust")
    result = rust.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["locked"] == ["cargo", "fetch", "--locked"]
    assert result["relaxed"] == ["cargo", "fetch"]
    assert result["build"] == ["cargo", "build", "--locked"]
    assert result["build_relaxed"] == ["cargo", "build"]
    assert result["discover"] == (["cargo", "test", "--", "--list"], "cargo_list")
    assert result["test"] == (["cargo", "test", "--no-fail-fast"], "cargo")
    assert result["coverage"] is None


def test_rust_without_lockfile(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\nname = 'demo'\nversion = '0.1.0'\n")
    project = _project(root, "rust")
    result = rust.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["locked"] == ["cargo", "fetch"]
    assert result["relaxed"] is None
    assert result["build"] == ["cargo", "build"]
    assert result["build_relaxed"] is None


# --- maven / gradle ----------------------------------------------------------------------


def test_maven_without_wrapper(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project></project>")
    project = _project(root, "maven")
    scratch = tmp_path / "scratch"
    restore = []
    result = jvm.plan(project, scratch, {}, 900, restore)

    base = ["mvn", "-B", "-ntp", f"-Dmaven.repo.local={scratch / 'm2'}"]
    assert result["toolchain"] == "maven"
    assert result["tool"] == "mvn"
    assert result["locked"] == base + ["dependency:go-offline"]
    assert result["relaxed"] == base + ["-U", "dependency:go-offline"]
    assert result["build"] == base + ["-DskipTests", "test-compile"]
    assert result["discover"] is None
    assert result["test"] == (base + ["-Dmaven.test.failure.ignore=true", "test"], None)
    assert result["test_junit"] == (
        "**/target/surefire-reports/*.xml",
        "**/target/failsafe-reports/*.xml",
    )
    assert restore == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes a POSIX executable bit on the wrapper script, which Windows has no "
    "equivalent of, so the wrapper falls back to the bare `mvn` command instead",
)
def test_maven_with_wrapper_restores_executable_bit(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project></project>")
    wrapper = root / "mvnw"
    wrapper.write_text('#!/bin/sh\nexec mvn "$@"\n')
    wrapper.chmod(0o644)
    original_mode = wrapper.stat().st_mode
    project = _project(root, "maven")
    scratch = tmp_path / "scratch"
    restore = []
    result = jvm.plan(project, scratch, {}, 900, restore)

    assert result["tool"] == str(wrapper)
    assert result["locked"][0] == str(wrapper)
    assert os.access(wrapper, os.X_OK)
    assert restore == [(wrapper, original_mode)]


def test_maven_wrapper_already_executable_is_left_alone(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project></project>")
    wrapper = root / "mvnw"
    wrapper.write_text('#!/bin/sh\nexec mvn "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    project = _project(root, "maven")
    restore = []
    jvm.plan(project, tmp_path / "scratch", {}, 900, restore)

    assert restore == []


def test_gradle_without_wrapper(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "build.gradle").write_text("plugins { id 'java' }\n")
    project = _project(root, "gradle")
    scratch = tmp_path / "scratch"
    result = jvm.plan(project, scratch, {}, 900, [])

    base = ["gradle", "--no-daemon", "--console=plain", "-g", str(scratch / "gradle")]
    assert result["toolchain"] == "gradle"
    assert result["tool"] == "gradle"
    assert result["locked"] == base + ["dependencies"]
    assert result["relaxed"] is None
    assert result["build"] == base + ["testClasses"]
    assert result["discover"] == (base + ["test", "--dry-run"], "gradle_dry")
    assert result["test"] == (base + ["--continue", "test"], None)
    assert result["test_junit"] == ("**/build/test-results/**/*.xml",)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes a POSIX executable bit on the wrapper script, which Windows has no "
    "equivalent of, so the wrapper falls back to the bare `gradle` command instead",
)
def test_gradle_with_wrapper_restores_executable_bit(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "build.gradle").write_text("plugins { id 'java' }\n")
    wrapper = root / "gradlew"
    wrapper.write_text('#!/bin/sh\nexec gradle "$@"\n')
    wrapper.chmod(0o644)
    original_mode = wrapper.stat().st_mode
    project = _project(root, "gradle")
    restore = []
    result = jvm.plan(project, tmp_path / "scratch", {}, 900, restore)

    assert result["tool"] == str(wrapper)
    assert os.access(wrapper, os.X_OK)
    assert restore == [(wrapper, original_mode)]


# --- dotnet ----------------------------------------------------------------------------


@pytest.mark.parametrize("manifest", ["demo.csproj", "demo.sln"])
def test_dotnet_plan(tmp_path, manifest):
    root = tmp_path / "proj"
    root.mkdir()
    (root / manifest).write_text("<Project></Project>")
    project = _project(root, "dotnet")
    result = dotnet.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["toolchain"] == "dotnet"
    assert result["locked"] == ["dotnet", "restore", "--locked-mode"]
    assert result["relaxed"] == ["dotnet", "restore"]
    assert result["build"] == ["dotnet", "build", "--no-restore"]
    assert result["discover"] == (["dotnet", "test", "--no-build", "--list-tests"], "dotnet_list")
    assert result["test"] == (["dotnet", "test", "--no-build", "--nologo"], "dotnet")
    assert result["coverage"] is None


# --- ruby ------------------------------------------------------------------------------


def test_ruby_plan(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Gemfile").write_text('source "https://rubygems.org"\n')
    project = _project(root, "ruby")
    result = ruby.plan(project, tmp_path / "scratch", {}, 900, [])

    assert result["toolchain"] == "bundler"
    assert result["locked"] == ["bundle", "install", "--deployment"]
    assert result["relaxed"] == ["bundle", "install"]
    assert result["build"] is None
    assert result["discover"] == (
        ["bundle", "exec", "rspec", "--dry-run", "--no-color"],
        "rspec_dry",
    )
    assert result["test"] == (["bundle", "exec", "rspec", "--no-color"], "rspec")
    assert result["coverage"] is None


# --- php -------------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes the POSIX vendor/bin/phpunit path form, not Windows' vendor\\bin\\phpunit.bat",
)
def test_php_plan(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "composer.json").write_text(json.dumps({"require-dev": {"phpunit/phpunit": "^10"}}))
    project = _project(root, "php")
    scratch = tmp_path / "scratch"
    result = php.plan(project, scratch, {}, 900, [])

    phpunit = str(scratch / "vendor" / "bin" / "phpunit")
    assert result["toolchain"] == "composer"
    assert result["locked"] == ["composer", "install", "--no-interaction", "--no-progress"]
    assert result["relaxed"] == [
        "composer",
        "install",
        "--no-interaction",
        "--no-progress",
        "--ignore-platform-reqs",
    ]
    assert result["build"] is None
    assert result["discover"] == ([phpunit, "--list-tests"], "phpunit_list")
    assert result["test"] == ([phpunit, "--do-not-cache-result"], "phpunit")
    assert result["coverage"] is None


# --- plan_for ----------------------------------------------------------------------------


def test_plan_for_dispatches_by_ecosystem_and_applies_overlay(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.21\n")
    project = _project(root, "go")
    scratch = tmp_path / "scratch"
    fake_bin = tmp_path / "fake-go-bin"
    fake_bin.mkdir()
    rt_plan = runtime.Plan(overlay={"PATH": str(fake_bin)})
    env = {"PATH": "/usr/bin:/bin"}

    result = plan_for(project, scratch, env, 900, [], rt_plan)

    assert result["toolchain"] == "go-modules"
    assert result["locked"] == ["go", "mod", "download"]
    assert result["env"]["PATH"] == f"{fake_bin}{os.pathsep}/usr/bin:/bin"
    assert result["runtime"] is rt_plan
    # the caller's own env is left untouched
    assert env["PATH"] == "/usr/bin:/bin"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="assumes the venv's POSIX bin/python layout, not Windows' Scripts\\python.exe",
)
def test_plan_for_python_passes_resolved_interpreter(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    project = _project(root, "python")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = _build_env(tmp_path)
    rt_plan = runtime.Plan(interpreter={"python": sys.executable})

    result = plan_for(project, scratch, env, 300, [], rt_plan)

    assert result["tool"] == str(scratch / "venv" / "bin" / "python")


def test_plan_for_unknown_ecosystem_has_no_tool(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    project = _project(root, "elixir")
    rt_plan = runtime.Plan()

    result = plan_for(project, tmp_path / "scratch", {}, 900, [], rt_plan)

    assert result["toolchain"] == "elixir"
    assert result["tool"] is None
    assert result["preflight"] == "command not found: elixir"
