"""History statistics and the four commit classes.

`COMMITS` is module-level on purpose: the history tests build the same repository from it,
so a change to the fixture moves both measurements together.
"""

from hazina_scan import git as gitstats
from tests.conftest import _git

COMMITS = [
    {
        "msg": "feat: add core",
        "files": {
            "src/core.py": "a\n" * 30,
            "src/util.py": "b\n" * 20,
            "tests/test_core.py": "t\n" * 10,
        },
        "date": "2024-01-15T10:00:00+00:00",
    },
    # The repair rewrites a line rather than only appending one: a commit whose diff is
    # pure addition is not counted as a repair here, whatever its subject says.
    {
        "msg": "fix: off by one in paging",
        "files": {"src/core.py": "a\n" * 29 + "z\n", "tests/test_core.py": "t\n" * 12},
        "name": "Dev Two",
        "email": "dev2@example.com",
        "date": "2024-02-01T10:00:00+00:00",
    },
    {
        "msg": "chore(deps): bump lodash",
        "files": {"package.json": "{}\n"},
        "name": "dependabot[bot]",
        "email": "49699333+dependabot[bot]@users.noreply.github.com",
        "date": "2024-02-02T10:00:00+00:00",
    },
]

#: A second history, for the classes `COMMITS` does not reach: a bulk test-add (B), an
#: impl-only feature (C-pre) and a merge. Kept apart from `COMMITS` so the counts the
#: history tests pin stay as they are.
CLASS_COMMITS = [
    {
        "msg": "feat: add parser",
        "files": {"src/parser.py": "p\n" * 40, "tests/test_parser.py": "t\n" * 12},
        "date": "2024-03-01T10:00:00+00:00",
    },
    {
        "msg": "test: cover parser edge cases",
        "files": {f"tests/test_edge{i}.py": "t\n" * 6 for i in range(1, 6)},
        "date": "2024-03-02T10:00:00+00:00",
    },
    {
        "msg": "feat: add exporter",
        "files": {"src/export.py": "x\n" * 20},
        "name": "Dev Two",
        "email": "dev2@example.com",
        "date": "2024-03-03T10:00:00+00:00",
    },
    {
        "msg": "fix: correct rounding in exporter",
        "files": {"src/export.py": "x\n" * 19 + "y\n"},
        "name": "Dev Two",
        "email": "dev2@example.com",
        "date": "2024-03-04T10:00:00+00:00",
    },
]


def _class_repo(repo_builder, name="c"):
    """`CLASS_COMMITS` plus a real merge commit, which the builder cannot make itself."""
    repo = repo_builder({}, commits=CLASS_COMMITS, name=name)
    stamp = "2024-03-05T10:00:00+00:00"
    env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "side.py").write_text("s\n" * 12, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add side channel", env=env)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side channel", "side", env=env)
    return repo


def test_aggregate_stats(repo_builder):
    repo = repo_builder({}, commits=COMMITS)
    stats = gitstats.aggregate_repo_stats(repo, gitstats.detect_anonymizer_tip(repo))
    assert stats["total_commits"] == 3
    assert stats["human_authors"] == 2 and stats["bot_authors"] == 1
    assert stats["bot_commit_count"] == 1
    assert stats["first_commit"].startswith("2024-01-15")
    assert stats["span_days"] == 18
    assert stats["top_authors"] == [] and stats["latest_tag"] is None


def test_bug_class_and_atomic_feature(repo_builder):
    repo = repo_builder({}, commits=COMMITS)
    out = gitstats.collect(repo)
    assert out["class_d_bug_count"] == 1
    assert out["analyzed_commits"] == 3 and out["full_history_scanned"] is True


def test_remaining_classes(repo_builder):
    out = gitstats.collect(_class_repo(repo_builder))
    assert out["class_a_count"] == 1  # feat + test, one impl file
    assert out["class_b_count"] == 1  # five specs, no impl
    assert out["class_c_pre_count"] == 2  # impl-only feature commits
    assert out["class_d_bug_count"] == 1
    assert out["confirmed_candidate_count"] == 6  # 1 + min(5, 15)
    assert out["provisional_candidate_count"] == 8  # plus the two C-pre
    assert out["repo_stats"]["merge_commit_count"] == 1
    # The merge itself is not one of the scanned commits.
    assert out["analyzed_commits"] == 5
    # The per-commit rows exist for the document's shape; no commit message is in them.
    assert all(row["subject"] is None for row in out["scanned_commits"])


def test_no_identity_survives_the_parse(repo_builder):
    """The author digest is run-local and nothing else about an author is kept."""
    repo = repo_builder({}, commits=COMMITS)
    commits = gitstats.parse_commits(repo, 0)
    keys = {c["author_key"] for c in commits}
    assert len(keys) == 3 and all(len(k) == 16 for k in keys)
    blob = repr(commits) + repr(gitstats.aggregate_repo_stats(repo))
    for secret in ("Dev One", "dev1@example.com", "Dev Two", "dependabot"):
        assert secret not in blob
    assert gitstats.author_key("Dev One", "dev1@example.com") != gitstats.author_key(
        "Dev Two", "dev1@example.com"
    )
