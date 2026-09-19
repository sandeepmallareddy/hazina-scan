from hazina_scan import classify, tree


def test_default_is_backend_for_a_plain_library(py_repo):
    out = classify.classify(tree.collect(py_repo))
    assert out["primary_class"] == "backend" and out["is_monorepo"] is False
