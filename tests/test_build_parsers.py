"""Unit tests for `hazina_scan.build.parsers`, against hand-written runner output.

Every sample below was typed by hand to match the shape each parser actually reads (not
copied from a live run), covering a clean pass, a run with failures, a run with skips, and
an empty or unrecognisable transcript. No toolchain is needed: these are plain strings and,
for `junit_counts`, small XML fixture files written to `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

from hazina_scan.build import parsers

# --- pytest ----------------------------------------------------------------------------


def test_parse_pytest_passing():
    text = (
        "============================= test session starts ==============================\n"
        "collected 12 items\n\n"
        "tests/test_a.py ............                                          [100%]\n\n"
        "============================== 12 passed in 0.34s ===============================\n"
    )
    assert parsers.parse_pytest(text) == {"collected": 12, "passed": 12}


def test_parse_pytest_failing_with_errors():
    text = (
        "collected 6 items\n\n"
        "tests/test_a.py ..FFE.                                                [100%]\n\n"
        "=================================== FAILURES ===================================\n"
        "...\n"
        "===================== 3 passed, 2 failed, 1 error in 0.51s ======================\n"
    )
    assert parsers.parse_pytest(text) == {
        "collected": 6,
        "passed": 3,
        "failed": 2,
        "errored": 1,
    }


def test_parse_pytest_skipped():
    text = "collected 4 items\n\ntests/test_a.py ..ss   [100%]\n\n2 passed, 2 skipped in 0.12s\n"
    assert parsers.parse_pytest(text) == {"collected": 4, "passed": 2, "skipped": 2}


def test_parse_pytest_empty():
    assert parsers.parse_pytest("no tests ran in 0.01s\n") == {"collected": 0}
    assert parsers.parse_pytest("garbage output with no pytest markers at all\n") == {}


# --- jest --------------------------------------------------------------------------------


def test_parse_jest_passing():
    text = (
        "Test Suites: 3 passed, 3 total\n"
        "Tests:       15 passed, 15 total\n"
        "Snapshots:   0 total\n"
        "Time:        1.234s\n"
    )
    assert parsers.parse_jest(text) == {"passed": 15, "collected": 15}


def test_parse_jest_failing():
    text = "Test Suites: 1 failed, 2 passed, 3 total\nTests:       2 failed, 13 passed, 15 total\n"
    assert parsers.parse_jest(text) == {"failed": 2, "passed": 13, "collected": 15}


def test_parse_jest_skipped():
    text = "Tests:       1 skipped, 14 passed, 15 total\n"
    assert parsers.parse_jest(text) == {"skipped": 1, "passed": 14, "collected": 15}


def test_parse_jest_empty():
    assert parsers.parse_jest("No tests found, exiting with code 1\n") == {}


# --- vitest ----------------------------------------------------------------------------


def test_parse_vitest_passing():
    text = " Test Files  3 passed (3)\n      Tests  15 passed (15)\n   Duration  820ms\n"
    assert parsers.parse_vitest(text) == {"passed": 15, "collected": 15}


def test_parse_vitest_failing():
    text = "      Tests  3 failed | 12 passed (15)\n"
    assert parsers.parse_vitest(text) == {"failed": 3, "passed": 12, "collected": 15}


def test_parse_vitest_skipped():
    text = "      Tests  2 skipped | 13 passed (15)\n"
    assert parsers.parse_vitest(text) == {"skipped": 2, "passed": 13, "collected": 15}


def test_parse_vitest_empty():
    assert parsers.parse_vitest("Error: no test files found\n") == {}


# --- mocha -----------------------------------------------------------------------------


def test_parse_mocha_passing():
    assert parsers.parse_mocha("  15 passing (120ms)\n") == {"passed": 15, "collected": 15}


def test_parse_mocha_failing():
    text = "  12 passing (200ms)\n  3 failing\n"
    assert parsers.parse_mocha(text) == {"passed": 12, "failed": 3, "collected": 15}


def test_parse_mocha_skipped():
    text = "  10 passing (90ms)\n  2 pending\n"
    assert parsers.parse_mocha(text) == {"passed": 10, "skipped": 2, "collected": 12}


def test_parse_mocha_empty():
    assert parsers.parse_mocha("nothing here resembling a mocha summary\n") == {}


# --- cargo -----------------------------------------------------------------------------


def test_parse_cargo_passing():
    text = (
        "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.01s\n"
    )
    assert parsers.parse_cargo(text) == {
        "passed": 5,
        "failed": 0,
        "skipped": 0,
        "collected": 5,
    }


def test_parse_cargo_failing():
    text = (
        "test result: FAILED. 3 passed; 2 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.02s\n"
    )
    assert parsers.parse_cargo(text) == {
        "passed": 3,
        "failed": 2,
        "skipped": 0,
        "collected": 5,
    }


def test_parse_cargo_skipped_and_multi_crate():
    text = (
        "test result: ok. 4 passed; 0 failed; 1 ignored; 0 measured; "
        "0 filtered out; finished in 0.02s\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.01s\n"
    )
    assert parsers.parse_cargo(text) == {
        "passed": 6,
        "failed": 0,
        "skipped": 1,
        "collected": 7,
    }


def test_parse_cargo_empty():
    assert parsers.parse_cargo("error: could not compile `demo`\n") == {}


# --- go -json ----------------------------------------------------------------------------


def test_parse_go_json_passing():
    text = (
        '{"Action":"run","Package":"pkg","Test":"TestA"}\n'
        '{"Action":"pass","Package":"pkg","Test":"TestA","Elapsed":0.01}\n'
        '{"Action":"pass","Package":"pkg"}\n'
    )
    assert parsers.parse_go_json(text) == {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
    }


def test_parse_go_json_failing():
    text = (
        '{"Action":"run","Test":"TestB"}\n'
        '{"Action":"fail","Test":"TestB","Elapsed":0.02}\n'
        '{"Action":"fail","Package":"pkg"}\n'
    )
    assert parsers.parse_go_json(text) == {
        "collected": 1,
        "passed": 0,
        "failed": 1,
        "skipped": 0,
    }


def test_parse_go_json_skipped():
    text = '{"Action":"run","Test":"TestC"}\n{"Action":"skip","Test":"TestC"}\n'
    assert parsers.parse_go_json(text) == {
        "collected": 1,
        "passed": 0,
        "failed": 0,
        "skipped": 1,
    }


def test_parse_go_json_empty():
    assert parsers.parse_go_json("not json at all\n{broken\n") == {}


# --- dotnet ----------------------------------------------------------------------------


def test_parse_dotnet_passing():
    text = (
        "Passed!  - Failed:     0, Passed:    10, Skipped:     0, Total:    10, Duration: 610 ms\n"
    )
    assert parsers.parse_dotnet(text) == {
        "failed": 0,
        "passed": 10,
        "skipped": 0,
        "collected": 10,
    }


def test_parse_dotnet_failing():
    text = (
        "Failed!  - Failed:     3, Passed:     7, Skipped:     0, Total:    10, Duration: 700 ms\n"
    )
    assert parsers.parse_dotnet(text) == {
        "failed": 3,
        "passed": 7,
        "skipped": 0,
        "collected": 10,
    }


def test_parse_dotnet_skipped():
    text = (
        "Passed!  - Failed:     0, Passed:     8, Skipped:     2, Total:    10, Duration: 550 ms\n"
    )
    assert parsers.parse_dotnet(text) == {
        "failed": 0,
        "passed": 8,
        "skipped": 2,
        "collected": 10,
    }


def test_parse_dotnet_empty():
    assert parsers.parse_dotnet("Build FAILED.\n") == {}


# --- phpunit ---------------------------------------------------------------------------


def test_parse_phpunit_passing():
    assert parsers.parse_phpunit("OK (10 tests, 25 assertions)\n") == {
        "collected": 10,
        "passed": 10,
        "failed": 0,
    }


def test_parse_phpunit_failing():
    text = "FAILURES!\nTests: 10, Assertions: 25, Failures: 2, Errors: 1.\n"
    assert parsers.parse_phpunit(text) == {
        "collected": 10,
        "failed": 2,
        "errored": 1,
        "passed": 7,
    }


def test_parse_phpunit_skipped():
    text = "OK, but incomplete, skipped, or risky tests!\nTests: 10, Assertions: 20, Skipped: 3.\n"
    assert parsers.parse_phpunit(text) == {"collected": 10, "skipped": 3, "passed": 7}


def test_parse_phpunit_empty():
    assert parsers.parse_phpunit("PHP Fatal error:  Uncaught Error\n") == {}


# --- rspec -----------------------------------------------------------------------------


def test_parse_rspec_passing():
    assert parsers.parse_rspec("10 examples, 0 failures\n") == {
        "collected": 10,
        "failed": 0,
        "skipped": 0,
        "passed": 10,
    }


def test_parse_rspec_failing():
    assert parsers.parse_rspec("10 examples, 3 failures\n") == {
        "collected": 10,
        "failed": 3,
        "skipped": 0,
        "passed": 7,
    }


def test_parse_rspec_skipped():
    text = "10 examples, 0 failures, 2 pending\n"
    assert parsers.parse_rspec(text) == {
        "collected": 10,
        "failed": 0,
        "skipped": 2,
        "passed": 8,
    }


def test_parse_rspec_empty():
    assert parsers.parse_rspec("An error occurred while loading ./spec/foo_spec.rb.\n") == {}


# --- junit_counts ------------------------------------------------------------------------

NESTED_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="A" tests="5" failures="1" errors="0" skipped="1"></testsuite>
  <testsuite name="B" tests="3" failures="0" errors="1" skipped="0"></testsuite>
</testsuites>
"""

