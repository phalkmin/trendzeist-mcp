from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from trendzeist_mcp import tools
from trendzeist_mcp.aeo import aeo_opportunities
from trendzeist_mcp.client import Settings, TrendsError
from trendzeist_mcp.discovery import discover_topics

from test_tools import AeoClient, FakeClient
from test_tools_050 import ArchiveClient, _seed_archive


def test_flattened_keywords_are_not_archived_as_readable_trends():
    archive = Mock()
    hub = SimpleNamespace(archive=archive)
    summary = {
        "latte": {"available": True, "direction_now": "rising"},
        "espresso": {"available": True, "direction_now": "no_interest"},
    }
    warnings = [{"type": "flattened", "shared_by": ["espresso"]}]

    tools._record_trend_summaries(hub, summary, "", "today 12-m", 0, "", warnings)

    archive.record_trend.assert_called_once_with(
        "latte", "", "today 12-m", summary["latte"]
    )


def test_low_value_off_topic_top_query_cannot_vouch_for_spam():
    class SpamClient(FakeClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": pd.DataFrame(
                    {"query": ["espresso machine", "abraham quiros villalba"], "value": [100, 1]}
                ),
                "rising": pd.DataFrame(
                    {
                        "query": [
                            "alpha espresso abraham quiros villalba",
                            "beta abraham quiros villalba espresso",
                            "abraham quiros villalba gamma espresso",
                        ],
                        "value": [9000, 9000, 9000],
                    }
                ),
            }

    out = discover_topics(SpamClient(), ["espresso"])

    assert [topic["topic"] for topic in out["topics"]] == ["espresso machine"]
    assert len(out["suspect"]) == 3
    assert all("espresso machine" != item["topic"] for item in out["suspect"])


def test_mixed_fresh_and_stale_questions_archive_only_fresh_observations():
    class Stats:
        def __init__(self):
            self.count = 0

        def fresh(self):
            return self.count

    class Autocomplete:
        def __init__(self, stats):
            self.stats = stats
            self.calls = 0

        def suggest(self, query, geo):
            self.calls += 1
            if self.calls == 1:
                self.stats.count += 1
                return ["how to make espresso"]
            return ["how to espresso go bad"]

    stats = Stats()
    archive = Mock()
    hub = SimpleNamespace(
        settings=Settings(min_interval=0),
        stats=stats,
        archive=archive,
        autocomplete=Autocomplete(stats),
    )

    out = tools.mine_questions(hub, "espresso", prefixes=("how to",))

    assert len(out["questions"]) == 2
    archived = archive.record_questions.call_args.args[2]
    assert [q["question"] for q in archived] == ["how to make espresso"]


def test_trending_filter_reads_headlines_beyond_display_limit():
    class RssClient(FakeClient):
        def trending_rss(self, geo, arts):
            articles = [
                {"title": "Coffee recall expands", "source": "News", "url": "https://example.test"},
                {"title": "Second headline", "source": "News", "url": "https://example.test/2"},
            ]
            return [
                {
                    "title": "Acme",
                    "traffic": "20K+",
                    "pub_date": "x",
                    "articles": articles[:arts],
                }
            ]

    out = tools.trending_now(RssClient(), "US", max_articles=0, topic_filter="coffee")

    assert out["count"] == 1
    assert "articles" not in out["trends"][0]


def test_localised_trends_questions_feed_discovery_and_aeo():
    class LocalClient(AeoClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": pd.DataFrame({"query": ["o que é espresso"], "value": [100]}),
                "rising": pd.DataFrame({"query": ["como fazer espresso"], "value": [9000]}),
            }

    discovery = discover_topics(LocalClient(), ["espresso"], geo="BR")
    aeo = aeo_opportunities(LocalClient(), ["espresso"], geo="BR")

    assert {q["question"] for q in discovery["questions"]} == {
        "o que é espresso",
        "como fazer espresso",
    }
    angles = {topic["topic"]: topic["angle"] for topic in discovery["topics"]}
    assert angles["o que é espresso"] == "definition"
    assert angles["como fazer espresso"] == "how-to"

    definition = next(
        topic for topic in discovery["topics"] if topic["topic"] == "o que é espresso"
    )
    assert definition["score_breakdown"]["aeo"]["points"] == 10
    assert definition["score_breakdown"]["aeo"]["reason"].startswith("question-shaped")

    assert any(
        "como fazer espresso" in opportunity["questions"]
        for opportunity in aeo["opportunities"]
    )


