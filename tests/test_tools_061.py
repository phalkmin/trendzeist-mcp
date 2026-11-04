"""0.6.1: localised question prefixes (issue #3) and rate-limit surfacing (issue #4)."""

from trendzeist_mcp import tools
from trendzeist_mcp.client import Settings

from test_tools import FakeClient


class PolishClient(FakeClient):
    """Autocomplete fake for a Polish market: completions follow the Polish prefixes."""

    def suggest(self, query, geo=""):
        self.raw_autocomplete_queries.append(query)
        q = query.strip()
        self.calls.append(("ac", q, geo))
        if q == "wakacje składkowe":  # bare seed: Google puts the question word *inside*
            return [
                "wakacje składkowe jak złożyć wniosek",
                "wakacje składkowe do kiedy",
                "wakacje składkowe zus",
            ]
        if q == "jak wakacje składkowe":
            return ["jak złożyć wniosek o wakacje składkowe", "jak wakacje składkowe"]
        if q == "ile wakacje składkowe":
            return ["ile wynosi wakacje składkowe"]
        return []


def test_mine_questions_uses_polish_prefixes_for_pl_geo():
    client = PolishClient()
    out = tools.mine_questions(client, "wakacje składkowe", geo="PL", limit=20)
    assert out["query"]["hl"] == "pl-PL"
    assert out["prefix_language"] == "pl"
    queried = [c[1] for c in client.calls if c[0] == "ac"]
    assert queried[0] == "wakacje składkowe"
    for word in ("jak", "kiedy", "gdzie", "ile", "czy"):
        assert f"{word} wakacje składkowe" in queried
    assert not any(q.startswith("how to") for q in queried)
    assert queried[-1] == "wakacje składkowe vs"  # the comparison probe is universal
    texts = [q["question"] for q in out["questions"]]
    assert "jak złożyć wniosek o wakacje składkowe" in texts
    assert "ile wynosi wakacje składkowe" in texts
    # Unprefixed seed completions that carry a question word are kept (the reporter's
    # fallback suggestion); plain completions are not.
    assert "wakacje składkowe jak złożyć wniosek" in texts
    assert "wakacje składkowe do kiedy" in texts
    assert "wakacje składkowe zus" not in texts
    # Prefix + seed with nothing added is padding in any language.
    assert [f["question"] for f in out["filtered_out"]] == ["jak wakacje składkowe"]
    assert "language_note" not in out
    jak = next(q for q in out["questions"] if q["question"].startswith("jak złożyć"))
    assert jak["prefix"] == "jak" and jak["angle"] == "how-to"


def test_mine_questions_falls_back_to_english_for_unlisted_language():
    client = FakeClient()
    out = tools.mine_questions(client, "espresso", geo="JP", limit=6)
    assert out["query"]["hl"] == "ja-JP"
    assert out["prefix_language"] == "en"
    assert client.calls[1] == ("ac", "how to espresso", "JP")
    assert "ja-JP" in out["language_note"] and "English" in out["language_note"]


class PortugueseClient(FakeClient):
    def suggest(self, query, geo=""):
        q = query.strip()
        self.calls.append(("ac", q, geo))
        return {
            "o que é espresso": ["o que é espresso ristretto"],
            "como espresso": ["como fazer espresso em casa"],
            "espresso vs": ["espresso vs cappuccino"],
        }.get(q, [])


def test_mine_questions_localised_prefixes_carry_their_angle():
    out = tools.mine_questions(PortugueseClient(), "espresso", geo="BR", limit=20)
    by_text = {q["question"]: q for q in out["questions"]}
    assert by_text["o que é espresso ristretto"]["angle"] == "definition"
    assert by_text["como fazer espresso em casa"]["angle"] == "how-to"
    assert by_text["espresso vs cappuccino"]["angle"] == "comparison"
    assert out["by_angle"] == {"definition": 1, "how-to": 1, "comparison": 1}


def test_mine_questions_explicit_prefixes_override_locale():
    client = FakeClient()
    out = tools.mine_questions(client, "espresso", geo="BR", limit=100, prefixes=("how to",))
    assert out["prefixes_queried"] == ["(seed)", "how to"]
    assert out["prefix_language"] == "custom"


def test_question_prefixes_cover_major_markets():
    for hl in ("pt-BR", "es-ES", "fr-FR", "de-DE", "it-IT", "pl-PL", "nl-NL"):
        lang, prefixes = tools.question_prefixes(hl)
        assert lang == hl[:2] and len(prefixes) >= 8 and prefixes[-1] == "vs"
    assert tools.question_prefixes("en-GB") == ("en", tools._QUESTION_PREFIXES)
    assert tools.question_prefixes("ko-KR") == ("en", tools._QUESTION_PREFIXES)


