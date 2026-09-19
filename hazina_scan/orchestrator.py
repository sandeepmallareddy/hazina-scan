"""The run itself: one budget, concurrent lanes, two documents, three files.

Every collector in this package answers one question about a repository and answers it
alone. This module is what turns six of them into a measurement: it runs the independent
ones at the same time against a single deadline, folds their raw output into the two
documents the contract describes, and puts those through the write boundary --
`schema.enforce`, then `redact.redact_tree`, then `redact.audit_no_leak` -- before anything
reaches a disk.

THE LANE GRAPH. Everything except the classification is independent of everything else, so
the independent work runs concurrently and the wall clock is the slowest lane rather than
the sum of all of them:

    phase 1, all at once    digest, tree, git (+ the calendar), structure, history, identity
    phase 2, after phase 1  classify -- it reads the tree lane's output, and it is a pure
                            function over a dict, so serialising it costs nothing
    phase 3, ALONE          the build check, which is not in this release

Phase 3 is empty here and the shape is still the shape, because the exclusivity is not a
performance choice. A build check runs the project's own install and test commands INSIDE
the checkout: it rewrites lockfiles and drops artefacts into the tree every other lane is
reading, so overlapping it with a reader would not be slow, it would be wrong. `LaneClock`
records the spans so `overlaps("build")` can be asserted on rather than reasoned about, and
it answers `[]` today because the lane never runs.

THE TWO DOCUMENTS. `measurement.json` is the full record -- the tree, git, classification
and company-identity blocks plus the additive `ext_signals`. `codebase_repos.{json,csv}` is
the flat row: numbers, dates, enums and public stack names only, built from the RAW
collector output rather than from the redacted document, because a language name is a fact
about a public technology and the scrub would read it as a symbol. Both are held to the
same leak audit.

NULL IS NOT ZERO anywhere below. A field nothing measured is null; 0 means measured-none.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, classify, env, git, history, identity, redact, schema, structure, tree

__all__ = [
    "Deadline",
    "LaneClock",
    "CSV_COLUMNS",
    "measurer_version",
    "repo_digest",
    "repo_full_name",
    "commits_by_month",
    "active_days",
    "build_git_block",
    "build_codebase_repos_row",
    "row_status",
    "measure",
    "write_outputs",
]

# The tool and contract a report was produced under. Overridable so a packaging step can
# stamp a build without editing a source file; read at call time, not at import, so a test
# or a wrapper can set it after this module is already loaded.
VERSION_ENV_VAR = "HAZINA_SCAN_VERSION"

# Interval between the "still running" notices. Silence is what convinces somebody that a
# tool has hung, and the cure they reach for is the kill signal -- which throws away
# everything the run has already paid for and leaves the repository unmeasured. A single
# line per minute costs nothing, so it is never switched off.
HEARTBEAT_SECONDS = 60

# The intended ceiling on one repository, in seconds. It is RECORDED, NOT ENFORCED in this
# release: a `Deadline` is built from it and reported on at the end, but every lane here is
# a bounded local read and none of them asks it for a slice. Enforcement arrives with the
# build check, which is the first lane that can run long enough to need it. The arithmetic
# it is sized for is: whichever concurrent reader finishes last, within the budget less the
# build reserve, then the build check within that reserve.
DEFAULT_BUDGET_SECONDS = 9000

# The build check's guaranteed share of that budget. It is the only lane that EXECUTES
# anything, so it must not be the lane that gets whatever the others leave behind.
DEFAULT_BUILD_BUDGET_SECONDS = 1800

# Ceiling for one command inside that check (an install, a build, a suite). Subordinate to
# the two budgets above: a phase gets the smallest of this, its project's share, and what is
# left overall.
DEFAULT_TIMEOUT_BUILD = 900

# How long a full attempt may run before the measurement is completed at the cheaper level.
DEFAULT_FULL_ATTEMPT_SECONDS = 900

# How many projects inside one repository the build check may reach.
DEFAULT_MAX_BUILD_PROJECTS = 8

_NOT_YET_BUILT = "the build check arrives in a later release"

# Lane display names for the timing sentence on the terminal. Keys not listed here fall back
# to their own name with underscores turned into spaces.
_LANE_WORDS = {
    "tree": "tree scan",
    "git": "history scan",
    "structure": "structure",
    "history": "history depth",
    "identity": "company identity",
    "build": "build check",
}


def measurer_version() -> str:
    """The tool and contract version stamped into every measurement."""
    return os.environ.get(VERSION_ENV_VAR) or f"hazina-scan@{__version__}"


# ---------------------------------------------------------------------------
# How long the run may take
# ---------------------------------------------------------------------------


class Deadline:
    """A single wall-clock allowance, held against a monotonic reference.

    Nothing in this release consults it to decide whether to stop. Every lane is a bounded
    local read, so none of them calls `slice()`, and the object exists to be started,
    reported against and carried into the release that does enforce it.

    The design it is built for puts one object in charge of how much time is left, with
    nothing permitted to ask for more than that. Per-lane ceilings would not bound anything,
    because several generous ceilings in sequence add up to no ceiling at all. Once
    enforcement lands, work the allowance does not reach is to be recorded as unmeasured
    with the reason beside it, so that a short budget yields an incomplete answer and never
    an understated one.
    """

    def __init__(self, budget_seconds: int) -> None:
        self.total = max(0, int(budget_seconds))
        self._opened_at = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since this allowance was opened."""
        return time.monotonic() - self._opened_at

    def remaining(self) -> float:
        """Seconds still available, floored at zero rather than going negative."""
        return max(0.0, self.total - self.elapsed())

    def slice(self, cap: int, reserve: float = 0.0) -> int:
        """How many whole seconds a lane may take, given its own ceiling and a reservation.

        `reserve` is what keeps time aside for the one lane that runs the repository's own
        commands. Without it a queue of slow readers would consume the allowance and the
        build check -- the only lane that cannot be retried cheaply -- would inherit
        nothing.
        """
        return max(0, int(min(cap, self.remaining() - max(0.0, reserve))))


