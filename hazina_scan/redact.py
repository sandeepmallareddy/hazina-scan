"""Last-resort masking of anything repository-shaped, and the audit that proves it worked.

The real write boundary is `schema.py`: a value that was never declared cannot be emitted at
all, and every declared prose field has already been held to `rejection_reason`'s standard
long before it arrives here. What is left for this module is the case nobody anticipated --
a free-text field some future collector adds, a key that arrived from a parsed manifest, a
mistake in a rule upstream. So the rules below were written from the output requirement
("no path, no symbol, no address, no hash may leave") rather than by echoing the checks in
`schema.py`, and they are deliberately blunter: false positives cost a placeholder, a false
negative costs a leak.

The module has two halves.

*One string at a time.* `scrub` rewrites a string so that what remains is prose; `leaks`
asks the opposite question and names the first thing it finds, or returns None. They share
one ordered table of `Mask`es, which is what keeps them from drifting apart.

*A whole document.* `redact_tree` rebuilds a document with identity keys removed and every
remaining string -- values and map keys alike -- put through `scrub`. `audit_no_leak` then
walks the rebuilt document and raises `LeakDetected` on anything `leaks` still recognises.
The audit is not a content judgement: if it fires, `redact_tree` has a bug, and nothing is
written until that bug is fixed.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from . import schema, vocab

__all__ = [
    "LeakDetected",
    "scrub",
    "leaks",
    "redact_tree",
    "audit_no_leak",
    "MAX_CHARS",
    "Mask",
    "MASKS",
    "DROPPED",
    "NAMES_A_TECHNOLOGY",
    "IS_PROVENANCE",
    "IS_OUR_OWN_WORD",
    "NAMES_AN_ENV_VAR",
    "SCRUB_EXEMPT",
    "AUDIT_EXEMPT",
]


class Mask(NamedTuple):
    """One shape the backstop recognises, and what it does about it.

    `called` is the phrase `leaks` uses to report the shape to a developer; `matches` finds
    it; `becomes` is what `scrub` puts in its place, which is the empty string for shapes
    that carry no information worth keeping a marker for.
    """

    called: str
    matches: re.Pattern[str]
    becomes: str


# Shapes that point at something outside the text: a person, a host, a file. These run first
# because they are the least ambiguous, and because a path swallowed whole by a later rule
# would be reported as the wrong kind of thing.
_LOCATIONS: tuple[Mask, ...] = (
    Mask("an email address", re.compile(r"\b[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}\b"), "[email]"),
    Mask("a URL", re.compile(r"\bhttps?://\S+|\bwww\.\S+\b"), "[url]"),
    Mask("a filesystem path", re.compile(r"\b(?:[\w.\-]+[/\\])+[\w.\-]+\b"), "[path]"),
    # A filename with no directory in front of it still names a file in the tree.
    Mask(
        "a filename",
        re.compile(
            r"\b[\w\-]+\.(?:py|js|jsx|mjs|cjs|ts|tsx|go|java|rb|php|cs|rs|kt|kts|"
            r"swift|scala|c|cc|cpp|h|hpp|m|mm|sh|sql|yml|yaml|toml|json)\b"
        ),
        "[file]",
    ),
)

# Fragments lifted out of a source file verbatim. None of them survives as a placeholder:
# knowing that a note once held a brace teaches a reader nothing, and a marker in its place
# would only clutter the sentence around it.
_VERBATIM: tuple[Mask, ...] = (
    Mask("an object hash", re.compile(r"\b[0-9a-f]{7,40}\b"), "[hash]"),
    # Between the quotes is somebody else's wording, so the quotes take their contents with
    # them. A lone apostrophe after a letter is an English possessive and is left alone.
    Mask("a quoted string", re.compile(r"\"[^\"]{1,200}\"|(?<![A-Za-z])'[^']{1,200}'"), ""),
    # Punctuation that only appears in code. Square brackets are absent on purpose: every
    # placeholder above is spelled in square brackets, and deleting them here would undo the
    # substitutions this pass has just made. A semicolon is absent because English uses one.
    Mask("code punctuation", re.compile(r"[{}]|=>|->|::|`"), ""),
)

# Names a programmer gave to something. Each of these becomes the same placeholder: the
# distinction between a constant and a method matters to `leaks`, which reports it, and not
# at all to a reader of the output.
_SYMBOLS: tuple[Mask, ...] = (
    # Dotted access such as `auth.check` or `os.path.join`. Two segments are enough, but
    # every segment must run to two characters or more. That threshold is the whole reason
    # ordinary punctuation survives: "e.g.", "i.e." and "U.S." are built from single letters
    # and never match, while "auth.check" and "db.models" do.
    Mask("an identifier", re.compile(r"\b[a-zA-Z_]\w+(?:\.[a-zA-Z_]\w+)+\b"), "[identifier]"),
    # Anything joined by underscores, whatever its casing: "db_url", but also a PascalCase
    # or camelCase token wearing a suffix, as in "AcmeAuthProvider_Legacy" or
    # "CompanyName_Prod". Underscores have to be handled here rather than left to the casing
    # rules further down, because `\b` does not fire between a letter and an underscore --
    # both are word characters -- so a suffixed token would otherwise slip past all of them.
    Mask(
        "an identifier", re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"), "[identifier]"
    ),
    Mask("a camelCase identifier", re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"), "[identifier]"),
    # Shouted constants. Two capitals is the bar rather than four or five, because a short
    # initialism is exactly the sort of word a company coins for itself.
    Mask(
        "an uppercase identifier",
        re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z][A-Z0-9]+)\b"),
        "[identifier]",
    ),
)

#: The masks in the order both `scrub` and `leaks` apply them.
MASKS: tuple[Mask, ...] = _LOCATIONS + _VERBATIM + _SYMBOLS

# Capitalisation carries identity that no character class can separate from prose: a class,
# a product, a company, a project. Naming a public technology is allowed and expected, so
# the test is membership of a published list rather than a pattern -- "Next.js" stays,
# "BillingGateway" does not.
_MIXED_CAPS = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b")
_ONE_CAPITALISED_WORD = re.compile(r"\b[A-Z][a-z]{2,}\b")

#: Longest string this module will hand on. A field that somehow arrives enormous is cut at
#: a word boundary and marked with an ellipsis; the length alone is never grounds to drop it.
MAX_CHARS = 2000

_RUN_OF_SPACES = re.compile(r"\s{2,}")
_REPEATED_PLACEHOLDER = re.compile(r"(\[\w+\])(\s+\1)+")


def _is_public_technology(name: str) -> bool:
    return name in vocab.TECH_NAMES


def _mask_capitalised(text: str) -> str:
    """Replace capitalised words that are not public technology names.

    A run of capitals inside a sentence is suspect on its own. A single capitalised word is
    only suspect when it is the *entire* string, because otherwise every sentence that opens
    with "Then" or "Because" would be reduced to a placeholder.
    """
    text = _MIXED_CAPS.sub(
        lambda m: m.group(0) if _is_public_technology(m.group(0)) else "[identifier]", text
    )
    if _ONE_CAPITALISED_WORD.fullmatch(text) and not _is_public_technology(text):
        return "[identifier]"
    return text


def _tidy(text: str) -> str:
    """Close the gaps the masks leave behind.

    Deleting a quoted string or a brace leaves double spaces, and two neighbouring paths
    collapse into "[path] [path]", which says nothing the first one did not.
    """
    text = _RUN_OF_SPACES.sub(" ", text).strip()
    return _REPEATED_PLACEHOLDER.sub(r"\1", text)


def _shorten(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def scrub(text: str) -> str:
    """Return `text` with everything repository-shaped replaced, and the prose intact."""
    if not text:
        return ""
    out = " ".join(str(text).split())
    for mask in MASKS:
        out = mask.matches.sub(mask.becomes, out)
    return _shorten(_tidy(_mask_capitalised(out)))


def leaks(text: str) -> str | None:
    """Name the first repository-shaped thing in `text`, or return None if there is none.

    This is asked of strings `scrub` has already been over, so an answer other than None is
    a report about this module, not about the string: some rule here did not do what the
    matching rule in `scrub` was supposed to have done.
    """
    if not text:
        return None
    for mask in MASKS:
        if mask.matches.search(text):
            return mask.called
    if any(not _is_public_technology(word) for word in _MIXED_CAPS.findall(text)):
        return "a class name"
    if _ONE_CAPITALISED_WORD.fullmatch(text) and not _is_public_technology(text):
        return "a proper name"
    return None


# --- which keys the two walkers treat specially ------------------------------------------

#: Keys removed rather than masked. Their values are nothing *but* identity, so a
#: placeholder would misrepresent them: the truthful record is that the field is absent.
#: Most are never filled by any collector in this release. The set is here so that adding a
#: collector later cannot quietly reintroduce one.
DROPPED = {
    "repo_path",
    "repo_name",  # where the checkout sits, and what it is called
    "head_sha",
    "effective_tip_sha",  # git object hashes
    "anonymizer_commit",
    "top_authors",  # who committed; never populated
    "secret_hit_details",  # no credential scanner exists to fill this
    "commit_sha",  # a git object hash, reached through ext_signals
    "latest_tag",  # a ref name; never populated
}

#: Keys whose strings name public technology. Saying a repository is written in Go is a fact
#: about Go, so these are handed on exactly as the collectors found them.
NAMES_A_TECHNOLOGY = {
    "primary_language",
    "secondary_languages",
    "detected_frameworks",
    "test_framework",
    "package_managers",
    "ci_systems",
    "project_type",
    "primary_class",
}

#: Keys carrying the record's own provenance: the content digest, the handle derived from
#: it, this tool's version, and the repository's name -- which is emitted deliberately, so
#: that a measurement can be matched back to what it measured.
IS_PROVENANCE = {"repo_digest", "fake_repo_name", "measurer_version", "real_repo_name"}

#: Keys whose values this tool chose from its own fixed list of words. Nothing in them came
#: out of the repository.
IS_OUR_OWN_WORD = {"demo_reasoning", "probe", "skip_reason"}

#: Keys that would hold environment-variable NAMES. No live path fills them, and a variable
#: name routinely names a vendor, so a value arriving here is masked whole.
NAMES_AN_ENV_VAR = {
    "env_vars_referenced_in_source",
    "env_vars_in_example",
    "env_vars_missing_from_example",
}

#: Fields `schema.py` has already matched against a closed list of words. Scrubbing them
#: cannot add safety and does destroy them -- the masks above read "Next.js" as a filename
#: and "GitHub Actions" as a class name -- so both walkers step over them.
SCRUB_EXEMPT = NAMES_A_TECHNOLOGY | IS_OUR_OWN_WORD | schema.CLOSED_VOCABULARY_KEYS
AUDIT_EXEMPT = SCRUB_EXEMPT | IS_PROVENANCE


class LeakDetected(schema.EmissionRefused):
    """A path, symbol, address or hash reached the audit alive, so nothing is written.

    It extends `EmissionRefused` because it is that same refusal one layer lower down: the
    schema turns away what it was never told to expect, and this turns away what the schema
    admitted and the scrub then failed to clean.
    """


def _field_under(key, enclosing: str | None) -> str | None:
    """Decide which declared field a child value belongs to.

    Both walkers need this and they must agree. A child is judged under its own key only
    when that key is a field this tool declared. When it is not -- as with the arbitrary
    tool names under `linters_and_formatters` -- the child stays under the enclosing field,
    so that a value already vouched for by a closed vocabulary is not re-examined merely
    because the key above it was unfamiliar.
    """
    return key if key in schema.DECLARED_FIELDS else enclosing


def redact_tree(node, under: str | None = None):
    """Return a copy of `node` with identity keys gone and every remaining string masked.

    Map keys are masked as well as map values: a key read out of a manifest is repository
    text just as much as the value beside it.
    """
    if isinstance(node, dict):
        kept = {}
        for key, value in node.items():
            if key in DROPPED:
                continue
            safe_key = key if key in schema.DECLARED_KEYS else scrub(str(key))
            kept[safe_key] = redact_tree(value, _field_under(key, under))
        return kept
    if isinstance(node, list):
        return [redact_tree(item, under) for item in node]
    if isinstance(node, str):
        if under in NAMES_AN_ENV_VAR:
            return "[identifier]"
        if under in SCRUB_EXEMPT:
            return node
        return scrub(node)
    return node


def audit_no_leak(node, where: str = "", under: str | None = None) -> None:
    """Raise `LeakDetected` if anything in the finished document still reads as source.

    `where` accumulates a dotted path so a failure names the offending field rather than
    dumping the document.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{where}.{key}" if where else str(key)
            if key not in schema.DECLARED_KEYS:
                found = leaks(str(key))
                if found:
                    raise LeakDetected(
                        f"refusing to write: the KEY '{here}' contains {found}. An undeclared "
                        f"key is supposed to be masked by redact_tree(); this one was not, "
                        f"which is a fault in this module and not in the content."
                    )
            audit_no_leak(value, here, _field_under(key, under))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            audit_no_leak(item, f"{where}[{index}]", under)
    elif isinstance(node, str):
        if under in AUDIT_EXEMPT:
            return
        found = leaks(node)
        if found:
            raise LeakDetected(
                f"refusing to write: field '{where}' contains {found} -- the value was "
                f"{node[:90]!r}. redact_tree() was supposed to have removed it, so this is a "
                f"fault in this module and not in the content."
            )
