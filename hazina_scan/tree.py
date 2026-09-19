"""What a checkout is, read from the files themselves.

One walk of the working tree answers every question in this module: which languages the
repository is written in, how big its files are, how much of it is infrastructure, and how
much of it is tests. Nothing here executes the repository, imports it, or resolves a
dependency; every file is opened read-only, decoded leniently, and read in bounded amounts.

Two rules run through all of it and are worth stating once.

*Blank lines are not lines.* Every count in this file is of non-empty lines, so a file
padded with whitespace measures the same as one that is not.

*A manifest is read, never believed about itself.* The dependency, framework and tooling
answers below come from parsed manifests -- the names a repository actually declares --
and not from searching manifest text, because a pyproject.toml whose description says
"migrating off Flask" declares no framework and a README sentence is not a dependency.

*Infrastructure is counted apart from source.* YAML, HCL and JSON are not code extensions,
so a Terraform module or a Kubernetes manifest is invisible to the language and file-size
accounting until `analyze_iac` claims it -- and it is only claimed on positive evidence,
never because a file has a `.yaml` on the end. The block assembly folds the infrastructure
tallies back in afterwards, which is what keeps a repository of pure manifests from
reporting zero lines of an unknown language without double-counting anything.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import defaultdict
from pathlib import Path

try:
    # CI configuration is YAML and every CI system requires it to parse, so the jobs, the
    # steps and the `if:` guards are read rather than the file being searched for the word
    # "test". Optional, so that a checkout without it still runs -- and when it is missing
    # the three CI flags are reported unmeasured rather than guessed.
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a pinned dependency
    yaml = None

from . import vocab

#: A file bigger than this stops being measured line by line; the cap is what gets
#: reported. A file of half a million lines is a data file whatever its extension says.
MAX_LINES = 50_000


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def count_lines(path: Path, max_lines: int = MAX_LINES) -> int:
    """Non-empty lines in a file, `max_lines` at most. Unreadable or binary counts 0.

    Decoding errors are ignored rather than raised: a file with one bad byte in it is
    still a file with a line count, and refusing to count it would silently shrink the
    repository. A file that cannot be opened at all counts zero, which is the only honest
    answer available without reporting something we did not read.
    """
    try:
        count = 0
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    return max_lines
                if line.strip():
                    count += 1
        return count
    except (OSError, PermissionError):
        return 0


def _read_head(path: Path, max_bytes: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read(max_bytes)
    except (OSError, PermissionError):
        return ""


def is_generated(path: Path) -> bool:
    """Was this file written by a tool? Decided from the first 500 bytes, where a
    generator puts its banner if it puts one anywhere."""
    head = _read_head(path, 500)
    return any(pattern.search(head) for pattern in vocab.GENERATED_PATTERNS)


# ---------------------------------------------------------------------------
# Walking the tree
# ---------------------------------------------------------------------------


def is_test_file(rel_path: str) -> bool:
    return any(pattern.search(rel_path) for pattern in vocab.TEST_PATTERNS)


def is_fixture_file(rel_path: str) -> bool:
    return any(pattern.search(rel_path) for pattern in vocab.FIXTURE_PATTERNS)


def should_skip_dir(name: str) -> bool:
    """Named in the skip list, or a dot-directory that is not one of the few kept."""
    if name in vocab.SKIP_DIRS:
        return True
    return name.startswith(".") and name not in vocab.DOT_DIRS_KEPT


def walk_source_files(root: Path) -> list[tuple[Path, str]]:
    """Every file under `root` that survives the skip list, as (absolute, relative).

    The relative path is posix-style on every operating system, because every pattern in
    this module is anchored on `/`: without that, `tests\\foo_test.py` would be a source
    file on Windows and a test file everywhere else.
    """
    results: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # In place, so os.walk never descends into what was pruned.
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for name in filenames:
            abs_path = Path(dirpath) / name
            results.append((abs_path, abs_path.relative_to(root).as_posix()))
    return results


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------


def jvm_dotnet_loc_share(loc_by_language: dict[str, int], total_loc: int) -> float | None:
    """Share of counted lines written in the JVM/.NET family, 0.0 to 1.0.

    A scalar rather than a per-language key because a gate cannot read
    `loc_by_language.Java`: an absent key is unmeasured rather than zero, so every
    repository without Java would land in a partial verdict and the scores would stop
    being comparable. This is always emitted, so it has no such hole.

    None only when nothing at all was counted -- a repository with no source is a
    question for the empty-repository screen, not something to report a 0.0 share for.
    """
    if not total_loc:
        return None
    family = sum(int(loc_by_language.get(lang) or 0) for lang in vocab.JVM_DOTNET_LANGUAGES)
    return round(family / total_loc, 4)


def analyze_languages(all_files: list[tuple[Path, str]]) -> dict:
    """Lines and file counts per language, over code files that nobody generated.

    A file counts towards its language even when it is empty, because an empty
    `__init__.py` is still a Python file; it contributes no lines, which is the truth
    about it. The primary language is simply the one with the most lines, and the
    secondary languages are the next three -- no threshold, because a share small enough
    to argue about is already visible in `loc_by_language`.
    """
    loc_by_lang: dict[str, int] = defaultdict(int)
    file_count_by_lang: dict[str, int] = defaultdict(int)

    for abs_path, _rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in vocab.CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            continue
        lang = vocab.LANGUAGE_BY_EXT.get(ext, "Other")
        loc_by_lang[lang] += count_lines(abs_path)
        file_count_by_lang[lang] += 1

    ordered = sorted(loc_by_lang.items(), key=lambda item: item[1], reverse=True)
    return {
        "primary_language": ordered[0][0] if ordered else "Unknown",
        "secondary_languages": [lang for lang, _ in ordered[1:4]] if len(ordered) > 1 else [],
        "total_loc": sum(loc_by_lang.values()),
        "loc_by_language": dict(ordered),
        "file_count_by_language": dict(file_count_by_lang),
    }


# ---------------------------------------------------------------------------
# File sizes
# ---------------------------------------------------------------------------


def analyze_file_sizes(
    all_files: list[tuple[Path, str]],
    top_n: int = 10,
    extra_files: list[tuple[int, str]] | None = None,
) -> dict:
    """The size distribution of the source files, and the largest of them.

    `extra_files` takes already-counted `(loc, rel_path)` entries -- the infrastructure
    manifests from `analyze_iac` -- so that a repository which is all infrastructure is
    not reported as having no files at all. They are counted here and nowhere else, so
    nothing is counted twice.

    Empty files are left out of the distribution entirely rather than entered as zeros,
    which would otherwise drag a median towards a number no file in the repository has.
    """
    sizes: list[tuple[int, str]] = []
    generated_excluded = 0

    for abs_path, rel_path in all_files:
        if abs_path.suffix.lower() not in vocab.CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            generated_excluded += 1
            continue
        loc = count_lines(abs_path)
        if loc > 0:
            sizes.append((loc, rel_path))

    if extra_files:
        sizes.extend((loc, rel) for loc, rel in extra_files if loc > 0)

    if not sizes:
        return {
            "total_source_files": 0,
            "total_non_test_source_files": 0,
            "median_loc": 0,
            "p90_loc": 0,
            "largest_files": [],
            "god_files_over_500": 0,
            "god_files_over_1000": 0,
            "generated_excluded": 0,
        }

    sizes.sort(key=lambda item: item[0], reverse=True)
    locs = sorted(loc for loc, _ in sizes)
    n = len(locs)
    return {
        "total_source_files": n,
        # The same files minus the tests, so the test:source ratio has a denominator that
        # does not already contain the tests it is being compared against.
        "total_non_test_source_files": sum(1 for _, rel in sizes if not is_test_file(rel)),
        "median_loc": locs[n // 2],
        "p90_loc": locs[int(n * 0.9)],
        "largest_files": [{"path": rel, "loc": loc} for loc, rel in sizes[:top_n]],
        "god_files_over_500": sum(1 for loc, _ in sizes if loc > 500),
        "god_files_over_1000": sum(1 for loc, _ in sizes if loc > 1000),
        "generated_excluded": generated_excluded,
    }


# ---------------------------------------------------------------------------
# Infrastructure as code
# ---------------------------------------------------------------------------


def _is_ci_config_path(rel_path: str) -> bool:
    low = rel_path.lower()
    return any(
        low.startswith(prefix) or ("/" + prefix) in ("/" + low)
        for prefix in vocab.IAC_CI_DIR_PREFIXES
    )


def _is_iac_candidate(abs_path: Path) -> bool:
    """Could this file be infrastructure at all? Extension and filename only -- no read."""
    name = abs_path.name.lower()
    ext = abs_path.suffix.lower()
    return (
        ext in vocab.IAC_HCL_EXTENSIONS
        or ext in vocab.IAC_YAML_EXTENSIONS
        or ext == ".json"
        or name.startswith("dockerfile")
        or name.endswith(".dockerfile")
    )


def _detect_iac_type(abs_path: Path, rel_path: str, chart_dirs: set[str]) -> str | None:
    """Which kind of infrastructure this file is, or None.

    Positive identification only: every answer rests on a filename that means one thing
    (`Chart.yaml`, `main.tf`) or on a marker read out of the file itself. A YAML file that
    is merely config stays unclaimed, because counting it would turn every repository's
    settings into infrastructure.
    """
    name = abs_path.name.lower()
    ext = abs_path.suffix.lower()

    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "Dockerfile"
    if ext in vocab.IAC_HCL_EXTENSIONS:
        return "Terraform"
    if ext not in vocab.IAC_YAML_EXTENSIONS and ext != ".json":
        return None
    if _is_ci_config_path(rel_path):
        return None

    head = _read_head(abs_path, vocab.IAC_SNIFF_BYTES)
    low = head.lower()

    if ext == ".json":
        if "awstemplateformatversion" in low or '"aws::' in low:
            return "CloudFormation"
        return None

    # ---- YAML ----
    parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    # A Chart.yaml is definitive. Anything else is Helm only when it belongs to one
    # SPECIFIC chart directory -- a directory that holds a Chart.yaml, where "" is the
    # repository root: a values file sitting beside that Chart.yaml, or a file under the
    # chart's own templates/. A chart at the root must not claim every nested templates/
    # in the tree, which is how unrelated directories end up filed as Helm.
    if name in ("chart.yaml", "chart.yml"):
        return "Helm"
    if name.startswith("values") and parent in chart_dirs:
        return "Helm"
    slashed = "/" + rel_path
    if "/templates/" in slashed:
        chart_part = slashed.split("/templates/", 1)[0].lstrip("/")  # "" at the top level
        if chart_part in chart_dirs:
            return "Helm"

    # Canonical compose filenames only: docker-compose*.y*ml, compose.y*ml and its
    # overrides. Not a bare `compose*` prefix, which also matches composer.yaml and
    # composition.yml. An oddly named real compose file is still caught by the content
    # check further down.
    if name.startswith("docker-compose") or name.startswith("compose."):
        return "Docker Compose"

    if "apiversion:" in low and "kind:" in low:
        for line in head.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("kind:"):
                kind = stripped.split(":", 1)[1].strip().strip("\"'")
                if kind in vocab.K8S_KINDS:
                    return "Kubernetes"
                break
        # apiVersion and kind, but a kind nobody recognises: still a manifest or a custom
        # resource, and calling it anything else would be worse.
        return "Kubernetes"

    if "awstemplateformatversion" in low or ("resources:" in low and "aws::" in low):
        return "CloudFormation"

    if head.startswith("services:") or "\nservices:" in head:
        if "image:" in low or "build:" in low or "container_name:" in low:
            return "Docker Compose"

    # Ansible has to be proved from content. A `tasks/` or `handlers/` directory is common
    # in repositories that have never seen Ansible, and matching the path alone inflated
    # the infrastructure count. So: an unmistakable Ansible marker anywhere in the head,
    # or a role/playbook path corroborated by something task-list shaped.
    ansible_path = "/roles/" in slashed or "/playbooks/" in slashed or parent.endswith("playbooks")
    strong = (
        head.startswith("hosts:")
        or "\nhosts:" in head
        or "ansible.builtin." in low
        or "\nbecome:" in low
        or "gather_facts:" in low
        or "\nvars_files:" in low
    )
    task_shaped = "\n- name:" in head or head.startswith("- name:") or "\ntasks:" in low
    if strong or (ansible_path and task_shaped):
        return "Ansible"

    return None


def analyze_iac(all_files: list[tuple[Path, str]], root: Path) -> dict:
    """Infrastructure files, their lines, and the per-kind signals.

    `_sized_files` comes back with the result so the caller can fold these files into the
    size distribution; it is working state, not an output field.
    """
    # The directories that hold a Chart.yaml, which is what lets a templates/*.yaml be
    # attributed to the chart it belongs to.
    chart_dirs: set[str] = set()
    for _abs_path, rel_path in all_files:
        base = rel_path.rsplit("/", 1)[-1].lower()
        if base in ("chart.yaml", "chart.yml"):
            chart_dirs.add(rel_path.rsplit("/", 1)[0] if "/" in rel_path else "")

    loc_by_type: dict[str, int] = defaultdict(int)
    files_by_type: dict[str, int] = defaultdict(int)
    sized_files: list[tuple[int, str]] = []
    sniffed = 0

    for abs_path, rel_path in all_files:
        if not _is_iac_candidate(abs_path):
            continue
        if is_test_file(rel_path) or is_fixture_file(rel_path) or is_generated(abs_path):
            continue
        ext = abs_path.suffix.lower()
        # Only the YAML and JSON candidates need a content read, so only they are capped --
        # and they are skipped rather than breaking the loop, which would drop the cheap
        # .tf and Dockerfile detections that come after them.
        if ext in vocab.IAC_YAML_EXTENSIONS or ext == ".json":
            sniffed += 1
            if sniffed > vocab.IAC_MAX_SNIFF:
                continue
        iac_type = _detect_iac_type(abs_path, rel_path, chart_dirs)
        if not iac_type:
            continue
        loc = count_lines(abs_path)
        if loc <= 0:
            continue
        loc_by_type[iac_type] += loc
        files_by_type[iac_type] += 1
        sized_files.append((loc, rel_path))

    return {
        "iac_loc": sum(loc_by_type.values()),
        "iac_file_count": sum(files_by_type.values()),
        "iac_loc_by_type": dict(sorted(loc_by_type.items(), key=lambda item: -item[1])),
        "iac_file_count_by_type": dict(files_by_type),
        "_sized_files": sized_files,
        # Per-kind signals, read by the classifier as the authoritative counts.
        "terraform_file_count": files_by_type.get("Terraform", 0),
        "terraform_present": files_by_type.get("Terraform", 0) > 0,
        "terraform_loc": loc_by_type.get("Terraform", 0),
        "k8s_manifest_count": files_by_type.get("Kubernetes", 0),
        "k8s_loc": loc_by_type.get("Kubernetes", 0),
        "docker_compose_file_count": files_by_type.get("Docker Compose", 0),
        "docker_compose_loc": loc_by_type.get("Docker Compose", 0),
        "helm_file_count": files_by_type.get("Helm", 0),
        "helm_loc": loc_by_type.get("Helm", 0),
        "ansible_file_count": files_by_type.get("Ansible", 0),
        "ansible_loc": loc_by_type.get("Ansible", 0),
        "cloudformation_file_count": files_by_type.get("CloudFormation", 0),
        "cloudformation_loc": loc_by_type.get("CloudFormation", 0),
        "dockerfile_loc": loc_by_type.get("Dockerfile", 0),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def analyze_tests(all_files: list[tuple[Path, str]]) -> dict:
    """Split the test tree into specs and the data the specs read.

    A file in a test directory that is a fixture by path, or simply enormous, is recorded
    data rather than a spec. Both are reported, because a repository with a thousand
    snapshots and four assertions should not read as a well-tested one.
    """
    spec_files: list[str] = []
    fixture_files: list[str] = []

    for abs_path, rel_path in all_files:
        if not is_test_file(rel_path):
            continue
        loc = count_lines(abs_path)
        if is_fixture_file(rel_path) or loc > vocab.FIXTURE_LOC_THRESHOLD:
            fixture_files.append(rel_path)
        else:
            spec_files.append(rel_path)

    return {
        "spec_files": len(spec_files),
        "fixture_and_snapshot_files": len(fixture_files),
        "total_test_files": len(spec_files) + len(fixture_files),
        "spec_file_paths_sample": spec_files[:20],
    }


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------

#: How much of a manifest is read when it is looked at for a marker rather than parsed.
#: A manifest that needs more than 16 KB to say which linter it configures is not saying it.
MANIFEST_HEAD_BYTES = 16_384

#: How much is read when the manifest is parsed as TOML or JSON. Larger, because a parse
#: of a truncated document fails outright -- a 200 KB package.json is unusual but real.
MANIFEST_PARSE_BYTES = 262_144

#: How much is read of the line-oriented dependency lists, which are scanned rather than
#: parsed and so lose only their tail when they are longer than this.
DEP_LIST_BYTES = 131_072

#: How much of a lockfile is counted. Lockfiles run to megabytes and the count is of
#: entries in what was read; past a megabyte the answer is "a great many" either way.
LOCKFILE_BYTES = 1_000_000

#: One PEP 508 requirement's name, up to the first version specifier or marker.
_PEP508_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: A single-line quoted string, which is how setup.py spells a requirement.
_QUOTED = re.compile(r"""['"]([^'"\n]+)['"]""")
#: `gem "rails"` in a Gemfile.
_GEMFILE_GEM = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]""", re.MULTILINE)
#: The two halves of a Maven coordinate, each in its own element.
_POM_COORD = re.compile(r"<(groupId|artifactId)>\s*([^<\s]+)\s*</\1>")
#: A Gradle coordinate string, `"group:artifact:version"`, keeping group and artifact.
_GRADLE_COORD = re.compile(r"""['"]([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(?::[^'"]*)?['"]""")
#: A go.mod requirement: a module path with a dot in its first segment, then a version.
_GO_MODULE = re.compile(
    r"^\s*(?:require\s+)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[^\s]+)\s+v\d", re.MULTILINE
)
#: The requirement lists setup.py passes to setup().
_SETUP_PY_REQUIRES = re.compile(
    r"(?:install_requires|extras_require|setup_requires|tests_require)\s*=\s*[\[{](.*?)[\]}]",
    re.DOTALL,
)