# ---------------------------------------------------------------------------
# Who ran when, and who was running at the same time
# ---------------------------------------------------------------------------


class LaneClock:
    """Keeps the start and finish offset of every lane, and says out loud that work goes on.

    The second duty matters as much as the first. These offsets are the *evidence* behind
    the exclusivity rule: the lane that executes a project's own build must never run
    alongside a lane reading the same tree, and whether two half-open intervals touch is
    arithmetic rather than interpretation. `overlaps()` settles it from the recorded numbers
    instead of from somebody's reading of the control flow.

    `label` says which repository is being timed. Several repositories can be measured by
    one process, all of them writing to the same stderr, and a bare `[lane] tree started`
    in that stream identifies the lane but not the repository -- which is the half the
    reader actually needs. The default is empty, so a single-repository run stays as quiet
    as it was before the label existed.
    """

    def __init__(self, interval: int = HEARTBEAT_SECONDS, label: str = "") -> None:
        self._label = f"{label}: " if label else ""
        self._origin = time.monotonic()
        self._lock = threading.Lock()
        self._opened: dict[str, float] = {}
        self._closed: dict[str, float] = {}
        self._interval = max(1, interval)
        self._silence = threading.Event()
        self._announcer: threading.Thread | None = None

    # -- the clock itself ---------------------------------------------------

    def _offset(self) -> float:
        """Seconds since this clock was created. Every recorded number is one of these."""
        return time.monotonic() - self._origin

    def elapsed(self) -> float:
        return self._offset()

    def _say(self, message: str) -> None:
        print(f"{self._label}{message}", file=sys.stderr, flush=True)

    @contextlib.contextmanager
    def lane(self, name: str):
        """Time whatever runs in this block, whether it returns or raises."""
        with self._lock:
            self._opened[name] = self._offset()
            # Reusing a name starts a fresh span; leaving the old finish behind would make
            # the lane look as though it had ended before it began.
            self._closed.pop(name, None)
        self._say(f"[lane] {name} started")
        try:
            yield
        finally:
            with self._lock:
                finished = self._offset()
                self._closed[name] = finished
                took = finished - self._opened[name]
            self._say(f"[lane] {name} finished after {took:.1f}s")

    # -- reading the record -------------------------------------------------

    def spans(self) -> dict[str, tuple[float, float]]:
        """Each lane's `(start, end)`. A lane still running is reported as ending now."""
        with self._lock:
            now = self._offset()
            return {
                name: (start, self._closed.get(name, now)) for name, start in self._opened.items()
            }

    def _in_start_order(self) -> list[tuple[str, tuple[float, float]]]:
        return sorted(self.spans().items(), key=lambda item: item[1][0])

    def seconds(self, name: str) -> float | None:
        span = self.spans().get(name)
        return None if span is None else span[1] - span[0]

    def overlaps(self, name: str) -> list[str]:
        """Which lanes were running while `name` was.

        An empty list carries two meanings that are the same answer to the question asked:
        the lane had the machine to itself, or the lane never ran at all.
        """
        spans = self.spans()
        if name not in spans:
            return []
        start, end = spans[name]
        return sorted(
            other
            for other, (began, ended) in spans.items()
            if other != name and began < end and start < ended
        )

    # -- saying that the run is still alive ---------------------------------

    def start_heartbeat(self) -> None:
        if self._announcer is None:
            self._silence.clear()
            self._announcer = threading.Thread(target=self._announce_while_running, daemon=True)
            self._announcer.start()

    def stop_heartbeat(self) -> None:
        self._silence.set()
        if self._announcer is not None:
            self._announcer.join(timeout=2)
            self._announcer = None

    def _announce_while_running(self) -> None:
        # `Event.wait` doubles as the sleep and as the stop signal: it returns True the
        # moment stop_heartbeat() fires, so shutdown never waits out a whole interval.
        while not self._silence.wait(self._interval):
            with self._lock:
                now = self._offset()
                unfinished = sorted(
                    (name, start)
                    for name, start in self._opened.items()
                    if name not in self._closed
                )
            if not unfinished:
                continue
            ages = ", ".join(f"{name} {now - start:.0f}s" for name, start in unfinished)
            self._say(f"[alive] {now:.0f}s elapsed; still running: {ages}")

    # -- what the operator reads at the end ---------------------------------

    def table(self) -> str:
        """A column per number: when each lane began, when it ended, how long it took."""
        lines = [
            "lane wall clock (seconds, offsets from the start of the run):",
            f"  {'lane':<18}{'start':>9}{'end':>9}{'elapsed':>9}",
        ]
        for name, (start, end) in self._in_start_order():
            lines.append(f"  {name:<18}{start:>9.1f}{end:>9.1f}{end - start:>9.1f}")
        total = self.elapsed()
        lines.append(f"  {'total':<18}{0.0:>9.1f}{total:>9.1f}{total:>9.1f}")
        return "\n".join(lines)

    def note(self) -> str:
        """The table's figures again, as a sentence, for somebody skimming the terminal.

        None of this goes into the measurement, and that is a decision rather than an
        omission. How long a lane took describes this machine at this hour; it says nothing
        about the repository. No output field is declared for it, and the right response to
        a missing field is to leave the number out, not to find some prose field it fits in.
        """
        pieces = [
            f"{_LANE_WORDS.get(name, name.replace('_', ' '))} {end - start:.0f}"
            for name, (start, end) in self._in_start_order()
        ]
        return (
            "lane wall clock in seconds: "
            + ", ".join(pieces)
            + f", total {self.elapsed():.0f}; independent lanes ran at the same time, "
            "so the total is below their sum"
        )


