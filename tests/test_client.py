"""Offline tests for TrendsClient against the real pytrends-modern classes.

HTTP is mocked at the ``requests`` level so the dependency's own code paths
(cookie fetch, payload building, RSS parsing) are exercised without network.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
import requests
from pytrends_modern import TrendReq

from trendzeist_mcp import server
from trendzeist_mcp.client import Settings, TrendsClient, TrendsError

RSS_XML = (
    '<rss><channel><item><title>espresso</title><ht:approx_traffic '
    'xmlns:ht="https://trends.google.com/trending/rss">20K+</ht:approx_traffic>'
    "</item></channel></rss>"
)


def _client(**kw) -> TrendsClient:
    settings = Settings(min_interval=kw.pop("min_interval", 0), **kw)
    return TrendsClient(settings, cache_dir=None)


def _cookie_response() -> Mock:
    resp = Mock(status_code=200)
    resp.cookies = requests.cookies.RequestsCookieJar()
    resp.cookies.set("NID", "abc")
    return resp


def test_cookie_failure_never_prints_to_stdout(capsys):
    with patch("requests.get", side_effect=requests.ConnectionError("offline")):
        req = _client()._req()
    assert req.cookies == {}
    assert capsys.readouterr().out == ""


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


def test_get_client_is_a_thread_safe_singleton(monkeypatch):
    created: list[object] = []

    def slow_construct() -> object:
        time.sleep(0.2)  # widen the race window
        created.append(object())
        return created[-1]

    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "TrendsClient", slow_construct)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: server.get_client(), range(4)))
    assert len(created) == 1
    assert all(r is results[0] for r in results)
