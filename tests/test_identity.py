"""The owning company, and the refusals that keep it honest.

`_cases` is module-level on purpose: it is the smallest tree per signal family, per refusal
and per outcome, and it is the trees that make the assertions below worth anything.
"""

import pytest

from hazina_scan import identity, schema

# ---------------------------------------------------------------------------
# The shape of an answer
# ---------------------------------------------------------------------------


def test_confident_from_licence_and_remote(py_repo):
    """The licence file says "Acme Corp" and the remote says `acme`; `normalise` drops the
    legal suffix so the two agree, and the licence file's spelling is the one shown --
    which is why the name here is "Acme Corp" rather than the remote's bare "Acme"."""
    out = identity.collect(py_repo, "acme/demo")
    assert out["outcome"] == "confident"
    assert out["company_candidates"][0]["company_name"] == "Acme Corp"
    assert set(out["company_candidates"][0]["signal_families"]) == {"licence_file", "git_remote"}


def test_gpl_steward_is_refused(repo_builder):
    repo = repo_builder({"LICENSE": "Copyright (C) 2007 Free Software Foundation, Inc.\nGNU GPL\n"})
    assert identity.collect(repo, None)["outcome"] == "none"


def test_empty_tree_is_none(repo_builder):
    out = identity.collect(repo_builder({"a.py": "x\n"}, name="bare"), None)
    assert out == {
        "outcome": "none",
        "company_candidates": [],
        "industry": None,
        "industry_signals": [],
    }


# ---------------------------------------------------------------------------
# The name gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Acme",
        "Acme Systems, Inc.",
        "The Procter & Gamble Company",
        "Ørsted",
        "Société Générale",
        "O'Reilly Media",
        "Acme-Systems Ltd",
        "Acme 2 Ltd",
    ],
)
def test_emittable_names(text):
    assert identity.is_emittable_name(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "acme.com",
        "com.acme.payments",
        "payments_core.py",
        "jane@acme.com",
        "https:",
        "/home/jane/work/acme",
        "& Co",
        "A" * 121,
        None,
        7,
        "One Two Three Four Five Six Seven Eight Nine",
    ],
)
def test_refused_names(text):
    assert not identity.is_emittable_name(text)


def test_normalise_folds_legal_suffixes():
    assert identity.normalise("Acme Systems, Inc.") == identity.normalise("acme-systems")
    assert identity.normalise("Acme Ltd") == "acme"
    assert identity.normalise("Inc.") == ""


def test_from_remote_needs_an_owner():
    assert identity.from_remote("acme/demo") == ["acme"]
    assert identity.from_remote("group/sub/demo") == ["group"]
    assert identity.from_remote("demo") == []
    assert identity.from_remote(None) == []


# ---------------------------------------------------------------------------
# The trees. One fixture per family, per refusal and per outcome.
# ---------------------------------------------------------------------------


