"""Convert pandas structures into compact, JSON-serialisable dicts.

Tool responses are consumed by an LLM, so payloads are kept small: long
time series are downsampled, floats are rounded and lists are capped.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

MAX_SERIES_POINTS = 60
BREAKOUT_THRESHOLD = 5000  # Google reports "Breakout" as +5000%
SCALE_NOTE = "0-100 relative to the peak across all keywords in this query"

# Title angles (roadmap N6). Order matters: the first matching rule wins, so the
# more specific intents (comparison, definition, release questions) are checked
# before the broad "how-to" catch-all; the generic news rule stays last.
ANGLES: tuple[str, ...] = ("how-to", "comparison", "listicle", "definition", "news")
_ANGLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # "when X released / launched / comes out" is a date question, not a how-to.
        "news",
        re.compile(
            r"^when\b.*\b(release[sd]?|launch(ed|es)?|come[s]?\s+out|came\s+out|announce[sd]?"
            r"|available|drop(ped|s)?)\b",
            re.I,
        ),
    ),
    (
        "comparison",
        re.compile(
            r"\b(vs\.?|versus|compared?\s+to|comparison|difference\s+between|alternatives?\s+to"
            r"|better\s+than|or)\b",
            re.I,
        ),
    ),
    (
        "definition",
        re.compile(
            r"^(what\s+(is|are|does|do)|who\s+(is|are|was))\b|\b(meaning|definition|explained)\b",
            re.I,
        ),
    ),
    (
        "how-to",
        re.compile(
            r"^(how\s+(to|do|does|can|long|much|many|often)|why|when|where|can|should|is|does|do"
            r"|will|would|could)\b|\b(tutorial|guide|steps?|diy|recipe)\b",
            re.I,
        ),
    ),
    (
        "listicle",
        re.compile(r"\b(best|top(\s+\d+)?|ideas|examples|tips|types\s+of|list\s+of|cheap(est)?)\b", re.I),
    ),
    (
        "news",
        re.compile(
            r"\b(news|update[sd]?|release[sd]?|launch(ed|es)?|announce[sd]?|leak(ed|s)?|recall"
            r"|lawsuit|20\d\d)\b",
            re.I,
        ),
    ),
)

# Interrogative prefixes that make a search query a question (roadmap N2).
QUESTION_RE = re.compile(
    r"^(how|why|what|when|where|which|who|whom|whose|can|could|should|would|will|is|are|was"
    r"|were|does|do|did|has|have)\b",
    re.I,
)


def classify_angle(text: str | None) -> str | None:
    """Tag a query/title with a content angle, or ``None`` when no rule matches."""
    if not text:
        return None
    for angle, rule in _ANGLE_RULES:
        if rule.search(text):
            return angle
    return None


def tag_angles(items: list[dict[str, Any]], key: str = "name") -> list[dict[str, Any]]:
    """Add ``angle`` to every item in place (from ``item[key]``)."""
    for it in items:
        it["angle"] = classify_angle(it.get(key))
    return items


def is_question(text: str | None) -> bool:
    return bool(text) and QUESTION_RE.match(text.strip()) is not None


def normalise_query(text: str) -> str:
    """Canonical form for de-duplication: lowercase, no punctuation, single spaces."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def extract_questions(
    *sources: tuple[str, list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Collect question-shaped queries from related lists.

    ``sources`` are ``(label, items)`` pairs in priority order (e.g. rising
    before top). Duplicates are removed by normalised form; the first source
    that mentions a question keeps it.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for source, items in sources:
        for it in items:
            name = (it.get("name") or "").strip()
            if not is_question(name):
                continue
            norm = normalise_query(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            entry: dict[str, Any] = {
                "question": name,
                "angle": classify_angle(name) or "how-to",
                "source": source,
                "value": it.get("value"),
            }
            if it.get("is_breakout"):
                entry["is_breakout"] = True
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


def _fmt_timestamp(ts: pd.Timestamp) -> str:
    """Date-only for daily/weekly series, ISO minute precision for intraday."""
    if ts.hour or ts.minute or ts.second:
        return ts.strftime("%Y-%m-%dT%H:%M")
    return ts.strftime("%Y-%m-%d")


def _to_native(value: Any) -> Any:
    """Convert numpy / pandas scalars to plain Python types."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return _fmt_timestamp(value)
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 2)
    return value


def records(df: pd.DataFrame | None, limit: int | None = None) -> list[dict[str, Any]]:
    """DataFrame -> list of dicts with native types, optionally truncated."""
    if df is None or df.empty:
        return []
    if limit is not None:
        df = df.head(limit)
    out: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        out.append({str(k): _to_native(v) for k, v in row.items()})
    return out


def _downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Reduce a time-indexed frame to at most ``max_points`` rows by mean-bucketing."""
    if len(df) <= max_points:
        return df
    bucket = math.ceil(len(df) / max_points)
    groups = [i // bucket for i in range(len(df))]
    numeric = df.select_dtypes(include="number")
    agg = numeric.groupby(groups).mean().round(1)
    labels = df.index.to_series().groupby(groups).last()
    agg.index = labels.values
    return agg


def _thirds(values: list[float]) -> tuple[float, float]:
    """Means of the first and last third of a series (len >= 3)."""
    third = max(1, len(values) // 3)
    return sum(values[:third]) / third, sum(values[-third:]) / third


def _recent_quarters(values: list[float]) -> tuple[float, float]:
    """Means of the second-to-last and last quarter of a series (len >= 4).

    On a 12-month series this is the same window as ``growth_3m``, so the
    recent label and the growth figure always agree in sign.
    """
    quarter = max(1, len(values) // 4)
    recent = values[-quarter:]
    previous = values[-2 * quarter : -quarter]
    return sum(previous) / len(previous), sum(recent) / len(recent)


def _label(before: float, after: float) -> str:
    """rising / falling / stable from two window means; 'new' when before is zero."""
    if before == 0 and after == 0:
        return "no_interest"
    if before == 0:
        return "new"  # born inside the window; growth from zero is undefined
    change = (after - before) / before
    if change >= 0.25:
        return "rising"
    if change <= -0.25:
        return "falling"
    return "stable"


def _direction(values: list[float]) -> str:
    """Whole-window label: first third vs. last third means.

    ``new`` means the topic had no interest in the first third; use
    :func:`_direction_now` to know whether it is still growing.
    """
    if len(values) < 3:
        return "insufficient_data"
    return _label(*_thirds(values))


def _direction_now(values: list[float]) -> str:
    """Recent-slope label: last quarter vs. the quarter before it.

    Answers "should I write about this *now*": a topic that peaked mid-window and
    has been falling since reads ``falling`` here even when ``_direction`` is
    ``rising`` (or ``new``) for the whole period.
    """
    if len(values) < 4:
        return "insufficient_data"
    return _label(*_recent_quarters(values))


_GROWTH_WINDOWS: dict[str, int] = {"growth_3m": 91, "growth_12m": 365}
GROWTH_NOTE = (
    "growth_3m needs at least 6 months of data (timeframe 'today 12-m'); "
    "growth_12m needs 'today 5-y'. Both compare the mean of the last window with the "
    "window before it."
)


def _pct_change(before: float, after: float) -> float | None:
    if before <= 0:
        return None  # growth from zero is undefined
    return round(100.0 * (after - before) / before, 1)


def _growth_windows(series: pd.Series) -> dict[str, float | None]:
    """% change of the mean over the last N days vs. the N days before that.

    ``None`` when the series does not span the two windows (roadmap C16) or
    when the index is not time-based.
    """
    out: dict[str, float | None] = {name: None for name in _GROWTH_WINDOWS}
    if len(series) < 4 or not isinstance(series.index, pd.DatetimeIndex):
        return out
    end = series.index.max()
    start = series.index.min()
    # Google buckets series (daily / weekly / monthly): each point covers one
    # ``step``, and the earliest bucket may start up to one step late, so the
    # covered span is (end - start + step) with one bucket of tolerance.
    step = pd.Series(series.index).diff().dropna().median()
    covered = end - start + step
    for name, days in _GROWTH_WINDOWS.items():
        window = pd.Timedelta(days=days)
        if covered < 2 * window - step:
            continue
        recent = series[series.index > end - window]
        previous = series[(series.index <= end - window) & (series.index > end - 2 * window)]
        if len(recent) < 2 or len(previous) < 2:
            continue
        out[name] = _pct_change(float(previous.mean()), float(recent.mean()))
    return out


def _growth_note(growth: dict[str, float | None]) -> str | None:
    """Explain null growth fields instead of leaving them silent."""
    return GROWTH_NOTE if any(val is None for val in growth.values()) else None


def _change_phrase(before: float, after: float) -> str:
    change = round(100.0 * (after - before) / before)
    verb = "rose" if change > 0 else "fell" if change < 0 else "held flat"
    return f"{verb} {abs(change)}%" if change else verb


def _insight(
    kw: str,
    vals: list[float],
    direction: str,
    peak: Any,
    peak_date: Any,
    direction_now: str | None = None,
) -> str:
    """One plain-English sentence per keyword so the model needs no arithmetic.

    When the whole-window and recent labels disagree the sentence says so, e.g.
    "rose 182% ... but fell 45% in the last third (falling)".
    """
    if direction == "insufficient_data":
        return f"Too few data points to judge the trend for '{kw}'."
    if direction == "no_interest":
        return f"'{kw}' shows no measurable search interest in this period."
    first, last = _thirds(vals)
    if first > 0:
        head = (
            f"Interest in '{kw}' {_change_phrase(first, last)} between the first and "
            "last third of the period"
        )
    else:
        head = f"Interest in '{kw}' appeared from zero during the period"
    tail = f", peaking at {peak} on {peak_date}" if peak is not None and peak_date else ""
    now = direction_now if direction_now is not None else _direction_now(vals)
    if now in (direction, "insufficient_data", "no_interest", "new"):
        return f"{head}{tail} ({direction})."
    previous, recent = _recent_quarters(vals)
    move = _change_phrase(previous, recent) if previous > 0 else "moved from zero"
    reversal = now == "falling" or (now == "rising" and direction == "falling")
    joiner = "but" if reversal else "and"
    return f"{head}{tail}, {joiner} {move} in the last quarter of the period ({now})."


def interest_over_time(
    df: pd.DataFrame | None, keywords: Iterable[str], max_points: int = MAX_SERIES_POINTS
) -> dict[str, Any]:
    """Shape an interest_over_time frame into points + per-keyword summary."""
    kws = list(keywords)
    if df is None or df.empty:
        return {
            "points": [],
            "summary": {kw: {"available": False} for kw in kws},
            "original_points": 0,
            "downsampled": False,
            "scale": SCALE_NOTE,
            "note": "Google returned no data for this query (too little search volume?).",
        }

    complete = df
    partial_flags = None
    if "isPartial" in df.columns:
        partial_flags = df["isPartial"].astype(bool)
        complete = df.drop(columns=["isPartial"])

    summary: dict[str, Any] = {}
    for kw in kws:
        if kw not in complete.columns:
            summary[kw] = {"available": False}
            continue
        series = complete[kw]
        stats_series = series
        if partial_flags is not None and int((~partial_flags).sum()) >= 3:
            stats_series = series[~partial_flags]
        vals = [float(v) for v in stats_series.tolist()]
        peak_idx = stats_series.idxmax() if len(stats_series) else None
        direction = _direction(vals)
        direction_now = _direction_now(vals)
        peak = _to_native(stats_series.max()) if len(stats_series) else None
        peak_date = _to_native(peak_idx) if peak_idx is not None else None
        growth = _growth_windows(stats_series)
        summary[kw] = {
            "available": True,
            "mean": round(sum(vals) / len(vals), 1) if vals else 0,
            "latest": _to_native(stats_series.iloc[-1]) if len(stats_series) else None,
            "peak": peak,
            "peak_date": peak_date,
            "direction": direction,
            "direction_now": direction_now,
            **growth,
            "insight": _insight(kw, vals, direction, peak, peak_date, direction_now),
        }
        growth_note = _growth_note(growth)
        if growth_note:
            summary[kw]["growth_note"] = growth_note

    sampled = _downsample(complete, max_points)
    points: list[dict[str, Any]] = []
    for idx, row in sampled.iterrows():
        points.append(
            {
                "date": _to_native(idx) if isinstance(idx, pd.Timestamp) else str(idx),
                "values": {str(k): _to_native(v) for k, v in row.items()},
            }
        )

    return {
        "points": points,
        "summary": summary,
        "original_points": int(len(complete)),
        "downsampled": len(sampled) != len(complete),
        "scale": SCALE_NOTE,
    }



def related_list(df: pd.DataFrame | None, limit: int, *, label: str) -> list[dict[str, Any]]:
    """Shape related_queries / related_topics frames.

    ``label`` is the column that carries the name ('query' or 'topic_title').
    """
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for rec in records(df, limit):
        name = rec.get(label) or rec.get("query") or rec.get("topic_title")
        item: dict[str, Any] = {"name": name, "value": rec.get("value")}
        if "topic_type" in rec:
            item["type"] = rec["topic_type"]
        if "topic_mid" in rec:
            item["mid"] = rec["topic_mid"]
        out.append(item)
    return out


def mark_breakouts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag 'Breakout' (>=5000% growth) entries in a rising list."""
    for it in items:
        v = it.get("value")
        it["growth_pct"] = v
        it["is_breakout"] = bool(isinstance(v, (int, float)) and v >= BREAKOUT_THRESHOLD)
    return items


# Suspect rising queries (run-data 10.2). Search-manipulation campaigns attach one
# name / brand to many unrelated queries so each shows up as a "Breakout" under
# popular seeds. Google's number is kept (is_breakout), but composites keep these
# out of the breakout bucket, the question lists and the score.
_SUSPECT_MIN_ITEMS = 3
_SUSPECT_MAX_OVERLAP = 0.34  # residual token Jaccard above this = genuinely related items
_DOMAIN_RE = re.compile(r"(^|\s)[\w-]+\s?\.\s?(com|net|org|io|co|app|ai|dev|xyz|info)(\s|$)")
_STOPWORDS = frozenset(
    "a an the and or of in on for to with vs versus is are how what why when where who "
    "can do does best top new free near me my your its it this that from by at as be".split()
)


def _shingles(tokens: list[str]) -> set[tuple[str, ...]]:
    """Bigrams and trigrams. Single tokens are deliberately not markers: one brand
    word across several product queries (``breville ...``) is normal, whereas the
    injection pattern seen in the wild is a full personal name."""
    out: set[tuple[str, ...]] = set()
    for n in (2, 3):
        out.update(tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
    return out


def flag_suspects(
    rising: list[dict[str, Any]], top: list[dict[str, Any]], seed: str
) -> list[dict[str, Any]]:
    """Mark rising items that look like injected spam with ``suspect`` / ``suspect_reason``.

    A marker (2-3-token shingle) is suspicious when it is not part of the seed,
    appears in at least three rising items, never appears in the top list, and the
    items carrying it have little else in common. Domain-like items (``brand .com``)
    are flagged on their own.
    """
    seed_tokens = set(normalise_query(seed).split())
    top_text = " ".join(normalise_query(t.get("name") or "") for t in top)
    tokens_by_idx: list[list[str]] = []
    carriers: dict[tuple[str, ...], list[int]] = {}
    for i, it in enumerate(rising):
        toks = normalise_query(it.get("name") or "").split()
        tokens_by_idx.append(toks)
        for sh in _shingles(toks):
            # A marker needs at least one content word that is not the seed itself;
            # "best <seed>" or "<seed> machine" are ordinary query shapes.
            if not (set(sh) - seed_tokens - _STOPWORDS):
                continue
            carriers.setdefault(sh, []).append(i)

    reasons: dict[int, str] = {}
    for sh in sorted(carriers, key=lambda s: (-len(s), s)):
        idxs = carriers[sh]
        if len(idxs) < _SUSPECT_MIN_ITEMS:
            continue
        if all(i in reasons for i in idxs):
            continue  # already explained by a longer shingle
        phrase = " ".join(sh)
        if re.search(rf"\b{re.escape(phrase)}\b", top_text):
            continue  # also a top query: a real sub-topic, not an injection
        residuals = [
            set(tokens_by_idx[i]) - set(sh) - seed_tokens - _STOPWORDS for i in idxs
        ]
        pairs = [(a, b) for n, a in enumerate(residuals) for b in residuals[n + 1 :]]
        overlap = (
            sum(len(a & b) / len(a | b) for a, b in pairs if a | b) / len(pairs) if pairs else 0.0
        )
        if overlap > _SUSPECT_MAX_OVERLAP:
            continue
        for i in idxs:
            reasons.setdefault(
                i,
                f"repeated token '{phrase}' across {len(idxs)} unrelated rising queries, "
                "absent from top queries",
            )

    for i, it in enumerate(rising):
        if i not in reasons and _DOMAIN_RE.search((it.get("name") or "").lower()):
            reasons[i] = "domain-like rising query (looks like a site promoting itself)"
        if i in reasons:
            it["suspect"] = True
            it["suspect_reason"] = reasons[i]
    return rising


def split_suspects(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(clean, suspect) partition of a flagged rising list."""
    clean = [it for it in items if not it.get("suspect")]
    suspect = [it for it in items if it.get("suspect")]
    return clean, suspect


def region_list(df: pd.DataFrame | None, keywords: list[str], limit: int) -> list[dict[str, Any]]:
    """Shape interest_by_region into a list sorted by combined interest."""
    if df is None or df.empty:
        return []
    present = [kw for kw in keywords if kw in df.columns]
    if not present:
        return []
    frame = df.copy()
    frame["_total"] = frame[present].sum(axis=1)
    frame = frame[frame["_total"] > 0].sort_values("_total", ascending=False).head(limit)
    out: list[dict[str, Any]] = []
    for name, row in frame.iterrows():
        entry: dict[str, Any] = {"region": str(name)}
        if "geoCode" in frame.columns:
            entry["geo_code"] = _to_native(row["geoCode"])
        entry["values"] = {kw: _to_native(row[kw]) for kw in present}
        out.append(entry)
    return out


def flatten_categories(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten Google's nested category tree into {id, name, path} rows."""
    result: list[dict[str, Any]] = []
    if not tree:
        return result

    def walk(node: dict[str, Any], path: list[str]) -> None:
        name = node.get("name")
        cid = node.get("id")
        here = path + [name] if name and name != "All categories" else path
        if cid is not None and name:
            result.append({"id": int(cid), "name": name, "path": " > ".join(here) or name})
        for child in node.get("children", []) or []:
            walk(child, here)

    walk(tree, [])
    return result


# ---------------------------------------------------------------- Google News (A5)
def publisher_table(articles: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Publisher frequency (mention / pitch targets) with share of all fetched articles."""
    counts = Counter((a.get("publisher") or "unknown") for a in articles)
    total = sum(counts.values()) or 1
    return [
        {"publisher": name, "articles": n, "share_pct": round(100.0 * n / total, 1)}
        for name, n in counts.most_common(limit)
    ]


def recency_histogram(
    articles: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Cumulative age buckets (an article <24 h old counts in all three) plus range."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {
        "last_24h": 0,
        "last_7d": 0,
        "last_30d": 0,
        "older": 0,
        "undated": 0,
        "newest": None,
        "oldest": None,
    }
    dated: list[str] = []
    for a in articles:
        raw = a.get("published")
        if not raw:
            out["undated"] += 1
            continue
        ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dated.append(raw)
        age_days = (now - ts).total_seconds() / 86400.0
        if age_days <= 1:
            out["last_24h"] += 1
        if age_days <= 7:
            out["last_7d"] += 1
        if age_days <= 30:
            out["last_30d"] += 1
        else:
            out["older"] += 1
    if dated:
        out["newest"], out["oldest"] = max(dated), min(dated)
    return out


def coverage_level(recency: dict[str, Any]) -> str:
    """Heuristic label from the 30-day count: none | low (<5) | moderate (<20) | high."""
    n = int(recency.get("last_30d", 0) or 0)
    if n == 0:
        return "none"
    if n < 5:
        return "low"
    if n < 20:
        return "moderate"
    return "high"


# ---------------------------------------------------------------- Wikipedia (A6)
def pageview_summary(points: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """Summarise daily Wikimedia pageviews: totals, peak, direction, growth, insight."""
    views = [int(p["views"]) for p in points]
    if not views:
        return {
            "days": 0,
            "total": 0,
            "daily_mean": None,
            "peak": None,
            "peak_date": None,
            "direction": "insufficient_data",
            "growth_pct": None,
            "insight": f"No pageview data for '{title}' in this window.",
        }
    peak_i = max(range(len(views)), key=views.__getitem__)
    fvals = [float(x) for x in views]
    direction = _direction(fvals)
    direction_now = _direction_now(fvals)
    growth: float | None = None
    if len(views) >= 3:
        growth = _pct_change(*_thirds(fvals))
    return {
        "days": len(views),
        "total": sum(views),
        "daily_mean": round(sum(views) / len(views), 1),
        "peak": views[peak_i],
        "peak_date": points[peak_i]["date"],
        "direction": direction,
        "direction_now": direction_now,
        "growth_pct": growth,
        "insight": _insight(
            title, fvals, direction, views[peak_i], points[peak_i]["date"], direction_now
        ),
    }


# ---------------------------------------------------------------- citability (A12)
# Roadmap A12: which evidence makes an answer citable, by title angle. From Aggarwal
# et al., "GEO: Generative Engine Optimization" (KDD 2024): citing sources, adding
# quotations and adding statistics each lifted visibility 30-40%, and the best evidence
# type depends on the question type. Keyword stuffing and authoritative tone did nothing.
CITABILITY_HINTS: dict[str, dict[str, Any]] = {
    "how-to": {
        "evidence": [
            "numbered steps",
            "a statistic per step (time, cost, yield)",
            "one expert or manufacturer quote",
        ],
        "structure": "direct 40-60-word answer first, then the steps",
        "cite": "primary sources: manuals, standards, peer-reviewed or manufacturer guidance",
    },
    "comparison": {
        "evidence": [
            "a side-by-side data table",
            "quotes from reviews or users on both sides",
            "a clear verdict with the deciding metric",
        ],
        "structure": "verdict first, then the table, then when each option wins",
        "cite": "spec sheets, independent tests, dated price sources",
    },
    "listicle": {
        "evidence": [
            "the selection criterion stated up front",
            "one statistic per item",
            "one source per item",
        ],
        "structure": "numbered list, best first, one line of why per item",
        "cite": "reviews, sales or usage data, expert round-ups",
    },
    "definition": {
        "evidence": [
            "a one-sentence definition",
            "an origin or first-use fact",
            "one authoritative quote",
        ],
        "structure": "definition first, then context, then examples",
        "cite": "Wikipedia, dictionaries, standards bodies, the primary source",
    },
    "news": {
        "evidence": [
            "dated publisher quotes",
            "the primary announcement or filing",
            "numbers from the announcement",
        ],
        "structure": "what changed, when, who said so, what it means for the reader",
        "cite": "the original announcement plus two independent outlets, with dates",
    },
}


def citability_hints(angle: str | None) -> dict[str, Any]:
    """Evidence recipe for an angle (falls back to how-to); returns a copy."""
    hints = CITABILITY_HINTS.get(angle or "how-to", CITABILITY_HINTS["how-to"])
    return {"evidence": list(hints["evidence"]), "structure": hints["structure"], "cite": hints["cite"]}



# ---------------------------------------------------------------------------
# 0.5.0: seasonality (N1)
# ---------------------------------------------------------------------------

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)
SEASONAL_MIN_YEARS = 3
SEASONAL_STRENGTH_THRESHOLD = 1.5
PUBLISH_LEAD_DAYS = 42
_FULL_YEAR_MIN_POINTS = 40  # weekly series: 52 points per full year, allow gaps
_PEAK_OVER_MEAN = 1.2  # a year's peak month must be >= 120% of that year's monthly mean


def seasonality_profile(series: pd.Series, today: date | None = None) -> dict[str, Any]:
    """Monthly demand profile from a multi-year (ideally 5-y weekly) series.

    - ``monthly_index``: mean per calendar month over all years, 0-100 relative to
      the strongest month.
    - ``peak_months``: months in a year's top 2 in >= 3 of the full years present.
    - ``seasonal_strength`` = mean(peak months) / mean(other months).
    - ``seasonal``: strength >= 1.5 and at least one peak month; ``None`` with a
      ``reason`` when fewer than 3 full years of non-zero data exist.
    - ``next_peak`` / ``next_peak_in_days`` / ``publish_by`` (peak - 42 days or
      ``"now"`` when already inside the lead window).
    """
    today = today or datetime.now(timezone.utc).date()
    out: dict[str, Any] = {
        "seasonal": None,
        "seasonal_strength": None,
        "peak_months": [],
        "trough_months": [],
        "monthly_index": {},
        "years_analysed": 0,
        "next_peak": None,
        "next_peak_in_days": None,
        "publish_by": None,
        "insight": None,
    }
    if series is None or len(series) == 0 or not isinstance(series.index, pd.DatetimeIndex):
        out["reason"] = "No time series available."
        return out
    s = series.astype(float)
    years = s.index.year
    counts = s.groupby(years).count()
    full_years = [
        int(y) for y, n in counts.items() if n >= _FULL_YEAR_MIN_POINTS and s[years == y].sum() > 0
    ]
    out["years_analysed"] = len(full_years)
    # Profile, strength and index all come from the same full-year subset: a partial
    # year's four January weeks must not be able to overturn three complete years.
    full = s[s.index.year.isin(full_years)] if full_years else s
    monthly_all = full.groupby(full.index.month).mean()
    if len(monthly_all):
        peak_val = float(monthly_all.max()) or 1.0
        out["monthly_index"] = {
            MONTH_NAMES[int(m) - 1]: round(100.0 * float(val) / peak_val, 1)
            for m, val in monthly_all.items()
        }
    if len(full_years) < SEASONAL_MIN_YEARS:
        out["reason"] = (
            f"Only {len(full_years)} full year(s) of data; seasonality needs at least "
            f"{SEASONAL_MIN_YEARS} (timeframe 'today 5-y')."
        )
        return out

    # A month is a peak candidate for a year when it is in that year's top 2 *and*
    # clearly above the year's mean; otherwise a flat year would nominate two
    # arbitrary months (ties resolve to January) and fake a second peak.
    top2_counts: Counter[int] = Counter()
    for y in full_years:
        year_s = s[years == y]
        year_months = year_s.groupby(year_s.index.month).mean()
        floor = float(year_months.mean()) * _PEAK_OVER_MEAN
        for m in year_months.sort_values(ascending=False).index[:2]:
            if float(year_months.loc[m]) >= floor:
                top2_counts[int(m)] += 1
    peaks = sorted(m for m, n in top2_counts.items() if n >= SEASONAL_MIN_YEARS)
    out["peak_months"] = [MONTH_NAMES[m - 1] for m in peaks]
    others = [int(m) for m in monthly_all.index if int(m) not in peaks]
    strength: float | None = None
    if peaks and others:
        peak_mean = float(monthly_all.loc[peaks].mean())
        other_mean = float(monthly_all.loc[others].mean())
        troughs = monthly_all.loc[others].nsmallest(2).index
        out["trough_months"] = [MONTH_NAMES[int(m) - 1] for m in sorted(int(x) for x in troughs)]
        if other_mean > 0:
            strength = round(peak_mean / other_mean, 2)
            out["seasonal_strength"] = strength
            out["seasonal"] = bool(strength >= SEASONAL_STRENGTH_THRESHOLD)
        else:
            # Zero interest outside the peak months: the strongest seasonality there
            # is, not an absence of it. The ratio is unbounded, so report it as such.
            out["seasonal_strength"] = None
            out["strength_note"] = "no measurable interest outside peak months (ratio unbounded)"
            out["seasonal"] = peak_mean > 0
    else:
        out["seasonal"] = False

    if out["seasonal"]:
        # A peak month that is *today's* month is active, not upcoming.
        if today.month in peaks:
            out["in_peak_now"] = True
            out["peak_month_now"] = MONTH_NAMES[today.month - 1]
        nxt = min(
            date(today.year if m > today.month else today.year + 1, m, 1) for m in peaks
        )
        out["next_peak"] = nxt.isoformat()
        out["next_peak_in_days"] = (nxt - today).days
        publish = nxt - timedelta(days=PUBLISH_LEAD_DAYS)
        out["publish_by"] = "now" if publish <= today else publish.isoformat()
        when = (
            f"every {out['peak_months'][0]}" if len(peaks) == 1 else "in " + " and ".join(out["peak_months"])
        )
        ratio = f"{strength}x the rest of the year" if strength is not None else "no off-season interest"
        if out.get("in_peak_now"):
            out["insight"] = (
                f"Peaks {when} ({ratio}); the peak is happening now ({out['peak_month_now']}), "
                f"so publish immediately or plan for {out['next_peak']} (publish by {out['publish_by']})."
            )
        else:
            by = "publish now" if out["publish_by"] == "now" else f"publish by {out['publish_by']}"
            out["insight"] = f"Peaks {when} ({ratio}); next peak {out['next_peak']}, {by}."
    else:
        out["insight"] = (
            "No reliable seasonal peak: demand is spread through the year. Publish on trend "
            "signals (direction_now) rather than the calendar."
        )
    return out



# ---------------------------------------------------------------------------
# 0.5.0: compare_channels
# ---------------------------------------------------------------------------

CHANNELS: tuple[tuple[str, str], ...] = (
    ("", "web"),
    ("youtube", "youtube"),
    ("news", "news"),
    ("images", "images"),
    ("froogle", "shopping"),
)
FORMAT_HINTS: dict[str, str] = {
    "web": "long-form article or guide",
    "youtube": "video or step-by-step visual tutorial",
    "shopping": "buying guide, comparison or 'best X' list",
    "news": "timely or newsjacking piece",
    "images": "visual / infographic-led content",
}
CHANNEL_NOISE_FLOOR = 5.0


def channel_insight(channels: dict[str, dict[str, Any]], fastest: str | None) -> str:
    """One sentence comparing the fastest-growing channel with web search."""
    if not fastest:
        return (
            "No channel shows enough interest (mean >= 5) to compare growth; the topic is "
            "too small or too new for a format decision."
        )

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.0f}%"

    label = "Web search" if fastest == "web" else fastest.capitalize()
    fg = channels[fastest].get("growth_3m") or 0.0
    wg = (channels.get("web") or {}).get("growth_3m")
    vs_web = "" if fastest == "web" else f" vs {pct(wg)} on web search"
    hint = FORMAT_HINTS[fastest]
    # "Fastest" among declining channels is "declining least": say so, and do not
    # dress a shrinking topic up as a format opportunity.
    if fg <= -5:
        return (
            f"Every channel is declining over the last quarter; {label} is falling least "
            f"({pct(fg)}{vs_web}). This is a refresh / consolidation signal, not a launch "
            f"signal; if you do publish, the least-bad format is a {hint}."
        )
    if fg < 5:
        return (
            f"No channel is growing meaningfully over the last quarter ({label} is flat at "
            f"{pct(fg)}{vs_web}). Choose the format by audience fit rather than momentum; "
            f"default: {hint}."
        )
    if fastest == "web":
        return f"Web search grows fastest ({pct(fg)} over the last quarter); lead with a {hint}."
    return f"{label} interest grows {pct(fg)}{vs_web}; lead with a {hint}."


# ---------------------------------------------------------------------------
# 0.5.0: site coverage (N3)
# ---------------------------------------------------------------------------

SITE_PARTIAL_JACCARD = 0.6


def site_coverage(candidate: str, site_queries: list[str]) -> tuple[str, str | None]:
    """``(covered | partial | gap, matched_site_query)`` for one candidate topic.

    covered: exact normalised match, or every candidate token appears in a site
    query; partial: token Jaccard >= 0.6 with some site query; gap: otherwise.
    """
    cand = normalise_query(candidate)
    if not cand or not site_queries:
        return "gap", None
    cand_tokens = set(cand.split())
    best: tuple[float, str | None] = (0.0, None)
    for sq in site_queries:
        norm = normalise_query(sq)
        if not norm:
            continue
        if norm == cand:
            return "covered", sq
        toks = set(norm.split())
        if cand_tokens <= toks:
            return "covered", sq
        union = cand_tokens | toks
        jac = len(cand_tokens & toks) / len(union) if union else 0.0
        if jac > best[0]:
            best = (jac, sq)
    if best[0] >= SITE_PARTIAL_JACCARD:
        return "partial", best[1]
    return "gap", None



# ---------------------------------------------------------------------------
# 0.5.0: unified explainable opportunity score (N8)
# ---------------------------------------------------------------------------

SCORE_MAX: dict[str, int] = {"demand": 25, "momentum": 25, "freshness": 15, "gap": 20, "aeo": 15}
SCORE_NOTE = (
    "score is a transparent 0-100 heuristic; score_breakdown gives points, max and the "
    "reason per component: demand 25 (questions found or top-query popularity), momentum 25 "
    "(seed direction_now: rising 15 / stable or new 8 / falling 0 / unknown 5; +10 breakout, "
    "+5 rising query), freshness 15 (Google News: 10 if covered this week, +5 if 10+ articles "
    "in 30 days; 0 when news was not fetched), gap 20 (vs. site_queries: gap 20 / partial 10 / "
    "covered 0; neutral 10 without site_queries), aeo 15 (question-shaped 5; definition angle "
    "with no Wikipedia article +10, rising Wikipedia attention +5). Compare scores within one "
    "call only."
)


def _component(points: float, maximum: int, reason: str) -> dict[str, Any]:
    return {"points": int(max(0, min(maximum, round(points)))), "max": maximum, "reason": reason}


def opportunity_score(
    *,
    questions: int = 0,
    popularity: float | None = None,
    direction_now: str | None = None,
    has_breakout: bool = False,
    is_rising: bool = False,
    news: dict[str, Any] | None = None,
    coverage: str | None = None,
    angle: str | None = None,
    wiki: dict[str, Any] | None = None,
    question_shaped: bool = False,
) -> tuple[int, dict[str, dict[str, Any]]]:
    """0-100 heuristic shared by discover_topics and aeo_opportunities.

    Every component reports ``points``, ``max`` and a one-line ``reason`` so the
    model can explain (or disagree with) the ranking. Weights per SCORE_NOTE.
    """
    parts: dict[str, dict[str, Any]] = {}

    if questions:
        parts["demand"] = _component(
            5 * questions, 25, f"{questions} question(s) found (5 each, max 25)"
        )
    elif popularity is not None:
        parts["demand"] = _component(popularity / 4, 25, f"top-query popularity {popularity:.0f}/100")
    else:
        parts["demand"] = _component(5, 25, "rising-only item: popularity unknown")

    base = {"rising": 15, "stable": 8, "new": 8, "falling": 0}
    if direction_now in base:
        pts, why = base[direction_now], f"seed direction_now {direction_now}"
    else:
        pts, why = 5, "seed trend unavailable"
    if has_breakout:
        pts, why = pts + 10, why + " + breakout"
    elif is_rising:
        pts, why = pts + 5, why + " + rising query"
    parts["momentum"] = _component(pts, 25, why)

    if news is None:
        parts["freshness"] = _component(0, 15, "news not fetched")
    else:
        a7 = int(news.get("articles_7d") or 0)
        a30 = int(news.get("articles_30d") or 0)
        parts["freshness"] = _component(
            (10 if a7 > 0 else 0) + (5 if a30 >= 10 else 0),
            15,
            f"{a7} article(s) in 7 d, {a30} in 30 d",
        )

    gap_pts = {"gap": 20, "partial": 10, "covered": 0}
    if coverage in gap_pts:
        parts["gap"] = _component(gap_pts[coverage], 20, f"site coverage: {coverage}")
    else:
        parts["gap"] = _component(10, 20, "no site_queries given (neutral)")

    pts = 5 if (question_shaped or questions) else 0
    why = "question-shaped" if pts else "not a question"
    rising_wiki = wiki is not None and wiki.get("has_article") and wiki.get("direction") == "rising"
    if angle == "definition":
        if wiki is None:
            pts, why = pts + 5, why + "; definition angle (Wikipedia not checked)"
        elif wiki.get("related_article"):
            why += "; Wikipedia has a related article (verify before claiming a gap)"
        elif wiki.get("has_article"):
            why += "; Wikipedia article exists"
            if rising_wiki:
                pts, why = pts + 5, why + " with rising attention"
        else:
            pts, why = pts + 10, why + "; no Wikipedia article: definition gap"
    elif rising_wiki:
        pts, why = pts + 5, why + "; Wikipedia attention rising"
    parts["aeo"] = _component(pts, 15, why)

    total = min(100, sum(p["points"] for p in parts.values()))
    return total, parts

