"""0.5.0 tools against the fake hub: what_changed, trend_history, seasonality,
compare_channels, trending_now(topic_filter), site_queries, unified score."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import tools
from trendzeist_mcp.aeo import aeo_opportunities
from trendzeist_mcp.archive import Archive
from trendzeist_mcp.client import TrendsError
from trendzeist_mcp.discovery import discover_topics
from trendzeist_mcp.validation import ValidationError

from test_tools import AeoClient, FakeClient


class ArchiveClient(FakeClient):
    """FakeClient plus a real (tmp) archive; no ``stats`` -> every fetch records."""

    def __init__(self, tmp_path, **kw):
        super().__init__(**kw)
        self.archive = Archive(tmp_path / "archive.sqlite3")

    def call_meta(self):
        meta = super().call_meta()
        if self.archive.call_error():
            meta["archive_error"] = self.archive.call_error()
        return meta

def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_archive(archive, seed, geo, days_ago, top, rising):
    """Record a related snapshot ``days_ago`` old via the public API; ``rising`` = [(item, status)]."""
    ts = _ts(days_ago)
    with patch("trendzeist_mcp.archive._now", return_value=ts):
        archive.record_related(
            seed, geo, "today 3-m",
            [{"name": t, "value": 50} for t in top],
            [{"name": it, "value": 9000 if st == "breakout" else 100, "is_breakout": st == "breakout"}
             for it, st in rising],
        )
    return ts


def _trend_archive(archive, seed, geo, days_ago, direction_now, timeframe="today 12-m"):
    with patch("trendzeist_mcp.archive._now", return_value=_ts(days_ago)):
        archive.record_trend(seed, geo, timeframe, {
            "available": True, "direction": "stable", "direction_now": direction_now, "growth_3m": 1.0,
        })


# ---- what_changed ------------------------------------------------------------


def test_what_changed_compares_with_baseline(tmp_path):
    client = ArchiveClient(tmp_path)
    # Baseline 10 days ago. Fake "now": top = [espresso machine, shared topic],
    # rising = [espresso viral (breakout), shared topic (rising)].
    _seed_archive(
        client.archive, "espresso", "", 10,
        top=["gone", "shared topic", "espresso machine"],
        rising=[("old thing", "rising"), ("cooled", "rising")],
    )
    out = tools.what_changed(client, ["espresso"], since_days=7)
    assert out["available"] is True and out["query"]["since_days"] == 7
    seed = out["seeds"][0]
    assert seed["baseline_at"] and seed["current_at"] > seed["baseline_at"]
    assert seed["current_items"] == 4 and seed["baseline_items"] == 5
    assert [b["topic"] for b in seed["new_breakouts"]] == ["espresso viral"]
    assert seed["new_breakouts"][0]["action"] == "investigate now"
    assert seed["new_breakouts"][0]["first_seen"] == seed["current_at"][:10]
    assert seed["new_rising"] == [] and seed["promoted"] == []
    dropped = {d["topic"]: d for d in seed["dropped"]}
    assert set(dropped) == {"gone", "old thing", "cooled"}
    # Leaving a bounded relative list is never "retire": every drop is a review item.
    assert all(d["action"].startswith("review:") for d in dropped.values())
    assert all("interest_over_time" in d["action"] for d in dropped.values())
    assert dropped["gone"]["was"] == "top" and dropped["old thing"]["was"] == "rising"
    assert seed["direction_flip"] is None  # a single trend snapshot cannot flip
    assert seed["counts"] == {
        "new_breakouts": 1, "promoted": 0, "new_rising": 0, "cooled": 0, "dropped": 3, "rank_moves": 0,
    }
    assert "retire only if direction_now is falling" in out["guidance"] and "note" not in out


def test_what_changed_reports_promotions_to_breakout(tmp_path):
    client = ArchiveClient(tmp_path)
    # 'espresso viral' is a breakout now; in the baseline it was an ordinary top query.
    _seed_archive(client.archive, "espresso", "", 9, top=["espresso viral", "espresso machine"], rising=[])
    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]
    assert seed["new_breakouts"] == []
    assert [p["topic"] for p in seed["promoted"]] == ["espresso viral"]
    assert seed["promoted"][0]["was"] == "top" and seed["promoted"][0]["action"].startswith("investigate now")


def test_what_changed_empty_snapshot_is_not_a_mass_drop(tmp_path):
    class Empty(ArchiveClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            self.calls.append(("rq", kw))
            return {"top": None, "rising": None}

    client = Empty(tmp_path)
    _seed_archive(client.archive, "espresso", "", 9, top=["a", "b"], rising=[("c", "rising")])
    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]
    assert seed["current_items"] == 0 and seed["baseline_items"] == 3
    assert "dropped" not in seed and "Google returned nothing" in seed["reason"]


def test_what_changed_without_baseline_says_when_to_rerun(tmp_path):
    client = ArchiveClient(tmp_path)
    out = tools.what_changed(client, ["espresso"], since_days=200)  # clamped to 90
    seed = out["seeds"][0]
    assert seed["current_at"] and seed["baseline_at"] is None
    assert "baseline recorded" in seed["reason"] and "rerun after" in seed["reason"]
    assert out["available"] is False and out["note"].startswith("since_days 200")
    assert out["query"]["since_days"] == 90
    with pytest.raises(ValidationError):
        tools.what_changed(client, ["espresso"], since_days=0)


def test_what_changed_direction_flip_and_cooled(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "espresso", "", 8, top=[], rising=[("espresso machine", "rising")])
    _trend_archive(client.archive, "espresso", "", 8, "falling")
    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]
    assert seed["direction_flip"] == {"from": "falling", "to": "stable", "field": "direction_now"}
    # rising -> top only, and it was NOT a long-standing top query: review, not refresh.
    assert seed["cooled"][0]["topic"] == "espresso machine"
    assert seed["cooled"][0]["action"].startswith("review: no longer rising")
    # rising AND top in the baseline, top only now: the site likely has a page -> refresh.
    client2 = ArchiveClient(tmp_path / "b")
    _seed_archive(client2.archive, "espresso", "", 8, top=["espresso machine"], rising=[("espresso machine", "rising")])
    seed2 = tools.what_changed(client2, ["espresso"], since_days=7)["seeds"][0]
    assert seed2["cooled"][0]["action"] == "refresh existing post"


def test_what_changed_rate_limit_is_partial(tmp_path):
    client = ArchiveClient(tmp_path, fail_seed="espresso")
    out = tools.what_changed(client, ["espresso", "latte"])
    assert out["errors"][0]["step"] == "related_queries:espresso"
    assert "partial" in out and len(out["seeds"]) == 2
    assert "reason" in out["seeds"][0] and out["seeds"][1]["current_at"] is None  # latte skipped


def test_display_limit_cannot_fabricate_drops(tmp_path):
    """fetch_related archives the complete lists regardless of the caller's limit."""
    client = ArchiveClient(tmp_path)
    top, rising = tools.fetch_related(client, "espresso", "today 3-m", "", 0, "", 1)
    assert len(top) == 1 and len(rising) == 1  # display limit respected ...
    snap = client.archive.snapshots("espresso", "", "related", "today 3-m")[0]
    assert snap["item_count"] == 4  # ... but all 2 top + 2 rising were archived


