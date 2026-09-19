"""The command line: validate every path, measure each repository, say what happened.

THE ORDER IS THE POINT. Everything that can be refused is refused before the first lane
starts: a path that is not a directory, a directory that is not a git repository, an
`--all` that selects nothing, an `--out` inside a measured tree. A run that cannot finish
correctly should cost a second, not twenty minutes and a half-written output directory -- and
`tree.collect` on a non-repository answers with an empty block rather than an error, so
"is this a repository" is a question this layer has to ask, not one it can rely on a
collector to raise.

MANY REPOSITORIES, ONE INVOCATION. `--jobs` here is how many REPOSITORIES run at once;
inside one repository the lanes are already concurrent, and each repository gets its own
`LaneClock` and its own `Deadline`, so the time each one takes is reported separately. A
repository whose measurement raises is reported on its own line and the rest still run:
the alternative is a firm pointing this at 147 repositories and losing the other 146 to
the first broken one.

WHAT IS PRINTED, AND WHERE. Results go to stdout -- the summary line, the review, what
was written -- and progress goes to stderr: the plan, the lane clock, the failures. So
`hazina-scan ... > report.txt` keeps the answer and leaves the narration on the terminal.

NOTHING IS SENT ANYWHERE. This tool opens no network connection; the files it writes are
on the operator's own disk and the review printed at the end says exactly what is in
them, before they decide whether to share them.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
import threading
import zipfile
from pathlib import Path

from . import __version__, env, orchestrator, report, schema

__all__ = ["main"]

#: Where the outputs land when the operator does not say. A relative default on purpose:
#: the tool writes into the working directory it was run from, never into the repository
#: it is measuring.
DEFAULT_OUT = "./hazina-out"

#: The archive is always called this, beside the output directory rather than inside it
#: -- an archive that contains itself is a race, and a fixed name is what a second
#: command (an upload, an attachment) can be written against without knowing the flags
#: the scan was run with.
ZIP_NAME = "hazina-out.zip"

#: How many repositories run at once by default. Two rather than one because the lanes
#: inside a repository are I/O-bound and leave a machine idle; two rather than eight
#: because each repository is itself a fan-out and oversubscribing makes every one of
#: them slower.
DEFAULT_JOBS = 2

#: Said once per invocation, before the first build check starts, and never repeated per
#: repository -- it describes what this command is about to do, not what one tree is like.
#: It goes out ahead of the work rather than in the help text alone, because the person who
#: typed the command is watching the terminal and may not have read the help at all.
BUILD_WARNING = (
    "[build] the build check executes THIS REPOSITORY'S OWN commands: it installs the "
    "dependencies its manifests declare, runs its build, lists its tests and runs them. "
    "Doing that MODIFIES THE CHECKOUT -- lockfiles, dependency directories, build output "
    "-- so point this at a disposable clone rather than at a tree you are working in. "
    "Pass --no-build to measure without executing anything."
)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hazina-scan",
        description="Measure a git repository and write three numbers-only files. "
        "Deterministic, local, and nothing is sent anywhere.",
    )
    parser.add_argument(
        "repos",
        nargs="*",
        metavar="REPO",
        help="Git repositories to measure. Combine freely with --all.",
    )
    parser.add_argument(
        "--all",
        dest="all_dir",
        metavar="DIR",
        default=None,
        help="Measure every immediate subdirectory of DIR that contains a .git.",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        metavar="DIR",
        help=f"Output directory, created if absent (default {DEFAULT_OUT}). Each "
        f"repository's files land in <out>/<its anonymous handle>/ -- named from the "
        f"tree's content, never from this machine's directory for it; two "
        f"repositories with identical trees are suffixed -2, -3. See "
        f"INDEX.local.txt, written in <out> after the run, for the map back to "
        f"where each folder came from.",
    )
    parser.add_argument(
        "--build",
        choices=("none", "discover", "full"),
        default="full",
        help="How much of the build check to run (default full). EXECUTES THE "
        "REPOSITORY'S OWN COMMANDS and modifies the checkout, so use a disposable "
        "clone: discover resolves dependencies, builds and lists the tests; full also "
        "runs the suite and reads coverage back; none executes nothing.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Same as --build none: measure without executing anything of the repository's own.",
    )
    parser.add_argument(
        "--budget-seconds",
        type=int,
        default=orchestrator.DEFAULT_BUDGET_SECONDS,
        dest="budget",
        help=f"Wall-clock budget for ONE repository (default "
        f"{orchestrator.DEFAULT_BUDGET_SECONDS}). The git-backed lanes and the build "
        f"check draw their ceilings from it and are enforced; the in-process readers are "
        f"bounded by their own file and size caps instead. Work the budget does not reach "
        f"is reported null with a reason rather than as a low number.",
    )
    parser.add_argument(
        "--build-budget-seconds",
        type=int,
        default=orchestrator.DEFAULT_BUILD_BUDGET_SECONDS,
        dest="build_budget",
        help=f"The build check's reserved share of that budget (default "
        f"{orchestrator.DEFAULT_BUILD_BUDGET_SECONDS}).",
    )
    parser.add_argument(
        "--full-attempt-seconds",
        type=int,
        default=orchestrator.DEFAULT_FULL_ATTEMPT_SECONDS,
        dest="full_attempt_seconds",
        help=f"How long a --build full attempt may run before the measurement is "
        f"completed at the cheaper level (default "
        f"{orchestrator.DEFAULT_FULL_ATTEMPT_SECONDS}).",
    )
    parser.add_argument(
        "--timeout-build",
        type=int,
        default=orchestrator.DEFAULT_TIMEOUT_BUILD,
        dest="timeout_build",
        help=f"Ceiling for ONE command inside the build check (default "
        f"{orchestrator.DEFAULT_TIMEOUT_BUILD}), always subordinate to the budgets.",
    )
    parser.add_argument(
        "--max-build-projects",
        type=int,
        default=orchestrator.DEFAULT_MAX_BUILD_PROJECTS,
        dest="max_build_projects",
        help=f"How many project roots inside one repository the build check may reach "
        f"(default {orchestrator.DEFAULT_MAX_BUILD_PROJECTS}).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"How many REPOSITORIES to measure at once (default {DEFAULT_JOBS}). The "
        f"lanes within one repository are already concurrent.",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Print every emitted field and its value, not just the per-kind counts.",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help=f"Do not write {ZIP_NAME}. One archive of this run's handle folders is "
        f"written beside the output directory by default -- for a single "
        f"repository as much as for several.",
    )
    parser.add_argument("--version", action="version", version=f"hazina-scan {__version__}")
    return parser


# ---------------------------------------------------------------------------
# Choosing the repositories, and refusing the ones that are not
# ---------------------------------------------------------------------------


def is_git_repo(path: Path) -> bool:
    """A directory git itself agrees is a repository.

    Asked at all because the collectors do not: `tree.collect` answers an empty block for
    a non-repository rather than raising, and an empty block written out as three files
    is a measurement of nothing that looks exactly like a measurement.

    git is asked first, so a worktree (whose `.git` is a FILE) and a bare checkout (which
    has no such entry) both answer yes. But a git directory alone is not enough: every
    SUBDIRECTORY of a repository also has one, and measuring `myrepo/src` would pair a
    subtree's files with the whole repository's history and call the pair a measurement.
    So the path must also be the top of that repository -- it holds the `.git` entry, or
    it IS the git directory, which is what a bare checkout looks like.
    """
    if not path.is_dir():
        return False
    git_dir = env.run_git(path, "rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    if (path / ".git").exists():
        return True
    return (path / git_dir).resolve() == path.resolve()


def select_repos(args, err) -> list[Path] | None:
    """Every repository this invocation should measure, or None when it cannot run.

    `None` is a usage error that has already been explained on `err`. Validation is
    total and happens here, before a single lane starts: a list of fifty repositories
    with a typo in the fortieth should fail in a second, not in an hour.
    """
    chosen: list[Path] = []

    if args.all_dir is not None:
        root = Path(args.all_dir).expanduser().resolve()
        if not root.is_dir():
            print(f"error: not a directory: {root}", file=err)
            return None
        found = sorted((p for p in root.iterdir() if is_git_repo(p)), key=lambda p: p.name)
        if not found:
            print(f"error: no git repositories in the immediate subdirectories of {root}", file=err)
            return None
        chosen.extend(found)

    for raw in args.repos:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            print(f"error: not a directory: {path}", file=err)
            return None
        if not is_git_repo(path):
            print(f"error: not a git repository: {path}", file=err)
            return None
        chosen.append(path)

    # `--all code` plus `code/api` names the same checkout twice. Measuring it twice would
    # spend the time twice and write the identical answer to `api` and `api-2`, which then
    # reads as two repositories in the zip. The first mention wins; the order is kept.
    chosen = list(dict.fromkeys(chosen))

    if not chosen:
        print("error: no repositories given; name one or more paths, or pass --all DIR", file=err)
        return None
    return chosen


def _repo_containing(path: Path, repos: list[Path]) -> Path | None:
    """Which selected repository, if any, `path` sits at or inside.

    Writing into a measured tree changes what the next run measures: the content digest
    covers every file, so a second scan of an otherwise untouched repository would disagree
    with the first. Used both before the run, on `out_dir` itself, and at write time, on
    each handle folder this run is about to create.
    """
    for repo in repos:
        if path.is_relative_to(repo):
            return repo
    return None


def local_labels(repos: list[Path]) -> list[str]:
    """One label per repository, in order, for the terminal only -- never for a file path.

    This is the directory name each repository sits in on THIS machine, so an operator
    reading the terminal can tell two lines apart; it plays no part any more in choosing
    where a repository's files are written, which is why a repeat is suffixed here rather
    than left to collide -- two repositories called `api` in different parents are an
    ordinary thing for a firm to have. The suffix goes on the LATER one, so the first
    repository named on the command line keeps the plain label.
    """
    labels: list[str] = []
    seen: dict[str, int] = {}
    for repo in repos:
        base = repo.name or "repo"
        seen[base] = seen.get(base, 0) + 1
        labels.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return labels


class OutputClashError(Exception):
    """This repository's handle folder would land at or inside a repository being measured.

    Handles are derived from tree content, so this is not the everyday hazard the old
    directory-name check guarded against -- it takes a content collision between the
    handle and an actual repository path, which is not a thing a real digest produces in
    practice. It is still checked, at the point the folder is about to be created, so the
    remote chance of it is a reported failure for that one repository rather than a write
    into a tree the next run would then measure differently.
    """


class _HandleAllocator:
    """Turns a repository's content handle into a folder name unique within this run.

    Two repositories can share a handle when their trees are byte-for-byte identical -- the
    digest has no notion of where either one is checked out. This keeps one claim count per
    handle behind a lock, so of two repositories finishing with the same handle, whichever
    claims it first keeps the plain form and the next gets `-2`, and so on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, int] = {}

    def claim(self, base: str) -> str:
        with self._lock:
            self._claims[base] = self._claims.get(base, 0) + 1
            n = self._claims[base]
        return base if n == 1 else f"{base}-{n}"


