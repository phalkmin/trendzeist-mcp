"""AEO opportunity finder (roadmap A7): which questions to answer, with what evidence,
and where to earn mentions, from Google Trends, Autocomplete, Google News and Wikipedia.

Budget per uncached call: 1 interest_over_time + per seed (1 related_queries +
1 + len(_AEO_PREFIXES) Autocomplete + 1 News + 1-2 Wikipedia). Google requests stop at
the first retryable error (429 / transport) like discover_topics; Wikipedia runs on
its own lane and is never skipped because Google is cooling down.
"""

from __future__ import annotations

from typing import Any

from . import formatters as fmt
from . import tools
from . import validation as v
from .client import TrendsError
from .sources import Hub

# Capped prefix set for the composite (the full 15-prefix list is mine_questions' job).
_AEO_PREFIXES: tuple[str, ...] = ("how to", "what is", "why", "can", "vs")
_QUESTIONS_PER_SEED = 15
_ANGLE_RANK = {angle: i for i, angle in enumerate(fmt.ANGLES)}
# Seed trend context always uses a 12-month window, independent of the
# related-queries timeframe: it is one request for up to 5 seeds either way, and
# only a >= 6-month series can yield growth_3m and a meaningful direction_now.
TREND_TIMEFRAME = "today 12-m"


def _score(
    cluster: dict[str, Any],
    trend: dict[str, Any],
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
) -> tuple[int, dict[str, int]]:
    """Transparent 0-100 heuristic; every component is reported in score_breakdown."""
    parts: dict[str, int] = {"questions": min(40, 8 * len(cluster["questions"]))}
    # Recent slope, not the whole-window label: a topic that peaked six months ago
    # and fell 70% since must not keep collecting "rising" points.
    parts["trend"] = {"rising": 20, "stable": 10, "new": 10}.get(trend.get("direction_now"), 0)
    parts["trend"] += 15 if cluster["has_breakout"] else 0
    if news is None:
        parts["news"] = 0
    else:
        parts["news"] = (10 if news["articles_7d"] > 0 else 0) + (
            5 if news["articles_30d"] >= 10 else 0
        )
    if wiki is None:
        parts["wikipedia"] = 0
    elif wiki.get("related_article"):
        parts["wikipedia"] = 0  # a search hit is not evidence of a missing article
    elif wiki["has_article"]:
        parts["wikipedia"] = 10 if wiki.get("direction") == "rising" else 0
    else:
        parts["wikipedia"] = 10 if cluster["angle"] == "definition" else 5
    return min(100, sum(parts.values())), parts


def _trend_block(trend: dict[str, Any]) -> dict[str, Any]:
    """Compact per-opportunity trend summary; explains a null growth_3m."""
    if not trend.get("available"):
        return {"available": False}
    block: dict[str, Any] = {
        "direction": trend.get("direction"),
        "direction_now": trend.get("direction_now"),
        "growth_3m": trend.get("growth_3m"),
        "insight": trend.get("insight"),
    }
    if block["growth_3m"] is None and trend.get("growth_note"):
        block["growth_note"] = trend["growth_note"]
    return block


def _brief(
    cluster: dict[str, Any],
    hints: dict[str, Any],
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
) -> str:
    n = len(cluster["questions"])
    text = (
        f"Answer {n} {cluster['angle']} question{'s' if n != 1 else ''} about "
        f"'{cluster['seed']}'; include {', '.join(hints['evidence'][:2])}."
    )
    if news and news["top_publishers"]:
        text += f" Quote or pitch: {', '.join(news['top_publishers'])}."
    if wiki is not None:
        if wiki["has_article"]:
            text += f" Cite Wikipedia '{wiki['title']}' as the definition anchor."
        elif wiki.get("related_article"):
            text += f" Wikipedia suggests '{wiki['related_article']}'; verify relevance before citing."
        else:
            text += " No exact Wikipedia article was found; verify the gap and cite primary sources."
    return text


