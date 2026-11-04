"""0.6.0 output-quality fixes found while writing the tutorial series (2026-11-03):
off-topic rising queries, spam names linked to a flagged domain, fuzzy site coverage,
level-shift warnings, mine_questions noise ranking and what_changed's first run."""

import pandas as pd

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import tools
from trendzeist_mcp.aeo import aeo_opportunities
from trendzeist_mcp.discovery import discover_topics

from test_tools import AeoClient, FakeClient
from test_tools_050 import ArchiveClient, _seed_archive


# ---- Issue 1: off-topic rising queries ----------------------------------------


class OffTopicClient(FakeClient):
    """Rising list mixing real neighbours with chatbot test prompts (run 2026-11-03)."""

    def related_queries(self, kw, tf, geo, cat, gprop):
        self.calls.append(("rq", kw))
        return {
            "top": pd.DataFrame({"query": [f"{kw} mac", f"{kw} harness"], "value": [100, 40]}),
            "rising": pd.DataFrame(
                {
                    "query": [
                        "best time to visit maldives",
                        "how to bake a cake",
                        f"{kw} router",
                        "deepseek harness",
                        f"how to install {kw}",
                    ],
                    "value": [7150, 6250, 5000, 300, 250],
                }
            ),
        }


def test_flag_off_topic_marks_queries_sharing_nothing_with_seed_or_top():
    rising = fmt.mark_breakouts(
        [{"name": n, "value": 9000} for n in [
            "best time to visit maldives", "how to bake a cake", "claude code router",
            "deepseek harness", "llms txt vs robots txt",
        ]]
    )
    top = [{"name": "claude code harness", "value": 100}]
    fmt.flag_off_topic(rising, top, "claude code")
    flags = [bool(it.get("off_topic")) for it in rising]
    assert flags == [True, True, False, False, True]
    assert "no word in common" in rising[0]["off_topic_reason"]
    # Punctuation in the seed must not hide a match ("llms.txt" ~ "llms txt").
    rising = fmt.mark_breakouts(
        [{"name": "llms txt vs robots txt", "value": 9000}, {"name": "what is a roth ira", "value": 9000}]
    )
    fmt.flag_off_topic(rising, [], "llms.txt")
    assert [bool(it.get("off_topic")) for it in rising] == [False, True]
    # Already-suspect items are left to the spam heuristic.
    rising = [{"name": "apple smartring abraham quiros villalba", "value": 100, "suspect": True}]
    fmt.flag_off_topic(rising, [], "claude code")
    assert "off_topic" not in rising[0]


def test_discover_topics_moves_off_topic_queries_to_filtered_out():
    out = discover_topics(OffTopicClient(), ["claude code"])
    topics = [t["topic"] for t in out["topics"]]
    assert "best time to visit maldives" not in topics and "how to bake a cake" not in topics
    assert "claude code router" in topics and "deepseek harness" in topics
    assert [q["question"] for q in out["questions"]] == ["how to install claude code"]
    assert [f["topic"] for f in out["filtered_out"]] == ["best time to visit maldives", "how to bake a cake"]
    assert out["filtered_out"][0]["kind"] == "off_topic" and out["filtered_out"][0]["source_seed"] == "claude code"
    assert out["filtered_out"][0]["is_breakout"] is True and out["filtered_out"][0]["growth_pct"] == 7150
    assert out["counts"]["off_topic"] == 2 and out["seeds"][0]["off_topic_count"] == 2
    assert "category=" in out["note"] and "unrelated" in out["note"]
    assert "filtered_out" in out["guidance"]
    clean = discover_topics(FakeClient(), ["espresso"])
    assert clean["filtered_out"] == [] and clean["counts"]["off_topic"] == 0 and "note" not in clean


def test_aeo_opportunities_keeps_off_topic_questions_out_of_clusters():
    class Client(OffTopicClient, AeoClient):
        pass

    out = aeo_opportunities(Client(), ["claude code"])
    questions = [q for c in out["opportunities"] for q in c["questions"]]
    assert "how to bake a cake" not in questions
    assert "how to install claude code" in questions
    assert out["seeds"][0]["off_topic_dropped"] == ["best time to visit maldives", "how to bake a cake"]


# ---- Issue 2: spam name carried by a flagged domain -----------------------------


