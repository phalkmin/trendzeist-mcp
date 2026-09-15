import asyncio
import json

import pandas as pd
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from trendzeist_mcp import server, tools
from trendzeist_mcp.client import Settings, TrendsError
from trendzeist_mcp.discovery import discover_topics


class FakeClient:
    """Stand-in for TrendsClient that never touches the network."""

    settings = Settings(min_interval=0)

    def __init__(self, fail_seed: str | None = None):
        self.calls: list[tuple] = []
        self.fail_seed = fail_seed
        self.autocomplete_fail: set[str] = set()

    def begin_call(self):
        self.calls.append(("begin",))

    def call_meta(self):
        return {"requests_made": 2, "cache_hit": False, "cache_hits": 0, "cache_misses": 2}

    def autocomplete(self, query, geo=""):
        self.calls.append(("ac", query, geo))
        if query in self.autocomplete_fail:
            raise TrendsError("429", retryable=True)
        if query == "espresso":  # bare seed: mixed suggestions
            return ["espresso machine", "how to make espresso", "what is espresso"]
        return [
            f"{query} at home",
            f"{query} at home",  # duplicate
            f"{query} without a machine",
            "espresso machine sale",  # not a question
        ]

    def interest_over_time(self, keywords, tf, geo, cat, gprop):
        self.calls.append(("iot", tuple(keywords)))
        idx = pd.date_range("2026-01-04", periods=12, freq="W")
        data = {kw: [10 * (i + 1) * (n + 1) for i in range(12)] for n, kw in enumerate(keywords)}
        data["isPartial"] = [False] * 11 + [True]
        return pd.DataFrame(data, index=idx)

    def related_queries(self, kw, tf, geo, cat, gprop):
        self.calls.append(("rq", kw))
        if kw == self.fail_seed:
            raise TrendsError("rate limited", retryable=True)
        return {
            "top": pd.DataFrame({"query": [f"{kw} machine", "shared topic"], "value": [100, 40]}),
            "rising": pd.DataFrame(
                {"query": [f"{kw} viral", "shared topic"], "value": [9000, 150]}
            ),
        }

    def related_topics(self, kw, tf, geo, cat, gprop):
        return {"top": None, "rising": None}

    def interest_by_region(self, keywords, tf, geo, cat, gprop, res):
        return pd.DataFrame(
            {keywords[0]: [100, 50], "geoCode": ["US-WY", "US-NM"]},
            index=pd.Index(["Wyoming", "New Mexico"], name="geoName"),
        )

    def suggestions(self, kw):
        return [{"mid": "/m/1", "title": "Coffee roasting", "type": "Topic"}]

    def categories(self):
        return {"name": "All categories", "id": 0, "children": [{"name": "Coffee & Tea", "id": 916}]}

    def trending_rss(self, geo, arts):
        return [
            {
                "title": "espresso",
                "traffic": "20K+",
                "pub_date": "x",
                "articles": [{"title": "A", "source": "S", "url": "http://a"}],
            }
        ]


def test_interest_over_time_tool():
    out = tools.interest_over_time(FakeClient(), ["Espresso", "espresso "], "today 3-m", "us")
    assert out["query"] == {
        "keywords": ["Espresso"],
        "timeframe": "today 3-m",
        "geo": "US",
        "hl": "en-US",
        "category": 0,
        "gprop": "web",
    }
    s = out["summary"]["Espresso"]
    assert s["direction"] == "rising"
    assert s["growth_3m"] is None and s["growth_12m"] is None  # 12 weeks: too short
    assert s["insight"].startswith("Interest in 'Espresso' rose") and "(rising)" in s["insight"]
    assert tools.interest_over_time(FakeClient(), ["x"], geo="BR")["query"]["hl"] == "pt-BR"


def test_compare_keywords_ranks():
    out = tools.compare_keywords(FakeClient(), ["a", "b"])
    assert out["leader"] == "b"
    assert out["ranking"][0]["share_pct"] + out["ranking"][1]["share_pct"] == pytest.approx(100, abs=0.2)


