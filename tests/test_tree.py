from hazina_scan import tree
from tests.conftest import PY_FILES


def test_walk_skips_vendored_dirs(repo_builder):
    repo = repo_builder({"a.py": "x=1\n", "node_modules/b/index.js": "x\n", ".git/junk": "x"})
    rels = sorted(rel for _, rel in tree.walk_source_files(repo))
    assert rels == ["a.py"]


def test_languages(repo_builder):
    repo = repo_builder({"a.py": "1\n2\n3\n", "b.ts": "1\n", "c.tsx": "1\n2\n"})
    files = tree.walk_source_files(repo)
    langs = tree.analyze_languages(files)
    assert langs["primary_language"] == "Python"
    assert langs["loc_by_language"]["TypeScript"] == 3
    assert langs["file_count_by_language"] == {"Python": 1, "TypeScript": 2}


def test_jvm_share():
    assert tree.jvm_dotnet_loc_share({"Java": 50, "Python": 50}, 100) == 0.5
    assert tree.jvm_dotnet_loc_share({"Python": 10}, 10) == 0.0
    assert tree.jvm_dotnet_loc_share({}, 0) is None


def test_tests_are_counted(repo_builder):
    repo = repo_builder(
        {"src/a.py": "x\n", "tests/test_a.py": "x\n", "tests/__snapshots__/a.snap": "x\n"}
    )
    t = tree.analyze_tests(tree.walk_source_files(repo))
    assert t["spec_files"] == 1
    assert t["fixture_and_snapshot_files"] == 1


# A repository whose infrastructure is the point: Terraform, raw Kubernetes manifests, a
# Helm chart (Chart.yaml + values + templates/) and a vendored tree that must not be walked.
IAC_FILES = {
    "main.tf": 'resource "aws_s3_bucket" "logs" {\n  bucket = "acme-logs"\n\n  tags = {\n'
    '    env = "prod"\n  }\n}\n',
    "infra/variables.tf": 'variable "region" {\n  type    = string\n  default = "us-east-1"\n}\n',
    "k8s/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    "spec:\n  replicas: 2\n",
    "k8s/service.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\nspec:\n"
    "  ports:\n    - port: 80\n",
    "charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
    "charts/web/values.yaml": "replicaCount: 2\nimage:\n  tag: latest\n",
    "charts/web/templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n"
    "metadata:\n  name: {{ .Release.Name }}\n",
    "Dockerfile": "FROM python:3.12-slim\n\nWORKDIR /app\nCOPY . .\nRUN pip install .\n",
    "docker-compose.yml": "services:\n  web:\n    image: acme/web\n    ports:\n      - 8080:80\n",
    "vendor/github.com/acme/lib/lib.go": "package lib\n\nfunc Noop() {}\n",
    "app/main.py": "import os\n\n\ndef main():\n    return os.getcwd()\n",
    "app/util.py": "def slug(s):\n    return s.lower()\n",
    "tests/test_main.py": "from app.main import main\n\n\ndef test_main():\n    assert main()\n",
    "web/app.ts": "export const base = 1;\nexport const twice = base * 2;\n",
    "README.md": "# Infra\n",
}


# ---------------------------------------------------------------------------
# Dependencies, frameworks, lint and test framework
# ---------------------------------------------------------------------------

# A small Node repository: a manifest with runtime and dev dependencies, the lockfile
# that goes with it, a test-runner config, a lint config and a dependency-update bot.
NODE_FILES = {
    "package.json": '{"name":"web","dependencies":{"next":"14.0.0","react":"18"},'
    '"devDependencies":{"jest":"29","eslint":"8"},"scripts":{"test":"jest"}}',
    "package-lock.json": '{"name":"web","lockfileVersion":3,"packages":{"":{},'
    '"node_modules/next":{"version":"14.0.0"},"node_modules/react":{"version":"18.2.0"},'
    '"node_modules/jest":{"version":"29.0.0"}}}',
    "jest.config.js": "module.exports = {}\n",
    ".eslintrc.json": "{}\n",
    "src/index.tsx": "export const x = 1\n",
    "renovate.json": "{}\n",
}


def test_node_dependencies(repo_builder):
    repo = repo_builder(NODE_FILES)
    deps = tree.analyze_dependencies(repo)
    # The package manager is reported as the family that reads the manifest, because
    # package.json alone does not say which of the three installed it.
    assert deps["package_managers"] == ["npm/yarn/pnpm"]
    assert deps["direct_runtime_deps"] == 2 and deps["direct_dev_deps"] == 2
    assert deps["dep_update_tooling"] == "Renovate"
    assert "package-lock.json" in deps["lockfiles_found"]
    assert deps["total_transitive_deps"] == 4


def test_frameworks_and_project_type(repo_builder):
    repo = repo_builder(NODE_FILES)
    fw = tree.analyze_frameworks(repo)
    # React from the manifest: there is no next.config.* here, and a framework is only
    # named from its marker file or from a declared dependency, never from prose.
    assert "React" in fw
    assert tree.infer_project_type(repo, fw) == "web app"


def test_dep_keywords_are_matched_per_ecosystem(repo_builder):
    repo = repo_builder(NODE_FILES)
    hits = tree.match_dep_keywords(tree.collect_dependency_names(repo))
    assert hits["frontend_frameworks"] == ["next", "react"]
    assert hits["ml_libs"] == []


def test_lint_and_test_framework(repo_builder):
    repo = repo_builder(NODE_FILES)
    assert tree.analyze_lint_config(repo) == {
        "linters_and_formatters": {"ESLint": ".eslintrc.json"},
        "has_lint_config": True,
    }
    tf = tree.analyze_test_framework(repo)
    assert tf["frameworks"] == ["jest"] and tf["config_files"] == ["jest.config.js"]
    assert tf["coverage_tooling"] is None and tf["coverage_threshold"] is None


