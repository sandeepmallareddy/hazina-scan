"""Execute a repository's own install, build, discovery, test and coverage steps and summarise them.

Most of hazina-scan only reads a checkout and infers from what is there. This module is the
exception: it launches the subprocesses a repository already defines for itself, all inside one
shared time budget, and condenses whatever they printed and returned into a compact record of
flags and two integer indices.

Three constraints hold throughout the module.

1. Budget accounting is centralised. A single `Budget` object tracks the whole run. Every
   project draws a share proportional to its own size out of what remains, and every command a
   project runs is capped by the smallest of three numbers: the fixed per-command ceiling, what
   is left of the project's share, and what is left of the run overall. When the clock runs out
   before a project is even reached, that project is not marked failed -- every one of its
   phases is recorded `skipped_budget` and every measurement stays unset.

2. A missing measurement is not a negative result. `install_ok` and `build_ok` can be `True`,
   `False`, or `None`; `False` is reserved for the repository's own fault, and `None` means the
   answer was never obtained because of something about this machine, its network, or the time
   available. `observed_runnability` is built the same way: either it sums four things that were
   actually executed, or it is left empty along with a sentence naming whose limitation caused
   that. Treating a term nobody measured as a zero would make an unprobed repository look the
   same as one that tried and failed everything.

3. The working tree comes back exactly as it was handed over. Before the first command runs,
   this module records the bytes of any file its installers are known to modify and notes which
   build outputs are already on disk. After the run, whatever output the run itself produced is
   deleted, whatever was already there is left untouched, and the recorded file bytes are
   restored. Leaving the checkout altered defeats the purpose of measuring it.

`collect()` is what callers invoke. `skipped_budget()` builds the placeholder record it returns
when there was no time left to even begin.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .. import env as env_mod
from ..redact import scrub
from . import parsers, runtime
from .discover import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_PHASE_TIMEOUT,
    MAX_COMMANDS,
    MAX_PROBED_PROJECTS,
    MIN_PHASE_SECONDS,
    Budget,
    Project,
    _allocate,
    discover_projects,
)
from .ecosystems import ECOSYSTEM_LANE, plan_for

__all__ = [
    "BUILD_LEVELS",
    "DEFAULT_LEVEL",
    "MIN_PHASE_SECONDS",
    "MAX_PROBED_PROJECTS",
    "PHASES",
    "STATUSES",
    "skipped_budget",
    "collect",
]

_IS_WIN = os.name == "nt"

#: What the probe may do. `discover` is the default because it turns four inferred
#: measurements into executed ones without paying for the suite, which is the expensive half.
BUILD_LEVELS = ("none", "discover", "full")
DEFAULT_LEVEL = "discover"

#: The phases, in order. They stay separate because collapsing them is what makes "this project
#: has no tests" indistinguishable from "this project's tests could not be started".
PHASES = ("resolve", "build", "discover", "test", "coverage")

#: The full vocabulary a phase's `status` field can hold. `no_tests` records a runner that
#: looked and found none, which is a fact about the repository, not a malfunction. `unavailable`
#: says the tool itself could not be reached on this host. `blocked` marks a phase that was never
#: attempted because an earlier phase in the same project already failed. The two `skipped_*`
#: values are their own category because a probe that chose not to run a phase is a different
#: event from one that tried and could not proceed.
STATUSES = (
    "passed",
    "failed",
    "no_tests",
    "unavailable",
    "timed_out",
    "blocked",
    "skipped_budget",
    "skipped_level",
)
_SKIPPED_STATUSES = ("skipped_budget", "skipped_level")

#: Who a phase's outcome belongs to.
ATTRIBUTIONS = ("repository", "runner", "external_service", "credentials", "unknown")

#: The coarse verdict vocabulary and the reported remediation scale.
FAILURE_CLASSES = ("NONE", "REPO_INTRINSIC", "ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED")
EFFORTS = ("none", "trivial", "moderate", "substantial", "infeasible", "unknown")

#: Listing style -> the parser that reads a runner's execution summary.
_PARSERS = {
    "pytest": parsers.parse_pytest,
    "jest": parsers.parse_jest,
    "vitest": parsers.parse_vitest,
    "mocha": parsers.parse_mocha,
    "cargo": parsers.parse_cargo,
    "go": parsers.parse_go_json,
    "dotnet": parsers.parse_dotnet,
    "phpunit": parsers.parse_phpunit,
    "rspec": parsers.parse_rspec,
}

#: What a runner's own listing actually enumerates. Jest and Vitest name FILES and Gradle names
#: TASKS, so counting either as tests would overstate the suite; the executed run fills the real
#: test count for those ecosystems instead.
_LISTING_UNIT = {
    "jest_files": "test_files",
    "gradle_dry": "test_tasks",
    "cargo_list": "tests",
    "go_list": "tests",
    "dotnet_list": "tests",
    "phpunit_list": "tests",
    "rspec_dry": "tests",
}


# --- reading a failure ----------------------------------------------------------------------
#
# Two vocabularies read the same log. `_classify` produces the coarse five-value verdict and is
# the sole authority on it; `_error_class` produces the finer name. The environment patterns are
# consulted first on purpose: several messages below could honestly belong to either side, and
# handing an ambiguous one to this runner rather than to the repository is the standing policy
# of the whole module.

_ENV = re.compile(
    # the tool or the runtime is simply not here
    r"command not found|executable file not found|is not recognized as an internal or external"
    r"|no such file or directory:\s*(npm|node|python3?|go|mvn|cargo|bundle"
    r"|composer|pnpm|yarn|dotnet)"
    r"|No module named"
    # the runtime on this host is not the one the tree asked for
    r"|unsupported engine|EBADENGINE|engine \"node\""
    r"|requires (node|python|ruby|\.NET|dotnet)|wrong ruby version"
    r"|requires a different Python|requires Python\s*[<>=]"
    r"|could not find (java|javac|jdk)|JAVA_HOME is not set"
    r"|invalid source release|class file version|Unsupported class file"
    # this host's own interpreter cannot be written into, or the process is running as the
    # wrong user for it. `externally.managed` is intentionally not anchored to a line start:
    # pip phrases this several different ways depending on the failure path, and a wrapper
    # around pip may only forward a fragment of the original message.
    r"|externally.managed"
    r"|break-system-packages|ensurepip is not available|python3?-venv"
    r"|permission denied.*gem|EACCES|EPERM|EROFS|operation not permitted"
    r"|(should not|must not|cannot|refus\w+ to) be run as root|running pip as the .root. user"
    # a system library, header or compiler this host does not have
    r"|fatal error: .*\.h: No such file|cannot find -l|ld: library not found"
    r"|library not found for|linker command failed|Microsoft Visual C\+\+ \d|cl\.exe"
    r"|command '(gcc|cc|clang|g\+\+|cmake)' failed|unable to execute '(gcc|cc|clang)'"
    r"|Failed building wheel for|metadata-generation-failed|subprocess-exited-with-error"
    r"|pkg-config.*not found|No package '[^']+' found"
    # no usable network, or no route to the index
    r"|Temporary failure in name resolution|Could not resolve host|getaddrinfo"
    r"|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|network is unreachable"
    r"|connection refused|Read timed out|Connection timed out|proxy|CERTIFICATE_VERIFY_FAILED"
    r"|SSLError|TLS handshake|offline mode|no internet"
    # this machine is not a platform the project targets
    r"|EBADPLATFORM|unsupported platform|not supported on this platform|Unsupported architecture"
    r"|incompatible architecture|wrong architecture|only supported on"
    r"|requires (macOS|Windows|Linux)"
    # a wrapper script the checkout is missing
    r"|gradle-wrapper\.jar|gradle wrapper.*not found"
    r"|corepack.*(prompt|enable)|Cannot find matching keyid",
    re.I,
)

# Failures the repository owns. Credential failures are deliberately absent: they are settled
# below, by asking which side wanted the private index in the first place.
_REPO = re.compile(
    r"E404|404 Not Found|ERESOLVE|unable to resolve dependency tree|peer dep"
    r"|version solving failed|no matching distribution|could not find a version"
    r"|could not resolve dependencies|artifact.*(not found|resolution)"
    # A lockfile that no longer agrees with its manifest is the tree's own inconsistency, and
    # the frozen install is attempted first precisely so that this surfaces instead of being
    # quietly papered over by a resolver.
    r"|lock(file)? (is )?(out of date|outdated|mismatch)|integrity check failed"
    r"|(package-lock\.json|lock ?file).{0,40}(out of sync|in sync)|Missing: .+ from lock file"
    r"|compilation (error|failed)|cannot find symbol|syntax error|parse error"
    r"|submodule.*(not initialized|failed|missing)",
    re.I,
)

# A package index refused the credentials it was offered. This is the one signature that truly
# belongs to either side: a tree that declares a dependency in a private index cannot be built
# by an outsider, while an operator whose own package manager holds a stale internal token has a
# host problem that says nothing about the repository.
_AUTH = re.compile(
    r"E401|E403|401 Unauthorized|403 Forbidden|authentication required"
    r"|Unable to authenticate|authentication token|npm login|not authori[sz]ed"
    r"|Not authorized to|Access denied|401 \(Unauthorized\)|403 \(Forbidden\)",
    re.I,
)

# The finer failure vocabulary, ordered, first match wins, with the host-flavoured specifics
# ahead of the generic repository ones. Nothing is renamed once it is here: the names are what
# every comparison across runs is keyed on.
_ERROR_SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"No space left on device|ENOSPC", re.I), "out_of_disk"),
    (re.compile(r"Out of memory|OutOfMemoryError|JavaScript heap out of memory"), "out_of_memory"),
    (re.compile(r"ERR_OSSL_EVP_UNSUPPORTED|error:0308010C"), "wrong_runtime"),
    (
        re.compile(
            r"Unsupported class file major version|invalid (source|target) release"
            r"|class file has wrong version|Source option \d+ is no longer supported"
        ),
        "wrong_runtime",
    ),
    (
        re.compile(
            r"Requires-Python|requires a different Python|python_requires"
            r"|This package requires Python|is not supported on Python"
        ),
        "wrong_runtime",
    ),
    (
        re.compile(r"engine \"?node\"? is incompatible|Unsupported engine|EBADENGINE"),
        "wrong_runtime",
    ),
    (re.compile(r"go\.mod requires go >= |requires go1\.\d+ or later"), "wrong_runtime"),
    (
        re.compile(
            r"Your Ruby version is [\d.]+, but your Gemfile specified"
            r"|Your PHP version \([\d.]+\) does not satisfy"
        ),
        "wrong_runtime",
    ),
    (
        re.compile(r"MSB3644|NETSDK1045|The reference assemblies for .* were not found"),
        "wrong_runtime",
    ),
    (
        re.compile(r"externally.managed|break-system-packages|ensurepip is not available"),
        "wrong_runtime",
    ),
    # A package manager NEWER than a pinned dependency's metadata allows. The resolution failure
    # that follows reads exactly like a tree pinning something that no longer exists, which
    # would charge the repository for the age of this host's pip.
    (
        re.compile(
            r"has invalid metadata: Expected matching|Please use pip<[\d.]+ if you need"
            r"|since it has invalid metadata"
        ),
        "wrong_runtime",
    ),
    # A standard-library member the interpreter this host chose has since removed, surfacing
    # from a pinned tool rather than from the tree's own code.
    (
        re.compile(
            r"module '(?:ast|configparser|collections|imp|inspect|asyncio|cgi|locale|"
            r"distutils)' has no attribute"
        ),
        "wrong_runtime",
    ),
    # Build machinery newer than a pinned source distribution knows how to build against.
    (
        re.compile(
            r"'build_ext' object has no attribute 'cython_sources'"
            r"|use_2to3 is invalid|No module named 'distutils'"
        ),
        "wrong_runtime",
    ),
    # This host's bundler is not the one the lockfile was written with. The bare
    # `Gem::GemNotFoundException` is deliberately not matched on its own: that is what an
    # ordinary missing gem raises, and treating it as a runtime mismatch would hide a plain
    # dependency failure behind a statement about our Ruby installation.
    (
        re.compile(
            r"Could not find 'bundler' \([\d.]+\) required by your"
            r"|Gem::GemNotFoundException.{0,80}bundler|bundle update --bundler"
        ),
        "wrong_runtime",
    ),
    (re.compile(r"world-writable|Don't run Bundler as root"), "container_misconfig"),
    (
        re.compile(r"Corepack is about to download|COREPACK_ENABLE_DOWNLOAD_PROMPT|YN0050"),
        "container_misconfig",
    ),
    (
        re.compile(
            r"fatal error: .*\.h: No such file|cannot find -l[a-zA-Z0-9_]+"
            r"|Could NOT find [A-Za-z0-9_]+ \(missing|pg_config executable is not found"
            r"|mysql_config: not found|libpq-fe\.h"
        ),
        "missing_system_lib",
    ),
    (
        re.compile(
            r"Exit handler never called|npm error code ENOTEMPTY|Maximum call stack size exceeded"
        ),
        "package_manager_bug",
    ),
    (
        re.compile(
            r"(Cannot find|No usable|Failed to launch|Could not find) (Chrome|Chromium|browser)"
            r"|CHROME_BIN|no DISPLAY|cannot open display"
        ),
        "browser_missing",
    ),
    (
        re.compile(
            r"Is the docker daemon running|Cannot connect to the Docker daemon"
            r"|docker: not found|docker-compose: not found"
        ),
        "docker_missing",
    ),
    (
        re.compile(
            r"(Connection refused|ECONNREFUSED|could not connect to server)"
            r".{0,80}(5432|3306|6379|27017|9200|localhost|127\.0\.0\.1)",
            re.S,
        ),
        "external_service_missing",
    ),
    (
        re.compile(
            r"Temporary failure in name resolution|Could not resolve host|getaddrinfo"
            r"|EAI_AGAIN|Network is unreachable|Could not transfer artifact"
        ),
        "no_network",
    ),
    # Checked before `toolchain_missing` deliberately: node-gyp's own compiler-detection probes
    # print a bare `: not found` line as routine output, and matching that generic pattern first
    # would misclassify an ordinary failed native rebuild as a missing toolchain.
    (re.compile(r"gyp ERR!|node-gyp|prebuild-install"), "native_build_failed"),
    (
        re.compile(
            r"command not found|executable file not found|: not found\b"
            r"|could not determine executable to run|npm ERR! could not determine"
            # A runner refusing to fetch a package the project never installed is this
            # environment lacking a tool, not the project lacking a suite.
            r"|npx canceled due to missing packages"
        ),
        "toolchain_missing",
    ),
    # A runner that STARTED and then could not import what the install did not provide. Three
    # facts that used to collapse into one: no suite at all, a suite that ran and failed, and a
    # suite that exists but could not be loaded.
    (
        re.compile(
            r"ImportError while importing test module"
            r"|Interrupted: \d+ errors? during collection"
            r"|code: 'MODULE_NOT_FOUND'|Cannot find module '"
        ),
        "test_dependency_missing",
    ),
    # The build backend named in the manifest cannot be imported on this host -- the expected
    # outcome when an install skips build isolation and the backend was never installed here.
    (
        re.compile(r"BackendUnavailable|Cannot import '[A-Za-z0-9_.]+\.(?:api|build_meta)'"),
        "build_backend_missing",
    ),
    (
        re.compile(
            r"E401|E403|401 Unauthorized|403 Forbidden|ENEEDAUTH|authentication required"
            r"|Incorrect or missing password|Permission denied \(publickey\)"
            r"|could not read Username|Authentication failed for"
        ),
        "private_registry",
    ),
    (re.compile(r"go: unrecognized import path|is not in GOROOT"), "private_registry"),
    (
        re.compile(
            r"MSB4025|The project file could not be loaded"
            r"|Could not find file .*\.(csproj|sln|fsproj)"
        ),
        "broken_project_file",
    ),
    (re.compile(r"probe: no installable manifest"), "no_installable_manifest"),
    (
        re.compile(r"GradleWrapperMain|gradle-wrapper\.jar.*(No such file|not found)"),
        "missing_wrapper_jar",
    ),
    (
        re.compile(
            r"No url found for submodule path|failed to clone .*submodule|Submodule .* could not"
        ),
        "missing_submodule",
    ),
    (
        re.compile(
            r"npm ci can only install packages when your package\.json and package-lock\.json"
            r"|Missing: .* from lock file|Invalid: lock file|EUSAGE"
        ),
        "broken_lockfile",
    ),
    (
        re.compile(
            r"(lockfile|Gemfile\.lock|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)"
            r".{0,80}(out of date|needs to be updated|is not up to date|does not match|frozen)",
            re.I | re.S,
        ),
        "broken_lockfile",
    ),
    (
        re.compile(
            r"Your lock file does not satisfy|The lockfile is not up to date"
            r"|the lock file .* needs to be updated"
        ),
        "broken_lockfile",
    ),
    (
        re.compile(
            r"No matching distribution found|Could not find a version that satisfies"
            r"|ResolutionImpossible|version solving failed|SolverProblemError"
        ),
        "dependency_resolution",
    ),
    (
        re.compile(
            r"ERESOLVE|Conflicting peer dependency|Couldn't find any versions for"
            r"|error Couldn't find package"
        ),
        "dependency_resolution",
    ),
    (
        re.compile(r"404 Not Found.{0,120}(registry\.npmjs\.org|registry\.yarnpkg\.com)", re.S),
        "dependency_resolution",
    ),
    (
        re.compile(
            r"Could not resolve dependencies for project|Could not find artifact"
            r"|Failed to collect dependencies|Non-resolvable (parent POM|import POM)"
            r"|Could not resolve all (files|dependencies|artifacts) for configuration"
        ),
        "dependency_resolution",
    ),
    (
        re.compile(
            r"Bundler could not find compatible versions|Could not find gem "
            r"|Your requirements could not be resolved|Root composer\.json requires"
            r"|failed to select a version|no matching package named"
        ),
        "dependency_resolution",
    ),
    (
        re.compile(
            r"go: .*: (unknown revision|invalid version|no required module provides)"
            r"|NU1101|NU1102|NU1103|Unable to find package"
        ),
        "dependency_resolution",
    ),
    (
        re.compile(r"Could not open requirements file|is not a valid editable requirement"),
        "broken_manifest",
    ),
    (
        re.compile(
            r"COMPILATION ERROR|cannot find symbol|package .* does not exist"
            r"|incompatible types:"
        ),
        "compile_error",
    ),
    (re.compile(r"\bSyntaxError\b|\bIndentationError\b|\bTabError\b"), "compile_error"),
    (
        re.compile(r"\.go:\d+:\d+: (undefined|cannot use|syntax error|declared and not used)"),
        "compile_error",
    ),
    (re.compile(r"error\[E\d+\]:|could not compile `"), "compile_error"),
    (re.compile(r"error TS\d+:|error CS\d+:|error BC\d+:|error FS\d+:"), "compile_error"),
    (re.compile(r"PHP Parse error|PHP Fatal error|unresolved reference"), "compile_error"),
    (
        re.compile(
            r"error in .* setup command|Failed building wheel for"
            r"|metadata-generation-failed|error: subprocess-exited-with-error"
        ),
        "build_backend_failed",
    ),
    (
        re.compile(
            r"npm ERR! code ELIFECYCLE|Command failed with exit code"
            r"|handling the (post-autoload-dump|post-install-cmd|pre-install-cmd) event"
        ),
        "script_failed",
    ),
    (
        re.compile(r"FAILURE: Build failed with an exception|BUILD FAILURE|BUILD FAILED"),
        "build_failed",
    ),
]

#: Which side of the fence a finer class sits on. Anything not named in one of these takes its
#: attribution from the coarse verdict, so the two can never contradict each other.
_CREDENTIAL_CLASSES = {"private_registry"}
_EXTERNAL_CLASSES = {
    "external_service_missing",
    "docker_missing",
    "browser_missing",
    "no_network",
}

#: Classes whose owner the class itself settles. Every one of these is a statement about the
#: machine, so the failure is not ambiguous even where the coarse patterns did not recognise it.
_RUNNER_CLASSES = frozenset(
    {
        "wrong_runtime",
        "toolchain_missing",
        "native_build_failed",
        "missing_system_lib",
        "container_misconfig",
        "out_of_disk",
        "out_of_memory",
        "package_manager_bug",
        "test_dependency_missing",
        "build_backend_missing",
        "missing_wrapper_jar",
    }
)

#: Failure classes whose meaning already points at this machine's state, independent of anything
#: the repository's own code could produce. Only membership here is allowed to override an
#: already-decided repository-intrinsic verdict, so a compile error that merely mentions a
#: build tool somewhere in its log still classifies as a compile error, nothing more.
_HOST_STATE_CLASSES = frozenset(
    {"wrong_runtime", "out_of_disk", "out_of_memory", "container_misconfig"}
)


# --- what the tree itself says about where its packages come from --------------------------

#: Package-index configuration that can be CHECKED IN, and the pattern that says it is being
#: used. One of these plus a rejected credential is what makes the failure the repository's.
_REGISTRY_CONFIG = {
    ".npmrc": re.compile(r"registry\s*=|_authToken|_auth\s*=", re.I),
    ".yarnrc": re.compile(r"registry\s+|registry\s*=", re.I),
    ".yarnrc.yml": re.compile(r"npmRegistryServer|npmScopes", re.I),
    "pip.conf": re.compile(r"index-url|extra-index-url", re.I),
    "pip/pip.conf": re.compile(r"index-url|extra-index-url", re.I),
    "poetry.toml": re.compile(r"\[\[?tool\.poetry\.source|url\s*=", re.I),
    "settings.xml": re.compile(r"<repository|<server", re.I),
    "nuget.config": re.compile(r"packageSources|<add\s", re.I),
    "gradle.properties": re.compile(
        r"(repo|registry|artifactory|nexus).*(url|user|password)", re.I
    ),
    ".netrc": re.compile(r"machine\s+\S+", re.I),
}

#: The public indexes. A checked-in config that names only these is configuration, not a
#: private dependency.
_PUBLIC_INDEX = re.compile(
    r"registry\.npmjs\.org|registry\.yarnpkg\.com|pypi\.org|files\.pythonhosted\.org"
    r"|repo\.maven\.apache\.org|repo1\.maven\.org|jcenter\.bintray|api\.nuget\.org"
    r"|rubygems\.org|proxy\.golang\.org|crates\.io",
    re.I,
)


def declares_private_registry(repo: Path) -> bool:
    """Does the TREE point at a package index that is not one of the public ones?

    Read from the checkout and nowhere else. Whatever this host's own package manager is
    configured to talk to is precisely the thing this question exists to rule out, so consulting
    it here would answer the wrong question.
    """
    for name, pattern in _REGISTRY_CONFIG.items():
        path = repo / name
        if not path.is_file():
            continue
        try:
            body = path.read_text(errors="replace")[:20000]
        except OSError:
            continue
        if pattern.search(body):
            hosts = re.findall(r"https?://([^/\s\"']+)", body)
            if not hosts or any(not _PUBLIC_INDEX.search(h) for h in hosts):
                return True
    return False


#: Signatures that say how much work a failure would take to clear, checked in order. Only ever
#: reported, never scored: what a fix costs depends on who is doing it.
_REMEDIATION = [
    # Reached only once the classifier has already decided a credential failure was the
    # repository's, which is what earns the flat wording.
    (
        "infeasible",
        re.compile(
            r"submodule.*(not initialized|failed|missing)"
            r"|E401|E403|401 Unauthorized|403 Forbidden|authentication required"
            r"|Unable to authenticate|authentication token|npm login"
            r"|private (registry|repository)|\.npmrc.*token|credentials",
            re.I,
        ),
        "an artefact only the owning organisation can hand out is required before this builds, "
        "so no change confined to the repository will make it build elsewhere",
    ),
    (
        "substantial",
        re.compile(
            r"E404|404 Not Found|no matching distribution|could not find a version"
            r"|artifact.*not found|end.?of.?life|no longer supported|deprecated runtime",
            re.I,
        ),
        "at least one dependency can no longer be fetched at the version it is pinned to; "
        "clearing it means choosing a replacement and reworking whatever calls into it",
    ),
    (
        "moderate",
        re.compile(
            r"ERESOLVE|unable to resolve dependency tree|peer dep|version solving failed"
            r"|lock(file)? (is )?(out of date|outdated|mismatch)|integrity check failed"
            r"|(package-lock\.json|lock ?file).{0,40}(out of sync|in sync)"
            r"|Missing: .+ from lock file|could not resolve dependencies",
            re.I,
        ),
        "the pinned dependency graph has no solution; rebuilding the lockfile, or loosening a "
        "handful of version constraints, is the likely way through",
    ),
    (
        "moderate",
        re.compile(r"compilation (error|failed)|cannot find symbol|parse error", re.I),
        "the sources as checked in do not compile, which usually means generated code is absent "
        "from the tree or the toolchain here is not the one they were written against",
    ),
    (
        "trivial",
        re.compile(
            r"unsupported engine|EBADENGINE|engine \"node\""
            r"|requires (node|python|ruby)"
            r"|wrong ruby version|JAVA_HOME|missing environment variable|\.env",
            re.I,
        ),
        "what the project expects of its runtime, or of one environment variable, is not what it "
        "found; supplying the variable or the expected version ought to be enough",
    ),
]


# --- putting the checkout back ---------------------------------------------------------------

#: Names of output and dependency directories this module may need to remove after the run. The
#: removal rule is unconditional across all of them: a name here is deleted only if it did not
#: already exist before the probe started, so a repository that legitimately ships something
#: under one of these names keeps it exactly as it was.
_ARTEFACTS = (
    # coverage and test-report output
    "coverage.json",
    ".coverage",
    ".coverage.tmp",
    "coverage.xml",
    "lcov.info",
    "junit.xml",
    "test-results.xml",
    "htmlcov",
    "coverage",
    ".nyc_output",
    # dependency and build directories. `build` and `.eggs` are here because a source install
    # writes them into the tree; both are subject to the same "only if it was ours" rule.
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    ".tox",
    "target",
    "obj",
    "build",
    ".eggs",
    # caches
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    ".gradle",
    ".next",
    ".turbo",
    ".pnpm-store",
)

#: Glob patterns for artefacts whose exact name varies by project; the same ours-only rule
#: applies to whatever they match.
_ARTEFACT_GLOBS = ("*.egg-info",)

#: Filenames an installer is expected to overwrite in place -- most of them version-controlled
#: lockfiles, not new output. Their contents are read before the run and reinstated afterwards,
#: which covers all three possible outcomes: a file that got created, one that got modified, and
#: one the installer left untouched.
_MUTABLE = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Gemfile.lock",
    "composer.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "go.sum",
    "go.mod",
    "packages.lock.json",
    "gradle.lockfile",
    "composer.json",
    "package.json",
)

#: A file larger than this is left alone rather than held in memory to protect it.
_MUTABLE_MAX = 32 * 1024 * 1024


def _clear_readonly_and_retry(func, path, exc_info) -> None:
    """`shutil.rmtree`'s error hook for a tree it could not delete on the first try.

    Go's module cache -- and every other tool that caches downloads the same way -- marks its
    directories read-only precisely so nothing edits a cached copy by accident, and that same
    bit stops this cleanup from removing them once the probe is done. What blocks a POSIX
    unlink or rmdir is the missing write permission on the ENTRY'S PARENT, not on the entry
    itself, so both are restored before the failed call is retried; either one alone leaves
    some layer of a nested read-only tree behind.
    """
    del exc_info
    target = Path(path)
    for candidate in (target, target.parent):
        try:
            candidate.chmod(candidate.stat().st_mode | stat.S_IRWXU)
        except OSError:
            pass
    try:
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """Delete a scratch tree even when a cache inside it made part of itself read-only."""
    shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def artefact_names(root: Path) -> set[str]:
    """Names, from both the fixed list and the glob patterns, that currently exist under `root`."""
    names = {name for name in _ARTEFACTS if (root / name).exists()}
    for pattern in _ARTEFACT_GLOBS:
        names.update(p.name for p in root.glob(pattern))
    return names


def snapshot(roots: list[Path]) -> dict[Path, bytes | None]:
    """The bytes of every installer-writable file before the run. `None` means "was not there".

    Taken across every project root rather than just the top of the tree: a workspace member's
    lockfile is as rewritable as the one beside the repository root, and just as tracked.
    """
    snap: dict[Path, bytes | None] = {}
    for root in roots:
        for name in _MUTABLE:
            path = root / name
            if path in snap:
                continue
            try:
                if not path.exists():
                    snap[path] = None
                elif path.is_file() and path.stat().st_size <= _MUTABLE_MAX:
                    snap[path] = path.read_bytes()
            except OSError:
                continue
    return snap


def restore_snapshot(snap: dict[Path, bytes | None]) -> None:
    """Write the captured bytes back, and delete the files this run brought into being."""
    for path, before in snap.items():
        try:
            if before is None:
                if path.is_file():
                    path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != before:
                path.write_bytes(before)
        except OSError:
            pass


# --- running one command ---------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int) -> tuple[int, str, bool]:
    """Run one argument list and return `(returncode, combined output, timed out)`.

    `cmd[0]` is resolved against the CHILD's `PATH` first. That matters twice: on Windows a
    package manager is a `.cmd` shim that `CreateProcess` will not find from a bare name, and on
    any host it keeps "is this tool available" and "which binary will run" the same question --
    resolving against our own `PATH` could hand a child a binary from under the operator's home
    that the build environment strips precisely so repository-controlled code cannot reach it.

    Everything goes through the build trust domain's runner, which puts the child in a session
    of its own and signals the whole group on a timeout. A build or test command that daemonises
    -- a dev server, a watcher, a database a suite started -- would otherwise outlive the probe.
    """
    argv = list(cmd)
    resolved = runtime.which(argv[0], env)
    if resolved:
        argv[0] = resolved
    try:
        done = env_mod.run(argv, domain=env_mod.BUILD, cwd=cwd, env=env, timeout=timeout)
        return done.returncode, (done.stdout or "") + (done.stderr or ""), False
    except subprocess.TimeoutExpired as expired:
        return 124, (expired.output or "") + (expired.stderr or ""), True
    except OSError as error:
        # A missing executable, a permission failure, and an invalid working directory all raise
        # OSError here, and downstream they should all read as one outcome: the child process
        # never got to run.
        return 127, f"command not found: {cmd[0]} ({type(error).__name__})", False


def _display(cmd: list[str]) -> str:
    """How a command is reported. Absolute host paths collapse to their last component so the
    evidence stays readable; the redaction pass then removes whatever survives that."""
    parts = []
    for arg in cmd:
        parts.append(Path(arg).name if (os.sep in arg and Path(arg).is_absolute()) else arg)
    return " ".join(parts)


def _as_commands(harness) -> list[list[str]]:
    """A plan's `harness` entry as a list of commands, accepting a single bare command too."""
    if not harness:
        return []
    return harness if isinstance(harness[0], list) else [harness]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