# ---------------------------------------------------------------------------
# Identifying the tree: what it contains, and what the world calls it
# ---------------------------------------------------------------------------

#: What stands in for a file's hash when this process is not allowed to read it. Recording
#: the obstruction keeps the walk going; raising half-way through would waste the rest.
_UNREADABLE = "unreadable"


def _digest_entries(root: Path):
    """Yield `(relative path, content hash)` for every regular file worth hashing.

    Symlinks are stepped over -- following one would hash a file twice or wander outside the
    tree -- and so is everything under `.git/`, whose contents change with operations that
    change nothing about the code.
    """
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        try:
            yield relative, hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            yield relative, _UNREADABLE


def repo_digest(repo: Path) -> str:
    """A sha256 fingerprint of the tree's contents, independent of where it is checked out.

    The hash runs over `relative path`, NUL, `sha256 of the bytes`, NUL, for every file in
    sorted order, so two directories holding the same files agree and nothing about the
    surrounding filesystem can move the answer.

    Nothing is excluded on the strength of `.gitignore`, not even a committed `.env`. The
    tool keeps none of what it reads here: no byte, no length and no filename can be got
    back out of sixty-four hex characters. What excluding files would cost is exactly the
    property the digest exists to provide, since the identity of a tree would then depend on
    a file inside the tree that anyone can edit.
    """
    running = hashlib.sha256()
    for relative, content_hash in _digest_entries(Path(repo)):
        running.update(relative.encode("utf-8"))
        running.update(b"\0")
        running.update(content_hash.encode("ascii"))
        running.update(b"\0")
    return running.hexdigest()