def test_compare_keywords_handles_empty_data():
    class EmptyClient(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            return pd.DataFrame()

    out = tools.compare_keywords(EmptyClient(), ["a", "b"])
    assert out["leader"] is None and out["ranking"] == []
    assert out["unavailable"] == ["a", "b"] and "note" in out
    assert out["scale"] and out["points"] == []


def test_related_topics_reports_unavailable():
    out = tools.related_topics(FakeClient(), "x")
    assert out["available"] is False and "reason" in out


def test_growth_windows_and_insight_on_long_series():
    class LongClient(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2024-01-07", periods=104, freq="W")  # two years, weekly
            vals = [20] * 52 + [40] * 39 + [60] * 13  # step up in the last year, again in the last 3 m
            return pd.DataFrame({keywords[0]: vals}, index=idx)

    s = tools.interest_over_time(LongClient(), ["k"], "today 5-y")["summary"]["k"]
    assert s["growth_12m"] == pytest.approx(122.1, abs=1)  # ~44.6 vs 20
    assert s["growth_3m"] == pytest.approx(50.0, abs=1)  # 60 vs 40
    assert "peaking at 60" in s["insight"] and "(rising)" in s["insight"]


def test_related_queries_questions_angles_and_limit_note():
    class QClient(FakeClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": pd.DataFrame(
                    {"query": ["how to make espresso", "best espresso machine", "What is espresso?"],
                     "value": [100, 80, 60]}
                ),
                "rising": pd.DataFrame(
                    {"query": ["How to make espresso", "espresso vs coffee", "espresso recall 2026"],
                     "value": [9000, 300, 120]}
                ),
            }

    out = tools.related_queries(QClient(), "espresso", limit=500)
    assert out["note"] == "limit 500 exceeds the maximum; clamped to 50."
    assert [t["angle"] for t in out["top"]] == ["how-to", "listicle", "definition"]
    assert [t["angle"] for t in out["rising"]] == ["how-to", "comparison", "news"]
    # Question extracted once (rising wins over top), breakout flag carried over.
    qs = out["questions"]
    assert [q["question"] for q in qs] == ["How to make espresso", "What is espresso?"]
    assert qs[0]["source"] == "rising" and qs[0]["is_breakout"] is True
    assert qs[1]["source"] == "top" and qs[1]["angle"] == "definition"
    assert "note" not in tools.related_queries(QClient(), "espresso", limit=10)


def test_interest_by_region_explains_empty_results():
    class ZeroClient(FakeClient):
        def interest_by_region(self, keywords, tf, geo, cat, gprop, res):
            return pd.DataFrame({keywords[0]: [0, 0]}, index=pd.Index(["A", "B"], name="geoName"))

    class NoneClient(FakeClient):
        def interest_by_region(self, keywords, tf, geo, cat, gprop, res):
            return pd.DataFrame()

    zero = tools.interest_by_region(ZeroClient(), ["x"], resolution="COUNTRY")
    assert zero["available"] is False and "zero interest" in zero["reason"]
    none = tools.interest_by_region(NoneClient(), ["x"], resolution="COUNTRY", limit=999)
    assert none["available"] is False and "no regional data" in none["reason"]
    assert none["note"].startswith("limit 999")
    ok = tools.interest_by_region(FakeClient(), ["x"], resolution="COUNTRY")
    assert ok["available"] is True and "reason" not in ok


def test_mine_questions_expands_dedupes_and_tags():
    client = FakeClient()
    out = tools.mine_questions(client, "espresso", geo="br", limit=6)
    assert out["query"] == {"seed": "espresso", "geo": "BR", "hl": "pt-BR"}
    assert client.calls[0] == ("ac", "espresso", "BR")
    assert client.calls[1] == ("ac", "how to espresso", "BR")
    assert out["count"] == 6 and len(out["questions"]) == 6
    texts = [q["question"] for q in out["questions"]]
    assert texts[0] == "how to make espresso" and "what is espresso" in texts
    assert len(set(texts)) == 6  # duplicates removed
    assert "espresso machine sale" not in texts  # non-question dropped
    assert out["questions"][0]["angle"] == "how-to" and out["questions"][0]["prefix"] is None
    assert out["by_angle"] == {"how-to": 5, "definition": 1}
    assert out["errors"] == [] and "partial" not in out
    # Budget is spread across prefixes (max 3 each) and stops once the limit is met.
    assert out["prefixes_queried"] == ["(seed)", "how to", "how do"]
    assert [q["prefix"] for q in out["questions"]] == [None, None, "how to", "how to", "how do", "how do"]
    assert "hl=pt-BR" in out["language_note"]
    assert "language_note" not in tools.mine_questions(FakeClient(), "espresso", geo="US", limit=3)


def test_mine_questions_vs_prefix_and_rate_limit_partial():
    client = FakeClient()
    client.autocomplete_fail = {"why espresso"}
    out = tools.mine_questions(client, "espresso", limit=100)
    assert out["errors"] == [{"prefix": "why", "error": "429"}]
    assert "partial" in out and out["prefixes_queried"] == ["(seed)", "how to", "how do", "how much", "how long"]
    assert out["count"] > 0  # earlier prefixes still returned

    client = FakeClient()
    client.autocomplete_fail = {"how to espresso"}  # non-fatal? it's retryable -> stops
    out = tools.mine_questions(client, "espresso", limit=100)
    assert out["prefixes_queried"] == ["(seed)"]


def test_mine_questions_reports_empty():
    class Silent(FakeClient):
        def autocomplete(self, query, geo=""):
            return []

    out = tools.mine_questions(Silent(), "zzz")
    assert out["count"] == 0 and "reason" in out
    assert tools.mine_questions(Silent(), "zzz", limit=101)["note"].startswith("limit 101")


def test_trending_now_tags_angle_and_explains_empty():
    class Empty(FakeClient):
        def trending_rss(self, geo, arts):
            return []

    out = tools.trending_now(FakeClient(), "US", 20)
    assert out["trends"][0]["angle"] == "news"
    assert out["note"] == "max_articles 20 exceeds the maximum; clamped to 10."
    assert "reason" in tools.trending_now(Empty(), "US", 0)


def test_region_suggest_trending_categories():
    assert tools.interest_by_region(FakeClient(), ["x"], resolution="region")["regions"][0]["region"] == "Wyoming"
    assert tools.suggest_keywords(FakeClient(), "coffee")["suggestions"][0]["mid"] == "/m/1"
    tr = tools.trending_now(FakeClient(), "br", 1)
    assert tr["geo"] == "BR" and tr["trends"][0]["articles"][0]["source"] == "S"
    assert tr["trends"][0]["published"] == "x"
    cats = tools.list_categories(FakeClient(), "coffee")
    assert cats["total_matches"] == 1 and cats["categories"][0]["id"] == 916


def test_discover_topics_ranks_and_dedupes():
    client = FakeClient()
    out = discover_topics(client, ["espresso", "latte"], geo="US")
    topics = {t["topic"]: t for t in out["topics"]}
    assert out["topics"][0]["signal"] == "breakout"
    shared = topics["shared topic"]
    assert shared["signal"] == "rising" and sorted(shared["source_seeds"]) == ["espresso", "latte"]
    assert shared["popularity"] == 40  # merged from 'top'
    assert out["counts"]["breakout"] == 2 and out["errors"] == []
    assert client.calls[0] == ("iot", ("espresso", "latte"))


def test_discover_topics_collects_questions_and_angles():
    class QClient(FakeClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": pd.DataFrame({"query": [f"how to clean {kw}", "best beans"], "value": [90, 50]}),
                "rising": pd.DataFrame({"query": ["How to clean espresso", f"{kw} vs pod"], "value": [8000, 200]}),
            }

    out = discover_topics(QClient(), ["espresso", "latte"], geo="BR")
    assert out["query"]["hl"] == "pt-BR"
    # Cross-seed dedupe by normalised form: "how to clean espresso" appears once.
    qs = [q["question"].lower() for q in out["questions"]]
    assert qs == ["how to clean espresso", "how to clean latte"]
    assert out["questions"][0]["seed"] == "espresso" and out["questions"][0]["is_breakout"] is True
    assert out["counts"]["questions"] == 2
    angles = {t["topic"]: t["angle"] for t in out["topics"]}
    assert angles["best beans"] == "listicle" and angles["espresso vs pod"] == "comparison"
    assert "note" not in out
    assert discover_topics(QClient(), ["x"], max_per_seed=99)["note"].startswith("max_per_seed 99")


def test_discover_topics_iot_non_retryable_failure_still_fetches_related():
    class IotBroken(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            raise TrendsError("rejected", retryable=False)

    client = IotBroken()
    out = discover_topics(client, ["espresso", "latte"])
    assert out["errors"] == [{"step": "interest_over_time", "error": "rejected"}]
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso"), ("rq", "latte")]
    assert all(s["trend"] == {"available": False} and s["rising_count"] == 2 for s in out["seeds"])
    assert out["counts"]["breakout"] == 2


def test_discover_topics_ranking_is_seed_order_independent():
    class GrowthClient(FakeClient):
        growth = {"a": 100, "b": 4000}

        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": None,
                "rising": pd.DataFrame({"query": ["shared topic"], "value": [self.growth[kw]]}),
            }

    forward = discover_topics(GrowthClient(), ["a", "b"])["topics"][0]
    backward = discover_topics(GrowthClient(), ["b", "a"])["topics"][0]
    assert forward["growth_pct"] == backward["growth_pct"] == 4000
    assert sorted(forward["source_seeds"]) == ["a", "b"]


def test_discover_topics_rate_limit_skips_uncached_seeds_but_reports_all():
    client = FakeClient(fail_seed="espresso")
    out = discover_topics(client, ["espresso", "latte", "mocha"], geo="US")
    assert out["errors"][0]["step"] == "related_queries:espresso"
    # Every seed gets a report; later ones are marked skipped, not silently dropped.
    assert [s["seed"] for s in out["seeds"]] == ["espresso", "latte", "mocha"]
    assert "skipped" in out["seeds"][1] and "skipped" in out["seeds"][2]
    # No further related_queries calls went to "Google" after the 429.
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso")]
    assert out["topics"] == []


def test_discover_topics_rate_limit_still_serves_cached_seeds():
    class CachingClient(FakeClient):
        cached = {"latte"}

        def has_cached_related_queries(self, kw, tf, geo, cat, gprop):
            return kw in self.cached

    client = CachingClient(fail_seed="espresso")
    out = discover_topics(client, ["espresso", "latte", "mocha"])
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso"), ("rq", "latte")]
    assert out["seeds"][1].get("rising_count") == 2
    assert "skipped" in out["seeds"][2]
    assert any(t["source_seeds"] == ["latte"] for t in out["topics"])


def test_discover_topics_iot_rate_limit_stops_further_requests():
    class IotFails(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            raise TrendsError("429", retryable=True)

    client = IotFails()
    out = discover_topics(client, ["espresso", "latte"])
    assert out["errors"][0]["step"] == "interest_over_time"
    assert [c for c in client.calls if c[0] == "rq"] == []
    assert all("skipped" in s for s in out["seeds"])


def test_discover_topics_non_retryable_error_continues():
    class BadSeed(FakeClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            if kw == "espresso":
                self.calls.append(("rq", kw))
                raise TrendsError("rejected", retryable=False)
            return super().related_queries(kw, tf, geo, cat, gprop)

    client = BadSeed()
    out = discover_topics(client, ["espresso", "latte"])
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso"), ("rq", "latte")]
    assert "error" in out["seeds"][0] and out["seeds"][1]["rising_count"] == 2


def test_interest_by_region_rejects_unsupported_geo_resolution():
    from trendzeist_mcp.validation import ValidationError

    with pytest.raises(ValidationError, match="REGION"):
        tools.interest_by_region(FakeClient(), ["x"], geo="BR", resolution="CITY")
    with pytest.raises(ValidationError, match="COUNTRY"):
        tools.interest_by_region(FakeClient(), ["x"], geo="US", resolution="COUNTRY")
    assert tools.interest_by_region(FakeClient(), ["x"], geo="US", resolution="DMA")["query"]["resolution"] == "DMA"
    assert tools.interest_by_region(FakeClient(), ["x"], geo="BR", resolution="region")["query"]["resolution"] == "REGION"


def test_server_registers_tools_and_prompt(monkeypatch):
    monkeypatch.setattr(server, "_client", FakeClient())
    names = {t.name for t in asyncio.run(server.server.list_tools())}
    assert names == {
        "interest_over_time",
        "compare_keywords",
        "related_queries",
        "related_topics",
        "interest_by_region",
        "suggest_keywords",
        "trending_now",
        "list_categories",
        "discover_topics",
        "mine_questions",
    }
    assert [p.name for p in asyncio.run(server.server.list_prompts())] == ["blog_ideas_from_trends"]

    res = asyncio.run(server.server.call_tool("related_queries", {"keyword": "espresso"}))
    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    assert payload["rising"][0]["is_breakout"] is True
    # Every result carries the schema version and per-call meta (C11 / N15).
    assert payload["schema_version"] == tools.SCHEMA_VERSION == 1
    assert payload["_meta"]["requests_made"] == 2 and payload["_meta"]["cache_hit"] is False
    assert server._client.calls[0] == ("begin",)

    mq = asyncio.run(server.server.call_tool("mine_questions", {"seed": "espresso", "limit": 3}))
    mq_payload = json.loads(mq.content[0].text)
    assert mq_payload["count"] == 3 and mq_payload["schema_version"] == 1

    # Validation failures surface as ToolError with an actionable message.
    with pytest.raises(ToolError, match="Invalid argument"):
        asyncio.run(server.server.call_tool("interest_over_time", {"keywords": [], "timeframe": "x"}))
