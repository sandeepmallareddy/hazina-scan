"""Every closed vocabulary and detection table this tool owns, held as data.

One copy, read by two kinds of caller: the collectors, which use the tables to *detect*
things in a repository, and `schema.py`, which uses the same tables to decide what a field
is *allowed to say*. Keeping them here is what stops the boundary drifting away from the
thing it is guarding -- a language the collector can name but the schema has never heard of
would otherwise be folded to "other" and the discrepancy would never surface.

These are our tables of public technology names. Not one of them is derived from the
repository being measured, which is why a value matched against one of them may leave the
machine at all.

The contents are pinned data, not a list to tidy: every entry decides a published number, so
a table is widened deliberately and never as a drive-by edit, and measurements stay
comparable across versions. Derived sets are computed at import from the source tables above
them, so adding an extension in one place widens the vocabulary everywhere.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

#: File extension -> the language it is written in. The only language detector there is:
#: nothing sniffs content to decide what a file is.
LANGUAGE_BY_EXT: dict[str, str] = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++",
    ".swift": "Swift",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".scala": "Scala",
    ".clj": "Clojure",
    ".hs": "Haskell",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".sql": "SQL",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".html": "HTML",
    ".htm": "HTML",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".tf": "Terraform",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
    ".mdx": "Markdown",
}

#: The extensions that count as code -- what LOC, file counts and file-size percentiles are
#: measured over. Deliberately narrower than LANGUAGE_BY_EXT: markup, config and data have
#: a language name but are not source, and counting them would flatter every repository
#: that ships a large lockfile or a pile of YAML.
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".py",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".cs",
        ".cpp",
        ".cc",
        ".cxx",
        ".c",
        ".h",
        ".swift",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".scala",
        ".clj",
        ".hs",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".vue",
        ".svelte",
    }
)

#: Infrastructure-as-code kinds. Kept apart from the language vocabulary as its own closed
#: set because infrastructure is counted separately from source and must never double-count
#: against it.
IAC_TYPES: frozenset[str] = frozenset(
    {
        "Dockerfile",
        "Terraform",
        "CloudFormation",
        "Helm",
        "Docker Compose",
        "Kubernetes",
        "Ansible",
    }
)

#: Everything a language-shaped field may say. "Unknown" is the honest answer for a file
#: whose extension is not in the table, and is a member rather than a fold to "other".
LANGUAGES: frozenset[str] = frozenset(LANGUAGE_BY_EXT.values()) | IAC_TYPES | {"Unknown"}

#: The language family whose LOC share is reported as its own scalar. No Groovy: there is
#: no Groovy entry in LANGUAGE_BY_EXT, so a .groovy file is never counted at all and naming
#: it here would imply a measurement nobody makes.
JVM_DOTNET_LANGUAGES: tuple[str, ...] = ("Java", "Kotlin", "Scala", "C#")


# ---------------------------------------------------------------------------
# Walking a checkout
# ---------------------------------------------------------------------------

#: Directory names never descended into: package caches, build output, virtualenvs,
#: vendored trees and generated code. Pruned by name at every depth, so `vendor` prunes
#: `third_party/vendor` as well as a top-level one.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "vendor",
        "dist",
        "build",
        "out",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "venv",
        ".venv",
        "env",
        ".env",
        "target",  # Rust target
        ".next",
        ".nuxt",
        ".output",
        ".turbo",
        "coverage",
        ".nyc_output",
        "htmlcov",
        "migrations",  # usually generated
        "generated",
        "gen",
        "__generated__",
        ".tox",
        "eggs",
        "*.egg-info",
    }
)

#: Dot-directories are skipped as a class -- these are the ones worth keeping, because
#: what a repository configures about itself is part of what it is.
DOT_DIRS_KEPT: frozenset[str] = frozenset({".github", ".gitlab", ".circleci", ".devcontainer"})

#: Markers that a file was written by a tool rather than a person. Matched against the
#: first 500 bytes only: a generator stamps its banner at the top or not at all.
GENERATED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"// Code generated", re.I),
    re.compile(r"# DO NOT EDIT", re.I),
    re.compile(r"# This file is auto-generated", re.I),
    re.compile(r"// AUTO-GENERATED", re.I),
    re.compile(r"/* eslint-disable \*/"),
]

#: Paths that are tests. Matched against the posix-style path relative to the repository
#: root, which is why every one of them is anchored on `/` rather than on a separator that
#: changes between operating systems.
TEST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$", re.I),
    re.compile(r"(^|/)test_[^/]+\.(py)$", re.I),
    re.compile(r"(^|/)[^/]+_test\.(go|py|rb)$", re.I),
    re.compile(r"(^|/)__tests__/", re.I),
    # Any file under a test/, tests/ or spec/ directory is a test. Fixtures inside one are
    # still split back out by the fixture patterns before anything is counted as a spec.
    re.compile(r"(^|/)tests?/", re.I),
    re.compile(r"(^|/)spec/", re.I),
    re.compile(r"[A-Z][^/]*Test[s]?\.(java|kt|cs)$"),
    re.compile(r"\.(test|spec)\.rs$", re.I),
]

#: Test-tree paths that hold data rather than assertions. Counted separately so a wall of
#: recorded snapshots never reads as a wall of tests.
FIXTURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)tests?/fixtures?/", re.I),
    re.compile(r"(^|/)__snapshots__/", re.I),
    re.compile(r"(^|/)__mocks__/", re.I),
    re.compile(r"\.snap$", re.I),
    re.compile(r"\.fixture\.(json|ts|js|yaml)$", re.I),
]

#: A test file above this many lines is recorded data, not a spec, whatever it is called.
FIXTURE_LOC_THRESHOLD: int = 5000


# ---------------------------------------------------------------------------
# Infrastructure as code
# ---------------------------------------------------------------------------

#: HCL is always infrastructure, so these need no content check.
IAC_HCL_EXTENSIONS: frozenset[str] = frozenset({".tf", ".tfvars", ".hcl"})

#: YAML and JSON are only infrastructure when their contents say so -- everything else
#: with these extensions is configuration or data and is counted as neither.
IAC_YAML_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml"})

#: Path prefixes whose YAML is CI configuration rather than infrastructure. CI is already
#: measured on its own, and counting a workflow twice would inflate both.
IAC_CI_DIR_PREFIXES: tuple[str, ...] = (".github/", ".gitlab", ".circleci/", ".gitlab-ci")

#: How much of a candidate manifest's head is read to identify it.
IAC_SNIFF_BYTES: int = 4096

#: A backstop on how many YAML/JSON heads are sniffed, so a pathological tree cannot stall
#: a scan. Set high because the skip list has already pruned the vendored trees and each
#: sniff reads 4 KB: a real repository's manifest count is nowhere near this.
IAC_MAX_SNIFF: int = 20_000

#: Kubernetes `kind:` values that mark a real cluster object, so that a YAML file which
#: merely happens to contain the words apiVersion and kind is not read as a manifest.
K8S_KINDS: frozenset[str] = frozenset(
    {
        "pod",
        "deployment",
        "service",
        "statefulset",
        "daemonset",
        "replicaset",
        "replicationcontroller",
        "job",
        "cronjob",
        "configmap",
        "secret",
        "ingress",
        "ingressclass",
        "persistentvolume",
        "persistentvolumeclaim",
        "storageclass",
        "namespace",
        "serviceaccount",
        "role",
        "rolebinding",
        "clusterrole",
        "clusterrolebinding",
        "horizontalpodautoscaler",
        "verticalpodautoscaler",
        "networkpolicy",
        "poddisruptionbudget",
        "customresourcedefinition",
        "endpoints",
        "endpointslice",
        "limitrange",
        "resourcequota",
        "list",
        "kustomization",
        "helmrelease",
        "gitrepository",
        "kustomize",
        "deploymentconfig",
        "route",
        "rollout",
        "sealedsecret",
        "certificate",
    }
)


# ---------------------------------------------------------------------------
# Frameworks
# ---------------------------------------------------------------------------

#: (marker file, framework it implies). A list rather than a dict because several markers
#: map to the same framework and the order is the detection order.
FRAMEWORK_MARKERS: list[tuple[str, str]] = [
    ("next.config.js", "Next.js"),
    ("next.config.ts", "Next.js"),
    ("next.config.mjs", "Next.js"),
    ("nuxt.config.ts", "Nuxt"),
    ("nuxt.config.js", "Nuxt"),
    ("svelte.config.js", "SvelteKit"),
    ("svelte.config.ts", "SvelteKit"),
    ("angular.json", "Angular"),
    ("vite.config.ts", "Vite"),
    ("vite.config.js", "Vite"),
    ("remix.config.js", "Remix"),
    ("remix.config.ts", "Remix"),
    ("astro.config.mjs", "Astro"),
    ("astro.config.ts", "Astro"),
    ("gatsby-config.js", "Gatsby"),
    ("gatsby-config.ts", "Gatsby"),
    ("vue.config.js", "Vue CLI"),
    ("manage.py", "Django"),
    ("settings.py", "Django"),
    ("wsgi.py", "WSGI (Flask/Django)"),
    ("asgi.py", "ASGI (FastAPI/Django Channels)"),
    ("Cargo.toml", "Rust (Cargo)"),
    ("go.mod", "Go Modules"),
    ("pom.xml", "Maven (Java)"),
    ("build.gradle", "Gradle"),
    ("build.gradle.kts", "Gradle (Kotlin DSL)"),
    ("Gemfile", "Ruby (Bundler)"),
    ("composer.json", "PHP (Composer)"),
    ("pubspec.yaml", "Flutter/Dart"),
    ("mix.exs", "Elixir (Mix)"),
    ("rebar.config", "Erlang (Rebar)"),
    ("project.clj", "Clojure (Leiningen)"),
    ("stack.yaml", "Haskell (Stack)"),
    ("cabal.project", "Haskell (Cabal)"),
    ("flake.nix", "Nix"),
    ("Makefile", "Make"),
    ("CMakeLists.txt", "CMake"),
    ("terraform.tfstate", "Terraform"),
    ("main.tf", "Terraform"),
    ("serverless.yml", "Serverless Framework"),
    ("serverless.yaml", "Serverless Framework"),
    ("amplify.yml", "AWS Amplify"),
    ("vercel.json", "Vercel"),
    ("netlify.toml", "Netlify"),
    ("fly.toml", "Fly.io"),
    ("helm/Chart.yaml", "Helm"),
    ("Chart.yaml", "Helm"),
    ("docker-compose.yml", "Docker Compose"),
    ("docker-compose.yaml", "Docker Compose"),
    ("Dockerfile", "Docker"),
    (".devcontainer/devcontainer.json", "Dev Container"),
    ("devcontainer.json", "Dev Container"),
]

#: (PyPI distribution name, the framework it implies). Read off the parsed dependency
#: names, never off the manifest text: a pyproject that says "migrating off Flask" names
#: no framework, and matching prose there is how a Python library became a web app.
PYPI_DEP_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("fastapi", "FastAPI"),
    ("flask", "Flask"),
    ("sqlalchemy", "SQLAlchemy"),
    ("pydantic", "Pydantic"),
)

#: (npm package name, the server framework it implies), in detection order.
NPM_DEP_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("express", "Express"),
    ("@nestjs/core", "NestJS"),
    ("hono", "Hono"),
    ("elysia", "Elysia"),
    ("koa", "Koa"),
    ("fastify", "Fastify"),
)

#: (npm package names, the library they imply), in detection order. Several names for one
#: library because a package can be depended on under either half of its split packaging.
NPM_DEP_LIBRARIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("prisma", "@prisma/client"), "Prisma"),
    (("drizzle-orm",), "Drizzle ORM"),
    (("typeorm",), "TypeORM"),
    (("zod",), "Zod"),
    (("tailwindcss",), "Tailwind CSS"),
)

#: Frameworks that have no marker file and are recognised from a manifest's dependency list
#: instead. Additive to the marker names above. React and tRPC are detected by hand rather
#: than from a table -- React only when no meta-framework already claimed the repository,
#: tRPC from any mention of one of its split `@trpc/*` packages -- so they are named here
#: directly.
DEP_FRAMEWORKS: frozenset[str] = (
    frozenset(name for _, name in PYPI_DEP_FRAMEWORKS)
    | frozenset(name for _, name in NPM_DEP_FRAMEWORKS)
    | frozenset(name for _, name in NPM_DEP_LIBRARIES)
    | {"React", "tRPC"}
)

FRAMEWORKS: frozenset[str] = frozenset(name for _, name in FRAMEWORK_MARKERS) | DEP_FRAMEWORKS

#: The meta-frameworks that already imply React. When one of them is present, a `react`
#: dependency is the meta-framework's own and is not reported a second time under its own
#: name -- "Next.js, React" says nothing "Next.js" does not.
REACT_META_FRAMEWORKS: tuple[str, ...] = ("Next.js", "Remix", "Gatsby")


# ---------------------------------------------------------------------------
# Manifests, package managers and lockfiles
# ---------------------------------------------------------------------------

#: Dependency manifest -> the package manager that reads it.
MANIFEST_TO_PM: dict[str, str] = {
    "package.json": "npm/yarn/pnpm",
    "pyproject.toml": "pip/poetry/uv",
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "go.mod": "Go modules",
    "Cargo.toml": "Cargo",
    "Gemfile": "Bundler",
    "composer.json": "Composer",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "pubspec.yaml": "pub (Dart)",
    "mix.exs": "Mix (Elixir)",
    "Package.swift": "Swift Package Manager",
}

PACKAGE_MANAGERS: frozenset[str] = frozenset(MANIFEST_TO_PM.values())

#: setup.py and setup.cfg are manifests we recognise but do not map to a package manager,
#: because either can belong to pip, poetry or setuptools and guessing would be a fiction.
MANIFESTS: frozenset[str] = frozenset(MANIFEST_TO_PM) | {"setup.py", "setup.cfg"}

LOCKFILE_PATTERNS: list[str] = [
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "go.sum",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "packages.lock.json",
    "gradle.lockfile",
    "pubspec.lock",
]

LOCKFILES: frozenset[str] = frozenset(LOCKFILE_PATTERNS)

#: The Python manifests, in the order they are looked for. Only the first one found is
#: recorded: a repository with a pyproject.toml and a requirements.txt has one Python
#: toolchain, not two, and naming both would double-count the ecosystem.
PYTHON_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "setup.py",
    "setup.cfg",
)

#: Manifest -> what should have been locked beside it. Phrases rather than filenames,
#: because several files satisfy one expectation and which one is a choice the repository
#: is entitled to make.
LOCKFILE_EXPECTED_BY_MANIFEST: dict[str, str] = {
    "package.json": "package-lock.json or yarn.lock or pnpm-lock.yaml",
    "go.mod": "go.sum",
    "Cargo.toml": "Cargo.lock",
    "Gemfile": "Gemfile.lock",
    "composer.json": "composer.lock",
}

#: The one expectation shared by every Python manifest above, for the same reason.
PYTHON_LOCKFILE_EXPECTED: str = "poetry.lock / Pipfile.lock / uv.lock / requirements.txt (pinned)"

#: What a reader is told *should* have been there when a manifest was found without a lock
#: beside it.
LOCKFILES_EXPECTED: frozenset[str] = frozenset(LOCKFILE_EXPECTED_BY_MANIFEST.values()) | {
    PYTHON_LOCKFILE_EXPECTED
}

#: (tool, the files that prove it is configured), in the order the answer is decided --
#: one tool is reported, and a repository running both is reported as running Dependabot.
DEP_UPDATE_TOOLING: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Dependabot", (".github/dependabot.yml", ".github/dependabot.yaml")),
    ("Renovate", ("renovate.json", ".renovaterc", ".renovaterc.json")),
)


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------

#: (config path, the CI system it belongs to).
CI_CONFIGS: list[tuple[str, str]] = [
    (".github/workflows", "GitHub Actions"),
    (".circleci/config.yml", "CircleCI"),
    (".gitlab-ci.yml", "GitLab CI"),
    ("Jenkinsfile", "Jenkins"),
    ("azure-pipelines.yml", "Azure Pipelines"),
    (".travis.yml", "Travis CI"),
    ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    (".buildkite/pipeline.yml", "Buildkite"),
    ("cloudbuild.yaml", "GCP Cloud Build"),
    (".woodpecker.yml", "Woodpecker CI"),
    (".drone.yml", "Drone CI"),
]

CI_SYSTEMS: frozenset[str] = frozenset(name for _, name in CI_CONFIGS)

#: Per CI system, the step key whose value holds a shell command or a list of them: GitHub
#: Actions and CircleCI call it `run`, GitLab, Travis, Bitbucket and Azure call it `script`,
#: Drone and Woodpecker call it `commands`, and Cloud Build calls it `args`.
CI_COMMAND_KEYS: frozenset[str] = frozenset(
    {
        "run",
        "script",
        "before_script",
        "after_script",
        "commands",
        "command",
    }
)

#: Conditions that switch a step off outright. Only a literal can be decided without
#: running the pipeline, so an expression whose value turns on the triggering event is not
#: treated as a disabled step at all.
CI_DISABLED: frozenset[str] = frozenset({"false", "${{ false }}", "${{false}}", "0", "no", "off"})

#: Prefixes that wrap a command without being one: runners, elevation, environment
#: assignments. Stripped so that `poetry run pytest` is read as `pytest`.
CI_WRAPPER: re.Pattern[str] = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:sudo\s+|time\s+|nice\s+|exec\s+|xvfb-run(?:\s+-\S+)*\s+|env\s+(?:\S+=\S*\s+)*|"
    r"(?:poetry|pipenv|uv|hatch|pdm|rye)\s+run\s+(?:--\S+\s+)*|"
    r"bundle\s+exec\s+|npx\s+(?:--\S+\s+)*|pnpm\s+(?:exec|dlx)\s+|yarn\s+dlx\s+)*",
    re.I,
)

#: Flags that build the project while switching the suite off -- `-DskipTests` on Maven,
#: `-x test` on Gradle. Both command lines contain the word "test" and neither one runs a
#: single test, which is precisely why they are matched separately.
CI_TESTS_SKIPPED: re.Pattern[str] = re.compile(
    r"-DskipTests|-Dmaven\.test\.skip|(^|\s)-x\s+test\b|--no-run\b", re.I
)

#: The test runners, matched against the head of a CI command rather than searched for in
#: the file: a workflow that merely contains the word "test" runs no suite.
CI_TEST_COMMAND: re.Pattern[str] = re.compile(
    r"^(?:"
    r"pytest|py\.test|tox|nox|behave|robot|"
    r"python[0-9.]*\s+-m\s+(?:pytest|unittest|nose2|tox|nox)|"
    r"python[0-9.]*\s+setup\.py\s+test|"
    r"jest|vitest|mocha|ava|karma|jasmine|tap|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?(?:test|tests)\b|"
    r"go\s+test|gotestsum|"
    r"cargo\s+(?:test|nextest\s+run)|"
    r"(?:\./)?gradlew?(?:\.bat)?\s+.*\b(?:test|check|build)\b|"
    r"(?:\./)?mvnw?\s+.*\b(?:test|verify|install)\b|mvn\s+.*\b(?:test|verify|install)\b|"
    r"make\s+.*\b(?:test|tests|check|coverage)\b|"
    r"just\s+.*\b(?:test|check)\b|"
    r"rspec|rake\s+(?:test|spec)|minitest|"
    r"phpunit|\S*vendor/bin/phpunit|composer\s+(?:run-script\s+)?test|"
    r"dotnet\s+test|ctest|bazel\s+test|ninja\s+test|swift\s+test|"
    r"playwright\s+test|cypress\s+run|"
    r"coverage\s+run|pytest-\S+"
    r")\b",
    re.I,
)

CI_LINT_COMMAND: re.Pattern[str] = re.compile(
    r"^(?:eslint|pylint|ruff|flake8|golangci-lint|clippy|cargo\s+clippy|rubocop|"
    r"black|isort|prettier|shellcheck|stylelint|standardrb|credo|swiftlint|ktlint|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?lint|"
    r"pre-commit\s+run|make\s+.*\blint\b|tox\s+.*\blint\b|nox\s+.*\blint\b)\b",
    re.I,
)

CI_TYPECHECK_COMMAND: re.Pattern[str] = re.compile(
    r"^(?:tsc|mypy|pyright|pyre|flow\s+check|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?type[-:]?check|"
    r"make\s+.*\btype.?check\b|tox\s+.*\b(?:mypy|type)\b|"
    r"python[0-9.]*\s+-m\s+(?:mypy|pyright|pyre|ty)\b)\b",
    re.I,
)

#: Deployment is looked for as free text rather than through the command table, and on
#: purpose. It usually appears as the name of a job, as an `environment:` block or as a
#: third-party action, and hardly ever as a command with a recognisable verb, so matching
#: commands would miss it in most pipelines that really do deploy.
CI_DEPLOY_TEXT: re.Pattern[str] = re.compile(
    r"\bdeploy\b|\brelease\b|\bpublish\b|\bpush.*ecr\b|\bpush.*registry\b", re.I
)

#: Third-party actions that perform the check themselves, leaving no command line behind
#: for the command table to find.
CI_USES_LINT: re.Pattern[str] = re.compile(
    r"^(?:golangci/golangci-lint-action|reviewdog/action-|"
    r"github/super-linter|super-linter/super-linter|psf/black|"
    r"astral-sh/ruff-action|chartboost/ruff-action|pre-commit/action|"
    r"rubocop/|wearerequired/lint-action)",
    re.I,
)

CI_USES_TYPECHECK: re.Pattern[str] = re.compile(
    r"^(?:jakebailey/pyright-action|tsuyoshicho/action-mypy|"
    r"jpetrucciani/mypy-check)",
    re.I,
)

#: The auditors a pipeline can run against its own dependencies. A text search of the CI
#: config, because an audit is as often a marketplace action as a command.
DEP_AUDIT_TEXT: re.Pattern[str] = re.compile(
    r"\baudit\b|\bsnyk\b|\btrivy\b|\bgrype\b|\bsafety\b|\bpip.audit\b", re.I
)


# ---------------------------------------------------------------------------
# Linting and formatting
# ---------------------------------------------------------------------------

#: Config filename -> the linter or formatter it configures. Two entries say "check
#: content" because the file is shared: pyproject.toml and setup.cfg belong to whichever
#: tool wrote a section into them.
LINT_CONFIGS: dict[str, str] = {
    ".eslintrc.js": "ESLint",
    ".eslintrc.cjs": "ESLint",
    ".eslintrc.ts": "ESLint",
    ".eslintrc.json": "ESLint",
    ".eslintrc.yaml": "ESLint",
    "eslint.config.js": "ESLint",
    "eslint.config.mjs": "ESLint",
    "eslint.config.ts": "ESLint",
    ".prettierrc": "Prettier",
    ".prettierrc.js": "Prettier",
    ".prettierrc.json": "Prettier",
    ".prettierrc.yaml": "Prettier",
    "ruff.toml": "Ruff",
    ".ruff.toml": "Ruff",
    ".pylintrc": "Pylint",
    "pylintrc": "Pylint",
    "pyproject.toml": "Ruff/Black (check content)",
    ".flake8": "Flake8",
    "setup.cfg": "Flake8 (check content)",
    "golangci-lint.yml": "golangci-lint",
    ".golangci.yml": "golangci-lint",
    ".golangci.yaml": "golangci-lint",
    ".rubocop.yml": "RuboCop",
    "phpcs.xml": "PHP_CodeSniffer",
    ".editorconfig": "EditorConfig",
    "biome.json": "Biome",
    "biome.jsonc": "Biome",
    ".oxlintrc": "Oxlint",
}

#: The tools a pyproject.toml can configure, named by the `[tool.<name>]` section they
#: write. A shared file proves nothing by existing, so it is read for these.
PYPROJECT_LINTERS: tuple[str, ...] = ("ruff", "black", "pylint", "flake8", "mypy", "pyright")

#: The marker-table names plus the lower-case tool names a shared config's contents can
#: name directly ("[tool.ruff]" in pyproject.toml).
LINTERS: frozenset[str] = frozenset(LINT_CONFIGS.values()) | frozenset(PYPROJECT_LINTERS)

LINT_CONFIG_FILES: frozenset[str] = frozenset(LINT_CONFIGS)


# ---------------------------------------------------------------------------
# Tests and coverage
# ---------------------------------------------------------------------------

#: A dependency name or config token -> the test framework it means. The key is what
#: appears in a manifest; the value is the name we are willing to write down.
TEST_FRAMEWORK_SIGNALS: dict[str, str] = {
    "vitest": "vitest",
    "jest": "jest",
    "mocha": "mocha",
    "jasmine": "jasmine",
    "ava": "ava",
    "tape": "tape",
    "qunit": "qunit",
    "cypress": "Cypress",
    "playwright": "Playwright",
    "@playwright/test": "Playwright",
    "pytest": "pytest",
    "unittest": "unittest (stdlib)",
    "nose2": "nose2",
    "rspec": "RSpec",
    "minitest": "Minitest",
    "go test": "go test",
    "testing": "go test",
    "JUnit": "JUnit",
    "TestNG": "TestNG",
    "RustTest": "rust built-in tests",
    "PHPUnit": "PHPUnit",
    "NUnit": "NUnit",
    "xUnit": "xUnit",
}

TEST_FRAMEWORKS: frozenset[str] = frozenset(TEST_FRAMEWORK_SIGNALS.values()) | {
    "Karma",
    "Mocha",
    "cargo test",
}

#: The JS/TS runner configs, in the order they are looked for. Every one of them is a
#: file that exists only to configure a test runner, so its presence is the detection.
JS_TEST_CONFIG_FILES: tuple[str, ...] = (
    "vitest.config.ts",
    "vitest.config.js",
    "vitest.config.mjs",
    "jest.config.ts",
    "jest.config.js",
    "jest.config.cjs",
    "jest.config.json",
    "playwright.config.ts",
    "playwright.config.js",
    "cypress.config.ts",
    "cypress.config.js",
    "karma.conf.js",
    ".mocharc.js",
    ".mocharc.json",
    ".mocharc.yaml",
)

#: (the token a runner's config filename contains, the runner it names). Checked in this
#: order against the config's own name, so `vitest.config.ts` is vitest and not jest.
JS_TEST_CONFIG_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("vitest", "vitest"),
    ("jest", "jest"),
    ("playwright", "Playwright"),
    ("cypress", "Cypress"),
    ("karma", "Karma"),
    ("mocha", "Mocha"),
)

#: The files a Python project can configure pytest in, in the order they are looked for.
#: Shared files, so each is read rather than merely found.
PYTEST_CONFIG_FILES: tuple[str, ...] = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")

#: What a pytest configuration looks like inside one of those shared files.
PYTEST_CONFIG_MARKERS: tuple[str, ...] = ("[tool.pytest", "[pytest]", "pytest")

TEST_CONFIG_FILES: frozenset[str] = frozenset(JS_TEST_CONFIG_FILES) | frozenset(PYTEST_CONFIG_FILES)

#: (what a package.json mentioning coverage can say, the tooling it means), in the order
#: the answer is decided.
JS_COVERAGE_PROBES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("c8", '"coverage"'), "c8/v8"),
    (("nyc", "istanbul"), "NYC/Istanbul"),
    (("lcov",), "lcov"),
)

#: The runner configs read for a coverage block and a line threshold.
COVERAGE_CONFIG_FILES: tuple[str, ...] = (
    "vitest.config.ts",
    "vitest.config.js",
    "jest.config.ts",
    "jest.config.js",
)

#: Either a named coverage tool, or the shared config file a coverage setting was found in.
#:
#: Kept as it stands, mismatch with the collector and all: the collector can only ever say
#: "configured in <a JS runner config>", so the four tool names below are unreachable and the
#: four reachable config names are missing. The schema folds whatever is not listed here to
#: "other", so correcting the table would change this field's value on repositories already
#: measured; it is left alone so measurements stay comparable across versions.
#: `COVERAGE_CONFIG_FILES` above is what the collector actually reads.
COVERAGE_TOOLING: frozenset[str] = frozenset({"c8/v8", "NYC/Istanbul", "lcov", "pytest-cov"}) | {
    f"configured in {name}" for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
}


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

#: Grouped by the language whose source is searched for them, because "logging" is a
#: different string in every ecosystem.
LOGGING_FRAMEWORKS: dict[str, list[str]] = {
    "js": ["pino", "winston", "bunyan", "log4js", "loglevel", "@nestjs/common"],
    "py": ["structlog", "loguru", "logging.getLogger", "logzero"],
    "go": ["zerolog", "zap", "logrus", "slog"],
    "java": ["slf4j", "log4j", "logback"],
    "ruby": ["Rails.logger", "Logger.new"],
}

#: The flat vocabulary the emitted field is checked against -- every library above, with
#: the language grouping dropped.
LOGGING_LIBRARIES: frozenset[str] = frozenset(
    lib for libs in LOGGING_FRAMEWORKS.values() for lib in libs
)

ERROR_TRACKING_LIBS: list[str] = [
    "@sentry/",
    "sentry-sdk",
    "sentry_sdk",
    "rollbar",
    "honeybadger",
    "bugsnag",
    "airbrake",
]

ERROR_TRACKING: frozenset[str] = frozenset(ERROR_TRACKING_LIBS)

VALIDATION_LIBS: frozenset[str] = frozenset(
    {
        "zod",
        "pydantic",
        "joi",
        "yup",
        "class-validator",
        "valibot",
        "jsonschema",
    }
)

#: (what a source file says when it validates its input, the library that says it), in
#: detection order. Public package names and their own public API, never a symbol from the
#: repository being read.
VALIDATION_PROBES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bzod\b|z\.object|z\.string"), "zod"),
    (re.compile(r"\bpydantic\b|BaseModel"), "pydantic"),
    (re.compile(r"\bjoi\b|Joi\.object"), "joi"),
    (re.compile(r"\byup\b"), "yup"),
    (re.compile(r"\bclass-validator\b|@IsString|@IsNotEmpty"), "class-validator"),
    (re.compile(r"\bvalibot\b"), "valibot"),
    (re.compile(r"\bjsonschema\b|jsonschema\.validate"), "jsonschema"),
)

#: The logging libraries looked for in each ecosystem's manifest, in detection order --
#: one name is reported, and the later ecosystems overrule the earlier ones because a
#: repository with a go.mod logs from Go whatever its package.json also carries.
NPM_LOGGING_LIBS: tuple[str, ...] = ("pino", "winston", "bunyan", "log4js")
PYPI_LOGGING_LIBS: tuple[str, ...] = ("structlog", "loguru")
GO_LOGGING_LIBS: tuple[str, ...] = ("zerolog", "zap", "logrus")

#: A route that answers "is this process alive": the quoted or slash-prefixed form, which
#: is how a path literal appears in every language that has one.
HEALTH_ENDPOINT_TEXT: re.Pattern[str] = re.compile(
    r"""["'/](health|ping|readiness|liveness)["']""", re.I
)

