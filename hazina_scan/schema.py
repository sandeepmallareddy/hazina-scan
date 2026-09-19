"""Everything that can end up in an output file has to be validated by code in this module.

This tool produces two documents, a `codebase_repos` row and a `measurement`, and every leaf
either one can hold is declared here together with the one *kind* of value it is permitted
to be: a plain number or boolean, a value from a closed vocabulary, a bounded token, a status
note written in this tool's own words, or a field that is always empty. Anything not on that
list is not sanitised, shortened or passed through with a note attached -- `enforce` raises
and nothing gets written, full stop.

This is an allowlist rather than a denylist by design, and the distinction matters. A
denylist only rejects what somebody thought to reject, so any field a future collector
happens to add ships automatically. Here the starting position is rejection, so adding a new
field means someone has to come to this file and declare it on purpose.

The five kinds this module recognises:

* **numbers and booleans** -- plain counts, ratios and flags; nothing from inside the tree
  passes through as one of these.
* **closed-vocabulary values** -- things like a language or a framework name, checked
  against the tables of public technology names in `vocab.py`. Anything not already in that
  table is either mapped to `"other"` or dropped, because an unrecognised string here is, by
  definition, text that came from somebody's repository rather than from this tool.
* **tokens** -- short bounded strings such as the content digest, the display handle, a
  timestamp or version string, and the two name fields (the repository's own name and its
  company). Each has a pattern tight enough that nothing of a different shape, such as an
  absolute filesystem path, can be mistaken for it.
* **status notes** -- short prose this tool wrote about its own run, drawn from its own
  vocabulary of flag names and enum values, which get masked out first so they cannot look
  like identifiers pulled from the repository. These are subject to the same prose rules as
  everything else emitted in free text.
* **declared-empty fields** -- fields that are permanently unset and say so in `review()`,
  so a reader can see what this tool chose not to collect rather than guessing at why a key
  is missing.

`enforce` never modifies the document it is given; it always builds and returns a new one.
"""

from __future__ import annotations

import math
import re

from . import vocab

__all__ = [
    "EmissionRefused",
    "enforce",
    "review",
    "rejection_reason",
    "SPEC",
    "DECLARED_FIELDS",
    "DECLARED_KEYS",
    "CLOSED_VOCABULARY_KEYS",
    "MAX_SENTENCE_WORDS",
]


class EmissionRefused(RuntimeError):
    """Raised at the write boundary. The run fails and nothing is written."""


# ---------------------------------------------------------------------------
# Prose: the only free text that leaves, and what disqualifies a line of it
# ---------------------------------------------------------------------------

MAX_SENTENCE_WORDS = 40

