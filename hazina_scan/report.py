"""Everything a run puts on the terminal: the forecast, the one-line result, the timings.

This module is prose about measurements, never a measurement. `orchestrator.measure`
produces the two documents and `orchestrator.write_outputs` puts them on disk; here they are
turned into the handful of lines somebody watching the run actually reads. Keeping that in a
separate file is the point: no edit to a sentence can reach a number.

Why forecast at all? Because the worst way for a scan to end is for the person who started
it to give up on it. Two minutes into a run of unknown length, killing it looks reasonable,
and then the time already spent buys nothing and the repository ends up recorded as
unmeasured. Saying up front roughly how long this will take removes the guesswork, so the
forecast is printed before the first lane opens.

The forecast's own walk (`survey`) is separate from the one `tree` does, and is as thin as
it can be: it counts directory entries and nothing else -- no file is opened, decoded,
hashed or classified. It prunes exactly the directories the real walk prunes, so the number
it reports is the number of files the run will genuinely visit, and it gives up at a cap,
after which it hedges with "at least". A forecast that costs a minute has already failed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from . import tree

__all__ = ["Survey", "survey", "plan_lines", "summary_line", "timing", "SURVEY_CAP"]

# The deterministic fan-out is modelled as a straight line: a fixed set-up cost, plus so
# many seconds for every thousand files. Both numbers come from watching real runs and
# neither pretends to be better than an order of magnitude. The forecast is a courtesy; what
# will one day bound a run is the budget, which this release records but does not yet
# enforce.
_SETUP_SECONDS = 5.0
_SECONDS_PER_1000_FILES = 3.0

# However large the repository, the forecast stops at an hour. Beyond some size the straight
# line has nothing left to say, and announcing "a 412 minute run" would dress a guess up as
# arithmetic.
_LONGEST_FORECAST_SECONDS = 3600.0

#: Directory entries the forecast's walk will look at before it stops counting. Past this it
#: reports a floor instead of a total, because a forecast nobody waits for is worthless.
SURVEY_CAP = 200_000


class Survey(NamedTuple):
    """How much there is to look at. `partial` is True when the walk hit its cap, in which
    case the two counts are floors and every sentence built from them has to hedge."""

    files: int
    directories: int
    partial: bool


def _spoken_duration(seconds: float) -> str:
    """Render a span the way it would be said aloud, as an adjective: "a 40 second run".

    Below two minutes it stays in seconds. Above that it switches to minutes, because
    telling somebody to expect "a 0 minute run" answers nothing they asked.
    """
    if seconds < 120:
        return f"{seconds:.0f} second"
    return f"{seconds / 60:.0f} minute"


def survey(repo: Path, cap: int = SURVEY_CAP) -> Survey:
    """Count what a run would walk over, stopping once `cap` entries have been seen.

    Directories the tree collector ignores -- `.git`, `node_modules`, `vendor`, dot
    directories that are not specifically kept -- are ignored here too, so the answer
    describes the work ahead rather than the size of the checkout.
    """
    files = directories = 0
    partial = False
    for _here, subdirectories, filenames in os.walk(Path(repo)):
        # Rewriting the list in place is what stops os.walk descending, and the predicate is
        # the collector's own, so the two walks prune identically.
        subdirectories[:] = [d for d in subdirectories if not tree.should_skip_dir(d)]
        directories += 1
        files += len(filenames)
        if files + directories >= cap:
            partial = True
            break
    return Survey(files, directories, partial)


def _forecast_seconds(files: int) -> float:
    return min(_LONGEST_FORECAST_SECONDS, _SETUP_SECONDS + _SECONDS_PER_1000_FILES * files / 1000.0)


def plan_lines(repo: Path, build_level: str, budget: int) -> list[str]:
    """Say what this run is about to cost, while it can still be reconsidered.

    Always two lines -- what was found, and how long that looks like -- and a third when the
    forecast overruns the budget. Nothing in this release stops a run at the budget, so
    that third line is a warning about how long this will take and not a description of
    what will be left out.
    """
    found = survey(repo)
    forecast = _forecast_seconds(found.files)
    hedge = "at least " if found.partial else ""

    lines = [
        f"[plan] {hedge}{found.files} files in {found.directories} directories scanned",
        f"[plan] this looks like a {_spoken_duration(forecast)} run against a "
        f"{_spoken_duration(budget)} budget (rough, from repository size)",
    ]
    if build_level != "none":
        # The command line rejects every level but "none" before control reaches here, so
        # this is currently unreachable. It is said out loud anyway: on the day the build
        # lane lands, a level the forecast does not model should be visible, not silent.
        lines.append(f"[plan] the build check at level {build_level} is not part of this estimate")
    if forecast > budget:
        lines.append(
            "[plan] the estimate EXCEEDS the budget. The budget is recorded but "
            "not enforced in this release, so the run will go past it rather "
            "than stop; raise --budget-seconds to record a realistic one."
        )
    return lines


def _display_name(row: dict, measurement: dict) -> str:
    """Pick the name to print for this repository.

    Prefer the true `owner/name` the remote gave, which the measurement carries: this runs
    on the operator's own machine against their own checkout, and a status line they cannot
    connect to a directory helps nobody. The flat row withholds that name deliberately, so
    the digest-derived handle stands in when the real name is unknown. Neither one travels
    from here into a file -- this module prints and never writes.
    """
    for candidate in (
        row.get("real_repo_name"),
        measurement.get("real_repo_name"),
        row.get("fake_repo_name"),
    ):
        if candidate:
            return candidate
    return "?"


def _grouped(value) -> str:
    """Thousands-separated, or `?` when the number was never obtained.

    Nothing unmeasured is ever printed as `0`: no commits at all is a fact about the
    repository, whereas no commit count is a fact about the run.
    """
    return "?" if value is None else f"{value:,}"


def summary_line(row: dict, measurement: dict) -> str:
    """Compress the whole measurement into the single line the operator wanted.

    It is not a grade and not a verdict -- just the few figures that confirm the run did
    what was asked. Every one of them also appears in the files.
    """
    frameworks = row.get("test_framework") or []
    tests = "/".join(frameworks) if frameworks else "none"
    has_ci = row.get("has_ci")
    ci = "?" if has_ci is None else ("yes" if has_ci else "no")
    return (
        f"{_display_name(row, measurement)}  {row.get('primary_language') or '?'}  "
        f"{_grouped(row.get('loc'))} LOC  {_grouped(row.get('commit_count'))} commits  "
        f"{_grouped(row.get('author_count'))} authors  tests: {tests}  ci: {ci}  "
        f"build: not run"
    )


def timing(clock) -> str:
    """The per-lane breakdown, followed by the same figures written out as a sentence.

    Shown after every run, however brief. Somebody judging whether four minutes was fair
    needs to see which lane spent them, not the total. These figures describe one machine on
    one afternoon rather than the repository, which is why they are printed and never
    emitted into an output document.
    """
    return f"{clock.table()}\n  {clock.note()}"