#: The metric exporters and agents a service instruments itself with.
METRICS_TEXT: re.Pattern[str] = re.compile(r"prometheus|prom-client|metrics|statsd|datadog", re.I)


# ---------------------------------------------------------------------------
# Repository classification
# ---------------------------------------------------------------------------

#: Dependency keywords grouped by the kind of work they evidence, then by the ecosystem
#: whose manifest they appear in. A trailing `*` is a prefix match; `dep_keyword_tokens()`
#: strips it, because what is emitted is the keyword that matched and not the glob.
CLASS_DEP_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "ml_libs": {
        "pypi": [
            "torch",
            "pytorch",
            "tensorflow",
            "tensorflow-gpu",
            "jax",
            "jaxlib",
            "flax",
            "scikit-learn",
            "sklearn",
            "keras",
            "xgboost",
            "lightgbm",
            "catboost",
            "transformers",
            "timm",
            "onnx",
            "onnxruntime",
            "diffusers",
            "sentence-transformers",
        ],
        "npm": ["@tensorflow/tfjs", "onnxruntime-node", "onnxruntime-web"],
    },
    "experiment_tracking": {
        "pypi": [
            "wandb",
            "mlflow",
            "tensorboard",
            "sacred",
            "neptune",
            "neptune-client",
            "clearml",
            "hydra-core",
            "optuna",
            "accelerate",
            "deepspeed",
            "pytorch-lightning",
            "lightning",
        ],
    },
    "data_eng": {
        "pypi": [
            "apache-airflow*",
            "dagster",
            "prefect",
            "dbt",
            "dbt-core",
            "pyspark",
            "apache-beam",
            "luigi",
            "kafka-python",
            "confluent-kafka",
            "great-expectations",
            "pandera",
            "dask",
            "apache-flink",
            "kedro",
            "feast",
            "delta-spark",
        ],
        "npm": ["kafkajs"],
        "go": ["sarama", "kafka-go"],
    },
    "security_libs": {
        "pypi": [
            "bandit",
            "semgrep",
            "scapy",
            "pwntools",
            "pycryptodome",
            "python-nmap",
            "yara-python",
            "volatility",
            "volatility3",
            "impacket",
            "mitmproxy",
            "angr",
            "capstone",
        ],
        "go": ["trufflehog", "nuclei", "gosec"],
    },
    "backend_frameworks": {
        "npm": ["express", "fastify", "koa", "hono", "@nestjs"],
        "pypi": ["fastapi", "flask", "django"],
        "go": ["gin-gonic", "gofiber", "labstack/echo"],
        "cargo": ["actix-web", "axum", "rocket"],
        "gem": ["sinatra"],
        "composer": ["laravel"],
        "maven": ["org.springframework.boot", "spring-boot*"],
    },
    # Deliberately npm-only. PyPI happens to have unrelated packages also named `lit`,
    # `astro`, `solid` and `ember`, and none of those turn a Python repository into a
    # frontend.
    "frontend_frameworks": {
        "npm": [
            "react",
            "react-dom",
            "react-native",
            "vue",
            "@vue",
            "svelte",
            "@sveltejs",
            "@angular",
            "next",
            "nuxt",
            "@nuxt",
            "solid-js",
            "preact",
            "tailwindcss",
            "@mui",
            "styled-components",
            "@chakra-ui",
            "@remix-run",
            "gatsby",
            "astro",
            "@builder.io/qwik",
            "ember-source",
            "lit",
        ],
    },
    "orm_db": {
        "npm": [
            "prisma",
            "@prisma",
            "drizzle-orm",
            "typeorm",
            "sequelize",
            "mongoose",
            "pg",
            "ioredis",
            "knex",
        ],
        "pypi": ["sqlalchemy", "psycopg*", "alembic", "pymongo", "asyncpg", "peewee"],
        "go": ["gorm", "sqlx", "pgx"],
        "any": ["redis", "mysql*", "mongodb"],
    },
    "infra_libs": {
        "any": [
            "pulumi",
            "@pulumi",
            "aws-cdk*",
            "@aws-cdk",
            "kubernetes",
            "@kubernetes",
            "ansible",
            "ansible-core",
        ],
        "pypi": ["boto3", "google-cloud*", "azure-mgmt*"],
        "npm": ["@google-cloud"],
    },
}


