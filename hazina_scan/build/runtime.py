"""Work out which language runtime a checkout wants, and which installed one to hand it.

A build step that runs the wrong interpreter fails for a reason that has nothing to do with
the repository: an old `.python-version` under a brand-new Python loses a stdlib module, a
`package.json` engines range built for Node 16 trips over an OpenSSL default that changed in
Node 18, and a `pom.xml` compiler level a modern JDK stopped supporting refuses to start. Every
one of those failures belongs to us, the runner, not to the code being measured, and the only
way to keep that attribution honest is to look up what the tree actually asked for before
picking anything to run it with.

This module does two things and stops there. `declarations()` reads the tree's own version
files and manifests and turns them into a version window per language lane -- it opens nothing
else and runs nothing. `resolve()` then looks at what is already installed on this machine,
matches it against those windows, and returns a `Plan`: what to run each lane with, an
environment overlay the caller applies to reach it, and which lanes this host simply cannot
satisfy. Nothing here fetches, builds, or installs a runtime -- a candidate is only ever
something already sitting on disk.

The default installation for a lane is preferred over anything else it could satisfy the
window with, on the theory that swapping in a "better" runtime is itself a way to turn a
working build into a broken one. We move off the default only when it fails the tree's own
window, and when we do move, we aim at the candidate nearest the floor the tree declared,
because a stated minimum describes what the project was actually built and tested against.

A runtime installed under the operator's own home directory is excluded unless the caller
passes `allow_home=True`, because a version manager (nvm, pyenv, rustup, and the rest) puts its
toolchains there, and handing a stranger's install hooks a path under someone's home leaks a
username and a set of personal binaries onto a machine that is not theirs to explore. This
tool always resolves with `allow_home` left at its default of False.

Every probe of an already-installed runtime asks for `--version` (or the local equivalent) and
nothing more, through `hazina_scan.env.run`, bounded by `_PROBE_TIMEOUT` seconds and capped at
`_MAX_PROBES` subprocesses in total so a machine with a long tail of old JDKs cannot turn one
resolution into dozens of child processes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hazina_scan import env as env_mod
from hazina_scan.env import run as _run_probe

# ---------------------------------------------------------------------------
# Fixed vocabulary and lookup tables. These describe published formats
# (semver, PEP 440, Node's own LTS codenames) and this module's own bounds --
# they are pinned data, not something a collector infers from a repository.
# ---------------------------------------------------------------------------

#: The runtime families a declaration can name. Matches `vocab.RUNTIME_LANES`.
LANES: tuple[str, ...] = ("node", "python", "go", "rust", "ruby", "java", "dotnet")

#: Ceiling, in seconds, on any one `--version` probe of an installed runtime.
_PROBE_TIMEOUT = 15

#: Ceiling on how many probe subprocesses one `resolve()` call may spend in total.
_MAX_PROBES = 24

#: `.nvmrc` may hold an LTS codename ("lts/hydrogen", "hydrogen") instead of a number.
_NODE_CODENAMES = {
    "argon": 4,
    "boron": 6,
    "carbon": 8,
    "dubnium": 10,
    "erbium": 12,
    "fermium": 14,
    "gallium": 16,
    "hydrogen": 18,
    "iron": 20,
    "jod": 22,
    "krypton": 24,
}

#: How many leading components of a bare pinned version actually bound compatibility, per
#: lane. Python and Ruby treat the minor release as the boundary; Node and Java treat the
#: major as the boundary. Getting this table wrong turns a satisfiable pin into a false
#: mismatch, or the reverse.
_PIN_PRECISION = {
    "python": 2,
    "ruby": 2,
    "go": 2,
    "rust": 2,
    "node": 1,
    "java": 1,
    "dotnet": 1,
}

#: A spec that contains one of these characters (or a bare `x`/`.x` wildcard) is a range,
#: not a bare pin or floor.
_HAS_OPERATOR = re.compile(r"[><=^~*|]|\bx\b|\.x")

#: Pulls a version number out of an installation path such as `.../node/v18.17.0/bin` when the
#: candidate itself did not have to be executed to know its own version.
_VERSION_IN_PATH = re.compile(r"(?:^|[/\\@-])v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[/\\-]|$)")


# --- reading and comparing versions ---------------------------------------


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Pull the first dotted version number out of `text`, as a tuple of ints.

    Returns None when nothing numeric is present. Deliberately permissive about what comes
    before or after the number, because the strings this reads span `--version` banners
    ("openjdk 21.0.2"), file contents ("v18.17.0"), and manifest values ("go1.22.12") that
    were never meant to be parsed by the same code.
    """
    if not text:
        return None
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text.strip())
    if match is None:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def _norm_java(version: tuple[int, ...]) -> tuple[int, ...]:
    """Collapse Java's old `1.N` numbering onto the modern `N` scheme it renamed to."""
    if version and version[0] == 1 and len(version) > 1:
        return version[1:]
    return version


