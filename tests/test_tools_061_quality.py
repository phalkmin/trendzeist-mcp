"""0.6.1 re-check of the 2026-11-03 tutorial issues (issues 7-11): junk arriving through
Google's top list or generic words, off-topic rows in the archive, head-term coverage,
shared level shifts, and small copy / contract mismatches."""

from unittest.mock import patch

import pandas as pd

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import tools
from trendzeist_mcp.aeo import aeo_opportunities
from trendzeist_mcp.archive import GROUP_RELATED
from trendzeist_mcp.discovery import discover_topics

from test_tools import AeoClient, FakeClient
from test_tools_050 import ArchiveClient, _seed_archive, _ts
from test_tools_060_quality import OffTopicClient


# ---- Issue 7: junk through the top list and generic words -------------------------

# related_queries("llms.txt") on 2026-11-04: Google put the same junk in the top list at
# values 0-3 while every real top query was 15 or more.
LLMS_TOP = [
    ("llms txt generator", 100), ("llms txt example", 40), ("llms txt wordpress plugin", 15),
    ("top tourist attractions in paris", 3), ("best pizza near me", 2), ("best laptop for work", 2),
    ("how to bake a cake", 1), ("how to make sushi", 0), ("best time to visit maldives", 0),
    ("cheapest flights to tokyo", 0),
]
LLMS_TOP_JUNK = [name for name, value in LLMS_TOP if value < 10]
LLMS_RISING = [
    ("how to bake a cake", 6250), ("how to make sushi", 7100), ("what is a roth ira", 5500),
    ("best time to visit maldives", 7150), ("does llms txt work", 400), ("llms txt vs robots txt", 250),
]
LLMS_RISING_JUNK = ["how to bake a cake", "how to make sushi", "what is a roth ira", "best time to visit maldives"]


def _items(pairs):
    return [{"name": n, "value": v} for n, v in pairs]


class LlmsRelatedClient(FakeClient):
    def related_queries(self, kw, tf, geo, cat, gprop):
        self.calls.append(("rq", kw))
        return {
            "top": pd.DataFrame({"query": [n for n, _ in LLMS_TOP], "value": [v for _, v in LLMS_TOP]}),
            "rising": pd.DataFrame({"query": [n for n, _ in LLMS_RISING], "value": [v for _, v in LLMS_RISING]}),
        }


def test_flag_off_topic_ignores_generic_words_and_prefix_lookalikes():
    # n8n's top list has "make" (the competitor) and "n8n workflow": neither "make" nor
    # the work/workflow prefix may carry relevance to a chatbot test prompt.
    rising = fmt.mark_breakouts(_items([
        ("how to make sushi", 7100), ("best laptop for work", 6000), ("how to bake a cake", 6250),
        ("n8n workflows", 5500),
    ]))
    top = _items([("n8n ai", 30), ("make", 25), ("n8n workflow", 20), ("docker", 15), ("n8n docker", 12)])
    fmt.flag_off_topic(rising, top, "n8n")
    assert [bool(it.get("off_topic")) for it in rising] == [True, True, True, False]
    assert not any(it.get("off_topic") for it in top)


def test_flag_off_topic_flags_low_value_top_junk_and_the_rising_items_that_echo_it():
    top, rising = _items(LLMS_TOP), fmt.mark_breakouts(_items(LLMS_RISING))
    fmt.flag_off_topic(rising, top, "llms.txt")
    assert [t["name"] for t in top if t.get("off_topic")] == LLMS_TOP_JUNK
    assert "top value" in top[3]["off_topic_reason"]
    flagged = [r["name"] for r in rising if r.get("off_topic")]
    assert flagged == LLMS_RISING_JUNK
    assert fmt.split_off_topic(top)[0] == top[:3]


def test_flag_off_topic_leaves_measured_rising_items_alone():
    # A rising item below breakout has a real base; only breakouts can be junk from nothing.
    rising = fmt.mark_breakouts(_items([("moka pot", 300), ("oat milk latte", 4500), ("how to bake a cake", 9000)]))
    fmt.flag_off_topic(rising, _items([("espresso machine", 100)]), "espresso")
    assert [bool(it.get("off_topic")) for it in rising] == [False, False, True]


