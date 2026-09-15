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
from .client import TrendsClient, TrendsError
from .discovery import discover_topics as _discover_topics
from .validation import ValidationError

logging.basicConfig(
    level=os.environ.get("TRENDZEIST_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

server = MCPServer(
    name="trendzeist",
    version=__version__,
    instructions=(
        "Google Trends data for topic discovery, blog ideation and answer-engine (AEO) "
        "content. Start with discover_topics for candidate ideas (it also returns "
        "question-shaped searches and a title angle per topic), expand questions with "
        "mine_questions, then validate finalists with interest_over_time / "
        "compare_keywords (summary includes growth_3m/growth_12m and a plain-English "
        "insight). gprop='news' or 'youtube' shows what news outlets and video "
        "audiences care about - the channels answer engines cite most. Timeframes: "
        "'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m', 'today 5-y', 'all' or "
        "'YYYY-MM-DD YYYY-MM-DD'. Geo: '' (worldwide), ISO country ('US', 'BR') or "
        "region ('US-CA'); results come back in the market's language unless "
        "TRENDZEIST_HL is set. Every result carries schema_version and _meta "
        "(requests_made, cache_hit). Google rate-limits aggressively: prefer few, "
        "well-targeted calls, and slow down when _meta.requests_made is high."
    ),
)

_client: TrendsClient | None = None
_client_lock = threading.Lock()


def get_client() -> TrendsClient:
    """Return the process-wide client; MCP runs sync tools on worker threads."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TrendsClient()
    return _client


def _tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Inject the shared client and convert domain errors into tool errors.

    mcp 2.x only forwards ``ToolError`` messages to the model; any other
    exception is reported as an opaque crash, so we translate explicitly.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        client = get_client()
        begin = getattr(client, "begin_call", None)
        if callable(begin):
            begin()
        try:
            return tools.finalize(client, fn(client, *args, **kwargs))
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
    summary (mean, latest, peak, direction, growth_3m/growth_12m % and a plain-English
    insight). Long series are downsampled to ~60 points. gprop: '' (web), 'news',
    'youtube' (authority channels answer engines cite), 'images', 'froogle'."""
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
    `questions` lists the question-shaped ones. Best single source of fresh blog angles."""
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
    trend direction plus rising/top related queries, de-duplicates and ranks
    candidates as breakout > rising > evergreen, each tagged with a title `angle`.
    `questions` collects question-shaped searches across seeds. Partial failures
    are reported per seed in `errors` instead of failing the call."""
    return _tool(_discover_topics)(seed_keywords, geo, timeframe, category, gprop, max_per_seed)


@server.tool()
def mine_questions(seed: str, geo: str = "", limit: int = 30) -> dict[str, Any]:
    """Long-tail questions people type about a seed, from Google Autocomplete
    ('how to', 'why', 'what is', 'vs' ... expansions), de-duplicated and tagged
    with a title angle. Feeds FAQ sections and answer-engine (AEO) content.
    Up to ~16 throttled requests per call; results cached 24 h. limit: 1-100."""
    return _tool(tools.mine_questions)(seed, geo, limit)


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


def main() -> None:
    """Run the server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