@dataclass(frozen=True)
class Interval:
    """A version window, inclusive at the bottom and exclusive at the top. Either end may
    be left unset to mean unbounded in that direction."""

    low: tuple[int, ...] | None = None
    high: tuple[int, ...] | None = None

    def contains(self, version: tuple[int, ...]) -> bool:
        if self.low is not None and version[: len(self.low)] < self.low:
            return False
        if self.high is not None and version[: len(self.high)] >= self.high:
            return False
        return True


def _bump(version: tuple[int, ...], place: int) -> tuple[int, ...]:
    """The smallest version that sits just past `version` at the given component index."""
    padded = list(version[: place + 1]) + [0] * (place + 1 - len(version))
    padded[place] += 1
    return tuple(padded[: place + 1])


def parse_spec(spec: str, lane: str, floor: bool = False) -> list[Interval]:
    """Turn a declared version string into the windows it allows.

    An empty list means the declaration exists but constrains nothing (a placeholder such as
    `"*"` or `"latest"`). This handles four shapes: a bare pin or minimum (`18`, `3.9.7`), an
    npm-style range (`^18.0.0`, `~3.9`, `>=14 <21`, `16 || 18`, `18.x`), a PEP 440 constraint
    (`>=3.8,<3.13`, `~=3.11`), and a Java release level (`1.8`, `17`).

    `floor` marks a bare number as a stated minimum rather than a pin -- true for `go.mod`'s
    `go` directive and Cargo's `rust-version`, false for a one-line version file, which names
    the exact release a project was tested against.
    """
    text = (spec or "").strip().strip("\"'")
    if not text or text in ("*", "x", "latest", "current", "node", "system", "stable", "default"):
        return []

    if lane == "node":
        codename = _NODE_CODENAMES.get(text.lower().replace("lts/", "").strip())
        if codename is not None:
            return [Interval((codename,), _bump((codename,), 0))]
        if text.lower().startswith("lts"):
            return []

    if lane == "java":
        # A release level doubles as a ceiling for the levels a current JDK has retired:
        # `--release 7` is gone past JDK 19, `--release 8` past JDK 25. Naming the ceiling
        # here keeps that retirement from reading as the repository's own failure.
        level = _norm_java(parse_version(text) or ())
        if not level:
            return []
        low = (level[0],)
        if level[0] <= 7:
            return [Interval(low, (20,))]
        if level[0] == 8:
            return [Interval(low, (25,))]
        return [Interval(low)]

    if not _HAS_OPERATOR.search(text):
        # A bare number: either a floor the tree states explicitly, or a pin that binds only
        # as far as the lane's own compatibility boundary reaches.
        version = parse_version(text)
        if version is None:
            return []
        precision = _PIN_PRECISION.get(lane, 2)
        if version[0] == 0:
            # Every semver-following ecosystem treats a 0.x minor as the real boundary.
            precision = max(precision, 2)
        if floor:
            return [Interval(version[:precision])]
        base = version[:precision]
        return [Interval(base, _bump(base, len(base) - 1))]

    # `||` separates an npm-style union of alternatives; a comma separates PEP 440 clauses
    # that must all hold at once. Split the union first, then narrow one window per branch.
    windows: list[Interval] = []
    for branch in re.split(r"\|\|", text):
        low: tuple[int, ...] | None = None
        high: tuple[int, ...] | None = None
        matched = False
        for op, raw in re.findall(
            r"(>=|<=|==|!=|~=|\^|~|>|<|=)?\s*v?([0-9][0-9.]*[0-9x*]?|\d)", branch
        ):
            wildcard = raw.endswith(("x", "*"))
            version = parse_version(raw)
            if version is None:
                continue
            matched = True
            place = max(0, len(version) - 1)
            if op == ">=":
                low = version if low is None else max(low, version)
            elif op == ">":
                low = _bump(version, place) if low is None else max(low, _bump(version, place))
            elif op == "<=":
                bound = _bump(version, place)
                high = bound if high is None else min(high, bound)
            elif op == "<":
                high = version if high is None else min(high, version)
            elif op == "^":
                # A caret on a 0.x release pins the minor -- semver's own carve-out.
                place = 1 if version[0] == 0 and len(version) > 1 else 0
                low, high = version, _bump(version, place)
            elif op in ("~", "~="):
                place = min(1, max(0, len(version) - 1))
                low, high = version, _bump(version, place)
            elif op == "!=":
                continue
            elif wildcard:
                low, high = version, _bump(version, max(0, len(version) - 1))
            else:
                # An unmarked `=` is a pin, not a lower bound: folding it in with `>=` would
                # quietly turn a request for exactly one release into "that or newer" and hide
                # the very mismatch this table exists to catch.
                low, high = version, _bump(version, place)
        if matched:
            windows.append(Interval(low, high))
    return windows