# --- classification ---------------------------------------------------------------------------


def classify(log: str, timed_out: bool, repo: Path | None = None) -> str:
    """The coarse verdict for one failed command. The sole authority on `failure_class`."""
    if timed_out:
        return "TIMEOUT"
    if _AUTH.search(log):
        # Whoever asked for the private index owns the refusal. With no index configuration in
        # the tree there is nothing suggesting the project needs one, so the credentials that
        # were rejected were this host's -- and ambiguity goes to the runner, as everywhere.
        return (
            "REPO_INTRINSIC"
            if repo is not None and declares_private_registry(repo)
            else "ENVIRONMENT"
        )
    if _ENV.search(log):
        return "ENVIRONMENT"
    if _REPO.search(log):
        return "REPO_INTRINSIC"
    return "UNCLASSIFIED"


def error_class(log: str, timed_out: bool) -> str:
    """The finer failure name. Separate from `classify` so the two can describe the same failure
    from different angles without ever being able to contradict each other."""
    if timed_out:
        return "timeout"
    if not log.strip():
        return "unknown_no_output"
    for pattern, code in _ERROR_SIGNATURES:
        if pattern.search(log):
            return code
    return "unclassified"


def reconcile(cls: str, ecode: str, repo: Path | None) -> str:
    """Settle the coarse verdict where the finer class already knows whose failure it was.

    The two vocabularies are independent pattern sets and they can disagree -- a named class
    sitting under an unnamed owner is exactly the hole that leaves a failure unattributable.
    Reconciling here rather than merging the pattern sets keeps each vocabulary's own histogram
    stable.

    This only ever moves a verdict TOWARD the runner. A signature that looks repository-flavoured
    but that the repository patterns did not match stays unclassified -- whose attribution is
    already the runner -- because promoting it would cost a repository its build verdict on the
    strength of a pattern we did not think good enough to write down.
    """
    if ecode in _HOST_STATE_CLASSES:
        # The class already says this machine was the problem, so the coarse verdict has to say
        # so too: otherwise a resolution failure caused by the age of our own toolchain lands on
        # the repository.
        return "ENVIRONMENT"
    if cls != "UNCLASSIFIED":
        return cls
    if ecode in _CREDENTIAL_CLASSES:
        # The same question, answered the same way: only the TREE can be the side asking for a
        # private index.
        return (
            "REPO_INTRINSIC"
            if repo is not None and declares_private_registry(repo)
            else "ENVIRONMENT"
        )
    if ecode in _RUNNER_CLASSES or ecode in _EXTERNAL_CLASSES:
        return "ENVIRONMENT"
    return cls


