"""Offline tests for TrendsClient against the real pytrends-modern classes.

HTTP is mocked at the ``requests`` level so the dependency's own code paths
(cookie fetch, payload building, RSS parsing) are exercised without network.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
import requests
from mcp.server.mcpserver.exceptions import ToolError
from pytrends_modern import TrendReq
from pytrends_modern.exceptions import (
    DownloadError,
    InvalidParameterError,
    ResponseError,
    TooManyRequestsError,
)

from trendzeist_mcp import server
from trendzeist_mcp.client import CallStats, Lane, Settings, Source, TrendsClient, TrendsError, _TTLCache
from trendzeist_mcp.sources import Hub

RSS_XML = (
    '<rss><channel><item><title>espresso</title><ht:approx_traffic '
    'xmlns:ht="https://trends.google.com/trending/rss">20K+</ht:approx_traffic>'
    "</item></channel></rss>"
)


def _hub(**kw) -> Hub:
    settings = Settings(min_interval=kw.pop("min_interval", 0), **kw)
    return Hub(settings, cache_dir=None)


def _client(**kw) -> TrendsClient:
    return _hub(**kw).trends


def _cookie_response() -> Mock:
    resp = Mock(status_code=200)
    resp.cookies = requests.cookies.RequestsCookieJar()
    resp.cookies.set("NID", "abc")
    return resp


def test_cookie_network_failure_is_a_clear_retryable_error(capsys):
    with patch("requests.get", side_effect=requests.ConnectionError("offline")):
        with pytest.raises(TrendsError) as exc:
            _client().categories()
    assert exc.value.retryable is True and "session cookie" in str(exc.value)
    assert capsys.readouterr().out == ""


def test_cookie_http_error_fails_fast():
    resp = Mock(status_code=503, url="u", text="")
    resp.cookies = requests.cookies.RequestsCookieJar()
    with patch("requests.get", return_value=resp):
        with pytest.raises(TrendsError) as exc:
            _client().categories()
    assert exc.value.retryable is True and "503" in str(exc.value)


def test_cookie_success_is_kept():
    with patch("requests.get", return_value=_cookie_response()):
        assert _client()._req().cookies == {"NID": "abc"}


def test_worldwide_query_does_not_inherit_previous_geo():
    with patch("requests.get", return_value=_cookie_response()), patch.object(
        TrendReq, "_get_tokens"
    ):
        c = _client()
        c._explore(["coffee"], "today 3-m", "US", 0, "")
        req = c._explore(["coffee"], "today 3-m", "", 0, "")
    items = json.loads(req.token_payload["req"])["comparisonItem"]
    assert items[0]["geo"] == ""


def test_throttle_runs_before_every_http_request():
    calls: list[str] = []
    c = _client()
    c._throttle = lambda: calls.append("throttle")  # type: ignore[method-assign]

    def fake_get(*args, **kwargs):
        calls.append("http")
        return _cookie_response()

    def fake_session_post(self, url, **kwargs):
        calls.append("http")
        resp = Mock(status_code=200, headers={"Content-Type": "application/json"})
        resp.text = ")]}'\n" + json.dumps({"widgets": []})
        return resp

    with patch("requests.get", fake_get), patch.object(requests.Session, "post", fake_session_post):
        c._explore(["coffee"], "today 3-m", "", 0, "")
    # cookie GET + explore-token POST: each HTTP call is preceded by a throttle.
    assert calls == ["throttle", "http", "throttle", "http"]


def _token_response(widgets):
    resp = Mock(status_code=200, headers={"Content-Type": "application/json"})
    resp.text = ")]}'\n" + json.dumps({"widgets": widgets})
    return resp


def test_stale_widgets_are_cleared_between_queries():
    """A token response lacking TIMESERIES must not reuse the previous query's widget."""
    c = _client()
    ts_widget = {"id": "TIMESERIES", "token": "old", "request": {"kw": "coffee"}}
    responses = iter([[ts_widget], []])

    def fake_session_post(self, url, **kwargs):
        return _token_response(next(responses))

    with patch("requests.get", return_value=_cookie_response()), patch.object(
        requests.Session, "post", fake_session_post
    ):
        req = c._explore(["coffee"], "today 3-m", "", 0, "")
        assert req.interest_over_time_widget == ts_widget
        req = c._explore(["espresso"], "today 3-m", "", 0, "")
    assert req.interest_over_time_widget == {}
    assert req.interest_by_region_widget == {}
    # Upstream raises instead of silently serving coffee's series as espresso's.
    with pytest.raises(Exception):
        req.interest_over_time()


