"""Which company a repository belongs to, read out of the code.

A measurement of somebody else's repository is a measurement *about* somebody, and until
this block existed nothing in the output said who. That question cannot be left to whoever
reads the report: a repository called `payments-core` belongs to whichever company wrote
it, and the numbers beside it do not distinguish the company from a fork of the company.

So this reads the evidence and a human confirms it. The confirmation is the load-bearing
half. Everything here is evidence offered to somebody who can overrule it, which is why
`none` is a first-class answer rather than a failure: a tree that says nothing gets a
blank, not a guess.

THE ONE BLOCK THAT NAMES A THIRD PARTY
--------------------------------------
Everywhere else this tool refuses identity outright -- no author names, no addresses, no
hostnames, no paths. Here a name leaves, and it leaves because the thing being described
IS a company and naming it is the entire point of the flow. That exception is to a class
of name, not to a field: `schema.py` still declares the block field by field and still
refuses to write anything undeclared, and `is_emittable_name` below is the gate that keeps
anything else from riding out wearing a company's shape.

FOUR FAMILIES, AND THE TWO THAT ARE REFUSED
-------------------------------------------
Four kinds of evidence are gathered, each under a family name that travels with the answer:

* `licence_file` -- whoever the root licence, notice or copying file says holds copyright.
* `copyright_header` -- the holder named in source-file headers, counted only when the same
  one turns up repeatedly.
* `git_remote` -- the organisation part of the `origin` URL.
* `package_manifest` -- a namespace registered in a manifest at the root.

Namespaces are all the manifest family will take: a scope in npm, a vendor in Composer, the
organisation segment of a Go module path or a Maven groupId, a marketplace publisher. The
`author`, `authors`, `maintainers` and `contact` entries are skipped, since each holds a
person, and this tool does not emit a person's name whatever file it was found in -- a name
in a manifest is no less personal than one in a commit. The consequence is that ecosystems
with a flat namespace, Cargo and PyPI among them, supply nothing at all here, because the
only name their manifests carry is the author's.

Two further signals are deliberately not collected, and both refusals are declared in
`schema.py` so they show up in the review a repository owner reads before sending
anything. Author email DOMAINS are derived from addresses, which the identity rule forbids
by name. Config and CI hostnames, registry paths and cloud project names collide with the
no-paths rule as well, and are the weakest evidence of the six -- the widest widening for
the least return.

NO NUMBER LEAVES THIS MODULE
----------------------------
The copyright family counts how many files a header appears in, and the industry guess
counts keyword hits. Both counts stop at a return statement. A per-name hit count is a
confidence number wearing different clothes, and a percentage printed beside a company's
name reads as a grade. What leaves is the outcome word and the family names -- provenance,
"read out of your licence file" -- which is the only form the confirm step wants.

EVERY READER IS BOUNDED AND NONE OF THEM RAISE
----------------------------------------------
A repository is untrusted input: as large as somebody else decided, and possibly hostile.
Every read here is capped in bytes, the file walk is capped in files, and each family
returns nothing rather than failing. Losing the whole measurement to somebody's malformed
`pom.xml` would be a bad trade for a question a human can answer by typing.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from pathlib import Path

from . import vocab

#: Kept short deliberately. These become the options a person is asked to choose between,
#: and past three the question gets harder to answer rather than better informed.
MAX_CANDIDATES = 3

#: A total ceiling on top of the name pattern's own bounds. Eight words of forty characters
#: is 320, and nothing that long is a company name; it is a sentence out of a licence file.
MAX_NAME_CHARS = 120

_MAX_FILES_SCANNED = 600
_MAX_HEADER_BYTES = 4096
#: Generous compared with a header read, because the whole licence text comes first and a
#: project's own notice is commonly appended below it; the Apache-2.0 appendix alone sits
#: some ten kilobytes down. A ceiling remains, since the file was written by a stranger.
_MAX_LICENCE_BYTES = 65_536
_MAX_MANIFEST_BYTES = 262_144
_MAX_README_BYTES = 40_960

#: A copyright entity has to REPEAT before it counts. One file's header is as likely to be
#: a single copied-in file's author as it is to be this repository's owner, and "strong
#: because it repeats" is the only reason this family is worth reading at all.
_MIN_COPYRIGHT_FILES = 2

#: Two keyword hits, not one. One industry word in a README is a coincidence.
_MIN_INDUSTRY_HITS = 2

_NAME_RE = re.compile(vocab.COMPANY_NAME_PATTERN)
_LETTER_RE = re.compile(rf"[{vocab.COMPANY_NAME_LETTERS}]")
#: What `normalise` tears a name apart on. Everything that is not a letter or a digit, with
#: the letter class running past ASCII so an accented legal entity keys to itself.
_NOT_ALNUM_RE = re.compile(r"[^0-9A-Za-zÀ-ÿ]+")

#: Where a name stops and the sentence around it begins. `*/` and `-->` earn their place
#: beside the sentence punctuation: a C, Java, JS or HTML header closes its comment on the
#: same line, and "Acme Systems */" is refused by the name pattern outright -- so leaving
#: the terminator in loses the signal entirely rather than trimming it.
_NAME_ENDS_RE = re.compile(r"\s*\*/|\s*-->|\s*[;:<(\[]|\s+-\s+|\s{2,}")
#: A full stop ends a name only when prose follows it. `Inc.` and `Ltd.` end in one, so
#: splitting on the dot itself would turn every incorporated company into an
#: unincorporated one.
_SENTENCE_END_RE = re.compile(r"\.\s+\S")

_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)
_GROUP_ID_RE = re.compile(r"<groupId>\s*([^<\s]{1,200})\s*</groupId>")
_ORGANISATION_RE = re.compile(r"<organization\b[^>]*>(.*?)</organization>", re.DOTALL)
_ORG_NAME_RE = re.compile(r"<name>\s*([^<]{1,200}?)\s*</name>")
_REQUIREMENT_RE = re.compile(r"[<>=!~\[; ]")
_GO_REQUIRE_RE = re.compile(r"^\s*(?:require\s+)?([\w.\-/]+)\s+v[\w.\-+]+", re.MULTILINE)
_GEM_RE = re.compile(r"^\s*gem\s+['\"]([^'\"]+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def is_emittable_name(text: object) -> bool:
    """Whether `text` is a company name this tool may say out loud.

    The single gate. `collect` calls it on every candidate before returning one, and
    `schema.COMPANY_NAME` applies the same pattern again at the write boundary as the
    backstop. Anything refused here is DROPPED, never repaired: a half-cleaned name is a
    company that does not exist.

    What the pattern refuses is what must not ride out wearing a name -- a filesystem path
    (`/`, `\\`), an address or handle (`@`), a URL or host:port (`:`), a snake_case
    identifier (`_`) -- and the rule doing most of the work is that a dot may END a word
    but may never JOIN two characters, which refuses `acme.com`, `com.acme.payments` and
    `payments_core.py` in one stroke. `U.S. Steel` is the honest casualty: a name we cannot
    spell is a name the human types in the confirm step instead.
    """
    return (
        isinstance(text, str)
        and 0 < len(text) <= MAX_NAME_CHARS
        and _NAME_RE.fullmatch(text) is not None
    )


def normalise(name: str) -> str:
    """Reduce a name to the key that decides whether two spellings mean one company.

    Case is folded away, a trailing legal suffix is dropped, and everything that is not a
    letter or a digit goes. "Acme Systems, Inc." and `acme-systems` therefore both arrive at
    `acmesystems`, which is what allows a licence file and a git remote to register as one
    company confirmed twice instead of two companies found once each. An empty result means
    the name carried nothing to key on.
    """
    words = [w for w in _NOT_ALNUM_RE.split(name.casefold()) if w]
    while words and words[-1] in vocab.COMPANY_LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


# ---------------------------------------------------------------------------
# Bounded reading
# ---------------------------------------------------------------------------


def _read(path: Path, max_bytes: int) -> str:
    """A capped read that never raises. Empty string for anything unreadable."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _sorted_walk(repo: Path):
    """`os.walk`-shaped, sorted at every level, breadth-first, silent on an unreadable
    directory.

    The caller prunes by assigning into the yielded `subdirs`, which works because this
    reads that list again after the yield returns -- the same contract `os.walk` offers.
    """
    queue = [repo]
    while queue:
        directory = queue.pop(0)
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        subdirs = [e.name for e in entries if e.is_dir() and not e.is_symlink()]
        files = [e.name for e in entries if e.is_file()]
        yield directory, subdirs, files
        queue.extend(directory / name for name in subdirs)


