"""0.6.0: compare_keywords interpretation (E2), ambiguity flag (E1), recurring_topics (E4)."""

from unittest.mock import patch

import pandas as pd
import pytest

from trendzeist_mcp import formatters as fmt
from trendzeist_mcp import tools
from trendzeist_mcp.aeo import aeo_opportunities
from trendzeist_mcp.client import TrendsError
from trendzeist_mcp.discovery import discover_topics
from trendzeist_mcp.validation import ValidationError

from test_tools import AeoClient, FakeClient
from test_tools_050 import ArchiveClient, _seed_archive, _ts


# ---- E2: compare_keywords interpretation ------------------------------------


def _row(kw, share, now, mean=None):
    return {"keyword": kw, "mean_interest": share if mean is None else mean,
            "share_pct": share, "direction_now": now}


def test_interpretation_roles_in_precedence_order():
    ranking = [
        _row("lead", 50.0, "stable"),
        _row("riser", 20.0, "rising"),
        _row("born", 12.0, "new"),        # 'new' counts as rising
        _row("small riser", 4.0, "rising"),  # rising but < 10% share -> niche
        _row("faller", 9.0, "falling"),
        _row("mid", 5.0, "stable"),
    ]
    out = fmt.compare_interpretation(ranking)
    roles = {r["keyword"]: r["role"] for r in ranking}
    assert roles == {
        "lead": "leader", "riser": "rising challenger", "born": "rising challenger",
        "small riser": "niche", "faller": "declining", "mid": "steady",
    }
    assert all(r["content_implication"] for r in ranking)
    assert out["winner"] == {"keyword": "lead", "reason": "highest interest and not falling"}
    s = out["seo_interpretation"]
    assert s.startswith("Target 'lead' (leader, stable)")
    assert "'riser', 'born' are rising" in s and "'faller' is declining" in s and s.endswith(".")


def test_interpretation_falling_leader_loses_to_rising_challenger():
    ranking = [_row("old", 60.0, "falling"), _row("new kid", 30.0, "rising"), _row("x", 10.0, "stable")]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"]["keyword"] == "new kid"
    assert out["winner"]["reason"] == (
        "old is bigger but falling; new kid is rising — target it and mention old for context"
    )
    assert ranking[0]["content_implication"].endswith(
        "Interest is falling; check whether a rising term is replacing it."
    )
    assert "'old' leads but is falling" in out["seo_interpretation"]


def test_interpretation_falling_leader_without_alternative():
    ranking = [_row("old", 70.0, "falling"), _row("other", 30.0, "stable")]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"] == {
        "keyword": "old", "reason": "highest interest, though falling; no rising alternative in this set",
    }


def test_interpretation_empty():
    assert fmt.compare_interpretation([]) == {"winner": None, "seo_interpretation": None}


def test_interpretation_all_zero_interest_has_no_winner():
    """A tie at zero is not evidence: no target, and the answer must not depend on input order."""
    for order in (("a", "b"), ("b", "a")):
        ranking = [_row(k, 0.0, "no_interest", mean=0) for k in order]
        out = fmt.compare_interpretation(ranking)
        assert out["winner"] is None and out["seo_interpretation"] is None
        assert "no keyword shows measurable search interest" in out["winner_reason"]
        assert all(r["role"] == "no interest" and "not a target" in r["content_implication"] for r in ranking)


def test_interpretation_unknown_recent_trend_is_not_called_stable():
    ranking = [_row("a", 50.0, "insufficient_data"), _row("b", 50.0, "insufficient_data")]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"] == {"keyword": "a", "reason": "highest interest; recent trend unknown (too few points)"}
    assert out["seo_interpretation"].startswith("Target 'a' (leader, trend unknown)")
    assert all(r["content_implication"].endswith("verify with a longer timeframe.") for r in ranking)
    # a positive-data control keeps the plain wording
    assert fmt.compare_interpretation([_row("a", 60.0, "stable"), _row("b", 40.0, "stable")])["winner"][
        "reason"
    ] == "highest interest and not falling"