def _remote_url(repo) -> str:
    """Whatever `origin` is set to, or an empty string when the checkout has no remote.

    Separated out because it is the only place in this package that asks about a remote at
    all. The parsing below is the sole consumer, and a test covering the parsing swaps this
    function out instead of constructing a repository for every URL shape.
    """
    return env.run_git(repo, "config", "--get", "remote.origin.url").strip()


def _split_host_and_path(url: str) -> tuple[str, str] | None:
    """Separate a remote URL into its host and its path, or return None if it has no host.

    This is the whole of the safety argument for `repo_full_name`. Git is happy to treat a
    directory as a remote, and `/srv/repo` or `file:///home/jo/work/repo` would sail through
    any test shaped like "does this look like owner/name" while publishing an account name
    and a directory layout. A URL therefore qualifies only if it genuinely names a machine:
    an explicit scheme that is not `file`, or the scp-like `host:path` spelling.
    """
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() == "file" or "/" not in rest:
            return None
        host, path = rest.split("/", 1)
        # `file:///...` and friends leave nothing before the first slash.
        if not host or host.startswith(":"):
            return None
        return host, path

    if ":" in url and not url.startswith(("/", ".", "~")):
        host, path = url.split(":", 1)
        # No hostname contains a slash, so a slash here means this was a path all along.
        # The length test rejects a Windows drive letter, as in `C:/src/repo`.
        if "/" in host or len(host.split("@")[-1]) < 2:
            return None
        return host, path

    # An absolute path, a relative one, or a plain directory name.
    return None


def repo_full_name(repo) -> str | None:
    """What the remote calls this repository, as `owner/name`, or None if it will not say.

    The answer comes from the remote and not from the directory the checkout sits in,
    because a clone is very often named `name` with the owning organisation thrown away.
    Both ways of writing a remote are understood::

        git@example.org:acme/widget.git           yields  acme/widget
        https://example.org/acme/team/widget.git  yields  acme/team/widget

    None is an ordinary answer, not a failure: a repository cloned from a directory, or one
    that never had a remote, simply has no such name, and the tool behaves as it did before
    this field was added. A remote that resolves to a local path also yields None -- see
    `_split_host_and_path` for why that matters more than it looks.
    """
    url = _remote_url(repo)
    if not url:
        return None

    split = _split_host_and_path(url[:-4] if url.endswith(".git") else url)
    if split is None:
        return None
    _host, path = split

    segments = [piece for piece in path.strip("/").split("/") if piece]
    if not 1 <= len(segments) <= 4:
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", piece) for piece in segments):
        return None
    return "/".join(segments)


# ---------------------------------------------------------------------------
# Two history figures that need to know which commit is the tip
# ---------------------------------------------------------------------------

_MONTHS_REPORTED = 12