def test_flag_suspects_links_name_carriers_to_a_flagged_domain():
    rising = fmt.mark_breakouts(
        [{"name": n, "value": 9000} for n in [
            "abrahamquirosvillalba .com",
            "solar shingles vs solar panels abraham quiros villalba",
            "apple smartring abraham quiros villalba",
            "claude code router",
            "claude code harness",
        ]]
    )
    out = fmt.flag_suspects(rising, [], "claude code")
    assert [bool(it.get("suspect")) for it in out] == [True, True, True, False, False]
    assert out[0]["suspect_reason"].startswith("domain-like")
    assert "abraham quiros villalba" in out[1]["suspect_reason"]
    assert "abrahamquirosvillalba .com" in out[1]["suspect_reason"]


def test_flag_suspects_domain_label_needs_two_or_more_tokens_to_link():
    # A one-word brand domain must not flag every query mentioning the brand.
    rising = [{"name": n, "value": 100} for n in ["breville .com", "breville barista express", "breville bambino"]]
    out = fmt.flag_suspects(rising, [], "espresso")
    assert [bool(it.get("suspect")) for it in out] == [True, False, False]


# ---- Issue 3: fuzzy site coverage ----------------------------------------------


def test_site_coverage_matches_near_variants():
    assert fmt.site_coverage("claude code mac", ["claude code macos"]) == ("covered", "claude code macos")
    assert fmt.site_coverage("claude code skill", ["claude code skills"]) == ("covered", "claude code skills")
    assert fmt.site_coverage("wp mcp", ["wordpress mcp server"]) == ("covered", "wordpress mcp server")
    assert fmt.site_coverage("wordpress mcp", ["wordpress ai integration"]) == ("gap", None)
    # Jaccard threshold stays 0.6 (conservative): one shared token out of three is a gap.
    assert fmt.site_coverage("claude code mac setup", ["claude code macos"]) == ("partial", "claude code macos")
    assert fmt.site_coverage("espresso beans", ["espresso machine"]) == ("gap", None)
    # Exact behaviour is unchanged.
    assert fmt.site_coverage("espresso machine", ["Espresso Machine"]) == ("covered", "Espresso Machine")


# ---- Issue 4: level shift -------------------------------------------------------


def _weekly(values, start="2025-11-02"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="W-SUN"))


def test_detect_level_shift_finds_sustained_step():
    shift = fmt.detect_level_shift(_weekly([60] * 36 + [38] * 17))
    assert shift["date"] == "2026-07-12"
    assert shift["change_pct"] == -36.7
    assert shift["before_mean"] == 60 and shift["after_mean"] == 38
    assert shift["post_shift_change_pct"] == 0.0
    assert fmt.detect_level_shift(_weekly([60] * 53)) is None


def test_detect_level_shift_ignores_a_spike_that_reverts():
    # Black-Friday shape: two weeks at 100 on a flat 40 baseline.
    assert fmt.detect_level_shift(_weekly([40] * 20 + [100, 100] + [40] * 31)) is None
    # A six-week season that ends is not a level shift either.
    assert fmt.detect_level_shift(_weekly([40] * 20 + [80] * 6 + [40] * 27)) is None


def test_interest_over_time_warns_when_series_step_together():
    idx = pd.date_range("2025-11-02", periods=53, freq="W-SUN")
    before = {"claude code": 59, "claude code skills": 71, "wordpress mcp": 72,
              "woocommerce subscriptions": 42, "rag chatbot": 62}
    after = {"claude code": 39, "claude code skills": 37, "wordpress mcp": 42,
             "woocommerce subscriptions": 22, "rag chatbot": 38}
    df = pd.DataFrame({kw: [before[kw]] * 36 + [after[kw]] * 17 for kw in before}, index=idx)
    df["flat"] = [50] * 53
    out = fmt.interest_over_time(df, list(before) + ["flat"])
    s = out["summary"]["claude code"]
    assert s["level_shift"]["date"] == "2026-07-12" and s["level_shift"]["growth_affected"] is True
    assert "2026-07-12" in s["level_shift"]["warning"]
    assert "level_shift" not in out["summary"]["flat"]
    assert len(out["warnings"]) == 1
    w = out["warnings"][0]
    assert w["type"] == "level_shift" and w["date"] == "2026-07-12" and w["direction"] == "down"
    assert set(w["shared_by"]) == set(before) and w["likely_artifact"] is True
    assert "overstate" in w["message"]


