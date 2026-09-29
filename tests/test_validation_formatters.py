from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import validation as v


def test_validate_keywords_dedupes_and_limits():
    assert v.validate_keywords(["  Python ", "python", "Rust"]) == ["Python", "Rust"]
    with pytest.raises(v.ValidationError):
        v.validate_keywords(["a", "b", "c", "d", "e", "f"])
    with pytest.raises(v.ValidationError):
        v.validate_keywords(["", "   "])
    with pytest.raises(v.ValidationError):
        v.validate_keywords(["only one"], minimum=2)


@pytest.mark.parametrize("tf", ["today 12-m", "now 7-d", "all", "2024-01-01 2024-06-30"])
def test_validate_timeframe_ok(tf):
    assert v.validate_timeframe(tf) == tf


@pytest.mark.parametrize(
    "tf",
    [
        "last week",
        "2026-02-31 2026-03-01",  # impossible date
        "2026-09-09 2020-01-01",  # reversed
        "2003-01-01 2003-06-30",  # before Google Trends data exists
        "2020-01-01 2999-01-01",  # future
    ],
)
def test_validate_timeframe_bad(tf):
    with pytest.raises(v.ValidationError):
        v.validate_timeframe(tf)


def test_validate_geo():
    assert v.validate_geo("") == ""
    assert v.validate_geo("us") == "US"
    assert v.validate_geo("us-ca") == "US-CA"
    with pytest.raises(v.ValidationError):
        v.validate_geo("United States")


def test_validate_gprop_and_resolution():
    assert v.validate_gprop("web") == ""
    assert v.validate_gprop("YouTube") == "youtube"
    with pytest.raises(v.ValidationError):
        v.validate_gprop("tiktok")
    assert v.validate_resolution("region") == "REGION"
    with pytest.raises(v.ValidationError):
        v.validate_resolution("planet")


def test_validate_limit_clamps():
    assert v.validate_limit(999, default=10, maximum=50) == 50
    assert v.validate_limit(None, default=10, maximum=50) == 10
    with pytest.raises(v.ValidationError):
        v.validate_limit(0, default=10, maximum=50)


def _iot_frame(n=120, rising=True):
    idx = pd.date_range("2025-01-05", periods=n, freq="W")
    vals = list(range(n)) if rising else list(range(n, 0, -1))
    return pd.DataFrame(
        {"kw": vals, "isPartial": [False] * (n - 1) + [True]}, index=idx
    ).rename_axis("date")


def test_interest_over_time_downsamples_and_summarises():
    out = fmt.interest_over_time(_iot_frame(), ["kw"])
    assert out["downsampled"] is True
    assert len(out["points"]) <= fmt.MAX_SERIES_POINTS
    assert out["original_points"] == 120
    s = out["summary"]["kw"]
    assert s["available"] and s["direction"] == "rising"
    assert s["peak"] == 118  # partial last row excluded from stats
    assert s["peak_date"] == "2027-04-11"
    assert "isPartial" not in out["points"][0]["values"]


def test_interest_over_time_falling_and_empty():
    assert fmt.interest_over_time(_iot_frame(rising=False), ["kw"])["summary"]["kw"]["direction"] == "falling"
    out = fmt.interest_over_time(pd.DataFrame(), ["kw"])
    assert out["points"] == [] and out["summary"]["kw"]["available"] is False
    # Same schema as the non-empty case so callers never KeyError.
    assert out["scale"] and out["original_points"] == 0 and out["downsampled"] is False


def test_interest_over_time_keeps_intraday_timestamps():
    idx = pd.date_range("2026-09-09 12:00", periods=4, freq="15min")
    out = fmt.interest_over_time(pd.DataFrame({"kw": [10, 20, 30, 40]}, index=idx), ["kw"])
    assert [p["date"] for p in out["points"]] == [
        "2026-09-09T12:00",
        "2026-09-09T12:15",
        "2026-09-09T12:30",
        "2026-09-09T12:45",
    ]
    assert out["summary"]["kw"]["peak_date"] == "2026-09-09T12:45"


def test_related_list_and_breakouts():
    df = pd.DataFrame({"query": ["a", "b"], "value": [9050, 120]})
    items = fmt.mark_breakouts(fmt.related_list(df, 10, label="query"))
    assert items[0] == {"name": "a", "value": 9050, "growth_pct": 9050, "is_breakout": True}
    assert items[1]["is_breakout"] is False
    assert fmt.related_list(None, 10, label="query") == []


