"""Formatter-only correctness regressions for 0.6.2."""

import pandas as pd

from trendzeist_mcp import formatters as fmt


def _weekly(values):
    return pd.Series(
        values,
        index=pd.date_range("2025-01-05", periods=len(values), freq="W-SUN"),
    )


def test_insight_does_not_call_a_middle_peak_no_interest_for_the_whole_period():
    values = [0.0] * 10 + [40.0] * 10 + [0.0] * 10
    direction = fmt._direction(values)
    assert direction == "no_interest"  # the public direction enum is unchanged

    text = fmt._insight("middle peak", values, direction, 40, "2025-04-01")
    assert "no measurable search interest in this period" not in text
    assert "returned to zero" in text
    assert text.endswith("(no_interest).")

    zeros = fmt._insight("empty", [0.0] * 9, "no_interest", 0, "2025-01-01")
    assert "no measurable search interest in this period" in zeros


def test_opportunity_score_no_interest_has_zero_base_momentum():
    _, parts = fmt.opportunity_score(direction_now="no_interest", popularity=100)
    assert parts["momentum"] == {
        "points": 0,
        "max": 25,
        "reason": "seed direction_now no_interest",
    }

    _, parts = fmt.opportunity_score(direction_now="no_interest", has_breakout=True)
    assert parts["momentum"]["points"] == 10
    assert parts["momentum"]["reason"] == "seed direction_now no_interest + breakout"


def test_level_shifts_require_two_full_prior_windows():
    early_ramp = _weekly([float(i) for i in range(1, 25)])
    assert fmt.detect_level_shifts(early_ramp) == []

    early_step = _weekly([10.0] * 4 + [30.0] * 20)
    assert fmt.detect_level_shifts(early_step) == []

    evidenced_step = _weekly([10.0] * 12 + [30.0] * 12)
    shifts = fmt.detect_level_shifts(evidenced_step)
    assert len(shifts) == 1
    assert shifts[0]["change_pct"] == 200.0


def test_growth_affected_is_false_when_growth_3m_is_unavailable():
    idx = pd.date_range("2026-01-05", periods=24, freq="W-SUN")
    frame = pd.DataFrame({"kw": [40.0] * 12 + [20.0] * 12}, index=idx)
    summary = fmt.interest_over_time(frame, ["kw"])["summary"]["kw"]

    assert summary["growth_3m"] is None
    assert len(summary["level_shifts"]) == 1
    shift = summary["level_shifts"][0]
    assert shift["growth_affected"] is False
    assert "growth_3m spans" not in shift["warning"]


def test_channel_insight_distinguishes_missing_growth_from_low_interest():
    channels = {
        "web": {"available": True, "mean": 50.0, "growth_3m": None},
        "youtube": {"available": True, "mean": 40.0, "growth_3m": None},
    }
    text = fmt.channel_insight(channels, None)
    assert "growth_3m" in text
    assert "cannot be compared" in text
    assert "too small or too new" not in text

    low_interest = fmt.channel_insight(
        {"web": {"available": True, "mean": 2.0, "growth_3m": None}}, None
    )
    assert "too small or too new" in low_interest


def test_channel_insight_qualifies_declining_and_flat_claims_to_comparable_channels():
    channels = {
        "web": {"available": True, "mean": 50.0, "growth_3m": -10.0},
        "youtube": {"available": True, "mean": 40.0, "growth_3m": -5.0},
        "news": {"available": False, "mean": 100.0, "growth_3m": 20.0},
        "images": {"available": True, "mean": 1.0, "growth_3m": 30.0},
    }
    declining = fmt.channel_insight(channels, "youtube")
    assert declining.startswith("Every channel is declining")
    assert "among those with sufficient interest and growth data" in declining
    assert "falling least" in declining

    channels["youtube"]["growth_3m"] = 2.0
    channels["web"]["growth_3m"] = 1.0
    flat = fmt.channel_insight(channels, "youtube")
    assert flat.startswith("No channel is growing meaningfully")
    assert "among those with sufficient interest and growth data" in flat


def test_channel_insight_does_not_compare_against_an_ineligible_web_channel():
    channels = {
        "web": {"available": True, "mean": 1.0, "growth_3m": -50.0},
        "youtube": {"available": True, "mean": 40.0, "growth_3m": 20.0},
    }
    text = fmt.channel_insight(channels, "youtube")
    assert "Youtube interest grows +20%" in text
    assert "vs -50%" not in text


def test_seasonality_requires_all_calendar_months_in_each_full_year():
    full_idx = pd.date_range("2022-01-02", periods=156, freq="W-SUN")
    full = pd.Series([10.0] * len(full_idx), index=full_idx)
    # Forty-five daily points meet the old count threshold but cover only Jan/Feb.
    partial_idx = pd.date_range("2025-01-01", periods=45, freq="D")
    partial = pd.Series([100.0] * len(partial_idx), index=partial_idx)

    profile = fmt.seasonality_profile(pd.concat([full, partial]))
    assert profile["years_analysed"] == 3
    assert profile["monthly_index"]["January"] == 100.0

    assert fmt.seasonality_profile(full)["years_analysed"] == 3


def test_site_coverage_strict_subsets_are_partial_but_equivalents_are_covered():
    longer = "how to clean an espresso machine"
    assert fmt.site_coverage("clean espresso machine", [longer]) == ("partial", longer)
    assert fmt.site_coverage("claude code", ["claude code macos"]) == (
        "partial",
        "claude code macos",
    )
    assert fmt.site_coverage("wp mcp", ["wordpress mcp server"]) == (
        "partial",
        "wordpress mcp server",
    )

    assert fmt.site_coverage("Espresso, Machine!", ["espresso machine"]) == (
        "covered",
        "espresso machine",
    )
    assert fmt.site_coverage("claude code mac skill", ["claude code macos skills"]) == (
        "covered",
        "claude code macos skills",
    )
    assert fmt.site_coverage("code mac", ["mac code"]) == ("covered", "mac code")