def _read_json(path: Path, max_bytes: int = MANIFEST_PARSE_BYTES) -> dict:
    """A JSON manifest as a mapping. A file that is missing, unreadable, malformed or
    simply not an object reads as `{}` -- there is nothing to be learned from it either
    way, and a broken package.json must not stop the rest of the scan."""
    try:
        parsed = json.loads(_read_head(path, max_bytes))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_toml(path: Path, max_bytes: int = MANIFEST_PARSE_BYTES) -> dict:
    # TOMLDecodeError is a ValueError, as is JSONDecodeError above.
    try:
        return tomllib.loads(_read_head(path, max_bytes))
    except ValueError:
        return {}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _table_values(value) -> list:
    """The values of a TOML/JSON table, or nothing when the manifest put something else
    where a table belonged."""
    return list(value.values()) if isinstance(value, dict) else []


def _normalize_dep_name(name: str) -> str:
    """Lowercase, de-quote and PEP 503-normalise one dependency name.

    `/`, `:` and `.` survive, because they are what segments an npm scope
    (`@angular/core`), a Go module path (`go.mongodb.org/mongo-driver`) and a Maven
    coordinate (`group:artifact`) -- and the keyword matcher anchors on those separators.
    Anything that is not shaped like a package name at all becomes the empty string and is
    dropped by the caller.
    """
    cleaned = name.strip().strip("'\"").lower().replace("_", "-").rstrip("/")
    return cleaned if re.fullmatch(r"[@a-z0-9][a-z0-9./:@+-]*", cleaned or "") else ""