def test_proxy_mode_throttles_between_cookie_and_data_request():
    calls: list[str] = []
    c = _client(proxies=["http://127.0.0.1:9999", "http://127.0.0.1:9998"])
    c._throttle = lambda: calls.append("throttle")  # type: ignore[method-assign]

    def fake_get(*args, **kwargs):
        calls.append("cookie")
        return _cookie_response()

    seen_proxies: list = []

    def fake_session_post(self, url, **kwargs):
        calls.append("data")
        seen_proxies.append(kwargs.get("proxies"))
        return _token_response([])

    with patch("requests.get", fake_get), patch.object(requests.Session, "post", fake_session_post):
        c._explore(["coffee"], "today 3-m", "", 0, "")
    # Constructor cookie, then per-request cookie refresh, then the data call:
    # every HTTP request is preceded by exactly one throttle, none back-to-back.
    assert calls == ["throttle", "cookie", "throttle", "cookie", "throttle", "data"]
    assert seen_proxies == [{"https": "http://127.0.0.1:9999"}]
    assert c._req().proxy_index == 1  # rotated after success
    assert c._req().proxies == ["http://127.0.0.1:9999", "http://127.0.0.1:9998"]


def test_cookie_http_429_maps_to_rate_limit_error():
    resp = Mock(status_code=429, url="u", text="")
    resp.cookies = requests.cookies.RequestsCookieJar()
    with patch("requests.get", return_value=resp):
        with pytest.raises(TrendsError) as exc:
            _client().categories()
    assert exc.value.retryable is True and "429" in str(exc.value)


def test_explore_cache_key_includes_locale():
    a = _client(hl="en-US", tz=360)
    b = _client(hl="pt-BR", tz=180)
    ka = a._explore_key("iot", kw=["x"])
    kb = b._explore_key("iot", kw=["x"])
    assert ka != kb and "en-US" in ka and "pt-BR" in kb


def test_concurrent_identical_misses_fetch_once():
    c = _client()
    fetches: list[int] = []

    def slow_fetch():
        fetches.append(1)
        time.sleep(0.2)
        return {"v": 1}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: c._cached("k", 60, slow_fetch), range(4)))
    assert fetches == [1]
    assert all(r == {"v": 1} for r in results)


def test_rss_uses_configured_proxy_and_throttle():
    resp = Mock(text=RSS_XML)
    resp.raise_for_status = Mock()
    c = _client(proxies=["http://127.0.0.1:9999"])
    throttled = []
    c._throttle = lambda: throttled.append(1)  # type: ignore[method-assign]
    with patch("requests.get", return_value=resp) as get:
        out = c.trending_rss("US", 0)
    assert get.call_args.kwargs["proxies"] == {
        "https": "http://127.0.0.1:9999",
        "http": "http://127.0.0.1:9999",
    }
    assert throttled == [1]
    assert out[0]["title"] == "espresso"


def test_rss_network_error_maps_to_retryable_trends_error():
    with patch("requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(TrendsError) as exc:
            _client().trending_rss("US", 0)
    assert exc.value.retryable is True


def test_rss_empty_feed_returns_no_items():
    resp = Mock(text="<rss><channel></channel></rss>")
    resp.raise_for_status = Mock()
    with patch("requests.get", return_value=resp):
        assert _client().trending_rss("US", 0) == []


@pytest.mark.parametrize(
    "raised, retryable, fragment",
    [
        (InvalidParameterError("bad geo"), False, "rejected the request parameters"),
        (ResponseError("500"), True, "request failed"),
        (DownloadError("dl"), True, "request failed"),
        (requests.ConnectionError("net"), True, "request failed"),
        (KeyError("widgets"), False, "Unexpected response"),
        (ValueError("json"), False, "Unexpected response"),
    ],
)
def test_guarded_maps_every_error_branch(raised, retryable, fragment):
    c = _client()

    def boom():
        raise raised

    with pytest.raises(TrendsError) as exc:
        c._guarded(boom)
    assert exc.value.retryable is retryable
    assert fragment in str(exc.value)


def test_guarded_rate_limit_resets_session():
    c = _client()
    c._trend_req = object()  # pretend a session exists

    def boom():
        raise TooManyRequestsError("quota")

    with pytest.raises(TrendsError) as exc:
        c._guarded(boom)
    assert exc.value.retryable is True and c._trend_req is None


def test_hl_follows_geo_unless_env_pins_it(monkeypatch):
    monkeypatch.delenv("TRENDZEIST_HL", raising=False)
    auto = Settings.from_env()
    assert auto.auto_hl is True
    assert auto.hl_for("BR") == "pt-BR" and auto.hl_for("BR-SP") == "pt-BR"
    assert auto.hl_for("") == "en-US" and auto.hl_for("XX") == "en-US"

    monkeypatch.setenv("TRENDZEIST_HL", "fr-FR")
    pinned = Settings.from_env()
    assert pinned.auto_hl is False and pinned.hl_for("BR") == "fr-FR"


def test_explore_uses_geo_language_and_cache_key_reflects_it():
    c = _client()
    with patch("requests.get", return_value=_cookie_response()), patch.object(
        TrendReq, "_get_tokens"
    ):
        req = c._explore(["cafe"], "today 3-m", "BR", 0, "")
    assert req.hl == "pt-BR" and req.headers["accept-language"] == "pt-BR"
    assert json.loads(req.token_payload["req"])["comparisonItem"][0]["geo"] == "BR"
    assert req.token_payload["hl"] == "pt-BR"
    assert "pt-BR" in c._explore_key("rq", geo="BR", kw="cafe")
    assert "en-US" in c._explore_key("rq", geo="", kw="cafe")


def _autocomplete_response(suggestions):
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=["q", suggestions])
    return resp