def _month_slots(latest: str) -> dict[str, int]:
    """Map the `YYYY-MM` labels of the reported window onto their positions in the array.

    `latest` occupies the last slot and each earlier month steps one place back, so the
    arithmetic runs in months-since-year-zero and converts back at the end -- which keeps
    December-to-January from needing a special case.
    """
    year, month = int(latest[:4]), int(latest[5:7])
    last = year * 12 + (month - 1)
    slots = {}
    for position in range(_MONTHS_REPORTED):
        count = last - (_MONTHS_REPORTED - 1 - position)
        slots[f"{count // 12:04d}-{count % 12 + 1:02d}"] = position
    return slots


def commits_by_month(repo: Path, tip: str) -> list[int]:
    """Twelve counts, one per calendar month, ending with the month of the newest commit.

    Position 11 is that newest month and position 0 is eleven months before it; a month
    without commits is 0 rather than absent. The window hangs off the repository's own last
    commit instead of today's date, so running the measurement later does not reshape a
    history that has not changed.
    """
    logged = env.run_git(repo, "log", tip, "--no-merges", "--format=%cd", "--date=format:%Y-%m")
    months = [line.strip() for line in logged.splitlines() if line.strip()]
    counts = [0] * _MONTHS_REPORTED
    if not months:
        return counts
    slots = _month_slots(max(months))
    for month in months:
        position = slots.get(month)
        if position is not None:
            counts[position] += 1
    return counts


def active_days(repo: Path, tip: str) -> int | None:
    """How many separate calendar dates carry at least one commit, or None if git said
    nothing at all."""
    logged = env.run_git(repo, "log", tip, "--no-merges", "--format=%cd", "--date=format:%Y-%m-%d")
    dates = {line.strip() for line in logged.splitlines() if line.strip()}
    return len(dates) if dates else None


# ---------------------------------------------------------------------------
# Field mapping -- the flat row and the git block
# ---------------------------------------------------------------------------

# Content signals only. A repository NAME cannot on its own settle whether a repository is
# a demo -- "poc", "sample", "playground" and "starter" are ordinary words in product
# repository names -- and the only name this tool has is the directory it was pointed at.
_STRONG_DEMO_KEYS = (
    "known_demo_app",
    "authoritative_demo",
    "scaffold_fingerprint",
    "template_readme",
)


def _demo(tree_raw: dict) -> tuple[bool, list[str]]:
    signals = tree_raw.get("demo_signals", {}) or {}
    fired = [k for k, v in signals.items() if v]
    is_demo = any(signals.get(k) for k in _STRONG_DEMO_KEYS)
    return is_demo, fired


def build_git_block(raw: dict) -> dict:
    """The `git` block = the collector's `repo_stats` sub-block plus its history tallies.

    The per-commit lists the collector also returns stay behind: they carry abbreviated
    hashes and author keys, and nothing downstream needs them.
    """
    block = dict(raw.get("repo_stats", {}) or {})
    for k in (
        "class_a_count",
        "class_b_count",
        "class_c_pre_count",
        "class_d_bug_count",
        "confirmed_candidate_count",
        "provisional_candidate_count",
        "analyzed_commits",
        "full_history_scanned",
    ):
        if k in raw:
            block[k] = raw[k]
    return block


def row_status(build: dict | None) -> tuple[str, str | None]:
    """Decide the row's two completeness columns. `measured` means every lane delivered.

    Only the build check can leave a gap in this row, and no build check runs in this
    release, so `build` arrives as None and the answer is always `measured` today. The
    general case is written out regardless: the alternative would be a hard-coded string now
    and a rewrite the day the lane ships, and this is the column people filter on. A row
    claiming `measured` while a check had died would be asserting a completeness that never
    existed.

    Where both apply, a timeout is the reason given rather than a failure. The person
    reading the row can raise a budget themselves, whereas a failure sends them off to fix a
    machine. A check the clock never reached counts as a timeout too -- it was asked for and
    the time ran out before it started.
    """
    if build is None:
        return "measured", None
    if build.get("timed_out") or build.get("build_skipped"):
        return "partial", "lanes_timed_out"
    if not build.get("ok"):
        return "partial", "lanes_unavailable"
    return "measured", None