def _cases(repo_builder, py_repo):
    """repo-name -> (tree, repo_full_name). Each entry is the smallest tree that
    exercises one family, one refusal or one outcome."""
    return {
        # the brief's three
        "py": (py_repo, "acme/demo"),
        "hdr": (
            repo_builder(
                {
                    "a.py": "# Copyright 2023 Globex Inc\n",
                    "b.py": "# Copyright 2023 Globex Inc\n",
                    "README.md": "# Ledger\n\ninvoices and payments and billing\n",
                },
                name="hdr",
            ),
            None,
        ),
        "none": (repo_builder({"a.py": "x\n"}, name="none"), None),
        # licence_file: a NOTICE rather than a LICENSE, and a dual-licensed pair
        "notice": (
            repo_builder(
                {
                    "NOTICE": "Initech Systems Software\nCopyright 2019 Initech"
                    " Systems, Inc.  All rights reserved.\n"
                },
                name="notice",
            ),
            None,
        ),
        "dual": (
            repo_builder(
                {
                    "LICENSE-MIT": "Copyright (c) 2022 Hooli Ltd\n",
                    "LICENSE-APACHE": "Copyright (c) 2022 Hooli Limited\n",
                },
                name="dual",
            ),
            None,
        ),
        # licence_file refusals: a steward, and an unfilled template placeholder
        "gpl": (
            repo_builder(
                {
                    "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 3\n\n"
                    "Copyright (C) 2007 Free Software Foundation, Inc."
                    " <https://fsf.org/>\n"
                },
                name="gpl",
            ),
            None,
        ),
        "template": (
            repo_builder(
                {"LICENSE": "MIT License\n\nCopyright (c) <year> <copyright holders>\n"},
                name="template",
            ),
            None,
        ),
        "placeholder": (
            repo_builder({"LICENSE": "Copyright (c) 2024 Your Company Name\n"}, name="placeholder"),
            None,
        ),
        # a copyright line with no entity on it at all
        "yearonly": (repo_builder({"LICENSE": "Copyright (c) 2024\n"}, name="yearonly"), None),
        # Apache-2.0's prose definition, which has "copyright owner or entity ..." in it
        "apacheprose": (
            repo_builder(
                {
                    "LICENSE": '"Legal Entity" shall mean the union of the acting entity.\n'
                    "copyright owner or entity authorized by the copyright owner\n"
                },
                name="apacheprose",
            ),
            None,
        ),
        # copyright_header: one file is not enough, two are
        "onehdr": (
            repo_builder(
                {"a.py": "# Copyright (c) 2021 Umbrella Corp\n", "b.py": "x = 1\n"}, name="onehdr"
            ),
            None,
        ),
        "tiehdr": (
            repo_builder(
                {
                    "a.py": "# Copyright 2021 Umbrella Corp\n",
                    "b.py": "# Copyright 2021 Umbrella Corp\n",
                    "c.py": "# Copyright 2021 Wayne Enterprises\n",
                    "d.py": "# Copyright 2021 Wayne Enterprises\n",
                },
                name="tiehdr",
            ),
            None,
        ),
        # a vendored tree's headers name ITS author and must not be walked into
        "vendored": (
            repo_builder(
                {
                    "node_modules/x/a.js": "// Copyright 2020 Tyrell Corp\n",
                    "node_modules/x/b.js": "// Copyright 2020 Tyrell Corp\n",
                    "src/main.js": "// Copyright 2020 Soylent Ltd\n",
                    "src/util.js": "// Copyright 2020 Soylent Ltd\n",
                },
                name="vendored",
            ),
            None,
        ),
        # a C-style header closes its comment on the same line
        "cstyle": (
            repo_builder(
                {
                    "a.c": "/* Copyright (c) 2020 Cyberdyne Systems */\n",
                    "b.c": "/* Copyright (c) 2020 Cyberdyne Systems */\n",
                },
                name="cstyle",
            ),
            None,
        ),
        # package_manifest: an npm scope
        "npm": (
            repo_builder(
                {"package.json": '{"name": "@vandelay/widgets", "version": "1.0.0"}\n'}, name="npm"
            ),
            None,
        ),
        # package_manifest: a marketplace publisher
        "publisher": (
            repo_builder(
                {"package.json": '{"name": "ext", "publisher": "Stark Industries"}\n'},
                name="publisher",
            ),
            None,
        ),
        # package_manifest: a Composer vendor
        "composer": (
            repo_builder(
                {"composer.json": '{"name": "oceanic/airframe", "require": {"php": "^8.2"}}\n'},
                name="composer",
            ),
            None,
        ),
        # package_manifest: a Go module path with a host in front of the organisation
        "gomod": (
            repo_builder(
                {"go.mod": "module github.com/massivedynamic/relay\n\ngo 1.22\n"}, name="gomod"
            ),
            None,
        ),
        # ... and one with no host, which names nobody
        "gobare": (repo_builder({"go.mod": "module relay\n\ngo 1.22\n"}, name="gobare"), None),
        # package_manifest: a Maven groupId, with a <parent> ahead of it that must not win
        "pom": (
            repo_builder(
                {
                    "pom.xml": "<project>\n"
                    "  <parent><groupId>org.springframework.boot</groupId>"
                    "<artifactId>spring-boot-starter-parent</artifactId></parent>\n"
                    "  <groupId>com.nakatomi</groupId>\n"
                    "  <artifactId>plaza</artifactId>\n"
                    "  <licenses><license><name>MIT License</name>"
                    "</license></licenses>\n"
                    "</project>\n"
                },
                name="pom",
            ),
            None,
        ),
        # ... and a pom whose <organization><name> is the answer
        "pomorg": (
            repo_builder(
                {
                    "pom.xml": "<project>\n"
                    "  <developers><developer><organization>Old Vendor"
                    "</organization></developer></developers>\n"
                    "  <organization><name>Weyland Yutani</name>"
                    "<url>x</url></organization>\n"
                    "</project>\n"
                },
                name="pomorg",
            ),
            None,
        ),
        # a manifest author field must contribute nothing
        "author": (
            repo_builder(
                {
                    "package.json": '{"name": "plain", "author": "Jane Roe",'
                    ' "maintainers": ["John Doe"]}\n'
                },
                name="author",
            ),
            None,
        ),
        # disagreement: licence + headers name one company, remote + npm scope another
        "fork": (
            repo_builder(
                {
                    "LICENSE": "Copyright (c) 2019 Cyberdyne Systems\n",
                    "a.py": "# Copyright 2019 Cyberdyne Systems\n",
                    "b.py": "# Copyright 2019 Cyberdyne Systems\n",
                    "package.json": '{"name": "@skynet/relay"}\n',
                },
                name="fork",
            ),
            "skynet/relay",
        ),
        # one family only
        "lonely": (
            repo_builder({"LICENSE": "Copyright (c) 2019 Pied Piper, Inc.\n"}, name="lonely"),
            None,
        ),
        # more corroborated names than the display cap
        "crowd": (
            repo_builder(
                {
                    "LICENSE": "Copyright (c) 2019 Aperture Science\n",
                    "a.py": "# Copyright 2019 Aperture Science\n",
                    "b.py": "# Copyright 2019 Aperture Science\n",
                    "package.json": '{"name": "@blackmesa/portal"}\n',
                    "composer.json": '{"name": "combine/gun"}\n',
                    "go.mod": "module github.com/vaultec/core\n",
                    "pom.xml": "<project><groupId>com.umbrella</groupId></project>\n",
                },
                name="crowd",
            ),
            "blackmesa/portal",
        ),
        # industry, read from dependencies rather than the README
        "deps": (
            repo_builder(
                {
                    "package.json": '{"name": "shop", "dependencies":'
                    ' {"stripe": "^1", "plaid": "^2"}}\n'
                },
                name="deps",
            ),
            None,
        ),
        # industry from a pyproject's declared dependencies
        "pydeps": (
            repo_builder(
                {
                    "pyproject.toml": '[project]\nname = "x"\n'
                    'dependencies = ["biopython", "pysam"]\n'
                },
                name="pydeps",
            ),
            None,
        ),
        # both sources agreeing
        "bothsig": (
            repo_builder(
                {
                    "README.md": "# Shop\n\ncheckout and storefront and cart\n",
                    "package.json": '{"name": "s", "dependencies":'
                    ' {"shopify-api": "^1", "medusa": "^2"}}\n',
                },
                name="bothsig",
            ),
            None,
        ),
        # a tie between two industries is no answer
        "tie": (
            repo_builder(
                {
                    "README.md": "# X\n\npatient records and clinical notes;"
                    " also gameplay and matchmaking\n"
                },
                name="tie",
            ),
            None,
        ),
        # one keyword is a coincidence
        "onehit": (repo_builder({"README.md": "# X\n\na ledger of things\n"}, name="onehit"), None),
        # a malformed manifest must cost nothing
        "broken": (
            repo_builder(
                {
                    "package.json": "{not json at all\n",
                    "composer.json": "[]\n",
                    "pyproject.toml": 'tool = "x"\n',
                    "pom.xml": "<project><organization>a string</organization></project>\n",
                },
                name="broken",
            ),
            None,
        ),
        # a one-segment full name names nobody
        "noowner": (repo_builder({"a.py": "x\n"}, name="noowner"), "demo"),
    }


def test_no_number_leaves_the_block(repo_builder, py_repo):
    """The copyright family counts files and the industry guess counts keyword hits. Both
    counts stop at a return statement: a hit count beside a company's name is a confidence
    number, and a confidence number reads as a grade."""

    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from leaves(item)
        else:
            yield value

    for name, (repo, full) in _cases(repo_builder, py_repo).items():
        found = [
            v
            for v in leaves(identity.collect(repo, full))
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        assert found == [], name


def test_scanned_file_cap_is_honoured(repo_builder):
    files = {f"d{i:03d}/f.py": "x\n" for i in range(identity._MAX_FILES_SCANNED + 40)}
    repo = repo_builder(files, name="capped")
    assert len(identity._source_files(repo)) == identity._MAX_FILES_SCANNED


def test_collected_blocks_pass_the_emission_boundary(repo_builder, py_repo):
    """The collector and `schema.py` read the same tables, so a block this module produces
    must survive the boundary untouched. If it does not, one of the two has drifted."""
    for name, (repo, full) in _cases(repo_builder, py_repo).items():
        block = identity.collect(repo, full)
        assert schema._COMPANY_IDENTITY.apply(block, "company_identity") == block, name
