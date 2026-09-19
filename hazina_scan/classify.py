"""Turn the tree scan's `class_signals` into a judgement about what kind of repository it is.

`classify()` reads a dict and returns a dict. It touches no file, opens no socket and holds
no state, so the same `tree.collect()` output always yields the same answer.

The weights, ceilings and thresholds live in `vocab.py` as data rather than as constants
spread through the code below, so the whole scoring rule can be read, and changed, in one
place. Every note that admits low confidence ends the same way -- check the source directly
-- because a person reading the repository is the only confirmation this tool has to offer.

Seven atomic classes are scored -- frontend, backend, ml, ai_research, data_engineering,
security, infra -- and an eighth, fullstack, exists only as the result of collapsing a
frontend/backend pair. The returned dict carries:

    class_confidence   each class's share of the evidence, 0 to 1
    raw_scores         each class's unnormalised total
    primary_class      the best-evidenced class, defaulting to "backend"
    suggested_classes  every detected class, after the fullstack collapse
    is_monorepo        True when more than one class was suggested
    notes              why anything above was not obvious

Two rules keep a single stray keyword from naming a repository.

*Frontend needs frontend material.* A dependency list can mention React in a repository with
no component file, no stylesheet worth the name and no web-language code. When the tree
holds none of that material, the frontend evidence drawn from dependencies and frameworks is
struck out rather than discounted.

*A primary class needs corroboration.* Either two independent term families back it, or the
one family behind it is strong enough (`MIN_SINGLE_FAMILY_RAW`) to beat a rival that does
have two. Failing both, the best corroborated class takes over, and failing that the
repository falls back to "backend", which is what general-purpose code looks like here.
"""

from __future__ import annotations

from hazina_scan import vocab

ATOMIC_CLASSES = vocab.ATOMIC_CLASSES

#: Raw floor for calling a class "detected", separate from the confidence threshold and from
#: the corroboration test. Named rather than spelled inline at each use, so the one floor is
#: visible in a single place and changing it is a single edit.
MIN_DETECTED_RAW: float = 2.0

#: The eight-class vocabulary minus the seven atomic ones.
FULLSTACK = "fullstack"

#: Where a repository lands when nothing else is evidenced.
FALLBACK_CLASS = "backend"


def _bounded(quantity: float, per_unit: float, ceiling: float) -> float:
    """Weight `quantity` and refuse to let it grow past `ceiling`.

    Every term is bounded so that one enormous count -- ten thousand generated SQL files,
    say -- cannot decide the class on its own.
    """
    return min(quantity * per_unit, ceiling)


def _has_frontend_material(stats: dict) -> bool:
    """True when the tree itself shows a user interface, not merely a dependency naming one.

    Any of three things is enough: at least one file that looks like a UI component, web
    languages holding a non-trivial share of the code, or enough CSS to be deliberate.
    """
    signals = stats.get("class_signals", {}) or {}
    loc_by_language = stats.get("loc_by_language", {}) or {}

    all_loc = sum(loc_by_language.values())
    web_loc = sum(n for lang, n in loc_by_language.items() if lang in vocab.WEB_LANGUAGES)
    web_share = web_loc / all_loc if all_loc > 0 else 0.0

    if signals.get("ui_component_file_count", 0) > 0:
        return True
    if web_share >= vocab.MIN_FRONTEND_WEB_LOC_SHARE:
        return True
    return signals.get("css_loc_ratio", 0.0) >= vocab.MIN_FRONTEND_CSS_RATIO


def _iac_share(stats: dict, signals: dict) -> float:
    """What fraction of the repository's code is infrastructure declared as code.

    Dockerfiles are left out of the numerator on purpose. Nearly every application ships
    one, so counting their lines here would make every application look like an infra
    repository; Dockerfiles keep a small count-based term of their own instead.
    """
    all_loc = stats.get("total_loc", 0) or 0
    if all_loc <= 0:
        return 0.0
    by_type = signals.get("iac_loc_by_type", {}) or {}
    return sum(n for kind, n in by_type.items() if kind != "Dockerfile") / all_loc