# A repository that exercises the manifest parsers the two fixtures above do not: a
# poetry pyproject, requirements files, a Pipfile, setup.cfg, a Gemfile and a go.mod.
POLYGLOT_FILES = {
    "pyproject.toml": '[tool.poetry]\nname = "svc"\n'
    '[tool.poetry.dependencies]\npython = "^3.11"\ndjango = "^5.0"\n'
    '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n'
    "[tool.ruff]\nline-length = 100\n",
    "requirements.txt": "boto3==1.34.0\n# a comment\nsqlalchemy>=2.0  # inline\n-e .\n",
    "Pipfile": '[packages]\nrequests = "*"\n[dev-packages]\nblack = "*"\n',
    "setup.cfg": "[flake8]\nmax-line-length = 100\n",
    "Gemfile": "source 'https://rubygems.org'\ngem 'sinatra'\n",
    "go.mod": "module example.com/svc\n\ngo 1.22\n\nrequire (\n"
    "\tgithub.com/gin-gonic/gin v1.9.1\n\tgo.mongodb.org/mongo-driver v1.13.1\n)\n",
    "go.sum": "github.com/gin-gonic/gin v1.9.1 h1:abc=\n"
    "github.com/gin-gonic/gin v1.9.1/go.mod h1:def=\n",
    "main.go": "package main\n\nfunc main() {}\n",
}


# Coverage, configured two ways: jest's `coverageThreshold` and vitest's `thresholds`.
# The number is lifted by a case-sensitive search for "threshold", so the camel-cased
# jest key yields no number and the lower-case vitest one does. The search is deliberately
# literal: a threshold is reported only where the config spells it the way the search looks
# for it, so the absence of a number is "not found", never a default.
COVERAGE_FILES = {
    "package.json": '{"name":"web","private":true,"devDependencies":{"jest":"29","nyc":"15"},'
    '"scripts":{"test":"jest --coverage"}}',
    "jest.config.js": "module.exports = {\n  collectCoverage: true,\n"
    "  coverageThreshold: { global: { lines: 80, statements: 75 } },\n}\n",
    "vitest.config.ts": "export default {\n  test: {\n    coverage: {\n"
    "      thresholds: { lines: 90 },\n    },\n  },\n}\n",
    "src/index.js": "export const x = 1\n",
}


def test_coverage_tooling_and_threshold(repo_builder):
    repo = repo_builder(COVERAGE_FILES, name="cov")
    tf = tree.analyze_test_framework(repo)
    assert tf["frameworks"] == ["vitest", "jest"]
    assert tf["config_files"] == ["vitest.config.ts", "jest.config.js"]
    # `nyc` in the manifest names the tooling; the runner configs only corroborate it.
    assert tf["coverage_tooling"] == "NYC/Istanbul"
    assert tf["coverage_threshold"] == 90


# ---------------------------------------------------------------------------
# CI, hygiene, docs, demo, reproducibility, observability and the assembly
# ---------------------------------------------------------------------------


def test_ci_parsed_from_workflow(py_repo):
    ci = tree.analyze_ci(py_repo)
    assert ci["ci_present"] is True and ci["ci_systems"] == ["GitHub Actions"]
    assert ci["runs_tests"] is True and ci["ci_analysis_method"] == "parsed"


def test_no_secret_scan_exists(py_repo):
    h = tree.analyze_hygiene(py_repo, tree.walk_source_files(py_repo))
    assert h["hardcoded_secret_hits"] is None and h["secret_hit_details"] == []


def test_demo_readme_is_content_not_name(repo_builder):
    """The two name signals are unmeasured, not false: the only name available is the
    directory the operator cloned into, which is ours rather than the repository's."""
    repo = repo_builder(
        {**PY_FILES, "README.md": "# Todo App\n\nA sample app built with the starter template.\n"},
        name="demo",
    )
    signals = tree.analyze_demo_signals(repo, tree.analyze_documentation(repo))
    assert signals["name_lexicon_hit"] is None and signals["strong_name_hit"] is None
    assert signals["template_readme"] is True and signals["authoritative_demo"] is True


# A pipeline that says nothing on its face: the work is behind `make`, behind an npm
# script, behind a shell script and behind a `${{ env.* }}` expression, and one step is
# switched off. What it runs is read out of the repository's own files.
CI_FILES = {
    "Makefile": "ci: lint test\n\ntest:\n\t@pytest -q\n\nlint:\n\truff check .\n",
    ".github/workflows/ci.yml": "name: ci\non: [push]\nenv:\n  PY: python3\njobs:\n  check:\n"
    "    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: make test\n"
    "      - run: ${{ env.PY }} -m mypy src\n"
    "      - run: bash scripts/audit.sh\n"
    "      - name: never\n        if: false\n        run: eslint .\n",
    "scripts/audit.sh": "#!/usr/bin/env bash\npip-audit\n",
    "src/app.py": "def main():\n    return '/health'\n",
    "README.md": "# Service\n",
}


def test_ci_follows_indirections_and_skips_disabled_steps(repo_builder):
    repo = repo_builder(CI_FILES, name="ci")
    ci = tree.analyze_ci(repo)
    # `make test` is followed into the Makefile, and `${{ env.PY }}` is resolved against
    # the workflow's own env block.
    assert ci["runs_tests"] is True and ci["runs_typecheck"] is True
    # The only lint command in the file is behind `if: false`, so it does not run.
    assert ci["runs_lint"] is False
