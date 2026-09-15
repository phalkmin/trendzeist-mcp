"""Tool implementations, independent of the MCP transport.

Each function validates its inputs, calls :class:`TrendsClient` and returns a
JSON-serialisable dict. ``server.py`` registers these with MCP.
"""

from __future__ import annotations

import math
from typing import Any

from . import formatters as fmt
from . import validation as v
from .client import TrendsClient, TrendsError

# Bump when a field is removed or changes meaning; adding fields is compatible.
SCHEMA_VERSION = 1

# mine_questions (roadmap A1): interrogative prefixes expanded through Google
# Autocomplete. Capped so a full run is at most len(_QUESTION_PREFIXES) + 1
# throttled requests; no a-z suffix expansion (too many requests for the gain).
_QUESTION_PREFIXES: tuple[str, ...] = (
    "how to", "how do", "how much", "how long", "why", "what is", "what are", "when",
    "where", "which", "can", "should", "is", "does", "vs",
)


def _hl_for(client: TrendsClient, geo: str) -> str:
    settings = getattr(client, "settings", None)
    hl_for = getattr(settings, "hl_for", None)
    return hl_for(geo) if callable(hl_for) else "en-US"


def _query_meta(
    client: TrendsClient, keywords: list[str], timeframe: str, geo: str, category: int, gprop: str
) -> dict:
    return {
        "keywords": keywords,
        "timeframe": timeframe,
        "geo": geo or "worldwide",
        "hl": _hl_for(client, geo),
        "category": category,
        "gprop": gprop or "web",
    }


def finalize(client: TrendsClient, out: dict[str, Any]) -> dict[str, Any]:
    """Attach ``schema_version`` and ``_meta`` (roadmap C11, N15)."""
    out["schema_version"] = SCHEMA_VERSION
    meta = getattr(client, "call_meta", None)
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
    out["query"] = _query_meta(client, kws, tf, g, cat, gp)
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
    top = fmt.tag_angles(fmt.related_list(res.get("top"), lim, label="query"))
    rising = fmt.tag_angles(
        fmt.mark_breakouts(fmt.related_list(res.get("rising"), lim, label="query"))
    )
    out: dict[str, Any] = {
        "query": _query_meta(client, [kw], tf, g, cat, gp),
        "top": top,
        "rising": rising,
        "questions": fmt.extract_questions(("rising", rising), ("top", top), limit=lim),
        "available": bool(top or rising),
        "notes": {
            "top": "value = relative popularity 0-100 among related searches",
            "rising": "value = % growth vs. previous period; is_breakout = >5000% (new/exploding)",
            "angle": "title angle: how-to | comparison | listicle | definition | news | null",
            "questions": "question-shaped related searches (rising first), deduplicated",
        },
    }
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


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
        "query": _query_meta(client, [kw], tf, g, cat, gp),
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
    res = v.validate_resolution(resolution, g)
    lim = v.validate_limit(limit, default=20, maximum=100)
    df = client.interest_by_region(kws, tf, g, cat, gp, res)
    regions = fmt.region_list(df, kws, lim)
    out: dict[str, Any] = {
        "query": {**_query_meta(client, kws, tf, g, cat, gp), "resolution": res},
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


def list_categories(client: TrendsClient, search: str = "", limit: int = 50) -> dict[str, Any]:
    lim = v.validate_limit(limit, default=50, maximum=500)
    needle = (search or "").strip().lower()
    rows = fmt.flatten_categories(client.categories())
    if needle:
        rows = [r for r in rows if needle in r["path"].lower()]
    out: dict[str, Any] = {"search": search, "total_matches": len(rows), "categories": rows[:lim]}
    note = v.limit_note(limit, lim)
    if note:
        out["note"] = note
    return out


def mine_questions(
    client: TrendsClient, seed: str, geo: str = "", limit: int = 30
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
    per_prefix = max(3, math.ceil(lim / len(_QUESTION_PREFIXES)))

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
    for prefix in ("", *_QUESTION_PREFIXES):
        if len(questions) >= lim:
            break
        query = f"{prefix} {kw}".strip() if prefix != "vs" else f"{kw} vs"
        try:
            absorb(prefix, client.autocomplete(query, g))
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
        "query": {"seed": kw, "geo": g or "worldwide", "hl": _hl_for(client, g)},
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

