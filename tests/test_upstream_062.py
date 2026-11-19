"""0.6.2: malformed upstream payloads become model-safe ``TrendsError``s."""

from datetime import date
from unittest.mock import Mock, patch

import pandas as pd
import pytest
from pytrends_modern.exceptions import TooManyRequestsError

from trendzeist_mcp.client import Settings, TrendsError, _cache_key
from trendzeist_mcp.sources import Hub


def _hub() -> Hub:
    return Hub(Settings(min_interval=0, wiki_min_interval=0), cache_dir=None)


def _response(payload) -> Mock:
    response = Mock(status_code=200)
    response.raise_for_status = Mock()
    response.json = Mock(return_value=payload)
    return response


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"query": []},
        {"query": {"search": {}}},
        {"query": {"search": [None]}},
        {"query": {"search": [{"title": None, "pageid": 1}]}},
        {"query": {"search": [{"title": "Coffee"}]}},
        {"query": {"search": [], "searchinfo": []}},
        {"query": {"search": [], "searchinfo": {"suggestion": 7}}},
    ],
)
def test_wikipedia_search_malformed_payload_is_a_trends_error(payload):
    with patch("requests.get", return_value=_response(payload)):
        with pytest.raises(TrendsError, match="Unexpected response from Wikipedia") as exc:
            _hub().wikipedia.search("coffee", "en")
    assert isinstance(exc.value.__cause__, (TypeError, ValueError, KeyError))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"items": {}},
        {"items": [None]},
        {"items": [{"timestamp": None, "views": 1}]},
        {"items": [{"timestamp": "20260917", "views": 1}]},
        {"items": [{"timestamp": "2026091700"}]},
    ],
)
def test_wikipedia_pageviews_malformed_payload_is_a_trends_error(payload):
    with patch("requests.get", return_value=_response(payload)):
        with pytest.raises(TrendsError, match="Unexpected response from Wikipedia") as exc:
            _hub().wikipedia.pageviews("Coffee", "en", 1, end=date(2026, 9, 17))
    assert isinstance(exc.value.__cause__, (TypeError, ValueError, KeyError))


@pytest.mark.parametrize("kind", ["top", "rising"])
@pytest.mark.parametrize("value", [None, 7, {"query": "coffee"}, ["coffee"]])
def test_related_queries_rejects_non_string_query_values(kind, value):
    client = _hub().trends
    frame = pd.DataFrame({"query": [value], "value": [100]})
    related = {"top": None, "rising": None, kind: frame}
    request = Mock()
    request.related_queries.return_value = {"coffee": related}

    with patch.object(client, "_explore", return_value=request):
        with pytest.raises(TrendsError, match="Unexpected response from Google Trends") as exc:
            client.related_queries("coffee", "today 3-m", "", 0, "")
    assert isinstance(exc.value.__cause__, TypeError)


def test_related_queries_preserves_valid_frames_and_none():
    client = _hub().trends
    top = pd.DataFrame({"query": ["coffee beans"], "value": [100]})
    rising = pd.DataFrame({"query": [], "value": []})
    request = Mock()
    request.related_queries.return_value = {"coffee": {"top": top, "rising": rising}}

    with patch.object(client, "_explore", return_value=request):
        result = client.related_queries("coffee", "today 3-m", "", 0, "")

    assert result["top"] is top
    assert result["rising"] is rising