def _requirement_name(spec: str) -> str:
    """The name in one PEP 508 requirement: `torch>=2.1 ; extra == "gpu"` -> `torch`.

    A URL, a path, a flag or a comment is not a named requirement and returns "".
    """
    spec = spec.strip().strip("'\"")
    if not spec or spec.startswith(("-", "#", "http", "git+", "file:", ".", "/")):
        return ""
    match = _PEP508_NAME.match(spec)
    return match.group(0) if match else ""


def _python_requirement_names(specs) -> list[str]:
    if not isinstance(specs, list):
        return []
    return [_requirement_name(s) for s in specs if isinstance(s, str)]


def _yaml_block_keys(text: str, sections: tuple[str, ...]) -> list[str]:
    """The keys nested one level under any of `sections` in a simple YAML mapping.

    Deliberately naive rather than a parser: the blocks this reads -- a pubspec's
    `dependencies`, a conda environment's `dependencies`/`pip` -- are flat lists of names,
    and a YAML dependency is not worth taking on to read a dependency list.
    """
    names: list[str] = []
    section_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if section_indent is not None and indent <= section_indent:
            section_indent = None
        if stripped.rstrip(":") in sections and stripped.endswith(":"):
            section_indent = indent
            continue
        if section_indent is None or indent <= section_indent:
            continue
        entry = stripped.lstrip("- ").split("#", 1)[0]
        names.append(re.split(r"[=<>!~\s:]", entry, maxsplit=1)[0])
    return names


def _setup_cfg_requirement_names(text: str) -> list[str]:
    """The requirements in setup.cfg's `install_requires`/`setup_requires` blocks, which
    are an indented list under the key rather than a value beside it."""
    names: list[str] = []
    in_block = False
    for line in text.splitlines():
        if re.match(r"^\s*(install_requires|setup_requires)\s*=", line):
            in_block = True
            names.append(_requirement_name(line.split("=", 1)[1]))
            continue
        if in_block:
            # The block ends at the first line that is not indented under it.
            if line.strip() and not line[:1].isspace():
                in_block = False
                continue
            names.append(_requirement_name(line))
    return names


def _npm_dep_names(manifest: dict) -> list[str]:
    names: list[str] = []
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names.extend(_mapping(manifest.get(field)))
    return names


def _pyproject_dep_names(data: dict) -> list[str]:
    """Every dependency a pyproject.toml declares, in any of the four places it may:
    PEP 621 `project`, PEP 735 dependency groups, the build backend's own requirements,
    and poetry's tables."""
    names: list[str] = []
    project = _mapping(data.get("project"))
    names.extend(_python_requirement_names(project.get("dependencies")))
    for extra in _table_values(project.get("optional-dependencies")):
        names.extend(_python_requirement_names(extra))
    for group in _table_values(data.get("dependency-groups")):
        names.extend(_python_requirement_names(group))
    names.extend(_python_requirement_names(_mapping(data.get("build-system")).get("requires")))
    poetry = _mapping(_mapping(data.get("tool")).get("poetry"))
    for field in ("dependencies", "dev-dependencies"):
        names.extend(_mapping(poetry.get(field)))
    for group in _table_values(poetry.get("group")):
        names.extend(_mapping(_mapping(group).get("dependencies")))
    return names


def collect_dependency_names(root: Path) -> dict[str, list[str]]:
    """The direct dependency names declared by the root manifests, keyed by ecosystem.

    Keying by ecosystem is what stops one language's keyword matching another language's
    package: PyPI ships distributions called `lit`, `astro` and `solid`, and none of them
    makes a repository a frontend. Only the root is read -- a dependency declared three
    directories down belongs to a sub-package, and a monorepo's leaves would otherwise
    decide what the repository as a whole is.
    """
    names: dict[str, set[str]] = defaultdict(set)

    def add(ecosystem: str, raw_names) -> None:
        for raw in raw_names:
            normalized = _normalize_dep_name(raw)
            if normalized:
                names[ecosystem].add(normalized)

    if (root / "package.json").exists():
        add("npm", _npm_dep_names(_read_json(root / "package.json")))

    for req in ("requirements.txt", "requirements-dev.txt"):
        if (root / req).exists():
            text = _read_head(root / req, DEP_LIST_BYTES)
            add("pypi", [_requirement_name(line.split("#", 1)[0]) for line in text.splitlines()])

    if (root / "pyproject.toml").exists():
        add("pypi", _pyproject_dep_names(_read_toml(root / "pyproject.toml")))

    if (root / "Pipfile").exists():
        pipfile = _read_toml(root / "Pipfile")
        for field in ("packages", "dev-packages"):
            add("pypi", _mapping(pipfile.get(field)))

    if (root / "setup.py").exists():
        text = _read_head(root / "setup.py", DEP_LIST_BYTES)
        for block in _SETUP_PY_REQUIRES.findall(text):
            add("pypi", [_requirement_name(s) for s in _QUOTED.findall(block)])

    if (root / "setup.cfg").exists():
        add("pypi", _setup_cfg_requirement_names(_read_head(root / "setup.cfg", DEP_LIST_BYTES)))

    for env in ("environment.yml", "environment.yaml"):
        if (root / env).exists():
            text = _read_head(root / env, DEP_LIST_BYTES)
            add("pypi", _yaml_block_keys(text, ("dependencies", "pip")))

    if (root / "go.mod").exists():
        add("go", _GO_MODULE.findall(_read_head(root / "go.mod", DEP_LIST_BYTES)))

    if (root / "Cargo.toml").exists():
        cargo = _read_toml(root / "Cargo.toml")
        for field in ("dependencies", "dev-dependencies", "build-dependencies"):
            add("cargo", _mapping(cargo.get(field)))
        add("cargo", _mapping(_mapping(cargo.get("workspace")).get("dependencies")))

    if (root / "Gemfile").exists():
        add("gem", _GEMFILE_GEM.findall(_read_head(root / "Gemfile", DEP_LIST_BYTES)))

    if (root / "composer.json").exists():
        composer = _read_json(root / "composer.json")
        for field in ("require", "require-dev"):
            add("composer", _mapping(composer.get(field)))

    if (root / "pom.xml").exists():
        text = _read_head(root / "pom.xml", MANIFEST_PARSE_BYTES)
        add("maven", [value for _, value in _POM_COORD.findall(text)])

    for gradle in ("build.gradle", "build.gradle.kts"):
        if (root / gradle).exists():
            add("maven", _GRADLE_COORD.findall(_read_head(root / gradle, DEP_LIST_BYTES)))

    if (root / "pubspec.yaml").exists():
        text = _read_head(root / "pubspec.yaml", DEP_LIST_BYTES)
        add("pub", _yaml_block_keys(text, ("dependencies", "dev_dependencies")))

    return {ecosystem: sorted(found) for ecosystem, found in names.items()}


def _compile_keyword(keyword: str) -> re.Pattern[str]:
    """One keyword as a pattern that must align to a dependency-name boundary.

    `/`, `:` and `.` separate the segments of a name, so a keyword matches a whole name,
    a whole segment, or an npm scope: `mongodb` matches `go.mongodb.org/mongo-driver`
    while `next` does not match `next-tick`. A trailing `*` is the one partial form and
    matches a segment prefix, so `google-cloud*` matches `google-cloud-storage`.
    """
    boundary = r"[/:.]"
    if keyword.endswith("*"):
        return re.compile(
            f"(?:^|{boundary})" + re.escape(keyword[:-1]) + f"[a-z0-9+-]*(?:$|{boundary})"
        )
    return re.compile(f"(?:^|{boundary})" + re.escape(keyword) + f"(?:$|{boundary})")


_KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {
    keyword: _compile_keyword(keyword)
    for ecosystems in vocab.CLASS_DEP_KEYWORDS.values()
    for keywords in ecosystems.values()
    for keyword in keywords
}