# Each entry is a pattern and the plain reason a reader would accept for dropping the line.
# Ordered most specific first so the reason names the most telling thing found. A lone
# semicolon is ordinary English and is deliberately not here.
_REJECTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[/\\]"), "it holds a path separator"),
    (re.compile(r"https?:|\bwww\."), "it holds a URL"),
    (re.compile(r"@"), "it holds an address or a handle"),
    (re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]"), "it holds a dotted identifier or a file name"),
    (re.compile(r"(?:^|\s)\.[A-Za-z0-9]{1,6}\b"), "it names a file extension"),
    (re.compile(r"[A-Za-z0-9]+_[A-Za-z0-9]+"), "it holds a snake_case identifier"),
    (re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"), "it holds a camelCase identifier"),
    # An apostrophe straight after a letter is a contraction, not an opening quote. Without
    # that lookbehind the rule pairs the apostrophe in "couldn't" with the one in "wasn't"
    # and throws out a perfectly ordinary English note as a quoted string. A real quote
    # opens at the start of the line or after a space, and is still refused.
    (re.compile(r"[\"`]|(?<![A-Za-z])'[^']{1,80}'"), "it holds a quoted string"),
    # Square brackets count as much as braces do: they are what a partial scrub leaves
    # behind, and this is the rule that stops "[identifier] handles [path]" going out as
    # though it said something.
    (re.compile(r"[{}\[\]<>=|*#$]|::|->|=>"), "it holds code punctuation"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "it holds an object hash"),
    (re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\b"), "it holds an acronym or a constant name"),
]

# `rejection_reason` only applies this pattern when `allow` is empty, i.e. for text nothing in
# this codebase vouches for. Parenthetical asides are routine in the notes this tool writes for
# itself -- a failed-check message naming the disabled flag is a typical example -- so banning
# parentheses unconditionally would make this tool unable to describe its own output. Whatever
# a parenthetical aside might otherwise smuggle past the schema is still caught by the patterns
# listed above it, which run regardless of where `allow` came from.
_ROUND_BRACKETS = (re.compile(r"[()]"), "it holds code punctuation")

_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def rejection_reason(
    text: str, max_words: int = MAX_SENTENCE_WORDS, allow: frozenset[str] = frozenset()
) -> str | None:
    """Return why `text` cannot go out as free-form prose, or `None` if the schema accepts it.

    The words in `allow` -- this tool's own flag names and the literal members of its closed
    vocabularies -- are stripped out of `text` first, via `_neutralise`, so a note this tool
    wrote itself, such as "check skipped (mine_disabled)", is not rejected merely for
    containing one of its own snake_case identifiers. A non-empty `allow` also marks `text` as
    something this tool generated rather than copied out of a repository, which is why it also
    relaxes the parenthesis rule below.

    What the remaining patterns reject, underneath that allowance, is anything specific enough
    to be describing someone's repository rather than describing this tool's own behaviour: a
    path, a file, a symbol out of somebody's source, a product name, a company name. A note is
    free to state a shape or an outcome in general terms, but never to name the particular
    thing it was drawn from.
    """
    if not isinstance(text, str) or not text.strip():
        return "it is empty"

    probe = " ".join(text.split())
    length = len(probe.split(" "))
    if length > max_words:
        return f"it runs to {length} words, over the limit of {max_words}"

    probe = _neutralise(probe, allow)
    for pattern, reason in _REJECTIONS if allow else [*_REJECTIONS, _ROUND_BRACKETS]:
        if pattern.search(probe):
            return reason
    return _capitalised_name_in(probe)


def _neutralise(probe: str, allow: frozenset[str]) -> str:
    """Blank out this tool's own words so the patterns never fire on them.

    Longer tokens go first: replacing a short one first would leave the tail of a longer
    token standing, and the patterns would then object to a fragment of our own vocabulary.
    """
    for token in sorted(allow, key=len, reverse=True):
        probe = probe.replace(token, "ok")
    return probe


def _capitalised_name_in(probe: str) -> str | None:
    """Report the first capitalised word that is neither sentence-initial nor a known
    technology, since such a word is a name.

    English capitalises a word at the opening of a sentence and, beyond that, when it is a
    proper noun. This tool has no way of telling a product from a company from a service
    from a class without shipping a dictionary, and it is not going to ship one -- so a
    capital anywhere other than a sentence boundary is refused.
    """
    for match in _WORD.finditer(probe):
        word = match.group(0)
        if not word[:1].isupper() or word in vocab.TECH_NAMES:
            continue
        preceding = probe[: match.start()].rstrip()
        if preceding and preceding[-1] not in ".!?":
            return f"it holds the capitalised name {word!r}"
    return None


# The vocabulary this tool's own status notes are allowed to be written from: its flag
# names, the values of its closed vocabularies, and the exception class names a collector
# interpolates via `type(e).__name__`. That last group belongs to the Python runtime, not to
# the repository being scanned, and excluding it would mean a note like "parser unavailable
# (ImportError)" gets rejected, leaving an operator with no clue what actually happened.
OWN_WORDS = frozenset(
    {
        "--no-build",
        "--build",
        "--review",
        "--out",
        "--all",
        "--jobs",
        "CLI",
        "JSON",
        "PATH",
        "LOC",
        "KB",
        "OK",
        "llm_disabled",
        "mine_disabled",
        "lanes_unavailable",
        "lanes_timed_out",
        "git_history",
        "code_structure",
        "measurement.json",
        "codebase_repos.json",
        "codebase_repos.csv",
        "REPO_INTRINSIC",
        "ENVIRONMENT",
        "TIMEOUT",
        "UNCLASSIFIED",
        "NONE",
        "ImportError",
        "ModuleNotFoundError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutExpired",
        "JSONDecodeError",
        "ValueError",
        "RuntimeError",
        "MemoryError",
        "UnicodeDecodeError",
        "NotADirectoryError",
        "IsADirectoryError",
    }
)


# ---------------------------------------------------------------------------
# Kinds: the vocabulary for saying what may appear at a single leaf
# ---------------------------------------------------------------------------

DROP = object()  # what a kind returns when it wants its key removed outright


def _refuse(where: str, expected: str, value) -> EmissionRefused:
    return EmissionRefused(
        f"refusing to write: {where} is declared as {expected} but was handed "
        f"{type(value).__name__} {str(value)[:60]!r}"
    )


class Kind:
    """One permitted shape. `apply` returns the value to emit, or raises.

    None is acceptable wherever a value is: a field with nothing to say is null, per the
    null-versus-zero rule. Refusing null would push collectors into inventing a
    measurement, which is the one failure mode worth more than a missing field.
    """

    label = "value"

    def apply(self, value, where: str):
        raise NotImplementedError


class Number(Kind):
    label = "number"

    def apply(self, value, where):
        if value is None:
            return None
        # bool is an int in Python; a flag written into a count is a collector bug.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _refuse(where, "a number", value)
        if isinstance(value, float) and not math.isfinite(value):
            raise EmissionRefused(
                f"refusing to write: {where} is {value!r}, which has no JSON spelling"
            )
        return value


class Boolean(Kind):
    label = "boolean"

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, bool):
            raise _refuse(where, "a boolean", value)
        return value


class Enum(Kind):
    """A value from one closed vocabulary.

    `unknown="other"` folds anything outside the table to the literal `"other"`, which is
    what a field wants when the world is wider than our table and the shape of the answer
    still matters. `unknown="reject"` stops the run, which is what a field wants when the
    vocabulary is entirely ours and an outside value can only mean a bug.
    """

    label = "enum"

    def __init__(self, name: str, vocabulary, unknown: str = "reject"):
        self.name = name
        self.vocabulary = frozenset(vocabulary)
        self.unknown = unknown

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise _refuse(where, f"one of the {self.name} vocabulary", value)
        if value in self.vocabulary:
            return value
        if self.unknown == "other":
            return "other"
        raise EmissionRefused(
            f"refusing to write: {where} is {value[:80]!r}, which is not in the closed "
            f"{self.name} vocabulary. Either it belongs there -- add it -- or it is a "
            f"string read out of the repository, and those do not leave."
        )


class Token(Kind):
    """A bounded string that must match a pattern end to end."""

    label = "token"

    def __init__(self, name: str, pattern: str):
        self.name = name
        self.pattern = re.compile(pattern)

    def apply(self, value, where):
        # An empty token is nothing to say rather than a value, so it is emitted as null.
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not self.pattern.fullmatch(value):
            raise _refuse(where, f"a {self.name}", value)
        return value


class Prose(Kind):
    """One short line of free text, dropped whole if anything disqualifies it.

    Refusal is total on purpose. A half-scrubbed line with a placeholder where the
    interesting noun used to be is worse than no line, and it hides the fact that something
    was taken out.
    """

    def __init__(
        self, max_words: int, allow: frozenset[str] = frozenset(), label: str = "sentence"
    ):
        self.max_words = max_words
        self.allow = allow
        self.label = label

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise _refuse(where, "a sentence", value)
        if not value.strip():
            return ""
        if rejection_reason(value, self.max_words, self.allow):
            return ""
        return " ".join(value.split())


class NotCollected(Kind):
    """Declared, never filled in, and named with its reason in `review()`."""

    label = "not collected"

    def __init__(self, shape, reason: str):
        self.shape = shape  # None, [] or {} -- or DROP to remove the key outright
        self.reason = reason

    def apply(self, value, where):
        if self.shape is DROP:
            return DROP
        # A fresh empty container of the declared shape, never the caller's object.
        return type(self.shape)() if isinstance(self.shape, (list, dict)) else None