def _source_files(repo: Path) -> list[Path]:
    """Gather source files to read headers from, at most `_MAX_FILES_SCANNED` of them.

    The walk is sorted at every level, so one tree always produces one sample and therefore
    one answer. Capping an unsorted walk would leave the result depending on the order the
    filesystem happened to hand back, and determinism is a property the rest of this package
    claims without qualification.

    What the cap counts is files kept, not directories entered. Reaching it returns at once,
    possibly part-way through a directory, and nothing beyond that point is examined. Where
    the walk stops is part of the measurement rather than an implementation detail: the same
    tree must always be cut in the same place, so the stopping rule is fixed here and left
    alone.
    """
    found: list[Path] = []
    for directory, subdirs, filenames in _sorted_walk(repo):
        subdirs[:] = [
            d for d in subdirs if d not in vocab.IDENTITY_SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if Path(filename).suffix.lower() in vocab.IDENTITY_SOURCE_SUFFIXES:
                found.append(directory / filename)
                if len(found) >= _MAX_FILES_SCANNED:
                    return found
    return found


# ---------------------------------------------------------------------------
# The four families
# ---------------------------------------------------------------------------


def _entity_from_copyright_line(text: str) -> str | None:
    """Pull the named holder out of a single copyright line, or return None.

    A line reading `Copyright (c) 2024 Acme Systems, Inc.` yields `Acme Systems, Inc.`.
    """
    match = vocab.COPYRIGHT_NOTICE_RE.search(text)
    if not match or not vocab.YEAR_OR_MARKER_RE.search(match.group("marker")):
        return None
    entity = vocab.RIGHTS_NOISE_RE.sub("", match.group("entity"))
    entity = _NAME_ENDS_RE.split(entity, maxsplit=1)[0]
    sentence_end = _SENTENCE_END_RE.search(entity)
    if sentence_end:
        entity = entity[: sentence_end.start() + 1]
    entity = entity.strip().strip(",").strip()
    if not entity:
        return None
    # "Copyright (c) 2024" with nothing after it leaves the year behind once the optional
    # year run has nothing to hand the rest of the line to. A company called 2024 is
    # precisely the bad guess this must never make, so an entity has to contain a letter.
    if not _LETTER_RE.search(entity):
        return None
    if normalise(entity) in vocab.NOT_A_COMPANY:
        return None
    return entity


def from_remote(repo_full_name: str | None) -> list[str]:
    """Take the organisation from an already-parsed `owner/name`, if there is one.

    The name arrives as an argument rather than being worked out here, and deliberately so.
    Turning a remote URL into a name is the step that has to refuse `file://` remotes and
    plain directories, or an operator's home directory would be reported as the owning
    organisation. That step is worth having in exactly one place; a duplicate would be a
    second copy to keep correct.

    Where the name has only one segment the URL named no owner, so nobody is identified. A
    nested group such as `group/subgroup/name` belongs to the outermost segment.
    """
    if not repo_full_name:
        return []
    parts = [p for p in repo_full_name.split("/") if p]
    if len(parts) < 2:
        return []
    # A slug is how a company spells itself to a git host; `normalise` is what makes
    # `acme-systems` agree with the licence file's "Acme Systems, Inc.".
    return [parts[0]]


def from_licence(repo: Path) -> list[str]:
    """Read the copyright holder from a licence or notice file at the top of the tree.

    Only the top. A licence sitting further down belongs to whatever code surrounds it, and
    nothing available here distinguishes a vendored dependency's licence from the project's
    own.
    """
    names: list[str] = []
    try:
        entries = sorted(repo.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name.upper().startswith(vocab.LICENCE_FILE_PREFIXES) and entry.is_file():
            entity = _entity_from_copyright_line(_read(entry, _MAX_LICENCE_BYTES))
            if entity:
                names.append(entity)
    return names


def from_copyright(repo: Path) -> list[str]:
    """Find the holder that keeps reappearing across source-file headers.

    Only the commonest one qualifies, and only once it has been seen in `_MIN_COPYRIGHT_FILES`
    separate files. Where several are level, all of them come back; downstream that reads as
    the evidence disagreeing with itself, which is true, rather than as a winner chosen by
    nothing in particular.

    Tallies never leave this function. They are how the choice gets made, not something the
    caller has any claim on.
    """
    counts: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    for path in _source_files(repo):
        entity = _entity_from_copyright_line(_read(path, _MAX_HEADER_BYTES))
        if not entity:
            continue
        key = normalise(entity)
        if not key:
            continue
        counts[key] += 1
        spelling.setdefault(key, entity)
    if not counts:
        return []
    top = max(counts.values())
    if top < _MIN_COPYRIGHT_FILES:
        return []
    return [spelling[key] for key, hits in sorted(counts.items()) if hits == top]


def from_manifests(repo: Path) -> list[str]:
    """Collect namespaces declared in root manifests, and nothing that names a person.

    Each of a scope in npm, a vendor in Composer, the organisation segment of a Go module
    path or of a Maven groupId, and a marketplace publisher is a namespace some company
    registered in its own name. The `author`, `authors` and `maintainers` fields identify a
    human being and are never read.
    """
    names: list[str] = []

    package_json = _json(repo / "package.json")
    if isinstance(package_json, dict):
        name = package_json.get("name")
        if isinstance(name, str) and name.startswith("@") and "/" in name:
            names.append(name[1:].split("/", 1)[0])
        publisher = package_json.get("publisher")
        if isinstance(publisher, str):
            names.append(publisher)

    composer = _json(repo / "composer.json")
    if isinstance(composer, dict):
        name = composer.get("name")
        if isinstance(name, str) and "/" in name:
            names.append(name.split("/", 1)[0])

    module = _MODULE_RE.search(_read(repo / "go.mod", _MAX_MANIFEST_BYTES))
    if module:
        org = _org_from_module_path(module.group(1))
        if org:
            names.append(org)

    # Deliberately matched with capped regular expressions instead of being parsed. This
    # file came from somebody else, an XML parser will expand whatever entities it declares,
    # and wanting two tags out of it is not reason enough to hand an untrusted document to
    # something that can be persuaded to allocate without limit.
    pom = _read(repo / "pom.xml", _MAX_MANIFEST_BYTES)
    if pom:
        names.extend(_from_pom(pom))

    return [n.strip() for n in names if n and n.strip()]


def _from_pom(pom: str) -> list[str]:
    """Extract the organisation a Maven project claims for itself. Two traps lie in wait.

    The first is that the opening `<groupId>` in a conventional POM belongs to `<parent>`,
    which Maven places before the project's own coordinates. Blocks belonging to somebody
    else are therefore cut away before anything is matched, and no parent value is used as a
    fallback -- a project inheriting its groupId names nobody here, which beats naming the
    framework it was built on.

    The second is that `<name>` has to be read from within `<organization>`. A lazy
    `.*?<name>` slips straight out of the element and settles on whichever `<name>` comes
    next, which is almost always the one under `<licenses><license>`, and the answer comes
    back as "MIT License".
    """
    stripped = pom
    for block in vocab.POM_FOREIGN_BLOCKS:
        stripped = re.sub(
            rf"<{block}\b[^>]*>.*?</{block}>", " ", stripped, flags=re.DOTALL | re.IGNORECASE
        )

    names: list[str] = []
    group = _GROUP_ID_RE.search(stripped)
    if group:
        org = _org_from_group_id(group.group(1))
        if org:
            names.append(org)

    # EVERY `<organization>`, not only the first. A string-valued one -- a person's
    # employer, in a block this does not strip -- has no `<name>` child, and stopping at it
    # would drop the project's real organisation silently, which is worse than reading the
    # wrong one because nothing downstream could tell the signal had been lost.
    for element in _ORGANISATION_RE.finditer(stripped):
        organisation = _ORG_NAME_RE.search(element.group(1))
        if organisation:
            names.append(organisation.group(1))
            break
    return names


def _org_from_module_path(path: str) -> str | None:
    """Read the organisation out of a Go module path, or decide there is not one.

    A published path runs host, then organisation, then module, so `example.org/acme/widget`
    gives `acme`. Anything shorter is either a local path or a module published at the host
    itself, and neither says who owns it.
    """
    segments = [s for s in path.strip().strip("/").split("/") if s]
    if len(segments) < 3 or "." not in segments[0]:
        return None
    return segments[1]


def _org_from_group_id(group_id: str) -> str | None:
    """Read the company out of a Maven groupId, or decide there is not one.

    A groupId is a domain written backwards, so the company sits in the second segment:
    `com.acme.widget` gives `acme`, and `com.acme` gives the same. A single segment
    identifies nobody, and when the leading segment is not one of the words domains actually
    begin with, the value is not a reverse domain -- picking a segment from it anyway would
    be manufacturing an answer.
    """
    segments = [s for s in group_id.strip().split(".") if s]
    if len(segments) < 2 or segments[0].lower() not in vocab.GROUP_ID_DOMAIN_WORDS:
        return None
    return segments[1]


def _json(path: Path) -> object:
    text = _read(path, _MAX_MANIFEST_BYTES)
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# Industry
#
# A hint for whoever sorts the catalogue, which has nothing else that supplies it.
# Deterministic keyword matching over the README and the declared dependencies -- no model
# call -- and nothing read out of the repository is emitted: the answer is one of OUR words
# from `vocab.INDUSTRIES`, or nothing at all.
# ---------------------------------------------------------------------------


def _readme_text(repo: Path) -> str:
    """The root README, case-folded, or empty. The first one in sorted order wins."""
    try:
        entries = sorted(repo.iterdir(), key=lambda p: p.name)
    except OSError:
        return ""
    for entry in entries:
        if entry.is_file() and entry.name.upper().split(".")[0] == "README":
            return _read(entry, _MAX_README_BYTES).casefold()
    return ""


def _dependency_names(repo: Path) -> list[str]:
    """List the dependencies the root manifests declare, folded to lower case.

    Names alone, and only the ones written down by hand. Version constraints carry no hint
    of an industry, and a lockfile holds thousands of transitive entries that characterise a
    package manager rather than a business.
    """
    found: list[str] = []

    package_json = _json(repo / "package.json")
    if isinstance(package_json, dict):
        for block in ("dependencies", "devDependencies", "peerDependencies"):
            section = package_json.get(block)
            if isinstance(section, dict):
                found.extend(str(key) for key in section)

    composer = _json(repo / "composer.json")
    if isinstance(composer, dict):
        for block in ("require", "require-dev"):
            section = composer.get(block)
            if isinstance(section, dict):
                found.extend(str(key) for key in section)

    for line in _read(repo / "requirements.txt", _MAX_MANIFEST_BYTES).splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            found.append(_REQUIREMENT_RE.split(line, maxsplit=1)[0])

    for manifest in ("pyproject.toml", "Cargo.toml"):
        found.extend(_toml_dependency_names(repo / manifest))

    found.extend(_GO_REQUIRE_RE.findall(_read(repo / "go.mod", _MAX_MANIFEST_BYTES)))
    found.extend(_GEM_RE.findall(_read(repo / "Gemfile", _MAX_MANIFEST_BYTES)))

    return [name.casefold() for name in found if name]


def _toml_dependency_names(path: Path) -> list[str]:
    """Read dependency names from a `pyproject.toml` or `Cargo.toml`, or give back none."""
    text = _read(path, _MAX_MANIFEST_BYTES)
    if not text:
        return []
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError):
        return []
    found: list[str] = []
    project = parsed.get("project")
    if isinstance(project, dict):
        declared = project.get("dependencies")
        if isinstance(declared, list):
            found.extend(_REQUIREMENT_RE.split(str(item), maxsplit=1)[0] for item in declared)
    for section in (
        _table(_table(_table(parsed, "tool"), "poetry"), "dependencies"),
        _table(parsed, "dependencies"),
        _table(parsed, "dev-dependencies"),
    ):
        found.extend(str(key) for key in section)
    return [name for name in found if name]


def _table(parsed: object, key: str) -> dict:
    """Step one level into a TOML table, handing back an empty table if that is impossible.

    The type is tested at each step instead of being taken on trust. Writing `tool = "x"` is
    perfectly valid TOML, and a chain such as `.get("tool", {}).get("poetry", {})` then
    raises on a string -- losing an entire measurement to a stranger's odd manifest, which is
    the failure this module is built to avoid.
    """
    if not isinstance(parsed, dict):
        return {}
    value = parsed.get(key)
    return value if isinstance(value, dict) else {}


def industry_of(repo: Path) -> tuple[str | None, list[str]]:
    """Guess an industry, and say which of the two sources of words pointed at it.

    The answer is `(None, [])` when the evidence falls short of `_MIN_INDUSTRY_HITS`, and
    also when two industries finish level. A draw really is no answer, and a blank field
    somebody can fill in serves a catalogue better than whichever of two words sorted first.
    """
    readme = _readme_text(repo)
    dependencies = _dependency_names(repo)

    per_source: dict[str, Counter[str]] = {"readme": Counter(), "dependencies": Counter()}
    for industry, words in vocab.INDUSTRY_README_WORDS.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", readme):
                per_source["readme"][industry] += 1
    for industry, words in vocab.INDUSTRY_DEPENDENCY_WORDS.items():
        for word in words:
            if any(word in dependency for dependency in dependencies):
                per_source["dependencies"][industry] += 1

    totals: Counter[str] = Counter()
    for counts in per_source.values():
        totals.update(counts)
    if not totals:
        return None, []
    top = max(totals.values())
    if top < _MIN_INDUSTRY_HITS:
        return None, []
    winners = [industry for industry, hits in totals.items() if hits == top]
    if len(winners) != 1:
        return None, []
    winner = winners[0]
    return winner, [source for source in vocab.INDUSTRY_SIGNALS if per_source[source][winner]]


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


def _gather_by_name(per_family: dict[str, list[str]]) -> dict[str, dict]:
    """Group every name found into one entry per company, keyed by its normalised spelling.

    Each entry records which families produced the name and how each of them wrote it, so a
    later step can report agreement and still show a readable spelling. Two kinds of name
    are dropped on the way in, and neither is repaired: one this tool has no safe way to
    write down, and one that turns out to name nobody once normalised.
    """
    gathered: dict[str, dict] = {}
    for family in vocab.SIGNAL_FAMILIES:
        for name in per_family[family]:
            if not is_emittable_name(name):
                continue
            key = normalise(name)
            if not key or key in vocab.NOT_A_COMPANY:
                continue
            entry = gathered.setdefault(key, {"families": set(), "spellings": {}})
            entry["families"].add(family)
            entry["spellings"].setdefault(family, name)
    return gathered


def _outcome_for(candidates: list[dict]) -> str:
    """Decide how much weight the reader should put on this list of candidates.

    Calling a read `confident` requires the families to agree with each other, and two names
    that are each backed twice are a contradiction rather than a double confirmation. A fork
    produces precisely that pattern: the inherited licence and headers still name the
    original company while the new remote and manifest namespace name the new one. That case
    is a large part of why this block exists, and marking it confident would seed one of two
    conflicting answers into a review step where confident rows tend to be waved through.

    The count runs over the full list, ahead of the cap on how many are displayed, so a
    candidate that never gets shown still costs the read its confidence.
    """
    if not candidates:
        return "none"
    corroborated = sum(1 for entry in candidates if len(entry["signal_families"]) >= 2)
    return "confident" if corroborated == 1 else "uncertain"


def collect(repo: Path, repo_full_name: str | None = None) -> dict:
    """Run all four families over `repo` and build the `company_identity` block.

    The block holds an `outcome` of `confident`, `uncertain` or `none`; a
    `company_candidates` list, each entry pairing a `company_name` with the
    `signal_families` that produced it; an `industry` or None; and the `industry_signals`
    behind that guess.

    Nothing a repository contains can make this raise. A tree that will not read produces an
    outcome of `none`.
    """
    repo = Path(repo)
    per_family = {
        "git_remote": from_remote(repo_full_name),
        "licence_file": from_licence(repo),
        "copyright_header": from_copyright(repo),
        "package_manifest": from_manifests(repo),
    }

    gathered = _gather_by_name(per_family)
    candidates = [
        {
            "company_name": _display(entry["spellings"]),
            "signal_families": [f for f in vocab.SIGNAL_FAMILIES if f in entry["families"]],
        }
        for _key, entry in sorted(gathered.items(), key=lambda item: _rank(item[0], item[1]))
    ]

    industry, industry_signals = industry_of(repo)
    return {
        "outcome": _outcome_for(candidates),
        "company_candidates": candidates[:MAX_CANDIDATES],
        "industry": industry,
        "industry_signals": industry_signals,
    }


def _display(spellings: dict[str, str]) -> str:
    """Choose which family's way of writing an agreed name gets shown to the reader."""
    for family in vocab.COMPANY_DISPLAY_PREFERENCE:
        if family in spellings:
            return spellings[family]
    return next(iter(spellings.values()))


def _rank(key: str, entry: dict) -> tuple:
    """Sort key: the best-corroborated candidate leads, then the one whose strongest family
    ranks highest, and the normalised name breaks any remaining tie so the order never
    wanders between runs."""
    strongest = min(vocab.SIGNAL_FAMILIES.index(f) for f in entry["families"])
    return (-len(entry["families"]), strongest, key)