def attribution(failure_class: str, ecode: str) -> str:
    """Who owns this outcome. `unknown` is a last resort and never a resting place.

    A reader who is told "unknown environment" can do nothing with it, so every outcome names a
    side. An unclassified failure resolves to the runner because that is the standing policy for
    ambiguity here, and a clean outcome is the repository's, since a build that works is a
    property of the repository.
    """
    if ecode in _CREDENTIAL_CLASSES:
        return "credentials"
    if ecode in _EXTERNAL_CLASSES:
        return "external_service"
    if failure_class == "REPO_INTRINSIC":
        return "repository"
    if failure_class in ("ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED"):
        return "runner"
    if failure_class == "NONE":
        return "repository"
    return "unknown"


def _confidence(failure_class: str, ecode: str) -> str:
    if failure_class == "UNCLASSIFIED" or ecode in ("unclassified", "unknown_no_output"):
        return "low" if failure_class == "UNCLASSIFIED" else "medium"
    return "high"


def _remediation(cls: str, log: str, toolchain: str | None) -> tuple[str, str]:
    """`(effort, notes)`. Reported and never scored: what a fix costs depends on the operator."""
    if cls == "NONE":
        return "none", "As checked in, the dependencies resolve and the project builds."
    if cls == "ENVIRONMENT":
        return "trivial", (
            f"What failed was this runner's environment, not the repository: the "
            f"{toolchain or 'unknown'} toolchain was missing here or was the wrong version. A host "
            f"carrying the expected runtime should be enough, and no penalty is recorded."
        )
    if cls == "TIMEOUT":
        return "unknown", (
            "Nothing finished inside the time allowed, so whether the build would have "
            "succeeded is simply unknown. Raise the build timeout and run it again to find out."
        )
    for effort, pattern, note in _REMEDIATION:
        if pattern.search(log):
            return effort, note[0].upper() + note[1:] + "."
    return "unknown", (
        "The build failed against no signature this tool recognises, so how much work it would "
        "take cannot be read off the output. Somebody has to look at it."
    )


