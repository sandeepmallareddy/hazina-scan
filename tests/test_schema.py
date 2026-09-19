import pytest

from hazina_scan import schema


def test_undeclared_key_is_refused():
    with pytest.raises(schema.EmissionRefused):
        schema.enforce("codebase_repos", {"repo_digest": "a" * 64, "surprise": 1})


def test_wrong_kind_is_refused():
    with pytest.raises(schema.EmissionRefused):
        schema.enforce("codebase_repos", {"loc": "twelve"})


def test_null_passes_everywhere():
    out = schema.enforce("codebase_repos", {"loc": None, "primary_language": None})
    assert out == {"loc": None, "primary_language": None}


def test_enum_folds_unknown_to_other():
    out = schema.enforce("measurement", {"tree": {"primary_language": "Brainfuck"}})
    assert out["tree"]["primary_language"] == "other"


def test_map_keys_outside_vocabulary_fold_and_sum():
    out = schema.enforce(
        "measurement", {"tree": {"loc_by_language": {"Python": 3, "X": 2, "Y": 5}}}
    )
    assert out["tree"]["loc_by_language"] == {"Python": 3, "other": 7}


def test_not_collected_fields_are_emptied():
    out = schema.enforce(
        "measurement", {"tree": {"test_spec_sample": ["src/a.py"], "hardcoded_secret_hits": 4}}
    )
    assert out["tree"] == {"test_spec_sample": [], "hardcoded_secret_hits": None}


def test_omitted_fields_are_dropped():
    out = schema.enforce("measurement", {"git": {"head_sha": "abc", "total_commits": 1}})
    assert out["git"] == {"total_commits": 1}


def test_own_prose_allows_our_words_and_rejects_paths():
    out = schema.enforce(
        "measurement",
        {
            "ext_signals": {
                "structure": {
                    "error": "parser unavailable (ImportError); reported unscored",
                    "note": "see src/main.py",
                }
            }
        },
    )
    assert out["ext_signals"]["structure"]["error"].startswith("parser unavailable")
    assert out["ext_signals"]["structure"]["note"] == ""


def test_own_prose_keeps_contractions():
    out = schema.enforce(
        "measurement",
        {
            "ext_signals": {
                "structure": {"note": "it couldn't  run because the parser wasn't   available"}
            }
        },
    )
    assert out["ext_signals"]["structure"]["note"] == (
        "it couldn't run because the parser wasn't available"
    )


def test_quoted_string_is_still_rejected():
    out = schema.enforce(
        "measurement", {"ext_signals": {"structure": {"note": "the 'main' module was skipped"}}}
    )
    assert out["ext_signals"]["structure"]["note"] == ""


def test_repo_full_name_rejects_paths():
    with pytest.raises(schema.EmissionRefused):
        schema.enforce("measurement", {"real_repo_name": "/home/x/repo"})
    assert schema.enforce("measurement", {"real_repo_name": "acme/demo"}) == {
        "real_repo_name": "acme/demo"
    }


def test_declared_key_sets():
    assert "loc_by_language" in schema.DECLARED_KEYS
    assert "Python" in schema.DECLARED_KEYS
    assert "primary_language" in schema.CLOSED_VOCABULARY_KEYS
    assert "readme_loc" not in schema.CLOSED_VOCABULARY_KEYS


def test_review_lists_not_collected_fields():
    doc = schema.enforce("measurement", {"tree": {"test_spec_sample": ["x"]}})
    text = schema.review({"measurement.json": doc}, full=False)
    assert "test_spec_sample" in text