def test_compare_keywords_and_aeo_surface_level_shift():
    class Stepped(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2025-11-02", periods=53, freq="W-SUN")
            return pd.DataFrame({kw: [60] * 36 + [38] * 17 for kw in keywords}, index=idx)

    out = tools.compare_keywords(Stepped(), ["claude code", "n8n"])
    assert out["warnings"][0]["shared_by"] == ["claude code", "n8n"]
    assert out["ranking"][0]["level_shift"]["date"] == "2026-07-12"

    class SteppedAeo(AeoClient, Stepped):
        pass

    out = aeo_opportunities(SteppedAeo(), ["espresso"])
    assert out["opportunities"][0]["trend"]["level_shift"]["date"] == "2026-07-12"


# ---- Issue 5: mine_questions noise --------------------------------------------


class LlmsClient(FakeClient):
    """Autocomplete answers seen for 'llms.txt' on 2026-11-03 (abridged)."""

    answers = {
        "llms.txt": ["llms.txt generator", "what is llms.txt"],
        "how to llms.txt": ["how to add llms txt in wordpress", "how to llms.txt"],
        "how long llms.txt": ["how long llms.txt last"],
        "when llms.txt": ["when llms.txt file", "when was llms.txt introduced"],
        "does llms.txt": ["does llms txt actually work"],
        "what is llms.txt": ["what is llms.txt", "what is llms.txt file"],
        "llms.txt vs": ["llms.txt vs robots.txt"],
    }

    def suggest(self, query, geo=""):
        self.raw_autocomplete_queries.append(query)
        return self.answers.get(query.strip(), [])


def test_mine_questions_filters_padding_and_ranks_by_realness():
    out = tools.mine_questions(
        LlmsClient(), "llms.txt", limit=100,
        prefixes=("how to", "how long", "when", "does", "what is", "vs"),
    )
    texts = [q["question"] for q in out["questions"]]
    assert "how long llms.txt last" not in texts and "when llms.txt file" not in texts
    assert "how to llms.txt" not in texts
    filtered = {f["question"]: f["reason"] for f in out["filtered_out"]}
    assert set(filtered) == {"how to llms.txt", "how long llms.txt last", "when llms.txt file"}
    assert "no verb" in filtered["how long llms.txt last"]
    assert out["filtered_count"] == 3
    # Ranked: scores descending, the seed-only "what is" (two prefixes) beats single-prefix hits.
    scores = [q["score"] for q in out["questions"]]
    assert scores == sorted(scores, reverse=True)
    assert texts[0] == "what is llms.txt" and out["questions"][0]["prefix_hits"] == 2
    assert {"does llms txt actually work", "how to add llms txt in wordpress", "llms.txt vs robots.txt"} <= set(texts[:5])
    assert "score" in out["guidance"]


# ---- Issue 6: what_changed first run ------------------------------------------


def test_what_changed_first_run_lists_current_breakouts_as_first_seen(tmp_path):
    client = ArchiveClient(tmp_path)
    out = tools.what_changed(client, ["espresso"], since_days=7)
    seed = out["seeds"][0]
    assert seed["first_run"] is True and "baseline recorded" in seed["reason"]
    assert [i["topic"] for i in seed["first_seen"]] == ["espresso viral", "shared topic"]
    assert seed["first_seen"][0]["status"] == "breakout"
    assert seed["first_seen"][0]["action"] == "investigate now (no history yet)"
    assert seed["first_seen"][1]["action"] == "consider (no history yet)"
    assert out["available"] is False
    assert "first_run" in out["guidance"]


def test_what_changed_uses_a_younger_snapshot_when_none_is_old_enough(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "espresso", "", 2, top=["gone"], rising=[("shared topic", "rising")])
    out = tools.what_changed(client, ["espresso"], since_days=7)
    seed = out["seeds"][0]
    assert "first_run" not in seed and seed["baseline_at"] is not None
    assert seed["baseline_age_days"] == 2
    assert "2 days ago" in seed["baseline_note"] and "7 requested" in seed["baseline_note"]
    assert [b["topic"] for b in seed["new_breakouts"]] == ["espresso viral"]
    assert [d["topic"] for d in seed["dropped"]] == ["gone"]
    assert out["available"] is True
