import asyncio
import json

import pandas as pd
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from trendzeist_mcp import server, tools
from trendzeist_mcp.client import TrendsError
from trendzeist_mcp.discovery import discover_topics


class FakeClient:
    """Stand-in for TrendsClient that never touches the network."""

    def __init__(self, fail_seed: str | None = None):
        self.calls: list[tuple] = []
        self.fail_seed = fail_seed

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
        "category": 0,
        "gprop": "web",
    }
    assert out["summary"]["Espresso"]["direction"] == "rising"


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


def test_discover_topics_partial_failure_stops_on_rate_limit():
    out = discover_topics(FakeClient(fail_seed="espresso"), ["espresso", "latte"])
    assert out["errors"][0]["step"] == "related_queries:espresso"
    assert len(out["seeds"]) == 1  # aborted before hitting Google again
    assert out["topics"] == []


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
    }
    assert [p.name for p in asyncio.run(server.server.list_prompts())] == ["blog_ideas_from_trends"]

    res = asyncio.run(server.server.call_tool("related_queries", {"keyword": "espresso"}))
    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    assert payload["rising"][0]["is_breakout"] is True

    # Validation failures surface as ToolError with an actionable message.
    with pytest.raises(ToolError, match="Invalid argument"):
        asyncio.run(server.server.call_tool("interest_over_time", {"keywords": [], "timeframe": "x"}))