# --- reading a coverage reporter's own artefact -------------------------------------------


def _read_coverage(
    kind: str, target: Path, env: dict, cwd: Path, timeout: int
) -> tuple[float | None, int | None, int | None, str | None]:
    """`(pct, covered lines, total lines, method)` out of whatever the reporter wrote."""
    if kind == "coveragepy":
        data = _read_json(target)
        totals = data.get("totals") if isinstance(data, dict) else None
        if isinstance(totals, dict) and totals.get("num_statements"):
            return (
                round(float(totals.get("percent_covered") or 0.0), 2),
                int(totals.get("covered_lines") or 0),
                int(totals.get("num_statements") or 0),
                "coverage.py via pytest-cov",
            )
        return None, None, None, None
    if kind == "istanbul":
        data = _read_json(target)
        lines = (data or {}).get("total", {}).get("lines") if isinstance(data, dict) else None
        if isinstance(lines, dict) and lines.get("total"):
            return (
                round(float(lines.get("pct") or 0.0), 2),
                int(lines.get("covered") or 0),
                int(lines.get("total") or 0),
                "istanbul json-summary",
            )
        return None, None, None, None
    if kind == "go":
        if not target.is_file():
            return None, None, None, None
        rc, log, _ = _run(["go", "tool", "cover", f"-func={target}"], cwd, env, min(timeout, 180))
        found = re.search(r"^total:\s+\(statements\)\s+([\d.]+)%", log, re.M)
        if rc == 0 and found:
            return float(found.group(1)), None, None, "go test -coverprofile"
        return None, None, None, None
    return None, None, None, None


# --- the phase runner ------------------------------------------------------------------------


def _phase(
    name: str,
    status: str,
    reason_code: str,
    attributed_to: str = "repository",
    confidence: str = "high",
    seconds: float = 0.0,
    command: str = "",
    **detail,
) -> dict:
    record = {
        "phase": name,
        "status": status,
        "reason_code": reason_code,
        "attribution": attributed_to,
        "confidence": confidence,
        "seconds": round(seconds, 1),
        "command": command,
    }
    record.update(detail)
    return record


def _fail_phase(
    name: str, log: str, timed_out: bool, repo: Path, command: str, seconds: float
) -> tuple[dict, str, str]:
    ecode = error_class(log, timed_out)
    cls = reconcile(classify(log, timed_out, repo), ecode, repo)
    owner = attribution(cls, ecode)
    return (
        _phase(
            name,
            "timed_out" if timed_out else "failed",
            ecode,
            owner,
            _confidence(cls, ecode),
            seconds,
            command,
            failure_class=cls,
        ),
        cls,
        ecode,
    )


def _blame_runtime(record: dict, phase: dict) -> None:
    """Override a phase's already-computed failure once a toolchain version mismatch is known.

    A caller runs this after `_resolve_runtime` has found `rt.unsatisfied`, and after the
    phase has already been classified from its command output -- so whatever `_fail_phase`
    concluded from the log text is deliberately discarded in favour of the one fact that is
    more reliable than any log message: the toolchain itself was wrong. On `phase`, this sets
    `failure_class`, `attribution`, `reason_code` and `confidence`. On the project-level
    `record`, it sets `failure_class`, `build_error_class` and `attribution`; `record` carries
    no `confidence` or `reason_code` field, so those two are only ever touched on `phase`.
    """
    phase["failure_class"] = "ENVIRONMENT"
    phase["attribution"] = "runner"
    phase["reason_code"] = "wrong_runtime"
    phase["confidence"] = "high"
    record["failure_class"] = "ENVIRONMENT"
    record["build_error_class"] = "wrong_runtime"
    record["attribution"] = "runner"


def _blank_record(project: Project) -> dict:
    """The record every project starts from: every key present, every measurement still unset."""
    return {
        "project_id": project.project_id,
        "relative_root": scrub(project.rel) or ".",
        "ecosystem": project.ecosystem,
        "toolchain": None,
        "workspace": project.workspace,
        "members": [scrub(m) for m in project.members[:20]],
        "required": project.required,
        "phases": [],
        "n_tests_collected": None,
        "n_passed": None,
        "n_failed": None,
        "coverage_pct": None,
        "coverage_method": None,
        # Defaults to the repository rather than to `None`: even a project that never fails
        # needs an owner on record, since a consumer asking "who is accountable here" should
        # get an answer on the clean path as well as the failing one.
        "failure_class": "NONE",
        "build_error_class": None,
        "attribution": "repository",
        "relaxed_retry": None,
        "skipped_reason": None,
        # The requested-versus-resolved runtime records for this project. Stays empty when the
        # tree makes no version request at all -- distinct from a request this host failed to
        # satisfy, which produces entries here instead.
        "runtime": [],
    }


def _skipped_record(project: Project, reason_code: str) -> dict:
    """Build the record for a project the probe never got to: all five phases marked skipped.

    Attribution is always `"runner"` here, never the repository, because the reason a project
    was skipped -- the clock, or the cap on how many roots get probed -- is entirely about
    this run's own limits, not about anything the project did. Producing a null measurement
    for a skipped project (rather than, say, a zero) is also what lets `_aggregate` turn a
    tree-wide result null when part of it was never reached, instead of quietly reporting a
    number that only covers the projects the run happened to have time for.
    """
    record = _blank_record(project)
    record["skipped_reason"] = reason_code
    record["failure_class"] = "NONE"
    record["attribution"] = "runner"
    for phase in PHASES:
        record["phases"].append(_phase(phase, "skipped_budget", reason_code, "runner", "high"))
    return record


def _resolve_runtime(project: Project, env: dict, allow_home: bool) -> runtime.Plan:
    """Resolve only the ONE lane this project's commands will run on.

    That is what keeps resolution affordable per root: a Node package does not pay to enumerate
    every JDK on the host, and an ecosystem with no version source of its own resolves nothing.
    """
    lane = ECOSYSTEM_LANE.get(project.ecosystem)
    if lane is None:
        return runtime.Plan()
    return runtime.resolve(project.root, env, allow_home=allow_home, lanes=(lane,))