# ---- trend_history -------------------------------------------------------------


def test_trend_history_empty_and_populated(tmp_path):
    client = ArchiveClient(tmp_path)
    out = tools.trend_history(client, "cold brew")
    assert out["available"] is False and "what_changed" in out["reason"]

    # Three polls of one continuous breakout = 1 episode; a gap then a new one = 2.
    for d in (20, 19, 18):
        _seed_archive(client.archive, "coffee", "", d, top=["coffee maker"], rising=[("cold brew", "breakout")])
    _seed_archive(client.archive, "coffee", "", 10, top=["coffee maker", "cold brew"], rising=[])
    _seed_archive(client.archive, "coffee", "", 1, top=["coffee maker"], rising=[("cold brew", "breakout")])
    tools.interest_over_time(client, ["cold brew"])  # fake hub: records a 12-m trend snapshot
    tools.interest_over_time(client, ["cold brew"], "today 5-y")  # and a 5-y one: separate timeline
    out = tools.trend_history(client, "Cold Brew", days=400)
    assert out["available"] and out["breakout_episodes"] == 2
    assert out["breakout_observations"] == 4 and out["rising_observations"] == 4
    assert out["seen_under_seeds"] == ["coffee"] and out["latest_rank"] == 2
    assert set(out["direction_timeline"]) == {"today 12-m", "today 5-y"}
    assert out["direction_timeline"]["today 12-m"][-1]["direction_now"] == "stable"
    assert out["note"].startswith("days 400") and out["query"]["days"] == 365
    assert client.calls.count(("rq", "coffee")) == 0  # archive only
    assert "polling frequency" in out["guidance"]

