import json
import pickle
import stat
import sys
import time

import pandas as pd
import pytest

from trendzeist_mcp import client as client_mod
from trendzeist_mcp.client import _TTLCache, default_cache_dir


def test_memory_only_cache_expires():
    c = _TTLCache(None)
    c.set("k", {"a": 1}, ttl=60)
    assert c.get("k") == {"a": 1}
    c.set("k2", 1, ttl=-1)
    assert c.get("k2") is None


def test_disk_cache_survives_new_instance(tmp_path):
    idx = pd.date_range("2026-01-04", periods=3, freq="W", name="date")
    df = pd.DataFrame({"x": [1, 2, 3], "isPartial": [False, False, True]}, index=idx)
    c1 = _TTLCache(tmp_path)
    c1.set("frame", df, ttl=60)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    json.loads(files[0].read_text())  # plain JSON, never pickle

    c2 = _TTLCache(tmp_path)  # simulates a server restart
    got = c2.get("frame")
    assert got is not None and got.equals(df)
    assert got.index.name == "date"


def test_disk_cache_roundtrips_nested_frames_and_none(tmp_path):
    payload = {"top": pd.DataFrame({"query": ["a"], "value": [9000]}), "rising": None}
    _TTLCache(tmp_path).set("rq", payload, ttl=60)
    got = _TTLCache(tmp_path).get("rq")
    assert got["rising"] is None and got["top"].equals(payload["top"])


def test_disk_cache_respects_expiry(tmp_path):
    c1 = _TTLCache(tmp_path)
    c1.set("k", "v", ttl=0.01)
    time.sleep(0.05)
    assert _TTLCache(tmp_path).get("k") is None
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize("garbage", [b"garbage", b'{"expires": null}', b'{"value": 1}', b"[]"])
def test_corrupt_disk_entry_is_ignored(tmp_path, garbage):
    c = _TTLCache(tmp_path)
    c.set("k", "v", ttl=60)
    for p in tmp_path.glob("*.json"):
        p.write_bytes(garbage)
    assert _TTLCache(tmp_path).get("k") is None


def test_planted_pickle_is_never_loaded(tmp_path):
    class Marker:
        def __reduce__(self):
            return (pytest.fail, ("pickle payload was executed",))

    c = _TTLCache(tmp_path)
    c.set("k", "v", ttl=60)
    for p in tmp_path.glob("*.json"):
        p.write_bytes(pickle.dumps((time.time() + 60, Marker())))
    assert _TTLCache(tmp_path).get("k") is None


def test_disk_writes_use_unique_temp_files(tmp_path, monkeypatch):
    """Two writers for the same key must never share a temp file (inode)."""
    import threading

    seen: list[str] = []
    real_mkstemp = client_mod.tempfile.mkstemp
    gate = threading.Barrier(2)

    def recording_mkstemp(*a, **kw):
        fd, name = real_mkstemp(*a, **kw)
        seen.append(name)
        gate.wait(timeout=5)  # both writers hold their temp file open simultaneously
        return fd, name

    monkeypatch.setattr(client_mod.tempfile, "mkstemp", recording_mkstemp)
    a, b = _TTLCache(tmp_path), _TTLCache(tmp_path)
    ta = threading.Thread(target=a.set, args=("k", "x" * 500, 60))
    tb = threading.Thread(target=b.set, args=("k", "y", 60))
    ta.start(); tb.start(); ta.join(); tb.join()

    assert len(set(seen)) == 2
    assert not list(tmp_path.glob("*.tmp"))  # no leftovers
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["value"] in ("x" * 500, "y")  # valid JSON, last-writer wins
    assert _TTLCache(tmp_path).get("k") in ("x" * 500, "y")


def test_memory_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_MEMORY_ENTRIES", 10)
    c = _TTLCache(None)
    for i in range(50):
        c.set(str(i), i, ttl=60)
    assert len(c._data) == 10
    assert c.get("49") == 49 and c.get("0") is None


def test_expired_disk_files_are_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "DISK_SWEEP_INTERVAL", 0)
    c = _TTLCache(tmp_path)
    for i in range(5):
        c.set(f"old{i}", i, ttl=-1)
    c.set("fresh", 1, ttl=60)
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cache_dir_is_private(tmp_path):
    d = tmp_path / "cache"
    _TTLCache(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_default_cache_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRENDZEIST_CACHE_DIR", "off")
    assert default_cache_dir() is None
    monkeypatch.setenv("TRENDZEIST_CACHE_DIR", str(tmp_path))
    assert default_cache_dir() == tmp_path
    monkeypatch.delenv("TRENDZEIST_CACHE_DIR")
    assert default_cache_dir() is not None
