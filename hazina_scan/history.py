"""How much real development a repository's history holds, and which ref to read it from.

Two questions, and the second one is the reason the first exists.

*Which ref.* The tip of the default branch is not reliably where the code lives. A mirror
routinely keeps its working tree on a feature branch, an integration branch or a fork's
branch while the branch that is checked out by default holds a placeholder, a readme or a
single configuration file, and a measurement taken from the wrong branch is not a small
error -- it is a repository scored as nearly empty. So the ref is chosen rather than
assumed: candidates are enumerated with cheap aggregate evidence, and the one with the
deepest reachable history wins. That rule is the whole of the decision here. It is stated
in the output as `ref_choice_reason`, in plain words, so a reader never has to guess which
tree the other measurements were taken from.

*How much of that history is development.* A commit count is not a measure of work.
Automation, lockfile refreshes and bulk anonymisation passes all inflate it, and a
repository can carry thousands of commits without holding a week of engineering. Three
numbers are reported instead of one. `real_commits` drops the machines. `mineable_commits`
is the strict one and the one that matters downstream: a commit qualifies only if it
touched at least two first-party implementation files and moved between
`MIN_CHURN` and `MAX_CHURN` lines. Two files is what makes a change a change rather than a
typo; the floor drops version bumps; the ceiling drops the vendored tree that arrives in a
single commit. `human_authors` counts distinct people.

Three rules run through all of it.

*Git is asked the same questions, the same way.* Every number here has to match the tool
this one re-implements, so the argument lists, the per-call timeouts and the twenty
thousand commit ceiling on the log walk are its own. `core.quotepath` is turned off, which
is what that tool does here: a path with a non-ASCII character in it therefore arrives as
itself, and the extension anchors in the path tables keep matching on a tree that was not
written in English.

*Nothing read here is kept.* No ref name is emitted -- `ref_analysed` is permanently null,
because a branch name carries product and customer identity as readily as a file does. No
path, subject or author name survives its own line: an address is consumed at the parse
site to decide machine-or-person and to tell two people apart, and what remains is a
salted digest that means something for the length of one process and nothing after it.

*An appended anonymiser patch is not history.* An anonymised delivery ends with a machine
commit that rewrites identifiers across the whole tree. Counting it would add a phantom
author and a phantom day of work to every repository in the corpus, so when the chosen
tip carries that signature the counting starts from its parent instead -- and says so, in
`anonymizer_tip_detected`.

There is no judgement in this module beyond the two rules above, and the output says as
much: `development_substance` is null rather than zero. An unmeasured judgement must never
read as a bad one.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from hazina_scan import vocab
from hazina_scan.env import run_git

#: Enumeration stops here however many refs a mirror carries. A repository with four
#: hundred refs costs the same as one with four thousand.
MAX_REFS_ENUMERATED = 400

#: How many of the deepest survivors get the expensive tree walk. Refs sharing a tip
#: commit collapse to one candidate first, so a mirror with forty remote branches at the
#: same commit is one candidate rather than forty.
MAX_CANDIDATES = 12

#: A mineable commit's churn band. Below the floor is a version bump or a typo; above the
#: ceiling is a vendored tree, a generated client or a reformatting sweep arriving whole.
MIN_CHURN = 20
MAX_CHURN = 10_000

#: Implementation files a commit must touch to count as mineable. One file is an edit.
MIN_IMPL_FILES = 2

#: The ceiling on the single log walk. A history longer than this is not measured more
#: accurately by reading all of it; it is only read more slowly.
LOG_COMMIT_CAP = 20_000

#: Per-call ceilings, in seconds. The log walk gets the long one because it is a single
#: pass over the whole history; the tree walks are per candidate and stay short.
COUNT_TIMEOUT = 300
TREE_TIMEOUT = 180
TOP_DIRS_TIMEOUT = 120
TIP_DATE_TIMEOUT = 120
LOG_TIMEOUT = 1800

#: The record separator in the log walk's own header lines. `%H` is a hash and `%an` is a
#: name, so neither is ever allowed past the loop that reads them.
_HEADER = "@@|"

#: Per-process salt for the author count. Random at import, so the digest below tells two
#: authors apart inside one run and identifies nobody once the process exits. See
#: `git.author_key` for the same reasoning at greater length.
_AUTHOR_SALT = secrets.token_bytes(32)


def _author_key(email: str) -> str:
    """A run-local handle for one address. The only thing here derived from one."""
    return hashlib.blake2b(
        email.strip().lower().encode("utf-8", "replace"), key=_AUTHOR_SALT, digest_size=8
    ).hexdigest()


def _is_bot(haystack: str) -> bool:
    """Whether a name-and-address, or a whole log line, belongs to a machine."""
    return any(p.search(haystack) for p in vocab.BOT_NAME_PATTERNS)


def _matches(path: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(path) for p in patterns)


def _count(repo: Path, rev: str) -> int | None:
    """How many commits this revision reaches, or None when git would not say.

    None and zero are kept apart on purpose. A ref git refuses to walk -- a broken one, a
    ref to a missing object -- is not a ref with an empty history, and treating it as one
    would let it win the deepest-history rule outright in a repository where every other
    walk also failed.
    """
    out = run_git(repo, "rev-list", "--count", rev, timeout=COUNT_TIMEOUT).strip()
    return int(out) if out.isdigit() else None


# --- which ref ----------------------------------------------------------------------------


def deepest_ref(repo: Path) -> tuple[str | None, int]:
    """The ref with the most reachable commits, and how many that is.

    Ties go to the first refname in git's own byte order, which is the order
    `for-each-ref` already returns, so the answer does not depend on how the walk was
    scheduled. A repository with no refs at all falls back to whatever `HEAD` names -- a
    branch that has never been committed to still has a name -- and zero commits.
    """
    refs = [r for r in run_git(repo, "for-each-ref", "--format=%(refname)").split() if r]
    if not refs:
        head = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return (head or None), 0
    best: str | None = None
    best_n = -1
    for ref in refs[:MAX_REFS_ENUMERATED]:
        n = _count(repo, ref)
        if n is not None and n > best_n:
            best, best_n = ref, n
    return best, max(best_n, 0)


def ref_kind(refname: str) -> str:
    """Local branch, remote-tracking branch, tag, or something else entirely."""
    if refname.startswith("refs/tags/"):
        return "tag"
    if refname.startswith("refs/remotes/"):
        return "remote"
    if refname.startswith("refs/heads/"):
        return "local"
    return "other"


def ref_candidates(repo: Path, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Candidate refs, deepest first, each with cheap aggregate evidence.

    The evidence is reachable commits, files in the tree, distinct top-level entries, the
    date of the tip commit, the kind of ref it is, and how many other refs point at the
    same commit. All of it is aggregate: a count of files, never a file name, so nothing
    collected here could carry a path out even if something downstream wanted one.

    Cost stays flat on a repository with hundreds of refs because of three bounds:
    enumeration stops at `MAX_REFS_ENUMERATED`, refs sharing a tip commit collapse into a
    single candidate, and only the `limit` deepest survivors are walked for tree evidence.
    """
    raw = run_git(
        repo, "for-each-ref", "--format=%(refname)%09%(objecttype)%09%(objectname)%09%(*objectname)"
    )
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        refname, objtype = parts[0], parts[1]
        # An annotated tag is its own object and is not a commit, so for one of those the
        # commit is what it points at -- the fourth field -- not the object itself.
        peeled = parts[3] if len(parts) > 3 else ""
        sha = peeled if objtype == "tag" and peeled else parts[2]
        if sha:
            rows.append((refname, sha))
        if len(rows) >= MAX_REFS_ENUMERATED:
            break
    if not rows:
        return []

    # Aliases collapse to their first refname in byte order, which is the same
    # representative the fallback above would have named for the same tie. The two rules
    # must never disagree merely about which of several equal names to print.
    groups: dict[str, dict] = {}
    for refname, sha in rows:
        group = groups.setdefault(sha, {"ref": refname, "sha": sha, "aliases": 0})
        group["aliases"] += 1
    for group in groups.values():
        group["commits"] = _count(repo, group["sha"]) or 0

    ranked = sorted(groups.values(), key=lambda g: (-g["commits"], g["ref"]))[:limit]
    for group in ranked:
        # Both tree walks are counted and discarded on the same line they arrive on.
        group["files"] = sum(
            1
            for line in run_git(
                repo, "ls-tree", "-r", "--name-only", group["sha"], timeout=TREE_TIMEOUT
            ).splitlines()
            if line.strip()
        )
        group["top_dirs"] = sum(
            1
            for line in run_git(
                repo, "ls-tree", "-d", "--name-only", group["sha"], timeout=TOP_DIRS_TIMEOUT
            ).splitlines()
            if line.strip()
        )
        group["last_commit"] = (
            run_git(
                repo, "log", "-1", "--format=%cs", group["sha"], timeout=TIP_DATE_TIMEOUT
            ).strip()
            or "unknown"
        )
        group["kind"] = ref_kind(group["ref"])
    return ranked