def test_related_queries_flags_top_junk_and_keeps_it_out_of_questions():
    out = tools.related_queries(LlmsRelatedClient(), "llms.txt")
    assert out["off_topic_count"] == len(LLMS_TOP_JUNK) + len(LLMS_RISING_JUNK)
    assert [t["name"] for t in out["top"] if t.get("off_topic")] == LLMS_TOP_JUNK
    assert [r["name"] for r in out["rising"] if r.get("off_topic")] == LLMS_RISING_JUNK
    questions = [q["question"] for q in out["questions"]]
    assert questions == ["does llms txt work"]
    assert "top" in out["notes"]["off_topic"]


def test_discover_topics_filters_junk_arriving_through_the_top_list():
    out = discover_topics(LlmsRelatedClient(), ["llms.txt"])
    topics = [t["topic"] for t in out["topics"]]
    assert not set(topics) & set(LLMS_TOP_JUNK + LLMS_RISING_JUNK)
    assert {"llms txt generator", "does llms txt work", "llms txt vs robots txt"} <= set(topics)
    assert [q["question"] for q in out["questions"]] == ["does llms txt work"]
    by_list = {}
    for f in out["filtered_out"]:
        assert f["kind"] == "off_topic"
        by_list.setdefault(f["list"], []).append(f["topic"])
    assert by_list == {"top": LLMS_TOP_JUNK, "rising": LLMS_RISING_JUNK}
    assert out["counts"]["off_topic"] == 11 and out["counts"]["evergreen"] == 3
    assert out["seeds"][0]["top_count"] == 3 and out["seeds"][0]["rising_count"] == 2
    assert out["seeds"][0]["off_topic_count"] == 11


def test_aeo_opportunities_drops_top_junk_and_awards_it_no_breakout_points():
    class Client(LlmsRelatedClient, AeoClient):
        pass

    out = aeo_opportunities(Client(), ["llms.txt"])
    questions = [q for c in out["opportunities"] for q in c["questions"]]
    assert not set(questions) & set(LLMS_TOP_JUNK + LLMS_RISING_JUNK)
    assert "does llms txt work" in questions
    assert set(out["seeds"][0]["off_topic_dropped"]) == set(LLMS_TOP_JUNK + LLMS_RISING_JUNK)
    # The only breakouts in the lists were junk: no cluster may be paid for them.
    assert not any("breakout" in c["score_breakdown"]["momentum"]["reason"] for c in out["opportunities"])


# ---- Issue 8: off-topic items and the archive --------------------------------------


def test_off_topic_items_never_enter_the_archive(tmp_path):
    class Client(LlmsRelatedClient, ArchiveClient):
        pass

    client = Client(tmp_path)
    discover_topics(client, ["llms.txt"])
    items = {r["item"] for r in client.archive.rising_items("", 30)}
    assert items == {"does llms txt work", "llms txt vs robots txt"}
    assert client.archive.history("how to bake a cake", "", 30) == []
    snaps = client.archive.snapshots("llms.txt", "", GROUP_RELATED, "today 3-m")
    assert snaps[0]["item_count"] == 5  # 3 clean top + 2 clean rising


def test_what_changed_reapplies_the_filters_to_an_older_snapshot(tmp_path):
    client = ArchiveClient(tmp_path)
    # A 0.5-era baseline written before the spam / off-topic rules existed.
    with patch("trendzeist_mcp.archive._now", return_value=_ts(10)):
        client.archive.record_related(
            "espresso", "", "today 3-m",
            _items([("espresso machine", 100), ("how to bake a cake", 1)]),
            fmt.mark_breakouts(_items([
                ("abrahamquirosvillalba .com", 9000),
                ("apple smartring abraham quiros villalba", 9000),
                ("solar shingles vs solar panels abraham quiros villalba", 9000),
                ("best time to visit maldives", 9000),
                ("shared topic", 100),
            ])),
        )
    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]
    assert seed["dropped"] == [] and seed["baseline_filtered"] == 5
    assert [b["topic"] for b in seed["new_breakouts"]] == ["espresso viral"]
    assert seed["counts"]["dropped"] == 0


# ---- Issue 9: head terms and prefix lookalikes --------------------------------------


