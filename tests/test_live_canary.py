"""Live canary: verifies Google's unofficial endpoints and the Wikimedia API still answer.

Excluded from the default run (see ``addopts`` in pyproject.toml). Run with
``pytest -m live`` locally or on the scheduled CI job. Failures here mean
upstream changed, not that the code regressed.
"""

import pytest

from trendzeist_mcp import tools
from trendzeist_mcp.client import Settings
from trendzeist_mcp.discovery import discover_topics
from trendzeist_mcp.sources import Hub

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    # Honour TRENDZEIST_* env (the CI job sets MIN_INTERVAL), but never below 3 s.
    settings = Settings.from_env()
    settings.min_interval = max(settings.min_interval, 3.0)
    return Hub(settings, cache_dir=None)


def test_interest_over_time_live(client):
    out = tools.interest_over_time(client, ["espresso"], "today 3-m", "US")
    assert out["summary"]["espresso"]["available"] is True
    assert len(out["points"]) >= 8


def test_related_queries_live(client):
    out = tools.related_queries(client, "espresso", "today 3-m", "US")
    assert out["available"] is True
    assert out["top"], "Google returned no top related queries"


def test_trending_now_live(client):
    out = tools.trending_now(client, "US", 1)
    assert out["count"] > 0 and out["trends"][0]["title"]


def test_categories_and_suggestions_live(client):
    assert tools.list_categories(client, "coffee")["total_matches"] >= 1
    assert tools.suggest_keywords(client, "coffee")["suggestions"]


def test_discover_topics_live(client):
    out = discover_topics(client, ["espresso"], geo="US")
    assert out["errors"] == []
    assert out["topics"], "discover_topics produced no candidates"


def test_mine_questions_live(client):
    # Autocomplete is a separate unofficial endpoint; keep the probe cheap (3 requests).
    out = tools.mine_questions(client, "espresso", geo="US", limit=5)
    assert out["errors"] == [], out["errors"]
    assert out["count"] > 0, "Autocomplete returned no question-shaped suggestions"
    assert out["questions"][0]["angle"] in {"how-to", "comparison", "listicle", "definition", "news"}


def test_news_coverage_live(client):
    out = tools.news_coverage(client, "espresso machine", geo="US", limit=3)
    assert out["available"] is True, out.get("reason")
    assert out["publishers"] and out["recency"]["newest"]


def test_wiki_attention_live(client):
    out = tools.wiki_attention(client, "espresso machine", lang="en", days=14)
    assert out["has_article"] is True
    assert out["pageviews"]["days"] >= 7, out["pageviews"]


def test_status_live(client):
    out = tools.trendzeist_status(client)
    assert {s["name"] for s in out["sources"]} == {
        "google_trends", "google_autocomplete", "google_news", "wikipedia"
    }