def test_trend_history_counts_episodes_on_every_timeframe(tmp_path):
    """Breakouts archived on a non-default related timeframe are still episodes,
    and a 3-m snapshot without the item does not end a 12-m run."""
    client = ArchiveClient(tmp_path)
    ts = _ts(5)
    with patch("trendzeist_mcp.archive._now", return_value=ts):
        client.archive.record_related(
            "espresso", "", "today 12-m",
            [{"name": "espresso machine", "value": 100}],
            [{"name": "espresso tonic", "value": 9000, "is_breakout": True}],
        )
    # Same seed, default 3-m timeframe, no breakout: must not break the 12-m episode.
    _seed_archive(client.archive, "espresso", "", 4, top=["espresso machine"], rising=[])
    with patch("trendzeist_mcp.archive._now", return_value=_ts(3)):
        client.archive.record_related(
            "espresso", "", "today 12-m",
            [{"name": "espresso machine", "value": 100}],
            [{"name": "espresso tonic", "value": 9000, "is_breakout": True}],
        )
    out = tools.trend_history(client, "espresso tonic")
    assert out["available"]
    assert out["breakout_observations"] == 2
    assert out["breakout_episodes"] == 1  # one continuous 12-m run
    assert out["seen_under_seeds"] == ["espresso"]




# ---- seasonality -----------------------------------------------------------------


class SeasonalClient(FakeClient):
    """Five years weekly; December and January spike every year."""

    def interest_over_time(self, keywords, tf, geo, cat, gprop):
        self.calls.append(("iot", tuple(keywords)))
        self.iot_timeframes.append(tf)
        idx = pd.date_range("2021-10-03", periods=261, freq="W")
        vals = [80 if d.month in (12, 1) else 20 for d in idx]
        return pd.DataFrame({keywords[0]: vals}, index=idx)


def test_seasonality_detects_winter_peak():
    client = SeasonalClient()
    out = tools.seasonality(client, "christmas gifts", geo="US")
    assert client.iot_timeframes == ["today 5-y"] and out["query"]["timeframe"] == "today 5-y"
    assert out["available"] and out["seasonal"] is True
    assert out["peak_months"] == ["January", "December"]
    assert out["seasonal_strength"] == pytest.approx(4.0, abs=0.1)
    assert out["monthly_index"]["December"] == 100.0 and out["monthly_index"]["June"] == 25.0
    assert out["next_peak"] and out["next_peak_in_days"] >= 0
    assert out["publish_by"] == "now" or out["publish_by"] < out["next_peak"]
    assert out["insight"].startswith("Peaks in January and December")
    assert out["years_analysed"] >= 3 and "growth_note" not in out
    assert isinstance(out["trend"]["direction_now"], str)


def test_seasonality_short_series_has_reason():
    out = tools.seasonality(FakeClient(), "espresso")  # the base fake: 12 weeks only
    assert out["available"] and out["seasonal"] is None
    assert "3" in out["reason"] and "today 5-y" in out["reason"]