def build_codebase_repos_row(
    tree_block: dict,
    git_block: dict,
    classification: dict,
    digest: str,
    structure_block: dict,
    capacity: int | None,
    build_ok,
    testable_at_head,
    measured_at: str,
    status: str = "measured",
    skip_reason: str | None = None,
) -> dict:
    """Lay the collected signals out across the flat row's columns.

    The row admits numbers, dates, fixed enum words and public stack names, and nothing
    else: no path and no symbol can appear, which makes it safe by construction. That is why
    it is assembled from the collectors' unredacted output -- a language name would not
    survive the scrub intact and does not need to. A column nothing measured is null, and 0
    is reserved for a column that was measured and came to none.
    """
    total_source_files = tree_block.get("total_source_files") or 0
    test_spec_files = tree_block.get("test_spec_files")
    test_ratio = (
        round(test_spec_files / total_source_files, 4)
        if test_spec_files is not None and total_source_files
        else None
    )
    return {
        # identity / provenance (the platform assigns the ids; the real name is withheld)
        "id": None,
        "codebase_id": None,
        "service_id": None,
        "repo_digest": digest,
        "real_repo_name": None,
        "fake_repo_name": f"repo-{digest[:12]}",  # stable, non-identifying handle
        "status": status,
        "skip_reason": skip_reason,
        "measured_at": measured_at,
        # size / language
        "loc": tree_block.get("total_loc"),
        "zip_bytes": None,  # no bundle is produced
        "excluded_loc": None,  # generated files are counted, not summed
        "languages": tree_block.get("loc_by_language"),
        "primary_language": tree_block.get("primary_language"),
        "frontend_pct": None,  # no clean deterministic LOC split
        "backend_pct": None,
        # tests
        "test_loc": None,  # test LOC is not separately summed
        "test_code_files": (
            structure_block.get("test_files") if structure_block.get("ok") else None
        ),
        "test_spec_files": test_spec_files,
        "test_ratio": test_ratio,
        "test_source_ratio": tree_block.get("test_source_ratio"),
        "test_framework": tree_block.get("test_framework"),
        # ci
        "has_ci": tree_block.get("ci_present"),
        "ci_present": tree_block.get("ci_present"),
        "ci_runs_tests": tree_block.get("ci_runs_tests"),
        "detected_frameworks": tree_block.get("detected_frameworks"),
        # history
        "commit_count": git_block.get("total_commits"),
        "author_count": git_block.get("human_authors"),
        "first_commit_at": git_block.get("first_commit"),
        "last_commit_at": git_block.get("last_commit"),
        "active_days": git_block.get("active_days"),
        "span_days": git_block.get("span_days"),
        "commits_by_month": git_block.get("commits_by_month"),
        # provider tables (absent -> null)
        "pr_count": None,
        "issue_count": None,
        # classification
        "repo_class": classification.get("primary_class"),
        "is_likely_demo": classification.get("is_likely_demo"),
        # legacy, deprecated
        "quality_score": None,
        # the additions this stage contributes
        "build_ok": build_ok,
        "testable_at_head": testable_at_head,
        "capacity": capacity,
    }


CSV_COLUMNS = [
    "id",
    "codebase_id",
    "service_id",
    "repo_digest",
    "real_repo_name",
    "fake_repo_name",
    "status",
    "skip_reason",
    "measured_at",
    "loc",
    "zip_bytes",
    "excluded_loc",
    "languages",
    "primary_language",
    "frontend_pct",
    "backend_pct",
    "test_loc",
    "test_code_files",
    "test_spec_files",
    "test_ratio",
    "test_source_ratio",
    "test_framework",
    "has_ci",
    "ci_present",
    "ci_runs_tests",
    "detected_frameworks",
    "commit_count",
    "author_count",
    "first_commit_at",
    "last_commit_at",
    "active_days",
    "span_days",
    "commits_by_month",
    "pr_count",
    "issue_count",
    "repo_class",
    "is_likely_demo",
    "quality_score",
    "build_ok",
    "testable_at_head",
    "capacity",
]


