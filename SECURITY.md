# Security Policy

## Reporting a vulnerability

Please report security issues privately to **partners@hazinalabs.com**. Do not open a
public issue for a suspected vulnerability.

Include, where you can:

- the version of `hazina-scan` you are running (`hazina-scan --version`),
- the command line you ran,
- what you expected versus what happened,
- and, if you can share it safely, a minimal repository that reproduces the issue.

We will acknowledge your report within **5 business days** and keep you updated as we
work through it. Once a fix is available we will coordinate a disclosure timeline with
you before any public write-up.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest `0.x` minor release | Yes |
| Older `0.x` minor releases | No |

Only the latest minor release receives security fixes. Please upgrade before reporting
an issue that may already be fixed.

## What this tool does on your machine

This section is a plain restatement of the tool's threat model, so a report is judged
against what the tool is actually meant to do.

- It reads: your working tree, dependency manifests, CI configuration files, and your
  git history, through read-only `git` commands with fixed argument lists.
- It executes `git`, always, read-only and with fixed argument lists.
- **It also executes your project's own commands, by default.** The build check
  (`--build full`, the default; `--build discover` for less; `--no-build` for none)
  installs the dependencies your manifests declare, runs your build, lists your tests and
  runs them. Those commands come from the repository being measured, so a hostile
  repository is executing code on your machine — treat it the way you would treat running
  `npm install` or `pip install -e .` in that checkout yourself, and use a disposable
  clone. What bounds it: an empty throwaway `HOME` and a private `TMPDIR`, `CI=1`, a
  `PATH` with your own directories removed, no credentials, no proxy settings and no agent
  socket; argument lists rather than a shell; a process group per command, killed whole
  when it overruns; one enforced time budget for the whole repository; and a snapshot of
  the checkout taken before anything runs and restored afterwards. The restoration is best
  effort. It is not a sandbox and does not claim to be one. **What is not bounded in this
  release:** a command's stdout and stderr are captured in full and are not size-limited,
  so a command that writes an unusually large amount of output is read to completion
  rather than truncated.
- `--no-build` switches all of that off, and then `git` is the only thing executed.
- It writes its OUTPUT only to the directory you specify (`--out`, default
  `./hazina-out`), never inside the repository being measured. The build check is the one
  exception to "nothing is written in your tree", and it writes there only what the
  project's own install and build commands write: lockfiles, dependency directories, build
  output. Those are snapshotted first and put back afterwards. Each repository's output
  folder is named after an anonymous, content-derived handle rather than its local
  directory name, and the only file that records which local path a folder came from,
  `INDEX.local.txt`, stays on your machine and is excluded from `hazina-out.zip`.
- **This tool** opens no network connection: no telemetry, no crash reporting, no version
  check, no upload of any kind. Your project's own package manager, run by the build
  check, reaches whatever index your manifests point it at — that is its traffic, not
  ours, and `--no-build` means none of it happens.
- It never collects: author names or email addresses, commit messages, branch or tag
  names, file or directory paths, environment-variable names, or credentials, keys,
  tokens or passwords. See the README for the full list of what is, and is not,
  collected, and why.

If you believe any of the above is not true of the code as shipped, that is itself worth
reporting as a security issue.