def match_dep_keywords(dep_names: dict[str, list[str]]) -> dict[str, list[str]]:
    """Which keyword of each group the declared dependencies match.

    Every group is reported, empty included: "no ML dependency" is a measurement, and a
    missing key would be read as an unmeasured one.
    """
    all_names = sorted({name for found in dep_names.values() for name in found})
    hits: dict[str, list[str]] = {}
    for group, by_ecosystem in vocab.CLASS_DEP_KEYWORDS.items():
        found: set[str] = set()
        for ecosystem, keywords in by_ecosystem.items():
            # "any" is the ecosystem-independent group: `redis` means a database whichever
            # language's manifest asked for it.
            candidates = all_names if ecosystem == "any" else dep_names.get(ecosystem, [])
            if not candidates:
                continue
            for keyword in keywords:
                pattern = _KEYWORD_PATTERNS[keyword]
                if any(pattern.search(name) for name in candidates):
                    found.add(keyword.rstrip("*"))
        hits[group] = sorted(found)
    return hits


# ---------------------------------------------------------------------------
# Frameworks
# ---------------------------------------------------------------------------


def analyze_frameworks(root: Path) -> list[str]:
    """The frameworks a repository declares, in detection order and without repeats.

    Three kinds of evidence, all of them positive: a marker file that exists to configure
    one framework, a Python distribution named in a manifest, and an npm package named in
    package.json. Nothing here greps source or prose.
    """
    detected: list[str] = []
    for marker, framework in vocab.FRAMEWORK_MARKERS:
        if (root / marker).exists():
            detected.append(framework)

    pypi = set(collect_dependency_names(root).get("pypi", []))
    for name, framework in vocab.PYPI_DEP_FRAMEWORKS:
        if name in pypi:
            detected.append(framework)

    if (root / "package.json").exists():
        pkg = _read_json(root / "package.json", MANIFEST_HEAD_BYTES)
        all_deps = {**_mapping(pkg.get("dependencies")), **_mapping(pkg.get("devDependencies"))}
        if "react" in all_deps and not any(f in detected for f in vocab.REACT_META_FRAMEWORKS):
            detected.append("React")
        for name, framework in vocab.NPM_DEP_FRAMEWORKS:
            if name in all_deps:
                detected.append(framework)
        # Any mention of tRPC in the dependency block: the client and the server halves
        # are published as separate `@trpc/*` packages, and an aliased or git-sourced
        # install names it in the version rather than in the key.
        if "trpc" in str(all_deps):
            detected.append("tRPC")
        for names, framework in vocab.NPM_DEP_LIBRARIES:
            if any(name in all_deps for name in names):
                detected.append(framework)

    return list(dict.fromkeys(detected))


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _go_require_count(text: str) -> int:
    """Requirements declared in a go.mod, both the block form and the single-line form."""
    in_block = False
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
        elif in_block and line == ")":
            in_block = False
        elif in_block and line and not line.startswith("//"):
            count += 1
        elif line.startswith("require "):
            count += 1
    return count


def _gemfile_lock_spec_count(content: str) -> int:
    """Gems under a `specs:` heading: the ones indented two spaces are the packages, the
    ones indented four are those packages' own constraints and are not counted again."""
    count = 0
    in_specs = False
    for line in content.splitlines():
        if line.strip() == "specs:":
            in_specs = True
        elif in_specs and line.startswith("  ") and not line.startswith("    "):
            count += 1
    return count


def _locked_entry_count(rel_path: str, content: str) -> int:
    """How many packages a lockfile pins, counted in whatever way that format admits.

    Every one of these is a count of entries in the text rather than a resolved graph:
    the question is how much third-party code the checkout installs, and the order of
    magnitude is the whole of the answer.
    """
    if rel_path.endswith("package-lock.json"):
        try:
            data = json.loads(content)
        except ValueError:
            return 0
        if not isinstance(data, dict):
            return 0
        # v2/v3 lockfiles key everything under "packages"; v1 used "dependencies".
        packages = data.get("packages", data.get("dependencies", {}))
        return len(packages) if isinstance(packages, (dict, list)) else 0
    if rel_path.endswith("yarn.lock"):
        return content.count("\n\n")
    if rel_path.endswith("pnpm-lock.yaml"):
        return content.count("\n  /")
    if rel_path.endswith(("poetry.lock", "Cargo.lock")):
        return content.count("[[package]]")
    if rel_path.endswith("go.sum"):
        # Two lines per module: the module and its go.mod.
        return content.count("\n") // 2
    if rel_path.endswith("Gemfile.lock"):
        return _gemfile_lock_spec_count(content)
    return 0


def analyze_dependencies(root: Path) -> dict:
    """What the repository depends on, what locks it, and what keeps it current.

    Direct counts come from the manifests, which is what a team wrote down; the transitive
    count comes from the lockfiles, which is what actually gets installed. `lockfiles_expected`
    is recorded next to `lockfiles_found` so the absence of a lock is legible as an absence
    rather than as a repository that needs none.
    """
    result: dict = {
        "package_managers": [],
        "manifests_found": [],
        "lockfiles_found": [],
        "lockfiles_expected": [],
        "direct_runtime_deps": 0,
        "direct_dev_deps": 0,
        "total_transitive_deps": 0,
        "dep_update_tooling": "none",
    }

    def record(manifest: str) -> None:
        result["manifests_found"].append(manifest)
        result["package_managers"].append(vocab.MANIFEST_TO_PM[manifest])
        result["lockfiles_expected"].append(vocab.LOCKFILE_EXPECTED_BY_MANIFEST[manifest])

    if (root / "package.json").exists():
        record("package.json")
        pkg = _read_json(root / "package.json", MANIFEST_HEAD_BYTES)
        result["direct_runtime_deps"] += len(_mapping(pkg.get("dependencies")))
        result["direct_dev_deps"] += len(_mapping(pkg.get("devDependencies")))

    # Python: the first manifest found stands for the ecosystem. Which of pip, poetry and
    # uv installed it is not knowable from the file, so the family is named instead.
    for manifest in vocab.PYTHON_MANIFESTS:
        if (root / manifest).exists():
            result["manifests_found"].append(manifest)
            result["package_managers"].append(vocab.MANIFEST_TO_PM["pyproject.toml"])
            result["lockfiles_expected"].append(vocab.PYTHON_LOCKFILE_EXPECTED)
            break

    if (root / "go.mod").exists():
        record("go.mod")
        result["direct_runtime_deps"] += _go_require_count(
            _read_head(root / "go.mod", MANIFEST_HEAD_BYTES)
        )

    # Cargo, Bundler and Composer are recorded but not counted: their manifests declare
    # dependencies in tables this does not parse, and a wrong count is worse than none.
    for manifest in ("Cargo.toml", "Gemfile", "composer.json"):
        if (root / manifest).exists():
            record(manifest)

    # One lockfile per format, anywhere in the tree -- a monorepo locks per workspace, and
    # the first one found is enough to say the format is in use. Vendored trees and the
    # git directory are not the repository's own lockfiles.
    for pattern in vocab.LOCKFILE_PATTERNS:
        for candidate in root.rglob(pattern):
            rel = str(candidate.relative_to(root))
            if "node_modules" not in rel and ".git" not in rel:
                result["lockfiles_found"].append(rel)
                break

    for rel in result["lockfiles_found"]:
        lock_path = root / rel
        if not lock_path.exists():
            continue
        result["total_transitive_deps"] += _locked_entry_count(
            rel, _read_head(lock_path, LOCKFILE_BYTES)
        )

    for tool, markers in vocab.DEP_UPDATE_TOOLING:
        if any((root / marker).exists() for marker in markers):
            result["dep_update_tooling"] = tool
            break

    return result


# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------


def analyze_lint_config(root: Path) -> dict:
    """The linters and formatters a repository configures, each with the file that says so.

    Two of the files in the table are shared property -- pyproject.toml and setup.cfg
    belong to whichever tool wrote a section into them -- so those are read for the section
    rather than counted for existing. Everything else exists only to configure one tool.
    """
    detected: dict[str, str] = {}
    for filename, tool in vocab.LINT_CONFIGS.items():
        if not (root / filename).exists():
            continue
        if filename == "pyproject.toml":
            content = _read_head(root / filename, MANIFEST_HEAD_BYTES)
            for linter in vocab.PYPROJECT_LINTERS:
                if f"[tool.{linter}]" in content:
                    detected[linter] = filename
        elif filename == "setup.cfg":
            if "[flake8]" in _read_head(root / filename, MANIFEST_HEAD_BYTES):
                detected["flake8"] = filename
        else:
            detected[tool] = filename
    return {"linters_and_formatters": detected, "has_lint_config": len(detected) > 0}


# ---------------------------------------------------------------------------
# Test framework and coverage
# ---------------------------------------------------------------------------