#: Why the ref that was read was the one read. Our own words, in the output, because the
#: ref name itself never is -- this sentence is the entire account a reader gets of which
#: tree every other number was taken from.
DEEPEST_REASON = "the deepest reachable history was used"
NO_CANDIDATES_REASON = (
    "no candidate branches or tags could be listed, so the deepest reachable history was used"
)

#: There is no sampled judgement of the history in this tool, only the counts below, so
#: the substance score is null and says why.
NO_SUBSTANCE_ERROR = (
    "this tool does not sample a history for a judgement of it, so no "
    "substance score was formed; the counts beside it are measured"
)


def choose_ref(repo: Path) -> tuple[str | None, int, int, str]:
    """(ref, reachable commits, candidates considered, why) -- the deepest-history rule."""
    candidates = ref_candidates(repo)
    if not candidates:
        ref, n = deepest_ref(repo)
        return ref, n, 0, NO_CANDIDATES_REASON
    deepest = max(c["commits"] for c in candidates)
    # The same tie-break as `deepest_ref`, read off evidence already gathered rather than
    # bought with a second walk over the refs.
    chosen = min((c for c in candidates if c["commits"] == deepest), key=lambda c: c["ref"])
    return chosen["ref"], chosen["commits"], len(candidates), DEEPEST_REASON