def _diagnostic_handle(repo: Path, label: str) -> str:
    """A handle to show for a repository whose measurement never produced one.

    The terminal line and the index row still need something in the handle column even
    when `orchestrator.measure` raised before returning a row. The content digest is cheap
    and does not depend on the measurement that failed, so it is tried first and gives the
    same `repo-<hex>` a successful run would have shown; if even that cannot be read, the
    local label stands in instead, so two such failures in one run still print two
    distinguishable lines.
    """
    try:
        return f"repo-{orchestrator.repo_digest(repo)[:12]}"
    except Exception:  # noqa: BLE001 -- best effort only, a display fallback follows
        return f"FAILED-{label}"


# ---------------------------------------------------------------------------
# One repository
# ---------------------------------------------------------------------------


class _Printer:
    """Serialises whole blocks of output across the repository threads.

    Each repository's results are assembled in full and then emitted under one lock, so
    two repositories finishing together produce two readable blocks rather than one
    interleaved one. The plan is printed live instead of held back -- it exists to be
    read BEFORE the wait, and an estimate that arrives with the answer is not an estimate.

    The lanes' own `[lane] ...` heartbeat comes from `LaneClock` and goes straight to
    stderr, so it still interleaves across repositories. That is the right trade: it is
    the only thing proving a long run is alive, and holding it back to keep it tidy would
    make it useless.
    """

    def __init__(self, prefix: bool) -> None:
        self._lock = threading.Lock()
        self._prefix = prefix
        self._said: set[str] = set()

    def label(self, name: str) -> str:
        """The prefix a lane clock should stamp on its own lines, or "" for one repo.

        The clock prints from inside the orchestrator and cannot go through `emit`, so it
        is handed the same name this printer would use and does the stamping itself.
        """
        return name if self._prefix else ""

    def tag(self, name: str, text: str) -> str:
        if not self._prefix:
            return text
        return "\n".join(f"{name}: {line}" for line in text.splitlines())

    def emit(self, name: str, blocks: list[tuple[object, str]]) -> None:
        """Print one repository's whole report, in order, without another's cutting in.

        The blocks alternate between the two streams on purpose -- the verdict, then
        where the time went, then what is in the files -- and the lock is held across all
        of them so that order survives two repositories finishing at once.
        """
        with self._lock:
            for stream, text in blocks:
                if not text:
                    continue
                if stream is sys.stderr:
                    print(self.tag(name, text), file=sys.stderr, flush=True)
                else:
                    print(text, flush=True)

    def progress(self, name: str, text: str) -> None:
        with self._lock:
            print(self.tag(name, text), file=sys.stderr, flush=True)

    def once(self, text: str) -> None:
        """Say something about the INVOCATION, exactly once, whoever gets there first.

        Unlabelled on purpose: a warning about what this command is about to do to every
        repository it was given is not a fact about whichever of them happened to reach it
        first, and stamping a folder name on it would read as though it were.
        """
        with self._lock:
            if text in self._said:
                return
            self._said.add(text)
            print(text, file=sys.stderr, flush=True)