class ListOf(Kind):
    label = "list"

    def __init__(self, item: Kind):
        self.item = item

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, list):
            raise _refuse(where, "a list", value)
        out = []
        for i, item in enumerate(value):
            emitted = self.item.apply(item, f"{where}[{i}]")
            # A line of prose that came back empty was refused; it leaves the list rather
            # than shipping as a blank entry that looks like a measurement.
            if emitted is DROP or (emitted == "" and isinstance(self.item, Prose)):
                continue
            out.append(emitted)
        return out


class MapOf(Kind):
    """A dynamic map whose KEYS come from a closed vocabulary.

    A map key is as much a string out of the repository as a map value is, so an unknown
    key folds to `"other"` rather than being written. Numeric values that collide there are
    summed, because the total under `"other"` is the honest reading of "everything else".
    """

    label = "map"

    def __init__(self, name: str, vocabulary, value_kind: Kind):
        self.name = name
        self.vocabulary = frozenset(vocabulary)
        self.value_kind = value_kind

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise _refuse(where, "a map", value)
        out: dict = {}
        for raw_key, raw_value in value.items():
            key = raw_key if isinstance(raw_key, str) and raw_key in self.vocabulary else "other"
            emitted = self.value_kind.apply(raw_value, f"{where}.{key}")
            if key in out and isinstance(emitted, (int, float)) and not isinstance(emitted, bool):
                out[key] = out[key] + emitted
            else:
                out[key] = emitted
        return out


class Object(Kind):
    """A fixed set of named fields. An undeclared name stops the run."""

    label = "object"

    def __init__(self, fields: dict):
        self.fields = fields

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise _refuse(where, "an object", value)
        out: dict = {}
        for name, raw in value.items():
            kind = self.fields.get(name)
            if kind is None:
                raise EmissionRefused(
                    f"refusing to write: {where}.{name} is not declared in schema.SPEC. "
                    f"Nothing undeclared leaves this machine. If the field is safe, declare "
                    f"it with the kind of thing it may hold; if it carries anything read out "
                    f"of the repository, it does not belong in the output at all."
                )
            emitted = kind.apply(raw, f"{where}.{name}" if where else name)
            if emitted is not DROP:
                out[name] = emitted
        return out


def NOT_COLLECTED(shape, reason: str) -> NotCollected:  # noqa: N802 -- named like a kind
    """Declared, permanently empty, and shown as such in `review()`."""
    return NotCollected(shape, reason)


def OMITTED(reason: str) -> NotCollected:  # noqa: N802 -- named like a kind
    """Declared and dropped outright: a placeholder would imply we had the answer."""
    return NotCollected(DROP, reason)


# ---------------------------------------------------------------------------
# The kinds, named once
# ---------------------------------------------------------------------------

NUMBER = Number()
BOOL = Boolean()
SENTENCE = Prose(MAX_SENTENCE_WORDS)
SUMMARY = Prose(120, label="summary")
OWN_PROSE = Prose(120, allow=OWN_WORDS, label="tool note")
DIGEST = Token("content digest", r"[0-9a-f]{64}")
HANDLE = Token("display handle", r"repo-[0-9a-f]{12}")
TIMESTAMP = Token(
    "timestamp",
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?",
)
RATIO = Token("ratio", r"\d+:\d+|0 tests")
VERSION = Token("version string", r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,80}")
MODEL_ID = Token("tool id", r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}")
# Either `owner/name` or, where a host supports it, a nested group. Limited to four
# segments and to characters a repository name is allowed to contain, which is what stops a
# remote that turns out to be a directory from leaving disguised as a name -- an absolute
# path being the very thing that must not escape. None is accepted, and is the truthful
# answer for a checkout with no remote.
REPO_FULL_NAME = Token(
    "repository name", r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,3}"
)
# A third party's name -- specifically the company that owns the repository, as opposed to
# the operator running this tool. This is the second deliberate carve-out from the general
# no-identity rule, bounded with the same token pattern used for REPO_FULL_NAME; see
# `vocab.COMPANY_NAME_PATTERN` for why each excluded character is excluded. `None` is a
# valid value here, standing for a checkout that gave no evidence of who owns it.
COMPANY_NAME = Token("company name", vocab.COMPANY_NAME_PATTERN)


# ---------------------------------------------------------------------------
# Why a declared field is never filled in. Written once, so the same words appear in the
# code and in the review the operator reads.
# ---------------------------------------------------------------------------

_NO_SCAN = (
    "this tool never searches a repository for its own credentials, and never "
    "records that it found one or where it lives"
)
_NO_PATHS = "the names of files and directories are not collected"
_NO_ENV_NAMES = "the names of environment variables are not collected"
_NO_IDENTITY = (
    "the names and addresses of the people who wrote the code, and the name of "
    "the repository itself, are not collected"
)
_NO_AUTHOR_DOMAINS = (
    "the email domains of the people who committed are not collected: a most-common domain "
    "is read off author addresses, which the identity rule above rules out by name, and the "
    "four families that are read answer the question without it"
)
_NO_CONFIG_IDENTITY = (
    "internal hostnames, registry paths and cloud project names are not collected: they are "
    "paths and hosts as much as they are identity, and they are the weakest of the company "
    "signals -- the widest reach for the least evidence"
)
_NO_HASHES = "no commit identifier and no object hash is recorded"
_NO_REF_NAMES = "branch and tag names are not collected: they routinely carry product identity"
_NO_COMMANDS = "the command lines this tool ran inside the checkout are not recorded"
_NO_COMMIT_PROSE = "commit subjects and commit messages are not collected"
_PLATFORM_ASSIGNED = "an identifier the receiving system fills in when it loads the row"


# ---------------------------------------------------------------------------
# The declaration table
# ---------------------------------------------------------------------------

