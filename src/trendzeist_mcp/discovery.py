"""Composite topic-discovery workflow built on the primitive tools."""

from __future__ import annotations

from typing import Any

from . import formatters as fmt
from . import tools
from . import validation as v
from .client import TrendsError
from .sources import Hub

_SIGNAL_ORDER = {"breakout": 0, "rising": 1, "evergreen": 2}
# Most favourable direction_now wins when a topic was surfaced by several seeds.
_DIRECTION_RANK = {"rising": 3, "new": 2, "stable": 2, "falling": 0, "no_interest": 0}
# Seed trend context window; see aeo.TREND_TIMEFRAME for the rationale.
TREND_TIMEFRAME = tools.TREND_TIMEFRAME
SKIP_MESSAGE = (
    "Not fetched: Google is rate limiting this session. Retry in ~60 s with 1-2 seeds."
)


def _momentum(signal: str, value: float | int | None) -> float:
    """Single sortable score: breakouts first, then growth %, then popularity."""
    val = float(value or 0)
    if signal == "breakout":
        return 1_000_000 + val
    if signal == "rising":
        return 10_000 + val
    return val


def discover_topics(
    hub: Hub,
    seed_keywords: list[str],
    geo: str = "",
    timeframe: str = "today 3-m",
    category: int = 0,
    gprop: str = "",
    max_per_seed: int = 15,
    site_queries: list[str] | None = None,
    exclude_covered: bool = False,
) -> dict[str, Any]:
    """For each seed, gather rising/top related queries plus trend direction.

    Returns a de-duplicated, ranked list of candidate blog topics. Partial
    failures (e.g. rate limits on a later seed) are reported per seed rather
    than failing the whole call. ``site_queries`` (what the site already ranks
    for) annotates each topic with ``coverage``; ``exclude_covered`` drops the
    covered ones.
    """
    seeds = v.validate_keywords(seed_keywords)
    tf, g, cat, gp = (
        v.validate_timeframe(timeframe),
        v.validate_geo(geo),
        v.validate_category(category),
        v.validate_gprop(gprop),
    )
    per_seed = v.validate_limit(max_per_seed, default=15, maximum=50)
    site, site_dropped = v.validate_site_queries(site_queries)

    candidates: dict[str, dict[str, Any]] = {}
    seed_reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    questions: list[dict[str, Any]] = []
    question_keys: set[str] = set()
    suspect: list[dict[str, Any]] = []

    # One interest_over_time call covers up to 5 seeds -> cheap trend context on a
    # fixed 12-month window (so direction_now / growth_3m are meaningful whatever
    # timeframe the related queries use). It is enrichment only: if it fails we
    # still try the first seed's related queries, and only enter cooling-down
    # (skip uncached seeds) once a related_queries request itself is refused.
    cooling_down = False
    has_cache = getattr(hub.trends, "has_cached_related_queries", None)

    directions: dict[str, Any] = {}
    try:
        directions = tools.fetch_trend(hub, seeds, TREND_TIMEFRAME, g, cat, gp)["summary"]
    except TrendsError as exc:
        errors.append({"step": "interest_over_time", "error": str(exc)})

    for seed in seeds:
        report: dict[str, Any] = {"seed": seed, "trend": directions.get(seed, {"available": False})}
        if cooling_down and not (callable(has_cache) and has_cache(seed, tf, g, cat, gp)):
            report["skipped"] = SKIP_MESSAGE
            errors.append({"step": f"related_queries:{seed}", "error": "skipped (rate limited)"})
            seed_reports.append(report)
            continue
        try:
            top, rising = tools.fetch_related(hub, seed, tf, g, cat, gp, per_seed)
        except TrendsError as exc:
            report["error"] = str(exc)
            errors.append({"step": f"related_queries:{seed}", "error": str(exc)})
            seed_reports.append(report)
            if exc.retryable:
                cooling_down = True
            continue

        rising, flagged = fmt.split_suspects(rising)
        for item in flagged:
            suspect.append(
                {
                    "topic": item["name"],
                    "growth_pct": item.get("growth_pct"),
                    "is_breakout": item.get("is_breakout", False),
                    "source_seed": seed,
                    "reason": item["suspect_reason"],
                }
            )
        report["rising_count"] = len(rising)
        report["top_count"] = len(top)
        if flagged:
            report["suspect_count"] = len(flagged)
        for q in fmt.extract_questions(("rising", rising), ("top", top), limit=per_seed):
            norm = fmt.normalise_query(q["question"])
            if norm not in question_keys:
                question_keys.add(norm)
                questions.append({**q, "seed": seed})
        if not rising and not top:
            report["hint"] = (
                "No related queries: search volume is too low for this seed. Try a "
                "broader term (use suggest_keywords), a wider geo, or a longer timeframe."
            )
        seed_reports.append(report)

        for item in rising:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            signal = "breakout" if item.get("is_breakout") else "rising"
            cand = {
                "topic": name,
                "signal": signal,
                "growth_pct": item.get("growth_pct"),
                "popularity": None,
                "source_seeds": [seed],
                "momentum": _momentum(signal, item.get("growth_pct")),
            }
            _merge(candidates, cand)

        for item in top:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            cand = {
                "topic": name,
                "signal": "evergreen",
                "growth_pct": None,
                "popularity": item.get("value"),
                "source_seeds": [seed],
                "momentum": _momentum("evergreen", item.get("value")),
            }
            _merge(candidates, cand)

    ranked = sorted(
        candidates.values(),
        key=lambda c: (_SIGNAL_ORDER[c["signal"]], -c["momentum"], c["topic"]),
    )
    covered_dropped = 0
    kept: list[dict[str, Any]] = []
    for c in ranked:
        c.pop("momentum", None)
        c["angle"] = fmt.classify_angle(c["topic"]) or ("news" if c["signal"] == "breakout" else None)
        coverage: str | None = None
        if site:
            coverage, matched = fmt.site_coverage(c["topic"], site)
            c["coverage"] = coverage
            if matched:
                c["matched"] = matched
            if exclude_covered and coverage == "covered":
                covered_dropped += 1
                continue
        # Seed context must not depend on seed order: among the seeds that surfaced
        # this topic, take the most favourable direction_now (consistent with _merge,
        # which keeps the strongest signal) and say which seed it came from.
        candidates_ctx = [
            (s, directions[s]) for s in c["source_seeds"] if directions.get(s, {}).get("available")
        ]
        if candidates_ctx:
            seed_name, seed_trend = max(
                candidates_ctx,
                key=lambda st: (_DIRECTION_RANK.get(st[1].get("direction_now"), -1), st[0]),
            )
            c["momentum_seed"] = seed_name
        else:
            seed_trend = {}
        c["score"], c["score_breakdown"] = fmt.opportunity_score(
            popularity=c.get("popularity"),
            direction_now=seed_trend.get("direction_now"),
            has_breakout=c["signal"] == "breakout",
            is_rising=c["signal"] == "rising",
            coverage=coverage,
            angle=c["angle"],
            question_shaped=fmt.is_question(c["topic"]),
        )
        kept.append(c)
    ranked = kept

    settings = getattr(hub, "settings", None)
    hl_for = getattr(settings, "hl_for", None)

    out: dict[str, Any] = {
        "query": {
            "seed_keywords": seeds,
            "timeframe": tf,
            "trend_timeframe": TREND_TIMEFRAME,
            "geo": g or "worldwide",
            "hl": hl_for(g) if callable(hl_for) else "en-US",
            "category": cat,
            "gprop": gp or "web",
        },
        "seeds": seed_reports,
        "topics": ranked,
        "questions": questions,
        "suspect": suspect,
        "counts": {
            "breakout": sum(1 for c in ranked if c["signal"] == "breakout"),
            "rising": sum(1 for c in ranked if c["signal"] == "rising"),
            "evergreen": sum(1 for c in ranked if c["signal"] == "evergreen"),
            "questions": len(questions),
            "suspect": len(suspect),
        },
        "errors": errors,
        "guidance": (
            "breakout = brand-new/exploding demand (time-sensitive, low competition); "
            "rising = growing interest (good near-term posts); evergreen = consistently "
            "popular (pillar content). angle = suggested title format. questions = "
            "question-shaped searches (FAQ / answer-engine candidates); expand them with "
            "mine_questions. suspect = rising queries that look like injected spam "
            "(kept out of topics and questions; do not write about them). Cross-check "
            "finalists with interest_over_time; seeds[].trend.direction_now is the "
            "recent slope over the last 12 months. score / score_breakdown rank within "
            "this call only (see score_note)."
        ),
        "score_note": fmt.SCORE_NOTE,
    }
    if site:
        out["query"]["site_queries_count"] = len(site)
        out["counts"]["covered"] = sum(1 for c in ranked if c.get("coverage") == "covered") + covered_dropped
        out["counts"]["partial"] = sum(1 for c in ranked if c.get("coverage") == "partial")
        out["counts"]["gap"] = sum(1 for c in ranked if c.get("coverage") == "gap")
    notes = [
        v.limit_note(max_per_seed, per_seed, name="max_per_seed"),
        f"site_queries truncated to {v.MAX_SITE_QUERIES}; {site_dropped} dropped." if site_dropped else None,
        f"{covered_dropped} covered topic(s) removed (exclude_covered=true)." if covered_dropped else None,
    ]
    notes = [n for n in notes if n]
    if notes:
        out["note"] = " ".join(notes)
    return out


def _merge(candidates: dict[str, dict[str, Any]], cand: dict[str, Any]) -> None:
    """Merge a candidate into the pool, keeping the strongest signal."""
    key = cand["topic"].lower()
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = cand
        return
    for s in cand["source_seeds"]:
        if s not in existing["source_seeds"]:
            existing["source_seeds"].append(s)
    new_rank, old_rank = _SIGNAL_ORDER[cand["signal"]], _SIGNAL_ORDER[existing["signal"]]
    stronger_signal = new_rank < old_rank
    same_signal_stronger = new_rank == old_rank and cand["momentum"] > existing["momentum"]
    if stronger_signal or same_signal_stronger:
        # Result must not depend on seed order: always keep the strongest evidence.
        existing.update(signal=cand["signal"], momentum=cand["momentum"])
        if cand["growth_pct"] is not None:
            existing["growth_pct"] = cand["growth_pct"]
    if cand["popularity"] is not None:
        existing["popularity"] = max(cand["popularity"], existing.get("popularity") or 0)
    if cand["growth_pct"] is not None and existing.get("growth_pct") is None:
        existing["growth_pct"] = cand["growth_pct"]