def measure_one(
    repo: Path,
    name: str,
    args,
    out_dir: Path,
    printer: _Printer,
    repos: list[Path],
    allocator: _HandleAllocator,
) -> dict:
    """Measure one repository, write its three files under its handle, and say what came out.

    Returns `{"name", "handle", "repo", "status", "error"}` and RAISES NOTHING. The whole
    body is inside one `try`, not just the call to `measure`: a full disk in
    `write_outputs`, an undeclared field in `review`, a broken pipe on the way to the
    terminal -- from the other repositories' point of view those are the same event as a
    collector raising, and any one of them escaping would take the remaining repositories,
    the per-repository status lines and the archive with it. The orchestrator is right to
    let a broken collector stop its OWN run; this layer is the one that knows there are
    others waiting.

    `name` is the LOCAL label, used only for the stderr prefix and the progress lines --
    `handle` is what the output folder and the zip entries are named after, and it is not
    known until `measure` returns a row.

    The failure is reported on stderr as `FAILED <name>: <class>: <message>` and returned,
    so the caller can exit 1 deliberately rather than by accident.
    """
    clock = orchestrator.LaneClock(label=printer.label(name))
    deadline = orchestrator.Deadline(args.budget)
    handle: str | None = None
    try:
        for line in report.plan_lines(repo, args.build_level, args.budget, args.max_build_projects):
            printer.progress(name, line)
        # Ahead of the lane that executes anything, and ahead of it for EVERY repository in
        # the run, since the first one to arrive here is about to start installing.
        if args.build_level != "none":
            printer.once(BUILD_WARNING)

        row, measurement = orchestrator.measure(
            repo,
            build_level=args.build_level,
            jobs=0,
            clock=clock,
            deadline=deadline,
            build_budget=args.build_budget,
            timeout_build=args.timeout_build,
            max_build_projects=args.max_build_projects,
            full_attempt_seconds=args.full_attempt_seconds,
        )
        handle = allocator.claim(row["fake_repo_name"])
        target = out_dir / handle
        blocker = _repo_containing(target, repos)
        if blocker is not None:
            raise OutputClashError(
                f"{target} is at or inside the repository being measured, {blocker}"
            )
        written = orchestrator.write_outputs(target, row, measurement)

        timing = [
            report.timing(clock),
            f"  budget: {deadline.elapsed():.0f}s of {deadline.total}s used",
        ]
        if row.get("status") != "measured":
            timing.append(
                f"WARNING: this run is PARTIAL ({row.get('skip_reason')}); the "
                f"lanes that did not run report NULL, not zero."
            )

        # These files belong to the person who ran the tool and have gone nowhere yet.
        # Print their contents so that decision can be made with the facts in hand.
        review = [
            schema.review(
                {"codebase_repos.json": row, "measurement.json": measurement}, full=args.review
            )
        ]
        if not args.review:
            review.append(
                "Run again with --review to see every field and its value "
                "before you share these files."
            )

        printer.emit(
            name,
            [
                (sys.stdout, report.summary_line(row, measurement)),
                (sys.stderr, "\n".join(timing)),
                (sys.stdout, "\n".join(review)),
                (sys.stdout, "\n".join(f"wrote {path}" for path in written)),
            ],
        )
    except Exception as exc:  # noqa: BLE001 -- one repo, not the run
        # The printer stamps the repository's name on every line it emits in a
        # multi-repository run, so naming it again here produces `bad: FAILED bad: ...`.
        printer.progress(name, f"FAILED: {type(exc).__name__}: {exc}")
        if handle is None:
            handle = _diagnostic_handle(repo, name)
        return {
            "name": name,
            "handle": handle,
            "repo": str(repo),
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": name,
        "handle": handle,
        "repo": str(repo),
        "status": row.get("status"),
        "error": None,
    }