def dep_keyword_tokens() -> set[str]:
    """Every token a dependency-keyword hit may be reported as.

    One flat set across all the groups and ecosystems above, with the prefix-match `*`
    removed and the case normalised, because that is the form the collector records a hit
    in and therefore the only form the schema may accept.
    """
    return {
        keyword.rstrip("*").lower()
        for ecosystems in CLASS_DEP_KEYWORDS.values()
        for keywords in ecosystems.values()
        for keyword in keywords
    }


DEP_KEYWORD_GROUPS: frozenset[str] = frozenset(CLASS_DEP_KEYWORDS)
DEP_KEYWORDS: frozenset[str] = frozenset(dep_keyword_tokens())

#: The kinds of engineering a repository can be classified as. Atomic: a repository gets
#: one of these, and a mixture is expressed by the per-class confidence map rather than by
#: inventing a compound name.
ATOMIC_CLASSES: list[str] = [
    "frontend",
    "backend",
    "ml",
    "ai_research",
    "data_engineering",
    "security",
    "infra",
]

REPO_CLASSES: frozenset[str] = frozenset(ATOMIC_CLASSES)

#: `detected_frameworks` names that count as frontend evidence, read by `classify.py`.
FRONTEND_FRAMEWORK_MARKERS: frozenset[str] = frozenset(
    {
        "React",
        "Vue",
        "Vue CLI",
        "Angular",
        "SvelteKit",
        "Astro",
        "Gatsby",
        "Next.js",
        "Nuxt",
        "Remix",
        "Tailwind CSS",
    }
)

