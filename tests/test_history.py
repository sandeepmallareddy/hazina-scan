"""How deep a repository's history really is.

The second fixture below is the one that earns its keep: `COMMITS` is a single-branch
history with one bot commit, so it exercises neither ref selection nor the anonymiser
rule. `_branchy_repo` builds a repository with four refs of three different kinds and
two different depths, and puts a synthetic anonymisation commit on the tip of the
deepest one -- the arrangement in which a mistake in either rule changes the answer.
"""

from hazina_scan import history, schema
from tests.conftest import _git
from tests.test_git import COMMITS


def test_collect_counts(repo_builder):
    repo = repo_builder({}, commits=COMMITS)
    out = history.collect(repo)
    assert out["ok"] and out["history_probe_mode"] == "deterministic"
    assert out["ref_analysed"] is None
    assert out["real_commits"] == 2  # bot excluded
    assert out["mineable_commits"] == 1  # first commit: 2 impl files, churn in band
    assert out["human_authors"] == 2


def test_no_refs(tmp_path):
    (tmp_path / "r").mkdir()
    out = history.collect(tmp_path / "r")
    assert out == {"probe": "git_history", "ok": False, "error": "no refs found"}


# --- a repository with several refs, a bot and an anonymiser tip ---------------------------

BRANCHY = [
    {
        "msg": "feat: first cut",
        "files": {"src/a.py": "a\n" * 30, "src/b.py": "b\n" * 25, "tests/test_a.py": "t\n" * 8},
        "date": "2024-04-01T10:00:00+00:00",
    },
    {
        "msg": "chore(deps): bump things",
        "files": {"package-lock.json": "{}\n" * 40},
        "name": "renovate[bot]",
        "email": "renovate@example.com",
        "date": "2024-04-02T10:00:00+00:00",
    },
]


def _commit(
    repo, rel, text, msg, name="Dev One", email="dev1@example.com", date="2024-04-05T10:00:00+00:00"
):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        msg,
        env={
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        },
    )


def _branchy_repo(repo_builder):
    """Four refs, three kinds, two depths, and a synthetic tip on the deepest one."""
    repo = repo_builder({}, commits=BRANCHY, name="branchy")
    _git(repo, "tag", "v0")  # a tag on the shallow default branch
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "src/c.py", "c\n" * 40, "feat: add the third module")
    _commit(
        repo,
        "src/d.py",
        "d\n" * 15,
        "feat: and a fourth",
        name="Dev Two",
        email="dev2@example.com",
        date="2024-04-06T10:00:00+00:00",
    )
    _git(repo, "tag", "v1")  # a tag aliasing the deep branch's commit
    _commit(
        repo,
        "src/a.py",
        "a\n" * 29 + "x\n",
        "anonymize repository identifiers",
        name="anonymizer",
        email="anonymizer@anonymizer.local",
        date="2024-04-07T10:00:00+00:00",
    )
    _git(repo, "checkout", "-q", "main")
    return repo


def test_branchy_deepest_ref_and_anonymizer_tip(repo_builder):
    repo = _branchy_repo(repo_builder)
    ref, n = history.deepest_ref(repo)
    assert ref == "refs/heads/work" and n == 5
    assert history.anonymizer_tip(repo, ref) is True
    assert history.anonymizer_tip(repo, "refs/heads/main") is False

    kinds = {c["kind"] for c in history.ref_candidates(repo)}
    assert kinds == {"local", "tag"}

    out = history.collect(repo)
    assert out["anonymizer_tip_detected"] is True
    assert out["ref_commits"] == 5
    # Four refs, three candidates: the shallow branch and the tag on it share a commit
    # and collapse into one, while the tag below the anonymiser tip stands on its own.
    assert out["ref_candidates_considered"] == 3
    # Measured from the anonymiser's parent: four commits, one of them a bot.
    assert out["real_commits"] == 3
    assert out["human_authors"] == 2
    # Only the first commit touches two implementation files with churn in band.
    assert out["mineable_commits"] == 1


# --- the notes are ours, and stay inside what the schema will emit -------------------------


def test_notes_are_emittable(repo_builder, tmp_path):
    out = history.collect(_branchy_repo(repo_builder))
    for field in ("ref_choice_reason", "development_substance_error"):
        assert schema.OWN_PROSE.apply(out[field], field) == out[field]
    assert out["development_substance"] is None
    assert out["development_substance_note"] is None

    (tmp_path / "empty").mkdir()
    bad = history.collect(tmp_path / "empty")
    assert schema.OWN_PROSE.apply(bad["error"], "error") == bad["error"]


def test_no_candidates_reason_is_emittable(repo_builder, monkeypatch):
    """The branch taken when refs exist but none can be enumerated as a candidate."""
    repo = repo_builder({}, commits=COMMITS)
    monkeypatch.setattr(history, "ref_candidates", lambda *a, **k: [])
    out = history.collect(repo)
    assert out["ref_candidates_considered"] == 0
    assert schema.OWN_PROSE.apply(out["ref_choice_reason"], "r") == out["ref_choice_reason"]
    assert out["ok"] and out["ref_commits"] == 3