def test_region_list_sorted_and_filtered():
    df = pd.DataFrame(
        {"kw": [10, 0, 100], "geoCode": ["US-A", "US-B", "US-C"]},
        index=pd.Index(["A", "B", "C"], name="geoName"),
    )
    out = fmt.region_list(df, ["kw"], 5)
    assert [r["region"] for r in out] == ["C", "A"]
    assert out[0]["geo_code"] == "US-C"


@pytest.mark.parametrize(
    "text, angle",
    [
        ("how to descale an espresso machine", "how-to"),
        ("why is my espresso bitter", "how-to"),
        ("can you froth oat milk", "how-to"),
        ("espresso vs drip coffee", "comparison"),
        ("breville or delonghi", "comparison"),
        ("what is a ristretto", "definition"),
        ("crema meaning", "definition"),
        ("best espresso beans", "listicle"),
        ("top 10 coffee gadgets", "listicle"),
        ("nespresso recall", "news"),
        ("new espresso machine 2026", "news"),
        ("espresso", None),
        ("", None),
        (None, None),
    ],
)
def test_classify_angle(text, angle):
    assert fmt.classify_angle(text) == angle


def test_is_question_and_normalise():
    assert fmt.is_question("How to make espresso") and fmt.is_question("  does espresso stain")
    assert not fmt.is_question("espresso how to") and not fmt.is_question("") and not fmt.is_question(None)
    assert fmt.normalise_query("  How-To  make Espresso?! ") == "how to make espresso"


def test_extract_questions_prioritises_and_dedupes():
    rising = fmt.mark_breakouts([{"name": "How to make espresso", "value": 9000}, {"name": "espresso beans", "value": 50}])
    top = [{"name": "how to make espresso?", "value": 100}, {"name": "what is crema", "value": 80}, {"name": "why espresso", "value": 10}]
    out = fmt.extract_questions(("rising", rising), ("top", top), limit=2)
    assert [q["question"] for q in out] == ["How to make espresso", "what is crema"]
    assert out[0]["source"] == "rising" and out[0]["is_breakout"] is True and out[0]["value"] == 9000
    assert out[1]["angle"] == "definition" and "is_breakout" not in out[1]


def test_tag_angles_in_place():
    items = [{"name": "best beans"}, {"name": "beans"}]
    assert fmt.tag_angles(items) is items
    assert [i["angle"] for i in items] == ["listicle", None]


def test_limit_note():
    assert v.limit_note(None, 25) is None
    assert v.limit_note(25, 25) is None
    assert v.limit_note(99, 50) == "limit 99 exceeds the maximum; clamped to 50."
    assert v.limit_note(99, 50, name="max_per_seed").startswith("max_per_seed 99")


def test_growth_windows_need_time_index_and_span():
    short = pd.Series([1.0, 2.0, 3.0, 4.0], index=pd.date_range("2026-01-01", periods=4, freq="W"))
    assert fmt._growth_windows(short) == {"growth_3m": None, "growth_12m": None}
    no_time = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert fmt._growth_windows(no_time) == {"growth_3m": None, "growth_12m": None}
    daily = pd.Series([10.0] * 100 + [20.0] * 91, index=pd.date_range("2026-01-01", periods=191, freq="D"))
    out = fmt._growth_windows(daily)
    assert out["growth_3m"] == 100.0 and out["growth_12m"] is None
    zero_base = pd.Series([0.0] * 100 + [5.0] * 91, index=pd.date_range("2026-01-01", periods=191, freq="D"))
    assert fmt._growth_windows(zero_base)["growth_3m"] is None


def test_insight_sentences():
    assert fmt._insight("k", [1.0], "insufficient_data", None, None).startswith("Too few")
    assert "no measurable" in fmt._insight("k", [0.0, 0.0, 0.0], "no_interest", 0, "2026-01-01")
    assert fmt._insight("k", [10.0, 10.0, 10.0], "stable", 10, "2026-01-01") == (
        "Interest in 'k' held flat between the first and last third of the period, "
        "peaking at 10 on 2026-01-01 (stable)."
    )
    assert "fell 50%" in fmt._insight("k", [20.0, 15.0, 10.0], "falling", 20, "d")
    assert "appeared from zero" in fmt._insight("k", [0.0, 5.0, 10.0], "rising", 10, "d")