def _probe_project(
    project: Project,
    repo: Path,
    scratch: Path,
    env: dict,
    budget: Budget,
    project_end: float,
    level: str,
    restore: list[tuple[Path, int]],
    tried: list[list[str]],
    allow_home: bool = False,
) -> dict:
    """Run resolve, build, discover, test and coverage for one project against a shared budget.

    `project_end` is the monotonic timestamp at which this project's allotted slice of the run
    runs out. Before each command, `allowance()` re-derives what that command may actually take:
    the smallest of the fixed per-command ceiling, the time left in this project's slice, and the
    time left in the run as a whole. Once that comes out too small to bother starting a command,
    every phase from that point on is recorded skipped and control returns to the caller, which
    reallocates whatever time this project did not use.
    """
    record = _blank_record(project)

    def allowance() -> int:
        return budget.for_phase(project_end)

    def skipped_from(phase_name: str, reason: str) -> dict:
        """Mark `phase_name` onward as skipped for `reason`: this run's own limit, not a failure."""
        record["skipped_reason"] = reason
        status = "skipped_level" if reason == "level_discover_only" else "skipped_budget"
        if status == "skipped_budget" and record["failure_class"] == "NONE":
            # A budget skip is charged to the runner. Stopping at the requested build level,
            # by contrast, is not a failure at all, so it leaves whatever attribution already
            # stood.
            record["attribution"] = "runner"
        for later in PHASES[PHASES.index(phase_name) :]:
            record["phases"].append(_phase(later, status, reason, "runner", "high"))
        return record

    def blocked_from(phase_name: str, reason: str) -> dict:
        """Mark `phase_name` onward blocked, carrying forward whoever the triggering failure named.

        Without an explicit owner, a blocked phase would default to reading as unattributable,
        which hides the one thing a reader actually wants to know: who is on the hook for it.
        """
        cause = record["attribution"] or "runner"
        for later in PHASES[PHASES.index(phase_name) :]:
            record["phases"].append(_phase(later, "blocked", reason, cause, "high"))
        return record

    if not allowance():
        return skipped_from("resolve", "run_budget_exhausted")

    pscratch = scratch / project.project_id
    pscratch.mkdir(parents=True, exist_ok=True)
    # For a Python project, `plan_for` creates a virtualenv, and that creation is itself a
    # subprocess call, so it is charged against `allowance()` like every other command here.
    rt = _resolve_runtime(project, env, allow_home)
    plan = plan_for(project, pscratch, env, allowance(), restore, rt)
    record["toolchain"] = plan.get("toolchain")
    # From here on, every command uses plan["env"], not the `env` this function received:
    # plan_for layers the resolved runtime's paths on top of it, and that overlay is what
    # actually puts the chosen toolchain on PATH.
    env = plan["env"]
    record["runtime"] = rt.records
    wrong_runtime = bool(rt.unsatisfied)

    if not plan.get("tool"):
        preflight = plan.get("preflight") or f"command not found: {project.ecosystem}"
        cls = classify(preflight, False, project.root)
        ecode = error_class(preflight, False)
        record["failure_class"] = "ENVIRONMENT" if cls == "UNCLASSIFIED" else cls
        record["build_error_class"] = ecode
        record["attribution"] = attribution(record["failure_class"], ecode)
        phase = _phase("resolve", "unavailable", ecode, record["attribution"], "high")
        record["phases"].append(phase)
        if wrong_runtime:
            _blame_runtime(record, phase)
            phase["status"] = "unavailable"
        return blocked_from("build", "resolve_unavailable")
    if not runtime.which(plan["locked"][0], env) and not Path(plan["locked"][0]).exists():
        record["failure_class"] = "ENVIRONMENT"
        record["build_error_class"] = "toolchain_missing"
        record["attribution"] = "runner"
        record["phases"].append(
            _phase("resolve", "unavailable", "toolchain_missing", "runner", "high")
        )
        return blocked_from("build", "resolve_unavailable")

    # --- phase 1: locked dependency resolution ----------------------------------------
    seconds = allowance()
    if not seconds:
        return skipped_from("resolve", "run_budget_exhausted")
    started = time.monotonic()
    tried.append(plan["locked"])
    rc, log, timed_out = _run(plan["locked"], project.root, env, seconds)
    elapsed = time.monotonic() - started
    if rc == 0:
        record["phases"].append(
            _phase(
                "resolve",
                "passed",
                "locked_install_succeeded",
                "repository",
                "high",
                elapsed,
                _display(plan["locked"]),
            )
        )
    else:
        phase, cls, ecode = _fail_phase(
            "resolve", log, timed_out, project.root, _display(plan["locked"]), elapsed
        )
        record["phases"].append(phase)
        record["failure_class"], record["build_error_class"] = cls, ecode
        record["attribution"] = attribution(cls, ecode)
        if wrong_runtime:
            _blame_runtime(record, phase)
        # The relaxed install below is diagnostic information only, never a second chance at the
        # verdict: if the locked graph does not resolve, that graph is broken regardless of what
        # a looser install would have accepted, so its result is attached alongside the recorded
        # failure and cannot overwrite it.
        relaxed = plan.get("relaxed")
        if relaxed is not None and not timed_out and allowance():
            tried.append(relaxed)
            rc2, log2, to2 = _run(relaxed, project.root, env, allowance())
            record["relaxed_retry"] = {
                "phase": "resolve",
                "ok": rc2 == 0,
                "reason_code": (
                    "relaxed_install_succeeded" if rc2 == 0 else error_class(log2, to2)
                ),
                "note": "diagnostic only; the locked install is the measurement",
            }
        return blocked_from("build", "resolve_failed")

    # Installs a test harness of our own choosing plus anything else the project's test extras
    # need. This runs best-effort and its result is not folded into the resolve verdict at all:
    # neither "the project never named a test runner" nor "the dev extras failed to install" is
    # a resolve failure -- the discover phase below is where either of those shows up instead.
    for command in _as_commands(plan.get("harness")):
        if not allowance():
            break
        _run(command, project.root, env, min(allowance(), 300))

    # --- phase 2: build ---------------------------------------------------------------
    build_cmd = plan.get("build")
    if build_cmd is None:
        record["phases"].append(
            _phase("build", "passed", "interpreted_no_build_step", "repository", "high")
        )
    else:
        seconds = allowance()
        if not seconds:
            return skipped_from("build", "run_budget_exhausted")
        started = time.monotonic()
        tried.append(build_cmd)
        rc, log, timed_out = _run(build_cmd, project.root, env, seconds)
        elapsed = time.monotonic() - started
        if rc == 0:
            record["phases"].append(
                _phase(
                    "build",
                    "passed",
                    "build_succeeded",
                    "repository",
                    "high",
                    elapsed,
                    _display(build_cmd),
                )
            )
        else:
            phase, cls, ecode = _fail_phase(
                "build", log, timed_out, project.root, _display(build_cmd), elapsed
            )
            record["phases"].append(phase)
            record["failure_class"], record["build_error_class"] = cls, ecode
            record["attribution"] = attribution(cls, ecode)
            if wrong_runtime:
                _blame_runtime(record, phase)
            if plan.get("build_relaxed") is not None and not timed_out and allowance():
                rc2, log2, to2 = _run(plan["build_relaxed"], project.root, env, allowance())
                record["relaxed_retry"] = {
                    "phase": "build",
                    "ok": rc2 == 0,
                    "reason_code": (
                        "relaxed_build_succeeded" if rc2 == 0 else error_class(log2, to2)
                    ),
                    "note": "diagnostic only; the locked build is the measurement",
                }
            return blocked_from("discover", "build_failed")

    # --- phase 3: the runner's own test discovery -------------------------------------
    discovery_step = plan.get("discover")
    n_collected: int | None = None
    if discovery_step is None:
        record["phases"].append(
            _phase("discover", "unavailable", "no_native_discovery", "runner", "medium")
        )
    else:
        argv, kind = discovery_step
        unit = "tests"
        seconds = allowance()
        if not seconds:
            return skipped_from("discover", "run_budget_exhausted")
        started = time.monotonic()
        tried.append(argv)
        rc, log, timed_out = _run(argv, project.root, env, seconds)
        elapsed = time.monotonic() - started
        if kind in _PARSERS:
            n_collected = _PARSERS[kind](log).get("collected")
        elif kind:
            n_collected = parsers.count_listing(log, kind)
            unit = _LISTING_UNIT.get(kind, "tests")
        if timed_out:
            record["phases"].append(
                _phase(
                    "discover", "timed_out", "timeout", "runner", "high", elapsed, _display(argv)
                )
            )
            return blocked_from("test", "discovery_timed_out")
        # An empty test suite is only recorded when the discovery command itself reported it
        # that way -- a zero exit, or the specific exit code its documentation reserves for
        # "found nothing." Every other exit code means the command failed to run at all, and
        # conflating that with "no tests" would make a project whose runner is simply absent
        # look like one that genuinely has no tests to collect.
        if rc not in (0, 5) and not n_collected:
            phase, cls, ecode = _fail_phase(
                "discover", log, timed_out, project.root, _display(argv), elapsed
            )
            phase["status"] = "unavailable"
            phase["attribution"] = (
                "runner" if ecode == "toolchain_missing" else phase["attribution"]
            )
            record["phases"].append(phase)
            record["n_tests_collected"] = None
            if record["failure_class"] == "NONE":
                record["attribution"] = phase["attribution"]
            if wrong_runtime:
                _blame_runtime(record, phase)
                phase["status"] = "unavailable"
            return blocked_from("test", "discovery_unavailable")
        if n_collected is None:
            # A successful exit with no parseable count means this tool has no parser for this
            # invocation style, or the selector it built happened to enumerate nothing -- either
            # way, a gap in what we can read, not evidence the suite is empty. Marking it that
            # way instead of as "no tests" avoids mistaking our own blind spot for a repository
            # fact, and control still falls through to actually running the suite afterward.
            record["phases"].append(
                _phase(
                    "discover",
                    "unavailable",
                    "discovery_produced_no_count",
                    "runner",
                    "medium",
                    elapsed,
                    _display(argv),
                )
            )
        else:
            if unit == "tests":
                record["n_tests_collected"] = n_collected
            if n_collected == 0:
                # The discovery step ran successfully and counted zero tests -- a fact about the
                # repository's own suite, and one that must not be reported as any kind of
                # failure on our part.
                record["phases"].append(
                    _phase(
                        "discover",
                        "no_tests",
                        "runner_collected_zero_tests",
                        "repository",
                        "high",
                        elapsed,
                        _display(argv),
                        discovered=0,
                        discovery_unit=unit,
                    )
                )
                record["phases"].append(
                    _phase("test", "no_tests", "nothing_to_execute", "repository", "high")
                )
                record["phases"].append(
                    _phase("coverage", "no_tests", "nothing_to_measure", "repository", "high")
                )
                return record
            record["phases"].append(
                _phase(
                    "discover",
                    "passed",
                    "runner_collected_tests",
                    "repository",
                    "high",
                    elapsed,
                    _display(argv),
                    discovered=n_collected,
                    discovery_unit=unit,
                )
            )

    # --- phase 4: executing the suite -------------------------------------------------
    # Stopping here at the `discover` level is intentional: everything up to this point has
    # already been executed and measured, and it is only the suite itself -- the costly part --
    # that is being skipped. The record below reflects that as a choice about build level, never
    # as a suite that was run and turned up empty.
    if level != "full":
        return skipped_from("test", "level_discover_only")
    test_step = plan.get("test")
    if test_step is None:
        record["phases"].append(
            _phase("test", "no_tests", "no_declared_test_command", "repository", "high")
        )
        record["phases"].append(
            _phase("coverage", "no_tests", "nothing_to_measure", "repository", "high")
        )
        return record
    argv, kind = test_step
    seconds = allowance()
    if not seconds:
        return skipped_from("test", "run_budget_exhausted")
    started = time.monotonic()
    tried.append(argv)
    rc, log, timed_out = _run(argv, project.root, env, seconds)
    elapsed = time.monotonic() - started
    counts = _PARSERS[kind](log) if kind in _PARSERS else {}
    if not counts and plan.get("test_junit"):
        counts = parsers.junit_counts(project.root, plan["test_junit"])
    if not counts and plan.get("test_fallback") and not timed_out and allowance():
        # The coverage-instrumented command failed because it depends on a plugin this project
        # never installed. Fall back to the plain invocation: coverage is a bonus measurement,
        # but getting the suite to run at all is not optional.
        argv, kind = plan["test_fallback"]
        tried.append(argv)
        rc, log, timed_out = _run(argv, project.root, env, allowance())
        counts = _PARSERS[kind](log) if kind in _PARSERS else {}
    # When a parser returns counts at all, an absent failure count means the runner reported
    # zero failures, and that gets recorded as the number 0 -- leaving it unset instead would
    # make a suite with no failures look identical to one this tool simply failed to parse.
    record["n_passed"] = counts.get("passed", 0) if counts else None
    record["n_failed"] = (counts.get("failed", 0) + counts.get("errored", 0)) if counts else None
    if record["n_tests_collected"] is None and counts.get("collected") is not None:
        record["n_tests_collected"] = counts["collected"]
    if timed_out:
        record["phases"].append(
            _phase("test", "timed_out", "timeout", "runner", "high", elapsed, _display(argv))
        )
        return blocked_from("coverage", "test_timed_out")
    if not counts and rc == 5:
        record["phases"].append(
            _phase(
                "test",
                "no_tests",
                "runner_collected_zero_tests",
                "repository",
                "high",
                elapsed,
                _display(argv),
            )
        )
        record["phases"].append(
            _phase("coverage", "no_tests", "nothing_to_measure", "repository", "high")
        )
        return record
    if not counts and rc not in (0, 1):
        phase, cls, ecode = _fail_phase(
            "test", log, timed_out, project.root, _display(argv), elapsed
        )
        record["phases"].append(phase)
        if record["failure_class"] == "NONE":
            record["failure_class"], record["build_error_class"] = cls, ecode
            record["attribution"] = attribution(cls, ecode)
        if wrong_runtime:
            _blame_runtime(record, phase)
        return blocked_from("coverage", "tests_did_not_run")
    # Reaching this point does not require the tests to have passed -- a repository whose suite
    # runs and fails still proved the harness itself works, which is a meaningfully different
    # outcome from having no suite at all, or from a suite that could not even start. Each of the
    # three keeps a distinct `reason_code` so a reader does not have to guess which one applied.
    failed = int(counts.get("failed") or 0) + int(counts.get("errored") or 0)
    record["phases"].append(
        _phase(
            "test",
            "passed",
            "tests_failed" if failed else "tests_passed",
            "repository",
            "high",
            elapsed,
            _display(argv),
            n_passed=counts.get("passed"),
            n_failed=failed,
        )
    )

    # --- phase 5: coverage ------------------------------------------------------------
    coverage = plan.get("coverage")
    if coverage is None:
        record["phases"].append(
            _phase(
                "coverage",
                "unavailable",
                plan.get("coverage_reason") or "no coverage reporter for this ecosystem",
                "runner",
                "high",
            )
        )
        return record
    kind, target = coverage
    pct, covered, total, method = _read_coverage(
        kind, target, env, project.root, max(allowance(), MIN_PHASE_SECONDS)
    )
    if pct is None:
        record["phases"].append(
            _phase("coverage", "unavailable", "reporter_produced_no_total", "runner", "medium")
        )
        return record
    record["coverage_pct"] = pct
    record["coverage_method"] = method
    record["phases"].append(
        _phase(
            "coverage",
            "passed",
            "coverage_measured",
            "repository",
            "high",
            0.0,
            "",
            coverage_pct=pct,
            covered_lines=covered,
            total_lines=total,
        )
    )
    return record