#: `detected_frameworks` names that count as backend evidence, read by `classify.py`.
BACKEND_FRAMEWORK_MARKERS: frozenset[str] = frozenset(
    {
        "Express",
        "NestJS",
        "FastAPI",
        "Flask",
        "Django",
        "Fastify",
        "Hono",
        "Koa",
        "Maven (Java)",
        "Ruby (Bundler)",
        "WSGI (Flask/Django)",
        "ASGI (FastAPI/Django Channels)",
        "tRPC",
    }
)

#: The languages any user interface has to be written in. CODE_EXTENSIONS covers neither
#: CSS nor HTML, which is why stylesheet weight reaches the classifier by its own route, as
#: `css_loc_ratio`.
WEB_LANGUAGES: frozenset[str] = frozenset({"TypeScript", "JavaScript", "Vue", "Svelte"})

#: Calling something a frontend requires frontend evidence besides keywords: component
#: files, a real amount of CSS, or a non-trivial share of lines in a web language. Without
#: at least one of those three, keyword and framework hits alone are treated as noise.
MIN_FRONTEND_WEB_LOC_SHARE: float = 0.10
MIN_FRONTEND_CSS_RATIO: float = 0.02

#: The minimum weight a term must contribute before it is treated as its own independent
#: piece of support, so a negligible, rounding-error-sized contribution does not get
#: counted as a whole additional signal.
SUPPORT_TERM_FLOOR: float = 0.5

#: The bar a single-family class has to clear before it can outrank a rival class that has
#: corroboration from more than one signal family.
MIN_SINGLE_FAMILY_RAW: float = 4.0

#: A leading score under this figure is too faint to support any conclusion at all.
MIN_PRIMARY_RAW: float = 1.0

#: Below this much accumulated evidence, the classifier declines to name a class at all
#: rather than guess. Set at the lowest score a corroborated signal can actually produce.
MIN_CONFIDENCE_EVIDENCE: float = 1.0

#: The named reasons a repository can be judged a demonstration or a scaffold rather than
#: real work. A tuple, because the classification block emits one boolean per signal and
#: the order is the order a reader sees them in.
DEMO_SIGNALS: tuple[str, ...] = (
    "name_lexicon_hit",
    "strong_name_hit",
    "known_demo_app",
    "template_readme",
    "scaffold_fingerprint",
    "boilerplate_readme",
    "authoritative_demo",
    "static_name_family",
    "static_content_family",
)

#: Names that mark a scaffold, an exercise or a demonstration rather than original work.
#: Only ever matched against a name from an authoritative source -- never against the
#: directory an operator happened to clone into, which is ours and not the repository's.
DEMO_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(^|[_\-/])("
    r"demo|sample|examples?|starter|boilerplate|scaffold|playground|"
    r"hello[-_]?world|tutorials?|workshops?|poc|proof[-_]?of[-_]?concept|"
    r"sandbox|kata|exercises?|todo|todoapp|test[-_]?ci"
    r")([_\-/]|$)"
)

#: The unambiguous half of that lexicon: words that mark a demonstration on their own.
#: The weaker tokens above -- todo, sandbox, kata, test-ci -- still need corroboration.
STRONG_DEMO_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(^|[_\-/])("
    r"demo|sample|examples?|boilerplate|scaffold|starter|"
    r"hello[-_]?world|playground|poc|proof[-_]?of[-_]?concept|todomvc"
    r")([_\-/]|$)"
)

#: The well-known demonstration and reference applications, recognised by name.
KNOWN_DEMO_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(sock[-_ ]?shop|weaveworks|realworld|petclinic|todomvc|"
    r"guestbook|2048|nodegoat|juice[-_ ]?shop)\b"
)

#: README phrases that are a scaffold's own words about itself.
TEMPLATE_README_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)("
    r"bootstrapped with create react app|create-react-app|"
    r"weaveworks|sock shop|realworld example|"
    r"this (is a|project is a|repo(sitory)? is a) "
    r"(demo|sample|example|template|boilerplate|starter)|"
    r"(sample|example|demo) (app|application|project)|"
    r"getting started template|starter (kit|template)|scaffold(ing)? for"
    r")"
)

#: A README shorter than this is boilerplate: there is not room in it to say what the
#: repository does.
BOILERPLATE_README_LOC: int = 12

#: Extensions that are a user-interface component, counted as their own class signal
#: because a repository of them is a frontend whatever its manifest says.
UI_COMPONENT_EXTENSIONS: tuple[str, ...] = (".tsx", ".jsx", ".vue", ".svelte")

#: Stylesheets. Counted apart from the languages because CSS is not a code extension, so
#: its lines appear nowhere else.
CSS_EXTENSIONS: tuple[str, ...] = (".css", ".scss", ".sass", ".less")

#: Columnar and record data files, whose presence is a data-engineering signal.
DATA_FILE_EXTENSIONS: tuple[str, ...] = (".parquet", ".avro", ".orc", ".feather")

#: The Terraform source extensions, counted as files rather than measured as lines.
TERRAFORM_EXTENSIONS: tuple[str, ...] = (".tf", ".tfvars")

#: The meta-frameworks that make a package.json an application rather than a library, no
#: matter what its `main` entry point says: a Next.js app ships a site, not an import.
APP_FRAMEWORKS: tuple[str, ...] = (
    "Next.js",
    "Remix",
    "Gatsby",
    "Nuxt",
    "SvelteKit",
    "Angular",
)

#: Those, plus the view libraries that are an application on their own.
WEB_APP_FRAMEWORKS: tuple[str, ...] = APP_FRAMEWORKS + ("React", "Vue CLI")

#: Frameworks whose job is to answer requests.
API_SERVICE_FRAMEWORKS: tuple[str, ...] = (
    "Express",
    "NestJS",
    "FastAPI",
    "Flask",
    "Django",
    "Hono",
    "Fastify",
    "Elysia",
    "Koa",
    "tRPC",
)

#: Frameworks whose subject is infrastructure rather than an application.
INFRASTRUCTURE_FRAMEWORKS: tuple[str, ...] = ("Terraform", "Helm", "Fly.io")

#: Toolchains that build either a binary or a library, which is decided from the layout
#: rather than from the manifest: `main.go`, `cmd/`, `src/main.rs`, `src/lib.rs`.
BINARY_OR_LIBRARY_FRAMEWORKS: tuple[str, ...] = ("Rust (Cargo)", "Go Modules")

PROJECT_TYPES: frozenset[str] = frozenset(
    {
        "library",
        "web app",
        "API service",
        "mobile",
        "infrastructure",
        "CLI / service",
        "CLI",
        "unknown",
    }
)


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

#: Heading topics a README is checked for, in the order they are reported. Lower case,
#: because the check folds case: what is emitted is the topic we looked for, never the
#: heading the repository wrote.
README_SECTION_TOPICS: tuple[str, ...] = (
    "install",
    "setup",
    "getting started",
    "run",
    "test",
    "environment",
    "contributing",
    "architecture",
    "usage",
    "api",
)

README_SECTIONS: frozenset[str] = frozenset(README_SECTION_TOPICS)

#: The README, changelog and contributing filenames, each in the order they are looked
#: for: the first one found is the answer, and a repository with two of them has chosen
#: the earlier one by convention.
README_FILENAMES: tuple[str, ...] = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
)

README_NAMES: frozenset[str] = frozenset(README_FILENAMES)

CHANGELOG_FILENAMES: tuple[str, ...] = (
    "CHANGELOG.md",
    "CHANGELOG.rst",
    "CHANGELOG",
    "CHANGES.md",
    "HISTORY.md",
    "RELEASES.md",
)

CHANGELOG_NAMES: frozenset[str] = frozenset(CHANGELOG_FILENAMES)

CONTRIBUTING_FILENAMES: tuple[str, ...] = (
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING",
)

CONTRIBUTING_NAMES: frozenset[str] = frozenset(CONTRIBUTING_FILENAMES)

#: The Dockerfile suffixes that mean a development image rather than a shipped one.
DEV_DOCKERFILE_SUFFIXES: tuple[str, ...] = ("dev", "development", "local")

#: The example environment files, in the order they are looked for. Only the filename is
#: ever recorded, and the file itself is never opened.
ENV_EXAMPLE_FILENAMES: tuple[str, ...] = (
    ".env.example",
    ".env.template",
    ".env.sample",
    ".env.test.example",
)


# ---------------------------------------------------------------------------
# Build and runtime
# ---------------------------------------------------------------------------

#: The toolchains a checkout can be built with -- manifest plus package manager, because
#: "node" alone does not say which lockfile to honour.
TOOLCHAINS: frozenset[str] = frozenset(
    {
        "node-npm",
        "node-yarn",
        "node-pnpm",
        "python",
        "python-venv",
        "go-modules",
        "maven",
        "gradle",
        "cargo",
        "bundler",
        "composer",
        "dotnet",
    }
)

#: Runtimes whose version a project is able to pin in its own configuration.
RUNTIME_LANES: frozenset[str] = frozenset(
    {
        "node",
        "python",
        "go",
        "rust",
        "ruby",
        "java",
        "dotnet",
    }
)


# ---------------------------------------------------------------------------
# Company identity
# ---------------------------------------------------------------------------

#: How sure the read is. `none` is a first-class answer: a checkout with no remote, generic
#: manifests and no copyright header names nobody, and saying so beats a guess.
COMPANY_OUTCOMES: tuple[str, ...] = ("confident", "uncertain", "none")

#: Where a name was read from, strongest first. It is provenance, not a score: nothing
#: multiplies these and no number derived from them is emitted.
SIGNAL_FAMILIES: tuple[str, ...] = (
    "licence_file",
    "copyright_header",
    "git_remote",
    "package_manifest",
)

#: Where an industry guess came from.
INDUSTRY_SIGNALS: tuple[str, ...] = ("readme", "dependencies")

#: This tool's own fixed set of industry labels, entirely independent of the repository's
#: own wording. The industry field can only ever hold one of these values or be empty --
#: never text copied out of a README.
INDUSTRIES: tuple[str, ...] = (
    "fintech",
    "healthcare",
    "insurance",
    "ecommerce",
    "logistics",
    "real_estate",
    "legal",
    "education",
    "gaming",
    "media",
    "travel",
    "telecom",
    "energy",
    "manufacturing",
    "automotive",
    "agriculture",
    "government",
    "security",
    "devtools",
    "data_infrastructure",
    "marketing",
    "hr_recruiting",
    "crm_sales",
    "productivity",
    "social",
    "crypto",
    "biotech",
)

# Defines what a company name is allowed to look like, which is also what keeps emitting
# one safe from a privacy standpoint. Length-capped and restricted to characters a real
# company name would actually contain, so nothing else can pass itself off as a name:
#
#   / \   looks like a filesystem path -- an absolute path is exactly the thing that must
#         never be emitted
#   @     looks like an address or a handle
#   :     looks like a URL, a scheme, or a host:port pair
#   _     looks like a snake_case identifier, i.e. a symbol rather than a name
#
# The rule doing the heavy lifting: a period may close out a word but may never sit between
# two letters joining them together. That lets "Acme Systems, Inc." through while rejecting
# `acme.com`, `com.acme.payments` and `payments_core.py` all with the same check. The
# allowed letters extend into the Latin-1 supplement so an accented European company name is
# preserved rather than garbled -- accented letters are neither paths nor addresses, so
# there is no privacy cost to allowing them.
_LETTER = "A-Za-zÀ-ÖØ-öø-ÿ"
_NAME_WORD = rf"[{_LETTER}0-9][{_LETTER}0-9&'-]{{0,39}}[.,]?"
#: A lone ampersand is a word of its own in "The Procter & Gamble Company", but may not
#: start a name: something beginning with punctuation is not a name.
COMPANY_NAME_PATTERN = rf"(?:{_NAME_WORD})(?: (?:{_NAME_WORD}|&)){{0,7}}"

#: The letter class the name pattern is built from, exposed because a copyright line's
#: entity has to be checked for a letter before it is believed: a "company" called 2024 is
#: the one bad guess the copyright family must never make.
COMPANY_NAME_LETTERS: str = _LETTER