def satisfies(version: tuple[int, ...], windows: list[Interval]) -> bool:
    """True when `version` lands inside at least one window, or when there are none to check."""
    return not windows or any(w.contains(version) for w in windows)


def _display(windows: list[Interval], spec: str) -> str:
    """A short, safe-to-emit label for what was asked for."""
    if not windows:
        return re.sub(r"[^A-Za-z0-9._@+-]", "", spec)[:40] or "any"
    low, high = windows[0].low, windows[-1].high
    if low is not None and high is not None and high == _bump(low, len(low) - 1):
        return ".".join(str(n) for n in low)
    text = ".".join(str(n) for n in low) if low else ""
    top = ".".join(str(n) for n in high) if high else ""
    if text and top:
        return f"{text}-{top}"
    return text or (f"to-{top}" if top else "any")


# --- what the tree itself declares -----------------------------------------


def _text(path: Path, cap: int = 200_000) -> str:
    """The file's contents, truncated, or an empty string when it cannot be read."""
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _first_line(path: Path) -> str | None:
    """The first line of a one-value-per-file marker such as `.nvmrc`, comments stripped."""
    for line in _text(path, 400).splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            return stripped
    return None


def _first_group(pattern: str, body: str) -> str | None:
    """The first capture group `pattern` finds in `body`, scanning line by line.

    `re.MULTILINE` is not optional here: without it, a pattern anchored with `^` only matches
    at the very start of the file, and a `go.mod` that opens with a `module` line (every one
    does) would report no Go version at all.
    """
    match = re.search(pattern, body, re.I | re.M)
    return match.group(1).strip() if match else None


def _json_at(path: Path, *keys: str):
    """Walk a chain of keys into a JSON file's contents; None on anything missing or broken."""
    try:
        node = json.loads(_text(path))
    except (ValueError, TypeError):
        return None
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _tool_versions(root: Path) -> list[tuple[str, str]]:
    """Parse `.tool-versions` (asdf, mise) into (lane, spec) pairs for the lanes this module
    knows about, ignoring the rest."""
    aliases = {
        "nodejs": "node",
        "node": "node",
        "python": "python",
        "golang": "go",
        "go": "go",
        "rust": "rust",
        "ruby": "ruby",
        "java": "java",
        "dotnet": "dotnet",
        "dotnet-core": "dotnet",
    }
    out: list[tuple[str, str]] = []
    for line in _text(root / ".tool-versions", 20_000).splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) < 2:
            continue
        lane = aliases.get(parts[0].lower())
        if lane is None:
            continue
        # A line can read "java temurin-17.0.9" or "python 3.11.7 3.10.13"; strip a vendor
        # prefix and keep only the first version token.
        spec = re.sub(r"^[a-z-]+-", "", parts[1], flags=re.I)
        out.append((lane, spec))
    return out