def test_autocomplete_is_throttled_localised_and_cached():
    hub = _hub(proxies=["http://127.0.0.1:9999"])
    ac = hub.autocomplete
    throttled = []
    ac._throttle = lambda: throttled.append(1)  # type: ignore[method-assign]
    with patch("requests.get", return_value=_autocomplete_response(["how to brew cafe"])) as get:
        first = ac.suggest("how to cafe", "BR")
        second = ac.suggest("How To Cafe", "BR")  # case-insensitive cache key
    assert first == second == ["how to brew cafe"]
    assert get.call_count == 1 and throttled == [1]
    params = get.call_args.kwargs["params"]
    assert params["hl"] == "pt-BR" and params["gl"] == "br" and params["client"] == "firefox"
    assert get.call_args.kwargs["proxies"]["https"] == "http://127.0.0.1:9999"


def test_autocomplete_429_and_bad_payload_map_to_trends_error():
    resp = Mock(status_code=429, url="u", text="")
    with patch("requests.get", return_value=resp):
        with pytest.raises(TrendsError, match="Google Autocomplete rate limit") as exc:
            _hub().autocomplete.suggest("x")
    assert exc.value.retryable is True

    with patch("requests.get", return_value=_autocomplete_response("not-a-list")):
        with pytest.raises(TrendsError, match="Unexpected response from Google Autocomplete"):
            _hub().autocomplete.suggest("y")


def test_call_meta_counts_requests_and_cache_hits():
    hub = _hub()
    ac = hub.autocomplete
    hub.begin_call()
    with patch("requests.get", return_value=_autocomplete_response(["a"])):
        ac.suggest("x")
    meta = hub.call_meta()
    assert meta == {"requests_made": 1, "cache_hit": False, "cache_hits": 0, "cache_misses": 1}

    hub.begin_call()
    ac.suggest("x")
    assert hub.call_meta() == {
        "requests_made": 0,
        "cache_hit": True,
        "cache_hits": 1,
        "cache_misses": 0,
    }


def test_hub_lanes_and_source_names():
    hub = _hub()
    assert hub.autocomplete._lane is hub.trends._lane  # both Google
    assert hub.news._lane is hub.trends._lane
    assert hub.wikipedia._lane is not hub.trends._lane
    assert [s["name"] for s in hub.statuses()] == [
        "google_trends", "google_autocomplete", "google_news", "wikipedia"
    ]
    assert not hasattr(hub.trends, "autocomplete")


def test_get_hub_is_a_thread_safe_singleton(monkeypatch):
    created: list[object] = []

    def slow_construct() -> object:
        time.sleep(0.2)  # widen the race window
        created.append(object())
        return created[-1]

    monkeypatch.setattr(server, "_hub", None)
    monkeypatch.setattr(server, "Hub", slow_construct)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: server.get_hub(), range(4)))
    assert len(created) == 1
    assert all(r is results[0] for r in results)


_TUNABLE_ENV = (
    "TRENDZEIST_EXPLORE_TTL", "TRENDZEIST_RSS_TTL", "TRENDZEIST_STATIC_TTL",
    "TRENDZEIST_MAX_MEMORY_ENTRIES", "TRENDZEIST_MAX_SERIES_POINTS", "TRENDZEIST_WIKI_MIN_INTERVAL",
)


