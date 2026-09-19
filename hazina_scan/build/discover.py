"""Find the buildable projects in a checkout, and hand out one shared clock to probe them.

A repository is not always one thing to build. It can be a single package, a workspace of
several packages under one root, or several unrelated projects sitting side by side -- a
library next to its docs site, a service next to a client SDK written in a different
language. `discover_projects()` walks the tree once, bounded in depth and directory count,
and returns every root that declares how it is built, tagged with the ecosystem its manifest
names. Where a manifest says "this directory's install command already covers everything
under it" -- an npm/pnpm workspace, a Cargo workspace, a multi-module Maven or Gradle build --
the members underneath are folded into that root instead of being probed on their own, so the
same dependency graph is never installed twice.

None of this opens a shell. The walk reads directory names and a handful of manifest files
with a plain text/JSON read; it never asks git what is tracked, so a directory git would
ignore is scanned exactly like any other -- the probe that later runs commands in these roots
cares what is really on disk, not what is committed.

`Budget` is the other half of the contract: everything this tool runs against a checkout,
across every discovered project and every phase of each one, draws down one wall-clock
allowance rather than getting its own. A project that fails fast or finds nothing to build
hands the time it did not use to whatever probes next, and a single command can never eat
the whole run because its own ceiling is always capped by what its project -- and the run as
a whole -- has left.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MAX_COMMANDS",
    "DEFAULT_PHASE_TIMEOUT",
    "DEFAULT_BUDGET_SECONDS",
    "MIN_PHASE_SECONDS",
    "MIN_PROJECT_SECONDS",
    "MAX_DEPTH",
    "MAX_DIRS",
    "MAX_PROJECTS",
    "MAX_PROBED_PROJECTS",
    "Budget",
    "Project",
    "is_ancillary",
    "scan",
    "peek_ecosystems",
    "node_workspace_globs",
    "cargo_workspace",
    "authoritative_roots",
    "discover_projects",
    "survey",
    "estimate_seconds",
]

# ---------------------------------------------------------------------------
# Fixed bounds. Every one of these is a deliberate ceiling, not a tuned default: past it, a
# scan or a probe stops doing more work and instead records what it left out, so a caller can
# always tell "this tree has nothing to build" apart from "the walk gave up early".
# ---------------------------------------------------------------------------

#: How many install/build/test commands a probe's evidence trail may quote. Commands are
#: reported so a human can see what actually ran, not so the run can be replayed, so this
#: stays a small number regardless of how many phases and projects a repository has.
MAX_COMMANDS = 20

#: Seconds one command is allowed before it is killed, subject to the project's own share of
#: `DEFAULT_BUDGET_SECONDS` and to whatever the whole run has left.
DEFAULT_PHASE_TIMEOUT = 900

#: Seconds the ENTIRE probe -- every project, every phase -- is allowed to spend. This is the
#: number that actually bounds a run; a per-phase timeout alone would not, since a tree with
#: two dozen projects and five phases apiece could otherwise run for the better part of a day.
DEFAULT_BUDGET_SECONDS = 1800

#: Below this many seconds a command has no realistic chance of doing useful work before being
#: killed, so it is never started -- the phase is recorded as skipped instead.
MIN_PHASE_SECONDS = 15

#: The smallest slice of the run a project is ever handed, even when its proportional share of
#: what is left would be smaller: a project that arrives late in the queue still gets a real
#: attempt rather than a token few seconds guaranteed to time out.
MIN_PROJECT_SECONDS = 30

#: How many directory levels below the repository root the walk will descend.
MAX_DEPTH = 4

#: How many directories the walk will visit in total before it stops and records the rest as
#: unreached, so a tree with an enormous, unpruned subtree cannot turn discovery into an
#: unbounded scan.
MAX_DIRS = 4000

#: How many project roots discovery will keep. Past this a tree reads less like a handful of
#: projects and more like a dump of vendored or generated trees the ignore list did not catch.
MAX_PROJECTS = 24

#: Of the projects discovery keeps, how many are actually handed a probe, largest first. The
#: rest are still counted and named in the report -- discovery is a directory walk and cheap;
#: probing runs somebody's install script and is not.
MAX_PROBED_PROJECTS = 8

# ---------------------------------------------------------------------------
# Detection tables. These describe how each ecosystem's own tooling recognises a project --
# the manifest filenames, the workspace declaration syntax -- so they are pinned data rather
# than something inferred from any one repository.
# ---------------------------------------------------------------------------

#: A Python project can be declared by any of these; there is no one canonical manifest name
#: the way there is for Node or Rust.
_PYTHON_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "tox.ini",
    "noxfile.py",
    "environment.yml",
)

#: Ecosystem name -> the manifest filenames that mark a directory as one of its projects. A
#: directory can match more than one entry here, and when it does every matching ecosystem
#: gets its own `Project` -- nothing here picks a single "winner" for a directory.
_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("node", ("package.json",)),
    ("python", _PYTHON_MARKERS),
    ("go", ("go.mod",)),
    ("maven", ("pom.xml",)),
    ("gradle", ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")),
    ("rust", ("Cargo.toml",)),
    ("ruby", ("Gemfile",)),
    ("php", ("composer.json",)),
)

#: .NET has no single manifest filename either; any of these project/solution file suffixes
#: marks the directory as a .NET project, checked separately from the table above.
_DOTNET_SUFFIXES = (".sln", ".csproj", ".fsproj", ".vbproj")

#: Directory names the walk never opens: package caches, editor and IDE state, build output,
#: vendored or third-party trees, and every language's own dependency directory. Matched by
#: exact name at any depth, so `vendor` prunes `services/api/vendor` just as it prunes a
#: top-level one.
_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        "vendor",
        "dist",
        "build",
        "target",
        "out",
        "bin",
        "obj",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".tox",
        ".nox",
        ".gradle",
        ".idea",
        ".vscode",
        ".terraform",
        "third_party",
        "Pods",
        "coverage",
        ".next",
        ".turbo",
        ".pnpm-store",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "Carthage",
        "DerivedData",
        ".dart_tool",
        "elm-stuff",
    }
)

#: A project under a directory named one of these ships beside the repository's real product
#: rather than being it -- a docs site, a folder of usage samples, a benchmark harness. Such a
#: project is still discovered and, later, still probed and reported like any other; it is
#: only excluded from deciding whether the REPOSITORY as a whole builds. Matched on a whole
#: path segment, never a substring, so a project literally named "docsgen" is not caught by
#: looking for "docs" inside its name.
#:
#: The list stays short and deliberate: `scripts/`, `tools/`, `contrib/` and any end-to-end or
#: integration test tree are left OUT on purpose, because folding a real component's failure
#: into "ancillary" would let a tree that does not actually build read as one that does, which
#: is a far worse mistake than the reverse.
ANCILLARY_DIRS = frozenset(
    {
        "docs",
        "doc",
        "documentation",
        "website",
        "site",
        "www",
        "examples",
        "example",
        "samples",
        "sample",
        "demo",
        "demos",
        "benchmark",
        "benchmarks",
        "bench",
        "playground",
        "sandbox",
    }
)

#: Ecosystems whose usual tooling can also emit a coverage percentage. A project in one of
#: these gets a small preference when only `MAX_PROBED_PROJECTS` slots are available, on the
#: theory that among similarly sized candidates the one that can hand back a coverage number
#: is worth the slot slightly more than one that cannot.
_COVERAGE_CAPABLE = frozenset({"python", "node", "go"})
_COVERAGE_WEIGHT_BONUS = 1.25

#: Coarse per-project and per-thousand-files seconds used only to print an ESTIMATE of how
#: long a run at a given level will take, fitted loosely to runs observed elsewhere. Never a
#: promise, and callers are expected to label it as an estimate wherever it is shown.
_ESTIMATE_PER_PROJECT = {"discover": 30.0, "full": 90.0}
_ESTIMATE_PER_KFILE = {"discover": 4.0, "full": 20.0}


def _read_text(path: Path, cap: int = 200_000) -> str:
    """Best-effort read of a small text file; any I/O problem is silently empty text."""
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _read_json(path: Path):
    """Best-effort read of a small JSON file; a missing or malformed file is `None`, not raised."""
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def _rel(repo: Path, path: Path) -> str:
    """`path` written relative to `repo`, as a forward-slash string, `"."` for the root itself."""
    try:
        rel = path.relative_to(repo).as_posix()
    except ValueError:
        return path.name
    return rel or "."


class Budget:
    """One monotonic clock shared by everything a probe runs against a repository.

    `total_seconds` is the whole run's allowance; `phase_cap` is the most any single command
    may take. `for_project()` turns a project's proportional share of what remains into an
    absolute deadline, and `for_phase()` turns that deadline into how many seconds the next
    command may run for -- the smallest of the phase cap, what is left of the project's slice,
    and what is left of the run as a whole. Because the arithmetic is always against a clock
    that only moves forward, a project that returns time early (by failing fast or finishing
    ahead of its slice) simply leaves more of `remaining()` for whichever project probes next;
    nothing is pre-allocated and locked away.
    """

    def __init__(self, total_seconds: float, phase_cap: int = DEFAULT_PHASE_TIMEOUT) -> None:
        self.total = max(0.0, float(total_seconds))
        self.phase_cap = max(1, int(phase_cap))
        self._started_at = time.monotonic()
        self._deadline = self._started_at + self.total

    def remaining(self) -> float:
        """Seconds left in the whole run, never negative even long after the deadline passed."""
        return max(0.0, self._deadline - time.monotonic())

    def elapsed(self) -> float:
        """Seconds since this budget was created, for reporting how much of it was spent."""
        return time.monotonic() - self._started_at

    @property
    def exhausted(self) -> bool:
        """True once what is left could not usefully start even one more command."""
        return self.remaining() < MIN_PHASE_SECONDS

    def for_project(self, n_left: float) -> float:
        """An absolute deadline (a `time.monotonic()` instant) for one project's turn.

        `n_left` is how many projects' worth of weight are still queued, this one counted as
        one unit of it -- so a project holding a third of the remaining weight passes 3.0 here
        and receives roughly a third of what is left. The share is floored at the smaller of
        `MIN_PROJECT_SECONDS` and what actually remains, so a project queued near the end of a
        long run is not handed a slice too thin to attempt anything in.
        """
        left = self.remaining()
        share = left / max(1.0, float(n_left))
        floor = min(MIN_PROJECT_SECONDS, left)
        return time.monotonic() + max(share, floor)

    def for_phase(self, project_share: float) -> int:
        """Seconds the next command may run for, given its project's deadline.

        The smallest of three ceilings: the fixed per-command cap, whatever is left before
        `project_share` (the project's own deadline) arrives, and whatever is left of the run
        as a whole. When that smallest number would not clear `MIN_PHASE_SECONDS`, 0 is
        returned -- the caller's job is then to record the phase as skipped rather than start
        a command it cannot finish.
        """
        left = min(self.remaining(), project_share - time.monotonic())
        seconds = int(min(self.phase_cap, left))
        return seconds if seconds >= MIN_PHASE_SECONDS else 0


@dataclass
class Project:
    """One buildable thing discovery found: a manifest, the ecosystem it names, and its size.

    A single directory that declares more than one ecosystem -- a `package.json` sitting next
    to a `Cargo.toml` -- yields one `Project` per ecosystem, never a merged or a chosen one.
    `weight` counts files seen under this root during the scan; it exists purely to let a
    caller spend its probe budget on the project that is actually most of the repository
    rather than on whichever manifest happened to sort first, and it is never part of any
    emitted measurement.
    """

    project_id: str
    root: Path
    rel: str
    ecosystem: str
    members: list[str] = field(default_factory=list)
    workspace: bool = False
    required: bool = True
    weight: int = 1


def is_ancillary(rel: str) -> bool:
    """Does this project live under a directory that ships alongside the product, not as it?

    Checked against whole path segments of `rel`, case-insensitively; the repository root
    (`"."`) can never itself be ancillary, no matter what the checkout happens to be called.
    """
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    return any(part.lower() in ANCILLARY_DIRS for part in parts)


def scan(repo: Path) -> tuple[dict[Path, set[str]], dict]:
    """Walk `repo` and return every directory holding a manifest, plus what the walk skipped.

    The walk is iterative and bounded by `MAX_DEPTH` and `MAX_DIRS`; anything past either
    bound, along with any directory the process could not read, is recorded in the report
    rather than silently dropped. Every candidate directory is re-resolved and checked against
    the repository's own resolved root before it is queued, so a symlink that points outside
    the checkout cannot make later phases run commands somewhere else on disk. Symlinked
    entries are otherwise never followed: only real directories and files are looked at.
    """
    repo_real = repo.resolve()
    found: dict[Path, set[str]] = {}
    omitted: list[dict] = []
    files_per_dir: dict[Path, int] = {}
    seen_dirs = 0
    stack: list[tuple[Path, int]] = [(repo, 0)]
    while stack:
        current, depth = stack.pop()
        seen_dirs += 1
        if seen_dirs > MAX_DIRS:
            omitted.append(
                {"root": "<scan>", "reason_code": "directory_budget_exhausted", "ecosystems": []}
            )
            break
        try:
            entries = list(os.scandir(current))
        except OSError:
            omitted.append(
                {
                    "root": _rel(repo, current),
                    "reason_code": "unreadable_directory",
                    "ecosystems": [],
                }
            )
            continue
        names: set[str] = set()
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = Path(entry.path)
                    if entry.name in _IGNORE_DIRS or entry.name.startswith("."):
                        continue
                    if depth + 1 > MAX_DEPTH:
                        omitted.append(
                            {
                                "root": _rel(repo, child),
                                "reason_code": "depth_limit",
                                "ecosystems": peek_ecosystems(child),
                            }
                        )
                        continue
                    if not child.resolve().is_relative_to(repo_real):
                        omitted.append(
                            {
                                "root": _rel(repo, child),
                                "reason_code": "symlink_escapes_repository",
                                "ecosystems": [],
                            }
                        )
                        continue
                    stack.append((child, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    names.add(entry.name)
            except OSError:
                continue
        files_per_dir[current] = len(names)
        ecosystems = {eco for eco, markers in _MARKERS if names & set(markers)}
        if any(n.endswith(_DOTNET_SUFFIXES) for n in names):
            ecosystems.add("dotnet")
        if ecosystems:
            found[current] = ecosystems
    return found, {
        "dirs_scanned": min(seen_dirs, MAX_DIRS),
        "omitted": omitted,
        "files_scanned": sum(files_per_dir.values()),
        "files_per_dir": files_per_dir,
    }


def peek_ecosystems(path: Path) -> list[str]:
    """Which ecosystems `path` declares, without descending into it.

    Used to describe a directory the walk decided NOT to enter (past the depth limit, for
    instance) so that it is reported as, say, "a Go module we did not reach" rather than
    folded into a generic "nothing found here" -- the two read very differently to whoever
    consumes the discovery report.
    """
    try:
        names = {e.name for e in os.scandir(path) if e.is_file(follow_symlinks=False)}
    except OSError:
        return []
    found = sorted(eco for eco, markers in _MARKERS if names & set(markers))
    if any(n.endswith(_DOTNET_SUFFIXES) for n in names):
        found.append("dotnet")
    return found


def node_workspace_globs(root: Path) -> list[str]:
    """The workspace member globs a Node root declares, across its three common spellings.

    Checks `package.json`'s `workspaces` field (either the array form or the
    `{"packages": [...]}` form) and, separately, a `pnpm-workspace.yaml` file. The pnpm file is
    read with a small regex rather than a YAML parser -- pulling in a dependency just to read
    one list under a `packages:` key is not worth it -- and a `pnpm-workspace.yaml` present
    with no list entries at all is treated as covering everything beneath it, which is the
    safer of the two possible misreadings.
    """
    pkg = _read_json(root / "package.json")
    globs: list[str] = []
    if isinstance(pkg, dict):
        workspaces = pkg.get("workspaces")
        if isinstance(workspaces, list):
            globs += [g for g in workspaces if isinstance(g, str)]
        elif isinstance(workspaces, dict) and isinstance(workspaces.get("packages"), list):
            globs += [g for g in workspaces["packages"] if isinstance(g, str)]
    pnpm_file = root / "pnpm-workspace.yaml"
    if pnpm_file.is_file():
        body = _read_text(pnpm_file, 20_000)
        entries = re.findall(r"^\s*-\s*['\"]?([^'\"\n]+)['\"]?\s*$", body, re.M)
        globs += [e.strip() for e in entries if e.strip()]
        if not entries:
            globs.append("**")
    return globs


def cargo_workspace(root: Path) -> tuple[list[str], list[str]] | None:
    """Return `(members, exclude)` if `root`'s `Cargo.toml` has a `[workspace]` table, else `None`.

    Read with plain regexes rather than a TOML parser: this file may be the only reason the
    directory was looked at, and a full parser would make discovery fail outright on a syntax
    quirk that cargo itself tolerates.
    """
    body = _read_text(root / "Cargo.toml", 200_000)
    if not re.search(r"^\s*\[workspace\]", body, re.M):
        return None

    def _array(key: str) -> list[str]:
        match = re.search(rf"^\s*{key}\s*=\s*\[(.*?)\]", body, re.M | re.S)
        return re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)) if match else []

    return _array("members"), _array("exclude")


def _matches_any(rel: str, globs: list[str]) -> bool:
    """Does `rel` (a path relative to a workspace root) fall under any of `globs`?"""
    for pattern in globs:
        pattern = pattern.strip().rstrip("/")
        if not pattern:
            continue
        if pattern in ("**", "*"):
            return True
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern + "/*") or rel == pattern:
            return True
        if pattern.endswith("/*") and rel.startswith(pattern[:-2] + "/"):
            return True
    return False


def authoritative_roots(
    repo: Path, found: dict[Path, set[str]]
) -> dict[str, dict[Path, list[str]]]:
    """Per ecosystem, which discovered roots claim their subtree with one command, and how much.

    This is the only place discovery ever suppresses a root in favour of another: a directory
    named by an npm/pnpm workspace's globs, a Cargo workspace's `members`, a Go `go.work`, a
    multi-module Maven `pom.xml`, a Gradle build with a `settings.gradle(.kts)`, or a `.sln`'s
    project list is folded into the covering root instead of being probed separately. Two
    ecosystems found in the same directory are never folded into each other -- only a root's
    OWN ecosystem can claim its descendants.
    """
    covers: dict[str, dict[Path, list[str]]] = {eco: {} for eco, _ in _MARKERS}
    covers["dotnet"] = {}
    for root, ecosystems in found.items():
        if "node" in ecosystems:
            globs = node_workspace_globs(root)
            if globs:
                covers["node"][root] = globs
        if "rust" in ecosystems:
            workspace = cargo_workspace(root)
            if workspace is not None:
                members, exclude = workspace
                covers["rust"][root] = members or ["**"]
                covers["rust"][root] = [m for m in covers["rust"][root] if m not in exclude]
        if "go" in ecosystems or (root / "go.work").is_file():
            if (root / "go.work").is_file():
                covers["go"][root] = ["**"]
        if "maven" in ecosystems and re.search(r"<modules>", _read_text(root / "pom.xml", 200_000)):
            covers["maven"][root] = ["**"]
        if "gradle" in ecosystems and any(
            (root / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")
        ):
            covers["gradle"][root] = ["**"]
        if "dotnet" in ecosystems and next(root.glob("*.sln"), None) is not None:
            covers["dotnet"][root] = ["**"]
    return covers


def _subtree_files(root: Path, files_per_dir: dict[Path, int]) -> int:
    """Files the walk counted at or beneath `root`, at least 1 so a project never sorts as empty."""
    prefix = root.as_posix().rstrip("/") + "/"
    total = 0
    for directory, count in files_per_dir.items():
        path = directory.as_posix()
        if directory == root or path.startswith(prefix):
            total += count
    return max(1, total)


def discover_projects(repo: Path) -> tuple[list[Project], dict]:
    """Every project root in `repo`, largest-scanning-order first, plus what could not be reached.

    Discovered roots are visited shallowest and then alphabetically first, so the assignment
    of `project_id` and the `MAX_PROJECTS` cap are both stable across runs of the same tree.
    A root a workspace already claims contributes its relative path to that workspace
    project's `members` instead of becoming a project of its own; a root claimed by an
    ecosystem with no probed project for it yet is recorded as `ambiguous_roots` rather than
    silently dropped. When every discovered project turns out to sit under an ancillary
    directory, the ancillary flag is lifted from all of them -- a checkout that is entirely
    examples or documentation has nothing to demote it relative to.
    """
    found, scan_report = scan(repo)
    covers = authoritative_roots(repo, found)
    projects: list[Project] = []
    omitted: list[dict] = list(scan_report["omitted"])
    ambiguous: list[dict] = []

    ordered = sorted(found.items(), key=lambda kv: (len(kv[0].parts), kv[0].as_posix()))
    for root, ecosystems in ordered:
        rel = _rel(repo, root)
        for eco in sorted(ecosystems):
            owner = None
            for wroot, globs in covers.get(eco, {}).items():
                if wroot == root:
                    continue
                try:
                    inner = root.relative_to(wroot).as_posix()
                except ValueError:
                    continue
                if _matches_any(inner, globs):
                    owner = wroot
                    break
            if owner is not None:
                for existing in projects:
                    if existing.root == owner and existing.ecosystem == eco:
                        existing.members.append(rel)
                        break
                else:
                    ambiguous.append(
                        {
                            "root": rel,
                            "ecosystems": [eco],
                            "reason_code": "workspace_root_not_probed",
                        }
                    )
                continue
            if len(projects) >= MAX_PROJECTS:
                omitted.append(
                    {"root": rel, "ecosystems": [eco], "reason_code": "project_budget_exhausted"}
                )
                continue
            projects.append(
                Project(
                    project_id=f"p{len(projects) + 1}",
                    root=root,
                    rel=rel,
                    ecosystem=eco,
                    workspace=root in covers.get(eco, {}),
                    weight=_subtree_files(root, scan_report["files_per_dir"]),
                    required=not is_ancillary(rel),
                )
            )

    if projects and not any(p.required for p in projects):
        for project in projects:
            project.required = True

    unreachable = sorted(
        {eco for gap in omitted + ambiguous for eco in gap.get("ecosystems") or []}
    )
    report = {
        "dirs_scanned": scan_report["dirs_scanned"],
        "files_scanned": scan_report["files_scanned"],
        "n_projects": len(projects),
        "ecosystems": sorted({p.ecosystem for p in projects}),
        "omitted_roots": omitted[:50],
        "ambiguous_roots": ambiguous[:50],
        "unreachable_ecosystems": unreachable,
        "truncated": len(projects) >= MAX_PROJECTS
        or bool([o for o in omitted if o["reason_code"] == "directory_budget_exhausted"]),
        "no_manifest": not projects and not omitted and not ambiguous,
    }
    return projects, report


def _allocate(projects: list[Project], max_projects: int) -> tuple[list[Project], list[Project]]:
    """Split discovered projects into the ones that get probed and the ones that do not.

    Ranked largest weight first, a small bonus applied to ecosystems that can also produce a
    coverage number, and workspace roots preferred over non-workspace ones of the same size --
    one command at a workspace root already covers its members, so it buys more measurement
    per second spent than a same-sized standalone project would.
    """

    def rank(project: Project) -> tuple:
        weight = project.weight * (
            _COVERAGE_WEIGHT_BONUS if project.ecosystem in _COVERAGE_CAPABLE else 1.0
        )
        return (-weight, not project.workspace, project.rel)

    ordered = sorted(projects, key=rank)
    cap = max(1, max_projects)
    return ordered[:cap], ordered[cap:]


def survey(repo: Path, max_projects: int = MAX_PROBED_PROJECTS) -> dict:
    """A cheap, read-only look at what probing `repo` would involve, before anything runs.

    One bounded directory walk, no manifest parsed beyond what discovery already reads, and no
    subprocess started. It exists so a caller can be told what a run is about to cost -- how
    many projects, which ecosystems, how much tree -- before that run starts spending its
    budget, which is the difference between a long probe and one that merely looks hung.
    """
    projects, report = discover_projects(repo)
    probe_list, over_cap = _allocate(projects, max_projects)
    return {
        "n_projects": report["n_projects"],
        "n_projects_probed": len(probe_list),
        "n_projects_over_cap": len(over_cap),
        "ecosystems": report["ecosystems"],
        "files_scanned": report["files_scanned"],
        "dirs_scanned": report["dirs_scanned"],
    }


def estimate_seconds(survey_report: dict, level: str) -> float:
    """A rough seconds estimate for a probe at `level`, built from a `survey()` result.

    `"none"` and any unrecognised level cost nothing, since nothing would run. This is always a
    guess, not a bound -- a cold dependency cache or one unusually slow suite can beat it in
    either direction -- and a caller should present it as one.
    """
    if level not in ("discover", "full"):
        return 0.0
    projects = max(0, int(survey_report.get("n_projects_probed") or 0))
    kfiles = max(0.0, float(survey_report.get("files_scanned") or 0) / 1000.0)
    return (_ESTIMATE_PER_PROJECT[level] * projects) + (_ESTIMATE_PER_KFILE[level] * kfiles)
