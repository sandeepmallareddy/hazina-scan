"""Measure where a codebase's structural complexity concentrates and how big it is.

Three figures come out of this module. Only the first is the actual reason it exists; the
other two exist to put the first one in context.

*Concentration.* `decisions_gini_top1pct` asks what fraction of every branch, loop and
guard clause in the tree lives inside its busiest one percent of functions. It is the one
number here that stays comparable regardless of codebase size: a small library and a huge
service can land on the same figure, because it measures whether complexity is piled into a
handful of modules or spread thinly across many, and that distinction disappears if you
only count lines or files. Doing this at all requires knowing where each function actually
starts and ends, which is exactly why this module depends on a real parser instead of a
regular expression over the text.

*Self-reported failure handling.* `error_handling_per_kloc` counts how often
try/catch/Result-style constructs appear per thousand lines of production code. Its honest
limitation: this is a lexical count, so it fires just as readily inside a comment or a
string as inside real code, and it has no way to see error handling a language expresses
through its type system rather than its keywords -- Rust's `?` operator, Haskell's
ExceptT, an OCaml `option` return all fall outside what a keyword scan can see, and no
amount of tuning the keyword list changes that.

*Raw size.* `prod_loc` and `source_files` do not score anything on their own; they exist
purely so the two figures above can be read relative to how much code there actually is.

Three rules apply throughout the module:

*An unmeasured repository never scores as zero.* If the parser package is not installed,
the block reports that and stops there -- a missing grammar is a fact about this machine,
not about the repository being scanned, and scoring it as zero would misrepresent someone
else's code. The same holds within an otherwise successful scan: a tree where no function
actually got parsed comes back with a null concentration figure and an explanatory note,
never a bare zero.

*Every limit hit is reported, never silently absorbed.* A walk that hit its cap, a file
that was skipped, a grammar that failed to load -- each is counted and surfaced through
`bounds`, because a scan that looks complete while quietly having skipped things is worse
than one that says plainly what it did not cover.

*Only counts leave this module, nothing else about the tree.* No path, no filename, no
snippet of source code -- `bounds` and `note` are strictly numbers and generic prose.

Language coverage has two layers because of the "never zero" rule above. Files in languages
the bundled parser grammars support are parsed and counted toward the concentration figure.
Files in real languages the grammar pack does not cover are still counted, just never
parsed, and reported under a separate `source_files_unparsed` total so a reader can tell how
much of the tree the concentration figure was actually derived from. A language this module
has never heard of at all does not drag a repository's score down -- it just makes that
slice of the repository look like it has no code, which is a gap in this module's coverage
rather than a verdict on the repository.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hazina_scan import vocab

#: How many source files one scan will look at. A ceiling rather than a sample: the walk
#: stops here and says so, because a monorepo is allowed to be enormous and a scan is not
#: allowed to take forever over it.
MAX_FILES = 2500

#: The per-file byte ceiling. Above this a file is a data blob, a vendored bundle or a
#: checked-in artefact far more often than it is something a person wrote, and parsing it
#: costs more than the one data point it would add.
MAX_BYTES = 1_000_000


# --- extensions shared by more than one language still in active use -------------------------
#
# Getting the wrong language here still counts the file's lines, just against the wrong
# total, so each of these functions actually inspects the file's bytes rather than trusting
# the extension alone. When the content is inconclusive, the fallback picks whichever of the
# two languages turns up more often in practice.


def resolve_m(data: bytes) -> str:
    """Decide whether a `.m` file is Objective-C or MATLAB.

    Objective-C announces itself with preprocessor directives and `@` declarations, none of
    which MATLAB has any use for, so finding one settles the question.
    """
    if re.search(rb"^[ \t]*(#import|#include|@interface|@implementation|@end)", data, re.M):
        return "objc"
    return "matlab"


def resolve_v(data: bytes) -> str:
    """Decide whether a `.v` file is Verilog, V or Coq.

    Only Verilog closes a module with `endmodule`, so its presence is conclusive. Where it
    is absent the file is read as V, and a Coq source read that way simply yields few
    functions -- the attribution suffers, while the file and its lines are still counted.
    """
    if re.search(rb"\bendmodule\b", data):
        return "verilog"
    return "v"


def resolve_pl(data: bytes) -> str:
    """Decide whether a `.pl` file is Perl or Prolog.

    Prolog is written as clauses of the form `head :- body.`, which Perl never produces.
    """
    if re.search(rb"^[ \t]*[a-z][A-Za-z0-9_]*[ \t]*(\(.*\))?[ \t]*:-", data, re.M):
        return "prolog"
    return "perl"


#: Extension -> the resolver that reads the file to decide which language it is.
AMBIGUOUS_EXT = {".m": resolve_m, ".v": resolve_v, ".pl": resolve_pl}


def notebook_source(data: bytes) -> bytes:
    """The code cells of a notebook, concatenated, as if they were one source file.

    On disk a notebook is a JSON document, but nobody who works in one thinks of it that
    way. Counting the JSON verbatim would pad the line count with stored output and base64
    images, while ignoring the extension entirely would describe a project of twenty
    notebooks as containing no code whatsoever.
    """
    try:
        nb = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return b""
    cells = nb.get("cells") if isinstance(nb, dict) else None
    out: list[str] = []
    for cell in cells or []:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        src = cell.get("source")
        if isinstance(src, list):
            out.append("".join(str(x) for x in src))
        elif isinstance(src, str):
            out.append(src)
    return ("\n".join(out)).encode("utf-8", "replace")


def gini_top(values: list[int], frac: float = 0.01) -> float:
    """Return what fraction of the total decision count sits in the top `frac` of `values`."""
    if not values:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    k = max(1, round(len(values) * frac))
    return sum(sorted(values, reverse=True)[:k]) / total


def decisions_per_function(tree, data_len: int, lang: str = "") -> list[int]:
    """Return one decision-node count per function found while walking `tree`.

    The walk uses an explicit stack rather than call recursion, since a large or
    machine-generated source file can nest past what the interpreter's own call stack would
    tolerate otherwise.

    Each node's grammar type decides how it is treated, with one adjustment: when `heads` is
    set for this language (Elixir, where `def` and `if` both parse as an ordinary call), the
    leading word of a call node's first child is checked against the configured keyword lists
    and can relabel the node as a function definition or an if-statement. A node with no
    children is never counted as a function, because several grammars reuse the definition
    keyword as a bare named node -- Python's `def`, PHP's and Lua's `function`, Ada's
    `procedure` -- even though in Lean, Haskell or Odin that identical spelling already is the
    whole definition; requiring at least one child separates the two cases without hard-coding
    a list of languages.

    Entering a function opens a new counter keyed by its byte span and, if another function
    was already open, marks that outer one as a wrapper. Every decision node instead adds one
    to whichever function currently encloses it. A function is dropped from the result only
    when it is marked as a wrapper and its own count came to zero; a function that legitimately
    has no decisions and contains no nested function is kept, because that count is real.
    """
    counts: dict[int, int] = {}
    wraps: set[int] = set()  # keys of functions that contain another function
    excluded = vocab.STRUCTURE_FUNCTION_NODES_EXCLUDED.get(lang)
    func = vocab.STRUCTURE_FUNCTION_NODES - excluded if excluded else vocab.STRUCTURE_FUNCTION_NODES
    heads = vocab.STRUCTURE_CALL_HEADS.get(lang)
    stack = [(tree.root_node, None)]
    while stack:
        node, fn = stack.pop()
        kind = node.type
        if heads and kind == "call" and node.child_count:
            # Elixir spells `def` and `if` as ordinary calls; the head word is the only
            # thing in the tree that says which construct this is.
            first = node.children[0]
            if not first.child_count:
                word = (first.text or b"").decode("utf-8", "replace")
                if word in heads[0]:
                    kind = "function_definition"
                elif word in heads[1]:
                    kind = "if_statement"
        if kind in func and node.child_count:
            if fn is not None:
                wraps.add(fn)
            # A key that is unique per span: the two byte offsets packed into one integer.
            fn = node.start_byte * data_len.bit_length() + node.end_byte
            counts.setdefault(fn, 0)
        elif fn is not None and kind in vocab.STRUCTURE_DECISION_NODES:
            counts[fn] = counts.get(fn, 0) + 1
        for child in node.children:
            stack.append((child, fn))
    return [n for key, n in counts.items() if n or key not in wraps]


def collect(repo: Path) -> dict:
    """Measure one checkout. Never raises: a tree that cannot be walked is reported."""
    out: dict = {"probe": "code_structure", "ok": False}
    try:
        from tree_sitter_language_pack import get_parser
    except Exception as e:
        out["error"] = (
            f"parser unavailable ({type(e).__name__}) -- install "
            f"tree-sitter-language-pack; structure criteria reported unscored"
        )
        return out

    prod_loc = err_hits = n_files = n_parsed = n_parse_failed = n_unparsed = 0
    test_files = 0
    skipped_big = skipped_generated = 0
    capped = False
    per_func: list[int] = []
    parsers: dict[str, object] = {}
    # When the pack has no grammar for a language, that is noted once against the language
    # rather than rediscovered for every file. `get_parser` raises for a language it does
    # not carry, and without this set a tree full of such files would pay for the same
    # failure over and over.
    parser_unavailable: set[str] = set()

    try:
        for path in repo.rglob("*"):
            if n_files >= MAX_FILES:
                capped = True
                break
            if not path.is_file() or path.is_symlink():
                continue
            # `as_posix()` rather than `str()` here: on Windows, `rglob` returns paths with
            # backslash separators, but every skip pattern below is written against `/`.
            # Using the native separator on that platform would make the patterns never
            # match at all, with no error raised -- a vendored directory or a test file
            # would silently get counted as production code, only on Windows.
            rel = path.relative_to(repo).as_posix()
            if vocab.STRUCTURE_SKIP_DIR.search(rel) or vocab.STRUCTURE_SKIP_FILE.search(rel):
                continue
            ext = path.suffix.lower()
            lang = vocab.STRUCTURE_EXT_LANG.get(ext)
            unparsed_tier = lang is None and ext in vocab.STRUCTURE_EXT_UNPARSED
            if lang is None and not unparsed_tier:
                continue
            # A test file adds to its own tally and takes no part in anything else. That
            # tally matters because the question "are there any tests here at all?" has to
            # have an answer that does not depend on a build succeeding -- a build can fail
            # for reasons of its own, and a measurement resting on it would then declare a
            # well-tested repository untested.
            if vocab.STRUCTURE_TEST_PATH.search(rel):
                test_files += 1
                continue
            try:
                if path.stat().st_size > MAX_BYTES:
                    skipped_big += 1
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if ext == ".ipynb":
                data = notebook_source(data)
                if not data.strip():
                    continue  # nothing but prose cells, so there is no code here
            elif vocab.STRUCTURE_GENERATED_HEADER.search(data[:4096]):
                skipped_generated += 1
                continue

            n_files += 1
            prod_loc += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
            err_hits += len(vocab.STRUCTURE_ERROR_KEYWORDS.findall(data)) + len(
                vocab.STRUCTURE_ERROR_TYPES.findall(data)
            )
            if unparsed_tier:
                n_unparsed += 1
                continue  # its lines count, but nothing can parse it

            if ext in AMBIGUOUS_EXT:
                lang = AMBIGUOUS_EXT[ext](data)
            if lang in parser_unavailable:
                n_parse_failed += 1
                continue
            try:
                if lang not in parsers:
                    parsers[lang] = get_parser(lang)
            except Exception:
                parser_unavailable.add(lang)  # missing on this machine, not in this file
                n_parse_failed += 1
                continue
            try:
                tree = parsers[lang].parse(data)
                per_func.extend(decisions_per_function(tree, len(data), lang))
                n_parsed += 1
            except Exception:
                n_parse_failed += 1
    except OSError as e:
        out["error"] = f"could not walk tree: {type(e).__name__}"
        return out

    kloc = max(prod_loc / 1000.0, 0.001)
    out.update(
        {
            "prod_loc": prod_loc,
            "source_files": n_files,
            "test_files": test_files,
            "n_functions_seen": len(per_func),
            "n_files_parsed": n_parsed,
            "n_files_parse_failed": n_parse_failed,
            # These contribute their lines but were never offered to a parser. Since
            # `source_files` counts them too, the two figures together show how much of the tree
            # the concentration number actually rests on -- a modest share measured across a
            # largely unparsed tree is weak evidence rather than a finding of even complexity.
            "source_files_unparsed": n_unparsed,
            "decisions_gini_top1pct": round(gini_top(per_func), 4) if per_func else None,
            "error_handling_per_kloc": round(err_hits / kloc, 3) if n_files else None,
            "ok": True,
        }
    )

    notes = []
    if capped:
        notes.append(f"file cap {MAX_FILES} reached; covers the first {n_files} source files")
    if skipped_big:
        notes.append(f"{skipped_big} files over {MAX_BYTES // 1000}KB skipped")
    if skipped_generated:
        notes.append(f"{skipped_generated} generated files skipped")
    if n_parse_failed:
        notes.append(f"{n_parse_failed} files failed to parse")
    if notes:
        out["bounds"] = "; ".join(notes)
    if not per_func:
        out["note"] = "no functions parsed; concentration criterion unscored rather than zero"
    out["structure_probe_mode"] = "deterministic"
    return out