def test_or_not_yes_no_questions_are_verdicts_but_alternatives_stay_comparisons():
    for question in (
        "is llms txt worth it or not",
        "does llms txt actually work or not?",
        "should i use llms txt or not",
    ):
        assert fmt.classify_angle(question) == "verdict"

    assert fmt.classify_angle("should i use claude or cursor") == "comparison"
    assert fmt.classify_angle("claude or cursor") == "comparison"


def test_local_question_prefixes_support_extraction_anywhere_and_local_angles_first():
    local_angles = {
        "como": "how-to",
        "como fazer": "how-to",
        "o que e": "definition",
        "vale a pena": "verdict",
    }

    assert fmt.is_question("café como fazer", local_angles)
    assert fmt.is_question("How to make coffee", local_angles)
    assert not fmt.is_question("café coado", local_angles)
    assert not fmt.is_question("café como fazer")

    assert fmt.classify_angle("o que é café", local_angles) == "definition"
    assert fmt.classify_angle("café vale a pena", local_angles) == "verdict"
    assert fmt.classify_angle("o que é café vs chá", local_angles) == "definition"
    assert fmt.classify_angle("café vs chá", local_angles) == "comparison"
    assert fmt.classify_angle("o que é café") is None

    items = [
        {"name": "café como fazer", "value": 100},
        {"name": "o que é café", "value": 90},
        {"name": "café coado", "value": 80},
    ]
    questions = fmt.extract_questions(("top", items), limit=10, local_angles=local_angles)
    assert [(q["question"], q["angle"]) for q in questions] == [
        ("café como fazer", "how-to"),
        ("o que é café", "definition"),
    ]


def test_flag_suspects_ignores_off_topic_top_queries_as_evidence():
    def rows():
        return [
            {"name": "alpha shared marker one", "value": 100},
            {"name": "beta shared marker two", "value": 100},
            {"name": "gamma shared marker three", "value": 100},
        ]

    flagged = fmt.flag_suspects(
        rows(), [{"name": "shared marker", "off_topic": True}], "unrelated seed"
    )
    assert all(item.get("suspect") for item in flagged)

    ordinary = fmt.flag_suspects(rows(), [{"name": "shared marker"}], "unrelated seed")
    assert not any(item.get("suspect") for item in ordinary)


def test_shared_early_ramps_do_not_bypass_level_shift_guard():
    values = [float(i * i) for i in range(1, 25)]
    index = pd.date_range("2025-01-05", periods=24, freq="W-SUN")
    out = fmt.interest_over_time(pd.DataFrame({"a": values, "b": values}, index=index), ["a", "b"])

    assert "level_shifts" not in out["summary"]["a"]
    assert out["warnings"] == []


def test_aggregate_level_shift_warning_does_not_assert_missing_growth():
    index = pd.date_range("2026-01-05", periods=24, freq="W-SUN")
    frame = pd.DataFrame({"kw": [40.0] * 12 + [20.0] * 12}, index=index)
    out = fmt.interest_over_time(frame, ["kw"])

    assert out["summary"]["kw"]["growth_3m"] is None
    assert out["summary"]["kw"]["level_shift"]["growth_affected"] is False
    assert "growth_3m is affected too when available" in out["warnings"][0]["message"]


def test_local_comparison_expansion_is_not_a_question():
    from trendzeist_mcp import tools

    rows = [{"name": "espresso vs pod", "value": 100}]
    out = fmt.extract_questions(
        ("top", rows), limit=10, local_angles=tools._local_angles("pt")
    )

    assert out == []


def test_local_question_deduplication_folds_accents():
    from trendzeist_mcp import tools

    rows = [{"name": "o que é café"}, {"name": "o que e cafe"}]
    out = fmt.extract_questions(
        ("top", rows), limit=10, local_angles=tools._local_angles("pt")
    )

    assert len(out) == 1


def test_local_question_matching_avoids_ambiguous_midquery_words():
    from trendzeist_mcp import tools

    angles = tools._local_angles("pt")
    rows = [
        {"name": "espresso como bebida"},
        {"name": "quais são os benefícios do espresso"},
    ]
    out = fmt.extract_questions(("top", rows), limit=10, local_angles=angles)

    assert [q["question"] for q in out] == ["quais são os benefícios do espresso"]
    assert fmt.classify_angle("espresso como bebida", angles) is None
    assert out[0]["angle"] == "definition"


def test_interest_over_time_non_finite_and_missing_values_are_json_safe():
    import json

    index = pd.date_range("2025-01-01", periods=400, freq="D")
    values = [float("nan")] * 399 + [10.0]
    values[0] = float("inf")
    frame = pd.DataFrame({"kw": values, "none": [None] * 400}, index=index)

    out = fmt.interest_over_time(frame, ["kw", "none", "all_nan"])

    assert out["summary"]["kw"]["available"] is True
    assert out["summary"]["kw"]["mean"] == 10.0
    assert out["summary"]["kw"]["growth_3m"] is None
    assert out["summary"]["none"] == {"available": False}
    json.dumps(out, allow_nan=False)


def test_interest_over_time_all_non_finite_keyword_is_unavailable():
    frame = pd.DataFrame({"kw": [float("nan"), float("inf"), None]})

    out = fmt.interest_over_time(frame, ["kw"])

    assert out["summary"]["kw"] == {"available": False}
