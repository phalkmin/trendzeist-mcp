"""0.6.1 (issue #4): cooldown after a 429, Retry-After, jittered pacing, stale fallback."""

import time
from unittest.mock import Mock, patch

import pytest
import requests
from pytrends_modern.exceptions import InvalidParameterError, TooManyRequestsError

from trendzeist_mcp import client as client_mod
from trendzeist_mcp.client import CallStats, Lane, Settings, Source, TrendsError, _TTLCache
from trendzeist_mcp.sources import Hub


def _source(**kw) -> Source:
    settings = Settings(min_interval=kw.pop("min_interval", 0), **kw)
    return Source(settings, _TTLCache(None), Lane(0), CallStats())


def _too_many(headers=None) -> TooManyRequestsError:
    return TooManyRequestsError.from_response(
        Mock(status_code=429, url="u", text="", headers=headers or {})
    )


# ------------------------------------------------------------------ pacing
def test_lane_jitter_stays_within_bounds():
    lane = Lane(0.02, jitter=0.5)
    spacings = {lane._spacing() for _ in range(50)}
    assert all(0.02 <= s <= 0.03 for s in spacings) and len(spacings) > 1
    assert Lane(0.02)._spacing() == 0.02  # default: no jitter unless asked


def test_lane_waits_at_least_min_interval_with_jitter():
    lane = Lane(0.03, jitter=1.0)
    t0 = time.perf_counter()
    lane.wait()
    lane.wait()
    assert time.perf_counter() - t0 >= 0.03


# ------------------------------------------------------------------ cooldown
def test_rate_limit_enters_cooldown_and_later_calls_fail_fast():
    s = _source(cooldown=60)

    def boom():
        raise _too_many()

    with pytest.raises(TrendsError) as exc:
        s._guarded(boom)
    err = exc.value
    assert err.retryable is True and err.retry_after == 60
    assert "60 s" in str(err) and "cached results" in str(err)

    called = []
    with pytest.raises(TrendsError) as exc2:
        s._guarded(lambda: called.append(1))
    assert called == []  # the upstream was not touched while cooling down
    assert exc2.value.retryable is True and 0 < exc2.value.retry_after <= 60
    assert "cooling down" in str(exc2.value) and "retry in" in str(exc2.value)

    st = s.status()
    assert st["cooling_down"] is True and 0 < st["retry_after_s"] <= 60
    assert st["cooldown_until"].endswith("Z")


def test_cooldown_expires_and_requests_resume():
    s = _source(cooldown=60)
    with patch.object(client_mod.time, "time", return_value=1_000_000.0):
        with pytest.raises(TrendsError):
            s._guarded(lambda: (_ for _ in ()).throw(_too_many()))
    with patch.object(client_mod.time, "time", return_value=1_000_000.0 + 61):
        assert s._guarded(lambda: "ok") == "ok"
        st = s.status()
    assert st["cooling_down"] is False and st["retry_after_s"] is None and st["cooldown_until"] is None


def test_retry_after_header_sets_the_cooldown_and_is_capped():
    s = _source(cooldown=60)
    with pytest.raises(TrendsError) as exc:
        s._guarded(lambda: (_ for _ in ()).throw(_too_many({"Retry-After": "120"})))
    assert exc.value.retry_after == 120 and "120 s" in str(exc.value)

    s2 = _source(cooldown=60)
    with pytest.raises(TrendsError) as exc:
        s2._guarded(lambda: (_ for _ in ()).throw(_too_many({"Retry-After": "99999"})))
    assert exc.value.retry_after == client_mod.COOLDOWN_MAX


def test_transport_429_after_retries_also_enters_cooldown():
    s = _source(cooldown=45)
    url = "https://trends.google.com/trends/api/explore?req=secret"
    exc_in = requests.exceptions.RetryError(
        f"HTTPSConnectionPool(host='trends.google.com', port=443): Max retries exceeded with "
        f"url: {url} (Caused by ResponseError('too many 429 error responses'))"
    )
    with pytest.raises(TrendsError) as exc:
        s._guarded(lambda: (_ for _ in ()).throw(exc_in))
    assert exc.value.retryable is True and exc.value.retry_after == 45
    assert "HTTP 429 from trends.google.com after retries" in str(exc.value)
    assert "secret" not in str(exc.value)
    assert s.status()["cooling_down"] is True


def test_non_rate_limit_errors_do_not_cool_down():
    s = _source(cooldown=60)
    with pytest.raises(TrendsError):
        s._guarded(lambda: (_ for _ in ()).throw(InvalidParameterError("bad")))
    assert s.status()["cooling_down"] is False
    assert s._guarded(lambda: "ok") == "ok"