class StaleMetaClient(FakeClient):
    def call_meta(self):
        return {
            "requests_made": 0,
            "cache_hit": False,
            "cache_hits": 0,
            "cache_misses": 0,
            "stale_hits": 1,
            "stale_age_s": 2 * 86400 + 3600,
        }


def test_finalize_surfaces_stale_fallback_at_top_level():
    out = tools.finalize(StaleMetaClient(), {"x": 1})
    assert out["_meta"]["stale_hits"] == 1
    assert out["stale"]["age_days"] == 2.0
    assert "rate limit" in out["stale"]["note"] and "stale" in out["stale"]["note"]
    assert "stale" not in tools.finalize(FakeClient(), {"x": 1})


class CoolingMetaClient(FakeClient):
    def call_meta(self):
        return {
            "requests_made": 1,
            "cache_hit": False,
            "cache_hits": 0,
            "cache_misses": 1,
            "rate_limited": [{"source": "google_trends", "retry_after_s": 42}],
        }


def test_finalize_keeps_rate_limited_meta():
    out = tools.finalize(CoolingMetaClient(), {"x": 1})
    assert out["_meta"]["rate_limited"] == [{"source": "google_trends", "retry_after_s": 42}]


def test_status_reports_cooldown_pacing_and_stale_settings():
    client = FakeClient()
    client.settings = Settings(min_interval=0, cooldown=90, jitter=0.25, stale_fallback=False)
    st = tools.trendzeist_status(client)["settings"]
    assert st["cooldown_s"] == 90 and st["jitter"] == 0.25 and st["stale_fallback"] is False


# ------------------------------------------------------------------ review round (0.6.1)
def test_aeo_opportunities_mines_questions_in_the_market_language():
    from test_tools import AeoClient
    from trendzeist_mcp.aeo import aeo_opportunities

    client = AeoClient()
    aeo_opportunities(client, ["espresso"], geo="PL")
    queried = [c[1] for c in client.calls if c[0] == "ac"]
    assert queried and not any(q.startswith("how to") for q in queried)
    assert "jak espresso" in queried and queried[-1] == "espresso vs"
    assert len(queried) == 1 + 5  # same request budget as the English composite

    client = AeoClient()
    aeo_opportunities(client, ["espresso"], geo="US")
    assert "how to espresso" in [c[1] for c in client.calls if c[0] == "ac"]


def test_site_coverage_jaccard_cannot_exceed_one():
    from trendzeist_mcp import formatters as fmt

    # Three of four candidate tokens match the *two* site tokens ("codex" is a 4-letter
    # prefix match of "code"); the old arithmetic made that Jaccard 1.0 -> "partial".
    assert fmt.site_coverage("claude code vs codex", ["claude code"]) == ("gap", None)
    assert fmt.site_coverage("claude code mac", ["claude code macos"]) == ("covered", "claude code macos")
    assert fmt.site_coverage("latte art tips", ["latte art"]) == ("partial", "latte art")


class BoundaryClient(FakeClient):
    def suggest(self, query, geo=""):
        q = query.strip()
        self.calls.append(("ac", q, geo))
        return {
            "is espresso": ["israel espresso import", "is espresso bad for you"],
            "can espresso": ["canada espresso brands", "can espresso go bad"],
        }.get(q, [])


def test_prefix_match_requires_a_word_boundary():
    out = tools.mine_questions(BoundaryClient(), "espresso", geo="US", limit=50)
    texts = [q["question"] for q in out["questions"]]
    assert "is espresso bad for you" in texts and "can espresso go bad" in texts
    assert "israel espresso import" not in texts and "canada espresso brands" not in texts


class SpanishClient(FakeClient):
    def suggest(self, query, geo=""):
        q = query.strip()
        self.calls.append(("ac", q, geo))
        return {
            # Google often answers an accented prefix with unaccented completions.
            "cómo espresso": ["como hacer espresso en casa", "cómo hacer espresso en casa"],
            "qué es espresso": ["que es espresso ristretto"],
            "espresso": ["espresso donde comprar"],
        }.get(q, [])


def test_localised_prefixes_match_unaccented_completions():
    out = tools.mine_questions(SpanishClient(), "espresso", geo="ES", limit=50)
    by_text = {q["question"]: q for q in out["questions"]}
    assert by_text["como hacer espresso en casa"]["angle"] == "how-to"
    assert "cómo hacer espresso en casa" not in by_text  # accent-insensitive dedupe
    assert by_text["que es espresso ristretto"]["angle"] == "definition"
    assert by_text["espresso donde comprar"]["prefix"] is None  # "dónde" matched without the accent
