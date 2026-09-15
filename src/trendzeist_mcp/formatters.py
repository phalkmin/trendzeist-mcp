"""Convert pandas structures into compact, JSON-serialisable dicts.

Tool responses are consumed by an LLM, so payloads are kept small: long
time series are downsampled, floats are rounded and lists are capped.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

import pandas as pd

MAX_SERIES_POINTS = 60
BREAKOUT_THRESHOLD = 5000  # Google reports "Breakout" as +5000%
SCALE_NOTE = "0-100 relative to the peak across all keywords in this query"

# Title angles (roadmap N6). Order matters: the first matching rule wins, so the
# more specific intents (comparison, definition) are checked before "how-to".
ANGLES: tuple[str, ...] = ("how-to", "comparison", "listicle", "definition", "news")
_ANGLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
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


def _direction(values: list[float]) -> str:
    """Classify a series as rising / falling / stable using first vs. last third means."""
    if len(values) < 3:
        return "insufficient_data"
    third = max(1, len(values) // 3)
    first = sum(values[:third]) / third
    last = sum(values[-third:]) / third
    if first == 0 and last == 0:
        return "no_interest"
    base = first if first > 0 else 1.0
    change = (last - first) / base
    if change >= 0.25:
        return "rising"
    if change <= -0.25:
        return "falling"
    return "stable"


_GROWTH_WINDOWS: dict[str, int] = {"growth_3m": 91, "growth_12m": 365}


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


def _insight(kw: str, vals: list[float], direction: str, peak: Any, peak_date: Any) -> str:
    """One plain-English sentence per keyword so the model needs no arithmetic."""
    if direction == "insufficient_data":
        return f"Too few data points to judge the trend for '{kw}'."
    if direction == "no_interest":
        return f"'{kw}' shows no measurable search interest in this period."
    third = max(1, len(vals) // 3)
    first = sum(vals[:third]) / third
    last = sum(vals[-third:]) / third
    if first > 0:
        change = round(100.0 * (last - first) / first)
        verb = "rose" if change > 0 else "fell" if change < 0 else "held flat"
        magnitude = f" {abs(change)}%" if change else ""
        head = f"Interest in '{kw}' {verb}{magnitude} between the first and last third of the period"
    else:
        head = f"Interest in '{kw}' appeared from zero during the period"
    tail = f", peaking at {peak} on {peak_date}" if peak is not None and peak_date else ""
    return f"{head}{tail} ({direction})."


def interest_over_time(df: pd.DataFrame | None, keywords: Iterable[str]) -> dict[str, Any]:
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
        peak = _to_native(stats_series.max()) if len(stats_series) else None
        peak_date = _to_native(peak_idx) if peak_idx is not None else None
        summary[kw] = {
            "available": True,
            "mean": round(sum(vals) / len(vals), 1) if vals else 0,
            "latest": _to_native(stats_series.iloc[-1]) if len(stats_series) else None,
            "peak": peak,
            "peak_date": peak_date,
            "direction": direction,
            **_growth_windows(stats_series),
            "insight": _insight(kw, vals, direction, peak, peak_date),
        }

    sampled = _downsample(complete, MAX_SERIES_POINTS)
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
