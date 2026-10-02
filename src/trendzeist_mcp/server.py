"""MCP server entrypoint: registers Google Trends tools and prompts."""

from __future__ import annotations

import logging
import os
import threading
from functools import wraps
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, tools
from . import formatters as fmt
from .aeo import aeo_opportunities as _aeo_opportunities
from .client import TrendsError
from .discovery import discover_topics as _discover_topics
from .sources import Hub
from .validation import ValidationError

logging.basicConfig(
    level=os.environ.get("TRENDZEIST_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

server = MCPServer(
    name="trendzeist",
    version=__version__,
    instructions=(
        "Free, keyless content-ideation and answer-engine (AEO) data from Google Trends, "
        "Google Autocomplete, Google News and Wikipedia. Workflow: discover_topics for "
        "ranked ideas (with question-shaped searches and a title angle each) -> "
        "mine_questions to expand questions -> aeo_opportunities to cluster questions by "
        "seed and angle with trend, news coverage, Wikipedia presence and citability hints "
        "-> validate finalists with interest_over_time / compare_keywords (use the summary's "
        "insight and growth_3m/growth_12m) -> news_coverage for who to quote or pitch and "
        "wiki_attention for the definition anchor (or the gap to fill). gprop='news' or "
        "'youtube' shows what outlets and video audiences care about. Timeframes: "
        "'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m', 'today 5-y', 'all' or "
        "'YYYY-MM-DD YYYY-MM-DD'. Geo: '' (worldwide), ISO country ('US', 'BR') or region "
        "('US-CA'); results come back in the market's language unless TRENDZEIST_HL is set. "
        "Every result carries schema_version and _meta (requests_made, cache_hit). Google "
        "rate-limits aggressively: prefer few, well-targeted calls, slow down when "
        "_meta.requests_made is high, and check trendzeist_status (free) when a source "
        "misbehaves."
    ),
)

_hub: Hub | None = None
_hub_lock = threading.Lock()


def get_hub() -> Hub:
    """Return the process-wide hub; MCP runs sync tools on worker threads."""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = Hub()
    return _hub


def _tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Inject the shared hub and convert domain errors into tool errors.

    mcp 2.x only forwards ``ToolError`` messages to the model; any other
    exception is reported as an opaque crash, so we translate explicitly.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            hub = get_hub()
            begin = getattr(hub, "begin_call", None)
            if callable(begin):
                begin()
            return tools.finalize(hub, fn(hub, *args, **kwargs))
        except ValidationError as exc:
            raise ToolError(f"Invalid argument: {exc}") from exc
        except TrendsError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@server.tool()
def interest_over_time(
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    """Search-interest time series (0-100) for 1-5 keywords, with per-keyword
    summary (mean, latest, peak, direction over the whole window, direction_now =
    recent slope, growth_3m/growth_12m % and a plain-English insight). Use
    direction_now to decide whether to write about a topic *now*; direction can be
    'rising' or 'new' for a topic that peaked months ago. Long series are downsampled
    to ~60 points. gprop: '' (web), 'news', 'youtube' (authority channels answer
    engines cite), 'images', 'froogle'."""
    return _tool(tools.interest_over_time)(keywords, timeframe, geo, category, gprop)


@server.tool()
def compare_keywords(
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
) -> dict[str, Any]:
    """Compare 2-5 keywords head-to-head: relative share, leader and trend
    direction for each. Use to pick the strongest angle among alternatives."""
    return _tool(tools.compare_keywords)(keywords, timeframe, geo, category, gprop)


@server.tool()
def related_queries(
    keyword: str,
    timeframe: str = "today 3-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    """Top and rising search queries related to a keyword. 'rising' includes %
    growth and is_breakout (>5000%); every item carries a title `angle`, and
    `questions` lists the question-shaped ones. Rising items flagged suspect=true look
    like injected spam (one name across unrelated queries, or a domain) and are left
    out of `questions`. Best single source of fresh blog angles."""
    return _tool(tools.related_queries)(keyword, timeframe, geo, category, gprop, limit)


@server.tool()
def related_topics(
    keyword: str,
    timeframe: str = "today 3-m",
    geo: str = "",
    category: int = 0,
    gprop: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    """Top and rising Knowledge-Graph topics related to a keyword. Google often
    returns nothing here; if available=false fall back to related_queries."""
    return _tool(tools.related_topics)(keyword, timeframe, geo, category, gprop, limit)


@server.tool()
def interest_by_region(
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
    resolution: str = "COUNTRY",
    category: int = 0,
    gprop: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Where a keyword is searched most. resolution: COUNTRY (geo='' only), REGION
    (states/provinces within geo), CITY and DMA (US metro areas; geo='US' or worldwide only).
    Non-US countries support REGION only."""
    return _tool(tools.interest_by_region)(
        keywords, timeframe, geo, resolution, category, gprop, limit
    )


@server.tool()
def suggest_keywords(keyword: str) -> dict[str, Any]:
    """Google's entity suggestions for a keyword (title, type, mid). Use to
    disambiguate ambiguous terms or find the canonical topic id."""
    return _tool(tools.suggest_keywords)(keyword)


@server.tool()
def trending_now(geo: str = "US", max_articles: int = 3) -> dict[str, Any]:
    """Real-time trending searches (last ~24h) for a country or US state, with
    approximate traffic and linked news headlines. Good for newsjacking."""
    return _tool(tools.trending_now)(geo, max_articles)


@server.tool()
def list_categories(search: str = "", limit: int = 50) -> dict[str, Any]:
    """Browse/search Google Trends category ids (e.g. 'coffee' -> Food & Drink >
    Coffee & Tea). Pass the id as `category` to other tools to narrow results."""
    return _tool(tools.list_categories)(search, limit)


@server.tool()
def discover_topics(
    seed_keywords: list[str],
    geo: str = "",
    timeframe: str = "today 3-m",
    category: int = 0,
    gprop: str = "",
    max_per_seed: int = 15,
) -> dict[str, Any]:
    """One-shot topic discovery for blog ideation. For 1-5 seed keywords, pulls
    12-month trend context (direction, direction_now, growth_3m) plus rising/top
    related queries, de-duplicates and ranks candidates as breakout > rising >
    evergreen, each tagged with a title `angle`. `questions` collects question-shaped
    searches across seeds; `suspect` lists spam-like rising queries kept out of the
    ranking. Partial failures are reported per seed in `errors` instead of failing
    the call. First call of a session: start with 1-2 seeds to avoid a 429."""
    return _tool(_discover_topics)(seed_keywords, geo, timeframe, category, gprop, max_per_seed)


@server.tool()
def mine_questions(seed: str, geo: str = "", limit: int = 30) -> dict[str, Any]:
    """Long-tail questions people type about a seed, from Google Autocomplete
    ('how to', 'why', 'what is', 'vs' ... expansions), de-duplicated and tagged
    with a title angle. Feeds FAQ sections and answer-engine (AEO) content.
    Up to ~16 throttled requests per call; results cached 24 h. limit: 1-100."""
    return _tool(tools.mine_questions)(seed, geo, limit)


@server.tool()
def aeo_opportunities(
    seeds: list[str], geo: str = "", timeframe: str = "today 3-m", limit: int = 10
) -> dict[str, Any]:
    """AEO opportunity finder. For 1-5 seeds: question-shaped searches (Trends + a
    capped Autocomplete pass) clustered by seed and title angle, each scored with
    the seed's recent trend (direction_now over 12 months, growth_3m), Google News
    coverage (who to quote / pitch), Wikipedia presence and the evidence type that
    makes the answer citable. Spam-like rising queries are ignored. Up to ~1+10
    requests per seed; partial failures per source in `errors`. limit: clusters (1-25)."""
    return _tool(_aeo_opportunities)(seeds, geo, timeframe, limit)


@server.tool()
def news_coverage(topic: str, geo: str = "US", limit: int = 10) -> dict[str, Any]:
    """Who is covering a topic in Google News: recent headlines, a publisher
    frequency table (mention / pitch targets) and a recency histogram with a
    coverage label. One request, cached 15 min. limit = headlines returned (1-50)."""
    return _tool(tools.news_coverage)(topic, geo, limit)


@server.tool()
def wiki_attention(topic: str, lang: str = "en", days: int = 30) -> dict[str, Any]:
    """Does Wikipedia have an exact-title article for a topic, and is attention growing?
    Exact article (title, encoded URL, wordcount), daily pageviews for `days` (7-90)
    with direction / growth / insight, or has_article=false with a related_article
    suggestion (when search finds a different title); verify gaps before citing.
    lang = Wikipedia edition ('en', 'pt', 'de'). Own rate lane; cached 24 h."""
    return _tool(tools.wiki_attention)(topic, lang, days)


@server.tool()
def trendzeist_status() -> dict[str, Any]:
    """Health of every data source (google_trends, google_autocomplete, google_news,
    wikipedia: live / error / idle with the last error), cache usage and effective
    settings. Free: makes no network requests."""
    return _tool(tools.trendzeist_status)()


@server.prompt()
def blog_ideas_from_trends(topic: str, audience: str = "general readers", geo: str = "") -> str:
    """Guided workflow: turn Google Trends data into a prioritised list of blog post ideas."""
    where = geo or "worldwide"
    return (
        f"You are a content strategist. Goal: propose 8-12 blog post ideas about "
        f"'{topic}' for {audience} (market: {where}).\n\n"
        "Steps:\n"
        f"1. Call discover_topics with seed_keywords derived from '{topic}' (1-5 seeds, "
        f"geo='{geo}', timeframe='today 3-m'). Note each topic's angle and the "
        "`questions` list.\n"
        "2. Pick the most promising breakout/rising topics plus 2-3 evergreen ones; "
        "aim for a mix of angles (how-to, comparison, listicle, definition, news).\n"
        f"3. Call mine_questions on the 1-2 strongest seeds (geo='{geo}') to collect "
        "FAQ / answer-engine questions for each idea.\n"
        "4. Validate 3-5 finalists with compare_keywords or interest_over_time "
        "(timeframe 'today 12-m'); use the summary's insight and growth_3m/growth_12m "
        "rather than computing numbers yourself.\n"
        "5. Optionally call trending_now for newsjacking, or rerun related_queries "
        "with gprop='news' / 'youtube' to see what outlets and video audiences cover.\n\n"
        "Output: for each idea give a working title, its angle, the search keyword it "
        "targets, 2-3 questions the post should answer, the trend signal "
        "(breakout/rising/evergreen with numbers), suggested publish timing, and a "
        "one-line rationale. Rank by opportunity. Be explicit when data was "
        "unavailable rather than guessing."
    )


@server.prompt()
def answer_brief(question: str, geo: str = "") -> str:
    """Citable-answer brief for one question: direct answer, statistic, quote, sources, FAQ, schema (GEO structure)."""
    angle = fmt.classify_angle(question) or "how-to"
    hints = fmt.citability_hints(angle)
    where = geo or "worldwide"
    news_geo = geo or "US"
    return (
        f"You are writing the answer that answer engines will cite for: '{question}' "
        f"(market: {where}; angle: {angle}).\n\n"
        "Research (few calls, reuse results):\n"
        f"1. mine_questions(seed=<the 2-3 word topic inside the question>, geo='{geo}', limit=15) "
        "to confirm the phrasing people use and collect 3 FAQ follow-ups.\n"
        f"2. news_coverage(topic=<same>, geo='{news_geo}') to take one dated headline and "
        "publisher to quote, and to see who covers the topic.\n"
        "3. wiki_attention(topic=<same>) - if has_article, cite the exact-title article "
        "as a definition anchor; otherwise verify the related hit or possible gap.\n"
        "4. Optional: interest_over_time([<topic>], 'today 12-m') for a growth statistic; use "
        "the summary's insight sentence verbatim.\n\n"
        "Write, in this order:\n"
        "- Direct answer: 40-60 words; the first sentence answers the question outright.\n"
        f"- Evidence for a {angle} answer: {', '.join(hints['evidence'])}. "
        f"Structure: {hints['structure']}.\n"
        "- One statistic with its source and date.\n"
        "- One quotation (expert, publisher or primary source) with attribution.\n"
        f"- Sources to cite (3-5): {hints['cite']}.\n"
        "- FAQ: 3 follow-up questions from mine_questions, each answered in 1-2 sentences.\n"
        "- Schema: the FAQPage (and HowTo, if there are steps) JSON-LD fields to add.\n\n"
        "Rules: never invent statistics or quotes - if research returned none, say exactly what "
        "to look up. Plain, fluent language; no keyword stuffing (it does not help answer engines)."
    )


@server.prompt()
def content_brief(topic: str, audience: str = "general readers", geo: str = "") -> str:
    """Full content brief for a topic: titles, H2 outline, FAQ, regions, evidence checklist, publishers to pitch."""
    where = geo or "worldwide"
    news_geo = geo or "US"
    resolution = "COUNTRY" if not geo else "REGION"
    return (
        f"You are a content strategist preparing a brief on '{topic}' for {audience} "
        f"(market: {where}).\n\n"
        "Research:\n"
        f"1. discover_topics(seed_keywords=[1-3 seeds from '{topic}'], geo='{geo}', "
        "timeframe='today 3-m'): the ranked topics become H2 candidates; note angle and signal.\n"
        f"2. mine_questions(seed=<strongest seed>, geo='{geo}', limit=20): FAQ candidates.\n"
        f"3. interest_by_region([<strongest seed>], geo='{geo}', resolution='{resolution}'): "
        "where demand lives.\n"
        f"4. news_coverage(topic=<strongest seed>, geo='{news_geo}'): publishers to quote or "
        "pitch, and a dated headline if the angle is news.\n"
        "5. wiki_attention(topic=<strongest seed>): cite a confirmed article, or verify "
        "a related hit or possible gap.\n"
        "6. Validate the 2-3 headline angles with compare_keywords (timeframe 'today 12-m'); "
        "quote the insight sentences rather than computing numbers.\n\n"
        "Output:\n"
        "- 3 title options, each with its angle and the keyword it targets.\n"
        "- H2 outline (6-10 sections) built from the discovered topics, ordered breakout > "
        "rising > evergreen, one line on what each section must answer.\n"
        "- FAQ block: 5-8 questions from mine_questions with one-sentence answers to draft.\n"
        "- Evidence checklist per angle (steps + numbers for how-to, table + quotes for "
        "comparison, cited definition for definition, dated publisher quotes for news).\n"
        "- Target regions and the language to write in.\n"
        "- Publishers / outlets to quote or pitch, from news_coverage.\n"
        "- Publish timing from the trend signals, and what data was unavailable."
    )


def main() -> None:
    """Run the server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

