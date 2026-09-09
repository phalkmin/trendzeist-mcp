"""Tool implementations, independent of the MCP transport.

Each function validates its inputs, calls :class:`TrendsClient` and returns a
JSON-serialisable dict. ``server.py`` registers these with MCP.
"""

from __future__ import annotations

from typing import Any

from . import formatters as fmt
from . import validation as v
from .client import TrendsClient


def _query_meta(keywords: list[str], timeframe: str, geo: str, category: int, gprop: str) -> dict:
    return {
        "keywords": keywords,
        "timeframe": timeframe,
        "geo": geo or "worldwide",
        "category": category,
        "gprop": gprop or "web",
    }


def _common(timeframe: str, geo: str, category: int, gprop: str) -> tuple[str, str, int, str]:
    return (
        v.validate_timeframe(timeframe),
        v.validate_geo(geo),
        v.validate_category(category),
        v.validate_gprop(gprop),
    )


def interest_over_time(
    client: TrendsClient,
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    kws = v.validate_keywords(keywords)
    tf, g, cat, gp = _common(timeframe, geo, category, gprop)
    df = client.interest_over_time(kws, tf, g, cat, gp)
    out = fmt.interest_over_time(df, kws)
    out["query"] = _query_meta(kws, tf, g, cat, gp)
    return out


def compare_keywords(
    client: TrendsClient,
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    kws = v.validate_keywords(keywords, minimum=2)
    data = interest_over_time(client, kws, timeframe, geo, category, gprop)
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
    client: TrendsClient,
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
    res = client.related_queries(kw, tf, g, cat, gp)
    top = fmt.related_list(res.get("top"), lim, label="query")
    rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), lim, label="query"))
    return {
        "query": _query_meta([kw], tf, g, cat, gp),
        "top": top,
        "rising": rising,
        "available": bool(top or rising),
        "notes": {
            "top": "value = relative popularity 0-100 among related searches",
            "rising": "value = % growth vs. previous period; is_breakout = >5000% (new/exploding)",
        },
    }


def related_topics(
    client: TrendsClient,
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
    res = client.related_topics(kw, tf, g, cat, gp)
    top = fmt.related_list(res.get("top"), lim, label="topic_title")
    rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), lim, label="topic_title"))
    out: dict[str, Any] = {
        "query": _query_meta([kw], tf, g, cat, gp),
        "top": top,
        "rising": rising,
        "available": bool(top or rising),
    }
    if not out["available"]:
        out["reason"] = (
            "Google returned no related topics for this query. This endpoint is "
            "unreliable; use related_queries instead."
        )
    return out


def interest_by_region(
    client: TrendsClient,
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
    res = v.validate_resolution(resolution)
    lim = v.validate_limit(limit, default=20, maximum=100)
    df = client.interest_by_region(kws, tf, g, cat, gp, res)
    return {
        "query": {**_query_meta(kws, tf, g, cat, gp), "resolution": res},
        "regions": fmt.region_list(df, kws, lim),
        "scale": "0-100 relative to the region with the highest share of searches",
    }


def suggest_keywords(client: TrendsClient, keyword: str) -> dict[str, Any]:
    kw = v.validate_keyword(keyword)
    items = client.suggestions(kw)
    return {
        "keyword": kw,
        "suggestions": [
            {"title": s.get("title"), "type": s.get("type"), "mid": s.get("mid")} for s in items
        ],
        "note": "Pass a 'mid' (e.g. '/m/05z1_') as a keyword to query the disambiguated topic.",
    }


def trending_now(client: TrendsClient, geo: str = "US", max_articles: int = 3) -> dict[str, Any]:
    g = v.validate_geo(geo) or "US"
    arts = v.validate_limit(max_articles, default=3, maximum=10) if max_articles else 0
    items = client.trending_rss(g, arts)
    trends: list[dict[str, Any]] = []
    for it in items:
        entry: dict[str, Any] = {
            "title": it.get("title"),
            "traffic": it.get("traffic"),
            "published": it.get("pub_date"),
        }
        if arts:
            entry["articles"] = [
                {"title": a.get("title"), "source": a.get("source"), "url": a.get("url")}
                for a in (it.get("articles") or [])[:arts]
            ]
        trends.append(entry)
    return {"geo": g, "count": len(trends), "trends": trends}


def list_categories(client: TrendsClient, search: str = "", limit: int = 50) -> dict[str, Any]:
    lim = v.validate_limit(limit, default=50, maximum=500)
    needle = (search or "").strip().lower()
    rows = fmt.flatten_categories(client.categories())
    if needle:
        rows = [r for r in rows if needle in r["path"].lower()]
    return {"search": search, "total_matches": len(rows), "categories": rows[:lim]}