_TREE = Object(
    {
        "schema_version": VERSION,
        "repo_path": OMITTED(_NO_IDENTITY),
        "repo_name": OMITTED(_NO_IDENTITY),
        "primary_language": Enum("language", vocab.LANGUAGES, unknown="other"),
        "secondary_languages": ListOf(Enum("language", vocab.LANGUAGES, unknown="other")),
        "loc_by_language": MapOf("language", vocab.LANGUAGES, NUMBER),
        "file_count_by_language": MapOf("language", vocab.LANGUAGES, NUMBER),
        "jvm_dotnet_loc_share": NUMBER,
        "detected_frameworks": ListOf(Enum("framework", vocab.FRAMEWORKS, unknown="other")),
        "project_type": Enum("project type", vocab.PROJECT_TYPES, unknown="other"),
        "total_loc": NUMBER,
        "total_source_files": NUMBER,
        "median_file_size_loc": NUMBER,
        "p90_file_size_loc": NUMBER,
        "god_files_over_500_loc": NUMBER,
        "god_files_over_1000_loc": NUMBER,
        "top_largest_files": ListOf(
            Object(
                {
                    "path": NOT_COLLECTED(None, _NO_PATHS),
                    "loc": NUMBER,
                }
            )
        ),
        "generated_files_excluded": NUMBER,
        "iac_loc": NUMBER,
        "iac_file_count": NUMBER,
        "iac_loc_by_type": MapOf("infrastructure type", vocab.IAC_TYPES, NUMBER),
        "test_spec_files": NUMBER,
        "test_fixture_files": NUMBER,
        "test_source_ratio": RATIO,
        "test_spec_sample": NOT_COLLECTED([], _NO_PATHS),
        "test_framework": ListOf(Enum("test framework", vocab.TEST_FRAMEWORKS, unknown="other")),
        "test_config_files": ListOf(Enum("test config", vocab.TEST_CONFIG_FILES, unknown="other")),
        "coverage_tooling": Enum("coverage tool", vocab.COVERAGE_TOOLING, unknown="other"),
        "coverage_threshold": NUMBER,
        "package_managers": ListOf(
            Enum("package manager", vocab.PACKAGE_MANAGERS, unknown="other")
        ),
        "manifests_found": ListOf(Enum("manifest", vocab.MANIFESTS, unknown="other")),
        "lockfiles_found": ListOf(Enum("lockfile", vocab.LOCKFILES, unknown="other")),
        "lockfiles_expected": ListOf(Enum("lockfile", vocab.LOCKFILES_EXPECTED, unknown="other")),
        "direct_runtime_deps": NUMBER,
        "direct_dev_deps": NUMBER,
        "total_transitive_deps": NUMBER,
        "dep_update_tooling": Enum("dependency bot", {"none", "Dependabot", "Renovate"}),
        "ci_systems": ListOf(Enum("ci system", vocab.CI_SYSTEMS, unknown="other")),
        "ci_config_files": NOT_COLLECTED([], _NO_PATHS),
        "ci_runs_tests": BOOL,
        "ci_runs_lint": BOOL,
        "ci_runs_typecheck": BOOL,
        "ci_has_deploy": BOOL,
        "ci_present": BOOL,
        # How the three ci_runs_* answers were arrived at. Where one of them is null, the
        # configuration defeated the parser; it does not mean the pipeline sits idle.
        "ci_analysis_method": Enum(
            "ci analysis method", frozenset({"parsed", "parser_unavailable", "no_ci"})
        ),
        "hardcoded_secret_hits": NOT_COLLECTED(None, _NO_SCAN),
        "secret_hit_details": NOT_COLLECTED([], _NO_SCAN),
        "env_files_committed": NOT_COLLECTED([], _NO_PATHS),
        "dep_audit_in_ci": BOOL,
        "input_validation_patterns": ListOf(
            Enum("validation library", vocab.VALIDATION_LIBS, unknown="other")
        ),
        "linters_and_formatters": MapOf(
            "linter", vocab.LINTERS, Enum("lint config", vocab.LINT_CONFIG_FILES, unknown="other")
        ),
        "has_lint_config": BOOL,
        "readme": Enum("readme name", vocab.README_NAMES, unknown="other"),
        "readme_loc": NUMBER,
        "readme_sections": ListOf(Enum("readme section", vocab.README_SECTIONS, unknown="other")),
        "changelog": Enum("changelog name", vocab.CHANGELOG_NAMES, unknown="other"),
        "contributing_guide": Enum("contributing name", vocab.CONTRIBUTING_NAMES, unknown="other"),
        "has_pr_template": BOOL,
        "has_issue_template": BOOL,
        "demo_signals": Object({name: BOOL for name in vocab.DEMO_SIGNALS}),
        "has_dockerfile": BOOL,
        "has_docker_compose": BOOL,
        "has_devcontainer": BOOL,
        "has_nix": BOOL,
        "env_example_file": NOT_COLLECTED(None, _NO_ENV_NAMES),
        "env_vars_referenced_in_source": NOT_COLLECTED([], _NO_ENV_NAMES),
        "env_vars_in_example": NOT_COLLECTED([], _NO_ENV_NAMES),
        "env_vars_missing_from_example": NOT_COLLECTED([], _NO_ENV_NAMES),
        "logging_framework": Enum("logging library", vocab.LOGGING_LIBRARIES, unknown="other"),
        "error_tracking": Enum("error tracker", vocab.ERROR_TRACKING, unknown="other"),
        "has_health_endpoint": BOOL,
        "has_metrics": BOOL,
        "class_signals": Object(
            {
                "dep_keyword_hits": Object(
                    {
                        group: ListOf(
                            Enum("dependency keyword", vocab.DEP_KEYWORDS, unknown="other")
                        )
                        for group in vocab.DEP_KEYWORD_GROUPS
                    }
                ),
                "notebook_count": NUMBER,
                "terraform_file_count": NUMBER,
                "terraform_present": BOOL,
                "terraform_loc": NUMBER,
                "k8s_manifest_count": NUMBER,
                "k8s_loc": NUMBER,
                "helm_present": BOOL,
                "helm_file_count": NUMBER,
                "helm_loc": NUMBER,
                "pulumi_present": BOOL,
                "ansible_present": BOOL,
                "ansible_file_count": NUMBER,
                "ansible_loc": NUMBER,
                "cloudformation_file_count": NUMBER,
                "cloudformation_loc": NUMBER,
                "docker_compose_file_count": NUMBER,
                "docker_compose_loc": NUMBER,
                "dockerfile_count": NUMBER,
                "dockerfile_loc": NUMBER,
                "ui_component_file_count": NUMBER,
                "sql_file_count": NUMBER,
                "sql_loc": NUMBER,
                "css_loc": NUMBER,
                "css_loc_ratio": NUMBER,
                "data_file_count": NUMBER,
                "iac_loc": NUMBER,
                "iac_file_count": NUMBER,
                "iac_loc_by_type": MapOf("infrastructure type", vocab.IAC_TYPES, NUMBER),
            }
        ),
    }
)