# --- rolling the projects up ------------------------------------------------------------------


def _phase_status(record: dict, name: str) -> str | None:
    """The status of one named phase in one project's record, or None if it never ran."""
    for phase in record["phases"]:
        if phase["phase"] == name:
            return phase["status"]
    return None


def _budget_skipped(record: dict) -> bool:
    """Did the run's clock, or the cap on how many roots may be probed, stop this project?"""
    return any(p["status"] == "skipped_budget" for p in record["phases"])


#: Maps an `attribution` enum value to the plain-English phrase used inside `_index_reason`'s
#: generated sentence. Kept out of that sentence as a raw enum value on purpose: the redaction
#: audit that scans emitted prose for identifier-shaped tokens cannot tell an underscored enum
#: name apart from a leaked one, and would flag it either way.
_ATTRIBUTION_PROSE = {
    "runner": "this runner",
    "external_service": "an external service",
    "credentials": "a credential the tree does not declare",
    "unknown": "a cause that could not be attributed",
}


def _suite_blocked_by_us(record: dict) -> str | None:
    """Return which non-repository party kept this project's test phase from running.

    Looks only at the record's test phase. A phase that passed, or that did not pass for a
    reason already attributed to the repository, yields None: the repository owns that
    outcome and it may count against the score. Any other attribution on the phase --
    "runner", "external_service", "credentials", or "unknown" -- is returned unchanged, and a
    record that carries no test phase at all is reported as blocked by "unknown" rather than
    left ambiguous. The attribution itself was decided when the phase was classified; this
    function only reports it back.
    """
    for phase in record["phases"]:
        if phase["phase"] != "test":
            continue
        if phase["status"] == "passed":
            return None
        return None if phase["attribution"] == "repository" else phase["attribution"]
    return "unknown"


def _index_reason(level: str, skipped: bool, blocked_by: list[str], build_ok: bool | None) -> str:
    """Explain why `observed_runnability` came out null, given it is only called for that case.

    Every branch here names a limit of the run itself -- never the repository. A repository
    whose own code fails produces a low, present index value, not a null one, so that case is
    handled elsewhere and this function is never reached for it.
    """
    if level != "full":
        return "this build level never runs the suite, so the index has no executed term to sum"
    if skipped:
        return "the run's clock expired partway through, leaving some of the probe unrun"
    if build_ok is None:
        return (
            "no build verdict holds across the whole tree, so the index would be adding up "
            "terms that were never actually observed"
        )
    if blocked_by:
        return (
            f"nothing ran a suite, and what stopped it belongs to "
            f"{_ATTRIBUTION_PROSE.get(blocked_by[0], 'a cause outside the repository')} "
            f"rather than to the repository; those terms are missing here, not zero"
        )
    return "not one term of the index was executed"


def _aggregate(records: list[dict], discovery: dict, level: str = "full") -> dict:
    """Fold every project's record into the tree-wide install/build/test verdict.

    Sorts the required projects into who resolved, who built, who failed for a cause the
    repository owns, and who the run's own budget cut off, then derives `install_ok` and
    `build_ok` as tri-states from those groups: True only when every required project cleared
    that phase, False only when a project failed for a repository-owned reason, and None for
    every other outcome -- an ecosystem discovery never reached, a budget skip, a runner-side
    limit, or a scan that did not finish. A None here reflects that the run could not tell,
    and it must never be read back as a failure nobody actually observed. The pass/fail test
    counts and the two runnability indices assembled later in this function follow the same
    rule: any budget or runner limit anywhere nulls them instead of letting an unmeasured
    project score silently as zero.
    """
    required = [r for r in records if r["required"]]
    built = [r for r in required if _phase_status(r, "build") == "passed"]
    resolved = [r for r in required if _phase_status(r, "resolve") == "passed"]
    intrinsic = [r for r in required if r["failure_class"] == "REPO_INTRINSIC"]
    skipped = [r for r in required if _budget_skipped(r)]
    limited = [
        r
        for r in required
        if r not in built
        and (
            _phase_status(r, "resolve") in ("unavailable", "timed_out")
            or _phase_status(r, "build") in ("unavailable", "timed_out")
            or r in skipped
            or r["attribution"] in ("runner", "external_service")
        )
    ]

    unreachable = discovery["unreachable_ecosystems"]
    unresolved_repo = [
        r
        for r in required
        if r not in resolved and r["attribution"] in ("repository", "credentials")
    ]
    if not required:
        install_ok = None
    elif len(resolved) == len(required):
        install_ok = True
    elif unresolved_repo:
        install_ok = False
    else:
        install_ok = None

    if not required:
        build_ok, reason = (
            None,
            (
                "discovery found no project root at all, so nothing was attempted"
                if discovery["no_manifest"]
                else "the scan left out every root it found: "
                + (", ".join(unreachable) or "reason unrecorded")
            ),
        )
    elif len(built) == len(required) and not unreachable and not skipped:
        build_ok, reason = True, f"every one of the {len(required)} required projects built"
    elif len(built) == len(required) and not skipped:
        build_ok, reason = (
            None,
            (
                f"the {len(required)} projects that were probed all built, but the scan never "
                f"reached every declared one ({', '.join(unreachable)}), so the tree is only "
                f"partly covered"
            ),
        )
    elif skipped and not intrinsic:
        build_ok, reason = (
            None,
            (
                f"{len(built)} of {len(required)} required projects built and the clock ran out "
                f"before {len(skipped)} of them were reached, so there is no verdict to give"
            ),
        )
    elif intrinsic:
        build_ok, reason = (
            False,
            (
                f"{len(intrinsic)} of the {len(required)} required projects failed for a cause "
                f"the repository itself owns"
            ),
        )
    elif limited or discovery["truncated"] or unreachable:
        build_ok, reason = (
            None,
            (
                f"{len(built)} of {len(required)} required projects built; this runner or a "
                f"partial scan limited the others, so there is no verdict to give"
            ),
        )
    else:
        build_ok, reason = (
            None,
            (
                f"{len(built)} of {len(required)} required projects built, and what stopped the "
                f"rest could not be attributed to either side"
            ),
        )

    test_statuses = {_phase_status(r, "test") for r in required}
    n_passed = sum(int(r["n_passed"] or 0) for r in required)
    n_failed = sum(int(r["n_failed"] or 0) for r in required)
    ran = [r for r in required if _phase_status(r, "test") == "passed"]
    if ran:
        tests_status = "tests_failed" if n_failed else "tests_passed"
    elif required and test_statuses <= {"no_tests"}:
        tests_status = "no_tests"
    elif not required:
        tests_status = "unknown"
    elif required and test_statuses <= {"skipped_level", "skipped_budget", "no_tests"}:
        # Covers every required project whose test phase is skipped_level, skipped_budget, or
        # no_tests, with no passing phase anywhere: no suite was ever going to run, whether
        # because the level excluded it or the budget ran out first. This is kept distinct from
        # `tests_did_not_run`, which implies an attempt was made and failed.
        tests_status = "not_attempted"
    else:
        tests_status = "tests_did_not_run"

    cov = [r for r in required if r["coverage_pct"] is not None]
    # Coverage is only "expected" from a project whose coverage phase actually reached a verdict
    # (passed or unavailable); a project that never got that far -- blocked earlier, or skipped
    # -- is excluded here rather than counted as a project that should have reported and did not.
    expected = [r for r in required if _phase_status(r, "coverage") in ("passed", "unavailable")]
    coverage_pct = round(sum(r["coverage_pct"] for r in cov) / len(cov), 2) if cov else None

    tests_ran = bool(ran)
    # `index` below sums four booleans, and each one is only meaningful if it was actually
    # observed rather than assumed. Deciding between a null index and a low one comes down to
    # asking, for whichever term is missing, whose decision left it that way.
    #
    # If the gap traces back to something this run controls -- a phase this run chose to skip,
    # a build whose pass/fail state could not be pinned down, or a suite that never started
    # because of a runtime this host lacked, an index that could not be reached, or the clock --
    # then `index` must come out null. Scoring that as a zero would be indistinguishable from
    # scoring a repository that tried everything and failed everything.
    #
    # If instead the gap is the codebase's own doing -- a build that fails on its own terms, or
    # a runner that looked for tests and genuinely found none -- that is a real, low measurement,
    # not a missing one, and it is supposed to score low.
    #
    # `blocked_by` is only consulted when not a single project managed to run a suite; the moment
    # even one has, the run has direct evidence to report instead of a guess about who is at
    # fault. This matters on a tree with several ecosystems: if the Rust suite executes while the
    # JavaScript toolchain is simply not on this host, treating "a suite ran somewhere" as false
    # would discard a measurement the probe was able to make.
    skipped_phases = any(p["status"] in _SKIPPED_STATUSES for r in required for p in r["phases"])
    blocked_by = sorted({a for a in (_suite_blocked_by_us(r) for r in required) if a})
    attempted_execution = (
        level == "full"
        and not skipped_phases
        and build_ok is not None
        and (tests_ran or not blocked_by)
    )
    index = (
        (int(build_ok is True) + int(tests_ran) + int(n_passed > 0) + int((coverage_pct or 0) > 0))
        if attempted_execution
        else None
    )
    return {
        "build_ok": build_ok,
        "install_ok": install_ok,
        "build_ok_reason": reason,
        "build_level": level,
        "n_required": len(required),
        "n_resolved": len(resolved),
        "n_built": len(built),
        "n_intrinsic_failures": len(intrinsic),
        "n_runner_limited": len(limited),
        "n_budget_skipped": len(skipped),
        "tests_status": tests_status,
        "n_tests_collected": sum(int(r["n_tests_collected"] or 0) for r in required) or None,
        "n_passed": n_passed if level == "full" else None,
        "n_failed": n_failed if level == "full" else None,
        "coverage_pct": coverage_pct,
        "coverage_projects_measured": len(cov),
        "coverage_projects_expected": len(expected),
        "coverage_complete": bool(expected) and len(cov) == len(expected),
        "observed_runnability": index,
        # `index` alone cannot distinguish a null score from a real zero once serialised, so this
        # flag carries that distinction explicitly for whatever reads the block next.
        "observed_runnability_complete": (
            index is not None
            and build_ok is not None
            and not discovery["truncated"]
            and not unreachable
        ),
        "observed_runnability_reason": (
            None
            if index is not None
            else _index_reason(level, skipped_phases, blocked_by, build_ok)
        ),
        # Structured alongside `observed_runnability_reason`'s free text, so a caller who wants
        # to branch on who is responsible can check this list instead of pattern-matching prose.
        "observed_runnability_blocked_by": blocked_by,
        "unreachable_ecosystems": unreachable,
    }


