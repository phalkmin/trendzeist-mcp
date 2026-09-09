"""Thin, rate-limited, cached wrapper around pytrends-modern.

All Google traffic goes through :class:`TrendsClient`, which enforces a
minimum interval between requests, caches responses for a short TTL and maps
library exceptions to :class:`TrendsError` with model-friendly messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import pandas as pd
import platformdirs
from pytrends_modern import TrendReq, TrendsRSS
from pytrends_modern.exceptions import (
    DownloadError,
    InvalidParameterError,
    ResponseError,
    TooManyRequestsError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

EXPLORE_TTL = 15 * 60
RSS_TTL = 5 * 60
STATIC_TTL = 24 * 60 * 60


class TrendsError(RuntimeError):
    """Raised for any upstream failure; message is safe to show the model."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Settings:
    """Runtime settings, sourced from environment variables."""

    hl: str = "en-US"
    tz: int = 360
    min_interval: float = 2.0
    retries: int = 3
    backoff_factor: float = 1.5
    proxies: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        proxies_raw = os.environ.get("TRENDZEIST_PROXIES", "").strip()
        proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]
        return cls(
            hl=os.environ.get("TRENDZEIST_HL", "en-US"),
            tz=int(os.environ.get("TRENDZEIST_TZ", "360")),
            min_interval=float(os.environ.get("TRENDZEIST_MIN_INTERVAL", "2.0")),
            retries=int(os.environ.get("TRENDZEIST_RETRIES", "3")),
            backoff_factor=float(os.environ.get("TRENDZEIST_BACKOFF", "1.5")),
            proxies=proxies,
        )


