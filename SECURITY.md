# Security Policy

## Reporting a vulnerability

Please report security issues privately to **msandeep85@gmail.com**. Do not open a
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
- It executes **only `git`** in this release. There is no build check in this release —
  `--no-build` is required on every command. A later release adds an **opt-in** build
  check that, only when a user asks for it on a given run, installs the project's own
  dependencies and runs the project's own build and test commands. That check does not
  exist in this release; nothing your project's own tooling would run is executed here.
- It writes only to the output directory you specify (`--out`, default `./hazina-out`).
  It never writes inside the repository being measured.
- It opens no network connection. There is no telemetry, no crash reporting, no version
  check, no upload of any kind.
- It never collects: author names or email addresses, commit messages, branch or tag
  names, file or directory paths, environment-variable names, or credentials, keys,
  tokens or passwords. See the README for the full list of what is, and is not,
  collected, and why.

If you believe any of the above is not true of the code as shipped, that is itself worth
reporting as a security issue.