def _csv_cell(v) -> str:
    """One cell. Null is an empty cell, never the string "None" and never a zero."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def write_outputs(out_dir: Path, row: dict, measurement: dict) -> list[Path]:
    """Write the row as JSON and as CSV, and the measurement as JSON. Returns what it wrote.

    Both spellings of the row are always written: the CSV is what a spreadsheet and a bulk
    loader want, the JSON is what keeps the types -- a null in a CSV cell and a null in JSON
    are the same fact, but only one of them survives a round trip.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    row_json = out_dir / "codebase_repos.json"
    row_json.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    written.append(row_json)

    row_csv = out_dir / "codebase_repos.csv"
    with row_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerow({c: _csv_cell(row.get(c)) for c in CSV_COLUMNS})
    written.append(row_csv)

    mpath = out_dir / "measurement.json"
    mpath.write_text(json.dumps(measurement, indent=2) + "\n", encoding="utf-8")
    written.append(mpath)
    return written


# ---------------------------------------------------------------------------
# The lanes
# ---------------------------------------------------------------------------


def _git_lane(repo: Path, git_top: int) -> tuple[dict, dict]:
    """The git collector plus the calendar it needs a tip commit for.

    One lane rather than two: the calendar depends on the effective tip, and both halves are
    log walks over the same history, so splitting them would only add a barrier.
    """
    git_raw = git.collect(repo, git_top=git_top)
    git_block = build_git_block(git_raw)
    tip = git_block.get("effective_tip_sha") or git_block.get("head_sha") or "HEAD"
    git_block["commits_by_month"] = commits_by_month(repo, tip)
    git_block["active_days"] = active_days(repo, tip)
    return git_raw, git_block