def score_terms(stats: dict) -> dict[str, dict[str, float]]:
    """Score every class, keeping the contributions apart instead of summing them.

    The sum answers "how strong"; the individual terms answer "how many different kinds of
    evidence", and the corroboration rule needs the second question answered too.
    """
    signals = stats.get("class_signals", {}) or {}
    keyword_hits = signals.get("dep_keyword_hits", {}) or {}
    frameworks = set(stats.get("detected_frameworks", []) or [])

    def keywords(group: str) -> int:
        """How many dependency keywords of one group the manifests mentioned."""
        return len(keyword_hits.get(group, []))

    def count(name: str, default=0):
        return signals.get(name, default)

    def flag(name: str, worth: float) -> float:
        return worth if signals.get(name) else 0.0

    web_frameworks = len(vocab.FRONTEND_FRAMEWORK_MARKERS & frameworks)
    service_frameworks = len(vocab.BACKEND_FRAMEWORK_MARKERS & frameworks)
    is_api_service = stats.get("project_type", "") == "API service"

    terms = {
        "frontend": {
            "dep_keywords": _bounded(keywords("frontend_frameworks"), 1.5, 6.0),
            "ui_components": _bounded(count("ui_component_file_count"), 0.1, 5.0),
            "css": _bounded(count("css_loc_ratio", 0.0) * 20.0, 1.0, 3.0),
            "frameworks": _bounded(web_frameworks, 1.0, 3.0),
        },
        "backend": {
            "dep_keywords": _bounded(keywords("backend_frameworks"), 1.5, 6.0),
            "orm_db": _bounded(keywords("orm_db"), 1.0, 4.0),
            "frameworks": _bounded(service_frameworks, 1.0, 3.0),
            "project_type": 3.0 if is_api_service else 0.0,
        },
        "ml": {
            "dep_keywords": _bounded(keywords("ml_libs"), 2.0, 8.0),
            "notebooks": _bounded(count("notebook_count"), 0.3, 4.0),
            "experiment_tracking": _bounded(keywords("experiment_tracking"), 1.0, 4.0),
        },
        # ml and ai_research read the same three signals with the emphasis swapped: serving
        # a model rewards the library list, reproducing an experiment rewards the notebooks
        # and the tracking tools.
        "ai_research": {
            "experiment_tracking": _bounded(keywords("experiment_tracking"), 2.0, 6.0),
            "notebooks": _bounded(count("notebook_count"), 0.5, 5.0),
            "ml_libs": _bounded(keywords("ml_libs"), 1.0, 4.0),
        },
        "data_engineering": {
            "dep_keywords": _bounded(keywords("data_eng"), 2.5, 9.0),
            "sql_files": _bounded(count("sql_file_count"), 0.3, 4.0),
            "sql_loc": _bounded(count("sql_loc") / 200.0, 1.0, 3.0),
            "data_files": _bounded(count("data_file_count"), 0.5, 2.0),
        },
        # Security has one family and no second opinion available, which is exactly why its
        # per-keyword weight is the highest in the table.
        "security": {
            "dep_keywords": _bounded(keywords("security_libs"), 3.0, 10.0),
        },
        "infra": {
            "terraform": flag("terraform_present", 4.0)
            + _bounded(count("terraform_file_count"), 0.3, 4.0),
            "k8s": _bounded(count("k8s_manifest_count"), 0.5, 4.0),
            "helm": flag("helm_present", 2.0),
            "pulumi": flag("pulumi_present", 2.0),
            "ansible": flag("ansible_present", 2.0),
            "dep_keywords": _bounded(keywords("infra_libs"), 1.0, 3.0),
            "dockerfiles": _bounded(count("dockerfile_count"), 0.5, 1.5),
            # Measured as a proportion rather than a count, so a repository that is mostly
            # HCL or mostly manifests scores near the ceiling while an application that
            # happens to carry a couple of deploy files barely registers.
            "iac_share": _bounded(_iac_share(stats, signals) * 8.0, 1.0, 6.0),
            "cloudformation": _bounded(count("cloudformation_file_count"), 0.5, 2.0),
        },
    }
    return {
        name: {term: round(value, 3) for term, value in scores.items()}
        for name, scores in terms.items()
    }


def _is_evidenced(name: str, raw: dict[str, float], families: dict[str, int]) -> bool:
    """Has this class cleared the floor with either corroboration or sheer weight?"""
    if raw[name] < vocab.MIN_PRIMARY_RAW:
        return False
    return families[name] >= 2 or raw[name] >= vocab.MIN_SINGLE_FAMILY_RAW


def choose_primary(raw: dict[str, float], support: dict[str, int], notes: list[str]) -> str | None:
    """Name the best-evidenced class, or None when nothing earns the title.

    Simply taking the highest score would let one dependency keyword classify a repository.
    So the leader is checked against the corroboration rule; if it fails, the strongest
    class that passes takes its place, and the swap is written down.
    """
    by_score = sorted(ATOMIC_CLASSES, key=lambda name: raw[name], reverse=True)
    leader = by_score[0]

    if raw[leader] < vocab.MIN_PRIMARY_RAW:
        return None
    if _is_evidenced(leader, raw, support):
        return leader

    runner_up = next((name for name in by_score[1:] if _is_evidenced(name, raw, support)), None)
    if runner_up is not None:
        notes.append(
            f"'{leader}' scored highest ({raw[leader]}) but on a single weak signal; "
            f"'{runner_up}' is corroborated by independent signals and was made "
            "primary instead."
        )
        return runner_up

    notes.append(
        f"Primary class '{leader}' rests on a single weak signal (raw {raw[leader]}). "
        "Low confidence — this tool cannot confirm it; check the source directly."
    )
    return leader