def test_cooldown_is_per_source_not_per_lane():
    hub = Hub(Settings(min_interval=0, cooldown=60), cache_dir=None)
    with pytest.raises(TrendsError):
        hub.trends._guarded(lambda: (_ for _ in ()).throw(_too_many()))
    assert hub.autocomplete._guarded(lambda: "fine") == "fine"
    hub.begin_call()
    meta = hub.call_meta()
    assert meta["rate_limited"][0]["source"] == "google_trends"
    assert 0 < meta["rate_limited"][0]["retry_after_s"] <= 60
    assert len(meta["rate_limited"]) == 1
    hub2 = Hub(Settings(min_interval=0), cache_dir=None)
    hub2.begin_call()
    assert "rate_limited" not in hub2.call_meta()


# ------------------------------------------------------------------ stale fallback
def test_ttlcache_get_stale_returns_expired_value_with_age():
    c = _TTLCache(None)
    c.set("k", "v", ttl=0.01)
    time.sleep(0.03)
    assert c.get("k") is None
    value, age = c.get_stale("k")
    assert value == "v" and age >= 0.03
    assert c.get_stale("missing") is None


def test_ttlcache_get_stale_reads_expired_disk_entry(tmp_path):
    _TTLCache(tmp_path).set("k", {"a": 1}, ttl=0.01)
    time.sleep(0.03)
    c2 = _TTLCache(tmp_path)  # server restart
    assert c2.get("k") is None  # expired entries are still *not* fresh hits
    value, age = c2.get_stale("k")
    assert value == {"a": 1} and age >= 0.03


def test_ttlcache_drops_entries_older_than_the_stale_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "DISK_SWEEP_INTERVAL", 0)
    c = _TTLCache(tmp_path)
    c.set("recent", 1, ttl=-1)  # expired a second ago: kept for stale fallback
    c.set("ancient", 2, ttl=-(client_mod.STALE_GRACE + 10))
    c.set("fresh", 3, ttl=60)
    assert c.get_stale("recent") is not None
    assert c.get_stale("ancient") is None
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_cached_serves_stale_entry_when_rate_limited():
    stats = CallStats()
    s = Source(Settings(min_interval=0, cooldown=60), _TTLCache(None), Lane(0), stats)
    stats.begin()
    assert s._cached("k", 0.01, lambda: "v1") == "v1"
    time.sleep(0.03)
    stats.begin()
    assert s._cached("k", 0.01, lambda: (_ for _ in ()).throw(_too_many())) == "v1"
    meta = stats.meta()
    assert meta["stale_hits"] == 1 and meta["stale_age_s"] >= 0.03
    # It was a miss (cache_hit must not read "entirely from cache"), but not a fresh
    # observation: the archive keys off ``fresh``, which stays at zero.
    assert meta["cache_misses"] == 1 and meta["cache_hit"] is False and stats.fresh() == 0
    assert s.status()["state"] == "error"  # health still records the refusal

    # While cooling down the next miss is served stale too, without touching upstream.
    stats.begin()
    called = []
    assert s._cached("k", 0.01, lambda: called.append(1)) == "v1"
    assert called == [] and stats.meta()["stale_hits"] == 1


def test_cached_stale_fallback_can_be_disabled_and_ignores_other_errors():
    stats = CallStats()
    s = Source(Settings(min_interval=0, stale_fallback=False), _TTLCache(None), Lane(0), stats)
    stats.begin()
    s._cached("k", 0.01, lambda: "v1")
    time.sleep(0.03)
    with pytest.raises(TrendsError, match="429"):
        s._cached("k", 0.01, lambda: (_ for _ in ()).throw(_too_many()))

    s2 = _source()
    s2._stats.begin()
    s2._cached("k", 0.01, lambda: "v1")
    time.sleep(0.03)
    with pytest.raises(TrendsError, match="rejected"):
        s2._cached("k", 0.01, lambda: (_ for _ in ()).throw(InvalidParameterError("bad")))


def test_call_meta_omits_stale_fields_when_nothing_was_stale():
    stats = CallStats()
    stats.begin()
    assert "stale_hits" not in stats.meta()


# ------------------------------------------------------------------ settings
def test_settings_env_cooldown_jitter_and_stale_fallback(monkeypatch):
    d = Settings.from_env()
    assert d.cooldown == 60.0 and d.jitter == 0.5 and d.stale_fallback is True
    monkeypatch.setenv("TRENDZEIST_COOLDOWN", "90")
    monkeypatch.setenv("TRENDZEIST_JITTER", "0.25")
    monkeypatch.setenv("TRENDZEIST_STALE_FALLBACK", "0")
    s = Settings.from_env()
    assert s.cooldown == 90.0 and s.jitter == 0.25 and s.stale_fallback is False
    hub = Hub(s, cache_dir=None)
    assert hub._google.jitter == 0.25 and hub._wikimedia.jitter == 0.25