def test_related_queries_uses_localised_question_angles():
    class LocalClient(FakeClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": pd.DataFrame({"query": ["o que é espresso"], "value": [100]}),
                "rising": None,
            }

    out = tools.related_queries(LocalClient(), "espresso", geo="BR")

    assert out["questions"][0]["angle"] == "definition"
    assert out["top"][0]["angle"] == "definition"


def test_aeo_surfaces_unsupported_language_fallback():
    out = aeo_opportunities(AeoClient(), ["espresso"], geo="JP")

    assert "English set was used" in out["language_note"]


def test_empty_snapshots_do_not_end_breakout_runs():
    snapshots = [
        {"id": "b1", "observed_at": "2026-08-01", "item_count": 1},
        {"id": "empty", "observed_at": "2026-08-02", "item_count": 0},
        {"id": "b2", "observed_at": "2026-09-01", "item_count": 1},
        {"id": "other", "observed_at": "2026-09-02", "item_count": 1},
        {"id": "b3", "observed_at": "2026-10-01", "item_count": 1},
    ]
    rows = [
        {"snapshot_id": "b1", "status": "breakout"},
        {"snapshot_id": "b2", "status": "breakout"},
        {"snapshot_id": "b3", "status": "breakout"},
    ]

    assert tools._pair_episode_runs(rows, snapshots) == [
        ("2026-08-01", "2026-09-01"),
        ("2026-10-01", "2026-10-01"),
    ]