_GIT = Object(
    {
        "head_sha": OMITTED(_NO_HASHES),
        "effective_tip_sha": OMITTED(_NO_HASHES),
        "anonymizer_commit": OMITTED(_NO_HASHES),
        "latest_tag": OMITTED(_NO_REF_NAMES),
        "top_authors": OMITTED(_NO_IDENTITY),
        "anonymizer_commit_detected": BOOL,
        "anonymizer_commit_excluded": BOOL,
        "total_commits": NUMBER,
        "total_commits_including_anonymizer": NUMBER,
        "first_commit": TIMESTAMP,
        "last_commit": TIMESTAMP,
        "span_days": NUMBER,
        "recency_days": NUMBER,
        "human_authors": NUMBER,
        "bot_authors": NUMBER,
        "bot_commit_count": NUMBER,
        "bot_commit_ratio": NUMBER,
        "conventional_rate_last_200": NUMBER,
        "tag_count": NUMBER,
        "semver_tag_count": NUMBER,
        "merge_commit_count": NUMBER,
        "looks_like_burst_copy": BOOL,
        "class_a_count": NUMBER,
        "class_b_count": NUMBER,
        "class_c_pre_count": NUMBER,
        "class_d_bug_count": NUMBER,
        "confirmed_candidate_count": NUMBER,
        "provisional_candidate_count": NUMBER,
        "analyzed_commits": NUMBER,
        "full_history_scanned": BOOL,
        "commits_by_month": ListOf(NUMBER),
        "active_days": NUMBER,
        # Subjects and hashes travel in the per-class commit lists, and the collector leaves
        # every one of them behind. Declaring the fields anyway means that reversing that
        # decision stops the run rather than shipping without anyone noticing.
        "class_a_commits": OMITTED(_NO_COMMIT_PROSE),
        "class_b_commits": OMITTED(_NO_COMMIT_PROSE),
        "class_c_pre_commits": OMITTED(_NO_COMMIT_PROSE),
        "class_d_bug_commits": OMITTED(_NO_COMMIT_PROSE),
        "scanned_commits": OMITTED(_NO_COMMIT_PROSE),
        "candidates": OMITTED(_NO_COMMIT_PROSE),
        "commit_subjects": OMITTED(_NO_COMMIT_PROSE),
    }
)

_STRUCTURE = Object(
    {
        "probe": Enum("probe name", {"code_structure"}),
        "ok": BOOL,
        "error": OWN_PROSE,
        "note": OWN_PROSE,
        "bounds": OWN_PROSE,
        "prod_loc": NUMBER,
        "source_files": NUMBER,
        "test_files": NUMBER,
        "prod_loc_deterministic": NUMBER,
        "source_files_deterministic": NUMBER,
        "test_files_deterministic": NUMBER,
        "n_functions_seen": NUMBER,
        "n_files_parsed": NUMBER,
        "n_files_parse_failed": NUMBER,
        "source_files_unparsed": NUMBER,
        "decisions_gini_top1pct": NUMBER,
        "error_handling_per_kloc": NUMBER,
        "structure_probe_mode": Enum(
            "probe mode",
            {"deterministic", "agentic_unavailable", "agentic_confirmed", "agentic_contradicted"},
        ),
        "structure_unknown_files": NUMBER,
        "structure_unknown_exts": NOT_COLLECTED(None, _NO_PATHS),
        "structure_agentic_languages": NOT_COLLECTED(None, _NO_PATHS),
        "structure_agentic_note": SENTENCE,
    }
)

_HISTORY = Object(
    {
        "probe": Enum("probe name", {"git_history"}),
        "ok": BOOL,
        "error": OWN_PROSE,
        "ref_analysed": NOT_COLLECTED(None, _NO_REF_NAMES),
        "commit_sha": NOT_COLLECTED(None, _NO_HASHES),
        "ref_commits": NUMBER,
        "ref_candidates_considered": NUMBER,
        "ref_choice_reason": OWN_PROSE,
        "ref_choice_overrode_deepest": BOOL,
        "anonymizer_tip_detected": BOOL,
        "development_substance": NUMBER,
        "development_substance_note": SENTENCE,
        "development_substance_error": OWN_PROSE,
        "development_sampled_commits": NUMBER,
        "development_real_in_sample": NUMBER,
        "real_commits": NUMBER,
        "mineable_commits": NUMBER,
        "human_authors": NUMBER,
        "history_probe_mode": Enum("probe mode", {"deterministic", "agentic"}),
        "history_model": MODEL_ID,
    }
)

# What happened when this repository's own install, build and test commands were run. The
# block is wide because the question it answers splits three ways and a reader has to be able
# to tell the three apart: what the repository did, what this machine could not offer it, and
# what the clock never reached. Every verdict therefore arrives with the reason beside it.
#
# Nothing here carries a command line, a package name, a path or a log line. The commands are
# declared and permanently empty (`build_commands_tried`), because which install hook a tree
# runs is a fact about the tree that scoring it never needed.
_BUILD_LEVEL = Enum("build level", {"none", "discover", "full"})

