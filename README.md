# hazina-scan

Measures a git repository and writes three small files describing its size, languages,
tests, history and ownership. It runs entirely on your machine, opens no network
connection, and the files it writes are yours to read before you decide whether to share
them.

The files hold numbers, dates, fixed labels and public technology names. They do not hold
your code, your file paths, your commit messages, or the name or address of anyone who
committed to the repository.

## What this release does and does not do

- **No build check.** A later release will optionally install the project's dependencies
  and run its own test suite to report whether the code builds. That is not in this
  release, so `--no-build` is required on every command. When the build check arrives it
  will be off by default and will say plainly what it executes before it executes it.
- **No AI of any kind.** There is no model, no inference, no API key, no prompt. Every
  number is produced by reading files and by running `git log` and its siblings. The same
  commit always produces the same answer.

## Install and run

Requires **Python 3.11 or newer** and **git**.

```bash
pipx install hazina-scan     # or: pip install hazina-scan
```

Three ways to run it:

```bash
# one repository
hazina-scan ./my-repo --no-build

# several repositories, into a directory you choose
hazina-scan ./api ./web ./jobs --no-build --out ./hazina-out

# every git repository directly inside a directory
hazina-scan --all ~/src --no-build
```

Output goes to `./hazina-out` unless `--out` says otherwise, one subdirectory per
repository, named after the repository's directory. Nothing is ever written inside the
repository being measured. Results are printed to stdout and progress to stderr, so
`hazina-scan ./my-repo --no-build > report.txt` keeps the answer and leaves the narration
on your terminal.

Useful flags:

| Flag | What it does |
| --- | --- |
| `--review` | Print every field that was emitted and its value, not just per-kind counts. |
| `--budget-seconds N` | Wall-clock budget for one repository. **Recorded, not enforced in this release**: the run reports how much of it was used, but nothing stops work when it is spent. Every lane in this release is a bounded local read; enforcement arrives with the build check. |
| `--jobs N` | How many repositories to measure at once (default 2). |
| `--no-zip` | Do not write `hazina-out.zip` beside the output directory after a multi-repository run. |
| `--version` | Print the version. |

`hazina-scan --help` lists them all.

## What it reads

- **The working tree**, one read-only pass. Files are opened, decoded leniently and read
  in bounded amounts to count non-empty lines per language, file sizes, test files,
  infrastructure files, and to parse manifests (`package.json`, `pyproject.toml`,
  `go.mod`, `Cargo.toml`, `pom.xml` and the rest) for declared dependencies, frameworks,
  package managers and linters. Vendored and generated directories are skipped.
- **The git history**, through read-only `git` commands with fixed argument lists: commit
  counts and dates, the span and recency of activity, commits per month, active days,
  merge and tag counts, how many distinct authors and how many of those are bots.
- **Licence and copyright files and the `origin` remote URL**, to work out which
  organisation appears to own the repository.

It does not import your code, resolve a dependency, install anything, or write to the
repository.

## What it executes

`git`, and nothing else. Every git invocation is read-only, uses a fixed argument list
with no shell, and runs with a child environment assembled from a short list of harmless
variables (`PATH`, `LANG`, `TZ` and similar) rather than a copy of yours — so credentials
and tokens in your environment are never handed to a subprocess.

Source files are parsed in-process with tree-sitter. Parsing is not execution: the parser
builds a syntax tree and never runs what it reads.

The build check, which *would* run a project's own install and test commands, is not in
this release.

## What leaves your machine

Nothing. There is no upload, no telemetry, no crash reporting, no version check, no
network call of any kind. The output files sit on your disk, you can read every one of
them, and sharing them is a decision you make afterwards.

## What is never collected

Six categories are excluded by design, not by configuration — the collectors never put
them in a document, and a second scrub plus a final audit refuse the write if anything
resembling them gets through anyway. That scrub's rules were derived from the output
requirement rather than from the checks they sit behind, so one oversight cannot slip past
both:

- **The names and email addresses of the people who committed.** Author identity is hashed
  with a per-run salt at the point it is parsed; only the *count* of distinct authors and
  whether each looked like a bot survives. No author's email domain is collected either.
- **Commit messages.** Only the shape of the history is counted — how many commits, when,
  how many were merges, how many follow a conventional-commit prefix. The text is never
  emitted.
- **Branch and tag names.** The tool chooses which ref to measure and records *why* in
  plain words, but never the ref's name.
- **File and directory names.** Counts and aggregates only. No path from your tree appears
  in any output, and the scrub masks paths, filenames, URLs, email addresses and object
  hashes wherever they might otherwise turn up in a free-text field.
- **Environment-variable names.** Which variables your code reads, and which your example
  file does or does not declare, are not collected — a variable name is frequently a
  service or vendor name.
- **Credentials, keys, tokens and passwords.** This tool has no credential scanner in it.
  It never searches your file contents for secret-shaped strings, so no such string is
  ever matched, counted, located or written — not the value, not a hash of it, not the
  file it lives in, not even a count of how many were found. `hardcoded_secret_hits` ships
  in the schema and is permanently null for exactly that reason: the field exists so the
  answer is stated rather than missing, and there is no code path that could ever fill it.

What *is* emitted about identity, and worth knowing before you share the files:

- **The repository's own name**, as `real_repo_name` in `measurement.json` (the CSV/JSON
  row carries only the content digest and a digest-derived handle such as
  `repo-8d456f33e1f4`), so a record can be matched back to the repository it describes.
- **Candidate owning organisations**, in `company_identity.company_candidates` — taken from
  the root licence or notice file, a copyright header that recurs across source files, the
  organisation in the `origin` URL, and an organisation namespace in a root manifest. When
  a project's copyright holder is an individual rather than a company, that individual's
  name is what the licence file says and is what appears there.
- **Public technology names** the repository declares — language, frameworks, CI system,
  package managers, linters. Facts about public technology, not about you.

Every value written is checked against a declared allowlist first; anything undeclared
stops the run rather than being written. The run ends by printing exactly what went into
the files, grouped by kind, so you can read it before deciding to share anything. Add
`--review` to see every field with its value.

## The output files

Per repository, in `<out>/<repository name>/`:

| File | What it is |
| --- | --- |
| `codebase_repos.csv` | One row, one line per repository: the flat summary — lines of code, languages, test ratios, commit counts and dates, classification. For a spreadsheet or a bulk load. |
| `codebase_repos.json` | The same row as JSON, which keeps the types — an empty CSV cell and a JSON `null` are the same fact, but only one of them survives a round trip. |
| `measurement.json` | The full record: the `tree`, `git`, `classification` and `company_identity` blocks, plus `ext_signals` holding the `history` and `structure` blocks. |

After a run over several repositories, `hazina-out.zip` is written *beside* the output
directory (pass `--no-zip` to skip it).

Throughout both documents, **null is not zero**. A field nothing could measure is null; `0`
means measured and found to be none. When the budget is enforced, in the release that adds
the build check, work it does not reach will be reported as null with a stated reason rather
than as a low number.

## Verifying the numbers

Every field has a test behind it. Purpose-built fixture repositories exercise each
collector, each threshold and each table, and the numbers are re-checked field for field
over nine real public repositories spanning seven ecosystems before a release goes out.

The detection tables in `hazina_scan/vocab.py` are data rather than logic -- a list of file
extensions or framework names is a fact about the ecosystem, not a judgement about your
repository. They are pinned value by value by the tests, because every entry decides a
published number, and widening one is always a deliberate change.

## Project files

- [SECURITY.md](SECURITY.md) — how to report a vulnerability, supported versions, and a
  restatement of what this tool does and does not do on your machine.
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, running the tests, style, commit
  conventions, and a "For maintainers" section covering releases and repository
  settings.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — the Contributor Covenant, and how to report
  a concern.
- [CHANGELOG.md](CHANGELOG.md) — what changed in each release.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
