"""Offline tests for the non-Trends sources (HTTP mocked at the requests level)."""

from datetime import date
from unittest.mock import Mock, patch

import pytest
import requests

from trendzeist_mcp.client import Settings, TrendsError
from trendzeist_mcp.sources import Hub, parse_news_rss

NEWS_XML = """<rss version="2.0"><channel><title>x</title>
<item><title>Old machine review - Tribune</title><link>https://news.google.com/a</link>
<pubDate>Wed, 17 Sep 2026 01:10:05 GMT</pubDate><source url="https://tribune.example">Tribune</source></item>
<item><title>New machine launch - The Manual</title><link>https://news.google.com/b</link>
<pubDate>Sat, 19 Sep 2026 19:00:00 GMT</pubDate><source url="https://themanual.example">The Manual</source></item>
<item><title>No publisher, no date</title></item>
<item><title></title><source>Empty</source></item>
</channel></rss>"""

SEARCH_HIT = {
    "query": {
        "searchinfo": {"totalhits": 1},
        "search": [{"title": "Espresso machine", "pageid": 321546, "wordcount": 1981}],
    }
}
SEARCH_MISS = {"query": {"searchinfo": {"totalhits": 0, "suggestion": "espresso"}, "search": []}}
VIEWS = {
    "items": [
        {"timestamp": "2026091500", "views": 100},
        {"timestamp": "2026091600", "views": 120},
        {"timestamp": "2026091700", "views": 151},
    ]
}


def _hub() -> Hub:
    return Hub(Settings(min_interval=0, wiki_min_interval=0), cache_dir=None)


def _resp(status=200, text="", json_value=None) -> Mock:
    resp = Mock(status_code=status, text=text, url="u")
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=json_value)
    return resp


def test_parse_news_rss_strips_publisher_sorts_newest_first():
    items = parse_news_rss(NEWS_XML)
    assert [i["title"] for i in items] == [
        "New machine launch", "Old machine review", "No publisher, no date",
    ]
    assert items[0] == {
        "title": "New machine launch",
        "publisher": "The Manual",
        "url": "https://news.google.com/b",
        "published": "2026-09-19T19:00:00Z",
    }
    assert items[2]["publisher"] is None and items[2]["published"] is None
    assert items[2]["url"] is None


def test_parse_news_rss_rejects_bad_xml():
    with pytest.raises(ValueError, match="not valid XML"):
        parse_news_rss("<rss><channel>")


def test_news_articles_localised_throttled_cached():
    hub = _hub()
    throttled = []
    hub.news._throttle = lambda: throttled.append(1)  # type: ignore[method-assign]
    with patch("requests.get", return_value=_resp(text=NEWS_XML)) as get:
        first = hub.news.articles("Espresso", "BR")
        second = hub.news.articles("espresso", "br-sp")  # same cache key
    assert first == second and len(first) == 3
    assert get.call_count == 1 and throttled == [1]
    params = get.call_args.kwargs["params"]
    assert params == {"q": "Espresso", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt"}
    assert "Mozilla" in get.call_args.kwargs["headers"]["User-Agent"]
    assert hub.news._lane is hub.trends._lane
    assert hub.news.status()["state"] == "live"


def test_news_edition_ignores_pinned_trends_language():
    hub = Hub(Settings(hl="en-US", auto_hl=False, min_interval=0), cache_dir=None)
    with patch("requests.get", return_value=_resp(text=NEWS_XML)) as get:
        hub.news.articles("espresso", "BR")
    assert get.call_args.kwargs["params"] == {
        "q": "espresso", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt",
    }


def test_news_errors_map_to_trends_error():
    hub = _hub()
    with patch("requests.get", return_value=_resp(status=429)):
        with pytest.raises(TrendsError, match="Google News rate limit"):
            hub.news.articles("x", "US")
    with patch("requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(TrendsError, match="Google News request failed") as exc:
            hub.news.articles("y", "US")
    assert exc.value.retryable is True and hub.news.status()["state"] == "error"


def test_wikipedia_search_and_pageviews_use_own_lane_and_ua():
    hub = _hub()
    assert hub.wikipedia._lane is not hub.trends._lane
    with patch(
        "requests.get", side_effect=[_resp(json_value=SEARCH_HIT), _resp(json_value=VIEWS)]
    ) as get:
        page = hub.wikipedia.search("espresso machine", "en")
        views = hub.wikipedia.pageviews(page["title"], "en", 3, end=date(2026, 9, 17))
    assert page == {"found": True, "title": "Espresso machine", "pageid": 321546, "wordcount": 1981}
    assert views == [
        {"date": "2026-09-15", "views": 100},
        {"date": "2026-09-16", "views": 120},
        {"date": "2026-09-17", "views": 151},
    ]
    search_call, views_call = get.call_args_list
    assert search_call.args[0] == "https://en.wikipedia.org/w/api.php"
    assert search_call.kwargs["params"]["srsearch"] == "espresso machine"
    assert views_call.args[0].endswith(
        "/en.wikipedia/all-access/user/Espresso_machine/daily/20260915/20260917"
    )
    ua = views_call.kwargs["headers"]["User-Agent"]
    assert ua.startswith("trendzeist-mcp/") and "github.com/phalkmin/trendzeist-mcp" in ua
    # both cached: no further HTTP
    hub.wikipedia.search("Espresso Machine", "en")
    hub.wikipedia.pageviews("Espresso machine", "en", 3, end=date(2026, 9, 17))
    assert get.call_count == 2


def test_wikipedia_prefers_exact_title_over_first_related_hit():
    hub = _hub()
    results = {"query": {"search": [
        {"title": "Coffee", "pageid": 1},
        {"title": "Espresso machine", "pageid": 2},
    ]}}
    with patch("requests.get", return_value=_resp(json_value=results)) as get:
        page = hub.wikipedia.search("espresso machine", "en")
    assert page["title"] == "Espresso machine" and page["pageid"] == 2
    assert get.call_args.kwargs["params"]["srlimit"] == 5


def test_wikipedia_miss_is_cached_and_404_views_are_empty():
    hub = _hub()
    with patch("requests.get", return_value=_resp(json_value=SEARCH_MISS)) as get:
        assert hub.wikipedia.search("zzqq", "en") == {"found": False, "suggestion": "espresso"}
        assert hub.wikipedia.search("zzqq", "en")["found"] is False
    assert get.call_count == 1
    not_found = _resp(status=404, json_value={"detail": "no data"})
    with patch("requests.get", return_value=not_found):
        assert hub.wikipedia.pageviews("Nothing here", "en", 5, end=date(2026, 9, 17)) == []


def test_wikipedia_title_is_url_encoded():
    hub = _hub()
    with patch("requests.get", return_value=_resp(json_value={"items": []})) as get:
        hub.wikipedia.pageviews("AC/DC (band)", "en", 2, end=date(2026, 9, 17))
    assert "/AC%2FDC_%28band%29/" in get.call_args.args[0]


def test_wikipedia_rate_limit_does_not_touch_google_lane():
    hub = _hub()
    with patch("requests.get", return_value=_resp(status=429)):
        with pytest.raises(TrendsError, match="Wikipedia rate limit"):
            hub.wikipedia.search("x", "en")
    assert hub.wikipedia.status()["state"] == "error"
    assert hub.trends.status()["state"] == "idle"
