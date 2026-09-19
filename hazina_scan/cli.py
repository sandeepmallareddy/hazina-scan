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
        f"repository gets <out>/<its directory name>/; a repeated name is "
        f"suffixed -2, -3.",
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
        help=f"Do not write {ZIP_NAME} beside the output directory after a run over "
        f"several repositories.",
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


def _writes_into_a_repo(
    out_dir: Path, names: list[str], repos: list[Path]
) -> tuple[Path, Path] | None:
    """Find a directory this run would create that lies inside a repository being measured.

    Writing into a measured tree changes what the next run measures: the content digest
    covers every file, so a second scan of an otherwise untouched repository would disagree
    with the first. Both `out_dir` and each `out_dir/<name>` are checked, because
    `--out ./results` from a parent directory can land `results/myrepo` squarely on top of
    the repository called `myrepo`.

    Returns `(the directory, the repository it is inside)`, or None when the run is clear.
    """
    for target in [out_dir, *(out_dir / name for name in names)]:
        for repo in repos:
            if target.is_relative_to(repo):
                return target, repo
    return None


def output_names(repos: list[Path]) -> list[str]:
    """One output folder name per repository, in order, with repeats suffixed.

    Two repositories called `api` in different parents are a normal thing for a firm to
    have, and silently writing the second over the first would lose a measurement. The
    suffix goes on the LATER one, so the first repository named on the command line keeps
    the plain name.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    for repo in repos:
        base = repo.name or "repo"
        seen[base] = seen.get(base, 0) + 1
        names.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return names


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


def measure_one(repo: Path, name: str, args, out_dir: Path, printer: _Printer) -> dict:
    """Measure one repository, write its three files, and say what came out.

    Returns `{"name", "status", "error"}` and RAISES NOTHING. The whole body is inside one
    `try`, not just the call to `measure`: a full disk in `write_outputs`, an undeclared
    field in `review`, a broken pipe on the way to the terminal -- from the other
    repositories' point of view those are the same event as a collector raising, and any
    one of them escaping would take the remaining repositories, the per-repository status
    lines and the archive with it. The orchestrator is right to let a broken collector stop
    its OWN run; this layer is the one that knows there are others waiting.

    The failure is reported on stderr as `FAILED <name>: <class>: <message>` and returned,
    so the caller can exit 1 deliberately rather than by accident.
    """
    clock = orchestrator.LaneClock(label=printer.label(name))
    deadline = orchestrator.Deadline(args.budget)
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
        written = orchestrator.write_outputs(out_dir / name, row, measurement)

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
        return {"name": name, "status": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "status": row.get("status"), "error": None}


def write_zip(out_dir: Path) -> Path:
    """Archive the output directory beside itself, its own name as the top-level folder.

    Beside and not inside, because an archive written into the directory it is archiving
    either contains a truncated copy of itself or has to be special-cased out; and the
    top-level folder is kept so unpacking it in a downloads directory produces one folder
    rather than a scatter of repository names.
    """
    zip_path = out_dir.parent / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                archive.write(path, str(Path(out_dir.name) / path.relative_to(out_dir)))
    return zip_path


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
    names = output_names(repos)

    clash = _writes_into_a_repo(out_dir, names, repos)
    if clash is not None:
        target, repo = clash
        print(
            f"error: --out would write inside the repository being measured: {target}\n"
            f"       is inside {repo}. Nothing is ever written into a measured tree, "
            f"because the files written would change the next run's repo_digest.\n"
            f"       choose a directory outside it, for example --out "
            f"{repo.parent / 'hazina-out'}",
            file=sys.stderr,
        )
        return 2

    printer = _Printer(prefix=len(repos) > 1)

    results: list[dict] = []
    if len(repos) == 1:
        results.append(measure_one(repos[0], names[0], args, out_dir, printer))
    else:
        workers = max(1, min(args.jobs if args.jobs > 0 else 1, len(repos)))
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(measure_one, repo, name, args, out_dir, printer)
                for repo, name in zip(repos, names, strict=True)
            ]
            results = [f.result() for f in futures]

    if len(repos) > 1:
        if not args.no_zip and out_dir.is_dir():
            print(f"wrote {write_zip(out_dir)}")
        for result in results:
            if result["error"]:
                print(f"{result['name']}: FAILED -- {result['error']}")
            else:
                print(f"{result['name']}: {result['status']}")

    return 1 if any(r["error"] for r in results) else 0
