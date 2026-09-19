# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **No build check in this release.** `--no-build` is required on every command. A
  later release adds an opt-in build check that installs and runs a project's own
  build and test commands; that is not part of this release.