@pytest.mark.parametrize("raw", [{}, {"coffee": {}}])
def test_related_queries_preserves_missing_result(raw):
    client = _hub().trends
    request = Mock()
    request.related_queries.return_value = raw

    with patch.object(client, "_explore", return_value=request):
        assert client.related_queries("coffee", "today 3-m", "", 0, "") == {
            "top": None,
            "rising": None,
        }


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_related_queries_validates_cached_results(stale):
    client = _hub().trends
    key = client._explore_key("rq", kw="coffee", tf="today 3-m", geo="", cat=0, gprop="")
    malformed = {
        "top": pd.DataFrame({"query": [7], "value": [100]}),
        "rising": None,
    }
    client._cache.set(key, malformed, ttl=-1 if stale else 60)
    upstream_error = TooManyRequestsError("quota") if stale else AssertionError("upstream called")

    with patch.object(client, "_explore", side_effect=upstream_error):
        with pytest.raises(TrendsError, match="Unexpected response from Google Trends") as exc:
            client.related_queries("coffee", "today 3-m", "", 0, "")
    assert isinstance(exc.value.__cause__, TypeError)


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_wikipedia_search_validates_cached_results(stale):
    source = _hub().wikipedia
    key = _cache_key("wiki_search_v2", q="coffee", lang="en")
    malformed = {"found": True, "title": None, "pageid": 1, "wordcount": 10}
    source._cache.set(key, malformed, ttl=-1 if stale else 60)
    upstream_error = TooManyRequestsError("quota") if stale else AssertionError("upstream called")

    with patch.object(source, "_fetch_search", side_effect=upstream_error):
        with pytest.raises(TrendsError, match="Unexpected response from Wikipedia") as exc:
            source.search("coffee", "en")
    assert isinstance(exc.value.__cause__, TypeError)


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_wikipedia_pageviews_validates_cached_results(stale):
    source = _hub().wikipedia
    end = date(2026, 9, 17)
    key = _cache_key(
        "wiki_views", title="Coffee", lang="en", start="2026-09-17", end="2026-09-17"
    )
    source._cache.set(key, [{"date": None, "views": 1}], ttl=-1 if stale else 60)
    upstream_error = TooManyRequestsError("quota") if stale else AssertionError("upstream called")

    with patch.object(source, "_fetch_pageviews", side_effect=upstream_error):
        with pytest.raises(TrendsError, match="Unexpected response from Wikipedia") as exc:
            source.pageviews("Coffee", "en", 1, end=end)
    assert isinstance(exc.value.__cause__, TypeError)


@pytest.mark.parametrize("timestamp", ["2026023000", "2026091724"])
def test_wikipedia_pageviews_rejects_impossible_timestamp(timestamp):
    payload = {"items": [{"timestamp": timestamp, "views": 1}]}
    with patch("requests.get", return_value=_response(payload)):
        with pytest.raises(TrendsError, match="Unexpected response from Wikipedia") as exc:
            _hub().wikipedia.pageviews("Coffee", "en", 1, end=date(2026, 9, 17))
    assert isinstance(exc.value.__cause__, ValueError)


@pytest.mark.parametrize(
    ("name", "value"),
    [("TRENDZEIST_TZ", "bad"), ("TRENDZEIST_RETRIES", "bad"), ("TRENDZEIST_RETRIES", "-1")],
)
def test_invalid_integer_environment_values_are_trends_errors(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(TrendsError, match=name):
        Settings.from_env()


@pytest.mark.parametrize(
    "name",
    [
        "TRENDZEIST_MIN_INTERVAL",
        "TRENDZEIST_BACKOFF",
        "TRENDZEIST_WIKI_MIN_INTERVAL",
        "TRENDZEIST_COOLDOWN",
        "TRENDZEIST_JITTER",
    ],
)
@pytest.mark.parametrize("value", ["bad", "-1", "nan", "inf"])
def test_invalid_numeric_environment_values_are_trends_errors(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(TrendsError, match=name):
        Settings.from_env()


def test_second_rate_limit_reports_the_preserved_cooldown():
    source = _hub().trends
    with patch("trendzeist_mcp.client.time.time", return_value=1000.0):
        first = source._rate_limited(RuntimeError(), 120)
    with patch("trendzeist_mcp.client.time.time", return_value=1010.0):
        second = source._rate_limited(RuntimeError(), 1)

    assert first.retry_after == 120
    assert second.retry_after == 110
    assert "110 s" in str(second)


def test_related_topics_malformed_payload_is_a_trends_error():
    client = _hub().trends
    request = Mock()
    request.related_topics.return_value = []

    with patch.object(client, "_explore", return_value=request):
        with pytest.raises(TrendsError, match="Unexpected response from Google Trends"):
            client.related_topics("coffee", "today 3-m", "", 0, "")


def test_interest_over_time_rejects_non_dataframe_payload():
    client = _hub().trends
    request = Mock()
    request.interest_over_time.return_value = []

    with patch.object(client, "_explore", return_value=request):
        with pytest.raises(TrendsError, match="Unexpected response from Google Trends"):
            client.interest_over_time(["coffee"], "today 12-m", "", 0, "")