def write_zip(out_dir: Path, handles: list[str]) -> Path:
    """Archive exactly this run's handle folders, beside the output directory itself.

    Beside and not inside, because an archive written into the directory it is archiving
    either contains a truncated copy of itself or has to be special-cased out; and the
    top-level folder is kept so unpacking it in a downloads directory produces one folder
    rather than a scatter of handles. Walking `handles` rather than everything under
    `out_dir` is what keeps stale content from an earlier run into the same `--out`, or
    anything else an operator happens to keep there, out of a file meant to leave the
    machine -- `--out .` must not turn the whole working directory into an attachment.
    """
    zip_path = out_dir.parent / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for handle in sorted(set(handles)):
            folder = out_dir / handle
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, str(Path(out_dir.name) / path.relative_to(out_dir)))
    return zip_path


INDEX_NAME = "INDEX.local.txt"

#: Said at the top of the local index every time it is written, so opening it away from
#: this tool's own docs still explains what it is and why it never travels with the zip.
INDEX_HEADER = (
    "# hazina-scan index -- LOCAL ONLY. This file is not included in hazina-out.zip.",
    "# It maps each output folder to the repository it came from on this machine.",
)


def write_index(out_dir: Path, entries: list[tuple[str, str, str]]) -> Path:
    """Write or update `INDEX.local.txt`, the one file that names a local path at all.

    `entries` is `(handle, absolute repo path, status or "FAILED")` for every repository
    this run touched. A run into an `--out` an earlier run already wrote to keeps that
    file's other rows untouched and only replaces the ones this run has a fresh answer
    for -- reading the old lines, swapping in the new ones by handle, and appending
    whatever handle is new, is simpler than reasoning about a merge.
    """
    path = out_dir / INDEX_NAME
    fresh = {handle: f"{handle}\t{repo_path}\t{status}" for handle, repo_path, status in entries}
    kept: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            handle = line.split("\t", 1)[0]
            if handle in fresh:
                kept.append(fresh[handle])
                seen.add(handle)
            else:
                kept.append(line)
    for handle in fresh:
        if handle not in seen:
            kept.append(fresh[handle])
            seen.add(handle)
    path.write_text("\n".join((*INDEX_HEADER, *kept)) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Settled once, here, so that every later reader of the level sees the same answer:
    # `--no-build` is the plainer spelling of `--build none` and always wins over it.
    args.build_level = "none" if args.no_build else args.build

    repos = select_repos(args, sys.stderr)
    if repos is None:
        return 2

    out_dir = Path(args.out).expanduser().resolve()
    names = local_labels(repos)

    blocker = _repo_containing(out_dir, repos)
    if blocker is not None:
        print(
            f"error: --out would write inside the repository being measured: {out_dir}\n"
            f"       is inside {blocker}. Nothing is ever written into a measured tree, "
            f"because the files written would change the next run's repo_digest.\n"
            f"       choose a directory outside it, for example --out "
            f"{blocker.parent / 'hazina-out'}",
            file=sys.stderr,
        )
        return 2

    printer = _Printer(prefix=len(repos) > 1)
    allocator = _HandleAllocator()

    results: list[dict] = []
    if len(repos) == 1:
        results.append(measure_one(repos[0], names[0], args, out_dir, printer, repos, allocator))
    else:
        workers = max(1, min(args.jobs if args.jobs > 0 else 1, len(repos)))
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(measure_one, repo, name, args, out_dir, printer, repos, allocator)
                for repo, name in zip(repos, names, strict=True)
            ]
            results = [f.result() for f in futures]

    # The index is written whatever the outcome -- even a run that measured nothing still
    # owes the operator a record of what was tried and what happened to it.
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = write_index(
        out_dir,
        [(r["handle"], r["repo"], r["status"] or "FAILED") for r in results],
    )

    for result in results:
        handle_display = result["handle"] or "FAILED"
        if result["error"]:
            print(f"{handle_display}  <-  {result['name']}: FAILED -- {result['error']}")
        else:
            print(f"{handle_display}  <-  {result['name']}: {result['status']}")

    if not args.no_zip:
        handles = [r["handle"] for r in results if r["error"] is None]
        if handles:
            print(f"wrote {write_zip(out_dir, handles)}")

    print(f"index (local only, not in the zip): {index_path}")

    return 1 if any(r["error"] for r in results) else 0