class _TTLCache:
    """Thread-safe TTL cache with an optional on-disk layer.

    MCP clients (e.g. Claude Desktop) restart the server process frequently, so
    an in-memory cache alone would rarely hit. Entries are pickled to
    ``cache_dir`` (disabled when ``cache_dir`` is ``None``). Disk errors never
    propagate: the cache silently degrades to memory-only.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._dir = cache_dir
        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("cache dir %s unavailable (%s); memory-only", self._dir, exc)
                self._dir = None

    def _path(self, key: str) -> Path | None:
        if self._dir is None:
            return None
        return self._dir / (hashlib.sha256(key.encode()).hexdigest() + ".pkl")

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                expires, value = hit
                if expires >= now:
                    return value
                del self._data[key]
        path = self._path(key)
        if path is None or not path.exists():
            return None
        try:
            with path.open("rb") as fh:
                expires, value = pickle.load(fh)
            if expires < now:
                path.unlink(missing_ok=True)
                return None
        except (OSError, pickle.PickleError, EOFError, ValueError) as exc:
            logger.debug("disk cache read failed for %s: %s", path, exc)
            return None
        with self._lock:
            self._data[key] = (expires, value)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        expires = time.time() + ttl
        with self._lock:
            self._data[key] = (expires, value)
        path = self._path(key)
        if path is None:
            return
        try:
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as fh:
                pickle.dump((expires, value), fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except (OSError, pickle.PickleError) as exc:
            logger.debug("disk cache write failed for %s: %s", path, exc)


def default_cache_dir() -> Path | None:
    """Resolve the cache directory from env; ``TRENDZEIST_CACHE_DIR=off`` disables it."""
    raw = os.environ.get("TRENDZEIST_CACHE_DIR", "").strip()
    if raw.lower() in {"off", "none", "0", "false"}:
        return None
    if raw:
        return Path(raw).expanduser()
    return Path(platformdirs.user_cache_dir("trendzeist-mcp"))


def _cache_key(name: str, **params: Any) -> str:
    return name + ":" + json.dumps(params, sort_keys=True, default=str)


class TrendsClient:
    """Serialises and caches access to Google Trends."""

    def __init__(
        self, settings: Settings | None = None, cache_dir: Path | None | str = "auto"
    ) -> None:
        self.settings = settings or Settings.from_env()
        resolved = default_cache_dir() if cache_dir == "auto" else cache_dir
        self._cache = _TTLCache(Path(resolved) if resolved else None)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._trend_req: TrendReq | None = None
        self._rss = TrendsRSS()

    # ------------------------------------------------------------------ internals
    def _req(self) -> TrendReq:
        if self._trend_req is None:
            self._trend_req = TrendReq(
                hl=self.settings.hl,
                tz=self.settings.tz,
                retries=self.settings.retries,
                backoff_factor=self.settings.backoff_factor,
                proxies=self.settings.proxies or None,
            )
        return self._trend_req

    def _reset(self) -> None:
        """Drop the session so fresh cookies are fetched on the next call."""
        self._trend_req = None

    def _throttle(self) -> None:
        wait = self.settings.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _guarded(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the global lock with throttling and error mapping."""
        with self._lock:
            self._throttle()
            try:
                return fn()
            except TooManyRequestsError as exc:
                self._reset()
                raise TrendsError(
                    "Google Trends rate limit reached (HTTP 429). Wait a minute before "
                    "retrying; cached results from earlier calls remain available.",
                    retryable=True,
                ) from exc
            except InvalidParameterError as exc:
                raise TrendsError(f"Google rejected the request parameters: {exc}") from exc
            except (ResponseError, DownloadError) as exc:
                self._reset()
                raise TrendsError(f"Google Trends request failed: {exc}", retryable=True) from exc
            except (ValueError, KeyError, IndexError) as exc:
                raise TrendsError(f"Unexpected response from Google Trends: {exc}") from exc

    def _cached(self, key: str, ttl: float, fn: Callable[[], T]) -> T:
        hit = self._cache.get(key)
        if hit is not None:
            logger.debug("cache hit %s", key)
            return hit
        value = self._guarded(fn)
        self._cache.set(key, value, ttl)
        return value

    def _explore(
        self, keywords: list[str], timeframe: str, geo: str, category: int, gprop: str
    ) -> TrendReq:
        req = self._req()
        req.build_payload(kw_list=keywords, cat=category, timeframe=timeframe, geo=geo, gprop=gprop)
        return req

    # ------------------------------------------------------------------ public API
    def interest_over_time(
        self, keywords: list[str], timeframe: str, geo: str, category: int, gprop: str
    ) -> pd.DataFrame:
        key = _cache_key("iot", kw=keywords, tf=timeframe, geo=geo, cat=category, gprop=gprop)
        return self._cached(
            key,
            EXPLORE_TTL,
            lambda: self._explore(keywords, timeframe, geo, category, gprop).interest_over_time(),
        )

    def interest_by_region(
        self,
        keywords: list[str],
        timeframe: str,
        geo: str,
        category: int,
        gprop: str,
        resolution: str,
    ) -> pd.DataFrame:
        key = _cache_key(
            "region", kw=keywords, tf=timeframe, geo=geo, cat=category, gprop=gprop, res=resolution
        )
        return self._cached(
            key,
            EXPLORE_TTL,
            lambda: self._explore(keywords, timeframe, geo, category, gprop).interest_by_region(
                resolution=resolution, inc_low_vol=True, inc_geo_code=True
            ),
        )

    def related_queries(
        self, keyword: str, timeframe: str, geo: str, category: int, gprop: str
    ) -> dict[str, pd.DataFrame | None]:
        key = _cache_key("rq", kw=keyword, tf=timeframe, geo=geo, cat=category, gprop=gprop)

        def fetch() -> dict[str, pd.DataFrame | None]:
            result = self._explore([keyword], timeframe, geo, category, gprop).related_queries()
            return result.get(keyword) or {"top": None, "rising": None}

        return self._cached(key, EXPLORE_TTL, fetch)

    def related_topics(
        self, keyword: str, timeframe: str, geo: str, category: int, gprop: str
    ) -> dict[str, pd.DataFrame | None]:
        key = _cache_key("rt", kw=keyword, tf=timeframe, geo=geo, cat=category, gprop=gprop)

        def fetch() -> dict[str, pd.DataFrame | None]:
            result = self._explore([keyword], timeframe, geo, category, gprop).related_topics()
            return result.get(keyword) or {"top": None, "rising": None}

        return self._cached(key, EXPLORE_TTL, fetch)

    def suggestions(self, keyword: str) -> list[dict[str, Any]]:
        key = _cache_key("sugg", kw=keyword, hl=self.settings.hl)
        return self._cached(key, STATIC_TTL, lambda: self._req().suggestions(keyword))

    def categories(self) -> dict[str, Any]:
        key = _cache_key("cats", hl=self.settings.hl)
        return self._cached(key, STATIC_TTL, lambda: self._req().categories())

    def trending_rss(self, geo: str, max_articles: int) -> list[dict[str, Any]]:
        key = _cache_key("rss", geo=geo, art=max_articles)
        return self._cached(
            key,
            RSS_TTL,
            lambda: self._rss.get_trends(
                geo=geo,
                output_format="dict",
                include_images=False,
                include_articles=max_articles > 0,
                max_articles_per_trend=max(max_articles, 1),
            ),
        )

