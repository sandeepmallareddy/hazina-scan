# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Follow-ups

- Cap a build-lane child's captured stdout/stderr (head+tail, e.g. 8 MB) instead of
  reading it in full, and re-run live parity once that lands — see SECURITY.md for the
  current unbounded behaviour.
- Split `_probe_project` in `hazina_scan/build/probe.py` (currently ~420 lines) into
  smaller per-phase helpers.
- Thread one discovery result through `report.plan_lines`, `probe.collect` and
  `_probe_tree` instead of walking the tree three separate times.
- `ext_signals.build.ok` is always `True` and `error` is never set, so `row_status`'s
  `lanes_unavailable` branch is unreachable. This is by design, not a bug: it is worth
  noting as dead code to simplify later.
- Windows support for the build check (venv `Scripts` layout, PATHEXT lookup, wrapper
  scripts without an executable bit) — the matrix runs Windows as allowed-to-fail until
  then.

## [0.3.1] - 2026-09-19

### Changed

- **Output folders on disk carry the repository's folder name again**, so results are
  easy to find -- a repeated folder name is suffixed `-2`, `-3`, as before 0.3.0. **The
  zip renames each folder to its anonymous handle as it packs it**, so no directory name
  travels with `hazina-out.zip`; two folders sharing a handle (identical trees) are still
  suffixed `-2`, `-3` inside the zip. `INDEX.local.txt` lists both names side by side.
- Package author contact is now **partners@hazinalabs.com**.

## [0.3.0] - 2026-09-19

### Changed

- **Output folders are named by the anonymous handle** (`repo-<hex>`, from the row's
  `fake_repo_name`) **instead of the repository's local directory name.** A directory name
  is exactly the kind of thing this tool promises never travels in the shared zip, and a
  folder named after one did precisely that; two repositories with identical trees still
  get distinct folders, suffixed `-2`, `-3`.
- **One `hazina-out.zip` is always written**, for a single repository measured alone just
  as for several, unless `--no-zip` is passed. It holds only the handle folders this run
  produced -- never stale content left under `--out` from an earlier run, and never
  anything else parked there.

### Added

- **`INDEX.local.txt`**, written beside the handle folders after every run: the one place
  that maps a folder back to the repository it came from on this machine. It stays on the
  machine and is never included in the zip.

## [0.2.0] - 2026-09-19

### Documentation

- A detailed README: where the scan fits in the Hazina Labs process, quick start, choosing a
  mode, a sample run, how to share results, troubleshooting and verify-it-yourself commands.
- Contact for all queries and security reports: partners@hazinalabs.com.

### Added

- **The build check** (`--build {none,discover,full}`, default `full`; `--no-build` for
  none). For each project root it finds, it resolves the language version the tree asks
  for, installs the dependencies the manifests declare, runs the project's build and lists
  its tests; at `full` it also runs the suite and reads any coverage report back. Node,
  Python, Go, Rust, Java (Maven and Gradle), .NET, Ruby and PHP are recognised from their
  own manifests, and each project is run with its own ecosystem's commands.
- **What it executes is the repository's own.** A one-line warning says so before the
  first check starts, and it says what everyone reading this should already do: **use a
  disposable clone**, because the check modifies the checkout — lockfiles, dependency
  directories, build output. The tree is snapshotted first and restored afterwards, on the
  way out of a failure included, but that restoration is best effort and not a sandbox.
  Every command runs with an empty throwaway `HOME` and a private `TMPDIR`, `CI=1`, no
  credentials, no proxy settings and no agent socket, a `PATH` with the operator's own
  directories removed, argument lists rather than a shell, and a process group of its own
  that is killed whole when a command overruns its ceiling.
- **`ext_signals.build`** in `measurement.json`: which level ran and which was asked for,
  whether the install and the build succeeded, whether tests were found and whether they
  ran, how it failed and whose failure it was, a coverage percentage, which runtime lanes
  the tree declared and which of them this machine could satisfy, and two indices scoring
  what was actually executed. The flat row gains `build_ok` and `testable_at_head`, and a
  row with a gap in it reports `partial` with the reason rather than `measured`.
- **A fallback from `full` to `discover`.** When a `full` attempt runs out of its share of
  the budget and the executed index therefore has no value at all, the measurement is
  finished at the cheaper level with what is left. Both levels are recorded — the one that
  ran and the one that was asked for — with the reason for the difference beside them. A
  build that genuinely fails, a project with no tests, or a failure that was this
  machine's does NOT fall back: those are complete answers already.
- `--build-budget-seconds`, `--full-attempt-seconds`, `--timeout-build` and
  `--max-build-projects` to size the check. Project roots past the cap are reported
  skipped, never dropped in silence.
- The up-front plan now names the project roots and ecosystems it found, splits the
  estimate between the reading lanes and the build check, and warns before the run starts
  when the estimate does not fit the budget or when roots will go unprobed.

### Changed

- **`--budget-seconds` is now enforced**, not merely recorded, for the lanes that draw on
  it. One allowance covers the whole repository; the git-backed lanes and the build check
  draw their ceilings from it, and the build check is held a reserved share so the one
  lane that executes anything is not left whatever the other lanes happen to leave behind.
  The in-process readers (tree, structure, identity, the content digest) consult no
  deadline and are bounded instead by their own file and size caps. Work the budget does
  not reach is reported null with a stated reason — never as a low number, and never as a
  truncated count.
- A failure that belongs to this machine rather than to the repository — a missing
  language runtime, a package index that could not be reached, a command the clock killed
  — leaves the affected verdicts null with the attribution beside them, rather than
  scoring the repository down for it.

## [0.1.0] - 2026-09-19

### Added

First public release.

- **Measurement.** Reads a git repository's working tree, git history, structure, and
  ownership signals, and writes numbers-only output files:
  - **Tree**: lines of code by language, file sizes, test and infrastructure files,
    declared dependencies, frameworks, package managers, and linters, parsed from
    manifests such as `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, and
    `pom.xml`.
  - **Git history**: commit counts and dates, the span and recency of activity, commits
    per month, active days, merge and tag counts, distinct authors, and how many of
    those look like bots.
  - **Structure**: directory shape, test layout, and other structural signals derived
    from the tree.
  - **Owning company**: candidate owning organisations, taken from licence/notice
    files, recurring copyright headers, the `origin` remote URL, and the organisation
    namespace in a root manifest.
  - **Classification**: a summary classification of the repository derived from the
    above.
- **Command line** (`hazina-scan`):
  - Measure a single repository, several repositories in one invocation, or every git
    repository directly inside a directory (`--all`).
  - `--jobs` to control how many repositories are measured at once.
  - `--review` to print every emitted field and its value, not just per-kind counts.
  - A zip archive (`hazina-out.zip`) written after a multi-repository run, next to the
    output directory (`--no-zip` to skip it).
- **Privacy boundary.** The tool opens no network connection and writes only to the
  output directory you choose, never into the repository being measured. Author names
  and email addresses, commit messages, branch and tag names, file and directory names,
  environment-variable names, and credentials are never collected. Every value written
  is checked against a declared allowlist before it is written; anything undeclared
  stops the run instead of being written.
- **No build check in that release.** `--no-build` was required on every command. The
  build check arrived in 0.2.0.
