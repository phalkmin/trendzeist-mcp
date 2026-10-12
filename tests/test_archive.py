"""Observation archive (0.5.0): recording, retention, opt-out and never-fail policy."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from trendzeist_mcp import tools
from trendzeist_mcp.archive import GROUP_RELATED, GROUP_TREND, Archive, _parse_traffic
from trendzeist_mcp.client import CallStats, Settings, TrendsError
from trendzeist_mcp.sources import Hub


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(archive: Archive) -> list[tuple]:
    conn = sqlite3.connect(str(archive.path))
    try:
        return conn.execute(
            "SELECT seed, geo, timeframe, kind, item, value, rank, status FROM observations ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def test_archive_records_related_clean_only_and_trend(tmp_path):
    a = Archive(tmp_path / "archive.sqlite3")
    assert a.status()["rows"] == 0 and not a.path.exists()  # lazy: no file yet
    when = a.record_related(
        "Espresso", "US", "today 3-m",
        top=[{"name": "espresso machine", "value": 100}, {"name": "", "value": 1}],
        rising_clean=[
            {"name": "espresso viral", "value": 9000, "is_breakout": True},
            {"name": "spam .com", "value": 5000, "is_breakout": True, "suspect": True},
            {"name": "espresso beans", "value": 150, "is_breakout": False},
        ],
    )
    assert when and a.path.exists()
    rows = _rows(a)
    assert ("espresso", "US", "today 3-m", "top_query", "espresso machine", 100.0, 1, None) in rows
    assert ("espresso", "US", "today 3-m", "rising_query", "espresso viral", 9000.0, 1, "breakout") in rows
    assert ("espresso", "US", "today 3-m", "rising_query", "espresso beans", 150.0, 3, "rising") in rows
    assert not any(r[4] == "spam .com" for r in rows)

    a.record_trend("espresso", "US", "today 12-m", {
        "available": True, "direction": "new", "direction_now": "falling", "growth_3m": -73.7,
    })
    kinds = {r[3]: r for r in _rows(a) if r[2] == "today 12-m"}
    assert kinds["direction"][7] == "new" and kinds["direction_now"][7] == "falling"
    assert kinds["growth_3m"][5] == -73.7
    assert a.record_trend("x", "", "today 12-m", {"available": False}) is None

    st = a.status()
    assert st["enabled"] and st["rows"] == 6 and st["oldest"] and st["newest"] and st["last_error"] is None


def test_archive_trending_and_questions(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    a.record_trending("US", [{"title": "Espresso", "traffic": "20K+"}, {"title": None}])
    a.record_questions("espresso", "", [{"question": "how to make espresso", "angle": "how-to"}])
    rows = _rows(a)
    assert ("__trending__", "US", "now", "trending", "espresso", 20000.0, 1, None) in rows
    assert ("espresso", "", "autocomplete", "question", "how to make espresso", None, 1, "how-to") in rows
    assert _parse_traffic("1M+") == 1_000_000 and _parse_traffic("abc") is None and _parse_traffic(5) == 5.0


def related_at(a: Archive, when: str, seed: str, geo: str, top, rising, timeframe="today 3-m"):
    """Write a related snapshot as if observed at ``when`` (ISO), via the public API."""
    with patch("trendzeist_mcp.archive._now", return_value=when):
        return a.record_related(
            seed, geo, timeframe,
            [{"name": t, "value": 50 + i} for i, t in enumerate(top)],
            [{"name": it, "value": 9000 if st == "breakout" else 150, "is_breakout": st == "breakout"}
             for it, st in rising],
        )


def test_archive_readers_history_first_seen_and_baseline(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    old, mid, new = _ts(10), _ts(5), _ts(0)
    s_old = related_at(a, old, "coffee", "", ["coffee maker"], [("cold brew", "rising")])
    s_mid = related_at(a, mid, "coffee", "", [], [("cold brew", "breakout")])
    s_new = related_at(a, new, "coffee", "", ["a", "b", "c", "coffee maker"], [("cold brew", "breakout")])
    with patch("trendzeist_mcp.archive._now", return_value=new):
        a.record_trend("cold brew", "", "today 12-m", {"available": True, "direction": "new", "direction_now": "rising", "growth_3m": 1})

    snaps = a.snapshots("coffee", "", GROUP_RELATED, "today 3-m")
    assert [s["id"] for s in snaps] == [s_new, s_mid, s_old]
    assert [s["item_count"] for s in snaps] == [5, 1, 2]
    lb = a.latest_and_baseline("coffee", "", GROUP_RELATED, "today 3-m", 7)
    assert (lb[0]["id"], lb[1]["id"]) == (s_new, s_old)
    assert a.latest_and_baseline("coffee", "", GROUP_RELATED, "today 3-m", 3)[1]["id"] == s_mid
    assert a.latest_and_baseline("coffee", "", GROUP_RELATED, "today 3-m", 30)[1] is None
    assert a.latest_and_baseline("tea", "", GROUP_RELATED, "today 3-m", 7) == (None, None)
    obs = a.observations(s_new)
    assert {(r["kind"], r["item"], r["rank"]) for r in obs} >= {
        ("rising_query", "cold brew", 1), ("top_query", "coffee maker", 4),
    }
    assert a.first_seen("Cold Brew", "") == old
    hist = a.history("cold brew", "", 90)
    assert len(hist) == 6 and hist[0]["observed_at"] == old
    assert {h["kind"] for h in hist if h["observed_at"] == new} == {"rising_query", "direction", "direction_now", "growth_3m"}
    assert len(a.history("cold brew", "", 2)) == 4
    assert len(a.snapshots("cold brew", "", GROUP_TREND, "today 12-m")) == 1


def test_archive_same_second_snapshots_do_not_merge(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    t = _ts(0)
    s1 = related_at(a, t, "coffee", "", ["first"], [])
    s2 = related_at(a, t, "coffee", "", ["second"], [])
    assert s1 != s2
    assert [r["item"] for r in a.observations(s1)] == ["first"]
    assert [r["item"] for r in a.observations(s2)] == ["second"]
    assert len(a.snapshots("coffee", "", GROUP_RELATED, "today 3-m")) == 2


def test_archive_empty_fetch_is_a_snapshot(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    related_at(a, _ts(9), "coffee", "", ["x"], [])
    sid = related_at(a, _ts(0), "coffee", "", [], [])
    assert sid is not None
    latest, baseline = a.latest_and_baseline("coffee", "", GROUP_RELATED, "today 3-m", 7)
    assert latest["id"] == sid and latest["item_count"] == 0 and baseline["item_count"] == 1
    assert a.status()["snapshots"] == 2 and a.status()["rows"] == 1


def test_archive_normalises_like_the_question_deduper(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    with patch("trendzeist_mcp.archive._now", return_value=_ts(0)):
        a.record_trend("Wi-Fi", "", "today 12-m", {"available": True, "direction": "stable", "direction_now": "rising", "growth_3m": 1})
    rows = a.history("wi fi", "", 30)
    assert rows and all(r["seed"] == "wi fi" for r in rows)
    assert a.snapshots("WI-FI", "", GROUP_TREND, "today 12-m")


def test_archive_malformed_timestamp_is_skipped_not_raised(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    related_at(a, _ts(8), "coffee", "", ["x"], [])
    good = related_at(a, _ts(0), "coffee", "", ["y"], [])
    conn = a._connect()
    conn.execute("UPDATE snapshots SET observed_at='not-a-date' WHERE snapshot_id!=?", (good,))
    conn.commit()
    latest, baseline = a.latest_and_baseline("coffee", "", GROUP_RELATED, "today 3-m", 7)
    assert latest["id"] == good and baseline is None


def test_archive_prunes_old_rows_once(tmp_path):
    a = Archive(tmp_path / "a.sqlite3", retention_days=30)
    related_at(a, _ts(60), "x", "", ["ancient"], [])
    a._pruned = False  # the write above happened before "process start" for this test
    a.record_related("x", "", "today 3-m", top=[{"name": "fresh", "value": 1}], rising_clean=[])
    assert [r[4] for r in _rows(a)] == ["fresh"]
    assert len(a.snapshots("x", "", GROUP_RELATED, "today 3-m")) == 1


def test_archive_disabled_is_a_noop(tmp_path):
    a = Archive(tmp_path / "a.sqlite3", enabled=False)
    assert a.record_related("x", "", "today 3-m", [{"name": "a", "value": 1}], []) is None
    assert a.history("x", "", 90) == [] and a.snapshots("x", "", GROUP_RELATED, "today 3-m") == []
    assert not (tmp_path / "a.sqlite3").exists()
    st = a.status()
    assert st["enabled"] is False and "disabled" in st["reason"]
    assert Archive(None).enabled is False


def test_archive_errors_never_raise(tmp_path):
    a = Archive(tmp_path / "a.sqlite3")
    a.begin_call()
    conn = a._connect()
    conn.execute("DROP TABLE observations")  # sabotage after open: writes/reads now fail
    conn.commit()
    assert a.record_related("x", "", "today 3-m", [{"name": "a", "value": 1}], []) is None
    assert a.last_error and "write" in a.last_error and a.call_error() == a.last_error
    assert a.history("x", "", 90) == []
    assert "read" in a.last_error
    a.begin_call()
    assert a.call_error() is None and a.last_error  # per-call error cleared, global kept
    assert a.status()["last_error"]


# ---- integration with the Hub / tools ---------------------------------------


class _FakeTrends:
    """Minimal source: counts cache misses on the shared CallStats like a real Source."""

    def __init__(self, stats: CallStats):
        self.stats = stats
        self.cache: dict = {}

    def _cached(self, key, fetch):
        if key in self.cache:
            self.stats.count("cache_hits")
            return self.cache[key]
        self.stats.count("cache_misses")
        self.stats.count("requests")
        self.cache[key] = fetch()
        return self.cache[key]

    def related_queries(self, kw, tf, geo, cat, gprop):
        return self._cached(("rq", kw, tf, geo), lambda: {
            "top": pd.DataFrame({"query": [f"{kw} maker"], "value": [90]}),
            "rising": pd.DataFrame({"query": [f"{kw} viral"], "value": [9000]}),
        })

    def interest_over_time(self, keywords, tf, geo, cat, gprop):
        def build():
            idx = pd.date_range("2025-01-05", periods=52, freq="W")
            return pd.DataFrame({kw: list(range(52)) for kw in keywords}, index=idx)
        return self._cached(("iot", tuple(keywords), tf, geo, gprop), build)

    def status(self):
        return {"name": "google_trends", "state": "live"}

    def trending_rss(self, geo, arts):
        return self._cached(
            ("rss", geo),
            lambda: [{"title": "espresso", "traffic": "20K+", "pub_date": "x", "articles": []}],
        )


def _hub(tmp_path, **env) -> Hub:
    settings = Settings(min_interval=0, **env)
    hub = Hub(settings=settings, cache_dir=tmp_path)
    hub.trends = _FakeTrends(hub.stats)
    return hub


def test_tools_record_only_on_cache_miss(tmp_path):
    hub = _hub(tmp_path)
    hub.begin_call()
    tools.related_queries(hub, "espresso", "today 3-m")
    tools.related_queries(hub, "espresso", "today 3-m")  # cache hit: no second snapshot
    assert len(hub.archive.snapshots("espresso", "", GROUP_RELATED, "today 3-m")) == 1
    rows_after_hit = hub.archive.status()["rows"]
    tools.interest_over_time(hub, ["espresso", "latte"], "today 12-m")
    tools.interest_over_time(hub, ["espresso"], "today 12-m", gprop="youtube")  # not archived
    assert hub.archive.snapshots("latte", "", GROUP_TREND, "today 12-m")
    assert len(hub.archive.snapshots("espresso", "", GROUP_TREND, "today 12-m")) == 1
    assert hub.archive.status()["rows"] == rows_after_hit + 6  # two trend snapshots, 3 rows each
    tools.trending_now(hub, "US")
    assert hub.archive.history("espresso", "US", 1)[0]["kind"] == "trending"
    assert hub.archive.status()["rows"] >= 8
    assert "archive_error" not in hub.call_meta()


def test_archive_env_opt_out_and_status(tmp_path, monkeypatch):
    hub = _hub(tmp_path, archive_enabled=False)
    tools.related_queries(hub, "espresso")
    assert not (tmp_path / "archive.sqlite3").exists()
    assert tools.trendzeist_status(hub)["archive"]["enabled"] is False
    out = tools.trend_history(hub, "espresso")
    assert out["available"] is False and "disabled" in out["reason"]
    out = tools.what_changed(hub, ["espresso"])
    assert out["available"] is False and "disabled" in out["reason"]

    monkeypatch.setenv("TRENDZEIST_ARCHIVE", "0")
    monkeypatch.setenv("TRENDZEIST_ARCHIVE_DAYS", "30")
    s = Settings.from_env()
    assert s.archive_enabled is False and s.archive_days == 30
    monkeypatch.setenv("TRENDZEIST_ARCHIVE_DAYS", "0")
    with pytest.raises(TrendsError, match="ARCHIVE_DAYS"):
        Settings.from_env()


def test_archive_failure_surfaces_in_meta_not_as_error(tmp_path):
    hub = _hub(tmp_path)
    hub.begin_call()
    hub.archive._connect().execute("DROP TABLE observations")
    out = tools.related_queries(hub, "espresso")
    assert out["available"] is True  # the tool still answered
    assert "write" in hub.call_meta()["archive_error"]
    assert tools.trendzeist_status(hub)["archive"]["last_error"]
