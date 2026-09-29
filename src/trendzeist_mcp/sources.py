"""Upstream sources composed behind one ``Hub``.

Google hosts (Trends, Autocomplete, News) share one :class:`Lane` because they
share the caller's IP budget with Google; Wikimedia gets its own lane so a Google
cool-down never blocks Wikipedia and vice versa. Everything shares the cache,
the per-call counters and the settings.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .formatters import normalise_query
from .client import (
    CallStats,
    Lane,
    Settings,
    Source,
    TrendsClient,
    _cache_key,
    _TTLCache,
    default_cache_dir,
    hl_for_geo,
)

_AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
_NEWS_URL = "https://news.google.com/rss/search"
_WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
_PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "{lang}.wikipedia/all-access/user/{title}/daily/{start}/{end}"
)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# Wikimedia's API policy asks for a descriptive UA with a contact URL.
WIKIMEDIA_UA = (
    f"trendzeist-mcp/{__version__} (https://github.com/phalkmin/trendzeist-mcp) python-requests"
)


class AutocompleteSource(Source):
    """Google Autocomplete suggestions (``suggestqueries.google.com``)."""

    name = "google_autocomplete"
    label = "Google Autocomplete"

    def suggest(self, query: str, geo: str = "") -> list[str]:
        """Suggestions for ``query`` in the market's language; stable for days -> 24 h tier."""
        hl = self.settings.hl_for(geo)
        gl = (geo or "").split("-")[0].lower()
        key = _cache_key("ac", q=query.lower(), hl=hl, gl=gl)
        return self._cached(key, self.settings.static_ttl, lambda: self._fetch(query, hl, gl))

    def _fetch(self, query: str, hl: str, gl: str) -> list[str]:
        response = self._get(
            _AUTOCOMPLETE_URL,
            params={"client": "firefox", "q": query, "hl": hl, "gl": gl or "us"},
            headers={"User-Agent": BROWSER_UA},
        )
        payload = response.json()
        # Firefox client format: ["query", ["suggestion", ...], ...]
        if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
            raise ValueError("unexpected autocomplete payload shape")
        return [str(s) for s in payload[1] if isinstance(s, str)]