def test_seasonality_profile_flat_is_not_seasonal():
    idx = pd.date_range("2021-01-03", periods=261, freq="W")
    prof = fmt.seasonality_profile(pd.Series([50.0] * 261, index=idx))
    assert prof["seasonal"] is False and prof["next_peak"] is None
    assert "spread through the year" in prof["insight"]
    empty = fmt.seasonality_profile(pd.Series([], dtype=float))
    assert empty["seasonal"] is None and "reason" in empty


def test_seasonality_publish_by_is_six_weeks_before_peak():
    from datetime import date

    idx = pd.date_range("2021-01-03", periods=261, freq="W")
    s = pd.Series([90.0 if d.month == 6 else 10.0 for d in idx], index=idx)
    prof = fmt.seasonality_profile(s, today=date(2026, 2, 1))
    assert prof["peak_months"] == ["June"] and prof["next_peak"] == "2026-06-01"
    assert prof["publish_by"] == "2026-04-20" and prof["next_peak_in_days"] == 120
    assert prof["insight"].startswith("Peaks every June")
    assert fmt.seasonality_profile(s, today=date(2026, 5, 1))["publish_by"] == "now"
    assert fmt.seasonality_profile(s, today=date(2026, 7, 1))["next_peak"] == "2027-06-01"


# ---- compare_channels ------------------------------------------------------------


class ChannelClient(FakeClient):
    def __init__(self, fail_gprop=None):
        super().__init__()
        self.fail_gprop = fail_gprop
        self.gprops: list[str] = []

    def interest_over_time(self, keywords, tf, geo, cat, gprop):
        self.gprops.append(gprop)
        if gprop == self.fail_gprop:
            raise TrendsError("HTTP 429", retryable=True)
        idx = pd.date_range("2025-01-05", periods=52, freq="W")
        slope = {"": 1.0, "youtube": 3.0, "news": 0.0, "images": 1.0, "froogle": 2.0}[gprop]
        if gprop == "images":  # tiny and noisy: below the mean >= 5 floor
            return pd.DataFrame({keywords[0]: [2.0 + (i % 2) for i in range(52)]}, index=idx)
        return pd.DataFrame({keywords[0]: [20.0 + slope * i for i in range(52)]}, index=idx)


def test_compare_channels_ranks_growth_and_hints_format():
    client = ChannelClient()
    out = tools.compare_channels(client, "espresso", geo="US")
    assert client.gprops == ["", "youtube", "news", "images", "froogle"]
    assert set(out["channels"]) == {"web", "youtube", "news", "images", "shopping"}
    assert out["fastest_channel"] == "youtube"
    assert out["format_hint"] == fmt.FORMAT_HINTS["youtube"]
    assert out["insight"].startswith("Youtube interest grows +") and "web search" in out["insight"]
    yt = out["channels"]["youtube"]
    assert set(yt) >= {"direction", "direction_now", "growth_3m", "mean", "latest", "format_hint"}
    assert out["channels"]["images"]["mean"] < 5  # excluded from fastest by the noise floor
    assert out["skipped"] == [] and out["errors"] == [] and "partial" not in out
    assert "separate Google request" in out["note"]


def test_compare_channels_429_skips_remaining_channels():
    client = ChannelClient(fail_gprop="news")
    out = tools.compare_channels(client, "espresso")
    assert client.gprops == ["", "youtube", "news"]
    assert set(out["channels"]) == {"web", "youtube"}
    assert out["skipped"] == ["news", "images", "shopping"]
    assert out["errors"] == [{"step": "interest_over_time:news", "error": "HTTP 429"}]
    assert "partial" in out and out["fastest_channel"] == "youtube"


def test_channel_insight_without_eligible_channel():
    assert "No channel" in fmt.channel_insight({}, None)
    text = fmt.channel_insight({"web": {"growth_3m": 12.0}}, "web")
    assert text.startswith("Web search grows fastest (+12% over")


# ---- trending_now(topic_filter) ------------------------------------------------