def anonymizer_tip(repo: Path, ref: str) -> bool:
    """Whether this ref's tip is the synthetic commit an anonymised delivery appends.

    Both halves are required: a machine author *and* the word the pass names itself with.
    A bot commit on its own is ordinary automation and stays in the history.
    """
    tip = run_git(repo, "log", "-1", "--format=%an|%ae|%s", ref)
    return _is_bot(tip) and "anonymi" in tip.lower()


# --- how much of it is development --------------------------------------------------------


class Tally:
    """The running counts of one log walk. Kept as an object so the close-out rule for a
    commit lives in one place rather than being repeated at the loop's end."""

    def __init__(self) -> None:
        self.real = 0
        self.mineable = 0
        self.authors: set[str] = set()
        self._open = False
        self._bot = False
        self._impl = 0
        self._churn = 0

    def start(self, name: str, email: str) -> None:
        self.close()
        self._open = True
        self._bot = _is_bot(f"{name} {email}")
        if not self._bot:
            self.authors.add(_author_key(email))

    def file(self, added: str, deleted: str, path: str) -> None:
        # Anything before the first header belongs to no commit and is not counted into
        # the next one: the walk only ever attributes a file to a commit already open.
        if not self._open or _matches(path, vocab.NON_IMPL_PATH_PATTERNS):
            return
        self._churn += int(added) if added.isdigit() else 0
        self._churn += int(deleted) if deleted.isdigit() else 0
        if not _matches(path, vocab.HISTORY_TEST_PATH_PATTERNS):
            self._impl += 1

    def close(self) -> None:
        if not self._open:
            return
        if not self._bot:
            self.real += 1
            if self._impl >= MIN_IMPL_FILES and MIN_CHURN <= self._churn <= MAX_CHURN:
                self.mineable += 1
        self._impl = self._churn = 0


def walk_history(repo: Path, ref: str) -> Tally:
    """One pass over the history behind `ref`, counting commits, work and people.

    Merges are excluded because a merge's diff is not anybody's work, and renames are not
    followed because a moved file is not churn. Neither the hash, the name nor the path on
    any line survives the iteration that reads it.
    """
    log = run_git(
        repo,
        "log",
        ref,
        "--no-merges",
        "--numstat",
        "--no-renames",
        f"--format={_HEADER}%H|%an|%ae",
        "-n",
        str(LOG_COMMIT_CAP),
        timeout=LOG_TIMEOUT,
    )
    tally = Tally()
    for line in log.splitlines():
        if line.startswith(_HEADER):
            # A name may itself hold the separator, so the split is bounded and the
            # remainder -- the address -- is whatever is left of the line.
            fields = (line.split("|", 3) + ["", "", ""])[:4]
            tally.start(fields[2], fields[3])
            continue
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) == 3:
            tally.file(*parts)
    tally.close()
    return tally


def collect(repo: Path) -> dict:
    """Everything this module measures, as one block. Never scored on directly."""
    out: dict = {"probe": "git_history", "ok": False}
    ref, ref_commits, n_candidates, reason = choose_ref(repo)
    if not ref:
        out["error"] = "no refs found"
        return out

    anonymised = anonymizer_tip(repo, ref)
    # The ref name is resolved because git needs one to walk, and is then dropped: what
    # goes out is the count, not the name.
    out["ref_analysed"] = None
    out["ref_commits"] = ref_commits
    out["ref_candidates_considered"] = n_candidates
    out["ref_choice_reason"] = reason
    out["ref_choice_overrode_deepest"] = False
    out["anonymizer_tip_detected"] = anonymised
    out["commit_sha"] = run_git(repo, "rev-parse", ref).strip()[:40]
    out["development_substance"] = None
    out["development_substance_note"] = None
    out["development_substance_error"] = NO_SUBSTANCE_ERROR

    tally = walk_history(repo, f"{ref}~1" if anonymised else ref)
    out.update(
        {
            "real_commits": tally.real,
            "mineable_commits": tally.mineable,
            "human_authors": len(tally.authors),
            "history_probe_mode": "deterministic",
            "history_model": None,
            "ok": True,
        }
    )
    return out
