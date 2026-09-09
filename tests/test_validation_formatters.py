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


def test_validate_timeframe_bad():
    with pytest.raises(v.ValidationError):
        v.validate_timeframe("last week")


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