def analyze_test_framework(root: Path) -> dict:
    """Which runners a repository is set up to run its tests with, and what measures them.

    A runner is named from its configuration, not from a dependency: `jest` in
    devDependencies with no config and no suite is an intention, while jest.config.js is
    a decision. The Go and Rust entries have no config file to find because their runners
    are part of the toolchain -- the manifest is the evidence there.
    """
    frameworks: list[str] = []
    config_files: list[str] = []
    coverage_tooling = None
    coverage_threshold = None

    for config in vocab.JS_TEST_CONFIG_FILES:
        if not (root / config).exists():
            continue
        config_files.append(config)
        for token, framework in vocab.JS_TEST_CONFIG_FRAMEWORKS:
            if token in config:
                frameworks.append(framework)
                break

    for config in vocab.PYTEST_CONFIG_FILES:
        if not (root / config).exists():
            continue
        content = _read_head(root / config, MANIFEST_HEAD_BYTES)
        if any(marker in content for marker in vocab.PYTEST_CONFIG_MARKERS):
            if "pytest" not in frameworks:
                frameworks.append("pytest")
                config_files.append(config)

    if (root / "go.mod").exists():
        frameworks.append("go test")
    if (root / "Cargo.toml").exists():
        frameworks.append("cargo test")
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        frameworks.append("RSpec")
    if (root / "test").is_dir() and (root / "Gemfile").exists():
        frameworks.append("Minitest")
    if (root / "phpunit.xml").exists() or (root / "phpunit.xml.dist").exists():
        frameworks.append("PHPUnit")

    pkg_content = _read_head(root / "package.json", MANIFEST_HEAD_BYTES)
    for probes, tooling in vocab.JS_COVERAGE_PROBES:
        if any(probe in pkg_content for probe in probes):
            coverage_tooling = tooling
            break

    for config in vocab.COVERAGE_CONFIG_FILES:
        content = _read_head(root / config, MANIFEST_HEAD_BYTES)
        if "coverage" not in content:
            continue
        if not coverage_tooling:
            coverage_tooling = "configured in " + config
        # The line threshold is the one number worth lifting out of a runner config: it
        # is the floor the repository set for itself, whoever enforces it.
        match = re.search(r"threshold.*?lines.*?:.*?(\d+)", content, re.S)
        if match:
            coverage_threshold = int(match.group(1))

    pyproject = _read_head(root / "pyproject.toml", MANIFEST_HEAD_BYTES)
    if "coverage" in pyproject or "pytest-cov" in pyproject:
        coverage_tooling = coverage_tooling or "pytest-cov"

    return {
        "frameworks": list(dict.fromkeys(frameworks)),
        "config_files": config_files,
        "coverage_tooling": coverage_tooling,
        "coverage_threshold": coverage_threshold,
    }


# ---------------------------------------------------------------------------
# Project type
# ---------------------------------------------------------------------------


def infer_project_type(root: Path, frameworks: list[str]) -> str:
    """What kind of thing the repository builds, from its manifest and its frameworks.

    One word, and "unknown" is a real answer: a repository with no framework and no entry
    point has not said what it is, and naming it anyway would be a guess dressed as a
    measurement.
    """
    if (root / "package.json").exists():
        pkg = _read_json(root / "package.json", MANIFEST_HEAD_BYTES)
        # A package with an entry point that is not marked private is published for other
        # code to import -- unless a meta-framework owns it, in which case the entry point
        # is the framework's and the repository is an application.
        is_lib = (
            pkg.get("private") is not True
            and pkg.get("main")
            and not any(f in frameworks for f in vocab.APP_FRAMEWORKS)
        )
        if is_lib:
            return "library"
    if any(f in frameworks for f in vocab.WEB_APP_FRAMEWORKS):
        return "web app"
    if any(f in frameworks for f in vocab.API_SERVICE_FRAMEWORKS):
        return "API service"
    if "Flutter/Dart" in frameworks:
        return "mobile"
    if any(f in frameworks for f in vocab.INFRASTRUCTURE_FRAMEWORKS):
        return "infrastructure"
    if any(f in frameworks for f in vocab.BINARY_OR_LIBRARY_FRAMEWORKS):
        # A Go or Rust toolchain builds either a binary or a library; the layout says which.
        if (root / "main.go").exists() or (root / "cmd").is_dir():
            return "CLI / service"
        if (root / "src" / "main.rs").exists() or (root / "src" / "lib.rs").exists():
            main_rs = _read_head(root / "src" / "main.rs", MANIFEST_HEAD_BYTES)
            return "CLI" if "fn main()" in main_rs else "library"
    return "unknown"


# ---------------------------------------------------------------------------
# Continuous integration
# ---------------------------------------------------------------------------

#: How far a CI line is followed into the repository's own files. `make ci`, `npm run
#: test:unit` and `bash scripts/test.sh` say nothing on their own; the Makefile, the
#: package.json and the script say what they do, and reading them is an observation where
#: guessing would not be. Two levels covers the shapes that occur (`make ci` -> `pytest`;
#: `bash scripts/test-cov.sh` -> `bash scripts/test.sh` -> `pytest`) and terminates
#: without needing a cycle check.
CI_INDIRECTION_DEPTH = 2

#: How much of a Makefile is read when a target is looked up, and how much of a shell
#: script is read when a CI line delegates to one.
MAKEFILE_BYTES = 262_144
CI_SCRIPT_BYTES = 131_072

#: `make -j4 VAR=1 ci` -> the target `ci`.
_MAKE_CALL = re.compile(r"^make(?:\s+-\S+|\s+\S+=\S*)*\s+([A-Za-z0-9_./-]+)", re.I)
#: `npm run test:unit`, `yarn test`, `pnpm run-script lint` -> the script name.
_NPM_SCRIPT_CALL = re.compile(
    r"^(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run(?:-script)?\s+)?"
    r"([A-Za-z0-9:_-]+)",
    re.I,
)
#: `bash scripts/test.sh`, `./ci.sh` -> the script's path.
_SCRIPT_CALL = re.compile(
    r"^(?:(?:ba|z|k|da)?sh\s+(?:-\S+\s+)*)?"
    r"(\.?/?[A-Za-z0-9_./-]+\.(?:sh|bash))\b"
)
#: The `${{ ... }}` form GitHub Actions uses to splice a value into a command.
_GHA_EXPR = re.compile(r"\$\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
#: A Jenkinsfile is Groovy rather than YAML, and its shell steps are `sh 'cmd'`,
#: `sh "cmd"` or `sh '''...'''`.
_JENKINS_SH = re.compile(
    r"\b(?:sh|bat|powershell)\s*(?:\(\s*)?"
    r"(?:'''(.*?)'''|\"\"\"(.*?)\"\"\"|'([^']*)'|\"([^\"]*)\")",
    re.S,
)


def _make_recipe(root: Path, target: str) -> list[str]:
    """Return the commands one Makefile target runs, taken from the Makefile in the tree."""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = _read_head(root / name, MAKEFILE_BYTES)
        if not text:
            continue
        lines: list[str] = []
        collecting = False
        for line in text.splitlines():
            # `target:` but not `target :=`, which is a variable rather than a rule.
            if re.match(rf"^{re.escape(target)}\s*:(?!=)", line):
                collecting = True
                continue
            if collecting:
                if line.startswith(("\t", " " * 4)):
                    lines.append(line.strip().lstrip("@-+").strip())
                elif line.strip() and not line.startswith("#"):
                    break
        if lines:
            return lines
    return []


def _npm_script_body(root: Path, script: str) -> list[str]:
    """Return what a named `package.json` script actually contains.

    Without this, `npm run test` would have to be taken at its word; with it, the script is
    read and judged on what it runs.
    """
    scripts = _mapping(_read_json(root / "package.json", MANIFEST_HEAD_BYTES).get("scripts"))
    body = scripts.get(script)
    return [body] if isinstance(body, str) else []


def _ci_indirection(root: Path, command: str) -> list[str]:
    """Follow a command that hands its work to something else in the tree, if it does."""
    match = _MAKE_CALL.match(command)
    if match:
        return _make_recipe(root, match.group(1))
    match = _NPM_SCRIPT_CALL.match(command)
    if match:
        return _npm_script_body(root, match.group(1))
    match = _SCRIPT_CALL.match(command)
    if match:
        # Inside the repository or nowhere: a CI line naming a path outside the checkout is
        # not something this tool opens.
        candidate = (root / match.group(1).lstrip("./")).resolve()
        if root.resolve() in candidate.parents and candidate.is_file():
            return _read_head(candidate, CI_SCRIPT_BYTES).splitlines()
    return []


def _expand_indirections(root: Path, commands: list[str], depth: int) -> list[str]:
    """Replace `make ci` / `npm run test` / `bash scripts/test.sh` with what they run.

    The body *replaces* the surface command whenever it settles the question, so an `npm
    test` script whose body is `eslint .` is a lint step and not a test step. When the body
    settles nothing -- a recipe of `$(PYTHON) -m pytest`, where the runner hides behind a
    variable -- the original command stays in the list too, since discarding it would
    convert a correct detection into a missed one.
    """
    if depth <= 0:
        return commands
    expanded: list[str] = []
    for command in commands:
        body = _expand_indirections(
            root, _split_commands(_ci_indirection(root, command)), depth - 1
        )
        if body and any(
            pattern.match(line)
            for line in body
            for pattern in (
                vocab.CI_TEST_COMMAND,
                vocab.CI_LINT_COMMAND,
                vocab.CI_TYPECHECK_COMMAND,
            )
        ):
            expanded.extend(body)
        else:
            expanded.append(command)
            expanded.extend(body)
    return expanded


def _ci_disabled(guard) -> bool:
    """Does this guard switch the step off? Only a literal can be judged without a run."""
    if guard is False:
        return True
    return isinstance(guard, str) and guard.strip().lower() in vocab.CI_DISABLED


def _command_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _command_strings(entry)]
    if isinstance(value, dict):
        # The long spelling CircleCI accepts, where `run` is a mapping with a `command`.
        return _command_strings(value.get("command"))
    return []