def test_compare_keywords_zero_series_has_no_winner_and_growth_note():
    class ZeroClient(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2025-10-01", periods=52, freq="W")
            return pd.DataFrame({kw: [0] * 52 for kw in keywords}, index=idx)

    out = tools.compare_keywords(ZeroClient(), ["a", "b"])
    assert out["winner"] is None and out["leader"] == "a"  # legacy field unchanged
    assert "winner_reason" in out
    short = tools.compare_keywords(FakeClient(), ["a", "b"], timeframe="today 3-m")
    assert all(r["growth_3m"] is None for r in short["ranking"])
    assert short["growth_note"] == fmt.GROWTH_NOTE


def test_compare_keywords_adds_interpretation_and_keeps_old_fields():
    out = tools.compare_keywords(FakeClient(), ["a", "b"])
    assert out["leader"] == "b" and out["winner"]["keyword"] == "b"
    top, second = out["ranking"]
    assert {"keyword", "mean_interest", "share_pct", "direction", "direction_now", "latest"} <= set(top)
    assert top["role"] == "leader" and second["role"] == "steady"
    assert "growth_3m" in top  # None on the 12-week fake series
    assert out["seo_interpretation"].startswith("Target 'b'")


def test_compare_keywords_empty_data_has_null_winner():
    class EmptyClient(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            return pd.DataFrame()

    out = tools.compare_keywords(EmptyClient(), ["a", "b"])
    assert out["winner"] is None and out["seo_interpretation"] is None


# ---- E1: ambiguity flag ----------------------------------------------------------

_SUGGESTIONS = {
    "apple": [
        {"mid": "/m/0k8z", "title": "Apple", "type": "Technology company"},
        {"mid": "/m/014j1m", "title": "Apple", "type": "Fruit"},
        {"mid": "/m/0x", "title": "Apple Music", "type": "Music streaming service"},
    ],
    "mercury": [  # same type twice -> not ambiguous
        {"mid": "/m/1", "title": "Mercury", "type": "Planet"},
        {"mid": "/m/2", "title": "Mercury", "type": "Planet"},
    ],
    "partial": [
        {"title": "Partial", "type": ""},
        {"title": "Partial"},
        {"mid": "/m/3", "type": "Thing"},
    ],
}


class _Ambiguous:
    def suggestions(self, kw):
        self.calls.append(("sugg", kw))
        if kw == "boom":
            raise TrendsError("rate limited", retryable=True)
        return _SUGGESTIONS.get(kw.lower(), [{"mid": "/m/9", "title": kw.title(), "type": "Topic"}])


class AmbiguousClient(_Ambiguous, FakeClient):
    pass


class AmbiguousAeoClient(_Ambiguous, AeoClient):
    pass


def test_entity_ambiguity_rule():
    c = AmbiguousClient()
    found = tools.entity_ambiguity(c, "Apple")  # case-insensitive exact title
    assert [e["type"] for e in found] == ["Technology company", "Fruit"]
    assert found[0] == {"title": "Apple", "type": "Technology company", "mid": "/m/0k8z"}
    assert tools.entity_ambiguity(c, "mercury") == []
    assert tools.entity_ambiguity(c, "n8n") == []


def test_entity_ambiguity_ignores_partial_entries():
    assert tools.entity_ambiguity(AmbiguousClient(), "partial") == []


def test_mid_seed_skips_suggestions():
    c = AmbiguousClient()
    assert tools.entity_ambiguity(c, "/m/0k8z") is None
    assert tools.entity_ambiguity(c, "/g/11b6abc") is None
    assert not [x for x in c.calls if x[0] == "sugg"]


def test_discover_topics_flags_ambiguous_seed():
    out = discover_topics(AmbiguousClient(), ["apple", "espresso"])
    apple, espresso = out["seeds"]
    assert [e["mid"] for e in apple["ambiguous"]] == ["/m/0k8z", "/m/014j1m"]
    assert apple["clarify"].startswith("'apple' matches several entities.")
    assert "'/m/0k8z'" in apple["clarify"]
    assert "ambiguous" not in espresso and "clarify" not in espresso
    assert out["ambiguous_seeds"] == ["apple"]
    assert apple["rising_count"] > 0  # data still fetched for the plain string


def test_discover_topics_no_ambiguity_field_when_clear():
    out = discover_topics(AmbiguousClient(), ["espresso"])
    assert "ambiguous_seeds" not in out


def test_discover_topics_suggestions_failure_is_enrichment():
    c = AmbiguousClient()
    out = discover_topics(c, ["boom", "espresso"])
    assert any(e["step"] == "entities:boom" for e in out["errors"])
    assert out["seeds"][0]["rising_count"] > 0  # seed still processed
    assert ("sugg", "espresso") in c.calls  # not cooling down after an entities failure


def test_discover_topics_skips_check_when_cooling():
    c = AmbiguousClient(fail_seed="espresso")
    discover_topics(c, ["espresso", "apple"])
    assert ("sugg", "apple") not in c.calls


def test_aeo_opportunities_flags_ambiguous_seed_without_mid_rerun_advice():
    out = aeo_opportunities(AmbiguousAeoClient(), ["apple", "espresso"])
    apple = next(s for s in out["seeds"] if s["seed"] == "apple")
    assert apple["ambiguous"] and out["ambiguous_seeds"] == ["apple"]
    # News / Wikipedia / Autocomplete take text, so AEO must not suggest a mid as the seed.
    assert "more specific phrase" in apple["clarify"] and "'apple technology company'" in apple["clarify"]
    assert "only works for Google Trends" in apple["clarify"]
    assert "re-run with that entity's mid" not in apple["clarify"]


@pytest.mark.parametrize(
    "payload",
    [[None], [{"title": 42, "type": "Topic"}], [{"title": "Apple", "type": []}, {"title": "Apple", "type": {}}], "nope"],
)
def test_malformed_suggestions_are_an_enrichment_error_not_a_crash(payload):
    class Bad(FakeClient):
        def suggestions(self, kw):
            return payload

    class BadAeo(AeoClient):
        def suggestions(self, kw):
            return payload

    out = discover_topics(Bad(), ["apple"])
    assert any(e["step"] == "entities:apple" and "Unexpected response" in e["error"] for e in out["errors"])
    assert out["seeds"][0]["rising_count"] > 0  # the seed itself was still fetched
    out = aeo_opportunities(BadAeo(), ["apple"])
    assert any(e["step"] == "entities:apple" for e in out["errors"]) and out["opportunities"]


def test_malformed_suggestions_through_server_wrapper_is_not_a_crash():
    from trendzeist_mcp import server

    class Bad(FakeClient):
        def suggestions(self, kw):
            return [{"title": "Apple", "type": {"x": 1}}]

    with patch.object(server, "get_hub", return_value=Bad()):
        out = server._tool(discover_topics)(["apple"])  # enrichment failure: still a result
    assert out["schema_version"] and any(e["step"] == "entities:apple" for e in out["errors"])


def test_aeo_opportunities_skips_check_when_cooling():
    c = AmbiguousAeoClient(fail_seed="espresso")
    out = aeo_opportunities(c, ["espresso", "apple"])
    assert ("sugg", "apple") not in c.calls and "ambiguous_seeds" not in out


# ---- E4: shared episode counter ----------------------------------------------------


def test_trend_history_no_longer_double_counts_across_seeds(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "coffee", "", 5, top=[], rising=[("cold brew", "breakout")])
    _seed_archive(client.archive, "iced coffee", "", 4, top=[], rising=[("cold brew", "breakout")])
    assert tools.trend_history(client, "cold brew")["breakout_episodes"] == 1


def test_episodes_under_two_seeds_weeks_apart_count_twice(tmp_path):
    client = ArchiveClient(tmp_path)
    _seed_archive(client.archive, "coffee", "", 30, top=[], rising=[("cold brew", "breakout")])
    _seed_archive(client.archive, "iced coffee", "", 9, top=[], rising=[("cold brew", "breakout")])
    assert tools.trend_history(client, "cold brew")["breakout_episodes"] == 2


def test_episode_merge_tolerates_bad_timestamp():
    snaps = {("s", "tf"): [{"id": "1", "observed_at": "garbage"}, {"id": "2", "observed_at": _ts(1)}]}
    rows = [
        {"seed": "s", "timeframe": "tf", "kind": "rising_query", "snapshot_id": "1", "status": "breakout"},
        {"seed": "s", "timeframe": "tf", "kind": "rising_query", "snapshot_id": "2", "status": "rising"},
    ]
    starts = tools._episode_starts(rows, lambda s, tf: snaps[(s, tf)])
    assert starts == ["garbage"]


def test_items_without_breakouts_never_touch_snapshot_headers():
    """Review F2: ordinary rising items must not trigger a per-item scan of every snapshot."""
    calls = []

    def snapshots_for(seed, tf):
        calls.append((seed, tf))
        return []

    rows = [{"seed": "s", "timeframe": "tf", "kind": "rising_query", "snapshot_id": "1", "status": "rising"}]
    assert tools._episode_starts(rows, snapshots_for) == [] and calls == []


def test_same_second_snapshots_keep_insertion_order(tmp_path):
    """Review F9: empty then breakout written in the same second must read in that order."""
    c = ArchiveClient(tmp_path)

    def rec(ts, breakout):
        with patch("trendzeist_mcp.archive._now", return_value=ts):
            c.archive.record_related(
                "coffee", "", "today 3-m", [],
                [{"name": "cold brew", "value": 9000, "is_breakout": True}] if breakout else [],
            )

    rec(_ts(40), True)
    tie = _ts(35)
    rec(tie, False)
    rec(tie, True)  # restart 5 days after day-40 start: merged -> still 1 episode so far
    rec(_ts(20), True)  # continuous since the tie (no empty snapshot in between): same run
    assert tools.trend_history(c, "cold brew")["breakout_episodes"] == 1
    assert tools.recurring_topics(c, min_episodes=2)["topics"] == []


def test_overlapping_runs_under_new_seeds_are_one_episode(tmp_path):
    """Review F8: adding seeds to a live breakout must not manufacture recurrence."""
    c = ArchiveClient(tmp_path)
    for d in (50, 40, 30):
        _seed_archive(c.archive, "coffee", "", d, top=[], rising=[("cold brew", "breakout")])
    _seed_archive(c.archive, "tea", "", 40, top=[], rising=[("cold brew", "breakout")])
    _seed_archive(c.archive, "matcha", "", 30, top=[], rising=[("cold brew", "breakout")])
    assert tools.recurring_topics(c, min_episodes=2)["topics"] == []
    assert tools.trend_history(c, "cold brew")["breakout_episodes"] == 1
    # ... but a genuinely separate run (gap > 7 days after the last breakout) still counts.
    _seed_archive(c.archive, "coffee", "", 29, top=[], rising=[])
    _seed_archive(c.archive, "tea", "", 29, top=[], rising=[])
    _seed_archive(c.archive, "matcha", "", 29, top=[], rising=[])
    _seed_archive(c.archive, "tea", "", 10, top=[], rising=[("cold brew", "breakout")])
    assert tools.trend_history(c, "cold brew")["breakout_episodes"] == 2


def test_self_related_rows_are_excluded_consistently(tmp_path):
    """Review F5: a seed listed as its own rising query is not a recurring topic in either tool."""
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "coffee", [80, 50, 20])
    assert tools.recurring_topics(c)["topics"] == []
    assert tools.trend_history(c, "coffee")["breakout_episodes"] == 0


def test_future_rows_are_not_history(tmp_path):
    """Review F4: future-stamped rows (clock fix, imported data) never count; coverage >= 0."""
    c = ArchiveClient(tmp_path)
    for age, rising in ((-10, [("cold brew", "breakout")]), (-11, []), (-30, [("cold brew", "breakout")])):
        _seed_archive(c.archive, "coffee", "", age, top=[], rising=rising)
    out = tools.recurring_topics(c, min_episodes=2)
    assert out["topics"] == [] and "reason" in out and out.get("coverage_days", 0) >= 0
    assert tools.trend_history(c, "cold brew")["available"] is False


def test_malformed_stored_timestamp_type_is_reported_not_raised(tmp_path):
    """Review F3: a BLOB in a TEXT column is skipped and surfaces as archive_error."""
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [30, 10])
    conn = c.archive._connect()
    # A BLOB timestamp sorts above every TEXT value, so the [cutoff, now] bounds drop
    # it in SQL: the 'run ends' snapshots vanish and the two breakouts merge into one
    # continuous run. No crash, and the result is still JSON-native.
    conn.execute(
        "UPDATE snapshots SET observed_at=? WHERE snapshot_id IN "
        "(SELECT snapshot_id FROM observations WHERE kind='top_query')",
        (b"garbage",),
    )
    conn.commit()
    c.archive.begin_call()
    assert tools.recurring_topics(c, min_episodes=2)["topics"] == []
    assert tools.trend_history(c, "cold brew")["breakout_episodes"] == 1
    # A BLOB in another text column reaches Python: skipped and reported, never raised.
    conn.execute("UPDATE observations SET item=? WHERE status='breakout' AND rank=1", (b"\x00\xff",))
    conn.commit()
    c.archive.begin_call()
    out = tools.recurring_topics(c, min_episodes=2)
    assert out["topics"] == [] and "malformed stored text" in c.archive.call_error()
    assert "malformed stored text" in c.call_meta()["archive_error"]


