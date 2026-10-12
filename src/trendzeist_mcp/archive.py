"""Observation archive (0.5.0, roadmap C10): what the tools saw, when.

The cache answers "do I need to fetch again?"; the archive answers "what changed
since last week?". Every *fresh* fetch (never a cache hit) of related queries,
trend labels, trending searches or mined questions becomes one **snapshot**
(header row, unique id, even when the response was empty) plus zero or more
normalised **observations** in a SQLite file inside the cache directory.

Design rules:

- stdlib ``sqlite3`` only, opened lazily on first use (cold start unaffected),
  WAL mode, one connection guarded by a lock (tools run on worker threads).
- **Never raises.** Every public method swallows ``sqlite3`` / ``OSError`` /
  malformed stored data, logs it, remembers it in ``last_error`` and lets the
  tool finish; the error is surfaced as ``_meta.archive_error`` and in
  ``trendzeist_status``.
- Snapshots are identified by a uuid, never by timestamp, so two fetches in the
  same second (or two processes sharing the cache dir) can never merge.
- Related-query snapshots hold the *complete* clean lists Google returned,
  independent of the caller's display ``limit``; readers compare like with like.
- Opt-out with ``TRENDZEIST_ARCHIVE=0``; rows older than
  ``TRENDZEIST_ARCHIVE_DAYS`` (default 365) are pruned once per process.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .formatters import normalise_query

logger = logging.getLogger(__name__)

ARCHIVE_FILENAME = "archive.sqlite3"
TRENDING_SEED = "__trending__"
# Snapshot groups (what was fetched) and observation kinds inside them.
GROUP_RELATED = "related"      # kinds: top_query (status None) / rising_query (breakout|rising)
GROUP_TREND = "trend"          # kinds: direction / direction_now (status) / growth_3m (value)
GROUP_TRENDING = "trending"    # kind: trending (value = traffic estimate)
GROUP_QUESTIONS = "questions"  # kind: question (status = angle)
TREND_KINDS: tuple[str, ...] = ("direction", "direction_now", "growth_3m")
RELATED_KINDS: tuple[str, ...] = ("rising_query", "top_query")
_TS = "%Y-%m-%dT%H:%M:%SZ"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    grp         TEXT NOT NULL,
    seed        TEXT NOT NULL,
    geo         TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_seed ON snapshots (seed, geo, grp, timeframe, observed_at);
CREATE TABLE IF NOT EXISTS observations (
    snapshot_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    geo         TEXT NOT NULL,
    seed        TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    item        TEXT NOT NULL,
    value       REAL,
    rank        INTEGER,
    status      TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_snapshot ON observations (snapshot_id);
CREATE INDEX IF NOT EXISTS ix_obs_seed ON observations (seed, geo, kind, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_item ON observations (item, geo, observed_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS)


def parse_ts(text: str | None) -> datetime | None:
    """Stored timestamp -> aware datetime, ``None`` when malformed (never raises)."""
    if not text:
        return None
    try:
        return datetime.strptime(text, _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _norm(text: str | None) -> str:
    """Same canonical form as the question de-duper, so lookups match everywhere."""
    return normalise_query(text or "")


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_traffic(traffic: Any) -> float | None:
    """'20K+' -> 20000; '1M+' -> 1_000_000; numbers pass through."""
    if traffic is None:
        return None
    if isinstance(traffic, (int, float)):
        return float(traffic)
    text = str(traffic).strip().upper().rstrip("+").replace(",", "")
    mult = 1.0
    if text.endswith("K"):
        mult, text = 1_000.0, text[:-1]
    elif text.endswith("M"):
        mult, text = 1_000_000.0, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


class Archive:
    """Append-only observation store; see module docstring for guarantees."""

    def __init__(self, path: Path | None, *, enabled: bool = True, retention_days: int = 365):
        self.enabled = enabled and path is not None
        self.path = path if self.enabled else None
        self.retention_days = retention_days
        self.last_error: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._pruned = False
        self._local = threading.local()

    # ---- plumbing -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        """Open (once) and migrate; ``None`` when disabled."""
        if not self.enabled or self.path is None:
            return None
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def _fail(self, op: str, exc: BaseException) -> None:
        self.last_error = f"{op}: {exc.__class__.__name__}: {exc}"[:200]
        self._local.error = self.last_error
        logger.warning("archive %s failed: %s", op, exc)

    def begin_call(self) -> None:
        """Forget the previous tool call's error (per thread)."""
        self._local.error = None

    def call_error(self) -> str | None:
        return getattr(self._local, "error", None)

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        if self._pruned:
            return
        self._pruned = True
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).strftime(_TS)
        conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
        conn.execute("DELETE FROM snapshots WHERE observed_at < ?", (cutoff,))
        conn.commit()

    def _write(
        self, group: str, seed: str, geo: str, timeframe: str, rows: Iterable[tuple]
    ) -> str | None:
        """Insert one snapshot (header + rows); returns ``snapshot_id`` or ``None``.

        ``rows`` are ``(kind, item, value, rank, status)``. An empty ``rows`` still
        writes the header: "Google returned nothing" is an observation too.
        """
        if not self.enabled:
            return None
        observed_at = _now()
        seed_n = _norm(seed)
        snapshot_id = uuid.uuid4().hex
        payload = [
            (snapshot_id, observed_at, geo, seed_n, timeframe, kind, _norm(item), value, rank, status)
            for kind, item, value, rank, status in rows
        ]
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return None
                self._prune_locked(conn)
                conn.execute(
                    "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                    (snapshot_id, observed_at, group, seed_n, geo, timeframe, len(payload)),
                )
                if payload:
                    conn.executemany(
                        "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)", payload
                    )
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self._fail("write", exc)
            return None
        return snapshot_id

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        if not self.enabled:
            return []
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return []
                return conn.execute(sql, params).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self._fail("read", exc)
            return []

    # ---- writers ---------------------------------------------------------------

    def record_related(
        self,
        seed: str,
        geo: str,
        timeframe: str,
        top: list[dict[str, Any]],
        rising_clean: list[dict[str, Any]],
    ) -> str | None:
        """Complete top + *clean* rising lists (suspects never enter the archive).

        Callers must pass the full lists Google returned, not a display-limited
        slice, so that two snapshots are comparable regardless of ``limit``.
        """
        rows: list[tuple] = []
        for rank, it in enumerate(top, 1):
            if it.get("name"):
                rows.append(("top_query", it["name"], _num(it.get("value")), rank, None))
        for rank, it in enumerate(rising_clean, 1):
            if it.get("suspect") or not it.get("name"):
                continue
            status = "breakout" if it.get("is_breakout") else "rising"
            rows.append(("rising_query", it["name"], _num(it.get("value")), rank, status))
        return self._write(GROUP_RELATED, seed, geo, timeframe, rows)

    def record_trend(
        self, keyword: str, geo: str, timeframe: str, summary: dict[str, Any]
    ) -> str | None:
        """direction / direction_now / growth_3m for one keyword from an IOT summary."""
        if not summary or not summary.get("available"):
            return None
        rows = [
            ("direction", "", None, None, summary.get("direction")),
            ("direction_now", "", None, None, summary.get("direction_now")),
            ("growth_3m", "", _num(summary.get("growth_3m")), None, None),
        ]
        return self._write(GROUP_TREND, keyword, geo, timeframe, rows)

    def record_trending(self, geo: str, trends: list[dict[str, Any]]) -> str | None:
        rows = [
            ("trending", t["title"], _parse_traffic(t.get("traffic")), rank, None)
            for rank, t in enumerate(trends, 1)
            if t.get("title")
        ]
        return self._write(GROUP_TRENDING, TRENDING_SEED, geo, "now", rows)

    def record_questions(self, seed: str, geo: str, questions: list[dict[str, Any]]) -> str | None:
        rows = [
            ("question", q["question"], None, rank, q.get("angle"))
            for rank, q in enumerate(questions, 1)
            if q.get("question")
        ]
        return self._write(GROUP_QUESTIONS, seed, geo, "autocomplete", rows)

    # ---- readers ---------------------------------------------------------------

    def snapshots(self, seed: str, geo: str, group: str, timeframe: str) -> list[dict[str, Any]]:
        """Snapshot headers for a seed, newest first: ``{id, observed_at, item_count}``."""
        rows = self._query(
            "SELECT snapshot_id, observed_at, item_count FROM snapshots WHERE seed=? AND geo=? "
            "AND grp=? AND timeframe=? ORDER BY observed_at DESC, rowid DESC",
            (_norm(seed), geo, group, timeframe),
        )
        return [{"id": i, "observed_at": o, "item_count": n} for i, o, n in rows]

    def observations(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT kind, item, value, rank, status FROM observations WHERE snapshot_id=? "
            "ORDER BY kind, rank",
            (snapshot_id,),
        )
        return [
            {"kind": k, "item": i, "value": val, "rank": r, "status": s}
            for k, i, val, r, s in rows
        ]

    def latest_and_baseline(
        self, seed: str, geo: str, group: str, timeframe: str, since_days: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Newest snapshot header and the newest one at least ``since_days`` older.

        Snapshots with an unparsable timestamp are skipped (and reported).
        """
        snaps = [s for s in self.snapshots(seed, geo, group, timeframe) if parse_ts(s["observed_at"])]
        if not snaps:
            return None, None
        latest = snaps[0]
        cutoff = parse_ts(latest["observed_at"]) - timedelta(days=since_days)  # type: ignore[operator]
        baseline = next((s for s in snaps[1:] if parse_ts(s["observed_at"]) <= cutoff), None)  # type: ignore[operator]
        return latest, baseline

    def first_seen(self, item: str, geo: str) -> str | None:
        key = _norm(item)
        rows = self._query(
            "SELECT MIN(observed_at) FROM observations WHERE (item=? OR seed=?) AND geo=?",
            (key, key, geo),
        )
        return rows[0][0] if rows else None

    def history(self, keyword: str, geo: str, days: int) -> list[dict[str, Any]]:
        """Every observation where the keyword is the seed *or* the item, oldest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TS)
        key = _norm(keyword)
        rows = self._query(
            "SELECT snapshot_id, observed_at, seed, timeframe, kind, item, value, rank, status "
            "FROM observations WHERE geo=? AND observed_at>=? AND (seed=? OR item=?) "
            "ORDER BY observed_at, snapshot_id",
            (geo, cutoff, key, key),
        )
        keys = ("snapshot_id", "observed_at", "seed", "timeframe", "kind", "item", "value", "rank", "status")
        return [dict(zip(keys, row)) for row in rows]

    def status(self) -> dict[str, Any]:
        """For ``trendzeist_status``; never raises."""
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "path": str(self.path) if self.path is not None else None,
            "retention_days": self.retention_days,
            "snapshots": None,
            "rows": None,
            "oldest": None,
            "newest": None,
            "last_error": self.last_error,
        }
        if not self.enabled:
            out["reason"] = "disabled (TRENDZEIST_ARCHIVE=0 or cache dir off)"
            return out
        if self._conn is None and self.path is not None and not self.path.exists():
            out["snapshots"] = out["rows"] = 0  # nothing written yet; do not create the file to count
            return out
        rows = self._query(
            "SELECT (SELECT COUNT(*) FROM snapshots), COUNT(*), MIN(observed_at), MAX(observed_at) "
            "FROM observations",
            (),
        )
        if rows:
            out["snapshots"], out["rows"], out["oldest"], out["newest"] = rows[0]
        out["last_error"] = self.last_error
        return out
