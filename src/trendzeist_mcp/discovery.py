"""Composite topic-discovery workflow built on the primitive tools."""

from __future__ import annotations

from typing import Any

from . import formatters as fmt
from . import validation as v
from .client import TrendsClient, TrendsError

_SIGNAL_ORDER = {"breakout": 0, "rising": 1, "evergreen": 2}


def _momentum(signal: str, value: float | int | None) -> float:
    """Single sortable score: breakouts first, then growth %, then popularity."""
    val = float(value or 0)
    if signal == "breakout":
        return 1_000_000 + val
    if signal == "rising":
        return 10_000 + val
    return val


def discover_topics(
    client: TrendsClient,
    seed_keywords: list[str],
    geo: str = "",
    timeframe: str = "today 3-m",
    category: int = 0,
    gprop: str = "",
    max_per_seed: int = 15,
) -> dict[str, Any]:
    """For each seed, gather rising/top related queries plus trend direction.

    Returns a de-duplicated, ranked list of candidate blog topics. Partial
    failures (e.g. rate limits on a later seed) are reported per seed rather
    than failing the whole call.
    """
    seeds = v.validate_keywords(seed_keywords)
    tf, g, cat, gp = (
        v.validate_timeframe(timeframe),
        v.validate_geo(geo),
        v.validate_category(category),
        v.validate_gprop(gprop),
    )
    per_seed = v.validate_limit(max_per_seed, default=15, maximum=50)

    candidates: dict[str, dict[str, Any]] = {}
    seed_reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # One interest_over_time call covers up to 5 seeds -> cheap trend context.
    directions: dict[str, Any] = {}
    try:
        iot = fmt.interest_over_time(client.interest_over_time(seeds, tf, g, cat, gp), seeds)
        directions = iot["summary"]
    except TrendsError as exc:
        errors.append({"step": "interest_over_time", "error": str(exc)})

    for seed in seeds:
        report: dict[str, Any] = {"seed": seed, "trend": directions.get(seed, {"available": False})}
        try:
            res = client.related_queries(seed, tf, g, cat, gp)
        except TrendsError as exc:
            report["error"] = str(exc)
            errors.append({"step": f"related_queries:{seed}", "error": str(exc)})
            seed_reports.append(report)
            if exc.retryable:
                # Stop hammering Google once it starts refusing.
                break
            continue

        rising = fmt.mark_breakouts(fmt.related_list(res.get("rising"), per_seed, label="query"))
        top = fmt.related_list(res.get("top"), per_seed, label="query")
        report["rising_count"] = len(rising)
        report["top_count"] = len(top)
        if not rising and not top:
            report["hint"] = (
                "No related queries: search volume is too low for this seed. Try a "
                "broader term (use suggest_keywords), a wider geo, or a longer timeframe."
            )
        seed_reports.append(report)

        for item in rising:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            signal = "breakout" if item.get("is_breakout") else "rising"
            cand = {
                "topic": name,
                "signal": signal,
                "growth_pct": item.get("growth_pct"),
                "popularity": None,
                "source_seeds": [seed],
                "momentum": _momentum(signal, item.get("growth_pct")),
            }
            _merge(candidates, cand)

        for item in top:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            cand = {
                "topic": name,
                "signal": "evergreen",
                "growth_pct": None,
                "popularity": item.get("value"),
                "source_seeds": [seed],
                "momentum": _momentum("evergreen", item.get("value")),
            }
            _merge(candidates, cand)

    ranked = sorted(
        candidates.values(),
        key=lambda c: (_SIGNAL_ORDER[c["signal"]], -c["momentum"], c["topic"]),
    )
    for c in ranked:
        c.pop("momentum", None)

    return {
        "query": {
            "seed_keywords": seeds,
            "timeframe": tf,
            "geo": g or "worldwide",
            "category": cat,
            "gprop": gp or "web",
        },
        "seeds": seed_reports,
        "topics": ranked,
        "counts": {
            "breakout": sum(1 for c in ranked if c["signal"] == "breakout"),
            "rising": sum(1 for c in ranked if c["signal"] == "rising"),
            "evergreen": sum(1 for c in ranked if c["signal"] == "evergreen"),
        },
        "errors": errors,
        "guidance": (
            "breakout = brand-new/exploding demand (time-sensitive, low competition); "
            "rising = growing interest (good near-term posts); evergreen = consistently "
            "popular (pillar content). Cross-check finalists with interest_over_time."
        ),
    }


def _merge(candidates: dict[str, dict[str, Any]], cand: dict[str, Any]) -> None:
    """Merge a candidate into the pool, keeping the strongest signal."""
    key = cand["topic"].lower()
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = cand
        return
    for s in cand["source_seeds"]:
        if s not in existing["source_seeds"]:
            existing["source_seeds"].append(s)
    new_rank, old_rank = _SIGNAL_ORDER[cand["signal"]], _SIGNAL_ORDER[existing["signal"]]
    stronger_signal = new_rank < old_rank
    same_signal_stronger = new_rank == old_rank and cand["momentum"] > existing["momentum"]
    if stronger_signal or same_signal_stronger:
        # Result must not depend on seed order: always keep the strongest evidence.
        existing.update(signal=cand["signal"], momentum=cand["momentum"])
        if cand["growth_pct"] is not None:
            existing["growth_pct"] = cand["growth_pct"]
    if cand["popularity"] is not None:
        existing["popularity"] = max(cand["popularity"], existing.get("popularity") or 0)
    if cand["growth_pct"] is not None and existing.get("growth_pct") is None:
        existing["growth_pct"] = cand["growth_pct"]