#: Removed from the tail of a name before two spellings are matched against each other.
#: Suffixes denoting a legal entity, and nothing beyond that: discarding `inc` is what lets
#: "Acme Systems, Inc." from a licence meet `acme-systems` from a remote, whereas also
#: discarding `systems` would equate "Acme Systems" with "Acme Labs", which is somebody
#: else entirely.
COMPANY_LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "lllp",
        "llp",
        "lp",
        "ltd",
        "ltda",
        "limited",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "mbh",
        "ag",
        "kg",
        "sa",
        "sas",
        "sarl",
        "srl",
        "spa",
        "bv",
        "nv",
        "ab",
        "as",
        "asa",
        "oy",
        "oyj",
        "aps",
        "pty",
        "pte",
        "kk",
        "kft",
        "zoo",
        "doo",
        "ou",
        "sro",
        "ug",
    }
)

#: Names that are never the company whose repository this is, keyed on the normalised
#: spelling. Two kinds of thing, and both arrive the same way -- inside somebody else's
#: boilerplate rather than inside an ownership claim.
#:
#: Licence and language STEWARDS come with the licence text. GPLv3 opens with
#: "Copyright (C) 2007 Free Software Foundation, Inc.", so reading the first copyright line
#: of a licence file -- which is the right thing to do -- would otherwise attribute every
#: copyleft repository in the world to the same wrong company, and attribute it
#: `confident`, because the licence file and every vendored header would agree.
#:
#: PLACEHOLDERS come with the template. MIT and BSD ship a slot where the name goes, and a
#: repository that never filled it in names nobody rather than naming "Your Company Name".
NOT_A_COMPANY: frozenset[str] = frozenset(
    {
        # Stewards.
        "freesoftwarefoundation",
        "fsf",
        "pythonsoftwarefoundation",
        "apachesoftwarefoundation",
        "openjsfoundation",
        "eclipsefoundation",
        "linuxfoundation",
        "thelinuxfoundation",
        "softwarefreedomconservancy",
        "unicodeconsortium",
        "theunicodeconsortium",
        "rustprojectdevelopers",
        "therustprojectdevelopers",
        "goauthors",
        "thegoauthors",
        "nodejscontributors",
        "openssfoundation",
        "openssl",
        "opensslproject",
        "regentsoftheuniversityofcalifornia",
        "universityofcalifornia",
        "mit",
        # Names standing in for a group, or for whoever forgot to edit the template.
        #
        # These are matched against a NORMALISED key, and normalising strips legal-entity
        # suffixes from the tail -- `company` being one of them. So an unedited "Your Company,
        # Inc." keys to `your`, not to `yourcompany`, and is NOT caught here: it survives as a
        # candidate spelled the way the licence file spelled it. `companyname` and
        # `yournamehere` keep their whole key and are caught. Widening the list to cover the
        # stripped forms would suppress candidates this tool has always emitted, so the entries
        # below stay as they are; the person confirming the result is the one who recognises a
        # template that was never filled in.
        "authors",
        "theauthors",
        "contributors",
        "thecontributors",
        "projectcontributors",
        "theproject",
        "project",
        "copyrightholder",
        "copyrightholders",
        "owner",
        "theowner",
        "author",
        "me",
        "myself",
        "you",
        "yourname",
        "yournamehere",
        "companyname",
        "yourcompany",
        "yourcompanyname",
        "nameofcopyrightowner",
        "opensource",
        "opensourcecommunity",
        "everyone",
        "anonymous",
        "developers",
        "thedevelopers",
        "maintainers",
        "themaintainers",
        "team",
        "theteam",
        "community",
        "thecommunity",
    }
)

#: Once a candidate name is agreed on, this decides whose exact wording of it gets shown --
#: a separate question from `SIGNAL_FAMILIES`'s strength ranking, and deliberately answered
#: differently: it favours whichever source spells the name the way a person would, since a
#: licence file tends to carry the full registered name while a remote URL only has a slug.
COMPANY_DISPLAY_PREFERENCE: tuple[str, ...] = (
    "licence_file",
    "copyright_header",
    "package_manifest",
    "git_remote",
)

#: Deleted from a copyright line before the entity is read -- the stock tail that follows a
#: name rather than being part of one.
RIGHTS_NOISE_RE: re.Pattern[str] = re.compile(
    r"\ball rights? reserved\b|\bsome rights reserved\b|\band contributors?\b"
    r"|\band others\b|\bet al\.?",
    re.IGNORECASE,
)

# Matches a genuine copyright NOTICE, a much narrower target than every place the word
# "copyright" happens to occur in a document. Two separate requirements, both added because
# of specific false positives seen in real licence text:
#
#   1. The match has to START the line, with at most a short run of comment syntax ahead of
#      it (`#`, `//`, `/*`, ` *`). A genuine notice is written on a line of its own, whereas
#      an incidental mention of the word shows up mid-sentence -- the Apache-2.0 definitions
#      section, for instance, contains "the copyright owner or entity authorized by the
#      copyright owner", and without anchoring to line-start, that phrase alone would get a
#      huge number of Apache-licensed repositories reporting a company literally named
#      "owner or entity authorized by".
#   2. A year, or a `(c)`/`(C)` marker, has to appear too, checked through the separate
#      `marker` group. A real notice always carries one of these; CC0's opening line,
#      "Copyright and Related Rights ...", does not. The cost of requiring it is missing a
#      bare "Copyright Acme Systems" with no year attached, in exchange for not matching
#      every document whose body happens to start with the word "Copyright".
COPYRIGHT_NOTICE_RE: re.Pattern[str] = re.compile(
    r"^[^\w\n]{0,24}copyright\b"
    r"(?P<marker>[\s.:]*(?:\(c\)|\(co\)|©|&copy;)?[\s.:]*"
    r"(?:\d{4}(?:\s*[-–—]\s*(?:\d{4}|present))?[\s,;]*)*"  # any run of years
    r"(?:\(c\)|©)?[\s,;]*)"
    r"(?:by\s+)?"
    r"(?P<entity>[^\n]{2,160})",
    re.IGNORECASE | re.MULTILINE,
)

#: The second guard above, applied to whatever the `marker` group swallowed.
YEAR_OR_MARKER_RE: re.Pattern[str] = re.compile(r"\d{4}|\(c\)|\(co\)|©|&copy;", re.IGNORECASE)

#: Directories `identity.py` never descends into. This list is broader than a typical
#: build-output exclusion list for one specific reason: a vendored dependency tree is the
#: real risk for the copyright-header signal, since the header inside a bundled dependency
#: names that dependency's own author, and mistaking that for this repository's owner would
#: be a confident, specific wrong answer. Kept separate from `SKIP_DIRS`, which serves a
#: different walk with a different purpose and does not exclude `third_party` or `Pods`.
IDENTITY_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "vendored",
        "third_party",
        "thirdparty",
        "third-party",
        "external",
        "externals",
        "bower_components",
        "Pods",
        ".venv",
        "venv",
        "env",
        "site-packages",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".nuxt",
        ".tox",
        "__pycache__",
        ".gradle",
        ".idea",
        ".mypy_cache",
    }
)

#: Checked as a prefix of the upper-cased filename rather than requiring an exact stem
#: match, because real repositories spell this several ways: a dual-licensed project might
#: use `LICENSE-MIT` or `LICENSE-APACHE`, and an LGPL project might use `COPYING.LESSER`.
LICENCE_FILE_PREFIXES: tuple[str, ...] = ("LICENSE", "LICENCE", "NOTICE", "COPYING", "COPYRIGHT")

#: The files a copyright header can be in. Wider than `CODE_EXTENSIONS` in some places and
#: narrower in others because it answers a different question: not "is this code we count"
#: but "is this a file whose first four kilobytes might carry a notice".
IDENTITY_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".scala",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".m",
        ".mm",
        ".sh",
        ".sql",
        ".tf",
        ".vue",
        ".svelte",
        ".ex",
        ".exs",
        ".erl",
        ".clj",
        ".groovy",
        ".pl",
    }
)

#: Elements of a `pom.xml` that describe an identity belonging to someone other than the
#: project itself, stripped out before this parser reads the project's own coordinates. The
#: most important is `parent`: Maven lists it before the project's own coordinates, so a
#: parser that just grabs the first `<groupId>` in a Spring Boot POM would attribute the
#: project to `org.springframework.boot` -- the framework, not the author. `developers` and
#: `contributors` describe people, and a person's `<organization>` element names their
#: employer rather than the project's owner. A `<relocation>` nested under
#: `distributionManagement` names the coordinates an artifact moved away from, not toward.
POM_FOREIGN_BLOCKS: tuple[str, ...] = (
    "parent",
    "dependencies",
    "dependencyManagement",
    "plugins",
    "pluginManagement",
    "profiles",
    "build",
    "reporting",
    "modules",
    "extensions",
    "developers",
    "contributors",
    "distributionManagement",
)

#: A groupId is only reverse-domain when it STARTS with one of these, and only then is its
#: second segment the company. `com.acme.payments` -> `acme`. Anything else is a namespace
#: whose shape we cannot read, and guessing which part of it is the company would be
#: inventing an answer.
GROUP_ID_DOMAIN_WORDS: frozenset[str] = frozenset(
    {
        "com",
        "org",
        "net",
        "io",
        "co",
        "dev",
        "ai",
        "app",
        "cloud",
        "eu",
        "uk",
        "de",
    }
)

#: Industry keywords matched against the README text at word boundaries, using words
#: distinctive enough to be meaningful. Even so, one match alone is never trusted to name an
#: industry by itself -- at least two separate hits are required before this table counts.
INDUSTRY_README_WORDS: dict[str, tuple[str, ...]] = {
    "fintech": (
        "payments",
        "payment",
        "invoicing",
        "invoices",
        "ledger",
        "banking",
        "lending",
        "underwriting",
        "brokerage",
        "kyc",
        "aml",
        "payroll",
        "settlement",
        "chargeback",
    ),
    "healthcare": (
        "patient",
        "patients",
        "clinical",
        "ehr",
        "emr",
        "fhir",
        "hl7",
        "diagnosis",
        "prescription",
        "telehealth",
        "hipaa",
    ),
    "insurance": ("policyholder", "underwriter", "claims", "actuarial", "premiums", "reinsurance"),
    "ecommerce": (
        "checkout",
        "storefront",
        "cart",
        "catalogue",
        "catalog",
        "sku",
        "merchandising",
        "fulfilment",
        "fulfillment",
    ),
    "logistics": (
        "shipment",
        "shipments",
        "freight",
        "warehouse",
        "dispatch",
        "courier",
        "tracking",
        "carrier",
        "fleet",
    ),
    "real_estate": (
        "listings",
        "property",
        "properties",
        "tenant",
        "landlord",
        "mortgage",
        "escrow",
        "leasing",
    ),
    "legal": (
        "contract",
        "contracts",
        "clause",
        "litigation",
        "counsel",
        "docket",
        "paralegal",
        "compliance",
    ),
    "education": (
        "student",
        "students",
        "curriculum",
        "courses",
        "enrolment",
        "enrollment",
        "grading",
        "classroom",
        "lms",
    ),
    "gaming": (
        "gameplay",
        "player",
        "players",
        "multiplayer",
        "matchmaking",
        "leaderboard",
        "sprites",
        "shader",
    ),
    "media": (
        "streaming",
        "playback",
        "subtitles",
        "transcoding",
        "editorial",
        "publishing",
        "podcast",
    ),
    "travel": (
        "booking",
        "bookings",
        "itinerary",
        "flights",
        "hotels",
        "reservations",
        "traveller",
        "traveler",
    ),
    "telecom": ("subscriber", "roaming", "sim", "provisioning", "voip", "sms", "lte"),
    "energy": (
        "grid",
        "metering",
        "kwh",
        "turbine",
        "solar",
        "photovoltaic",
        "emissions",
        "utility",
    ),
    "manufacturing": ("assembly", "factory", "shopfloor", "mes", "bom", "tooling", "throughput"),
    "automotive": (
        "vehicle",
        "vehicles",
        "telematics",
        "adas",
        "can bus",
        "powertrain",
        "dealership",
    ),
    "agriculture": ("crop", "crops", "harvest", "irrigation", "agronomy", "livestock", "yield"),
    "government": ("citizen", "municipal", "procurement", "permits", "constituency", "regulatory"),
    "security": (
        "vulnerability",
        "vulnerabilities",
        "malware",
        "intrusion",
        "siem",
        "threat",
        "forensics",
        "pentest",
    ),
    "devtools": (
        "linter",
        "compiler",
        "sdk",
        "cli",
        "debugger",
        "scaffolding",
        "codegen",
        "boilerplate",
    ),
    "data_infrastructure": (
        "etl",
        "pipeline",
        "pipelines",
        "warehouse",
        "lakehouse",
        "ingestion",
        "orchestration",
        "dbt",
    ),
    "marketing": (
        "campaign",
        "campaigns",
        "attribution",
        "segmentation",
        "funnel",
        "impressions",
        "adtech",
        "newsletter",
    ),
    "hr_recruiting": (
        "candidate",
        "candidates",
        "applicant",
        "recruiter",
        "onboarding",
        "payroll",
        "sourcing",
        "ats",
    ),
    "crm_sales": ("leads", "pipeline", "quota", "opportunity", "opportunities", "prospect", "crm"),
    "productivity": (
        "workspace",
        "notes",
        "kanban",
        "calendar",
        "reminders",
        "todo",
        "collaboration",
    ),
    "social": ("followers", "feed", "timeline", "messaging", "profiles", "hashtag"),
    "crypto": (
        "blockchain",
        "wallet",
        "onchain",
        "smart contract",
        "staking",
        "defi",
        "tokenomics",
    ),
    "biotech": (
        "genome",
        "genomic",
        "sequencing",
        "assay",
        "molecule",
        "proteomics",
        "clinical trial",
    ),
}

