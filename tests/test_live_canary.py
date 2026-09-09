"""Live canary: verifies Google's unofficial endpoints still answer.

Excluded from the default run (see ``addopts`` in pyproject.toml). Run with
``pytest -m live`` locally or on the scheduled CI job. Failures here mean
upstream changed, not that the code regressed.
"""

import pytest

from trendzeist_mcp import tools
from trendzeist_mcp.client import Settings, TrendsClient
from trendzeist_mcp.discovery import discover_topics

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    return TrendsClient(Settings(min_interval=3.0), cache_dir=None)


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