# ---- E4: recurring_topics ----------------------------------------------------------


def _episodes(archive, seed, item, days_list):
    """A breakout of ``item`` under ``seed`` at each of ``days_list``, separated by gaps."""
    for d in days_list:
        _seed_archive(archive, seed, "", d, top=[], rising=[(item, "breakout")])
        _seed_archive(archive, seed, "", d - 1, top=[item], rising=[])  # run ends


def test_recurring_topics_lists_items_with_enough_episodes(tmp_path):
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [80, 50, 20])
    _episodes(c.archive, "coffee", "espresso tonic", [40, 10])
    _seed_archive(c.archive, "tea", "", 60, top=[], rising=[("cold brew", "breakout")])  # 4th, under tea
    out = tools.recurring_topics(c)
    assert [t["topic"] for t in out["topics"]] == ["cold brew"]
    t = out["topics"][0]
    assert t["episodes"] == 4 and t["seeds"] == ["coffee", "tea"]
    assert t["first_breakout"] < t["last_breakout"] and t["action"].startswith("durable page")
    assert out["query"] == {"geo": "worldwide", "days": 90, "min_episodes": 3, "seeds": None}
    assert "Recurring is not the same as big" in out["guidance"] and c.calls.count(("rq", "coffee")) == 0
    two = tools.recurring_topics(c, min_episodes=2)
    assert [t["topic"] for t in two["topics"]] == ["cold brew", "espresso tonic"]  # episodes desc