#: Industry keywords matched as substrings of a declared dependency's name, which lets a Go
#: module path and an npm package name that both reference the same technology produce the
#: same signal. Every entry here, like the rest of this file's tables, names a public
#: technology; the industry field only ever emits the label on the left side, never
#: anything read directly out of the repository's own dependency list.
INDUSTRY_DEPENDENCY_WORDS: dict[str, tuple[str, ...]] = {
    "fintech": (
        "stripe",
        "plaid",
        "braintree",
        "adyen",
        "razorpay",
        "paypal",
        "quickbooks",
        "xero",
        "moneyed",
    ),
    "healthcare": ("fhir", "hl7", "pydicom", "dicom", "smart-on-fhir"),
    "ecommerce": ("shopify", "woocommerce", "magento", "bigcommerce", "medusa"),
    "logistics": ("easypost", "shippo", "aftership", "gtfs"),
    "gaming": ("unity", "godot", "pygame", "phaser", "bevy", "raylib"),
    "media": ("ffmpeg", "gstreamer", "hls", "shaka-player", "mux"),
    "telecom": ("twilio", "vonage", "asterisk", "kamailio"),
    "energy": ("pvlib", "modbus", "iec61850"),
    "security": ("yara", "scapy", "volatility", "semgrep", "trivy", "osquery"),
    "data_infrastructure": (
        "airflow",
        "dagster",
        "dbt-core",
        "kafka",
        "spark",
        "flink",
        "clickhouse",
        "duckdb",
    ),
    "marketing": ("segment", "mixpanel", "amplitude", "customerio", "mailchimp", "sendgrid"),
    "crm_sales": ("salesforce", "hubspot", "pipedrive"),
    "crypto": ("web3", "ethers", "solana", "wagmi", "viem", "bitcoinlib"),
    "biotech": ("biopython", "rdkit", "scanpy", "pysam", "anndata"),
    "devtools": ("tree-sitter", "libcst", "babel", "rollup", "esbuild"),
}


# ---------------------------------------------------------------------------
# Public technology names
# ---------------------------------------------------------------------------

#: Capitalised words that are ordinary technical English rather than somebody's product,
#: company or class. A capital letter away from the start of a sentence is otherwise a
#: refusal in `schema.Prose`, and without this list a note saying "parsed with tree-sitter
#: under PostgreSQL" would be dropped whole.
TECH_NAMES: frozenset[str] = frozenset(
    {
        "PostgreSQL",
        "MySQL",
        "MariaDB",
        "SQLite",
        "MongoDB",
        "DynamoDB",
        "ClickHouse",
        "BigQuery",
        "RedShift",
        "Snowflake",
        "ElasticSearch",
        "OpenSearch",
        "RabbitMQ",
        "GraphQL",
        "OpenAPI",
        "JavaScript",
        "TypeScript",
        "WebSocket",
        "WebAssembly",
        "PowerShell",
        "NoSQL",
        "OAuth",
        "GitHub",
        "GitLab",
        "BitBucket",
        "DevOps",
        "Kubernetes",
        "TensorFlow",
        "PyTorch",
        "NumPy",
        "SciPy",
        "DataDog",
        "NewRelic",
        "PagerDuty",
        "PayPal",
        "QuickBooks",
        "SalesForce",
        "Shopify",
        "Stripe",
        "Twilio",
        "SendGrid",
        "MacOS",
        "AppStore",
        "PlayStore",
        "AirFlow",
        "SpringBoot",
        "DjangoREST",
        "FastAPI",
        "NextJS",
        "NuxtJS",
        "SvelteKit",
        "DotNet",
        "AspNet",
        "Python",
        "Java",
        "Kotlin",
        "Swift",
        "Scala",
        "Rust",
        "Ruby",
        "React",
        "Angular",
        "Django",
        "Flask",
        "Fastapi",
        "Terraform",
        "Ansible",
        "Docker",
        "Maven",
        "Gradle",
    }
)


# ---------------------------------------------------------------------------
# Git history: authors, subjects and the file classes a commit is read through
# ---------------------------------------------------------------------------

#: Committer names and addresses recognised as automation rather than a human. The match
#: runs over "<name> <email>" combined into one string, so an entry here catches either
#: half. Entries whose bare word could also be a company name or a real surname (codecov,
#: vercel, netlify, sonarcloud, stale) are only matched via their `[bot]` suffix, so that a
#: person who genuinely works at Vercel and commits from a `@vercel.com` address is not
#: mistaken for automation.
BOT_NAME_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"\[bot\]",
        r"\bdependabot\b",
        r"\brenovate\b",
        r"\bsnyk-bot\b",
        r"\bgithub-actions\b",
        r"\bactions-user\b",
        r"\bmergify\b",
        r"\bpre-commit-ci\b",
        r"\bwhitesource\b",
        r"\bgreenkeeper\b",
        r"\bimgbot\b",
        r"\ballcontributors\b",
        r"\brestyled\b",
        r"\bscala-steward\b",
        r"\bpyup-bot\b",
        r"\bdepfu\b",
        r"\btravis-ci\b",
        r"\bcircleci\b",
        r"\bbuildkite\b",
        r"\bsemantic-release\b",
        r"\banonymi[sz]er\b",
        r"\bjenkins[-_. ]?(ci|bot|build|builder|agent|server)\b",
        r"\bbot\b",
    ]
]

#: The Conventional Commits subject shape. Only the RATE is ever emitted; no subject is.
CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)(\([^)]+\))?!?:\s",
    re.I,
)

#: The signature of the patch commit an anonymised delivery appends. It is not project
#: history, so history statistics are measured from its parent instead.
ANON_EMAIL_DOMAINS: tuple[str, ...] = ("anonymizer.local",)
ANON_NAME_RE = re.compile(r"anonymiz", re.I)
ANON_SUBJECT_RE = re.compile(r"\banonymiz\w*\s+(repo|repository|code)\b", re.I)

#: Subjects that say "this is not substantive engineering work" whatever the diff looks
#: like. Without them a dependency bump that happens to touch two code files is counted
#: as an atomic feature.
NOISE_SUBJECT_RE = re.compile(
    r"^(chore|revert|wip|bump|deps?|dependabot|lint|style|format|prettier|typo|"
    r"merge|release|version|removed?|cleanup|cleanups?|delete|remove|rename|"
    r"upgrade|update\s+(deps|dep|dependenc|package|lock))[\s:(\[]",
    re.I,
)

#: Subjects that announce a feature. Bare "test" is deliberately excluded -- it matches
#: noise like "test push" -- so only the conventional `test:` / `test(scope)` forms count.
FEATURE_SUBJECT_RE = re.compile(
    r"^(feat|add|implement|introduce|support)\b|^test[:(]",
    re.I,
)

#: Subjects that announce a repair. A NECESSARY condition only: the diff has to
#: corroborate the claim, because under the Bugzilla convention every subject in a
#: repository opens "Bug <id> - ...". The inflections are deliberate: `\b` after `fix`
#: refused "Fixed crash in parser" and "Fixes #123".
BUGFIX_SUBJECT_RE = re.compile(
    r"^(fix(e[sd])?|hotfix|bugfix|bug|regression|revert(s|ed)?|patch(es|ed)?|"
    r"resolve[sd]?|correct(s|ed)?)\b|"
    r"^(fix|revert)[:(]",
    re.I,
)

#: Test SPECS. Distinct from `TEST_PATTERNS` above, which answers a different question
#: about a working tree; this one reads the paths inside one commit.
TEST_FILE_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$",
        r"(^|/)(test_|tests/)",
        r"_test\.(go|py|rb)$",
        r"(^|/)__tests__/",
        r"(^|/)spec/",
        r"\.spec\.rb$",
        r"Test\.(java|kt|cs)$",
        r"Tests\.(java|kt|cs)$",
    ]
]

#: Test INFRASTRUCTURE: runners, fixtures, helpers and mocks. Neither specs nor
#: implementation, so a commit adding nine specs and two runner configs is not read as
#: an implementation change.
TEST_INFRA_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)vitest[^/]*\.config\.(ts|js|mjs)$",
        r"(^|/)jest\.config\.(ts|js|mjs|cjs|json)$",
        r"(^|/)playwright\.config\.(ts|js|mjs)$",
        r"(^|/)cypress\.config\.(ts|js)$",
        r"(^|/)karma\.conf\.(ts|js)$",
        r"(^|/)tests?/setup/",
        r"(^|/)tests?/fixtures/",
        r"(^|/)tests?/helpers/",
        r"(^|/)tests?/__mocks__/",
        r"(^|/)tests?/conftest\.py$",
        r"(^|/)conftest\.py$",
        r"(^|/)pytest\.ini$",
        r"(^|/)tox\.ini$",
        r"(^|/)\.mocharc\.(js|cjs|json|yml|yaml)$",
    ]
]

#: Data-shape files. Tagged for the schema signal and still counted as implementation.
SCHEMA_FILE_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)schemas?/",
        r"(^|/)types?/",
        r"\.schema\.(ts|js|py)$",
        r"(^|/)migrations?/",
        r"(^|/)drizzle/",
        r"(^|/)prisma/",
        r"\.proto$",
        r"openapi\.(ya?ml|json)$",
    ]
]

#: Prose and configuration. Never implementation, so a lockfile refresh cannot carry a
#: commit into a feature class on its own.
DOCS_OR_CONFIG_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"\.md$",
        r"^\.github/",
        r"^\.gitlab/",
        r"\.ya?ml$",
        r"^Dockerfile",
        r"package(-lock)?\.json$",
        r"pnpm-lock\.yaml$",
        r"yarn\.lock$",
        r"poetry\.lock$",
        r"Cargo\.lock$",
    ]
]


# ---------------------------------------------------------------------------
# Git history: the coarse path classes a *mineable* commit is measured through
# ---------------------------------------------------------------------------
#
# Deliberately coarser than `TEST_FILE_PATTERNS` and friends above. Those read one
# commit to decide which feature shape it has; these two answer a much blunter question
# -- is this path first-party implementation at all -- for the depth measurement, where a
# commit counts as mineable when it touches at least two implementation files with 20 to
# 10,000 lines of churn. Keeping the two sets apart is what stops a refinement to the
# feature classes silently moving a capacity number computed from a different table.

#: Paths whose churn is not development: dependency trees and build output, the lockfiles
#: a resolver writes, and the binary, minified or generated artefacts that get committed
#: beside real source. A commit touching only these is real history but not mineable work.
NON_IMPL_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)(node_modules|vendor|third_party|dist|build|out|target|\.git|"
        r"__pycache__|\.venv|venv|site-packages)/",
        r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Gemfile\.lock|"
        r"composer\.lock|Cargo\.lock|go\.sum|requirements\.txt\.lock)$",
        r"\.(min\.js|min\.css|map|snap|lock|svg|png|jpe?g|gif|ico|woff2?|ttf|eot|pdf|mp4)$",
    ]
]