def declarations(root: Path) -> list[dict]:
    """Every runtime version this tree names, one record per lane, most specific source first.

    Reads files only; nothing here runs a command or infers a lane the tree never mentioned.
    Each record carries `lane`, `spec`, `source`, `floor`, plus the parsed `windows` and a
    short `requested` label, and only the first (most specific) source for a lane survives --
    a pinned version file outranks a manifest range, which outranks `.tool-versions`.
    """
    found: list[dict] = []

    def add(lane: str, spec, source: str, floor: bool = False) -> None:
        if isinstance(spec, (int, float)):
            spec = str(spec)
        if isinstance(spec, str) and spec.strip():
            found.append(
                {"lane": lane, "spec": spec.strip()[:60], "source": source, "floor": floor}
            )

    add("node", _first_line(root / ".nvmrc"), ".nvmrc")
    add("node", _first_line(root / ".node-version"), ".node-version")
    add("node", _json_at(root / "package.json", "engines", "node"), "package.json engines")
    add("node", _json_at(root / "package.json", "volta", "node"), "package.json volta")

    add("python", _first_line(root / ".python-version"), ".python-version")
    pyproject = _text(root / "pyproject.toml")
    add(
        "python",
        _first_group(r"^\s*requires-python\s*=\s*[\"']([^\"']+)", pyproject),
        "pyproject requires-python",
    )
    add(
        "python",
        _first_group(r"^\s*python\s*=\s*[\"']([^\"']+)", pyproject),
        "pyproject poetry python",
    )
    add(
        "python",
        _first_group(r"python_requires\s*=\s*[\"']([^\"']+)", _text(root / "setup.py")),
        "setup.py python_requires",
    )
    add(
        "python",
        _first_group(r"^\s*python_requires\s*=\s*(.+)$", _text(root / "setup.cfg")),
        "setup.cfg python_requires",
    )
    add(
        "python",
        _first_group(r"python-([0-9.]+)", _text(root / "runtime.txt", 200)),
        "runtime.txt",
    )

    # `go 1.21` states the language level the module needs to compile, not a toolchain
    # request -- it is a floor, the same as Cargo's rust-version below.
    add(
        "go",
        _first_group(r"^\s*go\s+([0-9][0-9.]*)", _text(root / "go.mod")),
        "go.mod go",
        floor=True,
    )

    toolchain = _text(root / "rust-toolchain.toml") or _text(root / "rust-toolchain")
    channel = _first_group(r"channel\s*=\s*[\"']([^\"']+)", toolchain)
    if channel is None and toolchain.strip() and "=" not in toolchain and "[" not in toolchain:
        channel = toolchain.strip().splitlines()[0].strip()
    add("rust", channel, "rust-toolchain")
    add(
        "rust",
        _first_group(r"^\s*rust-version\s*=\s*[\"']([^\"']+)", _text(root / "Cargo.toml")),
        "Cargo.toml rust-version",
        floor=True,
    )

    add("ruby", _first_line(root / ".ruby-version"), ".ruby-version")
    add("ruby", _first_group(r"^\s*ruby\s+[\"']([^\"']+)", _text(root / "Gemfile")), "Gemfile ruby")

    pom = _text(root / "pom.xml")
    add(
        "java", _first_group(r"<maven\.compiler\.release>([^<]+)<", pom), "pom.xml compiler release"
    )
    add("java", _first_group(r"<maven\.compiler\.source>([^<]+)<", pom), "pom.xml compiler source")
    add("java", _first_group(r"<java\.version>([^<]+)<", pom), "pom.xml java version")
    add("java", _first_group(r"<release>([^<]+)</release>", pom), "pom.xml release")
    add("java", _first_group(r"<source>([^<]+)</source>", pom), "pom.xml source")
    gradle = (
        _text(root / "build.gradle")
        + "\n"
        + _text(root / "build.gradle.kts")
        + "\n"
        + _text(root / "gradle.properties", 20_000)
    )
    add("java", _first_group(r"JavaLanguageVersion\.of\((\d+)", gradle), "gradle toolchain")
    add(
        "java",
        _first_group(
            r"(?:source|target)Compatibility\s*=?\s*[\"']?(?:JavaVersion\.VERSION_)?([0-9_.]+)",
            gradle,
        ),
        "gradle compatibility",
    )

    add("dotnet", _json_at(root / "global.json", "sdk", "version"), "global.json sdk")

    for lane, spec in _tool_versions(root):
        add(lane, spec, ".tool-versions")

    seen: set[str] = set()
    ordered: list[dict] = []
    for record in found:
        if record["lane"] in seen:
            continue
        seen.add(record["lane"])
        record["windows"] = parse_spec(record["spec"], record["lane"], record["floor"])
        record["requested"] = _display(record["windows"], record["spec"])
        ordered.append(record)
    return ordered


