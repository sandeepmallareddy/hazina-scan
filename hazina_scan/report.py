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

The forecast's walk is the build check's own discovery pass (`build.discover.survey`), which
is as thin as a walk gets: it counts directory entries and reads the manifest names it finds,
and opens, decodes or hashes nothing. Reusing it rather than writing a second walk is what
makes the forecast describe the run that is about to happen -- the project roots it names are
the roots the check will actually probe, in the order it will reach them.
"""

from __future__ import annotations

from pathlib import Path

from .build import discover

__all__ = ["estimate_run", "plan_lines", "summary_line", "timing"]

# The deterministic fan-out is modelled as a straight line: a fixed set-up cost, plus so
# many seconds for every thousand files. Both numbers come from watching real runs and
# neither pretends to be better than an order of magnitude. The build check's own estimate
# comes from the module that will run it, which knows what a project root costs at each level.
_SETUP_SECONDS = 5.0
_SECONDS_PER_1000_FILES = 3.0

# However large the repository, the readers' forecast stops at an hour. Beyond some size the
# straight line has nothing left to say, and announcing "a 412 minute run" would dress a guess
# up as arithmetic.
_LONGEST_FORECAST_SECONDS = 3600.0

#: What the two phases are called in the split the forecast prints. The first name covers
#: every lane that runs concurrently; the second is the one lane that runs alone.
_READERS = "deterministic collectors"
_BUILD = "build probe"


def _plural(n: float, singular: str, plural: str | None = None) -> str:
    """Pick `singular` or `plural` for `n`, comparing the ROUNDED value so "1.0" stays singular."""
    return singular if round(n) == 1 else (plural or f"{singular}s")


def _spoken_duration(seconds: float, *, standalone: bool = False) -> str:
    """Render a span the way it would be said aloud.

    Below two minutes it stays in seconds; above that it switches to minutes, because
    telling somebody to expect "a 0 minute run" answers nothing they asked.

    Two forms, chosen by `standalone`. As an adjective before a noun ("a 40 second run"),
    the unit stays singular no matter the count -- that is how an ordinary compound
    adjective in English works, and it is the default here. As a number standing on its
    own ("40 seconds", "1 second"), pass `standalone=True` so the unit is pluralised
    correctly instead.
    """
    if seconds < 120:
        n, unit = seconds, "second"
    else:
        n, unit = seconds / 60, "minute"
    word = _plural(n, unit) if standalone else unit
    return f"{n:.0f} {word}"


def _reader_seconds(files: int) -> float:
    return min(_LONGEST_FORECAST_SECONDS, _SETUP_SECONDS + _SECONDS_PER_1000_FILES * files / 1000.0)


def estimate_run(
    repo: Path, build_level: str, max_build_projects: int = discover.MAX_PROBED_PROJECTS
) -> tuple[float, dict[str, float], dict]:
    """`(seconds, per-phase seconds, the read-only survey both came from)`.

    The lane graph is two phases, so the total is the concurrent readers PLUS the build
    check, never the sum of every lane: the readers overlap each other and the check
    overlaps nothing.
    """
    found = discover.survey(repo, max_build_projects)
    phases = {
        _READERS: _reader_seconds(found["files_scanned"] or 0),
        _BUILD: discover.estimate_seconds(found, build_level),
    }
    return phases[_READERS] + phases[_BUILD], phases, found


def plan_lines(
    repo: Path,
    build_level: str,
    budget: int,
    max_build_projects: int = discover.MAX_PROBED_PROJECTS,
) -> list[str]:
    """Say what this run is about to cost, while it can still be reconsidered.

    Two lines always -- what is in the tree, and how long that looks like against the budget
    -- and a further line for each of the two ways a run can come back with less than it was
    asked for: more project roots than the check may reach, and an estimate the budget does
    not cover. Both are said before a second has been spent, because the remedy for either
    is a flag, and a flag is only useful before the run.
    """
    total, phases, found = estimate_run(repo, build_level, max_build_projects)
    ecosystems = ", ".join(found["ecosystems"]) or "no build manifest found"
    split = ", ".join(
        f"{name} {_spoken_duration(value, standalone=True)}"
        for name, value in phases.items()
        if value
    )

    lines = [
        f"[plan] {found['n_projects']} {_plural(found['n_projects'], 'project root')} "
        f"({ecosystems}), {found['files_scanned']} "
        f"{_plural(found['files_scanned'], 'file')} in {found['dirs_scanned']} "
        f"{_plural(found['dirs_scanned'], 'directory', 'directories')} scanned",
        f"[plan] this looks like a {_spoken_duration(total)} run against a "
        f"{_spoken_duration(budget)} budget (rough, from repository size and project "
        f"count): {split}",
    ]
    if found["n_projects_over_cap"]:
        lines.append(
            f"[plan] {found['n_projects_over_cap']} of {found['n_projects']} "
            f"{_plural(found['n_projects'], 'project root')} are past the cap and will be "
            f"reported skipped rather than measured; raise --max-build-projects to probe them"
        )
    if total > budget:
        cut = "the build check" if phases[_BUILD] else "the reading lanes"
        lines.append(
            f"[plan] the estimate EXCEEDS the budget, so expect {cut} to be cut short: "
            f"whatever the budget does not reach is reported null with a reason, never as a "
            f"low number. Raise --budget-seconds to measure it all."
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
        f"build: {_build_word(row, measurement)}"
    )


def _build_word(row: dict, measurement: dict) -> str:
    """One word for the build check: what it was, or that it was not asked for.

    `?` and `not run` are kept apart deliberately. A check that ran and could not attribute
    what it saw is a different fact from a check nobody asked for, and collapsing the two
    would let a run whose verdict is genuinely unknown read as a run that skipped the
    question.
    """
    block = (measurement.get("ext_signals") or {}).get("build")
    if not block or block.get("build_skipped"):
        return "not run"
    verdict = row.get("build_ok")
    if verdict is None:
        return "?"
    return "ok" if verdict else "failed"


def timing(clock) -> str:
    """The per-lane breakdown, followed by the same figures written out as a sentence.

    Shown after every run, however brief. Somebody judging whether four minutes was fair
    needs to see which lane spent them, not the total. These figures describe one machine on
    one afternoon rather than the repository, which is why they are printed and never
    emitted into an output document.
    """
    return f"{clock.table()}\n  {clock.note()}"