_BUILD = Object(
    {
        "probe": Enum("probe name", {"build"}),
        "ok": BOOL,
        "error": OWN_PROSE,
        "note": OWN_PROSE,
        "build_skipped": BOOL,
        # The level that RAN and the level that was ASKED FOR, as a pair. One field cannot
        # carry both facts, and without the second a cheaper check somebody chose looks
        # identical to an expensive one the budget cut short -- which is the case that leaves
        # the executed index unscored and is the one an operator can fix by raising a number.
        "build_level": _BUILD_LEVEL,
        "build_level_requested": _BUILD_LEVEL,
        "build_level_fallback_reason": OWN_PROSE,
        "run_budget_exhausted": BOOL,
        "build_probe_mode": Enum("probe mode", {"deterministic", "agentic"}),
        "build_probe_model": MODEL_ID,
        "agentic_fallback_reason": OWN_PROSE,
        "toolchain": Enum("toolchain", vocab.TOOLCHAINS, unknown="other"),
        "build_attempted": BOOL,
        "build_commands_tried": NOT_COLLECTED([], _NO_COMMANDS),
        "install_ok": BOOL,
        "build_ok": BOOL,
        # The executed score, 0 to 4: the build worked, the suite ran, something passed,
        # coverage came back above nothing. A small integer with no detail in it, and the only
        # number in this document earned by running the project rather than reading its files.
        "observed_runnability": NUMBER,
        # Why that score is absent, and whose absence it is. A null with no reason beside it
        # is what a reader marks a repository down for.
        "observed_runnability_reason": OWN_PROSE,
        # Its companion at the cheaper level, 0 to 3: dependencies resolved, the build
        # succeeded, tests were found. Every term executed and none of them needing a suite,
        # which is what keeps a run that fell back gradeable.
        "discover_runnability": NUMBER,
        "tests_discovered": BOOL,
        "build_and_tests_ran": BOOL,
        "failure_class": Enum(
            "failure class",
            {"NONE", "REPO_INTRINSIC", "ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED"},
        ),
        "repo_intrinsic_failure": BOOL,
        "timed_out": BOOL,
        "coverage_pct": NUMBER,
        "coverage_method": OWN_PROSE,
        "coverage_unsupported_reason": OWN_PROSE,
        "build_remediation_effort": Enum(
            "remediation effort",
            {"none", "trivial", "moderate", "substantial", "infeasible", "unknown"},
        ),
        "build_remediation_notes": OWN_PROSE,
        # THE INTERPRETER THIS TREE NAMED, AND THE ONE IT RAN ON. Without these five a reader
        # cannot separate a repository that does not build from one this machine built with
        # the wrong interpreter, and those call for opposite conclusions. The lanes come from
        # our own table; the versions are version tokens, which is why a declared range is
        # normalised to two numbers rather than emitted as the comparison the manifest wrote.
        "runtime_lanes_declared": ListOf(Enum("runtime lane", vocab.RUNTIME_LANES)),
        "runtime_lanes_unsatisfied": ListOf(Enum("runtime lane", vocab.RUNTIME_LANES)),
        "runtime_requested": ListOf(VERSION),
        "runtime_used": ListOf(VERSION),
        "runtime_resolution_note": OWN_PROSE,
        # A repair pass this tool does not offer. The fields ship so that "never offered" and
        # "offered, and nothing needed it" stay distinguishable; a reader seeing neither field
        # could not tell them apart, and they mean different things about the repository.
        "repair_offered": BOOL,
        "repair_candidates_n": NUMBER,
        "repair_attempted_n": NUMBER,
        "repair_succeeded_n": NUMBER,
        "repair_refused_n": NUMBER,
        "repair_rejected_source_edit_n": NUMBER,
        "repair_seconds": NUMBER,
    }
)

# Which company owns the repository -- the one place in this schema where a third-party
# name is permitted, so its shape is kept deliberately small: a short list of candidate
# names, which evidence families backed each one, a single chosen outcome, and an industry
# guess drawn from this tool's own vocabulary.
#
# There is no confidence number anywhere in this block, and that is on purpose rather than
# an oversight. `identity.py` does tally how many files repeat a given copyright header
# internally, since recurrence is exactly what makes that evidence worth trusting, but the
# tally itself never leaves that function. A count attached to a candidate name would be a
# confidence score by another name, and a confidence score next to a company name reads like
# a grade. Which evidence families support a name is already enough for a reader to judge
# it -- "found in your licence file and your git remote" tells the whole story.
_COMPANY_IDENTITY = Object(
    {
        "outcome": Enum("company inference outcome", vocab.COMPANY_OUTCOMES),
        "company_candidates": ListOf(
            Object(
                {
                    "company_name": COMPANY_NAME,
                    "signal_families": ListOf(Enum("company signal family", vocab.SIGNAL_FAMILIES)),
                }
            )
        ),
        "industry": Enum("industry", vocab.INDUSTRIES),
        "industry_signals": ListOf(Enum("industry signal", vocab.INDUSTRY_SIGNALS)),
        "author_email_domains": OMITTED(_NO_AUTHOR_DOMAINS),
        "config_and_ci_identity": OMITTED(_NO_CONFIG_IDENTITY),
    }
)

_MEASUREMENT = Object(
    {
        "measurer_version": VERSION,
        "measured_at": TIMESTAMP,
        "repo_digest": DIGEST,
        # Here so a record can be matched to its repository, which the digest cannot do since
        # it identifies a set of file contents. A checkout without a remote yields null.
        "real_repo_name": REPO_FULL_NAME,
        # Declared next to `real_repo_name` because together they answer one two-part
        # question: that field identifies which repository this is, this one identifies
        # who owns it.
        "company_identity": _COMPANY_IDENTITY,
        "variant": Enum("variant", {"ext"}),
        "capacity": NUMBER,
        "tree": _TREE,
        "git": _GIT,
        "classification": Object(
            {
                "primary_class": Enum("repo class", vocab.REPO_CLASSES),
                "class_confidence": MapOf("repo class", vocab.REPO_CLASSES, NUMBER),
                "is_monorepo": BOOL,
                "is_likely_demo": BOOL,
                "demo_reasoning": ListOf(Enum("demo signal", vocab.DEMO_SIGNALS)),
            }
        ),
        "ext_signals": Object(
            {
                "structure": _STRUCTURE,
                "history": _HISTORY,
                # None still passes, and is what a run with the check switched off writes.
                "build": _BUILD,
            }
        ),
    }
)