# ------------------------------------------------------------------ review round (0.6.1)
def test_expired_memory_entry_does_not_hide_a_fresher_disk_entry(tmp_path):
    """Two server processes share the disk cache: when ours has an expired copy in
    memory and the other process has since written a fresh one, we must read it."""
    ours = _TTLCache(tmp_path)
    ours.set("k", "old", ttl=0.01)
    time.sleep(0.03)
    _TTLCache(tmp_path).set("k", "fresh", ttl=60)  # the other process
    assert ours.get("k") == "fresh"
    assert ours.get_stale("k")[0] == "fresh"


def test_has_cached_related_queries_counts_a_stale_entry_when_fallback_is_on():
    hub = Hub(Settings(min_interval=0, cooldown=60), cache_dir=None)
    key = hub.trends._explore_key("rq", kw="x", tf="today 3-m", geo="", cat=0, gprop="")
    assert hub.trends.has_cached_related_queries("x", "today 3-m", "", 0, "") is False
    hub.cache.set(key, {"top": None, "rising": None}, ttl=0.01)
    time.sleep(0.03)
    assert hub.trends.has_cached_related_queries("x", "today 3-m", "", 0, "") is True
    hub2 = Hub(Settings(min_interval=0, stale_fallback=False), cache_dir=None)
    hub2.cache.set(key, {"top": None, "rising": None}, ttl=0.01)
    time.sleep(0.03)
    assert hub2.trends.has_cached_related_queries("x", "today 3-m", "", 0, "") is False


def test_rate_limit_without_cooldown_is_flagged_but_has_no_retry_after():
    stats = CallStats()
    s = Source(Settings(min_interval=0, cooldown=0), _TTLCache(None), Lane(0), stats)
    with pytest.raises(TrendsError) as exc:
        s._guarded(lambda: (_ for _ in ()).throw(_too_many()))
    assert exc.value.rate_limited is True and exc.value.retry_after is None
    assert "Wait a minute" in str(exc.value) and s.status()["cooling_down"] is False
    # Other retryable errors are not rate limits.
    with pytest.raises(TrendsError) as exc2:
        s._guarded(lambda: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert exc2.value.rate_limited is False
    # The stale fallback keys off the flag, not the cooldown.
    stats.begin()
    s._cached("k", 0.01, lambda: "v1")
    time.sleep(0.03)
    assert s._cached("k", 0.01, lambda: (_ for _ in ()).throw(_too_many())) == "v1"


def test_failed_fetch_still_counts_as_a_miss_and_fresh_fetches_are_tracked():
    stats = CallStats()
    s = Source(Settings(min_interval=0, cooldown=60), _TTLCache(None), Lane(0), stats)
    stats.begin()
    s._cached("a", 60, lambda: "a")
    assert stats.fresh() == 1 and stats.meta()["cache_misses"] == 1
    stats.begin()
    s._cached("a", 60, lambda: "a")  # hit
    with pytest.raises(TrendsError):
        s._cached("b", 60, lambda: (_ for _ in ()).throw(InvalidParameterError("bad")))
    meta = stats.meta()
    assert meta["cache_hit"] is False and meta["cache_hits"] == 1 and meta["cache_misses"] == 1
    assert stats.fresh() == 0


def test_fetch_recording_never_archives_a_stale_answer():
    from types import SimpleNamespace

    from trendzeist_mcp import tools

    stats = CallStats()
    s = Source(Settings(min_interval=0, cooldown=60), _TTLCache(None), Lane(0), stats)
    hub = SimpleNamespace(stats=stats, archive=object())
    recorded = []
    stats.begin()
    tools.fetch_recording(hub, lambda: s._cached("k", 0.01, lambda: "v1"), recorded.append)
    assert recorded == ["v1"]  # fresh: archived
    time.sleep(0.03)
    stats.begin()
    tools.fetch_recording(
        hub, lambda: s._cached("k", 0.01, lambda: (_ for _ in ()).throw(_too_many())), recorded.append
    )
    assert recorded == ["v1"]  # stale: not archived again


def test_get_stale_applies_the_grace_to_memory_entries_too():
    c = _TTLCache(None)
    c.set("k", "v", ttl=1)
    with patch.object(client_mod.time, "time", return_value=time.time() + client_mod.STALE_GRACE + 5):
        assert c.get_stale("k") is None
        assert "k" not in c._data