#: Tests, for the depth measurement only. A test file still counts towards a commit's
#: churn -- writing one is work -- but it is not one of the implementation files a
#: mineable commit has to touch two of, or every test-only sweep would read as
#: development.
HISTORY_TEST_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)(tests?|spec|__tests__|e2e)/",
        r"(^|/)(test_|conftest)",
        r"[._-](test|spec)\.[a-z0-9]+$",
    ]
]


# ---------------------------------------------------------------------------
# Code structure: what a parser is pointed at, and what it is looking for
# ---------------------------------------------------------------------------
#
# Five tables and six patterns, pinned value by value in `tests/test_structure.py` so that
# widening one is always a deliberate edit. They are wide on purpose. `source_files == 0`
# and a low `prod_loc` are categorical judgements downstream, so a language missing from
# the extension map does not shade a repository's numbers down -- it reports a tree full of
# code as holding none. Erring wide costs a little precision in the attribution and buys
# the difference between "no code" and "a language we had not heard of".

#: File extension -> the tree-sitter grammar that parses it. Wider than
#: `LANGUAGE_BY_EXT` above and answering a different question: that table names a language
#: for a line count, this one names a parser. Data, markup, config, schema and IDL files
#: are absent from both, and so are the build and infrastructure DSLs -- configuration
#: written in a grammar is still configuration, and counting it would defeat the
#: zero-code reading it should be producing.
STRUCTURE_EXT_LANG: dict[str, str] = {
    # the mainstream
    ".py": "python",
    ".pyw": "python",
    ".pyi": "python",
    ".ipynb": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".es6": "javascript",
    ".gs": "javascript",  # Apps Script is JavaScript underneath
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".php": "php",
    ".phtml": "php",
    ".cs": "c_sharp",
    ".csx": "c_sharp",
    ".rs": "rust",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",  # headers stay C; inferring C++ costs more
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".ipp": "cpp",
    ".tpp": "cpp",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".ino": "arduino",
    ".mm": "objc",  # Objective-C++, close enough to the objc grammar
    ".lua": "lua",
    ".luau": "luau",
    ".dart": "dart",
    # BEAM and functional
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".escript": "erlang",
    ".gleam": "gleam",
    ".elm": "elm",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".purs": "purescript",
    ".ml": "ocaml",
    ".mli": "ocaml_interface",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".fsi": "fsharp_signature",
    ".sml": "sml",
    ".idr": "idris",
    ".lidr": "idris",
    ".agda": "agda",
    ".lagda": "agda",
    ".lean": "lean",
    ".roc": "roc",
    ".gren": "gren",
    # lisps. They parse, but an s-expression grammar has no function node, so they
    # contribute files and lines and no attribution -- see STRUCTURE_FUNCTION_NODES.
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".lisp": "commonlisp",
    ".lsp": "commonlisp",
    ".cl": "commonlisp",
    ".asd": "commonlisp",
    ".el": "elisp",
    ".scm": "scheme",
    ".ss": "scheme",
    ".sld": "scheme",
    ".rkt": "racket",
    ".fnl": "fennel",
    ".janet": "janet",
    ".hoon": "hoon",
    # scientific and statistical
    ".jl": "julia",
    ".r": "r",
    ".stan": "stan",
    ".f90": "fortran",
    ".f95": "fortran",
    ".f03": "fortran",
    ".f08": "fortran",
    ".f": "fortran",
    ".for": "fortran",
    ".f77": "fortran",
    ".ftn": "fortran",
    ".fpp": "fortran",
    # the legacy enterprise tail
    ".cbl": "cobol",
    ".cob": "cobol",
    ".cpy": "cobol",
    ".ada": "ada",
    ".adb": "ada",
    ".ads": "ada",
    ".pas": "pascal",
    ".pp": "pascal",
    ".dpr": "pascal",
    ".dpk": "pascal",
    ".lpr": "pascal",
    ".vb": "vb",
    ".vbs": "vb",
    ".bas": "vb",
    ".cls": "apex",
    ".trigger": "apex",
    ".apex": "apex",  # Salesforce, not Progress ABL
    ".bsl": "bsl",
    ".magik": "magik",
    ".cfc": "cfml",
    ".cfm": "cfml",
    # scripting
    ".pm": "perl",
    ".perl": "perl",  # .pl is ambiguous; see AMBIGUOUS_EXT
    ".prolog": "prolog",
    ".groovy": "groovy",
    ".gvy": "groovy",
    ".tcl": "tcl",
    ".tk": "tcl",
    ".awk": "awk",
    ".vim": "vim",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".sh": "bash",
    ".bash": "bash",
    ".ksh": "bash",
    ".csh": "bash",
    ".tcsh": "bash",
    ".zsh": "zsh",
    ".fish": "fish",
    ".nu": "nushell",
    ".elv": "elvish",
    ".bat": "batch",
    ".cmd": "batch",
    ".jq": "jq",
    ".rego": "rego",
    ".prql": "prql",
    ".ql": "ql",
    ".qll": "ql",
    ".mojo": "mojo",
    # systems
    ".nim": "nim",
    ".nims": "nim",
    ".nimble": "nim",
    ".zig": "zig",
    ".cr": "crystal",
    ".d": "d",
    ".odin": "odin",
    ".hx": "haxe",
    ".hack": "hack",
    ".hhi": "hack",
    ".pony": "pony",
    ".jai": "jai",
    ".ha": "hare",
    ".c3": "c3",
    ".nut": "squirrel",
    ".ck": "chuck",
    ".gd": "gdscript",
    ".as": "actionscript",
    ".brs": "brightscript",
    ".res": "rescript",
    ".resi": "rescript",
    ".smali": "smali",
    ".ll": "llvm",
    ".mlir": "mlir",
    ".wat": "wat",
    ".wast": "wast",
    ".tal": "uxntal",
    ".s": "asm",
    ".asm": "nasm",
    ".nasm": "nasm",
    ".masm": "x86asm",
    # hardware and shaders
    ".vhd": "vhdl",
    ".vhdl": "vhdl",
    ".vh": "verilog",
    ".sv": "systemverilog",
    ".svh": "systemverilog",
    ".glsl": "glsl",
    ".vert": "glsl",
    ".frag": "glsl",
    ".geom": "glsl",
    ".comp": "glsl",
    ".tesc": "glsl",
    ".tese": "glsl",
    ".hlsl": "hlsl",
    ".fx": "hlsl",
    ".wgsl": "wgsl",
    ".scad": "openscad",
    # smart contracts
    ".sol": "solidity",
    ".move": "move",
    ".cairo": "cairo",
    ".clar": "clarity",
    ".tact": "tact",
    ".sw": "sway",
    ".fc": "func",
    ".circom": "circom",
    # SQL. Stored procedures and PL/SQL packages are logic, not a data format.
    ".sql": "sql",
    ".pls": "sql",
    ".plsql": "sql",
    ".pks": "sql",
    ".pkb": "sql",
    ".prc": "sql",
    ".fnc": "sql",
    ".tsql": "sql",
    ".ddl": "sql",
    # single-file component formats. Their grammars treat the embedded script block as
    # opaque text rather than parsing into it, so none of these can ever contribute a
    # function boundary -- yet a Vue or Svelte file is genuinely full of code, so leaving
    # these extensions out of the map entirely would make such a codebase look like it
    # holds none.
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".razor": "razor",
    # odds and ends
    ".st": "smalltalk",
    ".forth": "forth",
    ".fth": "forth",
    ".4th": "forth",
    ".dl": "souffle",
}

#: Real programming languages the pinned parser pack has no grammar for. They are counted
#: as source -- files, lines and test files -- and never handed to a parser, and the
#: subset is reported on its own so a reader can see how much of a tree the concentration
#: figure was actually computed over. Omitting them instead would not make their
#: repositories score badly; it would make them score zero by rule.
STRUCTURE_EXT_UNPARSED: frozenset[str] = frozenset(
    {
        # the mainframe languages: ABAP, PL/I and what follows them
        ".abap",
        ".pli",
        ".pl1",
        ".rexx",
        ".rex",
        ".jcl",
        # toolchain languages from Apple and GNOME, none of which the pack parses
        ".applescript",
        ".vala",
        ".vapi",
        ".genie",
        ".metal",
        # statistics and numeric-computing languages: Stata, J, the kdb+ family, Wolfram.
        # Mercury is left out on purpose -- it shares the `.m` extension with MATLAB and
        # Objective-C, and adding a third candidate to disambiguate would cost MATLAB
        # detection accuracy for a language that shows up far less often in practice.
        ".sas",
        ".do",
        ".ado",
        ".mata",
        ".ijs",
        ".apl",
        ".aplf",
        ".dyalog",
        ".k",
        ".q",
        ".gms",
        ".wl",
        ".wls",
        # descendants of ML, plus dependent types, all beyond the pack's reach
        ".re",
        ".rei",
        ".dats",
        ".sats",
        ".curry",
        ".frege",
        ".mcr",
        # HPC and array languages
        ".chpl",
        ".fut",
        ".cuf",
        ".upc",
        # the object-oriented tail
        ".e",
        ".eiffel",
        ".m3",
        ".i3",
        ".ob2",
        ".obn",
        ".mod2",
        ".boo",
        ".cobra",
        ".ceylon",
        ".xtend",
        ".dylan",
        ".factor",
        ".io",
        ".wren",
        ".pike",
        ".icn",
        ".nial",
        # scripting languages with no grammar in the pack
        ".ahk",
        ".ahk2",
        ".coffee",
        ".litcoffee",
        ".ls",
        ".bal",
        ".rsc",
        ".moon",
        ".ring",
        ".sed",
        ".expect",
        ".4gl",
        ".p4",
        # hardware description outside the pack
        ".vams",
        ".sva",
        ".ucf",
        ".pcf",
        ".e2",
        ".asm51",
        # recent arrivals whose grammars have yet to appear
        ".mojopkg",
        ".carbon",
        ".val",
        ".vine",
        ".slint",
        ".gleam_ffi",
    }
)

