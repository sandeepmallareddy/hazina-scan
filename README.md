# hazina-scan

Measures your code repositories and writes one zip of numbers you can share with
[Hazina Labs](https://hazinalabs.com).

It runs on your machine. It sends nothing anywhere. It uses no AI. The output contains no
source code, no file names, no commit messages and no author names.

Questions: **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**

## 1. Install

Needs Python 3.11+ and git.

```bash
pipx install "git+https://github.com/sandeepmallareddy/hazina-scan.git@v0.3.1"
```

Or download the wheel from the
[latest release](https://github.com/sandeepmallareddy/hazina-scan/releases/latest) and run
`pipx install ./hazina_scan-0.3.1-py3-none-any.whl`.

## 2. Run

Make a throwaway clone of each repository, then scan it:

```bash
git clone /path/to/your-repo /tmp/scan/your-repo
hazina-scan /tmp/scan/your-repo
```

Several repositories:

```bash
hazina-scan /tmp/scan/api /tmp/scan/web /tmp/scan/jobs
hazina-scan --all /tmp/scan          # every repository inside a folder
```

**Use a throwaway clone.** By default the scan installs, builds and tests your project with
your project's own commands, and that changes the checkout (lockfiles, dependency folders,
build output).

## 3. Send

Every run writes one file: **`hazina-out.zip`**. Read the review printed at the end of the
run, then email the zip to **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**.

Nothing is sent unless you send it.

---

## Which mode to run

Run the first one that works for you.

| Priority | Command | What it does | Time |
|---|---|---|---|
| **1. Full build** (default) | `hazina-scan <repo>` | Installs, builds, **runs your tests**, reads coverage. | minutes |
| 2. Discover | `hazina-scan <repo> --build discover` | Installs, builds, **lists** your tests. Does not run them. | a minute or two |
| 3. Scan only | `hazina-scan <repo> --no-build` | Reads files and git history. Runs nothing of yours. | seconds |

The full build gives the most useful result, because it shows that the code installs, builds
and passes its own tests. Use **discover** if the test suite is too slow or needs services you
cannot start. Use **scan only** if you cannot run project code on this machine, or the project
needs a private package registry.

If a full build runs out of time, the scan finishes at the discover level by itself and says
so. A result is never marked down because a clock ran out.

Builds are supported for Node (npm, pnpm, yarn), Python, Go, Rust, Java (Maven, Gradle), .NET,
Ruby and PHP. In a monorepo the 8 largest projects are built; the rest are listed as skipped.

## What a run looks like

```text
$ hazina-scan /tmp/scan/requests
[plan] 2 project roots (python), 109 files in 21 directories scanned
[plan] this looks like a 3 minute run against a 150 minute budget ...
[build] ran at level full; observed_runnability=4, discover_runnability=3

psf/requests  Python  9,841 LOC  6,494 commits  824 authors  tests: pytest  ci: yes  build: ok

REVIEW -- this is everything the output files contain ...
NUMBERS -- 130
BOOLEANS -- 52
CLOSED-VOCABULARY VALUES -- 50
NOT COLLECTED BY POLICY -- 35 declared fields, never filled in
    commit_subjects: commit subjects and commit messages are not collected
    path: the names of files and directories are not collected
    ...

requests  ->  repo-6f0519253f51: measured
wrote /tmp/scan/hazina-out.zip
index (local only, not in the zip): /tmp/scan/hazina-out/INDEX.local.txt
```

That run took 79 seconds. Add `--review` to print every field and its value.

## What is in the zip

One folder per repository. Each folder holds three files:

| File | Contents |
|---|---|
| `codebase_repos.json` | One row: size, languages, tests, CI, commit and author counts, dates, `build_ok`, `testable_at_head`. |
| `codebase_repos.csv` | The same row as CSV. |
| `measurement.json` | The full record, including the build result and coverage. |

**On your machine** the folders carry your repository's folder name (`hazina-out/requests/`), so
you can find things. **Inside the zip** each folder is renamed to an anonymous handle
(`repo-6f0519253f51`). Your folder names do not travel.

`INDEX.local.txt` lists both names side by side. It stays on your machine and is never put in
the zip.

`null` means *not measured*. `0` means *measured, and the answer is none*. A missing language
runtime or an unreachable registry is reported as not measured. It never counts against your
code.

## What leaves your machine

Nothing, until you send the zip.

The tool has no upload, no telemetry and no update check. It imports no networking library.
When the build runs, your own package manager contacts your own registries, exactly as it does
on a developer's laptop.

## What is never collected

- Source code.
- File and directory names.
- Commit messages.
- Branch and tag names.
- Names and email addresses of the people who committed. Only a *count* of authors is kept.
- Environment-variable names.
- Credentials. The tool has no secret scanner. It never looks for them.

Every value is checked against a fixed list of allowed fields before it is written. Anything
else stops the run.

## What is included about identity

- **The repository's name** (`owner/name`, from the `origin` remote), so a result can be
  matched to its repository. Absent if there is no remote.
- **The company that owns the code**, read from your licence file, copyright headers, the
  remote URL and package namespaces. At most three candidates. If your licence names a person,
  that name appears.
- **Public technology names**: languages, frameworks, CI system, package managers.

Nothing else is named. To withhold a field, delete it from the JSON before you send the zip and
tell us which one.

## How the build is contained

- Commands run with an empty temporary home folder, no credentials, no proxy settings and
  `CI=1`.
- Your home directories are removed from `PATH`. Runtimes installed under your home folder
  (nvm, pyenv, rustup) are not used. System-wide ones are.
- Commands are argument lists, never shell strings. A command that overruns its limit is killed
  with all its child processes.
- One time budget covers the whole repository (default 150 minutes; the build gets 30).
- Files the install rewrites are put back afterwards. This is best effort, not a sandbox. That
  is why you use a throwaway clone.
- Command output is captured in full and is not size-limited in this release.

`--no-build` turns all of this off. Then `git` is the only program the tool runs.

## Options

| Flag | Meaning |
|---|---|
| `--all DIR` | Scan every git repository directly inside `DIR`. |
| `--out DIR` | Where to write results (default `./hazina-out`). Must be outside the repositories. |
| `--build {full,discover,none}` | Build level. Default `full`. |
| `--no-build` | Same as `--build none`. |
| `--review` | Print every field and its value. |
| `--jobs N` | Repositories to scan at once (default 2). |
| `--no-zip` | Do not write `hazina-out.zip`. |
| `--budget-seconds N` | Time budget per repository (default 9000). |
| `--build-budget-seconds N` | The build's share of that budget (default 1800). |
| `--full-attempt-seconds N` | How long a full build may run before falling back to discover (default 900). |
| `--timeout-build N` | Limit for one build command (default 900). |
| `--max-build-projects N` | Projects built per repository (default 8). |

Exit codes: `0` success. `1` a repository failed; the others still ran. `2` usage error.

## Problems

| You see | Do this |
|---|---|
| `error: not a git repository` | Point at the repository's top folder, the one containing `.git`. |
| `error: --out would write inside the repository` | Pass `--out` a folder outside the repository. |
| A toolchain is reported missing, but it is installed | It is installed under your home folder. Install it system-wide, or run `--no-build`. It does not count against your code. |
| The project needs a private registry | The build runs without your credentials on purpose. Run `--no-build`. |
| `status: partial` | One step could not finish. `skip_reason` says which. The other numbers are valid. |
| Anything else | Email the last 20 lines of output, `hazina-scan --version`, your OS and Python version to [partners@hazinalabs.com](mailto:partners@hazinalabs.com). |

Linux and macOS are fully supported. On Windows the scan works; the build is best effort.

## Check it yourself

```bash
grep -rnE '^\s*(import|from)\s+(requests|urllib|httpx|http|socket)' hazina_scan/   # no output: no networking
grep -rn 'shell=True' hazina_scan/                                                  # no output: no shell
less hazina_scan/schema.py                                                          # every field that can be written
sha256sum -c SHA256SUMS --ignore-missing                                            # verify a release download
```

---

[SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) ·
[CHANGELOG.md](CHANGELOG.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) ·
Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE))

**Hazina Labs** · [hazinalabs.com](https://hazinalabs.com) ·
[partners@hazinalabs.com](mailto:partners@hazinalabs.com)