def _undecided(
    confidence: dict[str, float], raw: dict[str, float], reason: str, notes: list[str]
) -> dict:
    """The answer when no class is evidenced: `backend`, alone, with the reason recorded.

    Two different roads lead here -- no evidence at all, and evidence too thin to choose
    between -- and they must produce the same shape of answer, so they share this exit.
    """
    notes.append(reason)
    return {
        "class_confidence": confidence,
        "raw_scores": raw,
        "primary_class": FALLBACK_CLASS,
        "suggested_classes": [FALLBACK_CLASS],
        "is_monorepo": False,
        "notes": notes,
    }


def classify(stats: dict, threshold: float = 0.18) -> dict:
    """Classify one repository from the tree scan's signals. See the module docstring."""
    notes: list[str] = []
    terms = score_terms(stats)

    # A UI cannot be conjured out of a manifest. If anything scored frontend on dependency
    # or framework evidence but the tree shows no frontend material, those two terms are
    # zeroed -- not merely reduced -- and the reader is told it happened.
    on_paper = terms["frontend"]["dep_keywords"] + terms["frontend"]["frameworks"]
    if on_paper > 0 and not _has_frontend_material(stats):
        terms["frontend"]["dep_keywords"] = 0.0
        terms["frontend"]["frameworks"] = 0.0
        notes.append(
            "Frontend dependency/framework signals were discarded: the repo has no "
            "component files, negligible CSS and almost no web-language LOC."
        )

    raw = {name: round(sum(scores.values()), 3) for name, scores in terms.items()}
    support = {
        name: sum(1 for value in scores.values() if value >= vocab.SUPPORT_TERM_FLOOR)
        for name, scores in terms.items()
    }
    evidence = sum(raw.values())

    if evidence <= 0:
        return _undecided(
            {name: 0.0 for name in ATOMIC_CLASSES},
            raw,
            "No strong class signals found — likely a general-purpose library/CLI. "
            "Defaulted primary to 'backend' (general code). This tool cannot confirm "
            "it — check the source directly.",
            notes,
        )

    # Dividing by the evidence actually found would make a lone faint signal look like
    # certainty, since it would be all of the evidence there is. Dividing by a floor instead
    # means a repository full of noise reports low confidence in everything, which is true.
    scale = max(evidence, vocab.MIN_CONFIDENCE_EVIDENCE)
    confidence = {name: round(value / scale, 4) for name, value in raw.items()}

    primary = choose_primary(raw, support, notes)
    if primary is None:
        return _undecided(
            confidence,
            raw,
            "No class cleared the minimum evidence bar — likely a general-purpose "
            "library/CLI. Defaulted primary to 'backend' (general code). This tool "
            "cannot confirm it — check the source directly.",
            notes,
        )

    # To be "detected" a class needs all three: a confident share, real raw weight, and the
    # same corroboration the primary class had to show.
    detected = [
        name
        for name in ATOMIC_CLASSES
        if confidence[name] >= threshold
        and raw[name] >= MIN_DETECTED_RAW
        and (support[name] >= 2 or raw[name] >= vocab.MIN_SINGLE_FAMILY_RAW)
    ]
    if primary not in detected:
        detected = sorted(set(detected) | {primary}, key=ATOMIC_CLASSES.index)

    suggested = _collapse_fullstack(detected, notes)

    if "ml" in detected and "ai_research" in detected:
        notes.append(
            "Both ml and ai_research signals present — these overlap. Pick one as the "
            "dominant class per component based on whether the emphasis is "
            "production/serving (ml) or experiments/reproduction (ai_research)."
        )

    return {
        "class_confidence": confidence,
        "raw_scores": raw,
        "primary_class": primary,
        "suggested_classes": suggested,
        "is_monorepo": len(suggested) >= 2,
        "notes": notes,
    }


def _collapse_fullstack(detected: list[str], notes: list[str]) -> list[str]:
    """Fold a frontend/backend pair into one class, or explain why it was left alone.

    A repository holding exactly those two is one application with a user interface, not two
    components living together. Once a third class joins them the same evidence reads as a
    monorepo, which is worth saying but not worth rewriting.
    """
    pair = {"frontend", "backend"}
    if set(detected) == pair:
        notes.append(
            "Strong frontend + backend signals with no other class — collapsed to "
            "a single 'fullstack' class."
        )
        return [FULLSTACK]
    if pair.issubset(detected):
        notes.append(
            "Frontend + backend both present alongside other classes — treated as a "
            "monorepo. Re-run per sub-directory for a true component breakdown."
        )
    return list(detected)
