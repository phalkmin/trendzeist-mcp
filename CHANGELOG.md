# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-09-29

"The AEO release": multi-source and still keyless. Find the questions people ask — and
LLMs answer — then make your site the source they cite.

### Added
- **`aeo_opportunities(seeds, geo, timeframe, limit)`** — composite. Question-shaped
  searches from Trends related queries plus a capped 5-prefix Autocomplete pass,
  deduplicated and clustered by seed × title angle; each cluster scored (transparent
  heuristic with `score_breakdown`) using the seed's trend direction, Google News coverage
  (`top_publishers` to quote or pitch), Wikipedia presence and `citability_hints`. Partial
  failures per source; Google requests stop at the first 429 while Wikipedia continues.
- **`news_coverage(topic, geo, limit)`** — Google News search RSS: headlines, publisher
  frequency table with shares, cumulative recency histogram, `coverage` label.
- **`wiki_attention(topic, lang, days)`** — Wikipedia search + Wikimedia pageviews: best
  article, daily views (7-90 days), direction, growth, insight, `has_article` gap flag.
- **`trendzeist_status()`** — per-source health (live / error / idle with last error), cache
  usage and effective settings. Makes no requests.
- **Prompts** `answer_brief(question, geo)` (GEO-paper structure: 40-60-word answer,
  statistic, quote, sources, FAQ, schema; evidence recipe picked from the question's angle)
  and `content_brief(topic, audience, geo)` (titles, H2 outline, FAQ, regions, publishers).
- **`SKILL.md`** — agent skill describing the discover → mine → opportunities → validate →
  brief workflow and the rate-limit budget rules.
- **Citability hints** table (`formatters.CITABILITY_HINTS`) — angle → evidence type,
  structure and what to cite, from Aggarwal et al., *GEO* (KDD 2024).
- **Env-tunable** `TRENDZEIST_EXPLORE_TTL`, `TRENDZEIST_RSS_TTL`, `TRENDZEIST_STATIC_TTL`,
  `TRENDZEIST_MAX_MEMORY_ENTRIES`, `TRENDZEIST_MAX_SERIES_POINTS`,
  `TRENDZEIST_WIKI_MIN_INTERVAL`.
- `mine_questions` accepts a `prefixes` subset (library use; the MCP tool is unchanged).
- Live canary probes Google News, Wikipedia and `trendzeist_status`.

### Fixed
- Wikipedia search results with a different title no longer count as confirmed topic
  articles or citation anchors in `wiki_attention` / `aeo_opportunities`; surface them
  as related suggestions instead. Inspect up to five search hits for an exact title,
  and encode reserved characters in article URLs. `schema_version` is now `2` because
  `has_article` has a stricter meaning; the package release version is unchanged.
- Google News keeps its country edition (`hl`, `gl`, `ceid`) consistent when
  `TRENDZEIST_HL` pins a different Trends UI language.
- Reject non-positive or non-integer cache TTL, memory-entry and series-point env values
  with actionable tool errors instead of crashing (including during hub startup).


### Changed
- **Source seam (internal).** `client.py` now holds the shared plumbing (`Source` base
  class with per-host `Lane` throttling, shared cache, error mapping and health) and the
  Google Trends implementation; `sources.py` adds `AutocompleteSource`, `GoogleNewsSource`,
  `WikipediaSource` and the `Hub` that tools receive. Google hosts share one rate lane;
  Wikimedia has its own. Error messages now name the failing source ("Google News request
  failed", "Wikipedia rate limit reached").
- Trending RSS and Autocomplete 429s are reported as rate-limit errors (previously a
  generic request failure).
- Server instructions and README describe the multi-source workflow; `schema_version`
  remained `1` at initial 0.4.0 implementation (fields were only added; see Unreleased fix).

### Library API (Python callers only)
- `TrendsClient(settings, cache_dir)` → `Hub(settings, cache_dir)`; tools take a `Hub` and
  read `hub.trends`, `hub.autocomplete`, `hub.news`, `hub.wikipedia`.
  `TrendsClient.autocomplete()` → `hub.autocomplete.suggest()`.

### Tests
- `tests/test_sources.py` (News RSS parsing, Wikipedia search/pageviews/404, lanes, UA);
  seam tests (lane independence, health states, `_get` mapping); fake-hub tests for every
  new tool and both prompts. 134 offline tests.

## [0.3.0] - 2026-09-15

"From data to ideas": the same nine tools plus one, and the output now does the
editorial thinking — questions people ask, title angles, plain-English trend insights,
growth windows, results in the market's language.

### Added
- **`mine_questions(seed, geo, limit)`** — new tool. Expands a seed through Google
  Autocomplete with 15 question prefixes (`how to`, `why`, `what is`, `vs`, …), dedupes
  by normalised form, tags each question with a title angle and reports `by_angle`
  counts. Budget is spread across prefixes, stops as soon as `limit` is met, returns
  partial results (`partial`, `errors`) when Google starts refusing. Throttled through
  the shared client, 24 h cache, honours proxies. Default 30, max 100.
- **`questions[]`** on `related_queries` and `discover_topics`: question-shaped related
  searches (rising first, deduplicated across seeds, breakout flag carried over).
- **Title angle** (`angle`: `how-to | comparison | listicle | definition | news | null`)
  on every related query, discovered topic, mined question and trending item.
- **`growth_3m` / `growth_12m`** in every `interest_over_time` / `compare_keywords`
  summary: % change of the last window vs. the window before; `null` when the
  timeframe is too short.
- **`insight`** per keyword: one plain-English sentence ("Interest in 'x' rose 40%
  between the first and last third of the period, peaking at 100 on 2026-09-05
  (rising).") so the model does not have to do arithmetic.
- **Language follows the market.** When `TRENDZEIST_HL` is unset, `hl` is derived
  from the request's `geo` (`BR` → `pt-BR`, ~50 countries; unknown → `en-US`) for
  explore and Autocomplete calls, and echoed as `query.hl`. Set `TRENDZEIST_HL` to pin
  one language as before.
- **`schema_version`** (`1`) on every tool result.
- **`_meta`** on every tool result: `requests_made`, `cache_hit`, `cache_hits`,
  `cache_misses` for that call.
- Live canary probes `mine_questions`.

### Changed
- Silent behaviours are now reported: clamped `limit` / `max_articles` / `max_per_seed`
  add a `note`; empty `interest_by_region` and `trending_now` results add a `reason`
  (`interest_by_region` also gains `available`).
- Cookie fetch failures (network error or HTTP ≥ 400) now raise a clear, retryable
  tool error instead of continuing cookieless and failing confusingly later. (0.2.2
  only did this for HTTP 429.)
- Server instructions and the `blog_ideas_from_trends` prompt cover the new fields,
  `mine_questions`, and `gprop='news' | 'youtube'` as authority channels.
- README: AEO positioning (AI-visibility *measurement* is explicitly out of scope),
  output conventions, `gprop` channels, RSS/Autocomplete use `proxies[0]`.

### Tests
- Coverage for every `_guarded` error branch, the empty RSS feed, discovery when
  `interest_over_time` fails non-retryably, the geo→hl mapping, Autocomplete
  fetch/cache/429, and per-call meta counters. 101 offline tests.

## [0.2.2] - 2026-09-12

Correctness release driven by an adversarial code review. Same nine tools.

### Fixed
- **No cross-query data contamination.** The reused upstream session kept the previous
  query's `interest_over_time` / `interest_by_region` widgets when Google's token
  response omitted them, so a later query could fetch the *old* keyword's data, label it
  with the new keywords and cache it under the new key. Widgets are now cleared before
  every `build_payload`.
- **Throttle really covers every request in proxy mode.** Upstream refreshes the cookie
  inside `_get_data` and fires the data request immediately after; the interval is now
  enforced between the cookie fetch and the data request.
- **Cookie endpoint 429 is a rate-limit error**, not a silent "continue without cookie";
  other HTTP errors on the cookie fetch are logged instead of ignored.
- **Concurrent identical cache misses fetch once.** Callers waiting on the network lock
  re-check the cache before hitting Google.
- **Disk cache writes use unique temp files.** Two processes sharing the cache dir could
  hold the same `.tmp` inode and corrupt the published file after the first rename.
- **Locale-aware cache keys.** Explore caches now include `hl`/`tz`; two servers with
  different `TRENDZEIST_HL` sharing the disk cache no longer see each other's results.
- **`interest_by_region` no longer claims a resolution it did not request.** Google only
  applies CITY/DMA for `geo='US'` (or worldwide) and REGION within a country; other
  combinations are now rejected with an actionable message instead of silently
  returning default-resolution data.
- **`discover_topics` after a 429:** later seeds are no longer dropped silently. Seeds
  already in cache are still served; uncached ones are reported as `skipped`. A
  rate-limited `interest_over_time` step now also stops further requests.
- Live canary honours `TRENDZEIST_*` env (it hard-coded a 3 s interval before).

### Changed
- `publish.yml` runs the offline suite and a server-startup check on the tagged commit
  and verifies `server.json` versions before publishing.

## [0.2.1] - 2026-09-09

### Added
- MCP Registry ownership marker (`mcp-name: io.github.phalkmin/trendzeist-mcp`) in the
  README so the registry can verify the PyPI package. No functional changes.

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