def test_recurring_topics_merges_same_week_across_seeds(tmp_path):
    c = ArchiveClient(tmp_path)
    for seed, d in (("coffee", 30), ("iced coffee", 28), ("coffee", 10)):
        _seed_archive(c.archive, seed, "", d, top=[], rising=[("cold brew", "breakout")])
        _seed_archive(c.archive, seed, "", d - 1, top=[], rising=[])
    out = tools.recurring_topics(c, min_episodes=2)
    assert out["topics"][0]["episodes"] == 2


def test_recurring_topics_agrees_with_trend_history(tmp_path):
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [80, 50, 20])
    _seed_archive(c.archive, "tea", "", 49, top=[], rising=[("cold brew", "breakout")])
    rt = tools.recurring_topics(c, min_episodes=2)["topics"][0]["episodes"]
    assert rt == tools.trend_history(c, "cold brew")["breakout_episodes"] == 3


def test_recurring_topics_seed_filter_is_normalised(tmp_path):
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [80, 50, 20])
    _episodes(c.archive, "tea", "matcha", [70, 40, 15])
    out = tools.recurring_topics(c, seeds=["Coffee ", "COFFEE"])
    assert [t["topic"] for t in out["topics"]] == ["cold brew"]
    assert out["query"]["seeds"] == ["Coffee"]


