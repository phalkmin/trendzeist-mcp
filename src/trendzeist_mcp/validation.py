"""Input validation for MCP tool parameters.

Every tool argument that reaches Google is validated here first so that the
model receives an actionable error message instead of an opaque HTTP failure.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Sequence

MAX_KEYWORDS = 5
_TRENDS_EPOCH = date(2004, 1, 1)  # Google Trends has no data before 2004
MAX_KEYWORD_LENGTH = 100

VALID_GPROPS: frozenset[str] = frozenset({"", "images", "news", "youtube", "froogle"})
VALID_RESOLUTIONS: frozenset[str] = frozenset({"COUNTRY", "REGION", "CITY", "DMA"})

# Google Trends relative timeframes.
_RELATIVE_TIMEFRAMES: frozenset[str] = frozenset(
    {
        "now 1-H",
        "now 4-H",
        "now 1-d",
        "now 7-d",
        "today 1-m",
        "today 3-m",
        "today 12-m",
        "today 5-y",
        "all",
    }
)
_DATE_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{4}-\d{2}-\d{2}$")
_GEO_RE = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")


class ValidationError(ValueError):
    """Raised when a tool argument is invalid."""


def validate_keywords(keywords: Sequence[str], *, minimum: int = 1) -> list[str]:
    """Normalise and validate a keyword list (1-5 non-empty strings)."""
    if not isinstance(keywords, (list, tuple)):
        raise ValidationError("keywords must be a list of strings")
    cleaned: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        if not isinstance(kw, str):
            raise ValidationError("each keyword must be a string")
        kw = " ".join(kw.strip().split())
        if not kw:
            continue
        if len(kw) > MAX_KEYWORD_LENGTH:
            raise ValidationError(
                f"keyword '{kw[:20]}...' exceeds {MAX_KEYWORD_LENGTH} characters"
            )
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(kw)
    if len(cleaned) < minimum:
        raise ValidationError(f"at least {minimum} non-empty keyword(s) required")
    if len(cleaned) > MAX_KEYWORDS:
        raise ValidationError(f"Google Trends supports at most {MAX_KEYWORDS} keywords per request")
    return cleaned


def validate_keyword(keyword: str) -> str:
    """Validate a single keyword."""
    return validate_keywords([keyword])[0]


def validate_timeframe(timeframe: str) -> str:
    """Validate a Google Trends timeframe string."""
    if not isinstance(timeframe, str):
        raise ValidationError("timeframe must be a string")
    tf = timeframe.strip()
    if tf in _RELATIVE_TIMEFRAMES:
        return tf
    if _DATE_RANGE_RE.match(tf):
        start_raw, end_raw = tf.split(" ")
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError as exc:
            raise ValidationError(f"invalid calendar date in timeframe: {exc}") from exc
        if start < _TRENDS_EPOCH:
            raise ValidationError(f"timeframe start must be on or after {_TRENDS_EPOCH}")
        if end < start:
            raise ValidationError("timeframe end date must not be before the start date")
        if end > date.today():
            raise ValidationError("timeframe end date must not be in the future")
        return tf
    raise ValidationError(
        "invalid timeframe. Use one of "
        f"{sorted(_RELATIVE_TIMEFRAMES)} or 'YYYY-MM-DD YYYY-MM-DD'"
    )


def validate_geo(geo: str) -> str:
    """Validate a geo code ('' = worldwide, 'US', 'US-CA', 'GB', ...)."""
    if geo is None:
        return ""
    if not isinstance(geo, str):
        raise ValidationError("geo must be a string")
    g = geo.strip().upper()
    if g == "" or g == "WORLDWIDE":
        return ""
    if not _GEO_RE.match(g):
        raise ValidationError(
            "invalid geo. Use '' for worldwide, an ISO-2 country code ('US', 'BR') "
            "or a country-region code ('US-CA')"
        )
    return g


def validate_gprop(gprop: str) -> str:
    """Validate a Google property filter."""
    if gprop is None:
        return ""
    if not isinstance(gprop, str):
        raise ValidationError("gprop must be a string")
    g = gprop.strip().lower()
    if g == "web":
        g = ""
    if g not in VALID_GPROPS:
        raise ValidationError(f"gprop must be one of {sorted(VALID_GPROPS)} ('' = web search)")
    return g


def validate_category(category: int) -> int:
    """Validate a Google Trends category id (0 = all)."""
    if isinstance(category, bool) or not isinstance(category, int):
        raise ValidationError("category must be an integer id (0 = all categories)")
    if category < 0:
        raise ValidationError("category must be >= 0")
    return category


def validate_resolution(resolution: str, geo: str = "") -> str:
    """Validate an interest_by_region resolution for the given (validated) geo.

    pytrends-modern only applies a custom resolution when geo is worldwide or
    ``US``; for any other geo Google's default is used silently, so the tool
    would claim a granularity it did not request. Reject those combinations.
    """
    if not isinstance(resolution, str):
        raise ValidationError("resolution must be a string")
    r = resolution.strip().upper()
    if r not in VALID_RESOLUTIONS:
        raise ValidationError(f"resolution must be one of {sorted(VALID_RESOLUTIONS)}")
    if geo and r == "COUNTRY":
        raise ValidationError(
            "resolution COUNTRY only applies to worldwide queries (geo=''); use REGION within a country"
        )
    if geo and geo != "US" and r != "REGION":
        raise ValidationError(
            f"resolution '{r}' is only supported for geo='US'; for '{geo}' use REGION"
        )
    return r


def validate_limit(limit: int, *, default: int, maximum: int) -> int:
    """Clamp a result-limit argument into a sane range."""
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError("limit must be an integer")
    if limit < 1:
        raise ValidationError("limit must be >= 1")
    return min(limit, maximum)