def test_settings_env_tunables(monkeypatch):
    for name in _TUNABLE_ENV:
        monkeypatch.delenv(name, raising=False)
    d = Settings.from_env()
    assert (d.explore_ttl, d.rss_ttl, d.static_ttl) == (900, 300, 86400)
    assert d.max_memory_entries == 256 and d.max_series_points == 60
    assert d.wiki_min_interval == 0.5

    monkeypatch.setenv("TRENDZEIST_EXPLORE_TTL", "60")
    monkeypatch.setenv("TRENDZEIST_RSS_TTL", "30")
    monkeypatch.setenv("TRENDZEIST_STATIC_TTL", "120")
    monkeypatch.setenv("TRENDZEIST_MAX_MEMORY_ENTRIES", "12")
    monkeypatch.setenv("TRENDZEIST_MAX_SERIES_POINTS", "24")
    monkeypatch.setenv("TRENDZEIST_WIKI_MIN_INTERVAL", "1.5")
    s = Settings.from_env()
    assert (s.explore_ttl, s.rss_ttl, s.static_ttl) == (60, 30, 120)
    assert s.max_memory_entries == 12 and s.max_series_points == 24 and s.wiki_min_interval == 1.5


@pytest.mark.parametrize("name", [
    "TRENDZEIST_EXPLORE_TTL", "TRENDZEIST_RSS_TTL", "TRENDZEIST_STATIC_TTL",
    "TRENDZEIST_MAX_MEMORY_ENTRIES", "TRENDZEIST_MAX_SERIES_POINTS",
])
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_positive_env_tunables_fail_with_actionable_error(monkeypatch, name, value):
    for setting in _TUNABLE_ENV:
        monkeypatch.delenv(setting, raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(TrendsError, match=f"{name} must be a positive integer"):
        Settings.from_env()


def test_bad_env_is_exposed_as_tool_error(monkeypatch):
    monkeypatch.setenv("TRENDZEIST_MAX_SERIES_POINTS", "0")
    monkeypatch.setattr(server, "_hub", None)
    with pytest.raises(ToolError, match="TRENDZEIST_MAX_SERIES_POINTS must be a positive integer"):
        server.trendzeist_status()


def test_lane_spaces_requests():
    lane = Lane(0.05)
    t0 = time.monotonic()
    lane.wait()
    lane.wait()
    assert time.monotonic() - t0 >= 0.05


def test_lanes_are_independent_locks():
    settings = Settings(min_interval=0)
    cache, stats = _TTLCache(None), CallStats()
    a = Source(settings, cache, Lane(0), stats)
    b = Source(settings, cache, Lane(0), stats)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(2)
        return 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(a._guarded, slow)
        assert started.wait(1)
        assert b._guarded(lambda: 2) == 2  # b's lane is free while a holds its lock
        release.set()
        assert fut.result() == 1


def test_source_health_tracks_last_outcome():
    s = Source(Settings(min_interval=0), _TTLCache(None), Lane(0), CallStats())
    s.name, s.label = "probe", "Probe"
    assert s.status()["state"] == "idle"
    assert s._guarded(lambda: 1) == 1
    assert s.status()["state"] == "live" and s.status()["last_success"].endswith("Z")

    def boom():
        raise requests.ConnectionError("down")

    with pytest.raises(TrendsError):
        s._guarded(boom)
    st = s.status()
    assert st["state"] == "error" and "Probe request failed" in st["last_error"]
    time.sleep(0.01)
    s._guarded(lambda: 1)
    assert s.status()["state"] == "live" and s.status()["last_error"] is not None


def test_hub_shares_cache_and_stats_and_uses_one_google_lane():
    hub = _hub()
    assert hub.trends._cache is hub.cache and hub.trends._stats is hub.stats
    assert hub.trends.name == "google_trends"
    assert hub.news._cache is hub.cache and hub.wikipedia._stats is hub.stats


def test_source_get_maps_429_and_http_errors():
    s = Source(Settings(min_interval=0), _TTLCache(None), Lane(0), CallStats())
    with patch("requests.get", return_value=Mock(status_code=429, url="u", text="")):
        with pytest.raises(TrendsError, match="429"):
            s._guarded(lambda: s._get("https://x"))
    not_found = Mock(status_code=404)
    not_found.raise_for_status.side_effect = requests.HTTPError("404")
    with patch("requests.get", return_value=not_found):
        with pytest.raises(TrendsError, match="request failed"):
            s._guarded(lambda: s._get("https://x"))
        assert s._guarded(lambda: s._get("https://x", allow=(404,))).status_code == 404


def test_rss_429_is_a_rate_limit_error():
    resp = Mock(status_code=429, url="u", text="")
    with patch("requests.get", return_value=resp):
        with pytest.raises(TrendsError, match="Google Trends rate limit"):
            _client().trending_rss("US", 0)
