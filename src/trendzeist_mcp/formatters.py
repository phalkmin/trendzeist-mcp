"""Convert pandas structures into compact, JSON-serialisable dicts.

Tool responses are consumed by an LLM, so payloads are kept small: long
time series are downsampled, floats are rounded and lists are capped.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import pandas as pd

MAX_SERIES_POINTS = 60
BREAKOUT_THRESHOLD = 5000  # Google reports "Breakout" as +5000%
SCALE_NOTE = "0-100 relative to the peak across all keywords in this query"


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
        summary[kw] = {
            "available": True,
            "mean": round(sum(vals) / len(vals), 1) if vals else 0,
            "latest": _to_native(stats_series.iloc[-1]) if len(stats_series) else None,
            "peak": _to_native(stats_series.max()) if len(stats_series) else None,
            "peak_date": _to_native(peak_idx) if peak_idx is not None else None,
            "direction": _direction(vals),
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
