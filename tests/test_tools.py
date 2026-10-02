import asyncio
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import server, tools
from trendzeist_mcp.aeo import _AEO_PREFIXES, aeo_opportunities
from trendzeist_mcp.client import Settings, TrendsError
from trendzeist_mcp.discovery import discover_topics


class _FakeCache:
    def stats(self):
        return {
            "dir": None,
            "disk_enabled": False,
            "memory_entries": 3,
            "max_memory_entries": 256,
            "disk_files": None,
        }


class FakeClient:
    """Stand-in for the Hub and every source; never touches the network.

    The Hub exposes sources as attributes (``hub.trends``, ``hub.autocomplete`` ...);
    one fake serves them all, so ``self.trends is self``.
    """

    settings = Settings(min_interval=0)
    cache = _FakeCache()

    def __init__(self, fail_seed: str | None = None):
        self.calls: list[tuple] = []
        self.fail_seed = fail_seed
        self.autocomplete_fail: set[str] = set()
        self.iot_timeframes: list[str] = []
        self.raw_autocomplete_queries: list[str] = []
        self.trends = self
        self.autocomplete = self
        self.news = self
        self.wikipedia = self

    def begin_call(self):
        self.calls.append(("begin",))

    def call_meta(self):
        return {"requests_made": 2, "cache_hit": False, "cache_hits": 0, "cache_misses": 2}

    def statuses(self):
        return [
            {"name": "google_trends", "label": "Google Trends", "state": "live",
             "last_success": "2026-09-19T10:00:00Z", "last_error": None, "last_error_at": None},
            {"name": "wikipedia", "label": "Wikipedia", "state": "idle",
             "last_success": None, "last_error": None, "last_error_at": None},
        ]

    # ---- Google News / Wikipedia fakes
    def articles(self, query, geo):
        self.calls.append(("news", query, geo))
        now = datetime.now(timezone.utc)

        def ts(hours):
            return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        return [
            {"title": f"{query} review", "publisher": "The Manual", "url": "http://a", "published": ts(2)},
            {"title": f"{query} deal", "publisher": "The Manual", "url": "http://b", "published": ts(50)},
            {"title": f"{query} recall", "publisher": "TechRadar", "url": "http://c", "published": ts(24 * 20)},
            {"title": "old story", "publisher": "Tribune", "url": "http://d", "published": ts(24 * 45)},
            {"title": "undated", "publisher": None, "url": None, "published": None},
        ]

    def search(self, topic, lang):
        self.calls.append(("wiki_search", topic, lang))
        if topic == "zzz":
            return {"found": False, "suggestion": "zz"}
        return {"found": True, "title": topic.title(), "pageid": 1, "wordcount": 500}

    def pageviews(self, title, lang, days, end=None):
        self.calls.append(("wiki_views", title, lang, days))
        return [{"date": f"2026-08-{d:02d}", "views": 100 + 10 * d} for d in range(1, min(days, 30) + 1)]

    # ---- Google Trends / Autocomplete fakes
    def suggest(self, query, geo=""):
        self.raw_autocomplete_queries.append(query)
        query = query.strip()  # the tool sends a trailing space (10.3); the fake ignores it
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
        self.iot_timeframes.append(tf)
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
    assert s["direction_now"] == "stable"  # linear ramp: last quarter is only +24%
    assert s["growth_3m"] is None and s["growth_12m"] is None  # 12 weeks: too short
    assert s["growth_note"] == fmt.GROWTH_NOTE
    assert s["insight"].startswith("Interest in 'Espresso' rose")
    assert s["insight"].endswith("and rose 24% in the last quarter of the period (stable).")
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