_CODEBASE_REPOS = Object(
    {
        "id": NOT_COLLECTED(None, _PLATFORM_ASSIGNED),
        "codebase_id": NOT_COLLECTED(None, _PLATFORM_ASSIGNED),
        "service_id": NOT_COLLECTED(None, _PLATFORM_ASSIGNED),
        "repo_digest": DIGEST,
        "real_repo_name": NOT_COLLECTED(None, _NO_IDENTITY),
        "fake_repo_name": HANDLE,
        # `partial` earns its place. Were a row to report `measured` after a check had died,
        # it would be asserting a completeness the run never achieved, leaving the explanation
        # buried in a nested block that nobody filters on. The kind of gap is named here; which
        # particular one it was stays in the block.
        "status": Enum("status", {"measured", "partial"}),
        "skip_reason": Enum(
            "skip reason", {"llm_disabled", "mine_disabled", "lanes_unavailable", "lanes_timed_out"}
        ),
        "measured_at": TIMESTAMP,
        "loc": NUMBER,
        "zip_bytes": NUMBER,
        "excluded_loc": NUMBER,
        "languages": MapOf("language", vocab.LANGUAGES, NUMBER),
        "primary_language": Enum("language", vocab.LANGUAGES, unknown="other"),
        "frontend_pct": NUMBER,
        "backend_pct": NUMBER,
        "test_loc": NUMBER,
        "test_code_files": NUMBER,
        "test_spec_files": NUMBER,
        "test_ratio": NUMBER,
        "test_source_ratio": RATIO,
        "test_framework": ListOf(Enum("test framework", vocab.TEST_FRAMEWORKS, unknown="other")),
        "has_ci": BOOL,
        "ci_present": BOOL,
        "ci_runs_tests": BOOL,
        "detected_frameworks": ListOf(Enum("framework", vocab.FRAMEWORKS, unknown="other")),
        "commit_count": NUMBER,
        "author_count": NUMBER,
        "first_commit_at": TIMESTAMP,
        "last_commit_at": TIMESTAMP,
        "active_days": NUMBER,
        "span_days": NUMBER,
        "commits_by_month": ListOf(NUMBER),
        "pr_count": NUMBER,
        "issue_count": NUMBER,
        "repo_class": Enum("repo class", vocab.REPO_CLASSES),
        "is_likely_demo": BOOL,
        "quality_score": NUMBER,
        "build_ok": BOOL,
        "testable_at_head": BOOL,
        "capacity": NUMBER,
    }
)

SPEC: dict[str, Object] = {
    "codebase_repos": _CODEBASE_REPOS,
    "measurement": _MEASUREMENT,
}


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def enforce(document: str, payload: dict) -> dict:
    """Validate `payload` leaf by leaf against its declarations and return a fresh copy, or raise.

    Each key must be one this module has declared -- or, for a dynamic map field, a key
    drawn from that map's own closed vocabulary -- and each value must match its declared
    kind. There is no partial handling of an undeclared or malformed field: it is never
    quietly stripped, shortened or passed through with a warning, the call simply raises and
    the caller has to stop rather than write a document with something unvetted inside it.
    """
    spec = SPEC.get(document)
    if spec is None:
        raise EmissionRefused(
            f"refusing to write: no schema is declared for {document!r}. "
            f"The declared documents are {sorted(SPEC)}."
        )
    return spec.apply(payload, document)


# ---------------------------------------------------------------------------
# The key sets, read off the declarations
# ---------------------------------------------------------------------------


def _gather(kind: Kind, fields: set[str], vocabulary: set[str]) -> None:
    if isinstance(kind, Object):
        fields.update(kind.fields)
        for child in kind.fields.values():
            _gather(child, fields, vocabulary)
    elif isinstance(kind, ListOf):
        _gather(kind.item, fields, vocabulary)
    elif isinstance(kind, MapOf):
        vocabulary.update(kind.vocabulary)
        vocabulary.add("other")
        _gather(kind.value_kind, fields, vocabulary)
    elif isinstance(kind, Enum):
        vocabulary.update(kind.vocabulary)
        vocabulary.add("other")


def _key_sets() -> tuple[frozenset[str], frozenset[str]]:
    fields: set[str] = set()
    vocabulary: set[str] = set()
    for spec in SPEC.values():
        _gather(spec, fields, vocabulary)
    return frozenset(fields), frozenset(fields | vocabulary)


def _is_closed(kind: Kind) -> bool:
    if isinstance(kind, (Enum, Token, MapOf)):
        return True
    if isinstance(kind, ListOf):
        return _is_closed(kind.item)
    return False


def _closed_vocabulary_keys() -> frozenset[str]:
    """List the fields whose values were already validated against a fixed vocabulary table.

    A later pass that scrubs free text for anything path- or identifier-shaped is told to
    leave these fields alone. Running that scrub over a value already checked against a
    table adds no safety and actively breaks it -- it would mangle "Next.js" as though it
    were a filename, or "GitHub Actions" as though it were a class name.
    """
    found: set[str] = set()

    def walk(kind: Kind) -> None:
        if isinstance(kind, Object):
            for name, child in kind.fields.items():
                if _is_closed(child):
                    found.add(name)
                walk(child)
        elif isinstance(kind, ListOf):
            walk(kind.item)
        elif isinstance(kind, MapOf):
            walk(kind.value_kind)

    for spec in SPEC.values():
        walk(spec)
    return frozenset(found)


#: Every declared field name, across both documents. What a writer checks a key against.
DECLARED_FIELDS, DECLARED_KEYS = _key_sets()
#: DECLARED_KEYS is DECLARED_FIELDS widened by every closed-vocabulary member and "other":
#: the full set of strings this tool is capable of writing as a key or an enum value.
CLOSED_VOCABULARY_KEYS = _closed_vocabulary_keys()