# --- what this host has ------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One runtime installation this host can actually point a build at."""

    lane: str
    version: tuple[int, ...]
    bin_dir: Path
    executable: Path
    under_home: bool
    home_dir: Path | None = None  # java only: the JAVA_HOME a caller must set alongside it


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _under_home(path: Path) -> bool:
    """True when `path` sits inside the operator's own home directory."""
    home = _home()
    if home is None:
        return False
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return resolved == home or home in resolved.parents


def _path_dirs(env: dict) -> list[Path]:
    """The directories on `env`'s PATH, as `Path` objects, in order."""
    out: list[Path] = []
    for entry in (env.get("PATH") or "").split(os.pathsep):
        if entry:
            out.append(Path(entry))
    return out


#: Where the common version managers and package distributions put a runtime once installed.
#: Every hit only ever gets globbed, never executed to discover it -- and every one is tagged
#: with whether it lives under the operator's home so a caller can decide whether to trust it.
_SEARCH_GLOBS: dict[str, tuple[str, ...]] = {
    "node": (
        "~/.nvm/versions/node/*/bin",
        "~/.volta/tools/image/node/*/bin",
        "~/.asdf/installs/nodejs/*/bin",
        "~/.local/share/mise/installs/node/*/bin",
        "~/.local/share/mise/installs/nodejs/*/bin",
        "~/n/versions/node/*/bin",
        "~/.fnm/node-versions/*/installation/bin",
        "/opt/homebrew/opt/node@*/bin",
        "/usr/local/opt/node@*/bin",
        "/usr/local/lib/nodejs/*/bin",
        "/opt/node-*/bin",
    ),
    "python": (
        "~/.pyenv/versions/*/bin",
        "~/.asdf/installs/python/*/bin",
        "~/.local/share/mise/installs/python/*/bin",
        "~/.local/share/uv/python/*/bin",
        "/opt/homebrew/opt/python@*/bin",
        "/usr/local/opt/python@*/bin",
        "/opt/python/*/bin",
        "/usr/local/lib/python*/bin",
    ),
    "go": (
        "/usr/local/go/bin",
        "/usr/lib/go-*/bin",
        "~/.asdf/installs/golang/*/go/bin",
        "~/.local/share/mise/installs/go/*/bin",
        "/opt/go/bin",
        "/opt/homebrew/opt/go@*/bin",
    ),
    "ruby": (
        "~/.rbenv/versions/*/bin",
        "~/.asdf/installs/ruby/*/bin",
        "~/.local/share/mise/installs/ruby/*/bin",
        "~/.rvm/rubies/*/bin",
        "/opt/homebrew/opt/ruby@*/bin",
        "/usr/local/opt/ruby@*/bin",
    ),
    "rust": ("~/.rustup/toolchains/*/bin", "~/.cargo/bin"),
    "java": (
        "/usr/lib/jvm/*",
        "/Library/Java/JavaVirtualMachines/*/Contents/Home",
        "~/Library/Java/JavaVirtualMachines/*/Contents/Home",
        "~/.sdkman/candidates/java/*",
        "/opt/homebrew/opt/openjdk@*/libexec/openjdk.jdk/Contents/Home",
        "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home",
        "/usr/local/opt/openjdk@*/libexec/openjdk.jdk/Contents/Home",
        "~/.asdf/installs/java/*",
        "~/.local/share/mise/installs/java/*",
        "/opt/java/*",
    ),
    "dotnet": (
        "/usr/share/dotnet",
        "/usr/local/share/dotnet",
        "~/.dotnet",
        "/opt/homebrew/opt/dotnet/libexec",
    ),
}