def test_recurring_topics_coverage_note_and_limit(tmp_path):
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [30, 20, 10])
    _episodes(c.archive, "coffee", "matcha", [29, 19, 9])
    out = tools.recurring_topics(c, limit=1)
    assert out["coverage_days"] == 30 and "only covers 30 of the requested 90 days" in out["note"]
    assert len(out["topics"]) == 1 and out["topics"][0]["topic"] == "matcha"  # same count, more recent


def test_recurring_topics_empty_and_disabled(tmp_path):
    c = ArchiveClient(tmp_path)
    out = tools.recurring_topics(c)
    assert out["topics"] == [] and "what_changed" in out["reason"]
    _episodes(c.archive, "coffee", "cold brew", [20])
    out = tools.recurring_topics(c)
    assert out["topics"] == [] and "min_episodes=2" in out["reason"]
    off = FakeClient()  # no archive attribute
    assert tools.recurring_topics(off)["reason"] == tools._ARCHIVE_OFF_REASON


def test_recurring_topics_validation(tmp_path):
    c = ArchiveClient(tmp_path)
    with pytest.raises(ValidationError, match="one breakout is not a recurrence"):
        tools.recurring_topics(c, min_episodes=1)
    with pytest.raises(ValidationError):
        tools.recurring_topics(c, min_episodes="3")
    out = tools.recurring_topics(c, min_episodes=50, limit=500)
    assert out["query"]["min_episodes"] == 10
    assert "min_episodes 50 exceeds" in out["note"] and "limit 500 exceeds" in out["note"]