def _yaml_env(node, found: dict[str, str]) -> None:
    """Collect every `env:` mapping in the document into one flat dictionary.

    Where a workflow runs `${{ env.CARGO }} test`, the value of CARGO was declared a little
    further up the same file, and picking it up is simply reading what is written. Skipping
    it is how a project that expresses its whole test matrix through variables ends up
    looking like a project with no tests at all.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                found.update(
                    {
                        str(k): str(v)
                        for k, v in value.items()
                        if isinstance(v, (str, int, float, bool))
                    }
                )
            else:
                _yaml_env(value, found)
    elif isinstance(node, list):
        for item in node:
            _yaml_env(item, found)


def _substitute(command: str, env: dict[str, str]) -> str:
    """Substitute values the document itself declared, and erase references it did not."""

    def replace(match: re.Match) -> str:
        key = match.group(1)
        name = key.split(".", 1)[1] if key.lower().startswith("env.") else key
        return env.get(name, "")

    return _GHA_EXPR.sub(replace, command)


def _yaml_steps(node, commands: list[str], actions: list[str]) -> None:
    """Walk a parsed CI document for every command it runs and every action it calls.

    A subtree whose own `if:` or `when:` is a constant false is skipped: a step guarded by
    `if: false` does not run, and counting it is how a switched-off suite reads as a live
    one.
    """
    if isinstance(node, dict):
        if _ci_disabled(node.get("if")) or _ci_disabled(node.get("when")):
            return
        for key, value in node.items():
            if not isinstance(key, str):
                _yaml_steps(value, commands, actions)
            elif key.lower() in vocab.CI_COMMAND_KEYS:
                commands.extend(_command_strings(value))
            elif key.lower() == "uses" and isinstance(value, str):
                actions.append(value)
            else:
                _yaml_steps(value, commands, actions)
    elif isinstance(node, list):
        for item in node:
            _yaml_steps(item, commands, actions)


def _split_commands(blocks: list[str], env: dict[str, str] | None = None) -> list[str]:
    """One shell line per entry, with the wrapper prefixes stripped off the front."""
    lines: list[str] = []
    for block in blocks:
        for raw in re.split(r"[\n;]|&&|\|\|", block):
            line = _substitute(raw, env).strip() if env else raw.strip()
            line = vocab.CI_WRAPPER.sub("", line, count=1).strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _ci_steps(rel_path: str, content: str) -> tuple[list[str], list[str]] | None:
    """(commands, actions) for one CI file, or None when the file could not be read."""
    blocks: list[str] = []
    actions: list[str] = []
    env: dict[str, str] = {}
    if rel_path.split("/")[-1].lower().startswith("jenkinsfile"):
        blocks = [
            "".join(group for group in match.groups() if group)
            for match in _JENKINS_SH.finditer(content)
        ]
    elif yaml is None:
        return None
    else:
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError:
            return None
        if document is not None:
            _yaml_steps(document, blocks, actions)
            _yaml_env(document, env)
    return _split_commands(blocks, env), actions


def _find_ci_configs(root: Path) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Locate the CI configuration in a tree.

    Returns the systems recognised, the relative paths of their configuration files, and
    those files paired with their contents. A path is only ever recorded once, however many
    routes lead to it.
    """
    systems: list[str] = []
    paths: list[str] = []
    contents: list[tuple[str, str]] = []

    def remember(path: Path) -> None:
        relative = str(path.relative_to(root))
        if relative not in paths:
            paths.append(relative)
            contents.append((relative, _read_head(path, MANIFEST_HEAD_BYTES)))

    for candidate, system in vocab.CI_CONFIGS:
        entry = root / candidate
        # Some entries in that table are directories -- `.github/workflows` among them --
        # and a directory must not be opened as though it were a configuration file. The
        # workflow directory is handled on its own terms just below.
        if entry.is_file():
            systems.append(system)
            remember(entry)

    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        if "GitHub Actions" not in systems:
            systems.append("GitHub Actions")
        for workflow in sorted(list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))):
            remember(workflow)

    return systems, paths, contents


def analyze_ci(root: Path) -> dict:
    """Which CI systems a repository configures, and what their pipelines actually run.

    The three `runs_*` answers come from walking the parsed configuration, never from
    searching its text, and any of them may be None. Failing to parse a file tells us
    nothing about whether its pipeline runs a suite, and `ci_analysis_method` records which
    of the two situations produced the answer.
    """
    ci_systems, ci_configs, configs = _find_ci_configs(root)

    commands: list[str] = []
    actions: list[str] = []
    parsed = unreadable = 0
    for relative, content in configs:
        steps = _ci_steps(relative, content)
        if steps is None:
            unreadable += 1
            continue
        parsed += 1
        found_commands, found_actions = steps
        commands.extend(found_commands)
        actions.extend(found_actions)

    commands = _expand_indirections(root, commands, CI_INDIRECTION_DEPTH)
    # A command that names the suite and then passes a flag turning it off has not run it,
    # whatever the step happens to be called.
    actually_run = [command for command in commands if not vocab.CI_TESTS_SKIPPED.search(command)]

    def runs(
        pattern: re.Pattern[str],
        uses: re.Pattern[str] | None = None,
        lines: list[str] | None = None,
    ) -> bool | None:
        # When every configuration defeated the parser there is nothing to conclude from,
        # and silence about a pipeline is not proof that the pipeline is idle. Say so with
        # None instead of answering False.
        if not parsed and unreadable:
            return None
        return any(
            pattern.match(command) for command in (commands if lines is None else lines)
        ) or bool(uses and any(uses.match(action) for action in actions))

    return {
        "ci_systems": list(dict.fromkeys(ci_systems)),
        "ci_configs": ci_configs,
        "runs_tests": runs(vocab.CI_TEST_COMMAND, lines=actually_run),
        "runs_lint": runs(vocab.CI_LINT_COMMAND, vocab.CI_USES_LINT),
        "runs_typecheck": runs(vocab.CI_TYPECHECK_COMMAND, vocab.CI_USES_TYPECHECK),
        "has_deploy_pipeline": any(vocab.CI_DEPLOY_TEXT.search(text) for _, text in configs),
        "ci_present": len(ci_systems) > 0,
        "ci_analysis_method": (
            "no_ci" if not configs else "parsed" if parsed else "parser_unavailable"
        ),
    }


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------

#: How many source files are considered, and how many of those are opened. Both bounds,
#: not samples with meaning: the validation libraries below are declared at the top of a
#: file that uses them, so a head of the first hundred source files answers the question.
HYGIENE_SCAN_LIMIT = 500
HYGIENE_READ_LIMIT = 100

#: How much of a source file is read when it is looked at for a library name.
SOURCE_SNIFF_BYTES = 4096