#: The executable a lane is identified by, relative to its `bin_dir` (or `bin_dir` itself for
#: dotnet, which has no separate bin subfolder to speak of).
_EXE = {
    "node": "node",
    "python": "python3",
    "go": "go",
    "ruby": "ruby",
    "rust": "rustc",
    "java": "javac",
    "dotnet": "dotnet",
}

#: The flag that makes each lane's executable print its own version and exit.
_VERSION_ARG = {
    "node": ("--version",),
    "python": ("--version",),
    "go": ("version",),
    "ruby": ("--version",),
    "rust": ("--version",),
    "java": ("-version",),
    "dotnet": ("--version",),
}


def _expand(pattern: str) -> list[Path]:
    """The directories a single search glob matches. Never runs anything; only lists."""
    text = os.path.expanduser(pattern)
    if "*" not in text:
        path = Path(text)
        return [path] if path.is_dir() else []
    # Anchor the glob at the first fixed segment so `Path.glob` has somewhere concrete to
    # start from.
    head, _, _tail = text.partition("*")
    anchor = Path(head).parent if not head.endswith(os.sep) else Path(head)
    try:
        return sorted(
            p for p in anchor.glob(Path(text).relative_to(anchor).as_posix()) if p.is_dir()
        )
    except (OSError, ValueError):
        return []


