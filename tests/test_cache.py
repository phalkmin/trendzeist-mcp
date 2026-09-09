import time

import pandas as pd

from trendzeist_mcp.client import _TTLCache, default_cache_dir


def test_memory_only_cache_expires():
    c = _TTLCache(None)
    c.set("k", {"a": 1}, ttl=60)
    assert c.get("k") == {"a": 1}
    c.set("k2", 1, ttl=-1)
    assert c.get("k2") is None


def test_disk_cache_survives_new_instance(tmp_path):
    df = pd.DataFrame({"x": [1, 2]})
    c1 = _TTLCache(tmp_path)
    c1.set("frame", df, ttl=60)
    assert len(list(tmp_path.glob("*.pkl"))) == 1

    c2 = _TTLCache(tmp_path)  # simulates a server restart
    got = c2.get("frame")
    assert got is not None and got.equals(df)


def test_disk_cache_respects_expiry(tmp_path):
    c1 = _TTLCache(tmp_path)
    c1.set("k", "v", ttl=0.01)
    time.sleep(0.05)
    assert _TTLCache(tmp_path).get("k") is None
    assert list(tmp_path.glob("*.pkl")) == []


def test_corrupt_disk_entry_is_ignored(tmp_path):
    c = _TTLCache(tmp_path)
    c.set("k", "v", ttl=60)
    for p in tmp_path.glob("*.pkl"):
        p.write_bytes(b"garbage")
    assert _TTLCache(tmp_path).get("k") is None


def test_default_cache_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRENDZEIST_CACHE_DIR", "off")
    assert default_cache_dir() is None
    monkeypatch.setenv("TRENDZEIST_CACHE_DIR", str(tmp_path))
    assert default_cache_dir() == tmp_path
    monkeypatch.delenv("TRENDZEIST_CACHE_DIR")
    assert default_cache_dir() is not None
