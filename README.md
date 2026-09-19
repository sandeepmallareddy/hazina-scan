# hazina-scan

**A free, local scan of your code repositories, from [Hazina Labs](https://hazinalabs.com).**

`hazina-scan` measures a git repository — its size, languages, tests, history, build health
and ownership — and writes three small files of numbers. It runs entirely on your machine,
opens no network connection of its own, uses no AI, and sends nothing to anyone. You read
what it produced, and only then decide whether to share it.

Questions at any point: **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**.

---

## Contents

- [Why this exists](#why-this-exists)
- [At a glance](#at-a-glance)
- [Quick start](#quick-start)
- [Choosing a mode: read-only or with a build check](#choosing-a-mode-read-only-or-with-a-build-check)
- [What you will see](#what-you-will-see)
- [Sharing the results with Hazina Labs](#sharing-the-results-with-hazina-labs)
- [What it reads](#what-it-reads)
- [What it executes](#what-it-executes)
- [What leaves your machine](#what-leaves-your-machine)
- [What is never collected](#what-is-never-collected)
- [What is emitted about identity](#what-is-emitted-about-identity)
- [The output files](#the-output-files)
- [All options](#all-options)
- [How long it takes](#how-long-it-takes)
- [Requirements and platform support](#requirements-and-platform-support)
- [Troubleshooting](#troubleshooting)
- [Verify it yourself](#verify-it-yourself)
- [Project files, licence, contact](#project-files-licence-contact)

---

## Why this exists

Hazina Labs helps organisations earn new revenue from the knowledge their teams have built —
code, engineering history and more — by licensing it to AI labs, while it stays yours: a
licence, not a sale. The process is described at [hazinalabs.com](https://hazinalabs.com):

1. Sign an NDA.
2. **Run a quick scan — free, on your machine.** ← *this tool*
3. Run a deeper analysis.
4. Get your estimate.
5. Accept or decline.

Step 2 exists so that neither side spends time on step 3 for a repository that is not a fit.
The scan answers the first-order questions — how much code is there, how actively was it
developed and by how many people, does it have tests and CI, does it build and do its tests
run — without anyone outside your organisation seeing a line of your source.

You do not need to have spoken to us to run it. It is open source, and useful on its own as a
quick health check of a repository.

## At a glance

| | |
|---|---|
| **Runs where** | On your machine, against your checkout. |
| **Network** | None of its own. No upload, telemetry, version check or crash report. |
| **AI** | None. No model, no API key, no prompt. The same commit always gives the same numbers. |
| **Output** | Three small files per repository (about 10 KB): numbers, dates, fixed labels, public technology names. |
| **Never in the output** | Source code, file or directory names, commit messages, branch or tag names, author names or emails, environment-variable names, credentials. |
| **You decide** | The run ends by printing exactly what is in the files. Nothing is shared unless you send it. |
| **Licence** | Apache-2.0. Read every line before you run it. |

## Quick start

You need **Python 3.11+** and **git**.

**1. Install.** Download the wheel from the
[latest release](https://github.com/sandeepmallareddy/hazina-scan/releases/latest) and install
it with [pipx](https://pipx.pypa.io) (or pip):

```bash
pipx install ./hazina_scan-0.2.0-py3-none-any.whl
hazina-scan --version
```

Or install straight from the repository:

```bash
pipx install "git+https://github.com/sandeepmallareddy/hazina-scan.git@v0.2.0"
```

Or run it from a clone without installing anything globally:

```bash
git clone https://github.com/sandeepmallareddy/hazina-scan.git
cd hazina-scan && python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/hazina-scan --version
```

**2. Scan.** The fastest, safest first run reads only — nothing of yours is executed:

```bash
hazina-scan /path/to/your-repo --no-build
```

Several repositories at once, or every repository inside a folder:

```bash
hazina-scan ./api ./web ./jobs --no-build
hazina-scan --all ~/code --no-build
```

**3. Read the review** printed at the end (add `--review` to see every field and value), and
look at the files in `./hazina-out/`.

**4. If you are happy to, [share the results](#sharing-the-results-with-hazina-labs).**

## Choosing a mode: read-only or with a build check

| | `--no-build` | `--build discover` | `--build full` (default) |
|---|---|---|---|
| Executes | `git` only | your install and build commands; asks your test runner to *list* tests | the same, then *runs* your test suite and reads coverage |
| Touches your checkout | no | yes | yes |
| Typical time | seconds | a minute or two | a few minutes, bounded by a budget |
| Tells you | size, languages, tests present, CI, history, ownership | + does it install and build, are tests discoverable | + do the tests run, how many pass, coverage |

**The build check runs your project's own commands** — `npm install`, `pip install`,
`go build`, `cargo test`, your test runner — the same things your CI runs. It therefore
**modifies the checkout** (lockfiles, dependency folders, build output). Use a clone you can
throw away:

```bash
git clone /path/to/your-repo /tmp/your-repo-scan
hazina-scan /tmp/your-repo-scan            # --build full is the default
rm -rf /tmp/your-repo-scan
```

If a full run does not finish inside its share of the time budget, the measurement is
completed at the `discover` level and says so; nothing is reported as a low score because a
clock ran out.

Supported build ecosystems: Node (npm, pnpm, yarn), Python, Go, Rust, Java (Maven, Gradle,
including wrapper scripts), .NET, Ruby, PHP. Monorepos are handled: up to 24 project roots are
found and the 8 largest are built (`--max-build-projects`); the rest are reported as skipped.

## What you will see

A run on a small public repository, read-only:

```text
$ hazina-scan ./itsdangerous --no-build
[plan] 1 project root (python), 40 files in 7 directories scanned
[plan] this looks like a 5 second run against a 150 minute budget ...
...
pallets/itsdangerous  Python  1,373 LOC  677 commits  41 authors  tests: pytest  ci: yes  build: not run

REVIEW -- this is everything the output files contain. They were written to this
machine and nothing has been sent anywhere ...

NUMBERS -- 130
BOOLEANS -- 40
CLOSED-VOCABULARY VALUES -- 41
    GitHub Actions, Pallets, Python, README.md, Shell, backend, confident, pytest, pytest-cov, ...
CONTENT DIGEST AND DISPLAY HANDLE -- 3
TIMESTAMPS -- 6
THIS TOOL'S OWN STATUS NOTES -- 2
NOT COLLECTED BY POLICY -- 35 declared fields, never filled in
    commit_subjects: commit subjects and commit messages are not collected
    path: the names of files and directories are not collected
    hardcoded_secret_hits: this tool never searches a repository for its own credentials ...
    ...
  wrote hazina-out/itsdangerous/codebase_repos.json
  wrote hazina-out/itsdangerous/codebase_repos.csv
  wrote hazina-out/itsdangerous/measurement.json
```

The one-line summary is for you. The **review** is the point: it lists every kind of value in
the files, every piece of free text verbatim, and every field that is deliberately left empty
with the reason. `--review` prints each field with its value.

Progress and the per-step timing table go to stderr and the results to stdout, so
`hazina-scan ./repo --no-build > report.txt` keeps the answer and leaves the narration on
your terminal.

## Sharing the results with Hazina Labs

Sharing is a separate act that you perform — the tool has no way to send anything.

- **One repository:** send the folder `hazina-out/<repo-name>/` (three files), zipped if you
  like.
- **Several repositories:** the run writes `hazina-out.zip` next to the output folder; send
  that one file.

Email it to **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**, or use whatever
channel we agreed with you. If you would rather remove a field first, tell us which one — the
files are plain JSON and CSV, and nothing in the process depends on a field you are not
comfortable sharing.

If you have not spoken to us yet and would like to, the same address works, and
[hazinalabs.com](https://hazinalabs.com) explains how partnerships, ownership and
confidentiality work (you keep ownership; an NDA from the first step; your organisation is
never named without your consent).

## What it reads

- **The working tree**, in one read-only pass. Files are opened and read in bounded amounts to
  count non-empty lines per language, file sizes, test files and infrastructure files, and to
  parse manifests (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml` and the
  rest) for declared dependencies, frameworks, package managers and linters. Vendored and
  generated directories are skipped.
- **CI configuration**, parsed as YAML, to answer whether CI exists and whether it runs tests,
  lint and type checks.
- **The git history**, through read-only `git` commands with fixed argument lists: commit
  counts and dates, the span and recency of activity, commits per month, active days, merge
  and tag counts, how many distinct authors there are and how many look like bots.
- **Source structure**, by parsing source files in-process with tree-sitter to see how
  decision logic and error handling are distributed. Parsing builds a syntax tree; it never
  runs what it reads.
- **Licence and copyright files, the `origin` remote URL and manifest namespaces**, to work
  out which organisation appears to own the repository.
- **Every file's bytes, once, to compute a content digest** (a SHA-256 over the tree). That
  includes any `.env` or key file that happens to be in the checkout: the bytes are hashed and
  nothing else is done with them — no value, length or name is kept.

Reading does not import your code, resolve a dependency, install anything, or write to the
repository.

## What it executes

**Always: `git`**, read-only, with a fixed argument list and never a shell. It runs with an
environment assembled from a short list of harmless variables (`PATH`, `LANG`, `TZ` and
similar) rather than a copy of yours, so tokens in your shell are not handed to a subprocess.

**With the build check: your project's own install, build and test commands.** What bounds
that:

- Every command runs with an **empty throwaway `HOME`** and a private `TMPDIR`, `CI=1`, no
  credentials, no proxy settings, no SSH agent socket, and a `PATH` with your own home
  directories removed. Language runtimes installed under your home directory (nvm, pyenv,
  rustup, asdf) are therefore not selected by runtime resolution; system-wide ones are. If the
  version your project asks for is not available, the result is reported as *not measured on
  this machine* — it never counts against your code.
- Argument lists only, never a shell string. Each command starts in its own process group,
  and one that overruns its time limit has **the whole group killed**, not just the process
  that was launched.
- One time budget covers the whole repository and the build check has a reserved share of it.
- The checkout is snapshotted before anything runs and restored afterwards, including after a
  failure: files an installer rewrote are put back and artefacts it created are removed. That
  restoration is best effort. It is not a sandbox and does not claim to be one — which is why
  we ask you to use a disposable clone.
- Not bounded in this release: a command's output is captured in full rather than truncated.

Your package manager, when the build check runs it, contacts whatever registry your manifests
point at. That is your build's traffic, not this tool's.

`--no-build` switches all of this off, and then `git` really is the only thing executed.

## What leaves your machine

Nothing. There is no upload, no telemetry, no crash reporting, no update check, no network
call of any kind in this tool. The output files sit on your disk; you can read every one of
them; sharing them is a decision you make afterwards.

## What is never collected

Six categories are excluded by design, not by configuration. The collectors never put them in
a document, every value is checked against a declared allowlist before it is written (anything
undeclared stops the run instead of being written), and a separate scrub and final audit refuse
the write if anything resembling them gets through anyway.

- **Names and email addresses of the people who committed.** Author identity is hashed with a
  per-run salt at the moment it is parsed; only the *count* of distinct authors and whether
  each looks like a bot survives. No email domain is collected either.
- **Commit messages.** Only the shape of the history is counted — how many commits, when, how
  many merges, how many follow a conventional prefix. The text is never emitted.
- **Branch and tag names.** The tool chooses which ref to measure and records *why* in plain
  words, never the ref's name.
- **File and directory names.** Counts and aggregates only. No path from your tree appears in
  any output.
- **Environment-variable names.** A variable name is frequently a service or vendor name.
- **Credentials, keys, tokens and passwords.** There is no credential scanner in this tool. It
  never searches your files for secret-shaped strings, so none is ever matched, counted,
  located or written. The schema keeps a `hardcoded_secret_hits` field that is permanently
  null, so that the answer is stated rather than missing.

## What is emitted about identity

Worth knowing before you share the files:

- **The repository's own name** (`owner/name`, read from the `origin` remote), as
  `real_repo_name` in `measurement.json`, so a record can be matched to the repository it
  describes. It is absent when there is no remote or the remote is a local path. The flat row
  (`codebase_repos.*`) carries only the content digest and a digest-derived handle such as
  `repo-68c3409112d1`.
- **Candidate owning organisations**, in `company_identity` — at most three, each tagged with
  where it was read from: the root licence or notice file, a copyright header that recurs
  across source files, the organisation in the `origin` URL, or an organisation namespace in
  a root manifest. No score or count accompanies a name. If your licence names an individual
  as copyright holder, that is what appears. If nothing is found, the answer is `none`, not a
  guess.
- **Public technology names** your repository declares — languages, frameworks, CI system,
  package managers, test frameworks, linters. Facts about public technology, not about you.

## The output files

Per repository, in `<out>/<repository folder name>/`:

| File | What it is |
|---|---|
| `codebase_repos.json` | One flat row: lines of code, languages, test counts and ratios, CI flags, commit and author counts, first and last commit dates, activity calendar, classification, `build_ok`, `testable_at_head`. |
| `codebase_repos.csv` | The same row as CSV, for a spreadsheet. |
| `measurement.json` | The full record: `tree`, `git`, `classification`, `company_identity`, and `ext_signals` holding `structure`, `history` and `build`. |

A few fields, to give the flavour:

```json
{
  "fake_repo_name": "repo-68c3409112d1",
  "status": "measured",
  "loc": 1373,
  "languages": {"Python": 1359, "Shell": 14},
  "test_framework": ["pytest"],
  "ci_runs_tests": true,
  "commit_count": 677,
  "author_count": 41,
  "span_days": 5104,
  "build_ok": null,
  "testable_at_head": null
}
```

**Null is not zero.** A field nothing could measure is `null`; `0` means measured and found to
be none. `build_ok` is `null` above because the run used `--no-build`. Work the time budget did
not reach is `null` with a stated reason, and so is a failure that was this machine's rather
than the repository's — a missing language runtime or an unreachable package registry never
counts against the code. `status` is `measured` when every step ran and `partial` otherwise,
with `skip_reason` saying why.

`ext_signals.build` records which build level ran and which was asked for, whether install and
build succeeded, whether tests were found and ran, coverage, the runtime the project asked for
and the one it got, and two small indices (`discover_runnability` 0–3, `observed_runnability`
0–4) that summarise what was actually executed.

## All options

| Flag | What it does |
|---|---|
| `REPO ...` | One or more repositories to measure. Combine freely with `--all`. |
| `--all DIR` | Measure every immediate subdirectory of `DIR` that is a git repository. |
| `--out DIR` | Output directory (default `./hazina-out`). Each repository gets `<out>/<its folder name>/`; a repeated name is suffixed `-2`, `-3`. **Must be outside the repositories being scanned.** |
| `--no-build` | Measure without executing anything of the repository's own. |
| `--build {none,discover,full}` | How much of the build check to run (default `full`). See [Choosing a mode](#choosing-a-mode-read-only-or-with-a-build-check). |
| `--review` | Print every emitted field and its value, not just per-kind counts. |
| `--jobs N` | How many repositories to measure at once (default 2). |
| `--no-zip` | Do not write `hazina-out.zip` after a multi-repository run. |
| `--budget-seconds N` | Wall-clock budget for one repository (default 9000). The git-backed steps and the build check draw their limits from it; the in-process readers are bounded by their own file and size caps instead. |
| `--build-budget-seconds N` | The build check's reserved share of that budget (default 1800). |
| `--full-attempt-seconds N` | How long a `--build full` attempt may run before the measurement is finished at `discover` (default 900). |
| `--timeout-build N` | Limit for one command inside the build check (default 900). |
| `--max-build-projects N` | How many project roots inside one repository the build check may reach (default 8). |
| `--version`, `--help` | The version; the full help. |

Exit codes: `0` every repository produced files; `1` at least one repository failed (the
others still ran, and each failure is named); `2` a usage error — a path that is not a git
repository, an output directory inside a scanned repository, no repositories found.

## How long it takes

Read-only (`--no-build`) is dominated by walking the git history, so it scales with commit
count more than code size. On a laptop: a 20k-line Go service with 2,000 commits takes about
2 seconds; a 100k-line Python project with 7,700 commits about 25 seconds; a 160k-line Rust
workspace with 4,700 commits about 16 seconds.

The build check takes as long as your own install and tests take — seconds for a small
library, a few minutes for a typical service — and never longer than its budget. The run
prints an estimate before it starts and a per-step timing table when it ends.

## Requirements and platform support

- **Python 3.11 or newer** and **git**. Python dependencies (`tree-sitter`,
  `tree-sitter-language-pack`, `PyYAML`) install automatically and are pinned.
- For the build check, the toolchain your project already uses, installed system-wide.
- **Linux and macOS** are fully supported and tested on every change. On **Windows**, the
  read-only scan works; the build check is best-effort in this release.

## Troubleshooting

- **`error: not a git repository: <path>`** — point it at the repository's top-level folder
  (the one containing `.git`), not a subfolder.
- **`error: --out would write inside the repository being measured`** — output written inside the repository would
  change its content digest. Pass `--out` somewhere else, or run from outside the repository.
- **The build check reports a toolchain as missing, but it is installed.** It is probably
  installed under your home directory (nvm, pyenv, rustup). Those are deliberately not used.
  Either install the runtime system-wide or use `--no-build`; the result is reported as *not
  measured here*, not as a failure of your project.
- **`status: partial`** — a step could not finish; `skip_reason` and the notes in
  `measurement.json` say which and why. The rest of the numbers are valid.
- **It needs a private package registry.** The build check runs without your credentials on
  purpose, so a private registry is unreachable. The install is reported as not measured for
  that reason. `--no-build` is the right mode for such repositories.
- **Anything else:** send the last 20 lines of output, your `hazina-scan --version`, OS and
  Python version to [partners@hazinalabs.com](mailto:partners@hazinalabs.com).

## Verify it yourself

Every claim above is a fact about code you can read. A few one-liners, run from a clone of
this repository:

```bash
# no networking library is imported anywhere              (expect: no output)
grep -rnE '^\s*(import|from)\s+(requests|urllib|httpx|http|socket|aiohttp)' hazina_scan/

# no command is ever handed to a shell                    (expect: no output)
grep -rn 'shell=True' hazina_scan/

# every subprocess is started in one module, hazina_scan/env.py
grep -rn 'subprocess.Popen\|subprocess.run' hazina_scan/

# every field that may be written, and the kind of value it may hold
less hazina_scan/schema.py

# the test suite (no network, no external tools needed)
pip install -e '.[dev]' && pytest -q
```

Release downloads ship with a `SHA256SUMS` file:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

Every field has tests behind it: purpose-built fixture repositories exercise each collector,
threshold and table, and the numbers are re-checked field by field against real public
repositories across several ecosystems before each release.

## Project files, licence, contact

- [SECURITY.md](SECURITY.md) — reporting a vulnerability, and a plain restatement of what the
  tool does on your machine.
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, tests, style, releases.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md)
- Licence: Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

**Hazina Labs** · [hazinalabs.com](https://hazinalabs.com) ·
[partners@hazinalabs.com](mailto:partners@hazinalabs.com)
