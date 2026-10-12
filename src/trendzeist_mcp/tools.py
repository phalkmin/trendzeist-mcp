"""Tool implementations, independent of the MCP transport.

Each function validates its inputs, calls the sources on a :class:`Hub` and
returns a JSON-serialisable dict. ``server.py`` registers these with MCP.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from typing import Any, Callable, TypeVar
from urllib.parse import quote

import pandas as pd

from . import __version__
from . import formatters as fmt
from . import validation as v
from .archive import GROUP_RELATED, GROUP_TREND, TREND_KINDS, parse_ts
from .client import TrendsError, hl_for_geo
from .sources import Hub

# Bump when a field is removed or changes meaning; adding fields is compatible.
# v2: wiki_attention.has_article now requires an exact-title match.
# v3: aeo_opportunities.score / score_breakdown use the unified explainable score
#     (components demand / momentum / freshness / gap / aeo with points, max, reason).
SCHEMA_VERSION = 3

# Window the archive compares across weeks (what_changed) and composites use for
# trend context. Related queries are archived on whatever timeframe was asked;
# what_changed reads the related_queries / discover_topics default.
TREND_TIMEFRAME = "today 12-m"
RELATED_TIMEFRAME = "today 3-m"

T = TypeVar("T")

# mine_questions (roadmap A1): interrogative prefixes expanded through Google
# Autocomplete. Capped so a full run is at most len(_QUESTION_PREFIXES) + 1
# throttled requests; no a-z suffix expansion (too many requests for the gain).
_QUESTION_PREFIXES: tuple[str, ...] = (
    "how to", "how do", "how much", "how long", "why", "what is", "what are", "when",
    "where", "which", "can", "should", "is", "does", "vs",
)


def _setting(hub: Hub, name: str, default: Any) -> Any:
    return getattr(getattr(hub, "settings", None), name, default)


def _hl_for(hub: Hub, geo: str) -> str:
    settings = getattr(hub, "settings", None)
    hl_for = getattr(settings, "hl_for", None)
    return hl_for(geo) if callable(hl_for) else "en-US"


def _query_meta(
    hub: Hub, keywords: list[str], timeframe: str, geo: str, category: int, gprop: str
) -> dict:
    return {
        "keywords": keywords,
        "timeframe": timeframe,
        "geo": geo or "worldwide",
        "hl": _hl_for(hub, geo),
        "category": category,
        "gprop": gprop or "web",
    }


def finalize(hub: Hub, out: dict[str, Any]) -> dict[str, Any]:
    """Attach ``schema_version`` and ``_meta`` (roadmap C11, N15)."""
    out["schema_version"] = SCHEMA_VERSION
    meta = getattr(hub, "call_meta", None)
    if callable(meta):
        out["_meta"] = meta()
    return out


def fetch_recording(hub: Hub, fetch: Callable[[], T], record: Callable[[T], Any]) -> T:
    """Run ``fetch`` and call ``record(result)`` only when it hit the network.

    The archive stores observations, not cache reads: a second call within the
    TTL must not create a second snapshot. A hub without ``stats`` (tests) or
    without an archive records always / never respectively. ``record`` is best
    effort: the archive swallows its own errors.
    """
    archive = getattr(hub, "archive", None)
    stats = getattr(hub, "stats", None)
    before = stats.misses() if stats is not None and hasattr(stats, "misses") else None
    result = fetch()
    if archive is None:
        return result
    after = stats.misses() if before is not None else None
    if before is None or (after is not None and after > before):
        record(result)
    return result


def _record_trend_summaries(
    hub: Hub, summary: dict[str, Any], geo: str, timeframe: str, category: int, gprop: str
) -> None:
    """Archive one trend snapshot per keyword; only plain web / all-category series."""
    if category or gprop:
        return
    for kw, s in summary.items():
        hub.archive.record_trend(kw, geo, timeframe, s)


def _common(timeframe: str, geo: str, category: int, gprop: str) -> tuple[str, str, int, str]:
    return (
        v.validate_timeframe(timeframe),
        v.validate_geo(geo),
        v.validate_category(category),
        v.validate_gprop(gprop),
    )


def interest_over_time(
    hub: Hub,
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    kws = v.validate_keywords(keywords)
    tf, g, cat, gp = _common(timeframe, geo, category, gprop)
    out = fetch_trend(hub, kws, tf, g, cat, gp)
    out["query"] = _query_meta(hub, kws, tf, g, cat, gp)
    return out


def fetch_trend(
    hub: Hub,
    kws: list[str],
    tf: str,
    g: str,
    cat: int = 0,
    gp: str = "",
    *,
    max_points: int | None = None,
) -> dict[str, Any]:
    """Fetch + format an IOT frame, archiving each keyword's labels on a cache miss.

    Shared by ``interest_over_time`` and the composites so every trend lookup
    feeds ``what_changed`` / ``trend_history``. ``max_points`` overrides the
    downsampling cap (``seasonality`` needs every weekly point).
    """
    if max_points is None:
        max_points = _setting(hub, "max_series_points", fmt.MAX_SERIES_POINTS)
    return fetch_recording(
        hub,
        lambda: fmt.interest_over_time(
            hub.trends.interest_over_time(kws, tf, g, cat, gp), kws, max_points=max_points
        ),
        lambda out: _record_trend_summaries(hub, out["summary"], g, tf, cat, gp),
    )


def fetch_related(
    hub: Hub, kw: str, tf: str, g: str, cat: int, gp: str, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Related queries as ``(top, rising)`` in Google's order; archives on a miss.

    ``rising`` items carry ``growth_pct`` / ``is_breakout`` and, where detected,
    ``suspect`` / ``suspect_reason`` (split with ``fmt.split_suspects``).

    Suspect detection and the archive both see the *complete* lists Google
    returned; ``limit`` is applied only to what is handed back, so a caller's
    display limit can never make ``what_changed`` report invented drops.
    """

    def fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        res = hub.trends.related_queries(kw, tf, g, cat, gp)
        top = fmt.related_list(res.get("top"), None, label="query")
        rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), None, label="query"))
        fmt.flag_suspects(rising, top, kw)
        return top, rising

    def record(pair: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> None:
        if cat or gp:
            return
        top, rising = pair
        hub.archive.record_related(kw, g, tf, top, fmt.split_suspects(rising)[0])

    top, rising = fetch_recording(hub, fetch, record)
    return top[:limit], rising[:limit]


def compare_keywords(
    hub: Hub,
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    kws = v.validate_keywords(keywords, minimum=2)
    data = interest_over_time(hub, kws, timeframe, geo, category, gprop)
    summary = data["summary"]
    means = {kw: s.get("mean", 0) or 0 for kw, s in summary.items() if s.get("available")}
    total = sum(means.values()) or 1.0
    ranking = sorted(means.items(), key=lambda kv: kv[1], reverse=True)
    out: dict[str, Any] = {
        "query": data["query"],
        "ranking": [
            {
                "keyword": kw,
                "mean_interest": mean,
                "share_pct": round(100 * mean / total, 1),
                "direction": summary[kw].get("direction"),
                "direction_now": summary[kw].get("direction_now"),
                "latest": summary[kw].get("latest"),
            }
            for kw, mean in ranking
        ],
        "leader": ranking[0][0] if ranking else None,
        "unavailable": [kw for kw, s in summary.items() if not s.get("available")],
        "points": data["points"],
        "scale": data.get("scale"),
    }
    if "note" in data:
        out["note"] = data["note"]
    return out


def related_queries(
    hub: Hub,
    keyword: str,
    timeframe: str = "today 3-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    kw = v.validate_keyword(keyword)
    tf, g, cat, gp = _common(timeframe, geo, category, gprop)
    lim = v.validate_limit(limit, default=25, maximum=50)
    top, rising = fetch_related(hub, kw, tf, g, cat, gp, lim)
    fmt.tag_angles(top)
    fmt.tag_angles(rising)
    clean_rising, suspects = fmt.split_suspects(rising)
    out: dict[str, Any] = {
        "query": _query_meta(hub, [kw], tf, g, cat, gp),
        "top": top,
        "rising": rising,
        "questions": fmt.extract_questions(("rising", clean_rising), ("top", top), limit=lim),
        "available": bool(top or rising),
        "notes": {
            "top": "value = relative popularity 0-100 among related searches",
            "rising": "value = % growth vs. previous period; is_breakout = >5000% (new/exploding)",
            "angle": "title angle: how-to | comparison | listicle | definition | news | null",
            "questions": "question-shaped related searches (rising first), deduplicated",
        },
    }
    if suspects:
        out["suspect_count"] = len(suspects)
        out["notes"]["suspect"] = (
            "rising items with suspect=true look like injected spam (one name attached to "
            "unrelated queries, or a domain); they are excluded from questions. Treat "
            "their is_breakout as unreliable."
        )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def related_topics(
    hub: Hub,
    keyword: str,
    timeframe: str = "today 3-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    kw = v.validate_keyword(keyword)
    tf, g, cat, gp = _common(timeframe, geo, category, gprop)
    lim = v.validate_limit(limit, default=25, maximum=50)
    res = hub.trends.related_topics(kw, tf, g, cat, gp)
    top = fmt.related_list(res.get("top"), lim, label="topic_title")
    rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), lim, label="topic_title"))
    out: dict[str, Any] = {
        "query": _query_meta(hub, [kw], tf, g, cat, gp),
        "top": top,
        "rising": rising,
        "available": bool(top or rising),
    }
    if not out["available"]:
        out["reason"] = (
            "Google returned no related topics for this query. This endpoint is "
            "unreliable; use related_queries instead."
        )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def interest_by_region(
    hub: Hub,
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    resolution: str = "COUNTRY",
    category: int = 0,
    gprop: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    kws = v.validate_keywords(keywords)
    tf, g, cat, gp = _common(timeframe, geo, category, gprop)
    res = v.validate_resolution(resolution, g)
    lim = v.validate_limit(limit, default=20, maximum=100)
    df = hub.trends.interest_by_region(kws, tf, g, cat, gp, res)
    regions = fmt.region_list(df, kws, lim)
    out: dict[str, Any] = {
        "query": {**_query_meta(hub, kws, tf, g, cat, gp), "resolution": res},
        "regions": regions,
        "available": bool(regions),
        "scale": "0-100 relative to the region with the highest share of searches",
    }
    if not regions:
        if df is None or df.empty or not any(kw in df.columns for kw in kws):
            out["reason"] = "Google returned no regional data for this query."
        else:
            out["reason"] = (
                "Every region reported zero interest (rows with no searches are dropped). "
                "Search volume is too low at this resolution; try a broader keyword, a "
                "wider geo or a longer timeframe."
            )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def suggest_keywords(hub: Hub, keyword: str) -> dict[str, Any]:
    kw = v.validate_keyword(keyword)
    items = hub.trends.suggestions(kw)
    return {
        "keyword": kw,
        "suggestions": [
            {"title": s.get("title"), "type": s.get("type"), "mid": s.get("mid")} for s in items
        ],
        "note": "Pass a 'mid' (e.g. '/m/05z1_') as a keyword to query the disambiguated topic.",
    }


def trending_now(
    hub: Hub, geo: str = "US", max_articles: int = 3, topic_filter: str = ""
) -> dict[str, Any]:
    g = v.validate_geo(geo) or "US"
    arts = v.validate_limit(max_articles, default=3, maximum=10) if max_articles else 0
    flt = v.validate_topic_filter(topic_filter)
    items = fetch_recording(
        hub,
        lambda: hub.trends.trending_rss(g, arts),
        lambda items: hub.archive.record_trending(
            g, [{"title": it.get("title"), "traffic": it.get("traffic")} for it in items]
        ),
    )
    total = len(items)
    if flt:
        tokens = fmt.normalise_query(flt).split()

        def matches(it: dict[str, Any]) -> bool:
            text = fmt.normalise_query(
                " ".join([it.get("title") or "", *(a.get("title") or "" for a in it.get("articles") or [])])
            )
            words = set(text.split())
            return all(t in words for t in tokens)

        items = [it for it in items if matches(it)]
    trends: list[dict[str, Any]] = []
    for it in items:
        entry: dict[str, Any] = {
            "title": it.get("title"),
            "traffic": it.get("traffic"),
            "published": it.get("pub_date"),
            "angle": fmt.classify_angle(it.get("title")) or "news",
        }
        if arts:
            entry["articles"] = [
                {"title": a.get("title"), "source": a.get("source"), "url": a.get("url")}
                for a in (it.get("articles") or [])[:arts]
            ]
        trends.append(entry)
    out: dict[str, Any] = {"geo": g, "count": len(trends), "trends": trends}
    if flt:
        out["topic_filter"] = flt
        out["unfiltered_count"] = total
    if not trends:
        out["reason"] = (
            f"None of the {total} trending searches match topic_filter '{flt}' (all words must "
            "appear in the trend title or its headlines). Drop the filter or use a broader word."
            if flt and total
            else "The trending feed returned no items for this geo right now."
        )
    note = v.limit_note(max_articles if max_articles else None, arts, name="max_articles")
    if note:
        out["note"] = note
    return out


def list_categories(hub: Hub, search: str = "", limit: int = 50) -> dict[str, Any]:
    lim = v.validate_limit(limit, default=50, maximum=500)
    needle = (search or "").strip().lower()
    rows = fmt.flatten_categories(hub.trends.categories())
    if needle:
        rows = [r for r in rows if needle in r["path"].lower()]
    out: dict[str, Any] = {"search": search, "total_matches": len(rows), "categories": rows[:lim]}
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def mine_questions(
    hub: Hub,
    seed: str,
    geo: str = "",
    limit: int = 30,
    *,
    prefixes: tuple[str, ...] = _QUESTION_PREFIXES,
) -> dict[str, Any]:
    """Expand a seed through Google Autocomplete into question-shaped long-tail queries.

    Each prefix ("how to", "why", ...) is one throttled request; the seed alone
    is queried first so the model gets *something* even when a 429 interrupts
    the expansion. Partial results are returned with the failure in ``errors``.
    """
    kw = v.validate_keyword(seed)
    g = v.validate_geo(geo)
    lim = v.validate_limit(limit, default=30, maximum=100)

    seen: set[str] = set()
    questions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    prefixes_done: list[str] = []
    # Spread the budget across prefixes so a small limit still yields a mix of
    # angles instead of ten "how to ..." variants from the first request.
    per_prefix = max(3, math.ceil(lim / len(prefixes)))
    # The seed must survive as whole words: Autocomplete completes an unfinished
    # last token ("wordpress ai" -> "wordpress airplay"), which is off-topic.
    seed_re = re.compile(rf"\b{re.escape(fmt.normalise_query(kw))}s?\b")

    def absorb(prefix: str, suggestions: list[str]) -> None:
        taken = 0
        for text in suggestions:
            if taken >= per_prefix:
                break
            text = " ".join(text.split())
            norm = fmt.normalise_query(text)
            if not norm or norm in seen:
                continue
            if not (fmt.is_question(text) or prefix == "vs"):
                continue
            if not seed_re.search(norm):
                continue
            seen.add(norm)
            taken += 1
            questions.append(
                {
                    "question": text,
                    "angle": fmt.classify_angle(text) or ("comparison" if prefix == "vs" else "how-to"),
                    "prefix": prefix or None,
                }
            )

    stopped = False
    fetched_fresh = False

    def mark_fresh(_: Any) -> None:
        nonlocal fetched_fresh
        fetched_fresh = True

    for prefix in ("", *prefixes):
        if len(questions) >= lim:
            break
        # Trailing space: ask Google for the *next* word instead of completing the
        # seed's last token.
        query = f"{prefix} {kw} ".lstrip() if prefix != "vs" else f"{kw} vs "
        try:
            absorb(
                prefix,
                fetch_recording(hub, lambda: hub.autocomplete.suggest(query, g), mark_fresh),
            )
            prefixes_done.append(prefix or "(seed)")
        except TrendsError as exc:
            errors.append({"prefix": prefix or "(seed)", "error": str(exc)})
            if exc.retryable:
                stopped = True
                break

    questions = questions[:lim]
    if fetched_fresh and questions and getattr(hub, "archive", None) is not None:
        hub.archive.record_questions(kw, g, questions)
    by_angle: dict[str, int] = {}
    for q in questions:
        by_angle[q["angle"]] = by_angle.get(q["angle"], 0) + 1

    out: dict[str, Any] = {
        "query": {"seed": kw, "geo": g or "worldwide", "hl": _hl_for(hub, g)},
        "questions": questions,
        "count": len(questions),
        "by_angle": by_angle,
        "prefixes_queried": prefixes_done,
        "errors": errors,
        "guidance": (
            "Each question is a candidate H2/FAQ entry or post title. Validate the "
            "strongest with related_queries / interest_over_time; pair angle with "
            "evidence type (how-to: steps + numbers; comparison: table + quotes; "
            "definition: cite a primary source; news: dated publisher quotes)."
        ),
    }
    if stopped:
        out["partial"] = (
            "Expansion stopped early because Google is rate limiting; results above "
            "are complete for the prefixes listed in prefixes_queried."
        )
    if not out["query"]["hl"].lower().startswith("en"):
        out["language_note"] = (
            "Question prefixes are English, so expansions are English-biased even though "
            "Autocomplete ran with hl=" + out["query"]["hl"] + ". For native-language "
            "questions pass a seed already phrased in that language (e.g. 'como fazer "
            "espresso')."
        )
    if not questions and not errors:
        out["reason"] = (
            "Autocomplete produced no question-shaped suggestions for this seed. Try a "
            "shorter or more common phrasing, or a different geo."
        )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out



def news_coverage(hub: Hub, topic: str, geo: str = "US", limit: int = 10) -> dict[str, Any]:
    """Who covers a topic in Google News, how often and how recently (roadmap A5)."""
    kw = v.validate_keyword(topic)
    g = (v.validate_geo(geo) or "US").split("-")[0]
    lim = v.validate_limit(limit, default=10, maximum=50)
    articles = hub.news.articles(kw, g)
    recency = fmt.recency_histogram(articles)
    out: dict[str, Any] = {
        "query": {"topic": kw, "geo": g, "hl": hl_for_geo(g)},
        "articles_total": len(articles),
        "headlines": [
            {
                "title": a["title"],
                "publisher": a["publisher"],
                "published": a["published"],
                "url": a["url"],
            }
            for a in articles[:lim]
        ],
        "publishers": fmt.publisher_table(articles, limit=15),
        "recency": recency,
        "coverage": fmt.coverage_level(recency),
        "available": bool(articles),
        "guidance": (
            "publishers = mention / pitch targets ranked by article count (share_pct of all "
            "fetched articles, up to ~100). recency buckets are cumulative by article age. "
            "coverage is a heuristic on the last 30 days: none (0), low (<5), moderate (5-19), "
            "high (20+); high = crowded, low + rising search interest = open field. Quote a "
            "dated headline for a news-angle answer."
        ),
    }
    if not articles:
        out["reason"] = (
            "Google News returned no articles for this topic in this edition. Try a broader "
            "term, the market's language, or geo='US'."
        )
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def wiki_attention(hub: Hub, topic: str, lang: str = "en", days: int = 30) -> dict[str, Any]:
    """Does Wikipedia cover a topic, and is attention to it growing? (roadmap A6)"""
    kw = v.validate_keyword(topic)
    lg = v.validate_lang(lang)
    d = v.validate_limit(days, default=30, maximum=90)
    if d < 7:
        raise v.ValidationError("days must be between 7 and 90")
    page = hub.wikipedia.search(kw, lg)
    exact_match = bool(page.get("found")) and (
        fmt.normalise_query(page["title"]) == fmt.normalise_query(kw)
    )
    out: dict[str, Any] = {
        "query": {"topic": kw, "lang": lg, "days": d},
        "has_article": exact_match,
    }
    if not exact_match:
        if page.get("found"):
            out["related_article"] = page["title"]
        out["suggestion"] = page.get("suggestion") or (
            page["title"] if page.get("found") else None
        )
        out["reason"] = (
            f"No exact-title {lg}.wikipedia.org article was found for '{kw}'. "
            "Search results alone cannot establish that no article exists."
        )
        out["guidance"] = (
            "Check a suggested spelling or related_article for relevance before citing it. "
            "If no relevant article exists, a well-cited definition page may fill a gap."
        )
    else:
        title = page["title"]
        points = hub.wikipedia.pageviews(title, lg, d)
        summary = fmt.pageview_summary(points, title)
        out["article"] = {
            "title": title,
            "url": f"https://{lg}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}",
            "pageid": page.get("pageid"),
            "wordcount": page.get("wordcount"),
            "exact_match": True,
        }
        out["pageviews"] = summary
        out["points"] = points
        out["guidance"] = (
            "pageviews = daily human views of the best-matching article (Wikimedia, all "
            "platforms). direction / growth_pct compare the first and last third of the window; "
            "rising attention with low news coverage = an audience looking for explanations. "
            "Cite the article as the definition anchor and go deeper than it. "
            "Only exact-title matches are treated as confirmed topic articles."
        )
        if not points:
            out["reason"] = "Wikimedia has no pageview data for this article in the window."
    note = v.limit_note(days, d, name="days")
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------------------
# 0.5.0: seasonality (N1)
# ---------------------------------------------------------------------------


def seasonality(hub: Hub, keyword: str, geo: str = "") -> dict[str, Any]:
    """When does demand for a keyword peak each year, and when to publish for it."""
    kw = v.validate_keyword(keyword)
    g = v.validate_geo(geo)
    # Keep every weekly point: 5 years = 261 of them, and monthly means need them all.
    data = fetch_trend(hub, [kw], "today 5-y", g, max_points=10_000)
    summary = data["summary"].get(kw, {"available": False})
    out: dict[str, Any] = {
        "query": {"keyword": kw, "geo": g or "worldwide", "timeframe": "today 5-y", "hl": _hl_for(hub, g)},
        "available": bool(summary.get("available")),
    }
    if not summary.get("available"):
        out.update(seasonal=None, reason="Google returned no data for this keyword (too little search volume?).")
        return out
    series = _series_from_points(data["points"], kw)
    profile = fmt.seasonality_profile(series)
    out.update(profile)
    out["trend"] = {
        "direction": summary.get("direction"),
        "direction_now": summary.get("direction_now"),
        "growth_12m": summary.get("growth_12m"),
    }
    out["guidance"] = (
        "monthly_index = mean interest per calendar month over the full years in the window, "
        "0-100 relative to the strongest month. peak_months recur in a year's top 2 in at least "
        "3 years; seasonal_strength = peak-month mean / other-month mean (seasonal when >= 1.5). "
        "publish_by = 6 weeks before next_peak so a page can rank before demand arrives. "
        "Combine with direction_now: a seasonal topic that is also falling year over year is "
        "a refresh, not a new post."
    )
    return out


def _series_from_points(points: list[dict[str, Any]], kw: str) -> pd.Series:
    """Rebuild a dated series from formatted points (downsampled when very long)."""
    idx: list[pd.Timestamp] = []
    vals: list[float] = []
    for p in points:
        val = (p.get("values") or {}).get(kw)
        if val is None:
            continue
        idx.append(pd.Timestamp(p["date"]))
        vals.append(float(val))
    return pd.Series(vals, index=pd.DatetimeIndex(idx))


# ---------------------------------------------------------------------------
# 0.5.0: compare_channels
# ---------------------------------------------------------------------------


def compare_channels(
    hub: Hub, keyword: str, geo: str = "", timeframe: str = TREND_TIMEFRAME
) -> dict[str, Any]:
    """Where a topic grows: web, YouTube, News, Images or Shopping, with a format hint."""
    kw = v.validate_keyword(keyword)
    g = v.validate_geo(geo)
    tf = v.validate_timeframe(timeframe)
    channels: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    cooling = False
    for gprop, name in fmt.CHANNELS:
        if cooling:
            skipped.append(name)
            continue
        try:
            summary = fetch_trend(hub, [kw], tf, g, 0, gprop)["summary"].get(kw, {"available": False})
        except TrendsError as exc:
            errors.append({"step": f"interest_over_time:{name}", "error": str(exc)})
            if exc.retryable:
                cooling = True
                skipped.append(name)
            continue
        block: dict[str, Any] = {"available": bool(summary.get("available"))}
        if block["available"]:
            block.update(
                direction=summary.get("direction"),
                direction_now=summary.get("direction_now"),
                growth_3m=summary.get("growth_3m"),
                mean=summary.get("mean"),
                latest=summary.get("latest"),
                format_hint=fmt.FORMAT_HINTS[name],
            )
            if summary.get("growth_3m") is None and summary.get("growth_note"):
                block["growth_note"] = summary["growth_note"]
        channels[name] = block

    eligible = {
        n: c
        for n, c in channels.items()
        if c.get("available") and (c.get("mean") or 0) >= fmt.CHANNEL_NOISE_FLOOR and c.get("growth_3m") is not None
    }
    fastest = max(eligible, key=lambda n: eligible[n]["growth_3m"]) if eligible else None
    fastest_growth = eligible[fastest]["growth_3m"] if fastest else None
    if fastest_growth is None:
        momentum = "unknown"
    elif fastest_growth <= -5:
        momentum = "all_declining"
    elif fastest_growth < 5:
        momentum = "flat"
    else:
        momentum = "growing"
    out: dict[str, Any] = {
        "query": {"keyword": kw, "geo": g or "worldwide", "timeframe": tf, "hl": _hl_for(hub, g)},
        "channels": channels,
        "fastest_channel": fastest,
        "momentum": momentum,
        "format_hint": fmt.FORMAT_HINTS.get(fastest) if fastest else None,
        "insight": fmt.channel_insight(channels, fastest),
        "skipped": skipped,
        "errors": errors,
        "note": (
            "Each channel is a separate Google request on its own 0-100 scale, so only "
            "direction / direction_now / growth_3m are comparable across channels, never mean "
            "or latest. fastest_channel considers channels with mean >= 5 and a growth_3m value; "
            "momentum says whether that 'fastest' is actually growing (growing / flat / "
            "all_declining / unknown). Treat format_hint as advice only when momentum is growing."
        ),
    }
    if skipped:
        out["partial"] = (
            "Google started rate limiting during this call; channels listed in skipped were not "
            "fetched. Retry in ~60 s for the missing ones."
        )
    if not channels:
        out["reason"] = "No channel returned data; see errors."
    return out



# ---------------------------------------------------------------------------
# 0.5.0: archive readers (N4 what_changed, trend_history)
# ---------------------------------------------------------------------------
_ARCHIVE_OFF_REASON = (
    "The observation archive is disabled (TRENDZEIST_ARCHIVE=0 or cache dir off), so there "
    "is no history to compare. Enable it and use the tools for a week to build a baseline."
)
# Editorial actions for what_changed. Disappearance from Google's *relative, bounded*
# related list is not evidence of falling demand, so nothing here says "retire":
# that needs an interest_over_time check on the item itself (see SKILL.md).
ACTION_INVESTIGATE = "investigate now"
ACTION_CONSIDER = "consider"
ACTION_PROMOTED = "investigate now (promoted)"
ACTION_COOLED = "review: no longer rising (check interest_over_time before retiring)"
ACTION_DROPPED = "review: left Google's list (check interest_over_time before retiring)"
ACTION_REFRESH = "refresh existing post"


def _archive(hub: Hub) -> Any | None:
    archive = getattr(hub, "archive", None)
    return archive if archive is not None and getattr(archive, "enabled", False) else None


def _fmt_day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def _seed_changes(
    hub: Hub, archive: Any, seed: str, g: str, since_days: int
) -> dict[str, Any]:
    """Compare the newest related-queries snapshot with one >= since_days older."""
    report: dict[str, Any] = {"seed": seed}
    latest, baseline = archive.latest_and_baseline(
        seed, g, GROUP_RELATED, RELATED_TIMEFRAME, since_days
    )
    report["current_at"] = latest["observed_at"] if latest else None
    report["baseline_at"] = baseline["observed_at"] if baseline else None
    if latest is None:
        report["reason"] = "No snapshot recorded yet for this seed; rerun after the next fetch."
        return report
    if baseline is None:
        snaps = archive.snapshots(seed, g, GROUP_RELATED, RELATED_TIMEFRAME)
        oldest = snaps[-1]["observed_at"]
        oldest_dt = parse_ts(oldest)
        rerun = (oldest_dt + timedelta(days=since_days)).date().isoformat() if oldest_dt else "?"
        report["reason"] = (
            f"baseline recorded {_fmt_day(oldest)}; rerun after {rerun} (or lower since_days)."
        )
        return report

    now_rows = archive.observations(latest["id"])
    base_rows = archive.observations(baseline["id"])
    report["current_items"] = latest["item_count"]
    report["baseline_items"] = baseline["item_count"]
    if latest["item_count"] == 0 or baseline["item_count"] == 0:
        # One side is an empty answer from Google: comparing would call everything
        # new or dropped. Say so instead.
        side = "current" if latest["item_count"] == 0 else "baseline"
        report["reason"] = (
            f"The {side} snapshot has no related queries (Google returned nothing); no "
            "comparison is possible. If this persists, search volume for the seed has fallen "
            "below Google's threshold."
        )
        return report

    now_rising = {r["item"]: r for r in now_rows if r["kind"] == "rising_query"}
    base_rising = {r["item"]: r for r in base_rows if r["kind"] == "rising_query"}
    now_top = {r["item"]: r for r in now_rows if r["kind"] == "top_query"}
    base_top = {r["item"]: r for r in base_rows if r["kind"] == "top_query"}
    base_items = set(base_rising) | set(base_top)
    now_items = set(now_rising) | set(now_top)

    def entry(item: str, row: dict[str, Any], action: str, **extra: Any) -> dict[str, Any]:
        return {
            "topic": item,
            "status": row.get("status"),
            "value": row.get("value"),
            "first_seen": _fmt_day(archive.first_seen(item, g)),
            "action": action,
            **extra,
        }

    new_breakouts = [
        entry(i, r, ACTION_INVESTIGATE)
        for i, r in now_rising.items()
        if r["status"] == "breakout" and i not in base_items
    ]
    # Promoted: known item (top, or rising-but-not-breakout) that is a breakout now.
    promoted = [
        entry(i, r, ACTION_PROMOTED, was="rising" if i in base_rising else "top")
        for i, r in now_rising.items()
        if r["status"] == "breakout"
        and i in base_items
        and (base_rising.get(i) or {}).get("status") != "breakout"
    ]
    new_rising = [
        entry(i, r, ACTION_CONSIDER)
        for i, r in now_rising.items()
        if r["status"] == "rising" and i not in base_items
    ]
    cooled = [
        entry(i, now_top[i], ACTION_REFRESH if i in base_top else ACTION_COOLED, was="rising")
        for i in base_rising
        if i not in now_rising and i in now_top
    ]
    dropped = []
    for i in sorted(base_items - now_items):
        row = base_rising.get(i) or base_top[i]
        was = "rising" if i in base_rising else "top"
        dropped.append(entry(i, row, ACTION_DROPPED, was=was))
    rank_moves = []
    for i in set(now_top) & set(base_top):
        delta = base_top[i]["rank"] - now_top[i]["rank"]
        if abs(delta) >= 3:
            rank_moves.append(
                {"topic": i, "from_rank": base_top[i]["rank"], "to_rank": now_top[i]["rank"], "delta": delta}
            )
    rank_moves.sort(key=lambda m: -abs(m["delta"]))

    # direction flip on direction_now over the composites' window
    flip = None
    t_latest, t_base = archive.latest_and_baseline(
        seed, g, GROUP_TREND, TREND_TIMEFRAME, since_days
    )
    if t_latest and t_base:

        def dir_now(snap: dict[str, Any]) -> str | None:
            return next(
                (r["status"] for r in archive.observations(snap["id"]) if r["kind"] == "direction_now"),
                None,
            )

        now_dir, base_dir = dir_now(t_latest), dir_now(t_base)
        if now_dir and base_dir and now_dir != base_dir:
            flip = {"from": base_dir, "to": now_dir, "field": "direction_now"}

    report.update(
        new_breakouts=new_breakouts,
        promoted=promoted,
        new_rising=new_rising,
        cooled=cooled,
        dropped=dropped,
        rank_moves=rank_moves[:10],
        direction_flip=flip,
        counts={
            "new_breakouts": len(new_breakouts),
            "promoted": len(promoted),
            "new_rising": len(new_rising),
            "cooled": len(cooled),
            "dropped": len(dropped),
            "rank_moves": len(rank_moves),
        },
    )
    return report


def what_changed(
    hub: Hub, seeds: list[str], geo: str = "", since_days: int = 7
) -> dict[str, Any]:
    """What is new, promoted, cooling or gone in a seed's related queries since a baseline."""
    kws = v.validate_keywords(seeds)
    g = v.validate_geo(geo)
    days = v.validate_since_days(since_days)
    archive = _archive(hub)
    out: dict[str, Any] = {
        "query": {"seeds": kws, "geo": g or "worldwide", "since_days": days,
                  "related_timeframe": RELATED_TIMEFRAME, "trend_timeframe": TREND_TIMEFRAME},
        "seeds": [],
        "errors": [],
        "guidance": (
            "Actions: 'investigate now' = a breakout absent from the baseline, or a known "
            "query promoted to breakout (time-sensitive); 'consider' = newly rising; "
            "'refresh existing post' = a long-standing top query that briefly rose and settled "
            "back; 'review: ...' = a query that stopped rising or left Google's list. Leaving "
            "a relative, bounded list is NOT proof that demand fell: run "
            "interest_over_time([<topic>], 'today 12-m') and retire only if direction_now is "
            "falling. first_seen is the earliest archive date for the item under any seed. "
            "direction_flip compares direction_now on the 12-month window."
        ),
    }
    if archive is None:
        out["available"] = False
        out["reason"] = _ARCHIVE_OFF_REASON
        return out

    # Refresh current data through the normal (recording) paths first.
    cooling = False
    try:
        fetch_trend(hub, kws, TREND_TIMEFRAME, g)
    except TrendsError as exc:
        out["errors"].append({"step": "interest_over_time", "error": str(exc)})
    for seed in kws:
        if not cooling:
            try:
                fetch_related(hub, seed, RELATED_TIMEFRAME, g, 0, "", 25)
            except TrendsError as exc:
                out["errors"].append({"step": f"related_queries:{seed}", "error": str(exc)})
                cooling = cooling or exc.retryable
        out["seeds"].append(_seed_changes(hub, archive, seed, g, days))
    out["available"] = any("counts" in s for s in out["seeds"])
    if cooling:
        out["partial"] = "Google rate limited this call; comparisons use the newest stored snapshot."
    note = v.limit_note(since_days, days, name="since_days")
    if note:
        out["note"] = note
    return out


def _breakout_episodes(rows: list[dict[str, Any]], all_snapshots: list[dict[str, Any]]) -> int:
    """Count separate breakout *episodes* for an item under one seed.

    ``rows`` are this item's rising rows under the seed; ``all_snapshots`` are every
    related snapshot of that seed in the window (so a snapshot where the item was
    not a breakout ends the run). Polling the same breakout three times is one episode.
    """
    breakout_ids = {r["snapshot_id"] for r in rows if r["status"] == "breakout"}
    episodes = 0
    inside = False
    for snap in sorted(all_snapshots, key=lambda s: s["observed_at"]):
        is_breakout = snap["id"] in breakout_ids
        if is_breakout and not inside:
            episodes += 1
        inside = is_breakout
    return episodes


def trend_history(hub: Hub, keyword: str, geo: str = "", days: int = 90) -> dict[str, Any]:
    """Everything the archive has seen about a keyword; makes no upstream request."""
    kw = v.validate_keyword(keyword)
    g = v.validate_geo(geo)
    d = v.validate_days(days, default=90)
    archive = _archive(hub)
    out: dict[str, Any] = {
        "query": {"keyword": kw, "geo": g or "worldwide", "days": d},
        "available": False,
    }
    if archive is None:
        out["reason"] = _ARCHIVE_OFF_REASON
        return out
    rows = archive.history(kw, g, d)
    note = v.limit_note(days, d, name="days")
    if not rows:
        out["reason"] = (
            "No observations for this keyword yet. History accrues as discover_topics, "
            "related_queries, interest_over_time, mine_questions and trending_now fetch fresh "
            "data; run what_changed to start a baseline."
        )
        if note:
            out["note"] = note
        return out
    key = fmt.normalise_query(kw)
    as_seed = [r for r in rows if r["seed"] == key]
    as_item = [r for r in rows if r["item"] == key and r["seed"] != key]

    # One timeline per timeframe: a 5-y label and a 12-m label on the same day are
    # different facts, not the same fact twice.
    timelines: dict[str, dict[str, dict[str, Any]]] = {}
    for r in as_seed:
        if r["kind"] in TREND_KINDS:
            day = timelines.setdefault(r["timeframe"], {}).setdefault(r["observed_at"][:10], {})
            day[r["kind"]] = r["value"] if r["kind"] == "growth_3m" else r["status"]
    direction_timeline = {
        tf: [{"date": day, **vals} for day, vals in sorted(days_.items())]
        for tf, days_ in timelines.items()
    }

    seeds_seen = sorted({r["seed"] for r in as_item})
    window_start = rows[0]["observed_at"]
    episodes = 0
    # Related lists are archived on whatever timeframe was requested, so a run is
    # tracked per (seed, timeframe): a 12-m breakout must not be ended (or hidden)
    # by the 3-m snapshots of the same seed.
    rising_pairs = sorted({(r["seed"], r["timeframe"]) for r in as_item if r["kind"] == "rising_query"})
    for s, tf in rising_pairs:
        under = [
            r for r in as_item if r["seed"] == s and r["timeframe"] == tf and r["kind"] == "rising_query"
        ]
        snaps = archive.snapshots(s, g, GROUP_RELATED, tf)
        episodes += _breakout_episodes(under, [sn for sn in snaps if sn["observed_at"] >= window_start])
    breakout_obs = {
        r["snapshot_id"] for r in as_item if r["kind"] == "rising_query" and r["status"] == "breakout"
    }
    rising_obs = {r["snapshot_id"] for r in as_item if r["kind"] == "rising_query"}
    out.update(
        available=True,
        first_seen=_fmt_day(rows[0]["observed_at"]),
        last_seen=_fmt_day(rows[-1]["observed_at"]),
        observations=len(rows),
        breakout_episodes=episodes,
        breakout_observations=len(breakout_obs),
        rising_observations=len(rising_obs),
        seen_under_seeds=seeds_seen,
        direction_timeline=direction_timeline,
        latest_rank=next((r["rank"] for r in reversed(as_item) if r["kind"] == "top_query"), None),
        questions_recorded=len({r["item"] for r in as_seed if r["kind"] == "question"}),
        guidance=(
            "breakout_episodes counts separate runs of 'breakout' status under other seeds "
            "(a run ends when a later snapshot of that seed no longer lists it as breakout); "
            ">= 3 episodes in 90 days = a recurring topic worth a durable page. "
            "breakout_observations is only how many snapshots saw it (polling frequency, not "
            "recurrence). direction_timeline is keyed by timeframe; trust the most recent "
            "direction_now of the 'today 12-m' series. Archive-only: nothing was fetched."
        ),
    )
    if note:
        out["note"] = note
    return out


def trendzeist_status(hub: Hub) -> dict[str, Any]:
    """Which sources are live / erroring / idle in this process, plus cache and settings (A9).

    Never probes an upstream: it only reports what earlier calls observed.
    """
    s = hub.settings
    archive = getattr(hub, "archive", None)
    return {
        "version": __version__,
        "sources": hub.statuses(),
        "cache": hub.cache.stats(),
        "archive": archive.status() if archive is not None else {"enabled": False},
        "settings": {
            "hl": "auto (follows geo)" if s.auto_hl else s.hl,
            "tz": s.tz,
            "min_interval_s": s.min_interval,
            "wiki_min_interval_s": s.wiki_min_interval,
            "retries": s.retries,
            "ttl_s": {"explore": s.explore_ttl, "rss": s.rss_ttl, "static": s.static_ttl},
            "max_memory_entries": s.max_memory_entries,
            "max_series_points": s.max_series_points,
            "proxies_configured": len(s.proxies),
        },
        "guidance": (
            "state: live = the source's last network call succeeded; error = it failed (see "
            "last_error; a later success flips it back); idle = no network call yet in this "
            "process (cache hits do not count). Nothing is probed by this tool, so it is free "
            "to call. If google_* sources show error with a 429 message, wait a minute and "
            "prefer cached queries; wikipedia has its own rate lane and keeps working. "
            "archive = the local observation history behind what_changed / trend_history "
            "(rows accrue as other tools fetch fresh data; last_error never fails a tool)."
        ),
    }