def test_site_coverage_head_terms_and_prefix_lookalikes_are_not_covered():
    site = ["claude code macos", "claude code wordpress"]
    assert fmt.site_coverage("codex", site) == ("gap", None)  # code -> codex is not a match
    assert fmt.site_coverage("claude", site) == ("partial", "claude code macos")  # head term
    assert fmt.site_coverage("wordpress", site) == ("partial", "claude code wordpress")
    assert fmt.site_coverage("claude code mac", site) == ("covered", "claude code macos")
    assert fmt.site_coverage("code review", site) == ("gap", None)
    # Plural folding, the synonym map and long stems still match.
    assert fmt.site_coverage("skill", ["claude code skills"]) == ("partial", "claude code skills")
    assert fmt.site_coverage("skills", ["skill"]) == ("covered", "skill")
    assert fmt.site_coverage("k8s operator", ["kubernetes operators"]) == ("covered", "kubernetes operators")
    assert fmt.site_coverage("automation guide", ["automations guide"]) == ("covered", "automations guide")


# ---- Issue 10: shared level shifts --------------------------------------------------


def _ramp(a, b, n):
    return [round(a + (b - a) * i / (n - 1), 2) for i in range(n)]


IDX = pd.date_range("2025-11-02", periods=53, freq="W-SUN")  # index 36 = 2026-07-12
WP = [30] * 14 + _ramp(55, 73, 22) + [44] * 5 + _ramp(44, 46, 12)  # Feb 8 growth, Jul 12 step
WOO = [40] * 36 + [21] * 12 + [8] * 5  # Jul 12 step, Oct 4 drop
RAG = [43] * 10 + [55] * 3 + [62] * 23 + [36] * 17  # back to an old level on Jul 12
CLAUDE = [30] * 9 + _ramp(62, 67, 27) + _ramp(45, 38, 17)  # Jan 4 growth, Jul 12 step, slide
THREE = {"wordpress mcp": WP, "woocommerce subscriptions": WOO, "rag chatbot": RAG}


def test_shared_step_is_one_warning_dated_the_step_week():
    out = fmt.interest_over_time(pd.DataFrame(THREE, index=IDX), list(THREE))
    shared = [w for w in out["warnings"] if w["type"] == "level_shift" and w["likely_artifact"]]
    assert len(shared) == 1
    assert shared[0]["date"] == "2026-07-12" and shared[0]["direction"] == "down"
    assert set(shared[0]["shared_by"]) == set(THREE)
    for kw in THREE:
        dates = [s["date"] for s in out["summary"][kw]["level_shifts"]]
        assert "2026-07-12" in dates, kw
    s = out["summary"]
    assert [x["date"] for x in s["rag chatbot"]["level_shifts"]] == ["2026-07-12"]  # not 07-19
    assert any(x["date"] == "2026-02-08" and x["change_pct"] > 0 for x in s["wordpress mcp"]["level_shifts"])
    assert any(x["date"] == "2026-10-04" and x["change_pct"] < 0 for x in s["woocommerce subscriptions"]["level_shifts"])
    jul = next(x for x in s["woocommerce subscriptions"]["level_shifts"] if x["date"] == "2026-07-12")
    assert jul["shared"] is True and jul["growth_affected"] is True


def test_level_shifts_lists_every_step_and_keeps_the_largest_as_level_shift():
    out = fmt.interest_over_time(pd.DataFrame({"claude code": CLAUDE}, index=IDX), ["claude code"])
    s = out["summary"]["claude code"]
    assert [x["date"] for x in s["level_shifts"]] == ["2026-01-04", "2026-07-12"]
    jan, jul = s["level_shifts"]
    assert jan["change_pct"] > 100 and jan["growth_affected"] is False
    assert jul["change_pct"] < -30 and jul["growth_affected"] is True and "2026-07-12" in jul["warning"]
    assert s["level_shift"]["date"] == "2026-01-04"  # largest step, kept for compatibility
    assert s["growth_3m"] < 0
    assert [w["date"] for w in out["warnings"]] == ["2026-01-04", "2026-07-12"]
    assert not any(w["likely_artifact"] for w in out["warnings"])


def test_black_friday_five_year_series_has_no_level_shift():
    idx = pd.date_range("2021-11-07", periods=261, freq="W-SUN")
    bf, cm = [40] * 261, [20] * 261
    for year in range(5):
        for k in (3 + 52 * year, 4 + 52 * year):
            bf[k], cm[k] = 100, 60
    out = fmt.interest_over_time(pd.DataFrame({"black friday deals": bf, "cyber monday deals": cm}, index=idx), ["black friday deals", "cyber monday deals"])
    assert out["warnings"] == []
    assert "level_shifts" not in out["summary"]["black friday deals"]


