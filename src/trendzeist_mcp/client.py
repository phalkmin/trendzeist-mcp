"""Thin, rate-limited, cached wrapper around pytrends-modern.

All Google traffic goes through :class:`TrendsClient`, which enforces a
minimum interval between *every* HTTP request, caches responses for a short
TTL (JSON on disk, bounded in memory) and maps library exceptions to
:class:`TrendsError` with model-friendly messages.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import pandas as pd
import platformdirs
import requests
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

MAX_MEMORY_ENTRIES = 256
DISK_SWEEP_INTERVAL = 5 * 60
_DF_TAG = "__dataframe__"
_COOKIE_URL = "https://trends.google.com/?geo={geo}"


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


def _encode(value: Any) -> Any:
    """Recursively convert cached values (incl. DataFrames) to JSON-safe data."""
    if isinstance(value, pd.DataFrame):
        return {_DF_TAG: json.loads(value.to_json(orient="table", date_format="iso"))}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {_DF_TAG}:
            return pd.read_json(io.StringIO(json.dumps(value[_DF_TAG])), orient="table")
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


class _TTLCache:
    """Thread-safe TTL cache with an optional on-disk layer.

    MCP clients (e.g. Claude Desktop) restart the server process frequently, so
    an in-memory cache alone would rarely hit. Entries are stored as JSON in
    ``cache_dir`` (disabled when ``cache_dir`` is ``None``) so a tampered cache
    file can never execute code. Memory is bounded to ``MAX_MEMORY_ENTRIES``
    (oldest-inserted evicted first) and expired disk files are swept
    periodically. Disk errors never propagate: the cache degrades to memory-only.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._dir = cache_dir
        self._last_sweep = 0.0
        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._dir, 0o700)
            except OSError as exc:
                logger.warning("cache dir %s unavailable (%s); memory-only", self._dir, exc)
                self._dir = None

    def _path(self, key: str) -> Path | None:
        if self._dir is None:
            return None
        return self._dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _evict_locked(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._data.items() if exp < now]
        for k in expired:
            del self._data[k]
        while len(self._data) > MAX_MEMORY_ENTRIES:
            del self._data[next(iter(self._data))]

    def _sweep_disk(self, now: float) -> None:
        if self._dir is None or now - self._last_sweep < DISK_SWEEP_INTERVAL:
            return
        self._last_sweep = now
        try:
            for p in self._dir.glob("*.json"):
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        if float(json.load(fh)["expires"]) < now:
                            p.unlink(missing_ok=True)
                except (OSError, ValueError, KeyError, TypeError):
                    p.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("disk cache sweep failed: %s", exc)

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
            with path.open("r", encoding="utf-8") as fh:
                envelope = json.load(fh)
            expires = float(envelope["expires"])
            if expires < now:
                path.unlink(missing_ok=True)
                return None
            value = _decode(envelope["value"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("disk cache read failed for %s: %s", path, exc)
            return None
        with self._lock:
            self._data[key] = (expires, value)
            self._evict_locked(now)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        now = time.time()
        expires = now + ttl
        with self._lock:
            self._data[key] = (expires, value)
            self._evict_locked(now)
        path = self._path(key)
        if path is None:
            return
        try:
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"expires": expires, "value": _encode(value)}, fh)
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("disk cache write failed for %s: %s", path, exc)
        self._sweep_disk(now)


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


class _QuietTrendReq(TrendReq):
    """TrendReq that never writes to stdout and throttles every HTTP request.

    pytrends-modern ``print()``s cookie warnings, which would corrupt the MCP
    stdio channel, and issues several HTTP calls per operation (cookie, token,
    data). ``before_request`` is invoked before each of them.
    """

    def __init__(self, before_request: Callable[[], None], **kwargs: Any) -> None:
        self._before_request = before_request
        super().__init__(**kwargs)

    def _get_google_cookie(self) -> dict[str, str]:
        self._before_request()
        kwargs: dict[str, Any] = dict(self.requests_args)
        kwargs.setdefault("timeout", self.timeout)
        headers = {"User-Agent": self._get_user_agent()}
        headers.update(kwargs.pop("headers", None) or {})
        if self.proxies:
            kwargs["proxies"] = {"https": self.proxies[self.proxy_index]}
        try:
            response = requests.get(_COOKIE_URL.format(geo=self.hl[-2:]), headers=headers, **kwargs)
        except requests.RequestException as exc:
            logger.warning("could not fetch Google cookie (%s); continuing without it", exc)
            return {}
        return {k: v for k, v in response.cookies.items() if k == "NID"}

    def _get_data(self, url: str, method: str = TrendReq.GET_METHOD, **kwargs: Any) -> dict:
        self._before_request()
        return super()._get_data(url, method, **kwargs)


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
            self._trend_req = _QuietTrendReq(
                self._throttle,
                hl=self.settings.hl,
                tz=self.settings.tz,
                retries=self.settings.retries,
                backoff_factor=self.settings.backoff_factor,
                proxies=list(self.settings.proxies) or None,
            )
        return self._trend_req

    def _reset(self) -> None:
        """Drop the session so fresh cookies are fetched on the next call."""
        self._trend_req = None

    def _proxies(self) -> dict[str, str] | None:
        """Proxy mapping for requests made outside TrendReq (RSS)."""
        if not self.settings.proxies:
            return None
        return {"https": self.settings.proxies[0], "http": self.settings.proxies[0]}

    def _throttle(self) -> None:
        """Enforce ``min_interval`` between consecutive HTTP requests to Google."""
        wait = self.settings.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _guarded(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the global lock with error mapping.

        Throttling happens per HTTP request inside :class:`_QuietTrendReq` and
        :meth:`_fetch_rss`, not per operation.
        """
        with self._lock:
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
            except (ResponseError, DownloadError, requests.RequestException) as exc:
                self._reset()
                raise TrendsError(f"Google Trends request failed: {exc}", retryable=True) from exc
            except (ValueError, KeyError, IndexError, TypeError) as exc:
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
        # build_payload does ``self.geo = geo or self.geo``, so a worldwide ('')
        # request would silently inherit the previous call's country. Reset first.
        req.geo = ""
        req.build_payload(kw_list=keywords, cat=category, timeframe=timeframe, geo=geo, gprop=gprop)
        return req

    def _fetch_rss(self, geo: str, max_articles: int) -> list[dict[str, Any]]:
        """Fetch the trending RSS feed honouring proxies and throttling."""
        self._throttle()
        url = self._rss.RSS_URL_TEMPLATE.format(geo=self._rss._validate_geo(geo))
        response = requests.get(url, timeout=self._rss.timeout, proxies=self._proxies())
        response.raise_for_status()
        return self._rss._parse_rss_feed(
            response.text,
            include_images=False,
            include_articles=max_articles > 0,
            max_articles_per_trend=max(max_articles, 1),
        )

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
        return self._cached(key, RSS_TTL, lambda: self._fetch_rss(geo, max_articles))