def test_mine_questions_drops_partial_word_completions():
    """10.3: 'wordpress ai' must not yield 'wordpress airplay' / 'ain't' questions."""

    class Completing(FakeClient):
        def suggest(self, query, geo=""):
            self.raw_autocomplete_queries.append(query)
            return [
                "why wordpress airplay not working",
                "why wordpress ain't working",
                "what is wordpress airtable",
                "why wordpress ai is bad",
                "what is wordpress AI, really",
                "how to use wordpress ai plugins",
            ]

    client = Completing()
    out = tools.mine_questions(client, "wordpress ai", limit=100, prefixes=("why", "vs"))
    texts = [q["question"] for q in out["questions"]]
    assert texts == [
        "why wordpress ai is bad",
        "what is wordpress AI, really",
        "how to use wordpress ai plugins",
    ]
    # Every request ends with a space so Google suggests the next word.
    assert client.raw_autocomplete_queries == ["wordpress ai ", "why wordpress ai ", "wordpress ai vs "]


def test_mine_questions_reports_empty():
    class Silent(FakeClient):
        def suggest(self, query, geo=""):
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


class SpamClient(FakeClient):
    """Rising list with an injected personal name (run-data 10.2) plus one real question."""

    def related_queries(self, kw, tf, geo, cat, gprop):
        self.calls.append(("rq", kw))
        return {
            "top": pd.DataFrame({"query": [f"{kw} cursor", f"install {kw}"], "value": [100, 40]}),
            "rising": pd.DataFrame(
                {
                    "query": [
                        f"codex vs {kw} abraham quiros villalba",
                        "apple smartring abraham quiros villalba",
                        "solar shingles vs solar panels abraham quiros villalba",
                        "abrahamquirosvillalba .com",
                        f"how to install {kw}",
                        f"{kw} vs cursor",
                    ],
                    "value": [13400, 4550, 4500, 4250, 9000, 300],
                }
            ),
        }


def test_discover_topics_keeps_suspect_breakouts_out_of_ranking():
    out = discover_topics(SpamClient(), ["claude code"])
    topics = [t["topic"] for t in out["topics"]]
    assert "codex vs claude code abraham quiros villalba" not in topics
    assert "abrahamquirosvillalba .com" not in topics
    assert out["topics"][0] == {
        "topic": "how to install claude code", "signal": "breakout", "growth_pct": 9000,
        "popularity": None, "source_seeds": ["claude code"], "angle": "how-to",
    }
    assert out["counts"] == {"breakout": 1, "rising": 1, "evergreen": 2, "questions": 1, "suspect": 4}
    assert [s["topic"] for s in out["suspect"]][:1] == ["codex vs claude code abraham quiros villalba"]
    assert out["suspect"][0]["is_breakout"] is True and "abraham quiros villalba" in out["suspect"][0]["reason"]
    assert out["seeds"][0]["suspect_count"] == 4 and out["seeds"][0]["rising_count"] == 2
    assert "suspect" in out["guidance"]
    # A clean run has the key too (empty), so callers never KeyError.
    clean = discover_topics(FakeClient(), ["espresso"])
    assert clean["suspect"] == [] and clean["counts"]["suspect"] == 0


def test_related_queries_flags_suspects_and_excludes_them_from_questions():
    out = tools.related_queries(SpamClient(), "claude code")
    flagged = [r["name"] for r in out["rising"] if r.get("suspect")]
    assert len(flagged) == 4 and "abrahamquirosvillalba .com" in flagged
    assert out["suspect_count"] == 4 and "suspect" in out["notes"]
    assert [q["question"] for q in out["questions"]] == ["how to install claude code"]
    assert "suspect_count" not in tools.related_queries(FakeClient(), "espresso")