def test_what_changed_preserves_topics_shared_with_another_seed(tmp_path):
    class SharedClient(ArchiveClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            rising = ["deepseek harness"] if kw == "n8n" else []
            return {
                "top": pd.DataFrame({"query": [f"{kw} machine"], "value": [100]}),
                "rising": pd.DataFrame({"query": rising, "value": [9000] * len(rising)}),
            }

    client = SharedClient(tmp_path)
    out = tools.what_changed(client, ["n8n", "deepseek"])

    n8n = next(seed for seed in out["seeds"] if seed["seed"] == "n8n")
    assert [item["topic"] for item in n8n["first_seen"]] == ["deepseek harness"]


def test_first_run_reapplies_spam_and_off_topic_filters(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(
        client.archive,
        "espresso",
        "",
        0,
        top=["espresso machine"],
        rising=[("how to bake a cake", "breakout"), ("abrahamquirosvillalba .com", "breakout")],
    )

    def fail(*args):
        raise TrendsError("unavailable")

    client.interest_over_time = fail
    client.related_queries = fail

    out = tools.what_changed(client, ["espresso"])
    seed = out["seeds"][0]

    assert seed["first_run"] is True
    assert seed["first_seen"] == []
    assert seed["first_run_filtered"] == 2


def test_aeo_serves_cached_related_queries_during_initial_cooldown():
    class CachedClient(AeoClient):
        def interest_over_time(self, *args):
            raise TrendsError("429", retryable=True, rate_limited=True)

        def has_cached_related_queries(self, kw, tf, geo, cat, gprop):
            return True

    client = CachedClient()
    out = aeo_opportunities(client, ["espresso"])
    kinds = [call[0] for call in client.calls]

    assert kinds.count("rq") == 1
    assert "ac" not in kinds and "news" not in kinds
    assert "wiki_search" in kinds
    assert out["opportunities"]
    assert "skipped" not in out["seeds"][0]


def test_initial_transport_failure_is_not_reported_as_rate_limiting():
    class TransportClient(AeoClient):
        def interest_over_time(self, *args):
            raise TrendsError("connection failed", retryable=True)

    discovery = discover_topics(TransportClient(), ["espresso"])
    aeo = aeo_opportunities(TransportClient(), ["espresso"])

    assert "skipped" not in discovery["seeds"][0]
    assert discovery["topics"]
    assert "skipped" not in aeo["seeds"][0]
    assert "partial" not in aeo
    assert aeo["opportunities"]


def test_nonempty_fully_filtered_snapshot_separates_breakout_runs():
    snapshots = [
        {"id": "b1", "observed_at": "2026-08-01", "item_count": 1, "source_item_count": 1},
        {"id": "filtered", "observed_at": "2026-08-08", "item_count": 0, "source_item_count": 2},
        {"id": "b2", "observed_at": "2026-09-01", "item_count": 1, "source_item_count": 1},
    ]
    rows = [
        {"snapshot_id": "b1", "status": "breakout"},
        {"snapshot_id": "b2", "status": "breakout"},
    ]

    assert tools._pair_episode_runs(rows, snapshots) == [
        ("2026-08-01", "2026-08-01"),
        ("2026-09-01", "2026-09-01"),
    ]


def test_discovery_skip_message_uses_actual_retry_after():
    class RetryClient(FakeClient):
        def interest_over_time(self, *args):
            raise TrendsError("429", retryable=True, rate_limited=True, retry_after=120)

        def retry_after(self):
            return 120

    out = discover_topics(RetryClient(), ["espresso"])

    assert "~120 s" in out["seeds"][0]["skipped"]


def test_malformed_current_rows_abstain_instead_of_inventing_drops(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "espresso", "", 10, top=["espresso machine"], rising=[])
    tools.what_changed(client, ["espresso"])
    latest, _ = client.archive.latest_and_baseline("espresso", "", "related", "today 3-m", 7)
    conn = client.archive._connect()
    conn.execute(
        "UPDATE observations SET rank='bad' WHERE snapshot_id=? AND item='espresso machine'",
        (latest["id"],),
    )
    conn.commit()

    out = tools._seed_changes(client, client.archive, "espresso", "", 7)

    assert "incomplete" in out["reason"]
    assert "dropped" not in out
    assert client.archive.call_error() is not None


def test_related_queries_schema_documents_every_parameter_and_alternatives():
    import asyncio

    from trendzeist_mcp import server

    registered = asyncio.run(server.server.list_tools())
    tool = next(item for item in registered if item.name == "related_queries")
    properties = tool.input_schema["properties"]

    assert set(properties) == {"keyword", "timeframe", "geo", "category", "gprop", "limit"}
    assert all(prop.get("description") for prop in properties.values())
    assert properties["timeframe"]["default"] == "today 3-m"
    assert "1-50" in properties["limit"]["description"]
    for alternative in ("related_topics", "suggest_keywords", "mine_questions", "discover_topics"):
        assert alternative in tool.description


def test_aeo_budget_includes_entity_news_and_wikipedia_requests():
    out = aeo_opportunities(AeoClient(), ["espresso", "latte"])

    assert "23 upstream requests" in out["budget_note"]


def test_mcp_non_finite_interest_response_is_valid_json(monkeypatch):
    import asyncio
    import json

    from trendzeist_mcp import server

    class NonFiniteClient(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            return pd.DataFrame({keywords[0]: [None, float("nan"), float("inf"), 5.0]})

    monkeypatch.setattr(server, "_hub", NonFiniteClient())
    res = asyncio.run(server.server.call_tool("interest_over_time", {"keywords": ["espresso"]}))

    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    json.dumps(payload, allow_nan=False)
    assert payload["summary"]["espresso"]["mean"] == 5.0


def test_what_changed_abstains_when_legacy_filtering_empties_baseline(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(
        client.archive,
        "espresso",
        "",
        10,
        top=[],
        rising=[("how to bake a cake", "breakout"), ("abrahamquirosvillalba .com", "breakout")],
    )

    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]

    assert "baseline snapshot" in seed["reason"]
    assert seed["baseline_filtered"] == 2
    assert "new_breakouts" not in seed


def test_what_changed_abstains_when_current_filtering_removes_everything(tmp_path):
    class JunkClient(ArchiveClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            return {
                "top": None,
                "rising": pd.DataFrame(
                    {
                        "query": ["how to bake a cake", "abrahamquirosvillalba .com"],
                        "value": [9000, 9000],
                    }
                ),
            }

    client = JunkClient(tmp_path)
    _seed_archive(
        client.archive,
        "espresso",
        "",
        10,
        top=["espresso machine"],
        rising=[("espresso tonic", "rising")],
    )

    seed = tools.what_changed(client, ["espresso"], since_days=7)["seeds"][0]

    assert "current snapshot" in seed["reason"]
    assert "dropped" not in seed


def test_duplicate_questions_merge_breakout_evidence_in_both_seed_orders():
    class SharedQuestionClient(AeoClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            value = 9000 if kw == "latte" else 100
            return {
                "top": None,
                "rising": pd.DataFrame({"query": ["how to brew espresso"], "value": [value]}),
            }

    for seeds in (["espresso", "latte"], ["latte", "espresso"]):
        discovered = discover_topics(SharedQuestionClient(), seeds)
        assert discovered["questions"][0]["is_breakout"] is True

        out = aeo_opportunities(SharedQuestionClient(), seeds)
        opportunity = next(
            item for item in out["opportunities"] if "how to brew espresso" in item["questions"]
        )
        assert opportunity["score_breakdown"]["momentum"]["points"] == 18
        assert "breakout" in opportunity["score_breakdown"]["momentum"]["reason"]


def test_history_readers_reapply_legacy_spam_and_off_topic_filters(tmp_path):
    client = ArchiveClient(tmp_path)
    junk = [("how to bake a cake", "breakout"), ("abrahamquirosvillalba .com", "breakout")]
    for days_ago in (40, 30, 20):
        _seed_archive(client.archive, "espresso", "", days_ago, top=["espresso machine"], rising=junk)

    for topic, _ in junk:
        history = tools.trend_history(client, topic, days=90)
        assert history["breakout_episodes"] == 0
        assert history["breakout_observations"] == 0
        assert history["seen_under_seeds"] == []

    recurring = tools.recurring_topics(client, days=90, min_episodes=3)
    assert recurring["topics"] == []


def test_later_transport_failures_stop_without_rate_limit_claims():
    class TransportClient(AeoClient):
        def related_queries(self, kw, tf, geo, cat, gprop):
            if kw == "espresso":
                raise TrendsError("connection failed", retryable=True)
            return super().related_queries(kw, tf, geo, cat, gprop)

    out = aeo_opportunities(TransportClient(), ["espresso", "latte"])

    assert "A Google request failed" in out["partial"]
    assert "rate limiting" not in out["partial"]
    assert "upstream request failed" in out["seeds"][1]["skipped"]
    assert "Wikipedia data is complete" in out["partial"]

    discovery = discover_topics(TransportClient(), ["espresso", "latte"])
    assert "request failed" in discovery["seeds"][1]["skipped"]
    assert "rate limiting" not in discovery["seeds"][1]["skipped"]


def test_autocomplete_transport_failure_has_accurate_partial_reason():
    class TransportClient(AeoClient):
        def suggest(self, query, geo=""):
            raise TrendsError("connection failed", retryable=True)

        def search(self, topic, lang):
            raise TrendsError("Wikipedia unavailable")

    mined = tools.mine_questions(TransportClient(), "espresso")
    assert mined["partial_reason"] == "transport"
    assert "request failed" in mined["partial"]

    out = aeo_opportunities(TransportClient(), ["espresso"])
    assert "A Google request failed" in out["partial"]
    assert "Some Wikipedia requests also failed" in out["partial"]


def test_related_429_without_cooldown_does_not_skip_later_seeds():
    class NoCooldownClient(AeoClient):
        def retry_after(self):
            return None

    client = NoCooldownClient(fail_seed="espresso")
    out = aeo_opportunities(client, ["espresso", "latte"])

    assert "partial" not in out
    assert "skipped" not in out["seeds"][1]
    assert ("rq", "latte") in client.calls


def test_mine_questions_rejects_ambiguous_local_one_word_midquery_match():
    class LocalClient(FakeClient):
        def suggest(self, query, geo=""):
            if query.strip() == "espresso":
                return ["espresso como bebida", "espresso vale a pena"]
            return []

    out = tools.mine_questions(LocalClient(), "espresso", geo="BR", limit=5)

    assert [q["question"] for q in out["questions"]] == ["espresso vale a pena"]
    assert out["questions"][0]["angle"] == "verdict"


def test_mine_questions_allows_an_empty_custom_prefix_set():
    out = tools.mine_questions(FakeClient(), "espresso", prefixes=())

    assert out["prefixes_queried"] == ["(seed)"]
    assert out["questions"]


def test_compare_channels_and_what_changed_distinguish_transport_failures(tmp_path):
    class TransportClient(ArchiveClient):
        def interest_over_time(self, *args):
            raise TrendsError("connection failed", retryable=True)

        def related_queries(self, *args):
            raise TrendsError("connection failed", retryable=True)

    client = TransportClient(tmp_path)
    channels = tools.compare_channels(client, "espresso")
    assert "request failed" in channels["partial"]
    assert "rate limiting" not in channels["partial"]

    changed = tools.what_changed(client, ["espresso"])
    assert changed["partial"] == (
        "A Google request failed this call; comparisons use the newest stored snapshot."
    )


def test_compare_channels_uses_actual_retry_delay():
    class RetryClient(FakeClient):
        def interest_over_time(self, *args):
            raise TrendsError("429", retryable=True, rate_limited=True)

        def retry_after(self):
            return 120

    out = tools.compare_channels(RetryClient(), "espresso")

    assert "~120 s" in out["partial"]
