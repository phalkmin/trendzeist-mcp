# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-09

Hardening release: the same nine tools, now safer, more accurate and more predictable
under real-world network conditions.

### Fixed
- **Clean MCP channel.** Upstream cookie warnings no longer leak onto stdout and break
  the JSON-RPC stream on flaky networks.
- **Correct worldwide results.** A worldwide query after a country query no longer
  inherits the previous geo (and no longer poisons the worldwide cache).
- **No crash on empty data.** `compare_keywords` returns a proper "no data" answer
  instead of an opaque error.
- **Proxies everywhere.** `trending_now` now honours `TRENDZEIST_PROXIES`.
- **Real throttling.** `TRENDZEIST_MIN_INTERVAL` is enforced between every HTTP
  request (cookie, token, data, RSS), not once per tool call.
- **One shared client.** Concurrent first calls can no longer create duplicate clients
  with independent rate limiters.
- **Order-independent ranking.** `discover_topics` keeps the strongest growth signal
  for a topic regardless of seed order.
- **Intraday timestamps.** Hourly / `now *` series keep their time of day.
- **Stricter timeframes.** Custom date ranges are parsed as real dates (no more
  `2026-02-31`, reversed ranges, or dates in the future).
- MCP Registry `server.json` description now fits the schema limit.

### Security
- Disk cache switched from `pickle` to JSON — a tampered cache file can no longer
  execute code. Cache directory is created with `0700` permissions.

### Changed
- In-memory cache is bounded (256 entries) and expired disk entries are swept
  automatically, so long sessions no longer grow without limit.
- Cache files use a new `.json` format; old `.pkl` files are ignored and can be deleted.

## [0.1.0] - 2026-09-07

### Added
- Nine tools: `discover_topics`, `interest_over_time`, `compare_keywords`,
  `related_queries`, `related_topics`, `interest_by_region`, `suggest_keywords`,
  `trending_now`, `list_categories`.
- `blog_ideas_from_trends` prompt template.
- Request throttling, retries and a persistent on-disk TTL cache.
- Distribution: PyPI package, `python -m trendzeist_mcp`, Dockerfile, MCP Registry
  `server.json`, Glama manifest.
- CI: offline tests on Python 3.11–3.13 plus a weekly live canary against Google.