def _probe_version(
    executable: Path, lane: str, env: dict, budget: list[int]
) -> tuple[int, ...] | None:
    """Ask an installed runtime its own version, spending one unit of the shared probe budget.

    Runs through `hazina_scan.env.run` with a fixed argument list and no shell, so this never
    executes anything beyond the one flag that makes a runtime announce itself.
    """
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        proc = _run_probe(
            [str(executable), *_VERSION_ARG[lane]],
            domain=env_mod.BUILD,
            env=dict(env),
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    version = parse_version(text.replace("go", " ").replace("Python", " "))
    return _norm_java(version) if lane == "java" and version else version


def _java_release_file(home: Path) -> tuple[int, ...] | None:
    """A JDK's version straight out of its own `release` file -- no subprocess required."""
    body = _text(home / "release", 8_000)
    raw = _first_group(r"JAVA_VERSION=\"?([0-9._]+)", body)
    return _norm_java(parse_version(raw) or ()) or None


def candidates(lane: str, env: dict, budget: list[int]) -> list[Candidate]:
    """List every `lane` install found on this host, newest version first, `under_home` tagged.

    Where the version is already spelled out somewhere free -- an install path like
    `node/v18.17.0/bin`, a binary named `python3.11`, a JDK `release` file -- it is read from
    there. Only when none of those sources apply does this fall back to actually invoking the
    executable with `--version` and spending part of `budget`.
    """
    found: dict[Path, Candidate] = {}
    exe_name = _EXE[lane]

    def consider(bin_dir: Path, executable: Path, home_dir: Path | None = None) -> None:
        if executable in found or not executable.is_file():
            return
        version: tuple[int, ...] | None = None
        if lane == "java" and home_dir is not None:
            version = _java_release_file(home_dir)
        if version is None and lane == "python":
            named = re.fullmatch(r"python(\d+)\.(\d+)", executable.name)
            if named:
                version = (int(named.group(1)), int(named.group(2)))
        if version is None:
            in_path = _VERSION_IN_PATH.search(str(bin_dir))
            if in_path:
                version = tuple(int(g) for g in in_path.groups() if g is not None)
        if version is None:
            version = _probe_version(executable, lane, env, budget)
        if version:
            found[executable] = Candidate(
                lane, version, bin_dir, executable, _under_home(executable), home_dir
            )

    search_dirs = list(_path_dirs(env))
    for pattern in _SEARCH_GLOBS.get(lane, ()):
        search_dirs.extend(_expand(pattern))

    for directory in search_dirs:
        if lane == "java":
            # A glob hit for java names a JAVA_HOME, not a bin directory, so it is handled on
            # its own path and never falls through to the generic `consider` call below.
            home_dir = None
            if (directory / "bin" / "javac").is_file():
                home_dir = directory
            elif (directory / "Contents" / "Home" / "bin" / "javac").is_file():
                home_dir = directory / "Contents" / "Home"
            if home_dir is not None:
                consider(home_dir / "bin", home_dir / "bin" / "javac", home_dir)
                continue
        if lane == "dotnet":
            consider(directory, directory / "dotnet")
        if not directory.is_dir():
            continue
        consider(directory, directory / exe_name)
        if lane == "python":
            for extra in sorted(directory.glob("python3.[0-9]")) + sorted(
                directory.glob("python3.[0-9][0-9]")
            ):
                consider(directory, extra)

    if lane == "python":
        # The interpreter running this process is always a legitimate candidate, and on a bare
        # host it may be the only one -- but it competes on its version like any other, never
        # by default of being the one already running.
        me = Path(sys.executable)
        if me.is_file():
            consider(me.parent, me)

    return sorted(found.values(), key=lambda c: c.version, reverse=True)


# --- choosing among what is installed ---------------------------------------


@dataclass
class Plan:
    """What resolving one project root against this host's runtimes produced."""

    records: list[dict] = field(default_factory=list)
    overlay: dict[str, str] = field(default_factory=dict)
    interpreter: dict[str, str] = field(default_factory=dict)
    unsatisfied: list[str] = field(default_factory=list)


def _choose(
    windows: list[Interval], pool: list[Candidate], default: Candidate | None
) -> Candidate | None:
    """Pick the installation to build with, or None when nothing in `pool` fits.

    The runtime a plain command already resolves to on this host wins whenever it is in the
    pool and satisfies the window -- switching runtimes is itself a way to turn a build that
    worked into one that does not, so we only move when staying put would run something the
    tree's own declaration rules out. When a move is required, the target is the candidate
    closest to the declared floor, on the theory that the floor is what a project was actually
    tested against and a newer release is exactly what tends to break it.
    """
    allowed = [c for c in pool if satisfies(c.version, windows)]
    if not allowed:
        return None
    if default is not None and satisfies(default.version, windows):
        return default
    floor = windows[0].low if windows and windows[0].low else None
    if floor is None:
        return allowed[0]
    return min(allowed, key=lambda c: (c.version[: len(floor)], tuple(-n for n in c.version)))


def _default_of(lane: str, pool: list[Candidate], env: dict) -> Candidate | None:
    """The candidate a bare `node` / `python3` / `javac` on this PATH would already resolve to.

    Matched back against `pool` by resolved path, so it reuses the version that search already
    found instead of running a second probe that could in principle disagree with the first.
    """
    if lane == "python":
        wanted = Path(sys.executable)
    else:
        found = which(_EXE[lane], env)
        if found is None:
            return None
        wanted = Path(found)
    try:
        target = wanted.resolve()
    except (OSError, ValueError):
        return None
    for candidate in pool:
        try:
            if candidate.executable.resolve() == target:
                return candidate
        except (OSError, ValueError):
            continue
    return None


def resolve(
    root: Path, env: dict, *, allow_home: bool = False, lanes: tuple[str, ...] | None = None
) -> Plan:
    """Match `root`'s declared runtime versions against what this host has installed.

    Read-only end to end: nothing is fetched, built, or written. `lanes`, when given, restricts
    the search to those families, so a caller that only cares about Node does not pay to
    enumerate every JDK on the machine. `allow_home` controls whether a runtime living under
    the operator's own home directory may be selected; this tool always leaves it False.
    """
    plan = Plan()
    budget = [_MAX_PROBES]
    for declaration in declarations(root):
        lane = declaration["lane"]
        if lanes is not None and lane not in lanes:
            continue
        every = candidates(lane, env, budget)
        pool = [c for c in every if allow_home or not c.under_home]
        default = _default_of(lane, pool, env)
        chosen = _choose(declaration["windows"], pool, default)
        present = default or (pool[0] if pool else None)
        record = {
            "lane": lane,
            "requested": declaration["requested"],
            "requested_source": declaration["source"],
            "used": None,
            "satisfied": None,
            "note": "",
        }
        if chosen is not None:
            record["used"] = ".".join(str(n) for n in chosen.version)
            record["satisfied"] = True
            record["note"] = (
                f"{lane} {record['used']} on this host falls inside the {record['requested']} "
                f"window the tree declared"
            )
            if chosen is not default:
                _apply(plan, lane, chosen)
                record["note"] += (
                    ", so it was picked over the PATH default to stay inside that window"
                )
        elif present is None:
            record["satisfied"] = False
            record["note"] = (
                f"the tree declares {lane} {record['requested']} but this host carries no "
                f"{lane} runtime at all, which is a gap in the runner rather than a fact "
                f"about the repository"
            )
            plan.unsatisfied.append(lane)
        else:
            record["used"] = ".".join(str(n) for n in present.version)
            record["satisfied"] = False
            record["note"] = (
                f"the tree declares {lane} {record['requested']} and the nearest this host "
                f"carries is {record['used']}, so a failure downstream in this lane is "
                f"attributed to the runner rather than to the repository"
            )
            plan.unsatisfied.append(lane)
            if present is not default:
                _apply(plan, lane, present)
        plan.records.append(record)
    return plan


def _apply(plan: Plan, lane: str, chosen: Candidate) -> None:
    """Record how a child process reaches `chosen`, without touching any other lane's setup."""
    if lane == "python":
        # Python is always invoked by absolute path, so no PATH change is needed for it -- and
        # that is also why it behaves identically whether or not home installs are allowed.
        plan.interpreter[lane] = str(chosen.executable)
        return
    existing = plan.overlay.get("PATH")
    prefix = str(chosen.bin_dir)
    plan.overlay["PATH"] = prefix if existing is None else prefix + os.pathsep + existing
    if lane == "java" and chosen.home_dir is not None:
        plan.overlay["JAVA_HOME"] = str(chosen.home_dir)


def apply_overlay(env: dict, overlay: dict) -> dict:
    """`env` with `overlay` merged in. A PATH entry is prepended, so nothing already reachable
    on the host's PATH becomes unreachable."""
    if not overlay:
        return env
    merged = dict(env)
    for key, value in overlay.items():
        if key == "PATH" and merged.get("PATH"):
            merged["PATH"] = value + os.pathsep + merged["PATH"]
        else:
            merged[key] = value
    return merged


def summarise(per_project: list[list[dict]]) -> dict:
    """Roll every project root's runtime records up into the whole-tree fields the build
    block emits.

    Takes the record lists rather than the `Plan` objects themselves, because by the time a
    whole scan is done the plans are gone and only what got written down survives. Each field
    is a list rather than a single value: a polyglot tree can ask for several runtimes at
    once, and reporting only the first would bury the exact mismatch that stopped the build.
    """
    declared: list[str] = []
    unsatisfied: list[str] = []
    requested: list[str] = []
    used: list[str] = []
    notes: list[str] = []
    for records in per_project:
        for record in records or ():
            lane = record["lane"]
            if lane in declared:
                continue
            declared.append(lane)
            requested.append(f"{lane}@{record['requested']}")
            if record["used"]:
                used.append(f"{lane}@{record['used']}")
            if record["satisfied"] is False:
                unsatisfied.append(lane)
                notes.append(record["note"])
    return {
        "runtime_lanes_declared": declared,
        "runtime_lanes_unsatisfied": unsatisfied,
        "runtime_requested": requested,
        "runtime_used": used,
        "runtime_resolution_note": (
            "; ".join(notes[:3])
            if notes
            else (
                "this host can supply every runtime lane the tree names"
                if declared
                else "no lane appears here because the tree names no runtime version at all"
            )
        ),
    }


def which(name: str, env: dict) -> str | None:
    """`shutil.which`, but against a supplied environment's PATH rather than the operator's."""
    return shutil.which(name, path=env.get("PATH"))
