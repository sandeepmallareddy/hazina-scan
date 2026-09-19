"""Where the complexity in a repository sits, and how much of it there is.

Three things come out of this module, and only the first of them is the reason it exists.

*How concentrated the decisions are.* `decisions_gini_top1pct` is the share of every branch,
loop and guarded path in the tree that sits in its densest one percent of functions. It is
the one structural figure here that does not move with size: a twenty-thousand-line service
and a two-thousand-line library can score identically, and what separates them -- complexity
gathered into a few real modules against complexity smeared evenly over many shallow files
-- is invisible to every count of lines or files. Attributing a decision to a function needs
real function boundaries, which is the whole reason a parser is a dependency here instead of
a regular expression.

*How much the code says about its own failure modes.* `error_handling_per_kloc` counts
try/catch/Result density in production code. The limit of that measurement is worth stating
plainly rather than hiding: it is lexical, so it fires inside comments and string literals,
and it cannot see failure handling a language expresses in its types rather than its words.
Rust's `?`, Haskell's ExceptT and an OCaml `option` return are all undercounted here and no
keyword list can fix it.

*How much code there is.* `prod_loc` and `source_files` are size controls. Nothing is scored
on them directly; they exist so the two figures above can be read in proportion.

Three rules run through all of it.

*Unmeasured is never zero.* If the parser is not installed, nothing is measured and the
block says so -- a missing grammar is a fact about the operator's machine, not a property of
the repository, and reporting it as a structural score of zero would be a lie about somebody
else's code. The same rule applies inside a successful scan: a tree in which no function was
parsed gets a null concentration figure and a note, not a zero.

*Bounds are reported, never applied silently.* A capped walk, a skipped file, a grammar that
would not load -- each of them is counted and said out loud in `bounds`, because a truncated
scan that looks complete is worse than one that admits what it missed.

*Nothing about the tree leaves except counts.* No path, no filename, no fragment of source.
`bounds` and `note` are numbers and plain words.

Language coverage is in two layers, and the second one exists because of the first rule.
Extensions the pinned parser pack can parse are counted and attributed. Real languages the
pack has no grammar for are counted and never parsed, and reported separately as
`source_files_unparsed` so a reader can see how much of the tree the concentration figure
was actually computed over. A language absent from both tables does not make its repository
score badly -- it makes the repository look empty, which is a gap in our table wearing the
costume of a verdict about somebody's work.
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


# --- extensions that belong to more than one living language --------------------------------
#
# Choosing wrong costs the file its attribution while still counting its lines, so each of
# these reads the file's own bytes. The fallback is whichever reading is commoner in real
# repositories.


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
    """The share of all decision points held by the densest `frac` of functions."""
    if not values:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    k = max(1, round(len(values) * frac))
    return sum(sorted(values, reverse=True)[:k]) / total


def decisions_per_function(tree, data_len: int, lang: str = "") -> list[int]:
    """One decision count per function in a parse tree. Walked iteratively, because a
    generated file nests deeply enough to exhaust a recursive walk's stack.

    Two corrections keep the distribution honest across grammars that spell the same
    construct more than once, and both are structural rather than a list of names.

      * Nothing without children can be a function. Several grammars expose the KEYWORD as a named
        node -- Python's `def`, PHP's and Lua's `function`, Ada's `procedure` -- and those
        names have to stay in the table because in Lean, Haskell and Odin the same spelling
        IS the definition. Requiring children tells the two apart without a per-language
        exception.
      * A wrapper that scores zero only because a nested function took all of its decisions
        is a phantom rather than a function. Counting it would open one empty function per
        real one, which halves every density and moves the top share for a reason that is
        grammar bookkeeping rather than code. A function with genuinely zero decisions and
        no function inside it is real, and is kept: straight-line code is a true data point.
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
            # as_posix, not str: rglob yields backslash separators on Windows and every
            # pattern below anchors on `/`. Matching the native separator would make them
            # silently never fire there -- a vendored tree counted as first-party code and
            # a test file as production, on that platform only, with no error to show.
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