def test_trending_now_topic_filter():
    out = tools.trending_now(FakeClient(), "US", topic_filter="Espresso")
    assert out["count"] == 1 and out["topic_filter"] == "Espresso" and out["unfiltered_count"] == 1
    out = tools.trending_now(FakeClient(), "US", topic_filter="tea")
    assert out["count"] == 0 and "topic_filter 'tea'" in out["reason"]
    out = tools.trending_now(FakeClient(), "US", max_articles=1, topic_filter="A")  # headline match
    assert out["count"] == 1
    assert "topic_filter" not in tools.trending_now(FakeClient(), "US")
    with pytest.raises(ValidationError):
        tools.trending_now(FakeClient(), "US", topic_filter="x" * 101)



# ---- site_queries coverage (N3) ----------------------------------------------------


def test_site_coverage_matching():
    site = ["Espresso Machine", "how to clean an espresso machine", "latte art"]
    assert fmt.site_coverage("espresso machine", site) == ("covered", "Espresso Machine")
    assert fmt.site_coverage("clean espresso machine", site) == ("covered", "how to clean an espresso machine")
    assert fmt.site_coverage("latte art tips", site) == ("partial", "latte art")  # 2/3 tokens
    assert fmt.site_coverage("cold brew", site) == ("gap", None)
    assert fmt.site_coverage("anything", []) == ("gap", None)


def test_discover_topics_site_queries_annotates_and_excludes():
    # Fake rising: '<seed> viral' (breakout), 'shared topic'; top: '<seed> machine', 'shared topic'.
    site = ["espresso machine", "shared topic"]
    out = discover_topics(FakeClient(), ["espresso"], site_queries=site)
    by_topic = {t["topic"]: t for t in out["topics"]}
    assert by_topic["espresso machine"]["coverage"] == "covered"
    assert by_topic["espresso machine"]["matched"] == "espresso machine"
    assert by_topic["espresso viral"]["coverage"] == "gap" and "matched" not in by_topic["espresso viral"]
    assert by_topic["espresso viral"]["score_breakdown"]["gap"]["points"] == 20
    assert by_topic["espresso machine"]["score_breakdown"]["gap"]["points"] == 0
    assert out["counts"]["covered"] == 2 and out["counts"]["gap"] == 1
    assert out["query"]["site_queries_count"] == 2

    out = discover_topics(FakeClient(), ["espresso"], site_queries=site, exclude_covered=True)
    assert [t["topic"] for t in out["topics"]] == ["espresso viral"]
    assert out["counts"]["covered"] == 2 and "2 covered topic(s) removed" in out["note"]

    plain = discover_topics(FakeClient(), ["espresso"])
    assert all("coverage" not in t for t in plain["topics"])
    assert all(t["score_breakdown"]["gap"]["points"] == 10 for t in plain["topics"])
    assert plain["score_note"] == fmt.SCORE_NOTE


def test_aeo_opportunities_site_queries_coverage_and_exclusion():
    # The definition cluster holds 'what is espresso' (covered) AND 'what is espresso at home'
    # from Autocomplete (not covered): a mixed cluster must not be called covered.
    out = aeo_opportunities(AeoClient(), ["espresso"], site_queries=["what is espresso"])
    ops = {o["angle"]: o for o in out["opportunities"]}
    d = ops["definition"]
    assert d["coverage_counts"]["covered"] == 1 and d["coverage_counts"]["gap"] >= 1
    assert d["coverage"] == "partial" and d["matched"] == "what is espresso"
    assert d["score_breakdown"]["gap"]["points"] == 10
    assert ops["how-to"]["coverage"] == "gap"
    assert out["counts"] == {**out["counts"], "covered": 0, "partial": 1}

    # Nothing is fully covered, so exclude_covered removes nothing and the gaps survive.
    out = aeo_opportunities(AeoClient(), ["espresso"], site_queries=["what is espresso"], exclude_covered=True)
    assert any(o["angle"] == "definition" for o in out["opportunities"])
    assert "note" not in out

    # Cover every definition question -> the cluster is covered and can be excluded.
    full = ["what is espresso", "what is espresso at home", "what is espresso without a machine"]
    out = aeo_opportunities(AeoClient(), ["espresso"], site_queries=full)
    assert {o["angle"]: o for o in out["opportunities"]}["definition"]["coverage"] == "covered"
    out = aeo_opportunities(AeoClient(), ["espresso"], site_queries=full, exclude_covered=True)
    assert all(o["angle"] != "definition" for o in out["opportunities"])
    assert "1 covered cluster(s) removed" in out["note"]