# ---- 0.6.0 review fixes ---------------------------------------------------------------


def test_recurring_topics_no_coverage_note_when_archive_is_older_than_window(tmp_path):
    c = ArchiveClient(tmp_path)
    _seed_archive(c.archive, "coffee", "", 200, top=["coffee maker"], rising=[])  # archive is old
    _episodes(c.archive, "coffee", "cold brew", [80, 50, 20])
    out = tools.recurring_topics(c)
    assert out["coverage_days"] == 90 and "note" not in out


def test_recurring_topics_empty_seed_list_means_no_filter(tmp_path):
    c = ArchiveClient(tmp_path)
    _episodes(c.archive, "coffee", "cold brew", [80, 50, 20])
    out = tools.recurring_topics(c, seeds=[])
    assert out["query"]["seeds"] is None and [t["topic"] for t in out["topics"]] == ["cold brew"]


def test_restart_within_merge_window_under_one_seed_is_one_episode(tmp_path):
    """Pinned: the merge covers every start, same seed included (a 3-day flicker is noise)."""
    c = ArchiveClient(tmp_path)
    _seed_archive(c.archive, "coffee", "", 10, top=[], rising=[("cold brew", "breakout")])
    _seed_archive(c.archive, "coffee", "", 9, top=[], rising=[("cold brew", "rising")])
    _seed_archive(c.archive, "coffee", "", 7, top=[], rising=[("cold brew", "breakout")])
    assert tools.trend_history(c, "cold brew")["breakout_episodes"] == 1


# ---- 0.6.0 review round 2 (2026-10-23) ----------------------------------------------


_NULL_TYPE_FIRST = [
    {"title": "Jaguar", "type": None, "mid": "/m/1"},
    {"title": "Jaguar", "type": "Animal", "mid": "/m/2"},
    {"title": "Jaguar", "type": "Car brand", "mid": "/m/3"},
]


class _NullTypeFirst:
    def suggestions(self, kw):
        return _NULL_TYPE_FIRST


class NullTypeAeoClient(_NullTypeFirst, AeoClient):
    pass


class NullTypeClient(_NullTypeFirst, FakeClient):
    pass


def test_aeo_ambiguity_with_null_first_type_is_not_a_crash():
    """G1: Google may leave ``type`` null on the first exact-title entity; the AEO hint
    must use the first *named* type rather than crash the composite."""
    out = aeo_opportunities(NullTypeAeoClient(), ["jaguar"])
    jag = out["seeds"][0]
    assert out["ambiguous_seeds"] == ["jaguar"] and jag["ambiguous"] == _NULL_TYPE_FIRST
    assert "'jaguar animal'" in jag["clarify"] and "only works for Google Trends" in jag["clarify"]
    assert out["opportunities"]  # the seed's own requests still ran