def _seed_context(
    hub: Hub,
    seed: str,
    tf: str,
    g: str,
    news_geo: str,
    lang: str,
    cooling: bool,
    errors: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None, bool, bool]:
    """Gather questions, news and Wikipedia context for one seed.

    Returns ``(found_questions, news, wiki, cooling, skipped_google)``.
    """
    found: list[dict[str, Any]] = []
    skipped = cooling

    if not cooling:
        try:
            res = hub.trends.related_queries(seed, tf, g, 0, "")
            rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), 25, label="query"))
            top = fmt.related_list(res.get("top"), 25, label="query")
            # Suspect (spam-like) rising items never become questions or breakouts.
            rising, _ = fmt.split_suspects(fmt.flag_suspects(rising, top, seed))
            for q in fmt.extract_questions(
                ("rising", rising), ("top", top), limit=_QUESTIONS_PER_SEED
            ):
                found.append(
                    {
                        "question": q["question"],
                        "angle": q["angle"],
                        "origin": f"trends_{q['source']}",
                        "is_breakout": bool(q.get("is_breakout")),
                    }
                )
        except TrendsError as exc:
            errors.append({"step": f"related_queries:{seed}", "error": str(exc)})
            cooling = cooling or exc.retryable

    if not cooling:
        mined = tools.mine_questions(
            hub, seed, g, limit=_QUESTIONS_PER_SEED, prefixes=_AEO_PREFIXES
        )
        for q in mined["questions"]:
            found.append(
                {
                    "question": q["question"],
                    "angle": q["angle"],
                    "origin": "autocomplete",
                    "is_breakout": False,
                }
            )
        for e in mined["errors"]:
            errors.append({"step": f"autocomplete:{seed}:{e['prefix']}", "error": e["error"]})
        if "partial" in mined:
            cooling = True

    news: dict[str, Any] | None = None
    if not cooling:
        try:
            articles = hub.news.articles(seed, news_geo)
            recency = fmt.recency_histogram(articles)
            news = {
                "articles_30d": recency["last_30d"],
                "articles_7d": recency["last_7d"],
                "coverage": fmt.coverage_level(recency),
                "top_publishers": [p["publisher"] for p in fmt.publisher_table(articles, 3)],
            }
        except TrendsError as exc:
            errors.append({"step": f"news:{seed}", "error": str(exc)})
            cooling = cooling or exc.retryable

    wiki: dict[str, Any] | None = None
    try:
        page = hub.wikipedia.search(seed, lang)
        if page.get("found") and fmt.normalise_query(page["title"]) == fmt.normalise_query(seed):
            summary = fmt.pageview_summary(
                hub.wikipedia.pageviews(page["title"], lang, 30), page["title"]
            )
            wiki = {
                "has_article": True,
                "title": page["title"],
                "direction": summary["direction"],
                "daily_mean": summary["daily_mean"],
            }
        else:
            wiki = {"has_article": False, "title": None}
            if page.get("found"):
                wiki["related_article"] = page["title"]
    except TrendsError as exc:
        errors.append({"step": f"wikipedia:{seed}", "error": str(exc)})

    return found, news, wiki, cooling, skipped