def test_cluster_coverage_is_conservative():
    from trendzeist_mcp.aeo import _cluster_coverage

    site = ["what is espresso"]
    assert _cluster_coverage({"questions": ["what is espresso", "what does espresso taste like"]}, site) == (
        "partial", "what is espresso", {"covered": 1, "partial": 0, "gap": 1},
    )
    assert _cluster_coverage({"questions": ["what is espresso"]}, site)[0] == "covered"
    assert _cluster_coverage({"questions": ["cold brew ratio"]}, site) == ("gap", None, {"covered": 0, "partial": 0, "gap": 1})
    assert _cluster_coverage({"questions": ["x"]}, []) == (None, None, {})


def test_validate_site_queries_and_days():
    from trendzeist_mcp import validation as v

    assert v.validate_site_queries(None) == ([], 0)
    assert v.validate_site_queries([" A  b ", "a b", "", "c"]) == (["A b", "c"], 0)
    queries, dropped = v.validate_site_queries([f"q{i}" for i in range(600)])
    assert len(queries) == 500 and dropped == 100
    with pytest.raises(ValidationError, match="list of strings"):
        v.validate_site_queries("not a list")
    with pytest.raises(ValidationError, match="200"):
        v.validate_site_queries(["x" * 201])
    assert v.validate_since_days(None) == 7 and v.validate_since_days(500) == 90
    assert v.validate_days(None, default=90) == 90 and v.validate_days(1000, default=90) == 365
    with pytest.raises(ValidationError):
        v.validate_days(-1, default=90)
    assert v.validate_topic_filter(None) == "" and v.validate_topic_filter("  a   b ") == "a b"


# ---- unified score (N8) -------------------------------------------------------------


def test_opportunity_score_bounds_and_reasons():
    best, parts = fmt.opportunity_score(
        questions=10, direction_now="rising", has_breakout=True,
        news={"articles_7d": 3, "articles_30d": 12}, coverage="gap", angle="definition",
        wiki={"has_article": False}, question_shaped=True,
    )
    assert best == 100
    assert {k: p["points"] for k, p in parts.items()} == {
        "demand": 25, "momentum": 25, "freshness": 15, "gap": 20, "aeo": 15,
    }
    assert all(p["max"] == fmt.SCORE_MAX[k] for k, p in parts.items())
    assert "definition gap" in parts["aeo"]["reason"] and "breakout" in parts["momentum"]["reason"]

    worst, parts = fmt.opportunity_score(direction_now="falling", news=None, coverage="covered")
    assert worst == 5  # rising-only item with unknown popularity still gets 5 demand points
    assert parts["freshness"]["reason"] == "news not fetched" and parts["gap"]["points"] == 0

    _, parts = fmt.opportunity_score(popularity=80)
    assert parts["demand"]["points"] == 20 and parts["momentum"]["points"] == 5
    assert parts["gap"]["points"] == 10 and "neutral" in parts["gap"]["reason"]

    _, parts = fmt.opportunity_score(
        questions=1, angle="definition", wiki={"has_article": False, "related_article": "Coffee"}
    )
    assert parts["aeo"]["points"] == 5 and "verify" in parts["aeo"]["reason"]
    _, parts = fmt.opportunity_score(
        questions=1, angle="how-to", wiki={"has_article": True, "direction": "rising"}
    )
    assert parts["aeo"]["points"] == 10 and "Wikipedia attention rising" in parts["aeo"]["reason"]



# ---- adversarial regressions (review of 0.5.0) -------------------------------------