def test_aeo_opportunities_ignores_suspect_breakouts():
    class SpamAeo(SpamClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            res = super().related_queries(kw, tf, geo, cat, gprop)
            # make the spam item question-shaped so only the suspect filter can stop it
            res["rising"].loc[0, "query"] = f"why {kw} abraham quiros villalba"
            return res

    out = aeo_opportunities(SpamAeo(), ["claude code"])
    all_qs = [q for op in out["opportunities"] for q in op["questions"]]
    assert not any("abraham" in q for q in all_qs)
    assert any(q == "how to install claude code" for q in all_qs)


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


def test_discover_topics_iot_rate_limit_still_tries_related_queries():
    """10.5: the trend step is enrichment; its 429 must not kill the main output."""

    class IotFails(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            raise TrendsError("429", retryable=True)

    client = IotFails()
    out = discover_topics(client, ["espresso", "latte"])
    assert out["errors"] == [{"step": "interest_over_time", "error": "429"}]
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso"), ("rq", "latte")]
    assert all(s["trend"] == {"available": False} and "skipped" not in s for s in out["seeds"])
    assert out["counts"]["breakout"] == 2


def test_discover_topics_iot_then_related_429_enters_cooldown():
    class BothFail(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            raise TrendsError("429", retryable=True)

    client = BothFail(fail_seed="espresso")
    out = discover_topics(client, ["espresso", "latte", "mocha"])
    assert [c for c in client.calls if c[0] == "rq"] == [("rq", "espresso")]
    assert out["seeds"][1]["skipped"].endswith("Retry in ~60 s with 1-2 seeds.")
    assert "skipped" in out["seeds"][2] and out["topics"] == []


def test_discover_topics_trend_context_uses_12_months():
    client = FakeClient()
    out = discover_topics(client, ["espresso"], timeframe="today 3-m")
    assert out["query"]["timeframe"] == "today 3-m"
    assert out["query"]["trend_timeframe"] == "today 12-m"
    assert client.iot_timeframes == ["today 12-m"]
    assert "direction_now" in out["seeds"][0]["trend"]


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
    monkeypatch.setattr(server, "_hub", FakeClient())
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
        "aeo_opportunities",
        "news_coverage",
        "wiki_attention",
        "trendzeist_status",
    }
    assert [p.name for p in asyncio.run(server.server.list_prompts())] == [
        "blog_ideas_from_trends", "answer_brief", "content_brief",
    ]

    res = asyncio.run(server.server.call_tool("related_queries", {"keyword": "espresso"}))
    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    assert payload["rising"][0]["is_breakout"] is True
    # Every result carries the schema version and per-call meta (C11 / N15).
    assert payload["schema_version"] == tools.SCHEMA_VERSION == 2
    assert payload["_meta"]["requests_made"] == 2 and payload["_meta"]["cache_hit"] is False
    assert server._hub.calls[0] == ("begin",)

    mq = asyncio.run(server.server.call_tool("mine_questions", {"seed": "espresso", "limit": 3}))
    mq_payload = json.loads(mq.content[0].text)
    assert mq_payload["count"] == 3 and mq_payload["schema_version"] == 2

    # Validation failures surface as ToolError with an actionable message.
    with pytest.raises(ToolError, match="Invalid argument"):
        asyncio.run(server.server.call_tool("interest_over_time", {"keywords": [], "timeframe": "x"}))


def test_interest_over_time_honours_max_series_points_setting():
    class Long(FakeClient):
        settings = Settings(min_interval=0, max_series_points=5)

        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2025-01-01", periods=100, freq="D")
            return pd.DataFrame({keywords[0]: range(100)}, index=idx)

    out = tools.interest_over_time(Long(), ["k"])
    assert len(out["points"]) == 5 and out["downsampled"] is True


def test_news_coverage_tool():
    client = FakeClient()
    out = tools.news_coverage(client, "Espresso", geo="", limit=2)
    assert client.calls == [("news", "Espresso", "US")]
    assert out["query"] == {"topic": "Espresso", "geo": "US", "hl": "en-US"}
    assert out["articles_total"] == 5 and out["available"] is True
    assert [h["title"] for h in out["headlines"]] == ["Espresso review", "Espresso deal"]
    assert out["publishers"][0] == {"publisher": "The Manual", "articles": 2, "share_pct": 40.0}
    assert out["publishers"][-1]["publisher"] == "unknown"
    assert out["recency"]["last_7d"] == 2 and out["recency"]["undated"] == 1
    assert out["coverage"] == "low" and "note" not in out
    assert tools.news_coverage(FakeClient(), "x", limit=99)["note"].startswith("limit 99")
    assert tools.news_coverage(FakeClient(), "x", geo="BR-SP")["query"]["geo"] == "BR"


def test_news_coverage_explains_empty():
    class Quiet(FakeClient):
        def articles(self, query, geo):
            return []

    out = tools.news_coverage(Quiet(), "zzz", geo="BR")
    assert out["available"] is False and "reason" in out and out["coverage"] == "none"
    assert out["publishers"] == [] and out["headlines"] == []


def test_wiki_attention_tool():
    client = FakeClient()
    out = tools.wiki_attention(client, "espresso machine", lang="EN", days=200)
    assert client.calls == [
        ("wiki_search", "espresso machine", "en"),
        ("wiki_views", "Espresso Machine", "en", 90),
    ]
    assert out["query"] == {"topic": "espresso machine", "lang": "en", "days": 90}
    assert out["has_article"] is True
    assert out["article"]["url"] == "https://en.wikipedia.org/wiki/Espresso_Machine"
    assert out["article"]["exact_match"] is True and out["article"]["wordcount"] == 500
    assert out["pageviews"]["days"] == 30 and out["pageviews"]["direction"] == "rising"
    assert len(out["points"]) == 30
    assert out["note"] == "days 200 exceeds the maximum; clamped to 90."
    with pytest.raises(tools.v.ValidationError, match="between 7 and 90"):
        tools.wiki_attention(FakeClient(), "x", days=3)


def test_wiki_attention_reports_gap():
    out = tools.wiki_attention(FakeClient(), "zzz")
    assert out["has_article"] is False and out["suggestion"] == "zz"
    assert "reason" in out and "gap" in out["guidance"] and "pageviews" not in out


def test_wiki_search_hit_is_only_a_related_article():
    class Related(FakeClient):
        def search(self, topic, lang):
            self.calls.append(("wiki_search", topic, lang))
            return {"found": True, "title": "Coffee", "pageid": 1, "wordcount": 500}

    client = Related()
    out = tools.wiki_attention(client, "espresso")
    assert out["has_article"] is False
    assert out["related_article"] == "Coffee" and out["suggestion"] == "Coffee"
    assert "pageviews" not in out and [call[0] for call in client.calls] == ["wiki_search"]
    assert "cannot establish" in out["reason"]


def test_wiki_attention_encodes_reserved_characters_in_article_url():
    class Special(FakeClient):
        def search(self, topic, lang):
            return {"found": True, "title": "A#B?", "pageid": 1, "wordcount": 500}

    out = tools.wiki_attention(Special(), "A#B?")
    assert out["has_article"] is True
    assert out["article"]["url"] == "https://en.wikipedia.org/wiki/A%23B%3F"


def test_trendzeist_status_reports_without_probing():
    from trendzeist_mcp import __version__

    client = FakeClient()
    out = tools.trendzeist_status(client)
    assert client.calls == []  # no network, not even a fake one
    assert out["version"] == __version__
    assert [s["name"] for s in out["sources"]] == ["google_trends", "wikipedia"]
    assert out["cache"]["memory_entries"] == 3
    assert out["settings"]["hl"] == "auto (follows geo)" and out["settings"]["min_interval_s"] == 0
    assert out["settings"]["ttl_s"] == {"explore": 900, "rss": 300, "static": 86400}
    assert out["settings"]["proxies_configured"] == 0 and "guidance" in out


def test_mine_questions_accepts_prefix_subset():
    client = FakeClient()
    out = tools.mine_questions(client, "espresso", limit=100, prefixes=("how to", "vs"))
    assert out["prefixes_queried"] == ["(seed)", "how to", "vs"]
    assert [c[1] for c in client.calls if c[0] == "ac"] == ["espresso", "how to espresso", "espresso vs"]


class AeoClient(FakeClient):
    def related_queries(self, kw, tf, geo, cat, gprop):
        self.calls.append(("rq", kw))
        if kw == self.fail_seed:
            raise TrendsError("rate limited", retryable=True)
        return {
            "top": pd.DataFrame({"query": [f"what is {kw}", f"{kw} machine"], "value": [90, 50]}),
            "rising": pd.DataFrame(
                {"query": [f"how to clean {kw}", f"{kw} vs pod"], "value": [8000, 200]}
            ),
        }


def test_aeo_opportunities_clusters_scores_and_ranks():
    client = AeoClient()
    out = aeo_opportunities(client, ["espresso", "zzz"], geo="BR", limit=3)
    assert out["query"] == {
        "seeds": ["espresso", "zzz"], "timeframe": "today 3-m", "trend_timeframe": "today 12-m",
        "geo": "BR", "hl": "pt-BR", "news_geo": "BR", "wikipedia_lang": "pt",
    }
    assert client.iot_timeframes == ["today 12-m"]
    # 1 IOT, then per seed: rq, seed + 5 prefixes of autocomplete, news, wiki search (+ views when found)
    kinds = [c[0] for c in client.calls]
    assert kinds[0] == "iot" and kinds.count("rq") == 2 and kinds.count("news") == 2
    assert kinds.count("ac") == 2 * (1 + len(_AEO_PREFIXES))
    assert kinds.count("wiki_search") == 2 and kinds.count("wiki_views") == 1  # 'zzz' has no article

    ops = out["opportunities"]
    assert len(ops) == 3 and out["counts"]["opportunities"] > 3 and out["counts"]["returned"] == 3
    assert [o["score"] for o in ops] == sorted((o["score"] for o in ops), reverse=True)
    top = ops[0]
    assert set(top) >= {
        "seed", "angle", "score", "score_breakdown", "questions", "question_count",
        "evidence_from", "trend", "news", "wikipedia", "citability_hints", "brief",
    }
    assert sum(top["score_breakdown"].values()) == top["score"]
    assert top["citability_hints"] == fmt.citability_hints(top["angle"])
    # cross-seed / cross-source dedupe by normalised form
    all_qs = [q.lower() for o in ops for q in o["questions"]]
    assert len(all_qs) == len(set(all_qs))
    # seed-level context is attached to every cluster of that seed
    espresso = [o for o in ops if o["seed"] == "espresso"]
    assert espresso and espresso[0]["news"]["top_publishers"][0] == "The Manual"
    assert espresso[0]["wikipedia"] == {
        "has_article": True, "title": "Espresso", "direction": "rising", "daily_mean": 255.0,
    }
    zzz = [s for s in out["seeds"] if s["seed"] == "zzz"][0]
    assert zzz["wikipedia"] == {"has_article": False, "title": None}
    assert out["errors"] == [] and "partial" not in out and "note" not in out
    assert aeo_opportunities(AeoClient(), ["x"], limit=99)["note"].startswith("limit 99")


def test_aeo_opportunities_trend_block_short_series_explains_null_growth():
    out = aeo_opportunities(AeoClient(), ["espresso"])
    trend = out["opportunities"][0]["trend"]
    assert trend["direction"] == "rising" and trend["direction_now"] == "stable"
    assert trend["growth_3m"] is None and "6 months" in trend["growth_note"]
    assert out["opportunities"][0]["score_breakdown"]["trend"] in (10, 25)  # stable now


def test_aeo_opportunities_scores_recent_slope_not_whole_window():
    """10.1: born-then-collapsed topic (openclaw shape) gets no 'rising' points."""

    class Peaked(AeoClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            self.calls.append(("iot", tuple(keywords)))
            self.iot_timeframes.append(tf)
            idx = pd.date_range("2025-10-05", periods=52, freq="W")
            vals = [0] * 17 + [50, 80, 100, 90, 70, 50, 40, 30, 20, 15, 10, 8, 6, 5] + [4] * 21
            return pd.DataFrame({kw: vals for kw in keywords}, index=idx)

    out = aeo_opportunities(Peaked(), ["espresso"])
    trend = out["opportunities"][0]["trend"]
    assert trend["direction"] == "new" and trend["direction_now"] == "falling"
    assert isinstance(trend["growth_3m"], float) and trend["growth_3m"] < 0
    assert "growth_note" not in trend
    assert "fell" in trend["insight"] and "(falling)" in trend["insight"]
    for op in out["opportunities"]:
        assert op["score_breakdown"]["trend"] in (0, 15)  # only the breakout bonus survives


def test_aeo_opportunities_iot_429_does_not_skip_seed_requests():
    class IotFails(AeoClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            raise TrendsError("429", retryable=True)

    client = IotFails()
    out = aeo_opportunities(client, ["espresso"])
    kinds = [c[0] for c in client.calls]
    assert kinds.count("rq") == 1 and "ac" in kinds and "news" in kinds
    assert out["errors"] == [{"step": "interest_over_time", "error": "429"}]
    assert "partial" not in out and "skipped" not in out["seeds"][0]
    assert out["opportunities"][0]["trend"] == {"available": False}


def test_aeo_opportunities_google_cooldown_keeps_wikipedia():
    client = AeoClient(fail_seed="espresso")  # related_queries 429 on the first seed
    out = aeo_opportunities(client, ["espresso", "latte"])
    kinds = [c[0] for c in client.calls]
    assert kinds.count("rq") == 1 and "ac" not in kinds and "news" not in kinds
    assert kinds.count("wiki_search") == 2  # own lane: never skipped
    assert out["errors"][0]["step"] == "related_queries:espresso"
    assert "skipped" in out["seeds"][1] and "partial" in out
    assert all(o["news"] is None for o in out["opportunities"])


def test_aeo_related_wikipedia_hit_is_not_a_citation_or_confirmed_gap():
    class Related(AeoClient):
        def search(self, topic, lang):
            self.calls.append(("wiki_search", topic, lang))
            return {"found": True, "title": "Coffee", "pageid": 1, "wordcount": 500}

    client = Related()
    out = aeo_opportunities(client, ["espresso"])
    wiki = {"has_article": False, "title": None, "related_article": "Coffee"}
    assert out["seeds"][0]["wikipedia"] == wiki
    assert all(op["wikipedia"] == wiki and op["score_breakdown"]["wikipedia"] == 0
               for op in out["opportunities"])
    assert all("verify relevance" in op["brief"] for op in out["opportunities"])
    assert not any(call[0] == "wiki_views" for call in client.calls)


def test_aeo_opportunities_source_failures_are_per_seed():
    class Flaky(AeoClient):
        def articles(self, query, geo):
            raise TrendsError("news broke", retryable=False)

        def search(self, topic, lang):
            raise TrendsError("wiki broke", retryable=False)

    out = aeo_opportunities(Flaky(), ["espresso"])
    steps = {e["step"] for e in out["errors"]}
    assert steps == {"news:espresso", "wikipedia:espresso"}
    assert out["opportunities"] and out["opportunities"][0]["news"] is None
    assert out["opportunities"][0]["wikipedia"] is None and "partial" not in out


def test_prompts_render_with_angle_specific_evidence():
    res = asyncio.run(
        server.server.get_prompt("answer_brief", {"question": "espresso vs drip coffee", "geo": "US"})
    )
    text = res.messages[0].content.text
    assert "angle: comparison" in text and "a side-by-side data table" in text
    assert "news_coverage" in text and "wiki_attention" in text and "40-60 words" in text
    assert "never invent statistics" in text

    res = asyncio.run(server.server.get_prompt("content_brief", {"topic": "home espresso"}))
    text = res.messages[0].content.text
    assert "discover_topics" in text and "mine_questions" in text and "interest_by_region" in text
    assert "news_coverage" in text and "wiki_attention" in text and "general readers" in text
    assert "resolution='COUNTRY'" in text
