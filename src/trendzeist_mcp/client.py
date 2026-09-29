"""Shared upstream plumbing plus the Google Trends source.

:class:`Source` owns everything every upstream needs: a per-host :class:`Lane`
(lock + minimum interval between HTTP requests), the shared TTL cache (JSON on
disk, bounded in memory), proxy handling, error mapping to :class:`TrendsError`
with model-friendly messages, and health tracking. :class:`TrendsClient` is the
Google Trends implementation built on pytrends-modern; the other sources live
in :mod:`trendzeist_mcp.sources`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
MAX_SERIES_POINTS = 60
DISK_SWEEP_INTERVAL = 5 * 60
HTTP_TIMEOUT = 10  # seconds, for plain ``requests`` calls made by sources
_DF_TAG = "__dataframe__"
_COOKIE_URL = "https://trends.google.com/?geo={geo}"

# geo -> hl when TRENDZEIST_HL is unset (roadmap N9): related queries and
# autocomplete come back in the market's language. Unlisted geos keep en-US.
GEO_TO_HL: dict[str, str] = {
    "AR": "es-AR", "AT": "de-AT", "AU": "en-AU", "BE": "nl-BE", "BR": "pt-BR",
    "CA": "en-CA", "CH": "de-CH", "CL": "es-CL", "CN": "zh-CN", "CO": "es-CO",
    "CZ": "cs-CZ", "DE": "de-DE", "DK": "da-DK", "EG": "ar-EG", "ES": "es-ES",
    "FI": "fi-FI", "FR": "fr-FR", "GB": "en-GB", "GR": "el-GR", "HK": "zh-HK",
    "HU": "hu-HU", "ID": "id-ID", "IE": "en-IE", "IL": "he-IL", "IN": "en-IN",
    "IT": "it-IT", "JP": "ja-JP", "KR": "ko-KR", "MX": "es-MX", "MY": "ms-MY",
    "NG": "en-NG", "NL": "nl-NL", "NO": "no-NO", "NZ": "en-NZ", "PE": "es-PE",
    "PH": "en-PH", "PL": "pl-PL", "PT": "pt-PT", "RO": "ro-RO", "RU": "ru-RU",
    "SA": "ar-SA", "SE": "sv-SE", "SG": "en-SG", "TH": "th-TH", "TR": "tr-TR",
    "TW": "zh-TW", "UA": "uk-UA", "US": "en-US", "VN": "vi-VN", "ZA": "en-ZA",
}


def hl_for_geo(geo: str, default: str = "en-US") -> str:
    """Language for a geo ('BR' or 'BR-SP' -> 'pt-BR'); ``default`` when unknown."""
    country = (geo or "").split("-")[0].upper()
    return GEO_TO_HL.get(country, default)


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
    # When True (TRENDZEIST_HL unset) the language follows the request's geo.
    auto_hl: bool = True
    # Wikimedia has its own request lane (and a far gentler rate policy than Google).
    wiki_min_interval: float = 0.5
    explore_ttl: int = EXPLORE_TTL
    rss_ttl: int = RSS_TTL
    static_ttl: int = STATIC_TTL
    max_memory_entries: int = MAX_MEMORY_ENTRIES
    max_series_points: int = MAX_SERIES_POINTS

    @classmethod
    def from_env(cls) -> "Settings":
        proxies_raw = os.environ.get("TRENDZEIST_PROXIES", "").strip()
        proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]
        hl_env = os.environ.get("TRENDZEIST_HL", "").strip()
        env = os.environ.get

        def positive_int(name: str, default: int) -> int:
            raw = env(name, str(default))
            try:
                value = int(raw)
            except ValueError as exc:
                raise TrendsError(f"{name} must be a positive integer (got {raw!r}).") from exc
            if value < 1:
                raise TrendsError(f"{name} must be a positive integer (got {raw!r}).")
            return value

        return cls(
            hl=hl_env or "en-US",
            tz=int(env("TRENDZEIST_TZ", "360")),
            min_interval=float(env("TRENDZEIST_MIN_INTERVAL", "2.0")),
            retries=int(env("TRENDZEIST_RETRIES", "3")),
            backoff_factor=float(env("TRENDZEIST_BACKOFF", "1.5")),
            proxies=proxies,
            auto_hl=not hl_env,
            wiki_min_interval=float(env("TRENDZEIST_WIKI_MIN_INTERVAL", "0.5")),
            explore_ttl=positive_int("TRENDZEIST_EXPLORE_TTL", EXPLORE_TTL),
            rss_ttl=positive_int("TRENDZEIST_RSS_TTL", RSS_TTL),
            static_ttl=positive_int("TRENDZEIST_STATIC_TTL", STATIC_TTL),
            max_memory_entries=positive_int("TRENDZEIST_MAX_MEMORY_ENTRIES", MAX_MEMORY_ENTRIES),
            max_series_points=positive_int("TRENDZEIST_MAX_SERIES_POINTS", MAX_SERIES_POINTS),
        )

    def hl_for(self, geo: str) -> str:
        """Effective UI language for a request targeting ``geo``."""
        return hl_for_geo(geo, self.hl) if self.auto_hl else self.hl


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
    file can never execute code. Memory is bounded to ``max_entries``
    (oldest-inserted evicted first) and expired disk files are swept
    periodically. Disk errors never propagate: the cache degrades to memory-only.
    """

    def __init__(self, cache_dir: Path | None = None, max_entries: int | None = None) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._dir = cache_dir
        self._max_entries = max_entries if max_entries is not None else MAX_MEMORY_ENTRIES
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
        while len(self._data) > self._max_entries:
            del self._data[next(iter(self._data))]

    def stats(self) -> dict[str, Any]:
        """Cache health for ``trendzeist_status``; never raises."""
        with self._lock:
            entries = len(self._data)
        disk_files: int | None = None
        if self._dir is not None:
            try:
                disk_files = sum(1 for _ in self._dir.glob("*.json"))
            except OSError:
                disk_files = None
        return {
            "dir": str(self._dir) if self._dir is not None else None,
            "disk_enabled": self._dir is not None,
            "memory_entries": entries,
            "max_memory_entries": self._max_entries,
            "disk_files": disk_files,
        }

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
        # Unique temp file per write: a shared ``.tmp`` name lets two writers
        # (e.g. two server processes on the same cache dir) hold the same inode
        # and corrupt the published file after the first ``os.replace``.
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"expires": expires, "value": _encode(value)}, fh)
            os.replace(tmp_name, path)
            tmp_name = None
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("disk cache write failed for %s: %s", path, exc)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
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


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lane:
    """One upstream host family (Google, Wikimedia): a lock that serialises its
    network requests plus the minimum spacing between them."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        """Sleep until ``min_interval`` has passed since the previous request."""
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


class CallStats:
    """Per-thread request / cache counters for the tool call in flight (roadmap N15).

    MCP runs each sync tool on its own worker thread, so thread-local storage
    scopes the counters to one call even though sources are shared.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def begin(self) -> None:
        self._local.requests = 0
        self._local.cache_hits = 0
        self._local.cache_misses = 0

    def count(self, attr: str) -> None:
        setattr(self._local, attr, getattr(self._local, attr, 0) + 1)

    def meta(self) -> dict[str, Any]:
        hits = getattr(self._local, "cache_hits", 0)
        misses = getattr(self._local, "cache_misses", 0)
        return {
            "requests_made": getattr(self._local, "requests", 0),
            "cache_hit": hits > 0 and misses == 0,
            "cache_hits": hits,
            "cache_misses": misses,
        }