def aeo_opportunities(
    hub: Hub,
    seeds: list[str],
    geo: str = "",
    timeframe: str = "today 3-m",
    limit: int = 10,
) -> dict[str, Any]:
    """Question clusters x trend x news coverage x Wikipedia presence x citability hints."""
    kws = v.validate_keywords(seeds)
    tf = v.validate_timeframe(timeframe)
    g = v.validate_geo(geo)
    lim = v.validate_limit(limit, default=10, maximum=25)
    hl = tools._hl_for(hub, g)
    lang = hl.split("-")[0].lower()
    news_geo = g.split("-")[0] if g else "US"

    errors: list[dict[str, str]] = []
    cooling = False

    trends: dict[str, Any] = {}
    try:
        trends = fmt.interest_over_time(
            hub.trends.interest_over_time(kws, TREND_TIMEFRAME, g, 0, ""), kws
        )["summary"]
    except TrendsError as exc:
        errors.append({"step": "interest_over_time", "error": str(exc)})
        # Trend context is enrichment only. Do not enter cooling here: the first
        # seed's own (lane-paced) requests get a chance, and only their failure
        # marks the session as rate limited (see _seed_context).

    seen: set[str] = set()
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    seed_reports: list[dict[str, Any]] = []
    context: dict[str, tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = {}

    for seed in kws:
        trend = trends.get(seed, {"available": False})
        report: dict[str, Any] = {"seed": seed, "trend": trend}
        found, news, wiki, cooling, skipped = _seed_context(
            hub, seed, tf, g, news_geo, lang, cooling, errors
        )
        if skipped:
            report["skipped"] = "Google requests skipped: rate limited earlier in this call."

        added = 0
        for q in found:
            norm = fmt.normalise_query(q["question"])
            if not norm or norm in seen:
                continue
            seen.add(norm)
            added += 1
            angle = q["angle"] or "how-to"
            cluster = clusters.setdefault(
                (seed, angle),
                {
                    "seed": seed,
                    "angle": angle,
                    "questions": [],
                    "origins": set(),
                    "has_breakout": False,
                },
            )
            cluster["questions"].append(q["question"])
            cluster["origins"].add(q["origin"])
            cluster["has_breakout"] = cluster["has_breakout"] or q["is_breakout"]

        report["questions_found"] = added
        report["news"] = news
        report["wikipedia"] = wiki
        seed_reports.append(report)
        context[seed] = (trend, news, wiki)

    ranked: list[dict[str, Any]] = []
    for cluster in clusters.values():
        trend, news, wiki = context[cluster["seed"]]
        score, parts = _score(cluster, trend, news, wiki)
        hints = fmt.citability_hints(cluster["angle"])
        ranked.append(
            {
                "seed": cluster["seed"],
                "angle": cluster["angle"],
                "score": score,
                "score_breakdown": parts,
                "questions": cluster["questions"][:8],
                "question_count": len(cluster["questions"]),
                "evidence_from": sorted(cluster["origins"]),
                "trend": _trend_block(trend),
                "news": news,
                "wikipedia": wiki,
                "citability_hints": hints,
                "brief": _brief(cluster, hints, news, wiki),
            }
        )
    ranked.sort(
        key=lambda c: (-c["score"], kws.index(c["seed"]), _ANGLE_RANK.get(c["angle"], 99))
    )

    per_seed_requests = 2 + (1 + len(_AEO_PREFIXES)) + 2
    out: dict[str, Any] = {
        "query": {
            "seeds": kws,
            "timeframe": tf,
            "trend_timeframe": TREND_TIMEFRAME,
            "geo": g or "worldwide",
            "hl": hl,
            "news_geo": news_geo,
            "wikipedia_lang": lang,
        },
        "opportunities": ranked[:lim],
        "seeds": seed_reports,
        "counts": {
            "opportunities": len(ranked),
            "returned": min(len(ranked), lim),
            "questions": len(seen),
        },
        "errors": errors,
        "score_note": (
            "score is a transparent heuristic (0-100): questions found (8 each, max 40); seed "
            "trend over the last 12 months by direction_now, the recent slope (rising 20 / "
            "stable or new 10 / falling 0; +15 if a breakout question); news (10 if covered "
            "this week, +5 if 10+ articles in 30 days); Wikipedia (10 if the article's "
            "attention is rising; no search hit scores 10 for definition angles, else 5; "
            "a related-only hit scores 0). "
            "Compare scores within one call only."
        ),
        "guidance": (
            "Each opportunity is one cluster of question-shaped searches sharing a seed and a "
            "title angle. Write one page per cluster: answer the questions in order, include "
            "the citability_hints evidence, quote or pitch news.top_publishers, and use the "
            "exact Wikipedia article (or verify the suggested related hit/gap) as the brief says. "
            "Expand a cluster with mine_questions(seed) for the full prefix list; "
            "validate with interest_over_time."
        ),
        "budget_note": (
            f"Worst case {1 + len(kws) * per_seed_requests} upstream requests per uncached call; "
            "Google results are cached 15 min / 24 h, Wikipedia 24 h."
        ),
    }
    if cooling:
        out["partial"] = (
            "Google started rate limiting during this call: later seeds have fewer questions "
            "and no news figures (see seeds[].skipped and errors). Wikipedia data is complete."
        )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out