def test_aeo_ambiguity_with_no_named_type_example_is_omitted():
    class NoTypes(AeoClient):
        def suggestions(self, kw):
            return [{"title": "X", "type": None, "mid": "/m/1"}, {"title": "X", "type": "A", "mid": None},
                    {"title": "X", "type": "B", "mid": None}]

    rep, errs = {}, []
    assert tools.flag_ambiguity(NoTypes(), "x", rep, errs, mid_rerun=False)
    assert rep["clarify"].endswith("(e.g. 'x a'). A mid like '/m/1' only works for Google Trends; this tool "
                                   "also searches News, Wikipedia and Autocomplete with the seed text.")

    class Bare(AeoClient):
        def suggestions(self, kw):
            return [{"title": "X", "type": None, "mid": None}, {"title": "X", "type": "A", "mid": None},
                    {"title": "X", "type": "B", "mid": None}]

    rep, errs = {}, []
    assert tools.flag_ambiguity(Bare(), "x", rep, errs, mid_rerun=False)
    assert rep["clarify"].endswith("more specific phrase (e.g. 'x a').")


def test_null_first_type_through_server_wrapper():
    from trendzeist_mcp import server

    with patch.object(server, "get_hub", return_value=NullTypeAeoClient()):
        out = server._tool(aeo_opportunities)(["jaguar"])
    assert out["ambiguous_seeds"] == ["jaguar"]
    with patch.object(server, "get_hub", return_value=NullTypeClient()):
        out = server._tool(discover_topics)(["jaguar"])
    assert "(e.g. '/m/1')" in out["seeds"][0]["clarify"]


def test_interpretation_dead_leader_is_not_called_not_falling():
    """G2: ``no_interest`` (zero in both recent quarters) is worse than falling."""
    ranking = [_row("old", 70.0, "no_interest"), _row("other", 30.0, "stable")]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"] == {
        "keyword": "old", "reason": "highest interest, though dead now; no rising alternative in this set",
    }
    assert "No interest in the most recent weeks" in ranking[0]["content_implication"]
    assert out["seo_interpretation"].startswith("Target 'old' (leader, no recent interest)")


def test_interpretation_dead_leader_loses_to_rising_challenger():
    ranking = [_row("old", 60.0, "no_interest"), _row("new kid", 40.0, "rising")]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"]["keyword"] == "new kid"
    assert out["winner"]["reason"].startswith("old is bigger but dead now; new kid is rising")
    assert "'old' leads but has no recent interest" in out["seo_interpretation"]


def test_interpretation_no_interest_non_leader_is_declining_not_niche():
    ranking = [_row("lead", 60.0, "stable"), _row("gone", 40.0, "no_interest")]
    fmt.compare_interpretation(ranking)
    assert ranking[1]["role"] == "declining"


def test_interpretation_noise_floor_qualifies_winner():
    """G5: means below 5 everywhere -> the winner is still named but flagged as weak evidence."""
    ranking = [_row("a", 60.0, "falling", mean=3), _row("b", 40.0, "rising", mean=2)]
    out = fmt.compare_interpretation(ranking)
    assert out["winner"]["keyword"] == "b" and "near the noise floor" in out["winner"]["reason"]
    ranking = [_row("a", 60.0, "stable", mean=30), _row("b", 40.0, "rising", mean=2)]
    assert "noise floor" not in fmt.compare_interpretation(ranking)["winner"]["reason"]