def test_flatten_categories():
    tree = {
        "name": "All categories",
        "id": 0,
        "children": [
            {"name": "Food & Drink", "id": 71, "children": [{"name": "Coffee & Tea", "id": 916}]}
        ],
    }
    rows = fmt.flatten_categories(tree)
    assert {"id": 916, "name": "Coffee & Tea", "path": "Food & Drink > Coffee & Tea"} in rows
    assert rows[0]["id"] == 0


def test_interest_over_time_respects_max_points():
    idx = pd.date_range("2025-01-01", periods=200, freq="D")
    df = pd.DataFrame({"x": range(200)}, index=idx)
    assert len(fmt.interest_over_time(df, ["x"])["points"]) <= fmt.MAX_SERIES_POINTS
    out = fmt.interest_over_time(df, ["x"], max_points=10)
    assert len(out["points"]) == 10 and out["downsampled"] is True


def _articles(now):
    def ts(hours):
        return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return [
        {"title": "a", "publisher": "The Manual", "url": None, "published": ts(2)},
        {"title": "b", "publisher": "The Manual", "url": None, "published": ts(50)},
        {"title": "c", "publisher": "TechRadar", "url": None, "published": ts(24 * 20)},
        {"title": "d", "publisher": "Tribune", "url": None, "published": ts(24 * 45)},
        {"title": "e", "publisher": None, "url": None, "published": None},
    ]


def test_publisher_table_and_recency_and_coverage():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    arts = _articles(now)
    table = fmt.publisher_table(arts, limit=2)
    assert table == [
        {"publisher": "The Manual", "articles": 2, "share_pct": 40.0},
        {"publisher": "TechRadar", "articles": 1, "share_pct": 20.0},
    ]
    rec = fmt.recency_histogram(arts, now=now)
    assert (rec["last_24h"], rec["last_7d"], rec["last_30d"], rec["older"], rec["undated"]) == (
        1, 2, 3, 1, 1,
    )
    assert rec["newest"] == "2026-09-19T10:00:00Z" and rec["oldest"] == "2026-08-05T12:00:00Z"
    assert fmt.coverage_level(rec) == "low"
    assert fmt.coverage_level({"last_30d": 0}) == "none"
    assert fmt.coverage_level({"last_30d": 12}) == "moderate"
    assert fmt.coverage_level({"last_30d": 20}) == "high"
    assert fmt.recency_histogram([])["newest"] is None


def test_validate_lang():
    assert v.validate_lang(" EN ") == "en" and v.validate_lang("pt") == "pt"
    assert v.validate_lang("zh-yue") == "zh-yue"
    assert v.validate_lang("simple") == "simple"
    for bad in ("", "e", "en_US", "en.wikipedia.org", "x" * 20):
        with pytest.raises(v.ValidationError):
            v.validate_lang(bad)


def test_pageview_summary():
    pts = [{"date": f"2026-09-{d:02d}", "views": 100 + 10 * d} for d in range(1, 10)]
    s = fmt.pageview_summary(pts, "Espresso")
    assert s["days"] == 9 and s["total"] == 1350 and s["daily_mean"] == 150.0
    assert s["peak"] == 190 and s["peak_date"] == "2026-09-09"
    assert s["direction"] == "rising" and s["growth_pct"] == pytest.approx(50.0, abs=0.1)
    assert s["insight"].startswith("Interest in 'Espresso' rose")
    empty = fmt.pageview_summary([], "x")
    assert empty["days"] == 0 and empty["direction"] == "insufficient_data"
    assert empty["growth_pct"] is None


def test_citability_hints_cover_every_angle_and_copy():
    for angle in fmt.ANGLES:
        h = fmt.citability_hints(angle)
        assert set(h) == {"evidence", "structure", "cite"} and len(h["evidence"]) == 3
    assert fmt.citability_hints(None) == fmt.citability_hints("how-to")
    fmt.citability_hints("news")["evidence"].append("x")
    assert len(fmt.CITABILITY_HINTS["news"]["evidence"]) == 3  # returned a copy