# ---------------------------------------------------------------------------
# The review an operator reads before deciding to share anything
# ---------------------------------------------------------------------------

_GROUPS = (
    ("numbers", "NUMBERS"),
    ("booleans", "BOOLEANS"),
    ("enums", "CLOSED-VOCABULARY VALUES"),
    ("identity", "CONTENT DIGEST AND DISPLAY HANDLE"),
    ("timestamps", "TIMESTAMPS"),
    ("sentences", "ONE-LINE DESCRIPTIONS (the only free text, shown in full)"),
    ("notes", "THIS TOOL'S OWN STATUS NOTES"),
    ("unmeasured", "EMITTED WITH NO VALUE (the field ships, the value was not collected)"),
)

# What stands in for a value the run never produced. "not collected" is itself information
# an operator is entitled to: the field ships either way.
_NOT_COLLECTED_MARK = "not collected"
_EMPTY_MARK = "not collected (empty)"


def _group_of(kind: Kind) -> str:
    if isinstance(kind, Number):
        return "numbers"
    if isinstance(kind, Boolean):
        return "booleans"
    if isinstance(kind, Enum):
        return "enums"
    if kind is DIGEST or kind is HANDLE:
        return "identity"
    if kind is TIMESTAMP:
        return "timestamps"
    if isinstance(kind, Prose):
        return "notes" if kind.allow else "sentences"
    return "enums"


def _withheld(kind: Kind, name: str, into: dict) -> None:
    """List every field this tool declares but never populates, with the reason for each.

    This reads the field declarations themselves, not any particular run's payload, because
    a statement like "commit messages are never collected" is true of the tool as a whole
    and should show up in the review regardless of whether this run's repository even had
    commit messages to withhold.
    """
    if isinstance(kind, NotCollected):
        into.setdefault(name, kind.reason)
    elif isinstance(kind, Object):
        for field, child in kind.fields.items():
            _withheld(child, field, into)
    elif isinstance(kind, ListOf):
        _withheld(kind.item, name, into)
    elif isinstance(kind, MapOf):
        _withheld(kind.value_kind, name, into)


def _children(kind: Kind, value, path: str) -> list[tuple[Kind, object, str]]:
    """What one container expands into. Empty when it holds nothing."""
    if isinstance(kind, Object) and isinstance(value, dict):
        return [(kind.fields[k], v, f"{path}.{k}") for k, v in value.items() if k in kind.fields]
    if isinstance(kind, ListOf) and isinstance(value, list):
        return [(kind.item, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(kind, MapOf) and isinstance(value, dict):
        return [(kind.value_kind, v, f"{path}.{k}") for k, v in value.items()]
    return []


def _walk(kind: Kind, value, path: str, found: dict) -> None:
    """Note down every field that will be emitted, those with no value included.

    Anyone weighing up whether to pass these files on needs to see the complete list rather
    than whichever entries happen to be filled in. That `quality_score` was not collected is
    a fact about this run, and they are owed it before anything goes anywhere.
    """
    if isinstance(kind, NotCollected):
        return
    if isinstance(kind, (Object, ListOf, MapOf)):
        children = _children(kind, value, path)
        if children:
            for child_kind, child_value, child_path in children:
                _walk(child_kind, child_value, child_path, found)
        else:
            found.setdefault("unmeasured", []).append(
                (path, _NOT_COLLECTED_MARK if value is None else _EMPTY_MARK)
            )
        return
    if value is None:
        found.setdefault("unmeasured", []).append((path, _NOT_COLLECTED_MARK))
        return
    if value == "":
        found.setdefault("unmeasured", []).append((path, _EMPTY_MARK))
        return
    found.setdefault(_group_of(kind), []).append((path, value))


def review(documents: dict[str, dict], full: bool = False) -> str:
    """Summarise, in plain language, what one or more validated documents contain.

    Nothing about calling this sends anything anywhere -- it only reads back what
    `enforce` already produced. `documents` maps a display label to a payload that has
    already passed `enforce`. With `full=True` the summary lists every field and its actual
    value, empty ones included. With `full=False` it instead gives, per kind, how many
    fields of each type were emitted, the text of every free-form note verbatim, and the
    list of fields this tool never collects at all -- arguably the most useful part of the
    review, and the part a raw field-by-field dump tends to bury.
    """
    found: dict[str, list] = {}
    withheld: dict[str, str] = {}
    for display, payload in documents.items():
        document = display.split(".json")[0].split(".csv")[0]
        spec = SPEC.get(document)
        if spec is None:
            continue
        _walk(spec, payload, display, found)
        _withheld(spec, document, withheld)

    lines = [
        "",
        "REVIEW -- this is everything the output files contain. They were written to this",
        "machine and nothing has been sent anywhere: this tool opens no network connection,",
        "so the only copy is the one on your disk. The repository is yours and so are these",
        "files. Read what follows, then decide whether to share them.",
        "",
    ]
    for key, heading in _GROUPS:
        entries = found.get(key, [])
        if not entries:
            continue
        show = key in ("sentences", "identity", "notes") or full
        lines.append(f"{heading} -- {len(entries)}")
        if show:
            for path, value in entries:
                lines.append(f"    {path} = {value}")
        elif key == "enums":
            distinct = sorted({str(v) for _, v in entries})
            lines.append(f"    {', '.join(distinct[:40])}")
        lines.append("")

    if withheld:
        lines.append(f"NOT COLLECTED BY POLICY -- {len(withheld)} declared fields, never filled in")
        for name, reason in sorted(withheld.items()):
            lines.append(f"    {name}: {reason}")
        lines.append("")

    lines.append("Nothing in this tool searches your source for secret-shaped strings. No author")
    lines.append("name or address, no commit message, no branch or tag name and no file path is")
    lines.append("emitted. Every value above was checked against a declared allowlist before it")
    lines.append("was written, and anything undeclared would have stopped the run instead. One")
    lines.append("disclosed exception sits beside that: the content digest reads every file's")
    lines.append("bytes in order to hash them. It keeps nothing it read.")
    lines.append("")
    return "\n".join(lines)