#: Grammar node type names that represent a branch, a loop or another guarded path -- in
#: other words, one decision point. This set is intentionally generous and drawn straight
#: from each grammar's own vocabulary rather than guessed at, because grammars name the same
#: construct differently from one another. Counting one construct too many, uniformly across
#: a whole language, is harmless here since the downstream metric is a proportion and a
#: constant multiplier cancels out of a ratio; missing a real construct is the mistake that
#: actually skews the number, which is why the set errs toward inclusion. Deliberately
#: excluded are names that merely contain a decision-sounding keyword without representing
#: an actual branch -- a type-level spelling, the C preprocessor's `#if`, SQL's
#: `keyword_case` -- since counting those would inflate one language's figure relative to
#: how its function bodies are actually measured.
STRUCTURE_DECISION_NODES: frozenset[str] = frozenset(
    {
        # the C-family and English core
        "if_statement",
        "if_expression",
        "if",
        "elif_clause",
        "elsif",
        "else_clause",
        "unless",
        "for_statement",
        "for_in_statement",
        "for_of_statement",
        "for_expression",
        "for",
        "while_statement",
        "while_expression",
        "while",
        "do_statement",
        "loop_expression",
        "switch_statement",
        "switch_expression",
        "case_statement",
        "case",
        "when_clause",
        "when",
        "match_statement",
        "match_expression",
        "catch_clause",
        "except_clause",
        "rescue",
        "conditional_expression",
        "ternary_expression",
        "boolean_operator",
        "guard_statement",
        "select_statement",
        "type_switch_statement",
        "try_statement",
        # branch, in other grammars' spellings
        "if_expr",
        "if_clause",
        "if_stmt",
        "if_block",
        "if_then_else",
        "if_else_expr",
        "exp_if",
        "if_case_statement",
        "multi_way_if",
        "single_line_if",
        "multi_line_if",
        "cond_exp",
        "conditional",
        "conditional_statement",
        "conditional_declaration",
        "static_if_statement",
        "elif",
        "elif_expression",
        "elseif",
        "elseif_clause",
        "elseif_statement",
        "elseif_block",
        "else_if_clause",
        "else_if_statement",
        "else_if_expr",
        "else_if",
        "if_header",
        "elsif_statement_item",
        "elsif_expression_item",
        "else_statement",
        "else_block",
        "else_part",
        "else_expression",
        "else_if_header",
        "unless_modifier",
        "if_modifier",
        "modifier_if",
        "modifier_unless",
        "arithmetic_if_statement",
        # multi-way dispatch
        "switch_stmt",
        "switch",
        "switch_case",
        "switch_default",
        "switch_entry",
        "switch_match",
        "case_clause",
        "case_expression",
        "case_expr",
        "case_exp",
        "case_item",
        "case_stmt",
        "case_of_expr",
        "case_of_branch",
        "case_pattern",
        "case_match",
        "case_else_block",
        "match",
        "match_arm",
        "match_block",
        "match_case",
        "match_alt",
        "match_branch",
        "match_pattern",
        "match_expr",
        "when_expression",
        "when_entry",
        "when_statement",
        "when_is_expr",
        "when_other",
        "select_case_statement",
        "select_expression",
        "evaluate_header",
        "case_statement_alternative",
        "case_expression_alternative",
        "select_type_statement",
        "ofBranch",
        "caseStmt",
        "SwitchExpr",
        "SwitchProng",
        # iteration
        "for_stmt",
        "for_expr",
        "for_clause",
        "for_loop",
        "for_block",
        "for_in_clause",
        "for_range_loop",
        "foreach",
        "foreach_statement",
        "foreach_stmt",
        "for_each",
        "for_each_statement",
        "for_each_in_statement",
        "for_each_loop",
        "enhanced_for_statement",
        "for_generic_clause",
        "for_numeric_clause",
        "generic_for_statement",
        "numeric_for_statement",
        "cstyle_for_statement",
        "c_style_for_statement",
        "search_statement",
        "for_generate_statement",
        "loop_statement",
        "loop",
        "do_loop",
        "do_while_statement",
        "do_while_expression",
        "do_until_statement",
        "repeat_statement",
        "repeat",
        "until",
        "until_statement",
        "while_stmt",
        "while_loop",
        "repeat_while_statement",
        "iterate_statement",
        "iteration_scheme",
        "while_modifier",
        "until_modifier",
        "do_stmt",
        "do_group",
        "perform_statement_loop",
        "perform_varying",
        "comprehension_for",
        "comprehension_if",
        "forStmt",
        "whileStmt",
        "ForStatement",
        "WhileStatement",
        "LoopStatement",
        # failure paths
        "try_expression",
        "try_expr",
        "try_block",
        "try_catch",
        "try",
        "catch",
        "catch_block",
        "catch_statement",
        "catch_expr",
        "rescue_block",
        "rescue_modifier",
        "modifier_rescue",
        "ensure",
        "finally_clause",
        "except_group_clause",
        "seh_try_statement",
        "seh_except_clause",
        "catch_unwrap",
        "try_unwrap",
        "scope_guard_statement",
        "tryStmt",
        "tryExceptStmt",
        # guards
        "guard",
        "guard_clause",
        "guard_pattern",
        "pattern_guard",
        "if_guard",
        "unless_guard",
        "guard_equation",
        "match_guard",
        "conditional_execution",
        # shell and Nushell control words
        "ctrl_if",
        "ctrl_match",
        "ctrl_for",
        "ctrl_while",
        "ctrl_loop",
        "ctrl_try",
        "IfStatement",
        "ifStmt",
        "elifStmt",
        "elseStmt",
        "inlineIfStmt",
        "inlineTryStmt",
    }
)

#: Determines which node type a decision is attributed to as its enclosing function. This
#: table is deliberately kept narrower than the decision table: a node name that would match
#: something nested inside a function -- a bare `block`, a `*_body` or `*_type` spelling --
#: is left out, because matching it would pull decisions away from the actual function and
#: fragment the counts. A language is only listed here if it also has entries in the
#: decision table; giving a language function nodes here while its branches stay invisible
#: to that table would flood the results with functions counted as having zero decisions and
#: distort the density figures with nothing real behind them. That constraint is exactly why
#: the s-expression languages, the single-file component formats, and grammars with no real
#: statement structure show up in the extension-to-language map but not in this table: their
#: files and their line counts are still measured, just not their structural complexity.
STRUCTURE_FUNCTION_NODES: frozenset[str] = frozenset(
    {
        # the C-family and English core
        "function_definition",
        "function_declaration",
        "function_item",
        "function_expression",
        "method_definition",
        "method_declaration",
        "method",
        "constructor_declaration",
        "arrow_function",
        "func_literal",
        "lambda",
        "singleton_method",
        "local_function_statement",
        # anonymous and first-class forms
        "lambda_expression",
        "lambda_literal",
        "anonymous_function",
        "anonymous_function_expr",
        "anonymous_method_expression",
        "closure_expression",
        "fun_expression",
        "exp_lambda",
        "lambda_case",
        "function_literal",
        "closure",
        "block_argument",
        "anon_fun_expr",
        # named definitions, other spellings
        "function",
        "function_statement",
        "function_signature",
        "procedure",
        "procedure_declaration",
        "subroutine",
        "subroutine_subprogram",
        "function_subprogram",
        "subprogram_body",
        "entry_body",
        "expression_function_declaration",
        "fun_decl",
        "fun_dec",
        "func_def",
        "funcdef",
        "function_or_value_defn",
        "member_defn",
        "method_or_prop_defn",
        "subroutine_declaration_statement",
        "let_binding",
        "rule",
        "monotonic_rule",
        "decl_def",
        "routine",
        "Decl",
        "def",
        "module_field_func",
        "defProc",
        "method_def",
        "value_declaration",
        "func_definition",
        "global_function",
        "storage_function",
        "native_function",
        "receive_function",
        # In Dart's grammar, the signature and the body sit as siblings under the
        # declaration node rather than one containing the other, so only the body's own
        # span can hold decision counts. Kotlin, Swift, D and Solidity structure it
        # differently -- the body node nests inside the definition node -- which turns the
        # outer definition into an empty wrapper and shifts attribution down to the inner
        # body instead. Either arrangement ends up counting decisions against the body,
        # since that is where they actually occur.
        "function_body",
        # blocks that carry the behaviour in the languages that declare them
        "always_construct",
        "initial_construct",
        "task_declaration",
        "process_statement",
    }
)

#: Covers the cases the walk's two general structural rules cannot resolve on their own: a
#: node name that is the actual function definition in one language but a nested,
#: non-leaf piece of it in another, where the outer node genuinely holds decisions of its
#: own and so does not qualify as an empty wrapper either. For those specific languages,
#: explicitly excluding the inner name is the only remaining way to avoid opening a second,
#: empty function entry for every real one. This list is short because each entry was
#: confirmed by walking a sample snippet through that language's actual grammar, not
#: guessed from the node names alone.
STRUCTURE_FUNCTION_NODES_EXCLUDED: dict[str, frozenset[str]] = {
    "systemverilog": frozenset({"function", "function_statement"}),
    "perl": frozenset({"function"}),
    "jai": frozenset({"procedure"}),
}

#: Elixir's grammar has no dedicated node type for either a function definition or a
#: branch, because `def`, `if` and `case` are all macros there, and the parser represents
#: every one of them as a plain `call` node. Nothing in the tree is literally named
#: "function" or "if", so without special handling a real Elixir module would parse as
#: though it contained neither -- a widely used language scoring as though its tree were
#: empty. Checking the word at the head of each `call` node recovers both categories. This
#: table is keyed per language specifically so no other grammar pays for the extra lookup,
#: and each entry is a pair: the head words that mark a function, and the head words that
#: mark a decision.
STRUCTURE_CALL_HEADS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "elixir": (
        frozenset({"def", "defp", "defmacro", "defmacrop"}),
        frozenset({"if", "unless", "case", "cond", "for", "with", "try", "receive"}),
    ),
}

#: Directories whose contents are not the repository's own work. Anchored on `/` and
#: matched against a posix-style relative path, because a pattern anchored on the native
#: separator would silently never fire on Windows and count a vendored tree as
#: first-party. Kept as it stands, `bin` and `env` and all: in some layouts these prune real
#: first-party directories, but every structure number already reported was computed with
#: them pruned, so the list is left alone and the limitation documented here instead.
STRUCTURE_SKIP_DIR: re.Pattern[str] = re.compile(
    r"(^|/)(node_modules|bower_components|vendor|third_party|thirdparty|dist|build|out|"
    r"target|bin|obj|\.git|__pycache__|\.venv|venv|env|site-packages|coverage|"
    r"migrations|generated|gen|\.next|\.nuxt|\.terraform|Pods)/",
    re.I,
)

#: Files that are build output wearing a source extension.
STRUCTURE_SKIP_FILE: re.Pattern[str] = re.compile(
    r"\.(min|bundle|generated|pb|_pb2|d)\.[a-z]+$", re.I
)

#: Where tests live. Go projects use `testdata`, JavaScript ones use `spec` and
#: `__tests__`, and the final alternative picks up `foo_test.go`, `foo.test.ts` and
#: `foo-spec.rb`. The separator in front of `test` is what stops `latest.py` being counted,
#: which a looser pattern swallows whole.
STRUCTURE_TEST_PATH: re.Pattern[str] = re.compile(
    r"(^|/)(tests?|spec|specs|__tests__|__mocks__|e2e|fixtures|testdata)/|"
    r"(^|/)(test_|conftest\.)|[._-](test|spec)\.[a-z0-9]+$",
    re.I,
)

#: What a generator stamps at the top of what it wrote. Matched against the head of the
#: file only, which is where such a banner is or is nowhere.
STRUCTURE_GENERATED_HEADER: re.Pattern[bytes] = re.compile(
    rb"@generated|DO NOT EDIT|Code generated by|autogenerated", re.I
)

#: A plain keyword list used to count error-handling constructs. Worth being upfront about
#: its limits: matching is purely lexical, so a keyword counts just as much inside a comment
#: or a string literal as it does in real code, and there is no way for a keyword list to
#: see error handling that a language expresses through its type system instead of its
#: vocabulary -- Rust's `?` operator, Haskell's ExceptT, an OCaml `option` return are all
#: invisible to this table and always will be regardless of how the list is tuned. `require`
#: stays on the list even though it doubles as an import keyword in some of these languages,
#: because removing it now would shift the count for every repository already measured and
#: break comparability across tool versions. Left off deliberately are the bare words
#: `error`, `exception` and `warn`, since they are respectively an ordinary Go identifier, a
#: word that shows up constantly in unrelated comments, and a logging term.
STRUCTURE_ERROR_KEYWORDS: re.Pattern[bytes] = re.compile(
    rb"\b(try|catch|except|rescue|finally|throw|throws|raise|panic|recover|"
    rb"unwrap_or|map_err|expect_err|ok_or|"
    rb"assert|invariant|precondition|require|ensure|unreachable|"
    # Rust, Zig, Swift, Kotlin
    rb"rethrow|errdefer|bail|anyhow|with_context|runCatching|"
    # the scientific and scripting set: Perl through MATLAB
    rb"croak|confess|carp|pcall|xpcall|tryCatch|stopifnot|withCallingHandlers|MException|"
    # functional languages, from Haskell through to the Lisps
    rb"throwIO|catchError|catches|bracket|failwith|badmatch|ex-info|handler-case|ignore-errors|"
    # the remainder: Objective-C, PowerShell, shells, Solidity, Fortran and the SQL
    # dialects
    rb"NSError|trap|revert|iostat|SQLERRM|RAISE_APPLICATION_ERROR|RAISERROR|die)\b|"
    rb"Write-Error|-ErrorAction|\bset\s+-[a-z]*e\b|\bON\s+SIZE\s+ERROR\b|\bWHEN\s+OTHERS\b|"
    rb"\berror\s+stop\b",
    re.I,
)

#: The half of error handling that is a type or an idiom rather than a keyword: a Result
#: or Either in the signature, Go's `err != nil`, the Erlang and Elixir tagged tuple, C's
#: errno protocol. Counted alongside the keywords, never instead of them.
STRUCTURE_ERROR_TYPES: re.Pattern[bytes] = re.compile(
    rb"\bResult\s*<|\bEither\s*<|errors\.(New|Wrap|Is|As)\b|"
    rb"fmt\.Errorf\b|\berr\s*!=\s*nil\b|"
    # square brackets, because that is how Scala and Rust write a generic
    rb"\b(Result|Either|Try)\s*\[|"
    # a tagged tuple, which is how Erlang and Elixir report a failure
    rb"\{:?error[,}]|"
    # C's errno protocol
    rb"\b(errno|perror|strerror)\b"
)