FLAT_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="C" tests="4" failures="2" errors="0" skipped="0"></testsuite>
"""


def test_junit_counts_nested_testsuites(tmp_path: Path):
    (tmp_path / "nested.xml").write_text(NESTED_JUNIT, encoding="utf-8")
    result = parsers.junit_counts(tmp_path, ("*.xml",))
    assert result == {
        "collected": 8,
        "failed": 1,
        "errored": 1,
        "skipped": 1,
        "passed": 5,
    }


def test_junit_counts_flat_testsuite(tmp_path: Path):
    (tmp_path / "flat.xml").write_text(FLAT_JUNIT, encoding="utf-8")
    result = parsers.junit_counts(tmp_path, ("*.xml",))
    assert result == {
        "collected": 4,
        "failed": 2,
        "errored": 0,
        "skipped": 0,
        "passed": 2,
    }


def test_junit_counts_combines_multiple_files(tmp_path: Path):
    (tmp_path / "nested.xml").write_text(NESTED_JUNIT, encoding="utf-8")
    (tmp_path / "flat.xml").write_text(FLAT_JUNIT, encoding="utf-8")
    result = parsers.junit_counts(tmp_path, ("*.xml",))
    assert result == {
        "collected": 12,
        "failed": 3,
        "errored": 1,
        "skipped": 1,
        "passed": 7,
    }


def test_junit_counts_empty_when_nothing_matches(tmp_path: Path):
    assert parsers.junit_counts(tmp_path, ("*.xml",)) == {}


def test_junit_counts_skips_unparsable_file(tmp_path: Path):
    (tmp_path / "broken.xml").write_text("not xml at all <<<", encoding="utf-8")
    assert parsers.junit_counts(tmp_path, ("*.xml",)) == {}


# --- count_listing -----------------------------------------------------------------------


def test_count_listing_jest_files():
    text = (
        "/home/user/project/src/foo.test.ts\n"
        "/home/user/project/src/bar.test.js\n"
        "C:\\Users\\dev\\project\\baz.test.tsx\n"
        "not a path\n"
    )
    assert parsers.count_listing(text, "jest_files") == 3


def test_count_listing_cargo_list():
    text = "tests::test_add: test\nsome other line\ntests::test_sub: test\n"
    assert parsers.count_listing(text, "cargo_list") == 2


def test_count_listing_go_list():
    text = "TestAdd\nTestSub\nExampleFoo\nok  \tpkg\t0.010s\n"
    assert parsers.count_listing(text, "go_list") == 3


def test_count_listing_dotnet_list():
    text = (
        "Test run for /path/to/test.dll\n"
        "The following Tests are available:\n"
        "    Namespace.Class.Test1\n"
        "    Namespace.Class.Test2\n"
        "Not indented line\n"
    )
    assert parsers.count_listing(text, "dotnet_list") == 2


def test_count_listing_phpunit_list():
    text = " - Namespace\\ClassTest::testOne\n - Namespace\\ClassTest::testTwo\nNot a bullet\n"
    assert parsers.count_listing(text, "phpunit_list") == 2


def test_count_listing_rspec_dry():
    assert parsers.count_listing("10 examples, 0 failures\n", "rspec_dry") == 10


def test_count_listing_gradle_dry():
    text = "> Task :compileJava\n> Task :test\n> Task :module:test\n> Task :testClasses\n"
    assert parsers.count_listing(text, "gradle_dry") == 2


def test_count_listing_empty_text():
    assert parsers.count_listing("", "jest_files") == 0
    assert parsers.count_listing("nothing relevant here\n", "cargo_list") == 0


def test_count_listing_unknown_kind():
    assert parsers.count_listing("anything", "unknown_kind") == 0
