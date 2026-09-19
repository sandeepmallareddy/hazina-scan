# Contributing to hazina-scan

Thanks for your interest in improving hazina-scan. This document covers setup,
running the tests, style, and commit conventions. It also has a "For maintainers"
section at the end.

## Setup

Requires Python 3.11+ and `git`.

```bash
python -m venv .venv
source .venv/bin/activate       # or .venv\Scripts\activate on Windows
pip install -e '.[dev]'
pre-commit install
```

`pip install -e '.[dev]'` installs the package in editable mode plus the development
tools: `pytest`, `ruff`, `pre-commit`, `build`, and `pip-audit`.

## Running the tests

```bash
pytest -q
```

Fixture repositories are built on the fly under `tmp_path`; nothing here depends on
network access or an external service. Every collector, threshold, and output field has
a test behind it — if you add or change a field, add or change the test that pins it.

## Style

This project uses [ruff](https://docs.astral.sh/ruff/) for both linting and formatting:

```bash
ruff check .
ruff format .
```

Both are also enforced by the pre-commit hooks (see below), so `pre-commit install`
once and you won't need to remember to run them by hand.

## Commit messages

Commit messages use a short conventional prefix:

- `feat:` a new capability
- `fix:` a bug fix
- `docs:` documentation only
- `test:` tests only, no behavior change
- `refactor:` a code change that is not a fix or a feature
- `chore:` everything else (dependencies, tooling, packaging)

A sign-off (DCO-style `Signed-off-by:` trailer) is **not** required.

## Opening a pull request

- Keep the change focused; unrelated formatting or renames make a diff hard to review.
- Add or update a test for any behavior change.
- Run `pytest -q` and `ruff check .` locally before opening the PR — pre-commit will
  catch the same issues, but it's faster to find them yourself first.

## For maintainers

### Release steps

1. Bump `__version__` in `hazina_scan/__init__.py`.
2. Add a new section to `CHANGELOG.md` describing what changed, under
   `## [X.Y.Z] - YYYY-MM-DD`, following [Keep a Changelog](https://keepachangelog.com/).
3. Commit, tag the release `vX.Y.Z`, and push the tag.

### Repository settings to apply by hand

These are GitHub repository settings, not files in this repository, so they have to be
applied once by a maintainer with admin access:

- Branch protection on `main`: require the CI check to pass before merging, and require
  pull requests (no direct pushes to `main`).
- No force-push to `main`.
- Require signed tags for releases.

### Local scratch directories

Local scratch directories used during development are ignored through
`.git/info/exclude`, not `.gitignore` — they are a per-clone convenience, not something
every contributor needs to configure or that belongs in the shared ignore file.