def analyze_hygiene(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    """Two hygiene questions: is the dependency tree audited, and is input validated?

    Neither answer requires a credential, and no `.env` file is opened.

    There is no secret scanner here and there never was one: this module holds no
    credential pattern, never searches for a secret-shaped string and never opens a `.env`
    file. `hardcoded_secret_hits` and `secret_hit_details` are kept in the result so the
    emitted block is unchanged, and are permanently null and empty -- None means "not
    collected by policy" and never "measured zero".

    What is left is hygiene that names nothing private: whether CI audits the dependency
    tree, and which validation *libraries* -- public package names, not symbols read out
    of the repository -- the source declares.
    """
    dep_audit_in_ci = False
    input_validation_patterns: list[str] = []

    for config_path, _system in vocab.CI_CONFIGS:
        full = root / config_path
        if full.exists():
            # A directory reads as empty rather than raising: `.github/workflows` is one of
            # these entries and its files are read below.
            if vocab.DEP_AUDIT_TEXT.search(_read_head(full, MANIFEST_HEAD_BYTES)):
                dep_audit_in_ci = True
                break

    gha_dir = root / ".github" / "workflows"
    if gha_dir.is_dir():
        for workflow in list(gha_dir.glob("*.yml")) + list(gha_dir.glob("*.yaml")):
            if vocab.DEP_AUDIT_TEXT.search(_read_head(workflow, MANIFEST_HEAD_BYTES)):
                dep_audit_in_ci = True

    scan_files = sorted(
        (
            (abs_path, rel_path)
            for abs_path, rel_path in source_files
            if abs_path.suffix.lower() in vocab.CODE_EXTENSIONS
            and not is_test_file(rel_path)
            and ".env" not in rel_path
        ),
        key=lambda pair: pair[1],
    )[:HYGIENE_SCAN_LIMIT]

    combined = "\n".join(
        _read_head(abs_path, SOURCE_SNIFF_BYTES)
        for abs_path, _rel in scan_files[:HYGIENE_READ_LIMIT]
    )
    for probe, name in vocab.VALIDATION_PROBES:
        if probe.search(combined):
            input_validation_patterns.append(name)

    return {
        # Retained keys, permanently not collected. None is not 0.
        "hardcoded_secret_hits": None,
        "secret_hit_details": [],
        "env_files_committed": [],
        "dep_audit_in_ci": dep_audit_in_ci,
        "input_validation_patterns": input_validation_patterns,
    }


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def analyze_documentation(root: Path) -> dict:
    """The documents a repository keeps about itself, and what its README covers.

    The sections are our topics rather than the repository's headings: what is recorded is
    the word we searched for, never the line we found it on, so no prose leaves here.
    """
    readme = next((name for name in vocab.README_FILENAMES if (root / name).exists()), None)

    readme_sections: list[str] = []
    readme_loc = 0
    if readme:
        content = _read_head(root / readme, MANIFEST_HEAD_BYTES)
        readme_loc = len([line for line in content.splitlines() if line.strip()])
        readme_sections = [
            topic for topic in vocab.README_SECTION_TOPICS if re.search(topic, content, re.I)
        ]

    return {
        "readme": readme,
        "readme_loc": readme_loc,
        "readme_sections_detected": readme_sections,
        "changelog": next(
            (name for name in vocab.CHANGELOG_FILENAMES if (root / name).exists()), None
        ),
        "contributing_guide": next(
            (name for name in vocab.CONTRIBUTING_FILENAMES if (root / name).exists()), None
        ),
        "has_pr_template": (
            (root / ".github" / "PULL_REQUEST_TEMPLATE.md").exists()
            or (root / ".github" / "pull_request_template.md").exists()
        ),
        "has_issue_template": (
            (root / ".github" / "ISSUE_TEMPLATE").is_dir()
            or (root / ".github" / "issue_template.md").exists()
        ),
    }


# ---------------------------------------------------------------------------
# Demonstrations, templates and exercise copies
# ---------------------------------------------------------------------------


def analyze_demo_signals(root: Path, docs: dict) -> dict:
    """Static signals that a checkout is a demonstration, a template or an exercise copy
    rather than original work.

    The two name signals are None rather than False, and this function takes no name: the
    only name available here is the directory the operator cloned into, which is ours and
    not the repository's. Reading it made every checkout under `src/` a demonstration and
    turned ordinary product names -- a "poc" prefix, a "sample" in the middle -- into
    conclusive verdicts. So the verdict rests on content: a README that describes itself as
    a scaffold, or a well-known demonstration application named in it.
    """
    readme = docs.get("readme")
    readme_text = _read_head(root / readme, SOURCE_SNIFF_BYTES) if readme else ""

    known_demo = bool(vocab.KNOWN_DEMO_PATTERN.search(readme_text))
    template_readme = bool(vocab.TEMPLATE_README_PATTERN.search(readme_text))
    boilerplate_readme = (not readme) or (docs.get("readme_loc", 0) < vocab.BOILERPLATE_README_LOC)

    # The default fingerprints a scaffolding tool leaves behind.
    def exists(*parts: str) -> bool:
        return (root / Path(*parts)).exists()

    scaffold = (
        ((exists("src", "App.js") or exists("src", "App.tsx")) and exists("src", "logo.svg"))
        or (
            exists("src", "App.vue")
            and exists("public", "favicon.ico")
            and not exists("src", "router")
        )
        or (exists("pages", "index.js") and exists("pages", "api", "hello.js"))
    )

    return {
        "name_lexicon_hit": None,
        "strong_name_hit": None,
        "known_demo_app": known_demo,
        "template_readme": template_readme,
        "scaffold_fingerprint": bool(scaffold),
        "boilerplate_readme": bool(boilerplate_readme),
        # Conclusive on its own, and both halves are content: a named demonstration
        # application, or a README that says it is a template.
        "authoritative_demo": bool(template_readme or known_demo),
        "static_name_family": known_demo,
        "static_content_family": template_readme or bool(scaffold),
    }


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def analyze_reproducibility(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    """Whether a stranger could build and run this checkout from what it ships.

    The *names* of environment variables stay uncollected. Such a name comes out of the
    repository and very often spells a product, a vendor or a service; nothing in this tool
    would use it for anything; and combing through source code to harvest a list of them is
    the sort of thing a supplier ought to decline to do. All three keys remain in the
    result, empty, so the block this emits keeps its shape -- and that is also why
    `source_files` is taken as an argument and then never looked at.
    """
    return {
        "has_dockerfile": (
            (root / "Dockerfile").exists()
            or any(
                (root / f"Dockerfile.{suffix}").exists() for suffix in vocab.DEV_DOCKERFILE_SUFFIXES
            )
        ),
        "has_docker_compose": (
            (root / "docker-compose.yml").exists() or (root / "docker-compose.yaml").exists()
        ),
        "has_devcontainer": (
            (root / ".devcontainer").is_dir() or (root / "devcontainer.json").exists()
        ),
        "has_nix": (root / "flake.nix").exists() or (root / "shell.nix").exists(),
        # The filename only. The file itself is never opened.
        "env_example_file": next(
            (name for name in vocab.ENV_EXAMPLE_FILENAMES if (root / name).exists()), None
        ),
        "env_vars_referenced_in_source": [],
        "env_vars_in_example": [],
        "env_vars_missing_from_example": [],
    }


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

#: How many source files are read looking for a health route or a metrics exporter. A
#: bound rather than a sample: a service that exposes either declares it early and often.
OBSERVABILITY_SCAN_LIMIT = 200


def analyze_observability(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    """What this repository would tell an operator at runtime: how it logs, where its
    errors go, and whether it answers a health check or exports a metric.

    One logging library is reported. The ecosystems are read in order and a later one
    overrules an earlier one, because a repository with a go.mod logs from Go whatever
    its package.json also happens to carry.
    """
    logging_framework = None
    error_tracking = None
    has_health_endpoint = False
    has_metrics = False

    package_json = _read_head(root / "package.json", MANIFEST_HEAD_BYTES)
    for lib in vocab.NPM_LOGGING_LIBS:
        if lib in package_json:
            logging_framework = lib
            break

    python_manifests = _read_head(root / "requirements.txt", MANIFEST_HEAD_BYTES) + _read_head(
        root / "pyproject.toml", MANIFEST_HEAD_BYTES
    )
    for lib in vocab.PYPI_LOGGING_LIBS:
        if lib in python_manifests:
            logging_framework = lib
            break

    for lib in vocab.ERROR_TRACKING_LIBS:
        if lib in package_json or lib.replace("-", "_") in python_manifests:
            error_tracking = lib
            break

    scanned = 0
    for abs_path, rel_path in source_files:
        if abs_path.suffix.lower() not in vocab.CODE_EXTENSIONS or is_test_file(rel_path):
            continue
        content = _read_head(abs_path, SOURCE_SNIFF_BYTES)
        if vocab.HEALTH_ENDPOINT_TEXT.search(content):
            has_health_endpoint = True
        if vocab.METRICS_TEXT.search(content):
            has_metrics = True
        scanned += 1
        if scanned > OBSERVABILITY_SCAN_LIMIT:
            break

    go_mod = _read_head(root / "go.mod", MANIFEST_HEAD_BYTES)
    for lib in vocab.GO_LOGGING_LIBS:
        if lib in go_mod:
            logging_framework = lib
            break

    return {
        "logging_framework": logging_framework,
        "error_tracking": error_tracking,
        "has_health_endpoint": has_health_endpoint,
        "has_metrics": has_metrics,
    }


# ---------------------------------------------------------------------------
# Class signals
# ---------------------------------------------------------------------------

#: The infrastructure measurements that overrule whatever the file-shaped signals below
#: found: `analyze_iac` counted lines and identified each file positively, which is a
#: better answer than "a .tf exists" everywhere the two disagree.
_IAC_CLASS_SIGNALS = (
    "terraform_file_count",
    "terraform_loc",
    "k8s_manifest_count",
    "k8s_loc",
    "docker_compose_file_count",
    "docker_compose_loc",
    "helm_file_count",
    "helm_loc",
    "ansible_file_count",
    "ansible_loc",
    "cloudformation_file_count",
    "cloudformation_loc",
    "dockerfile_loc",
    "iac_loc",
    "iac_file_count",
    "iac_loc_by_type",
)


def analyze_class_signals(
    root: Path,
    all_files: list[tuple[Path, str]],
    languages: dict,
    iac: dict,
    dep_names: dict[str, list[str]],
) -> dict:
    """The raw signals the classifier turns into per-class confidences.

    Counting and keyword matching only: nothing is scored here and no class is decided.
    """
    keyword_hits = match_dep_keywords(dep_names)

    notebook_count = 0
    tf_count = 0
    dockerfile_count = 0
    sql_file_count = 0
    data_file_count = 0
    ui_component_count = 0
    css_loc = 0

    for abs_path, _rel_path in all_files:
        name = abs_path.name.lower()
        ext = abs_path.suffix.lower()
        if ext == ".ipynb":
            notebook_count += 1
        elif ext in vocab.TERRAFORM_EXTENSIONS:
            tf_count += 1
        elif ext in vocab.UI_COMPONENT_EXTENSIONS:
            ui_component_count += 1
        elif ext == ".sql":
            sql_file_count += 1
        elif ext in vocab.CSS_EXTENSIONS:
            # Counted here because CSS is not a code extension, so its lines appear in no
            # other tally.
            css_loc += count_lines(abs_path)
        elif ext in vocab.DATA_FILE_EXTENSIONS:
            data_file_count += 1
        if name == "dockerfile" or name.startswith("dockerfile."):
            dockerfile_count += 1

    infra_hits = set(keyword_hits.get("infra_libs", []))
    pulumi_present = (root / "Pulumi.yaml").exists() or bool(infra_hits & {"pulumi", "@pulumi"})
    ansible_present = (
        (root / "ansible.cfg").exists()
        or (root / "playbook.yml").exists()
        or (root / "playbooks").is_dir()
        or bool(infra_hits & {"ansible", "ansible-core"})
    )
    # The marker files that name Helm and Terraform in `analyze_frameworks`, checked here
    # directly rather than by taking the framework list apart again.
    helm_present = (root / "Chart.yaml").exists() or (root / "helm" / "Chart.yaml").exists()
    terraform_present = (
        tf_count > 0 or (root / "main.tf").exists() or (root / "terraform.tfstate").exists()
    )

    loc_by_language = languages["loc_by_language"]
    # SQL comes from the language tally because .sql IS a counted code extension; CSS
    # cannot, which is why it was counted by hand above.
    sql_loc = loc_by_language.get("SQL", 0)
    total_loc = sum(loc_by_language.values()) or 1

    signals = {
        "dep_keyword_hits": keyword_hits,
        "notebook_count": notebook_count,
        "terraform_file_count": tf_count,
        "terraform_present": terraform_present,
        # Named here so the key keeps its place among the infrastructure signals; the
        # count itself comes from `analyze_iac`, which identified each manifest by its
        # `kind:` rather than by the two words apiVersion and kind appearing in a file.
        "k8s_manifest_count": 0,
        "helm_present": helm_present,
        "pulumi_present": pulumi_present,
        "ansible_present": ansible_present,
        "dockerfile_count": dockerfile_count,
        "ui_component_file_count": ui_component_count,
        "sql_file_count": sql_file_count,
        "sql_loc": sql_loc,
        "css_loc": css_loc,
        # The share of all counted lines, code and CSS together, that is CSS. Bounded 0..1.
        "css_loc_ratio": round(css_loc / (total_loc + css_loc), 4),
        "data_file_count": data_file_count,
    }

    for key in _IAC_CLASS_SIGNALS:
        signals[key] = iac[key]
    # Presence stays true when either detector fired: the file-based one above, or the
    # line-counting one in `analyze_iac`.
    signals["terraform_present"] = terraform_present or iac["terraform_present"]
    signals["helm_present"] = helm_present or iac["helm_file_count"] > 0
    signals["ansible_present"] = ansible_present or iac["ansible_file_count"] > 0
    return signals


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _fold_iac_into_languages(languages: dict, iac: dict) -> dict:
    """Fold the infrastructure tallies into the language ones.

    Infrastructure extensions are outside the code extensions, so this never double-counts;
    without it a repository of pure manifests reports zero lines of an Unknown language,
    which is false about a Terraform module and useless to everyone downstream.
    """
    if not iac.get("iac_loc"):
        return languages
    merged_loc = dict(languages["loc_by_language"])
    merged_counts = dict(languages["file_count_by_language"])
    for iac_type, loc in iac["iac_loc_by_type"].items():
        merged_loc[iac_type] = merged_loc.get(iac_type, 0) + loc
        merged_counts[iac_type] = merged_counts.get(iac_type, 0) + iac[
            "iac_file_count_by_type"
        ].get(iac_type, 0)
    ordered = sorted(merged_loc.items(), key=lambda item: item[1], reverse=True)
    return {
        "primary_language": ordered[0][0] if ordered else "Unknown",
        "secondary_languages": [lang for lang, _ in ordered[1:4]],
        "total_loc": sum(merged_loc.values()),
        "loc_by_language": dict(ordered),
        "file_count_by_language": merged_counts,
    }


def collect(repo: Path, top_files: int = 10) -> dict:
    """Everything this module measures about a checkout, as one block.

    One walk feeds every analysis below it. `repo_path` and `repo_name` are the only two
    fields here that name anything of the operator's, and the schema drops both on the way
    out; they are kept at this level so the raw block is comparable field for field.
    """
    root = Path(repo).resolve()
    all_files = walk_source_files(root)

    languages = analyze_languages(all_files)
    iac = analyze_iac(all_files, root)
    # Working state, not an output: the sized infrastructure files go into the size
    # distribution and then the key is gone, so no path reaches the block.
    iac_sized_files = iac.pop("_sized_files", [])
    languages = _fold_iac_into_languages(languages, iac)

    file_sizes = analyze_file_sizes(all_files, top_n=top_files, extra_files=iac_sized_files)
    tests = analyze_tests(all_files)
    frameworks = analyze_frameworks(root)
    dependencies = analyze_dependencies(root)
    ci = analyze_ci(root)
    hygiene = analyze_hygiene(root, all_files)
    lint = analyze_lint_config(root)
    test_fw = analyze_test_framework(root)
    docs = analyze_documentation(root)
    demo_signals = analyze_demo_signals(root, docs)
    repro = analyze_reproducibility(root, all_files)
    observability = analyze_observability(root, all_files)
    class_signals = analyze_class_signals(
        root, all_files, languages, iac, collect_dependency_names(root)
    )
    project_type = infer_project_type(root, frameworks)

    # The denominator is the non-test source, so the tests are not counted on the source
    # side as well -- which would understate how dense they are. "0 tests" only when there
    # are no specs: an all-test repository reports "1:0" rather than contradicting itself.
    spec_count = tests["spec_files"]
    non_test_source = file_sizes["total_non_test_source_files"]
    test_source_ratio = f"1:{round(non_test_source / spec_count)}" if spec_count > 0 else "0 tests"

    return {
        "schema_version": "1.0",
        "repo_path": str(root),
        "repo_name": root.name,
        # Identity
        "primary_language": languages["primary_language"],
        "secondary_languages": languages["secondary_languages"],
        "loc_by_language": languages["loc_by_language"],
        "file_count_by_language": languages["file_count_by_language"],
        "jvm_dotnet_loc_share": jvm_dotnet_loc_share(
            languages["loc_by_language"], languages["total_loc"]
        ),
        "detected_frameworks": frameworks,
        "project_type": project_type,
        # Size
        "total_loc": languages["total_loc"],
        "total_source_files": file_sizes["total_source_files"],
        "median_file_size_loc": file_sizes["median_loc"],
        "p90_file_size_loc": file_sizes["p90_loc"],
        "god_files_over_500_loc": file_sizes["god_files_over_500"],
        "god_files_over_1000_loc": file_sizes["god_files_over_1000"],
        "top_largest_files": file_sizes["largest_files"],
        "generated_files_excluded": file_sizes["generated_excluded"],
        # Infrastructure, folded into the totals above and broken out here so a repository
        # that is all infrastructure is legible rather than empty.
        "iac_loc": iac["iac_loc"],
        "iac_file_count": iac["iac_file_count"],
        "iac_loc_by_type": iac["iac_loc_by_type"],
        # Tests
        "test_spec_files": tests["spec_files"],
        "test_fixture_files": tests["fixture_and_snapshot_files"],
        "test_source_ratio": test_source_ratio,
        "test_spec_sample": tests["spec_file_paths_sample"],
        "test_framework": test_fw["frameworks"],
        "test_config_files": test_fw["config_files"],
        "coverage_tooling": test_fw["coverage_tooling"],
        "coverage_threshold": test_fw["coverage_threshold"],
        # Dependencies
        "package_managers": dependencies["package_managers"],
        "manifests_found": dependencies["manifests_found"],
        "lockfiles_found": dependencies["lockfiles_found"],
        "lockfiles_expected": dependencies["lockfiles_expected"],
        "direct_runtime_deps": dependencies["direct_runtime_deps"],
        "direct_dev_deps": dependencies["direct_dev_deps"],
        "total_transitive_deps": dependencies["total_transitive_deps"],
        "dep_update_tooling": dependencies["dep_update_tooling"],
        # CI
        "ci_systems": ci["ci_systems"],
        "ci_config_files": ci["ci_configs"],
        "ci_runs_tests": ci["runs_tests"],
        "ci_runs_lint": ci["runs_lint"],
        "ci_runs_typecheck": ci["runs_typecheck"],
        "ci_has_deploy": ci["has_deploy_pipeline"],
        "ci_present": ci["ci_present"],
        # How the three flags above were reached, so a null is legible as "the config
        # could not be parsed" rather than as "the pipeline does nothing".
        "ci_analysis_method": ci["ci_analysis_method"],
        # Hygiene. The first three are not collected, by policy.
        "hardcoded_secret_hits": hygiene["hardcoded_secret_hits"],
        "secret_hit_details": hygiene["secret_hit_details"],
        "env_files_committed": hygiene["env_files_committed"],
        "dep_audit_in_ci": hygiene["dep_audit_in_ci"],
        "input_validation_patterns": hygiene["input_validation_patterns"],
        # Linting
        "linters_and_formatters": lint["linters_and_formatters"],
        "has_lint_config": lint["has_lint_config"],
        # Documentation
        "readme": docs["readme"],
        "readme_loc": docs["readme_loc"],
        "readme_sections": docs["readme_sections_detected"],
        "changelog": docs["changelog"],
        "contributing_guide": docs["contributing_guide"],
        "has_pr_template": docs["has_pr_template"],
        "has_issue_template": docs["has_issue_template"],
        "demo_signals": demo_signals,
        # Reproducibility
        "has_dockerfile": repro["has_dockerfile"],
        "has_docker_compose": repro["has_docker_compose"],
        "has_devcontainer": repro["has_devcontainer"],
        "has_nix": repro["has_nix"],
        "env_example_file": repro["env_example_file"],
        "env_vars_referenced_in_source": repro["env_vars_referenced_in_source"],
        "env_vars_in_example": repro["env_vars_in_example"],
        "env_vars_missing_from_example": repro["env_vars_missing_from_example"],
        # Observability
        "logging_framework": observability["logging_framework"],
        "error_tracking": observability["error_tracking"],
        "has_health_endpoint": observability["has_health_endpoint"],
        "has_metrics": observability["has_metrics"],
        # Classification
        "class_signals": class_signals,
    }