def test_compare_keywords_peaked_then_dead_series():
    """Integration: a term big early in the window and dead for six months must not be
    recommended as the H1 term with 'not falling'."""

    class Peaked(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2025-10-05", periods=52, freq="W")
            a = [100] * 20 + [0] * 32
            b = [0] * 20 + list(range(1, 33))
            return pd.DataFrame({"a": a, "b": b, "isPartial": [False] * 52}, index=idx)

    out = tools.compare_keywords(Peaked(), ["a", "b"])
    roles = {r["keyword"]: r["role"] for r in out["ranking"]}
    assert roles["a"] == "leader" and out["ranking"][0]["direction_now"] == "no_interest"
    assert roles["b"] == "rising challenger" and out["winner"]["keyword"] == "b"
    assert "not falling" not in out["winner"]["reason"]
    assert out["leader"] == "a"  # legacy field unchanged



# ---- 0.6.0 review round 3 (2026-10-23) ----------------------------------------------


def test_ambiguity_hl_note_for_non_english_geo_when_nothing_found():
    """H1: the suggestions endpoint answers in the server hl, so a 'not ambiguous' answer
    for a BR seed is weak; say so. Nothing for an English geo, nothing when a match was found."""
    c = AmbiguousClient()
    out = discover_topics(c, ["café"], geo="BR")
    assert out["seeds"][0]["ambiguity_note"].startswith("Entity ambiguity was checked in the server language (en-US)")
    assert "TRENDZEIST_HL" in out["seeds"][0]["ambiguity_note"]
    out = discover_topics(AmbiguousClient(), ["café"], geo="US")
    assert "ambiguity_note" not in out["seeds"][0]
    out = discover_topics(AmbiguousClient(), ["apple"], geo="BR")
    assert out["seeds"][0]["ambiguous"] and "ambiguity_note" not in out["seeds"][0]
    out = aeo_opportunities(AmbiguousAeoClient(), ["café"], geo="BR")
    assert "ambiguity_note" in out["seeds"][0]
    # A mid seed never consults suggestions and gets no note either.
    c = AmbiguousClient()
    out = discover_topics(c, ["/m/0k8z"], geo="BR")
    assert ("sugg", "/m/0k8z") not in c.calls and "ambiguity_note" not in out["seeds"][0]


def test_ambiguity_skip_while_cooling_is_noted():
    """H3: a seed report that lost its 'ambiguous' field because the call was rate
    limited says so instead of silently reading as 'not ambiguous'."""
    class Cached(AmbiguousClient):
        def has_cached_related_queries(self, kw, tf, geo, cat, gprop):
            return kw == "apple"  # served from cache while cooling, so it is still processed

    c = Cached(fail_seed="espresso")
    out = discover_topics(c, ["espresso", "apple"])
    apple = next(s for s in out["seeds"] if s["seed"] == "apple")
    assert "skipped" not in apple and apple["rising_count"] > 0
    assert apple["ambiguity_note"] == tools.AMBIGUITY_SKIPPED_NOTE and "ambiguous" not in apple
    # An uncached seed is skipped wholesale and says so; no separate ambiguity note.
    out = discover_topics(AmbiguousClient(fail_seed="espresso"), ["espresso", "apple"])
    apple = next(s for s in out["seeds"] if s["seed"] == "apple")
    assert "skipped" in apple and "ambiguity_note" not in apple
    c = AmbiguousAeoClient(fail_seed="espresso")
    out = aeo_opportunities(c, ["espresso", "apple"])
    apple = next(s for s in out["seeds"] if s["seed"] == "apple")
    assert apple["ambiguity_note"] == tools.AMBIGUITY_SKIPPED_NOTE


def test_compare_keywords_all_unavailable_has_winner_reason():
    """H2: the only null-winner case without an explanation."""

    class Empty(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            return pd.DataFrame()

    out = tools.compare_keywords(Empty(), ["a", "b"])
    assert out["winner"] is None and out["leader"] is None and out["unavailable"] == ["a", "b"]
    assert "no keyword returned data" in out["winner_reason"]


def test_compare_keywords_one_horse_race_is_labelled():
    """H5: one keyword with data + one unavailable is not a comparison."""

    class OneOnly(FakeClient):
        def interest_over_time(self, keywords, tf, geo, cat, gprop):
            idx = pd.date_range("2025-10-05", periods=52, freq="W")
            return pd.DataFrame({"a": [50] * 52, "isPartial": [False] * 52}, index=idx)

    out = tools.compare_keywords(OneOnly(), ["a", "b"])
    assert out["winner"]["keyword"] == "a" and "only term with data (1 unavailable)" in out["winner"]["reason"]
    assert out["seo_interpretation"].endswith("the only term with data (1 unavailable), not a comparison.")
    # Two real rows: no such suffix.
    assert "only term" not in tools.compare_keywords(FakeClient(), ["a", "b"])["winner"]["reason"]


def test_weekly_review_prompt_mentions_recurring_topics_and_ambiguity():
    from trendzeist_mcp import server

    text = server.weekly_review("coffee, tea", geo="BR")
    assert "recurring_topics(geo='BR', days=90)" in text and "'ambiguous'" in text