def parse_news_rss(xml_text: str) -> list[dict[str, Any]]:
    """Google News search RSS -> [{title, publisher, url, published}], newest first.

    Titles arrive as "Headline - Publisher"; the suffix is stripped when it
    matches the ``<source>`` element. ``published`` is ISO-8601 UTC or ``None``.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"news feed is not valid XML: {exc}") from exc
    out: list[dict[str, Any]] = []
    for item in root.iterfind("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source = item.find("source")
        publisher = (source.text or "").strip() if source is not None else ""
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -(len(publisher) + 3)].rstrip()
        published: str | None = None
        raw = item.findtext("pubDate")
        if raw:
            try:
                published = (
                    parsedate_to_datetime(raw).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            except (TypeError, ValueError):
                published = None
        out.append(
            {
                "title": title,
                "publisher": publisher or None,
                "url": (item.findtext("link") or "").strip() or None,
                "published": published,
            }
        )
    out.sort(key=lambda a: a["published"] or "", reverse=True)
    return out


class GoogleNewsSource(Source):
    """Google News search RSS: who covers a topic and how recently (roadmap A5)."""

    name = "google_news"
    label = "Google News"

    def articles(self, query: str, geo: str) -> list[dict[str, Any]]:
        """Up to ~100 recent articles for ``query`` in the ``geo`` edition (15 min tier)."""
        gl = (geo or "US").split("-")[0].upper()
        # News ceid is an edition, not a UI preference. A pinned Trends language
        # must not produce incompatible pairs such as gl=BR, ceid=BR:en.
        hl = hl_for_geo(gl)
        ceid = f"{gl}:{hl.split('-')[0]}"
        key = _cache_key("news", q=query.lower(), hl=hl, gl=gl)
        return self._cached(key, self.settings.explore_ttl, lambda: self._fetch(query, hl, gl, ceid))

    def _fetch(self, query: str, hl: str, gl: str, ceid: str) -> list[dict[str, Any]]:
        response = self._get(
            _NEWS_URL,
            params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
            headers={"User-Agent": BROWSER_UA},
        )
        return parse_news_rss(response.text)


class WikipediaSource(Source):
    """Wikipedia search + Wikimedia pageviews (roadmap A6). Own lane, 24 h cache."""

    name = "wikipedia"
    label = "Wikipedia"

    _headers = {"User-Agent": WIKIMEDIA_UA, "Accept": "application/json"}

    def search(self, topic: str, lang: str) -> dict[str, Any]:
        """Prefer an exact title among the first five hits, otherwise the top hit."""
        # v2 avoids reusing first-hit-only results cached by earlier 0.4.0 builds.
        key = _cache_key("wiki_search_v2", q=topic.lower(), lang=lang)
        return self._cached(key, self.settings.static_ttl, lambda: self._fetch_search(topic, lang))

    def pageviews(
        self, title: str, lang: str, days: int, *, end: date | None = None
    ) -> list[dict[str, Any]]:
        """Daily user pageviews for the ``days`` days ending ``end`` (default: yesterday,
        the latest day Wikimedia has published)."""
        last = end or (date.today() - timedelta(days=1))
        first = last - timedelta(days=days - 1)
        key = _cache_key(
            "wiki_views", title=title, lang=lang, start=first.isoformat(), end=last.isoformat()
        )
        return self._cached(
            key, self.settings.static_ttl, lambda: self._fetch_pageviews(title, lang, first, last)
        )

    def _fetch_search(self, topic: str, lang: str) -> dict[str, Any]:
        response = self._get(
            _WIKI_API.format(lang=lang),
            params={
                "action": "query",
                "list": "search",
                "srsearch": topic,
                "srlimit": 5,
                "format": "json",
            },
            headers=self._headers,
        )
        query = response.json().get("query") or {}
        hits = query.get("search") or []
        if not hits:
            return {"found": False, "suggestion": (query.get("searchinfo") or {}).get("suggestion")}
        best = next(
            (hit for hit in hits if normalise_query(hit["title"]) == normalise_query(topic)),
            hits[0],
        )
        return {
            "found": True,
            "title": str(best["title"]),
            "pageid": int(best["pageid"]),
            "wordcount": int(best.get("wordcount") or 0),
        }

    def _fetch_pageviews(
        self, title: str, lang: str, first: date, last: date
    ) -> list[dict[str, Any]]:
        url = _PAGEVIEWS_URL.format(
            lang=lang,
            title=quote(title.replace(" ", "_"), safe=""),
            start=first.strftime("%Y%m%d"),
            end=last.strftime("%Y%m%d"),
        )
        response = self._get(url, headers=self._headers, allow=(404,))
        if response.status_code == 404:
            return []  # the article exists but Wikimedia has no view data for the range
        items = response.json().get("items") or []
        return [
            {
                "date": f"{it['timestamp'][:4]}-{it['timestamp'][4:6]}-{it['timestamp'][6:8]}",
                "views": int(it["views"]),
            }
            for it in items
        ]


class Hub:
    """Every upstream source behind one object; what tools receive."""

    def __init__(
        self, settings: Settings | None = None, cache_dir: Path | None | str = "auto"
    ) -> None:
        self.settings = settings or Settings.from_env()
        resolved = default_cache_dir() if cache_dir == "auto" else cache_dir
        self.cache = _TTLCache(
            Path(resolved) if resolved else None, max_entries=self.settings.max_memory_entries
        )
        self.stats = CallStats()
        self._google = Lane(self.settings.min_interval)
        self._wikimedia = Lane(self.settings.wiki_min_interval)
        self.trends = TrendsClient(self.settings, self.cache, self._google, self.stats)
        self.autocomplete = AutocompleteSource(self.settings, self.cache, self._google, self.stats)
        self.news = GoogleNewsSource(self.settings, self.cache, self._google, self.stats)
        self.wikipedia = WikipediaSource(self.settings, self.cache, self._wikimedia, self.stats)

    @property
    def sources(self) -> tuple[Source, ...]:
        return (self.trends, self.autocomplete, self.news, self.wikipedia)

    def begin_call(self) -> None:
        """Reset request/cache counters for the tool call starting on this thread."""
        self.stats.begin()

    def call_meta(self) -> dict[str, Any]:
        return self.stats.meta()

    def statuses(self) -> list[dict[str, Any]]:
        return [s.status() for s in self.sources]