def test_seasonality_zero_off_season_is_strongly_seasonal():
    from datetime import date

    idx = pd.date_range("2021-01-03", periods=261, freq="W")
    s = pd.Series([90.0 if d.month == 6 else 0.0 for d in idx], index=idx)
    prof = fmt.seasonality_profile(s, today=date(2026, 2, 1))
    assert prof["seasonal"] is True and prof["peak_months"] == ["June"]
    assert prof["seasonal_strength"] is None and "unbounded" in prof["strength_note"]
    assert prof["insight"].startswith("Peaks every June (no off-season interest)")
    assert prof["publish_by"] == "2026-04-20"


def test_seasonality_partial_year_cannot_overturn_full_years():
    from datetime import date

    idx = pd.date_range("2022-01-02", periods=156, freq="W")  # three full years
    base = pd.Series([3.0 if d.month == 6 else 1.0 for d in idx], index=idx)
    ok = fmt.seasonality_profile(base, today=date(2025, 2, 1))
    assert ok["seasonal"] is True and ok["seasonal_strength"] == pytest.approx(3.0, abs=0.05)
    # Four January weeks of a partial fourth year at 100 must change nothing.
    extra = pd.Series([100.0] * 4, index=pd.date_range("2025-01-05", periods=4, freq="W"))
    polluted = fmt.seasonality_profile(pd.concat([base, extra]), today=date(2025, 2, 1))
    assert polluted["years_analysed"] == 3
    assert polluted["seasonal"] is True and polluted["seasonal_strength"] == ok["seasonal_strength"]
    assert polluted["monthly_index"]["January"] == ok["monthly_index"]["January"]


def test_seasonality_inside_peak_month_is_active_not_past():
    from datetime import date

    idx = pd.date_range("2021-01-03", periods=261, freq="W")
    s = pd.Series([90.0 if d.month == 6 else 10.0 for d in idx], index=idx)
    prof = fmt.seasonality_profile(s, today=date(2026, 6, 15))
    assert prof["in_peak_now"] is True and prof["peak_month_now"] == "June"
    assert prof["next_peak"] == "2027-06-01" and prof["next_peak_in_days"] > 0
    assert "happening now" in prof["insight"]
    before = fmt.seasonality_profile(s, today=date(2026, 5, 31))
    assert "in_peak_now" not in before and before["next_peak"] == "2026-06-01"


def test_compare_channels_all_declining_is_not_called_growth():
    class Declining(ChannelClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            self.gprops.append(gprop)
            idx = pd.date_range("2025-01-05", periods=52, freq="W")
            slope = {"": -1.0, "youtube": -0.5, "news": -2.0, "images": -1.5, "froogle": -1.2}[gprop]
            return pd.DataFrame({keywords[0]: [80.0 + slope * i for i in range(52)]}, index=idx)

    out = tools.compare_channels(Declining(), "fax machine")
    assert out["fastest_channel"] == "youtube" and out["momentum"] == "all_declining"
    assert out["insight"].startswith("Every channel is declining")
    assert "grows" not in out["insight"] and "falling least" in out["insight"]
    assert fmt.channel_insight({"web": {"growth_3m": 2.0}}, "web").startswith("No channel is growing")
    assert fmt.channel_insight({"web": {"growth_3m": 12.0}}, "web").startswith("Web search grows fastest")
    assert tools.compare_channels(ChannelClient(), "espresso")["momentum"] == "growing"


def test_discover_topics_score_is_independent_of_seed_order():
    class UpDown(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            self.calls.append(("iot", tuple(keywords)))
            idx = pd.date_range("2025-01-05", periods=52, freq="W")
            data = {}
            for kw in keywords:
                data[kw] = list(range(10, 62)) if kw == "up" else list(range(62, 10, -1))
            return pd.DataFrame(data, index=idx)

    def shared_score(seeds):
        out = discover_topics(UpDown(), seeds)
        topic = next(t for t in out["topics"] if t["topic"] == "shared topic")
        return topic["score"], topic["momentum_seed"], topic["score_breakdown"]["momentum"]["reason"]

    a, b = shared_score(["up", "down"]), shared_score(["down", "up"])
    assert a == b and a[1] == "up" and "rising" in a[2]

