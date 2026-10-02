"""Tool implementations, independent of the MCP transport.

Each function validates its inputs, calls the sources on a :class:`Hub` and
returns a JSON-serialisable dict. ``server.py`` registers these with MCP.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

from . import __version__
from . import formatters as fmt
from . import validation as v
from .client import TrendsError, hl_for_geo
from .sources import Hub

# Bump when a field is removed or changes meaning; adding fields is compatible.
# v2: wiki_attention.has_article now requires an exact-title match.
SCHEMA_VERSION = 2

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
    df = hub.trends.interest_over_time(kws, tf, g, cat, gp)
    out = fmt.interest_over_time(
        df, kws, max_points=_setting(hub, "max_series_points", fmt.MAX_SERIES_POINTS)
    )
    out["query"] = _query_meta(hub, kws, tf, g, cat, gp)
    return out


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
    res = hub.trends.related_queries(kw, tf, g, cat, gp)
    top = fmt.tag_angles(fmt.related_list(res.get("top"), lim, label="query"))
    rising = fmt.tag_angles(
        fmt.mark_breakouts(fmt.related_list(res.get("rising"), lim, label="query"))
    )
    fmt.flag_suspects(rising, top, kw)
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


def trending_now(hub: Hub, geo: str = "US", max_articles: int = 3) -> dict[str, Any]:
    g = v.validate_geo(geo) or "US"
    arts = v.validate_limit(max_articles, default=3, maximum=10) if max_articles else 0
    items = hub.trends.trending_rss(g, arts)
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
    if not trends:
        out["reason"] = "The trending feed returned no items for this geo right now."
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
    for prefix in ("", *prefixes):
        if len(questions) >= lim:
            break
        # Trailing space: ask Google for the *next* word instead of completing the
        # seed's last token.
        query = f"{prefix} {kw} ".lstrip() if prefix != "vs" else f"{kw} vs "
        try:
            absorb(prefix, hub.autocomplete.suggest(query, g))
            prefixes_done.append(prefix or "(seed)")
        except TrendsError as exc:
            errors.append({"prefix": prefix or "(seed)", "error": str(exc)})
            if exc.retryable:
                stopped = True
                break

    questions = questions[:lim]
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


def trendzeist_status(hub: Hub) -> dict[str, Any]:
    """Which sources are live / erroring / idle in this process, plus cache and settings (A9).

    Never probes an upstream: it only reports what earlier calls observed.
    """
    s = hub.settings
    return {
        "version": __version__,
        "sources": hub.statuses(),
        "cache": hub.cache.stats(),
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
            "prefer cached queries; wikipedia has its own rate lane and keeps working."
        ),
    }