# --- shaping the emitted block ----------------------------------------------------------------


def _clean_commands(values) -> list[str]:
    """Commands are evidence, and they are the one field here that can carry a path, so every
    entry goes through the redaction pass rather than relying on an exemption."""
    out: list[str] = []
    for value in values or []:
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned = scrub(" ".join(value.split())[:200])
        if cleaned:
            out.append(cleaned)
        if len(out) >= MAX_COMMANDS:
            break
    return out


def _skipped(note: str = "no build was asked for, so every runnability field is unscored") -> dict:
    """Build the shared shape for a build probe that produced no measurements at all.

    Every runnability field is left out of this dict entirely rather than set to `False`, so a
    consumer can tell "this was never measured" apart from "this was measured and came up
    negative."
    """
    return {"probe": "build", "build_skipped": True, "ok": True, "note": note}


def skipped_budget(seconds_left: float = 0.0) -> dict:
    """Build the stub for a probe `collect()` never started because the budget was already spent.

    Its `note` differs from `_skipped()`'s default text on purpose: `level="none"` is a caller's
    deliberate choice, while running out of budget is an operational limit whose fix is to
    allow more time, and the two should not read as the same situation.
    """
    return _skipped(
        f"the run had {int(max(0.0, seconds_left))} seconds left when the build probe's turn "
        f"came, too few to start it; every runnability measurement is therefore null"
    )


#: The repair pass is not part of this tool, so its fields report the state it never entered.
#: They are emitted rather than omitted because "not offered" and "offered and nothing needed
#: it" are different facts, and a reader who sees neither field cannot tell them apart.
_REPAIR_NOT_OFFERED = {
    "repair_offered": False,
    "repair_attempted_n": None,
    "repair_succeeded_n": None,
    "repair_refused_n": None,
    "repair_rejected_source_edit_n": None,
    "repair_seconds": None,
}


def _finalise(
    out: dict,
    cls: str,
    install_ok: bool | None,
    build_ok: bool | None,
    discovered: bool,
    ran: bool | None,
    timed_out: bool,
    n_passed: int = 0,
    discover_measured: bool = True,
) -> dict:
    """Make the flags on `out` mutually consistent, then derive the two summary indices from them.

    First, three adjustments to the raw flags: a failed install forces `build_ok` to False
    regardless of what the build phase itself reported; a `build_ok` that is neither None nor
    already a bool is coerced to one; and `ran`, when it is not None, is narrowed to True only
    if the suite was discovered AND the build did not fail -- otherwise it becomes False. `ran`
    passed in as None is left as None; it is never forced to a boolean. Separately, a leftover
    failure class on a build that ended up succeeding is reset to "NONE", and a build that
    failed without any class attached is given "UNCLASSIFIED" rather than being left empty.

    With the flags settled, `observed_runnability` is the count of `build_ok is True`, `ran`,
    `n_passed > 0` and `coverage > 0` -- but only computed at all when `ran` is not None, since
    a suite nobody attempted should score as absent, not as a zero. `discover_runnability`
    similarly counts `install_ok`, `build_ok` and `discovered`, and stays None unless the caller
    passed `discover_measured=True` and both `install_ok` and `build_ok` resolved to an actual
    bool. Deriving both indices here, from the same flags this function just reconciled, is what
    guarantees neither one can ever print a number that contradicts `install_ok` or `build_ok`.
    """
    if install_ok is False:
        build_ok = False
    elif build_ok is not None and install_ok is not None:
        build_ok = bool(build_ok)
    discovered = bool(discovered)
    # Checked with `is not False`, deliberately not `is True`: on a tree with more than one
    # project, `build_ok` can land on None because one project's toolchain was unavailable even
    # though a different project's suite genuinely executed. Demanding a positive build verdict
    # here would misreport that second project's suite as one that never ran.
    ran = None if ran is None else bool(ran and discovered and build_ok is not False)
    if build_ok is True and cls in ("REPO_INTRINSIC", "UNCLASSIFIED"):
        cls = "NONE"
    # A tree where nothing was ever attempted has no failure class of its own to fall back on,
    # so it stays "NONE". A build that WAS attempted and still ended up False gets promoted to
    # "UNCLASSIFIED" instead, so a build failure is never silently reported as a clean tree.
    if build_ok is False and cls == "NONE" and out.get("build_attempted"):
        cls = "UNCLASSIFIED"
    coverage = out.get("coverage_pct")
    out.update(
        {
            "install_ok": install_ok,
            "build_ok": build_ok,
            "tests_discovered": discovered,
            "build_and_tests_ran": ran,
            "failure_class": cls,
            "repo_intrinsic_failure": cls == "REPO_INTRINSIC",
            "timed_out": bool(timed_out),
            "observed_runnability": (
                None
                if ran is None
                else int(build_ok is True) + int(ran) + int(n_passed > 0) + int((coverage or 0) > 0)
            ),
            "discover_runnability": (
                None
                if not discover_measured or install_ok is None or build_ok is None
                else int(bool(install_ok)) + int(bool(build_ok)) + int(discovered)
            ),
            "ok": True,
        }
    )
    return out


# Version managers (rustup, nvm, pyenv and the like) install toolchains under the operator's
# home directory, and `_scratch_env` deliberately drops home-directory entries from the child
# PATH -- so a toolchain that only exists there is invisible to the commands this module runs,
# by design, and no attempt is made to locate one anyway. This is a known and accepted trade-off
# rather than an oversight: on a host where Rust was only ever installed via rustup into $HOME,
# a Rust project here comes back unavailable, attributed to the runner rather than scored as a
# repository failure. A toolchain installed outside the home directory is measured normally.


def _scratch_env(scratch: Path) -> dict:
    """Build the environment the probe's child processes run under, redirected into `scratch`.

    Every command launched from this environment belongs to the repository being probed --
    its installer, its build tool, its test runner, and any hook or plugin those pull in. None
    of it is trusted, so the base environment carries no cloud, version-control, database,
    package-index, SSH or other provider credential, and `home=scratch` also keeps the
    operator's own dotfiles out of reach. On top of that base, each ecosystem's cache and
    package directory below is pointed at a path under `scratch`, so a package manager cannot
    write into a system directory or into the checkout itself; either of those would be this
    module's mistake, not the repository's, yet would still be recorded against the repository
    if it were allowed to happen.
    """
    env = env_mod.build_env(domain=env_mod.BUILD, home=scratch)
    env.update(
        {
            "GEM_HOME": str(scratch / "gems"),
            "BUNDLE_PATH": str(scratch / "gems"),
            "PIP_CACHE_DIR": str(scratch / "pip"),
            "npm_config_cache": str(scratch / "npm"),
            "YARN_CACHE_FOLDER": str(scratch / "yarn"),
            "PNPM_HOME": str(scratch / "pnpm"),
            "npm_config_store_dir": str(scratch / "pnpm-store"),
            "GOPATH": str(scratch / "go"),
            "GOMODCACHE": str(scratch / "go" / "pkg" / "mod"),
            "GOFLAGS": "-mod=mod",
            "CARGO_HOME": str(scratch / "cargo"),
            "COMPOSER_HOME": str(scratch / "composer"),
            "COMPOSER_VENDOR_DIR": str(scratch / "vendor"),
            "COVERAGE_FILE": str(scratch / ".coverage"),
            "GRADLE_USER_HOME": str(scratch / "gradle"),
            "NUGET_PACKAGES": str(scratch / "nuget"),
            "CI": "1",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "npm_config_update_notifier": "false",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_ROOT_USER_ACTION": "ignore",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
        }
    )
    return env


def _coverage_reason(records: list[dict], aggregate: dict) -> str:
    """Produce the specific sentence for a missing `coverage_pct`; a bare "not measurable" is not
    useful on its own to whoever reads it."""
    if aggregate["tests_status"] == "no_tests":
        return "nothing here declares a test to instrument"
    if aggregate["tests_status"] == "not_attempted":
        return (
            "no suite ran, so there was nothing for a reporter to instrument; ask for the "
            "full build level if coverage is wanted"
        )
    if aggregate["tests_status"] == "tests_did_not_run":
        return "not one suite got as far as executing, so nothing was instrumented"
    for record in records:
        for phase in record["phases"]:
            if phase["phase"] == "coverage" and phase["status"] == "unavailable":
                return phase["reason_code"]
    return "no reporter wrote a total this tool could read back"