def test_flattened_series_get_a_scale_warning_instead_of_a_shift():
    flat = [1 if i % 5 == 0 else 0 for i in range(53)]
    df = pd.DataFrame({"claude code": CLAUDE, "woocommerce subscriptions": flat}, index=IDX)
    out = fmt.interest_over_time(df, ["claude code", "woocommerce subscriptions"])
    scale = [w for w in out["warnings"] if w["type"] == "flattened"]
    assert len(scale) == 1 and scale[0]["shared_by"] == ["woocommerce subscriptions"]
    assert "separate call" in scale[0]["message"]
    assert "level_shifts" not in out["summary"]["woocommerce subscriptions"]


def test_level_shift_warnings_group_within_one_week():
    shifts = {
        "a": [{"date": "2026-07-12", "change_pct": -40.0}],
        "b": [{"date": "2026-07-19", "change_pct": -35.0}],
        "c": [{"date": "2026-07-12", "change_pct": 50.0}],
    }
    out = fmt.level_shift_warnings(shifts, tolerance_days=7)
    down = next(w for w in out if w["direction"] == "down")
    assert down["date"] == "2026-07-12" and down["shared_by"] == ["a", "b"] and down["likely_artifact"] is True
    up = next(w for w in out if w["direction"] == "up")
    assert up["shared_by"] == ["c"] and up["likely_artifact"] is False


def test_compare_keywords_carries_level_shifts_and_the_shared_warning():
    class Stepped(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            return pd.DataFrame({kw: THREE[kw] for kw in keywords}, index=IDX)

    out = tools.compare_keywords(Stepped(), list(THREE))
    shared = [w for w in out["warnings"] if w["likely_artifact"]]
    assert len(shared) == 1 and shared[0]["date"] == "2026-07-12"
    row = next(r for r in out["ranking"] if r["keyword"] == "woocommerce subscriptions")
    assert [s["date"] for s in row["level_shifts"]] == ["2026-07-12", "2026-10-04"]
    assert row["level_shift"]["date"] == "2026-10-04"


# ---- Issue 11: copy / contract mismatches -------------------------------------------


def test_discover_topics_note_respects_a_category_already_set():
    out = discover_topics(OffTopicClient(), ["claude code"], category=5)
    assert "within this category" in out["note"] and "category=" not in out["note"]
    out = discover_topics(OffTopicClient(), ["claude code"])
    assert "category=" in out["note"]


def test_recurring_topics_reason_does_not_repeat_the_setting(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "coffee", "", 5, top=[], rising=[("cold brew", "breakout")])
    reason = tools.recurring_topics(client, min_episodes=2)["reason"]
    assert "min_episodes=2" not in reason and "longer window" in reason and "polling" in reason
    reason = tools.recurring_topics(client, min_episodes=4)["reason"]
    assert "min_episodes=3" in reason


def test_verdict_angle_for_yes_no_questions():
    assert "verdict" in fmt.ANGLES
    for q in ["is llms.txt worth it", "does llms txt actually work", "is llms txt a standard", "should i use llms.txt"]:
        assert fmt.classify_angle(q) == "verdict", q
    assert fmt.classify_angle("how to add llms txt in wordpress") == "how-to"
    assert fmt.classify_angle("is claude code better than cursor") == "comparison"
    assert fmt.classify_angle("what is llms.txt") == "definition"
    assert fmt.classify_angle("can you run claude code on windows") == "how-to"
    hints = fmt.citability_hints("verdict")
    assert "verdict" in " ".join(hints["evidence"]).lower()


def test_mine_questions_padding_filter_knows_short_verbs_and_participles():
    class Client(FakeClient):
        answers = {
            "where llms.txt": ["where llms.txt stored", "where llms.txt go"],
            "how long llms.txt": ["how long llms.txt last"],
        }

        def suggest(self, query, geo=""):
            return self.answers.get(query.strip(), [])

    out = tools.mine_questions(Client(), "llms.txt", limit=50, prefixes=("where", "how long"))
    texts = [q["question"] for q in out["questions"]]
    assert "where llms.txt stored" in texts and "where llms.txt go" in texts
    assert [f["question"] for f in out["filtered_out"]] == ["how long llms.txt last"]
