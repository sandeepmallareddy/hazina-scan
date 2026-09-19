"""What a repository's history shows, read from git and from nothing else.

Two measurements live here and they answer different questions.

*The repository-wide statistics* -- how many commits, over how long, by how many people,
how many of them machines, how conventional the subjects are, how many tags -- describe
activity and maintenance. They are host-agnostic on purpose: tags stand in for releases
because a tag means the same thing on GitHub, GitLab and Bitbucket, while pull requests
and reviews do not exist on all three. Nothing here asks a forge anything.

*The per-commit classes* read each commit's own diff and sort it into at most one of
three feature shapes -- A (an atomic change that lands with its test), B (a bulk test-add,
which is decomposable work), C-pre (a feature that shipped without one) -- and,
independently, into D (a repair). C is "pre" because this module never reads the tree at
HEAD, so it cannot know whether the behaviour ended up covered; its tally is therefore
reported apart from A and B rather than added to them.

Four rules run through all of it.

*Git is asked the same questions, the same way.* Every number here has to match the tool
this one re-implements, field for field, so the argument lists are its argument lists and
the configuration is the configuration it runs under -- including `core.quotepath`, which
this module leaves alone rather than turning off. A path with a non-ASCII character in it
therefore arrives quoted and octal-escaped, and is classified on that form. Reading it
more accurately than the tool we have to agree with would be a difference in the numbers,
which is the one thing that is not allowed to happen.

*Identity is consumed, never kept.* A name and an address are read at the parse site to
decide machine-vs-person and to tell two authors apart, and what survives that line is a
salted digest, a count and a boolean. The salt is random per process, so the digest means
something inside one run and nothing outside it, and no structure this module returns has
ever held a name, an address or a tag name.

*A subject is a claim, not a finding.* Every class checks the diff as well: a commit that
says "fix" and only adds lines is a feature whatever it calls itself, and a subject that
says "chore" keeps its commit out of the feature classes whatever its diff looks like.

*An appended anonymiser patch is not history.* An anonymised delivery ends with a machine
commit that repairs the anonymisation; trusting it would make every repository look
freshly active and add a phantom author, so when the tip carries that signature and real
history remains, every statistic below is measured from its parent instead.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from hazina_scan import vocab
from hazina_scan.env import run_git

#: The commit shapes a feature class will accept. A change spread over more than
#: `MAX_COMMIT_FILES` files, or more than `MAX_TOP_DIRS` top-level directories, is a sweep
#: rather than one unit of work.
MAX_COMMIT_FILES = 30
MAX_TOP_DIRS = 3

#: Class B is a bulk test-add: at least this many specs against fewer than this many
#: implementation files. Its latent-task count is the spec count, capped, because one
#: commit is never evidence of more than a handful of separable tasks.
BULK_TEST_MIN = 5
BULK_IMPL_MAX = 3
LATENT_TASK_CAP = 15

#: A repair changes code that already exists. So a commit only corroborates its "fix"
#: subject when it removed at least one line, and -- unless it also lands a test -- when
#: it is not overwhelmingly new code.
MAX_ADDITIONS_PER_DELETION = 10

#: The repair-complexity proxy: implementation breadth, directory spread and churn,
#: bounded at 1.0, then cut into the shared bands.
COMPLEXITY_PER_IMPL_FILE = 0.15
COMPLEXITY_PER_DIRECTORY = 0.1
COMPLEXITY_CHURN_SCALE = 2000.0
COMPLEXITY_MEDIUM_AT = 0.33
COMPLEXITY_LARGE_AT = 0.66

#: The conventional-commit rate is measured over this many recent subjects, not over all
#: of history: what it is meant to describe is the convention in force now.
CONVENTIONAL_WINDOW = 200

#: The "burst copy" fingerprint: several commits, in a couple of days, no merges, one or
#: two people. The shape of a scaffold dumped and abandoned rather than developed. A
#: single commit is not a burst -- that would flag every one-commit repository.
BURST_MIN_COMMITS = 2
BURST_MAX_COMMITS = 12
BURST_MAX_SPAN_DAYS = 2
BURST_MAX_AUTHORS = 2

#: `git shortlog -sne` output: a count, a name, and an address in angle brackets.
_SHORTLOG_LINE = re.compile(r"^\s*(\d+)\s+(.*?)\s+<(.+)>\s*$")

#: A tag that names a version. The tag NAME is read to test this shape and then dropped:
#: releases are routinely named after the product, the customer or an internal milestone.
_SEMVER_TAG = re.compile(r"^v?\d+\.\d+")

#: One record per commit, fields separated by a unit separator so that a subject
#: containing any ordinary punctuation still parses.
_SEP = "\x1f"
_LOG_FORMAT = _SEP.join(("%H", "%h", "%an", "%ae", "%aI", "%s"))

#: Random per process, never written down. It is what makes `author_key` a handle rather
#: than an identifier: it cannot be reversed by dictionary attack, and two runs -- or two
#: repositories in one run -- produce keys that cannot be joined against each other.
_AUTHOR_SALT = secrets.token_bytes(32)


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists() or run_git(
        repo, "rev-parse", "--git-dir", quotepath_false=False
    ).strip() != ""


def author_key(name: str, email: str) -> str:
    """A salted, run-local handle for one author.

    This is the only thing anywhere in this module derived from a name or an address. It
    counts distinct authors and attributes two commits to the same person within a run;
    it identifies nobody outside this process and survives no restart.
    """
    return hashlib.blake2b(
        f"{name}\x00{email}".encode("utf-8", "replace"), key=_AUTHOR_SALT, digest_size=8
    ).hexdigest()


def is_bot_author(name: str, email: str) -> bool:
    """Whether a commit was made by a machine. Name and address are read as one string."""
    haystack = f"{name} {email}"
    return any(p.search(haystack) for p in vocab.BOT_NAME_PATTERNS)


def _matches_any(path: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(path) for p in patterns)


def classify_files(files: list[str]) -> dict:
    """Sort one commit's paths into spec, test-infrastructure, schema, docs and impl.

    A path can be in more than one bucket: schema files are tagged for the schema signal
    and still counted as implementation. Implementation is what is left after the specs,
    the test infrastructure and the prose/configuration are taken out -- which is what
    stops a runner config or a lockfile carrying a commit into a feature class.
    """
    test_files = [f for f in files if _matches_any(f, vocab.TEST_FILE_PATTERNS)]
    test_infra = [
        f for f in files if f not in test_files and _matches_any(f, vocab.TEST_INFRA_PATTERNS)
    ]
    return {
        "test_files": test_files,
        "test_infra_files": test_infra,
        "schema_files": [f for f in files if _matches_any(f, vocab.SCHEMA_FILE_PATTERNS)],
        "docs_or_config_files": [
            f for f in files if _matches_any(f, vocab.DOCS_OR_CONFIG_PATTERNS)
        ],
        "impl_files": [
            f
            for f in files
            if not _matches_any(f, vocab.TEST_FILE_PATTERNS)
            and not _matches_any(f, vocab.TEST_INFRA_PATTERNS)
            and not _matches_any(f, vocab.DOCS_OR_CONFIG_PATTERNS)
        ],
    }


def directory_diversity(files: list[str]) -> int:
    """How many top-level directories a commit reaches. Files at the root count for none."""
    return len({f.split("/", 1)[0] for f in files if "/" in f})


def parse_commits(repo: Path, limit: int, ref: str = "HEAD") -> list[dict]:
    """Every commit reachable from `ref`, with its numstat, merges excluded.

    `limit <= 0` reads the whole history, which is the default: substantive work is spread
    across a repository's life and a window near the tip misrepresents it. A positive
    limit is a fast-preview knob and nothing else.

    Every call in this module asks `env.run_git` NOT to set `core.quotepath=false`, so a
    path with a non-ASCII character in it arrives in git's own quoted, octal-escaped form.
    That is deliberate and it is the one thing here that is not the obvious choice: the
    escaping decides which patterns a path matches, so `tests/café_test.py` is read as a
    test only when both tools see it the same way, and the numbers have to agree.
    """
    args = ["log", f"--pretty=format:{_LOG_FORMAT}", "--numstat", "--no-merges"]
    if limit and limit > 0:
        args.insert(1, f"-{limit}")
    args.append(ref)
    raw = run_git(repo, *args, quotepath_false=False)
    if not raw:
        return []

    commits: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        if not line:
            continue
        if _SEP in line:
            if current:
                commits.append(current)
            sha, abbrev, name, email, iso_date, subject = line.split(_SEP, 5)
            # Identity is used on these two lines and then out of scope. What the record
            # keeps is a salted digest and a boolean.
            current = {
                "sha": sha,
                "abbrev": abbrev,
                "author_key": author_key(name, email),
                "author_is_bot": is_bot_author(name, email),
                "date": iso_date,
                "subject": subject,
                "files": [],
                "additions": 0,
                "deletions": 0,
            }
            continue
        # A numstat line: additions, deletions, path. A binary file reports "-" for both,
        # which is counted as no line change rather than skipped, so the file still counts.
        parts = line.split("\t")
        if len(parts) != 3 or current is None:
            continue
        added, removed, path = parts
        try:
            a = int(added) if added != "-" else 0
            d = int(removed) if removed != "-" else 0
        except ValueError:
            a = d = 0
        current["additions"] += a
        current["deletions"] += d
        current["files"].append(path)
    if current:
        commits.append(current)
    return commits


def score_atomic_feature(commit: dict) -> dict:
    """Which feature class one commit belongs to, and how many tasks it stands for.

    The three classes are exclusive and tried in order. A: small, focused, at least one
    spec and one implementation file, by a person, not a chore. B: a bulk test-add, worth
    as many latent tasks as it has specs. C-pre: the same feature shape as A but with no
    test at all, and only when the subject claims a feature -- an untested change that
    does not announce itself as a feature is not evidence of anything.
    """
    files = commit["files"]
    classified = classify_files(files)
    diversity = directory_diversity(files)
    is_bot = commit["author_is_bot"]
    subject = commit["subject"]
    is_noise_subject = bool(vocab.NOISE_SUBJECT_RE.match(subject))
    is_feature_subject = bool(vocab.FEATURE_SUBJECT_RE.match(subject))

    test_count = len(classified["test_files"])
    impl_count = len(classified["impl_files"])
    has_schema = bool(classified["schema_files"])
    small_enough = len(files) <= MAX_COMMIT_FILES
    focused = diversity <= MAX_TOP_DIRS
    substantive = not is_bot and not is_noise_subject

    is_class_a = small_enough and focused and test_count >= 1 and impl_count >= 1 and substantive
    is_class_b = (
        not is_class_a
        and test_count >= BULK_TEST_MIN
        and impl_count < BULK_IMPL_MAX
        and substantive
    )
    is_class_c_pre = (
        not is_class_a
        and not is_class_b
        and small_enough
        and focused
        and impl_count >= 1
        and test_count == 0
        and is_feature_subject
        and not is_bot
    )

    if is_class_a:
        klass, latent_tasks = "A", 1
    elif is_class_b:
        klass, latent_tasks = "B", min(test_count, LATENT_TASK_CAP)
    elif is_class_c_pre:
        # Provisional: whether the behaviour is covered at HEAD is not knowable here.
        klass, latent_tasks = "C-pre", 1
    else:
        klass, latent_tasks = None, 0

    qualifies = klass is not None
    return {
        "qualifies": qualifies,
        "klass": klass,
        "latent_tasks": latent_tasks,
        "confidence": sum([qualifies, is_feature_subject, has_schema]),
        "small_enough": small_enough,
        "focused": focused,
        "has_tests": test_count > 0,
        "has_impl": impl_count > 0,
        "has_schema": has_schema,
        "is_bot": is_bot,
        "is_noise_subject": is_noise_subject,
        "is_feature_subject": is_feature_subject,
        "diversity": diversity,
        "test_file_count": test_count,
        "impl_file_count": impl_count,
    }


def _complexity_band(x: float) -> str:
    """The shared Small/Medium/Large band for a 0..1 proxy."""
    if x < COMPLEXITY_MEDIUM_AT:
        return "Small"
    return "Medium" if x < COMPLEXITY_LARGE_AT else "Large"


def classify_bug_repair(commit: dict) -> dict:
    """Class D: whether one commit repaired a defect, and how large the repair was.

    The subject does not decide on its own. A repair changes code that already exists, so
    the diff has to show a removal; and a commit that is overwhelmingly new code is a
    feature however it is worded, unless it lands a test with the change, which is the
    other -- and stronger -- corroboration. Complexity is a bounded proxy, never a
    measurement of the defect: a one-line hotfix scores near zero and a multi-file
    regression fix near one. No code belonging to the repository is run at any point.
    """
    files = commit["files"]
    classified = classify_files(files)
    is_bot = commit["author_is_bot"]
    impl_count = len(classified["impl_files"])
    test_count = len(classified["test_files"])
    diversity = directory_diversity(files)
    additions, deletions = commit["additions"], commit["deletions"]
    churn = additions + deletions

    is_bug_subject = bool(vocab.BUGFIX_SUBJECT_RE.match(commit["subject"]))
    diff_corroborates = deletions >= 1 and (
        test_count >= 1 or additions <= MAX_ADDITIONS_PER_DELETION * deletions
    )
    qualifies = is_bug_subject and impl_count >= 1 and not is_bot and diff_corroborates

    repair_complexity = round(
        min(
            1.0,
            COMPLEXITY_PER_IMPL_FILE * impl_count
            + COMPLEXITY_PER_DIRECTORY * diversity
            + churn / COMPLEXITY_CHURN_SCALE,
        ),
        3,
    )
    return {
        "qualifies": qualifies,
        "has_regression_test": qualifies and test_count >= 1,
        "repair_complexity": repair_complexity,
        "complexity_band": _complexity_band(repair_complexity),
        "impl_file_count": impl_count,
        "test_file_count": test_count,
        "diversity": diversity,
        "churn": churn,
        "is_bot": is_bot,
    }


def _looks_like_anonymizer(name: str, email: str, subject: str) -> bool:
    """Whether one commit carries the anonymiser patch signature."""
    email_l = (email or "").lower()
    if any(
        email_l == d or email_l.endswith("@" + d) or email_l.endswith("." + d)
        for d in vocab.ANON_EMAIL_DOMAINS
    ):
        return True
    if name and vocab.ANON_NAME_RE.search(name):
        return True
    return bool(subject and vocab.ANON_SUBJECT_RE.search(subject))


def detect_anonymizer_tip(repo: Path) -> dict:
    """Whether the tip is an appended anonymiser patch, and which ref history is read from.

    The sole commit is never dropped: peeling it would leave nothing to measure, and a
    repository whose only commit is the delivery patch has no history to misreport.
    """
    total = run_git(repo, "rev-list", "--count", "HEAD", quotepath_false=False).strip()
    total_commits = int(total) if total.isdigit() else 0

    info = run_git(
        repo,
        "log",
        "-1",
        f"--pretty=format:%H{_SEP}%an{_SEP}%ae{_SEP}%s",
        "HEAD",
        quotepath_false=False,
    )
    detected = False
    commit = None
    if info and _SEP in info:
        sha, name, email, subject = info.split(_SEP, 3)
        if _looks_like_anonymizer(name, email, subject):
            detected = True
            # The signature is what matters, not who signed it: the sha alone is emitted.
            commit = {"sha": sha}

    analysis_ref = "HEAD~1" if (detected and total_commits > 1) else "HEAD"
    return {
        "detected": detected,
        "applied": analysis_ref != "HEAD",
        "commit": commit,
        "total_commits_including_anonymizer": total_commits,
        "analysis_ref": analysis_ref,
    }


def _author_rows(repo: Path, ref: str) -> list[dict]:
    """Author cardinality, not authorship.

    `git shortlog -sne` is read one line at a time; the name and the address decide
    machine-vs-person and are gone on the next iteration. What is kept per author is a
    salted digest, a commit count and a boolean.
    """
    rows = []
    for line in run_git(repo, "shortlog", "-sne", ref, quotepath_false=False).splitlines():
        m = _SHORTLOG_LINE.match(line.strip())
        if not m:
            continue
        count, name, email = m.groups()
        rows.append(
            {
                "author_key": author_key(name, email),
                "commits": int(count),
                "is_bot": is_bot_author(name, email),
            }
        )
    return rows


def _iso_days(earlier: str, later: str) -> int | None:
    """Whole days between two git ISO timestamps, or None if either will not parse."""
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).days
    except ValueError:
        return None


def aggregate_repo_stats(repo: Path, anon: dict | None = None) -> dict:
    """The repository-wide block: size, age, authorship, convention, releases.

    Everything is measured from the analysis ref, which is the tip unless an anonymiser
    patch was peeled -- including the tags, so a tag placed on the delivery commit is not
    counted as release cadence.
    """
    if anon is None:
        anon = detect_anonymizer_tip(repo)
    ref = anon["analysis_ref"]

    total = run_git(repo, "rev-list", "--count", ref, quotepath_false=False).strip()
    total_commits = int(total) if total.isdigit() else 0

    # `--max-count` is applied before `--reverse`, so the two cannot be combined to find
    # the root commit. The rev-list root finder is exact and costs one call.
    roots = (
        run_git(repo, "rev-list", "--max-parents=0", ref, quotepath_false=False)
        .strip()
        .splitlines()
    )
    first = (
        run_git(repo, "log", "-1", "--pretty=format:%aI", roots[0], quotepath_false=False).strip()
        if roots
        else ""
    )
    last = run_git(repo, "log", "-1", "--pretty=format:%aI", ref, quotepath_false=False).strip()

    authors = _author_rows(repo, ref)
    human_authors = [a for a in authors if not a["is_bot"]]
    bot_authors = [a for a in authors if a["is_bot"]]
    bot_commit_count = sum(a["commits"] for a in bot_authors)
    bot_ratio = bot_commit_count / total_commits if total_commits else 0.0

    subjects = run_git(
        repo, "log", f"-{CONVENTIONAL_WINDOW}", "--pretty=format:%s", ref, quotepath_false=False
    ).splitlines()
    conventional = sum(1 for s in subjects if vocab.CONVENTIONAL_RE.match(s))
    conventional_rate = conventional / len(subjects) if subjects else 0.0

    # Tag names are read to test the semver shape and then dropped; only counts are kept.
    tag_names = [
        t
        for t in run_git(repo, "tag", "--merged", ref, quotepath_false=False).splitlines()
        if t.strip()
    ]
    semver_tags = sum(1 for t in tag_names if _SEMVER_TAG.match(t.strip()))
    tag_count = len(tag_names)
    del tag_names

    recency_days = None
    if last:
        try:
            now = datetime.now(UTC)
            recency_days = (now - datetime.fromisoformat(last).astimezone(UTC)).days
        except ValueError:
            pass
    span_days = _iso_days(first, last) if first and last else None

    merges = run_git(
        repo, "rev-list", ref, "--min-parents=2", "--count", quotepath_false=False
    ).strip()
    merge_commit_count = int(merges) if merges.isdigit() else 0
    looks_like_burst_copy = bool(
        BURST_MIN_COMMITS <= total_commits <= BURST_MAX_COMMITS
        and span_days is not None
        and span_days <= BURST_MAX_SPAN_DAYS
        and merge_commit_count == 0
        and len(human_authors) <= BURST_MAX_AUTHORS
    )

    return {
        "head_sha": run_git(repo, "rev-parse", "HEAD", quotepath_false=False).strip() or None,
        # When an anonymiser patch is peeled, every statistic below is measured from this
        # commit -- the parent -- and not from the tip.
        "effective_tip_sha": run_git(repo, "rev-parse", ref, quotepath_false=False).strip() or None,
        "anonymizer_commit_detected": anon["detected"],
        "anonymizer_commit_excluded": anon["applied"],
        "anonymizer_commit": anon["commit"],
        "total_commits": total_commits,
        "total_commits_including_anonymizer": anon["total_commits_including_anonymizer"],
        "first_commit": first or None,
        "last_commit": last or None,
        "span_days": span_days,
        "recency_days": recency_days,
        "human_authors": len(human_authors),
        "bot_authors": len(bot_authors),
        "bot_commit_count": bot_commit_count,
        "bot_commit_ratio": round(bot_ratio, 4),
        "conventional_rate_last_200": round(conventional_rate, 4),
        "tag_count": tag_count,
        "semver_tag_count": semver_tags,
        # Retained null: the counts above carry the release signal, and a tag name does
        # not leave this machine.
        "latest_tag": None,
        "merge_commit_count": merge_commit_count,
        "looks_like_burst_copy": looks_like_burst_copy,
        # Retained empty: the two author counts are the whole signal, and neither an
        # author's identity nor their share of the history is emitted.
        "top_authors": [],
    }


def _cap(items: list, top: int) -> list:
    """Bound an emitted list. `top <= 0` emits all of it."""
    return items[:top] if top and top > 0 else items


def collect(repo: Path, git_top: int = 1) -> dict:
    """The whole git block: the repository statistics and the four class tallies.

    The full history is always scanned. `git_top` bounds only the per-commit lists at the
    end, which exist so the raw document keeps one shape whatever `git_top` is set to; every
    number a caller reads is computed over all of it, capped or not.
    """
    repo = Path(repo).resolve()
    anon = detect_anonymizer_tip(repo)
    stats = aggregate_repo_stats(repo, anon)

    analyzed = []
    for c in parse_commits(repo, 0, ref=anon["analysis_ref"]):
        analyzed.append(
            {
                "sha": c["abbrev"],
                "full_sha": c["sha"],
                # Retained null. The subject decided this row's classes a line ago and goes no
                # further: a commit message is prose the repository wrote, and this tool emits
                # none of that. The key stays so the document's shape is unchanged.
                "subject": None,
                "author_key": c["author_key"],
                "date": c["date"],
                "files_changed": len(c["files"]),
                "additions": c["additions"],
                "deletions": c["deletions"],
                **score_atomic_feature(c),
                "bug": classify_bug_repair(c),
            }
        )

    class_a = [c for c in analyzed if c["klass"] == "A"]
    class_b = [c for c in analyzed if c["klass"] == "B"]
    class_c_pre = [c for c in analyzed if c["klass"] == "C-pre"]
    class_d = [c for c in analyzed if c["bug"]["qualifies"]]

    # A and B are decided from the commit alone, so their latent tasks are the confirmed
    # count. C-pre depends on coverage at HEAD, which nothing here reads, so it is added
    # only to the provisional count.
    confirmed = sum(c["latent_tasks"] for c in class_a) + sum(c["latent_tasks"] for c in class_b)
    return {
        "schema_version": "4.0",
        "repo_path": str(repo),
        "repo_stats": stats,
        "analyzed_commits": len(analyzed),
        "full_history_scanned": True,
        "class_a_count": len(class_a),
        "class_b_count": len(class_b),
        "class_c_pre_count": len(class_c_pre),
        "class_d_bug_count": len(class_d),
        "confirmed_candidate_count": confirmed,
        "provisional_candidate_count": confirmed + len(class_c_pre),
        "class_a_commits": _cap(class_a, git_top),
        "class_b_commits": _cap(class_b, git_top),
        "class_c_pre_commits": _cap(class_c_pre, git_top),
        "class_d_bug_commits": _cap(class_d, git_top),
        "scanned_commits": _cap(analyzed, git_top),
    }