class Source:
    """Base for every upstream: throttling, caching, error mapping and health.

    Subclasses implement fetchers that call :meth:`_get` (or :meth:`_throttle`
    before any other HTTP call) and wrap public operations in :meth:`_cached`.
    ``name`` is the id reported by ``trendzeist_status``; ``label`` is used in
    error text shown to the model.
    """

    name = "source"
    label = "Source"

    def __init__(self, settings: Settings, cache: _TTLCache, lane: Lane, stats: CallStats) -> None:
        self.settings = settings
        self._cache = cache
        self._lane = lane
        self._stats = stats
        self._health_lock = threading.Lock()
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._last_error_at: float | None = None

    # ------------------------------------------------------------------ transport
    def _proxies(self) -> dict[str, str] | None:
        """Proxy mapping for plain ``requests`` calls (always the first proxy)."""
        if not self.settings.proxies:
            return None
        return {"https": self.settings.proxies[0], "http": self.settings.proxies[0]}

    def _throttle(self) -> None:
        """Count the request and enforce the lane's minimum interval."""
        self._stats.count("requests")
        self._lane.wait()

    def _reset(self) -> None:
        """Hook: drop upstream session state after a transport failure."""

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow: tuple[int, ...] = (),
    ) -> requests.Response:
        """Throttled, proxied GET. 429 -> rate-limit error; other 4xx/5xx raise
        unless the status is listed in ``allow`` (e.g. a meaningful 404)."""
        self._throttle()
        response = requests.get(
            url, params=params, headers=headers, timeout=HTTP_TIMEOUT, proxies=self._proxies()
        )
        if response.status_code == 429:
            raise TooManyRequestsError.from_response(response)
        if response.status_code not in allow:
            response.raise_for_status()
        return response

    # ------------------------------------------------------------------ health
    def _record(self, error: str | None) -> None:
        with self._health_lock:
            if error is None:
                self._last_success = time.time()
            else:
                self._last_error, self._last_error_at = error, time.time()

    def status(self) -> dict[str, Any]:
        """live = last network call succeeded; error = it failed; idle = none yet."""
        with self._health_lock:
            ok_at, err, err_at = self._last_success, self._last_error, self._last_error_at
        if ok_at is None and err_at is None:
            state = "idle"
        elif err_at is not None and (ok_at is None or err_at > ok_at):
            state = "error"
        else:
            state = "live"
        return {
            "name": self.name,
            "label": self.label,
            "state": state,
            "last_success": _iso(ok_at),
            "last_error": err,
            "last_error_at": _iso(err_at),
        }

    # ------------------------------------------------------------------ guard + cache
    def _map_error(self, exc: BaseException) -> TrendsError | None:
        """Translate an upstream exception; ``None`` means re-raise unchanged."""
        if isinstance(exc, TooManyRequestsError):
            self._reset()
            return TrendsError(
                f"{self.label} rate limit reached (HTTP 429). Wait a minute before "
                "retrying; cached results from earlier calls remain available.",
                retryable=True,
            )
        if isinstance(exc, InvalidParameterError):
            return TrendsError(f"{self.label} rejected the request parameters: {exc}")
        if isinstance(exc, (ResponseError, DownloadError, requests.RequestException)):
            self._reset()
            return TrendsError(f"{self.label} request failed: {exc}", retryable=True)
        if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)):
            return TrendsError(f"Unexpected response from {self.label}: {exc}")
        return None

    def _guarded(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the lane lock with error mapping and health tracking.

        Throttling happens per HTTP request inside the fetchers, not per operation.
        """
        with self._lane.lock:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - mapped or re-raised below
                mapped = self._map_error(exc)
                if mapped is None:
                    raise
                self._record(str(mapped))
                raise mapped from exc
            self._record(None)
            return result

    def _cached(self, key: str, ttl: float, fn: Callable[[], T]) -> T:
        hit = self._cache.get(key)
        if hit is not None:
            logger.debug("cache hit %s", key)
            self._stats.count("cache_hits")
            return hit

        def fetch_and_store() -> T:
            # Re-check under the lane lock: a concurrent caller with the same
            # key may have fetched and stored while we were waiting for it.
            again = self._cache.get(key)
            if again is not None:
                logger.debug("cache hit after wait %s", key)
                self._stats.count("cache_hits")
                return again
            self._stats.count("cache_misses")
            value = fn()
            self._cache.set(key, value, ttl)
            return value

        return self._guarded(fetch_and_store)


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
        # Fail fast: continuing without a cookie yields confusing downstream
        # errors (empty widgets, 4xx on the data call). ``_guarded`` maps these
        # to a retryable TrendsError with an actionable message.
        try:
            response = requests.get(_COOKIE_URL.format(geo=self.hl[-2:]), headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise DownloadError(f"could not reach Google Trends to fetch a session cookie: {exc}") from exc
        if response.status_code == 429:
            raise TooManyRequestsError.from_response(response)
        if response.status_code >= 400:
            raise ResponseError.from_response(response)
        return {k: v for k, v in response.cookies.items() if k == "NID"}

    def _get_data(self, url: str, method: str = TrendReq.GET_METHOD, **kwargs: Any) -> dict:
        # With proxies, upstream refreshes the cookie *inside* ``_get_data`` and
        # fires the data request immediately after, so the two HTTP calls are
        # not spaced. Do the cookie fetch ourselves (throttled), throttle again,
        # then run the parent with the proxy list hidden so it does not repeat
        # the cookie fetch. The proxy is passed explicitly and rotated afterwards
        # to keep upstream's rotation semantics.
        if self.proxies:
            self.cookies = self._get_google_cookie()
            proxy = {"https": self.proxies[self.proxy_index]}
            self._before_request()
            proxies_backup, self.proxies = self.proxies, []
            try:
                return super()._get_data(url, method, proxies=proxy, **kwargs)
            finally:
                self.proxies = proxies_backup
                self._get_new_proxy()
        self._before_request()
        return super()._get_data(url, method, **kwargs)

    def build_payload(self, *args: Any, **kwargs: Any) -> None:
        # Upstream ``_get_tokens`` only *overwrites* these widgets when Google
        # returns them; a response lacking TIMESERIES / GEO_MAP would leave the
        # previous query's widget in place and mislabel its data with the new
        # keywords. Always start from a clean slate.
        self.interest_over_time_widget = {}
        self.interest_by_region_widget = {}
        self.related_topics_widget_list = []
        self.related_queries_widget_list = []
        super().build_payload(*args, **kwargs)


class TrendsClient(Source):
    """Google Trends: explore (interest, related), suggestions, categories, trending RSS."""

    name = "google_trends"
    label = "Google Trends"

    def __init__(self, settings: Settings, cache: _TTLCache, lane: Lane, stats: CallStats) -> None:
        super().__init__(settings, cache, lane, stats)
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

    def _explore_key(self, name: str, geo: str = "", **params: Any) -> str:
        # hl/tz change what Google returns (language of related queries,
        # bucket boundaries); two processes with different env sharing the disk
        # cache must not see each other's results.
        return _cache_key(
            name, hl=self.settings.hl_for(geo), tz=self.settings.tz, geo=geo, **params
        )

    def _explore(
        self, keywords: list[str], timeframe: str, geo: str, category: int, gprop: str
    ) -> TrendReq:
        req = self._req()
        # build_payload does ``self.geo = geo or self.geo``, so a worldwide ('')
        # request would silently inherit the previous call's country. Reset first.
        req.geo = ""
        # ``hl`` is read from the instance at payload-build time, so switching it
        # per request is safe; the cookie geo is derived from the constructor hl.
        req.hl = self.settings.hl_for(geo)
        req.headers["accept-language"] = req.hl
        req.build_payload(kw_list=keywords, cat=category, timeframe=timeframe, geo=geo, gprop=gprop)
        return req

    def _fetch_rss(self, geo: str, max_articles: int) -> list[dict[str, Any]]:
        """Fetch the trending RSS feed honouring proxies and throttling."""
        url = self._rss.RSS_URL_TEMPLATE.format(geo=self._rss._validate_geo(geo))
        response = self._get(url)
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
        key = self._explore_key("iot", kw=keywords, tf=timeframe, geo=geo, cat=category, gprop=gprop)
        return self._cached(
            key,
            self.settings.explore_ttl,
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
        key = self._explore_key(
            "region", kw=keywords, tf=timeframe, geo=geo, cat=category, gprop=gprop, res=resolution
        )
        return self._cached(
            key,
            self.settings.explore_ttl,
            lambda: self._explore(keywords, timeframe, geo, category, gprop).interest_by_region(
                resolution=resolution, inc_low_vol=True, inc_geo_code=True
            ),
        )

    def has_cached_related_queries(
        self, keyword: str, timeframe: str, geo: str, category: int, gprop: str
    ) -> bool:
        """True when :meth:`related_queries` would be served without hitting Google."""
        key = self._explore_key("rq", kw=keyword, tf=timeframe, geo=geo, cat=category, gprop=gprop)
        return self._cache.get(key) is not None

    def related_queries(
        self, keyword: str, timeframe: str, geo: str, category: int, gprop: str
    ) -> dict[str, pd.DataFrame | None]:
        key = self._explore_key("rq", kw=keyword, tf=timeframe, geo=geo, cat=category, gprop=gprop)

        def fetch() -> dict[str, pd.DataFrame | None]:
            result = self._explore([keyword], timeframe, geo, category, gprop).related_queries()
            return result.get(keyword) or {"top": None, "rising": None}

        return self._cached(key, self.settings.explore_ttl, fetch)

    def related_topics(
        self, keyword: str, timeframe: str, geo: str, category: int, gprop: str
    ) -> dict[str, pd.DataFrame | None]:
        key = self._explore_key("rt", kw=keyword, tf=timeframe, geo=geo, cat=category, gprop=gprop)

        def fetch() -> dict[str, pd.DataFrame | None]:
            result = self._explore([keyword], timeframe, geo, category, gprop).related_topics()
            return result.get(keyword) or {"top": None, "rising": None}

        return self._cached(key, self.settings.explore_ttl, fetch)

    def suggestions(self, keyword: str) -> list[dict[str, Any]]:
        key = _cache_key("sugg", kw=keyword, hl=self.settings.hl)
        return self._cached(key, self.settings.static_ttl, lambda: self._req().suggestions(keyword))

    def categories(self) -> dict[str, Any]:
        key = _cache_key("cats", hl=self.settings.hl)
        return self._cached(key, self.settings.static_ttl, lambda: self._req().categories())

    def trending_rss(self, geo: str, max_articles: int) -> list[dict[str, Any]]:
        key = _cache_key("rss", geo=geo, art=max_articles)
        return self._cached(key, self.settings.rss_ttl, lambda: self._fetch_rss(geo, max_articles))
