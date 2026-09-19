"""Turn a test runner's own output into plain pass/fail/skip counts.

A build step can install cleanly and still tell us nothing about the code it built unless we
go read what the runner printed (or, for JUnit-style tooling, what it wrote to disk). Every
function below is scoped to exactly one runner's vocabulary -- pytest's "3 passed, 1 failed",
Mocha's "2 passing", cargo's "test result: ok. 4 passed; 0 failed; 1 ignored", and so on -- and
returns only the keys that vocabulary actually gave us. A key that the text never mentioned is
left out of the dict rather than defaulted to zero, because a runner that never printed a
skipped count is not the same claim as a runner that printed "0 skipped": the first is silence,
the second is a fact.

None of this looks at a filename or an import statement. "There is a file named test_thing.py"
and "a runner ran and reported N passing tests" are different claims, and only the second one
belongs here.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

__all__ = [
    "parse_pytest",
    "parse_jest",
    "parse_vitest",
    "parse_mocha",
    "parse_cargo",
    "parse_go_json",
    "parse_dotnet",
    "parse_phpunit",
    "parse_rspec",
    "junit_counts",
    "count_listing",
]


def parse_pytest(text: str) -> dict:
    """Read pytest's own summary line(s) for a collected count and per-outcome totals.

    pytest announces how many items it collected near the top of a run and a separate outcome
    tally ("3 passed, 1 failed in 0.42s") near the bottom, so the two are read from different
    slices of the same text: the collection count can appear anywhere, while the outcome tally
    is searched for only in the tail, where pytest actually prints its final line.
    """
    out: dict = {}
    collected = re.search(r"(\d+) tests? collected", text) or re.search(
        r"collected (\d+) items?", text
    )
    if collected:
        out["collected"] = int(collected.group(1))
    if re.search(r"no tests ran|collected 0 items", text):
        out.setdefault("collected", 0)
    tail = text[-6000:]
    for key, pattern in (
        ("passed", r"(\d+) passed"),
        ("failed", r"(\d+) failed"),
        ("errored", r"(\d+) errors?"),
        ("skipped", r"(\d+) skipped"),
    ):
        found = re.search(pattern, tail)
        if found:
            out[key] = int(found.group(1))
    return out


def parse_jest(text: str) -> dict:
    """Read Jest's "Tests:" summary line, which packs every outcome onto one row."""
    out: dict = {}
    line = re.search(r"^Tests:\s+(.+)$", text, re.M)
    if not line:
        return out
    for key, pattern in (
        ("failed", r"(\d+) failed"),
        ("passed", r"(\d+) passed"),
        ("skipped", r"(\d+) skipped"),
        ("collected", r"(\d+) total"),
    ):
        found = re.search(pattern, line.group(1))
        if found:
            out[key] = int(found.group(1))
    return out


def parse_vitest(text: str) -> dict:
    """Read Vitest's "Tests" summary row and derive a total, which Vitest itself omits."""
    out: dict = {}
    line = re.search(r"^\s*Tests\s+(.+?)\s*$", text, re.M)
    if not line:
        return out
    for key, pattern in (
        ("failed", r"(\d+) failed"),
        ("passed", r"(\d+) passed"),
        ("skipped", r"(\d+) skipped"),
    ):
        found = re.search(pattern, line.group(1))
        if found:
            out[key] = int(found.group(1))
    if out:
        out["collected"] = sum(out.values())
    return out


def parse_mocha(text: str) -> dict:
    """Read Mocha's "N passing" / "N failing" / "N pending" lines and sum them for a total."""
    out: dict = {}
    for key, pattern in (
        ("passed", r"(\d+) passing"),
        ("failed", r"(\d+) failing"),
        ("skipped", r"(\d+) pending"),
    ):
        found = re.search(pattern, text)
        if found:
            out[key] = int(found.group(1))
    if out:
        out["collected"] = sum(out.values())
    return out


def parse_cargo(text: str) -> dict:
    """Sum cargo's per-binary "test result:" lines; a workspace prints one line per crate."""
    total = {"passed": 0, "failed": 0, "skipped": 0}
    matched_any = False
    for found in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
        matched_any = True
        total["passed"] += int(found.group(1))
        total["failed"] += int(found.group(2))
        total["skipped"] += int(found.group(3))
    if not matched_any:
        return {}
    total["collected"] = sum(total.values())
    return total


def parse_go_json(text: str) -> dict:
    """Tally terminal pass/fail/skip actions from `go test -json` output, one action per test.

    The stream also carries package-scoped events with no `Test` field attached (build output,
    package-level pass/fail); those describe the package, not a test case, and are skipped so
    they cannot inflate the count.
    """
    passed = failed = skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not event.get("Test"):
            continue
        action = event.get("Action")
        if action == "pass":
            passed += 1
        elif action == "fail":
            failed += 1
        elif action == "skip":
            skipped += 1
    if not (passed or failed or skipped):
        return {}
    return {
        "collected": passed + failed + skipped,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }


def parse_dotnet(text: str) -> dict:
    """Read `dotnet test`'s trailer, where each outcome and the grand total get their own line."""
    out: dict = {}
    for key, pattern in (
        ("failed", r"Failed!?:\s+(\d+)"),
        ("passed", r"Passed!?:\s+(\d+)"),
        ("skipped", r"Skipped!?:\s+(\d+)"),
        ("collected", r"Total:\s+(\d+)"),
    ):
        found = re.search(pattern, text)
        if found:
            out[key] = int(found.group(1))
    return out


def parse_phpunit(text: str) -> dict:
    """Read PHPUnit's summary, which has a short all-green form and a longer form when it isn't.

    An all-passing run prints only "OK (N tests, ...)" with no per-outcome breakdown, so that
    case is handled first and short-circuits the rest. Anything else is read from the "Tests:"
    line plus whichever of Failures/Errors/Skipped PHPUnit chose to print, and passed is
    whatever is left over after those are subtracted from the total.
    """
    all_ok = re.search(r"OK \((\d+) tests?", text)
    if all_ok:
        count = int(all_ok.group(1))
        return {"collected": count, "passed": count, "failed": 0}
    totals = re.search(r"Tests: (\d+)", text)
    if not totals:
        return {}
    out: dict = {"collected": int(totals.group(1))}
    for key, pattern in (
        ("failed", r"Failures: (\d+)"),
        ("errored", r"Errors: (\d+)"),
        ("skipped", r"Skipped: (\d+)"),
    ):
        found = re.search(pattern, text)
        if found:
            out[key] = int(found.group(1))
    out["passed"] = max(
        0, out["collected"] - out.get("failed", 0) - out.get("errored", 0) - out.get("skipped", 0)
    )
    return out


def parse_rspec(text: str) -> dict:
    """Read RSpec's one-line summary: "N examples, M failures[, K pending]"."""
    found = re.search(r"(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", text)
    if not found:
        return {}
    total = int(found.group(1))
    failed = int(found.group(2))
    pending = int(found.group(3) or 0)
    return {
        "collected": total,
        "failed": failed,
        "skipped": pending,
        "passed": max(0, total - failed - pending),
    }


def junit_counts(root: Path, patterns: tuple[str, ...]) -> dict:
    """Add up every `<testsuite>` matched by `patterns` under `root` into one outcome dict.

    A JUnit-style report can be one `<testsuite>` at the document root or a `<testsuites>`
    wrapper holding several, so both shapes are normalised to a flat list of suites before their
    `tests`/`failures`/`errors`/`skipped` attributes are summed. A file that does not parse as
    XML is skipped rather than treated as a zero-test file, and an empty result (nothing found
    or nothing parsed) is reported as an empty dict, the same silence convention the text
    parsers above use.
    """
    total = {"collected": 0, "failed": 0, "errored": 0, "skipped": 0}
    suites_seen = 0
    for pattern in patterns:
        for report_file in root.glob(pattern):
            try:
                document = ET.parse(report_file).getroot()
            except (ET.ParseError, OSError):
                continue
            suites = [document] if document.tag == "testsuite" else list(document.iter("testsuite"))
            for suite in suites:
                suites_seen += 1
                total["collected"] += int(suite.get("tests") or 0)
                total["failed"] += int(suite.get("failures") or 0)
                total["errored"] += int(suite.get("errors") or 0)
                total["skipped"] += int(suite.get("skipped") or 0)
    if not suites_seen:
        return {}
    total["passed"] = max(
        0, total["collected"] - total["failed"] - total["errored"] - total["skipped"]
    )
    return total


# A listing path Jest/Vitest print for `--listTests`: an absolute POSIX path or a drive-letter
# Windows path, ending in a JS/TS extension (optionally .cjs/.mjs/.cts/.mts, optionally .jsx/.tsx).
_JEST_LISTED_PATH = re.compile(r"^(?:/|[A-Za-z]:\\).*\.[cm]?[jt]sx?$")


def count_listing(text: str, kind: str) -> int:
    """Count how many units a runner's own `--list`/dry-run output named, by listing style.

    "kind" identifies the shape of the listing, not the language: Jest/Vitest print one file
    path per line, cargo/go/dotnet/phpunit/rspec print one entry per test, and Gradle's dry run
    prints one line per task. Each shape gets its own line pattern; an unrecognised kind counts
    as zero rather than raising, since a caller that cannot classify a listing has nothing to
    count.
    """
    if kind == "jest_files":
        return sum(1 for line in text.splitlines() if _JEST_LISTED_PATH.match(line.strip()))
    if kind == "cargo_list":
        return len(re.findall(r": test\s*$", text, re.M))
    if kind == "go_list":
        return sum(
            1
            for line in text.splitlines()
            if re.match(r"^(Test|Example|Fuzz|Benchmark)\w*$", line.strip())
        )
    if kind == "dotnet_list":
        return sum(1 for line in text.splitlines() if line.startswith("    "))
    if kind == "phpunit_list":
        return len(re.findall(r"^\s*-\s+\S", text, re.M))
    if kind == "rspec_dry":
        return int(parse_rspec(text).get("collected") or 0)
    if kind == "gradle_dry":
        return len(re.findall(r"^> Task .*:test\b", text, re.M))
    return 0