def _probe_tree(
    repo: Path,
    scratch: Path,
    budget: Budget,
    restore: list[tuple[Path, int]],
    level: str,
    max_projects: int,
    allow_home: bool = False,
) -> dict:
    """Run every discovered project through its phases and assemble the tree-level block.

    Projects come off the queue largest first. Just before each one starts, its slice of the
    time still remaining is recomputed in proportion to its own weight against the weight of
    everything still waiting, so a project that finishes early or fails outright returns its
    unused share to the queue rather than losing it. Once the clock is actually exhausted, the
    rest of the queue is recorded as skipped instead of attempted -- the same condition
    `_aggregate` treats no differently from a scan that never reached those projects.

    Beyond invoking `_probe_project` per project, this also enforces the `max_projects` cap
    (recording the overflow as capped, not probed), tallies each project's phase-status
    outcome, and adds the runtime summary and the fixed repair-not-offered fields to the
    block before returning it.
    """
    out: dict = {"probe": "build", "build_probe_mode": "deterministic"}
    env = _scratch_env(scratch)

    projects, discovery = discover_projects(repo)
    probe_list, over_cap = _allocate(projects, max_projects)
    tried: list[list[str]] = []
    records: list[dict] = []
    pending = list(probe_list)
    while pending:
        project = pending.pop(0)
        if budget.exhausted:
            records.append(_skipped_record(project, "run_budget_exhausted"))
            continue
        weight_left = project.weight + sum(p.weight for p in pending)
        records.append(
            _probe_project(
                project,
                repo,
                scratch,
                env,
                budget,
                budget.for_project(weight_left / max(1, project.weight)),
                level,
                restore,
                tried,
                allow_home,
            )
        )
    records.extend(_skipped_record(p, "project_cap_reached") for p in over_cap)
    discovery["n_projects_probed"] = len(probe_list)
    discovery["n_projects_over_cap"] = len(over_cap)
    aggregate = _aggregate(records, discovery, level)
    aggregate["budget_seconds"] = int(budget.total)
    aggregate["budget_spent_seconds"] = round(budget.elapsed(), 1)

    counts = {status: 0 for status in STATUSES}
    for record in records:
        status = _phase_status(record, "build") or _phase_status(record, "resolve") or "blocked"
        counts[status] = counts.get(status, 0) + 1
    out["build_projects"] = {
        "projects": records,
        "discovery": discovery,
        "aggregate": aggregate,
        "counts": counts,
    }
    # Surfaced at the top level rather than left buried in each project's `runtime` list: a
    # reader deciding whether a failure below even belongs to the repository needs to know, up
    # front, whether the toolchain that ran was the one the tree actually asked for.
    out.update(runtime.summarise([r["runtime"] for r in records]))
    out.update(_REPAIR_NOT_OFFERED)

    if not projects:
        # The distinction a run cannot afford to lose: a tree with no manifest, versus a scan
        # that gave up. The first is claimed only when discovery reached everything.
        ecode = "no_manifest" if discovery["no_manifest"] else "discovery_incomplete"
        out.update(
            {
                "toolchain": None,
                "build_attempted": False,
                "build_commands_tried": [],
                "coverage_pct": None,
                "build_error_class": ecode,
                "coverage_unsupported_reason": "there is no build definition here to instrument",
                "build_remediation_effort": "unknown",
                "build_remediation_notes": (
                    "Nothing in this tree declares how it is built, so there is nothing to "
                    "install and nothing to run. Declaring it comes before any of the rest."
                ),
                "run_budget_exhausted": False,
                "observed_runnability_reason": (
                    None
                    if discovery["no_manifest"]
                    else "the scan stopped short of some declared root, so nothing ran at all"
                ),
            }
        )
        # A tree with no manifest is a measured property of that tree, so both indices are a
        # real zero. A scan that gave up measured nothing, so both are null.
        return _finalise(
            out,
            "NONE",
            False,
            False,
            False,
            False if discovery["no_manifest"] else None,
            False,
            discover_measured=discovery["no_manifest"],
        )

    primary = next((r for r in records if _phase_status(r, "build") == "passed"), records[0])
    out["toolchain"] = primary["toolchain"]
    out["build_commands_tried"] = _clean_commands([_display(c) for c in tried])
    out["build_attempted"] = bool(tried)

    failing = next((r for r in records if r["failure_class"] == "REPO_INTRINSIC"), None) or next(
        (r for r in records if r["failure_class"] != "NONE"), None
    )
    cls = failing["failure_class"] if failing else "NONE"
    out["build_error_class"] = failing["build_error_class"] if failing else None

    out["coverage_pct"] = aggregate["coverage_pct"]
    if aggregate["coverage_pct"] is None:
        out["coverage_unsupported_reason"] = _coverage_reason(records, aggregate)
    else:
        out["coverage_method"] = primary["coverage_method"] or next(
            (r["coverage_method"] for r in records if r["coverage_method"]), "reported by probe"
        )
        if not aggregate["coverage_complete"]:
            out["coverage_method"] += (
                f" ({aggregate['coverage_projects_measured']} of "
                f"{aggregate['coverage_projects_expected']} projects)"
            )

    effort, notes = _remediation(cls, out["build_error_class"] or "", out["toolchain"])
    out["build_remediation_effort"] = effort
    out["build_remediation_notes"] = notes

    install_ok = aggregate["install_ok"]
    build_ok = aggregate["build_ok"]
    # This asks whether a suite was FOUND, not whether it succeeded -- so a `discover` phase
    # that passed counts even if the `test` phase later failed to run. The second clause exists
    # for ecosystems with no separate discovery step: there, the only evidence of a suite's
    # existence is the test phase itself reporting a positive count, and that counts too.
    discovered = any(
        _phase_status(r, "discover") == "passed"
        or (_phase_status(r, "test") == "passed" and (r["n_tests_collected"] or 0) > 0)
        for r in records
    )
    # Stays None, rather than collapsing to False, exactly when `aggregate` itself could not
    # form an index -- whether because the level stopped short of `full` or the budget ran out
    # first -- so that None carries forward into `_finalise`'s own index computation.
    ran = (
        None
        if aggregate["observed_runnability"] is None
        else aggregate["tests_status"] in ("tests_passed", "tests_failed")
    )
    timed_out = any(p["status"] == "timed_out" for r in records for p in r["phases"])
    # The two ways a project can go unmeasured are counted separately: without this split, a
    # tree that simply has more projects than `max_projects` allows would look identical in the
    # output to one where the probe genuinely ran out of time.
    capped = sum(1 for r in records if r["skipped_reason"] == "project_cap_reached")
    out_of_clock = aggregate["n_budget_skipped"] - capped
    # Surfaced on the block itself because it answers a question a caller cannot answer from
    # anywhere else in the output: would running this again at a cheaper level actually cover
    # more of the tree? Reaching the project cap would not change that answer; running out of
    # clock would.
    out["run_budget_exhausted"] = bool(out_of_clock)
    out["observed_runnability_reason"] = aggregate["observed_runnability_reason"]
    if aggregate["n_budget_skipped"]:
        causes = []
        if out_of_clock:
            causes.append(f"{out_of_clock} because the run had no time left")
        if capped:
            causes.append(f"{capped} because the cap on how many roots are probed was reached")
        out["note"] = (
            f"{aggregate['n_budget_skipped']} of {aggregate['n_required']} project roots went "
            f"unmeasured, "
            + " and ".join(causes)
            + "; what they would have scored is null, not zero"
        )
    elif level != "full":
        out["note"] = (
            "the dependencies were installed and each runner was asked to enumerate its own "
            "tests; running those tests is not part of this build level"
        )
    result = _finalise(
        out, cls, install_ok, build_ok, discovered, ran, timed_out, aggregate["n_passed"] or 0
    )
    aggregate["observed_runnability"] = result["observed_runnability"]
    return result


#: Keys that carry per-project evidence rather than a measurement. Computed either way so that
#: the emitted block is a projection of the full result rather than a different measurement,
#: then dropped on the way out.
_DETAIL_KEYS = ("build_projects", "build_error_class")

#: Keys the block always carries, even when nothing set them. A reader who cannot tell an absent
#: key from a null one cannot tell "this did not apply" from "this tool forgot".
_ALWAYS_PRESENT = (
    "note",
    "error",
    "toolchain",
    "coverage_method",
    "coverage_unsupported_reason",
    "agentic_fallback_reason",
)


def collect(
    repo: Path,
    *,
    timeout: int = DEFAULT_PHASE_TIMEOUT,
    level: str = DEFAULT_LEVEL,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    max_projects: int = MAX_PROBED_PROJECTS,
    allow_home_toolchains: bool = False,
) -> dict:
    """Entry point: probe `repo`'s install/build/test lane and return its build block.

    `level="none"` returns the empty skipped shape immediately, without touching the
    filesystem. For any other level, an exhausted `budget_seconds` at the start skips the
    whole probe rather than beginning it; otherwise this records which artefact paths and
    which file contents already exist under every discovered project root, hands the actual
    work to `_probe_tree`, and guarantees -- in a `finally` block, so it still runs after a
    failure -- that the checkout ends up exactly as it started: files an installer rewrote are
    restored from the recorded snapshot, artefacts this run created are removed, and the
    scratch home directory is deleted. Restoration happens last of all, because deleting a
    dependency directory can itself make a package manager touch a lockfile on its way out.

    `timeout` bounds a single command; `budget_seconds` bounds this whole call, which is the
    limit that matters once a tree has more than a couple of project roots. `max_projects` and
    `allow_home_toolchains` pass straight through to `_probe_tree` and runtime resolution.
    Raises `ValueError` if `level` is not one of `BUILD_LEVELS`.
    """
    if level not in BUILD_LEVELS:
        raise ValueError(f"unknown build level {level!r}; expected one of {BUILD_LEVELS}")
    if level == "none":
        return _skipped()
    budget = Budget(budget_seconds, phase_cap=timeout)
    if budget.exhausted:
        return skipped_budget(budget.remaining())

    scratch = Path(tempfile.mkdtemp(prefix="hazina-build-"))
    # Opened right after the scratch directory is made, so a raise anywhere below --
    # including from discovery or the snapshot themselves -- still reaches the `finally`
    # and the scratch directory is never left behind. Every value the `finally` block reads
    # is therefore given a safe default first, in case the matching step never got to run.
    restore: list[tuple[Path, int]] = []
    artefact_roots: list[Path] = []
    pre_existing: set[tuple[Path, str]] = set()
    snap: dict[Path, bytes | None] = {}
    try:
        # Two separate snapshots of the pre-run state, both taken before any command executes
        # (once something has run, there is no way to tell which changes were already there
        # from which ones this probe made). `pre_existing` records which artefact names already
        # existed, so cleanup only removes names this run introduces; `snap` records file
        # contents for anything an installer might overwrite, so an installer-modified lockfile
        # can be put back. Project discovery runs first, and it only reads the filesystem, so
        # both snapshots end up covering every discovered project root, not just `repo` itself.
        project_roots = [repo, *(p.root for p in discover_projects(repo)[0])]
        artefact_roots = list(dict.fromkeys(project_roots))
        pre_existing = {(root, name) for root in artefact_roots for name in artefact_names(root)}
        snap = snapshot(artefact_roots)
        result = _probe_tree(
            repo, scratch, budget, restore, level, max_projects, allow_home_toolchains
        )
        # Records the level that was actually used, not just requested, directly on the result:
        # this is what lets a reader tell "the suite was never in scope" apart from "the suite
        # was in scope but ran out of time" -- two outcomes that otherwise look alike.
        result["build_level"] = level
        for key in _DETAIL_KEYS:
            result.pop(key, None)
        for key in _ALWAYS_PRESENT:
            result.setdefault(key, None)
        return result
    finally:
        for path, mode in restore:
            try:
                path.chmod(mode)
            except OSError:
                pass
        _rmtree(scratch)
        for root in artefact_roots:
            for name in artefact_names(root):
                if (root, name) in pre_existing:
                    continue
                path = root / name
                if path.is_dir():
                    _rmtree(path)
                elif path.exists():
                    path.unlink(missing_ok=True)
        # Runs after artefact cleanup, not before: removing a dependency directory can itself
        # trigger a package manager to touch a lockfile again on the way out, and the restored
        # snapshot needs to be whatever is written to disk last.
        restore_snapshot(snap)