def measure(
    repo: Path,
    *,
    top_files: int = 10,
    git_top: int = 1,
    threshold: float = 0.18,
    build_level: str = "none",
    jobs: int = 0,
    clock: LaneClock | None = None,
    deadline: Deadline | None = None,
    build_budget: int = DEFAULT_BUILD_BUDGET_SECONDS,
    timeout_build: int = DEFAULT_TIMEOUT_BUILD,
    max_build_projects: int = DEFAULT_MAX_BUILD_PROJECTS,
    full_attempt_seconds: int = DEFAULT_FULL_ATTEMPT_SECONDS,
) -> tuple[dict, dict]:
    """Collect everything and return `(codebase_repos_row, measurement)`.

    Every number here is deterministic: the same commit always produces the same answer.
    All string leaves in both returned documents have been through the declaration
    boundary, the scrub and the leak audit before this returns, in that order.

    `build_level` must be `"none"`. `build_budget`, `timeout_build`, `max_build_projects`
    and `full_attempt_seconds` size a build check and are accepted and carried so the
    signature does not change under its callers the day that lane lands; passing any level
    other than `"none"` raises rather than quietly measuring less than it was asked for.
    `deadline` likewise: every lane here is a bounded local read, so nothing draws a slice
    from it yet, and it exists so one caller can put several repositories under one clock.

    Neither the wall-clock table nor the timing sentence is printed from here. Both are on
    `clock`, and what a run SAYS is the caller's decision -- this returns documents.
    """
    if build_level != "none":
        raise NotImplementedError(_NOT_YET_BUILT)

    repo = Path(repo).resolve()
    measured_at = datetime.now(UTC).isoformat()
    clock = clock if clock is not None else LaneClock()
    deadline = deadline if deadline is not None else Deadline(DEFAULT_BUDGET_SECONDS)

    # Resolved up front, ahead of the fan-out. Two consumers want this name -- the
    # measurement document and the identity lane -- it costs one `git config`, and the
    # careful remote parsing exists in exactly one place, so the lane receives the answer
    # instead of working it out again.
    full_name = repo_full_name(repo)

    # Each lane is a name paired with a zero-argument callable. An exception inside one
    # comes back out of `result()` on this thread, just as it would have done when these
    # were ordinary sequential calls: a collector that breaks stops the run instead of
    # handing back a measurement that is quietly missing a block.
    lanes = {
        "digest": lambda: repo_digest(repo),
        "tree": lambda: tree.collect(repo, top_files=top_files),
        "git": lambda: _git_lane(repo, git_top),
        "structure": lambda: structure.collect(repo),
        "history": lambda: history.collect(repo),
        "identity": lambda: identity.collect(repo, full_name),
    }

    def run(name: str, thunk):
        with clock.lane(name):
            return thunk()

    workers = jobs if jobs and jobs > 0 else len(lanes)
    clock.start_heartbeat()
    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(lanes)))) as pool:
            futures = {name: pool.submit(run, name, thunk) for name, thunk in lanes.items()}
            results = {name: fut.result() for name, fut in futures.items()}

        digest = results["digest"]
        tree_raw = results["tree"]
        git_raw, git_block = results["git"]
        structure_raw = results["structure"]
        history_raw = results["history"]
        company = results["identity"]

        # --- phase 2: classification, which reads the tree lane's output ---
        with clock.lane("classify"):
            classify_raw = classify.classify(tree_raw, threshold)

        # --- phase 3: the build check, alone, after every reader. Not in this release. ---
        build_ok = None
        testable_at_head = None
        build_raw = None
    finally:
        clock.stop_heartbeat()

    # Capacity is the deterministic count of mineable commits across history, which the
    # history collector owns. Unavailable -> null: a wrong number under the right name is
    # worse than no number at all.
    capacity = history_raw.get("mineable_commits") if history_raw.get("ok") else None

    is_demo, demo_reasoning = _demo(tree_raw)
    classification = {
        "primary_class": classify_raw.get("primary_class"),
        "class_confidence": classify_raw.get("class_confidence"),
        "is_monorepo": classify_raw.get("is_monorepo"),
        "is_likely_demo": is_demo,
        "demo_reasoning": demo_reasoning,
    }

    measurement = {
        "measurer_version": measurer_version(),
        "measured_at": measured_at,
        "repo_digest": digest,
        # Present so that a record can be tied to the repository it came from. The digest
        # cannot serve here: it names a set of file contents, whereas anything holding these
        # records will be organised by repository name, and matching the two by hand is
        # guesswork.
        "real_repo_name": full_name,
        # WHOSE code this is, which is a different question from WHICH repository it is,
        # and one nothing else in the output answers.
        "company_identity": company,
        "variant": "ext",
        "tree": tree_raw,
        "git": git_block,
        "classification": classification,
        "capacity": capacity,
        "ext_signals": {
            "structure": structure_raw,
            "history": history_raw,
            "build": build_raw,
        },
    }

    # --- DECLARE, then REDACT, then AUDIT, and nothing is returned until all three agree.
    # The declarations do the real work: every key and every leaf is checked against what
    # schema.py permits in this document, and anything undeclared halts the run. Behind that
    # sit the scrub and the audit, whose rules were derived from the output requirement
    # rather than from schema.py's checks, so the same oversight cannot pass both.
    measurement = schema.enforce("measurement", measurement)
    measurement = redact.redact_tree(measurement)
    redact.audit_no_leak(measurement)

    # The flat row holds numbers, dates, enums and public stack-name lists only, so it is
    # built from the RAW collector output -- real language and framework names -- and
    # verified by the same audit, which exempts the provenance and stack-fact fields.
    status, skip_reason = row_status(build_raw)
    row = build_codebase_repos_row(
        tree_block=tree_raw,
        git_block=git_block,
        classification=classification,
        digest=digest,
        structure_block=structure_raw,
        capacity=capacity,
        build_ok=build_ok,
        testable_at_head=testable_at_head,
        measured_at=measured_at,
        status=status,
        skip_reason=skip_reason,
    )
    row = schema.enforce("codebase_repos", row)
    redact.audit_no_leak(row)
    return row, measurement
