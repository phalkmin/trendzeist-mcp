# Changelog

Notable user-facing changes are documented here. The project uses
[Semantic Versioning](https://semver.org/).

## [0.6.2] - 2026-11-18

Correctness and reliability fixes. `schema_version` stays 3.

### Changed
- Stricter site coverage: broader head terms inside longer site queries are `partial`,
  not `covered`, so `exclude_covered` does not hide unanswered intent.
- During a 429 cooldown, composites use cached or stale related data when available
  and report the actual retry delay for skipped work. Wikipedia remains available in AEO.
- Trends question and angle detection now supports the same pt, es, fr, de, it, pl and
  nl locales as Autocomplete. Unsupported locales report their English-prefix fallback.
- Clearer `related_queries` parameter schemas and usage guidance. Registry metadata now
  correctly describes automatic language selection when `TRENDZEIST_HL` is unset.

### Fixed
- More accurate editorial output for series returning to zero, `no_interest` momentum,
  yes/no verdict questions, early level shifts and channels without growth data.
- Seasonality now requires all 12 calendar months in each full year.
- Archive writes roll back on failure; malformed rows and incomplete snapshots no longer
  produce invented changes. Future snapshots cannot become comparison baselines.
- Empty upstream responses no longer split breakout episodes. Fresh and stale question
  results are not mixed in the archive, and flattened trend labels are not recorded.
- `what_changed`, `trend_history` and `recurring_topics` reapply spam/off-topic cleanup
  to legacy rows. Comparisons abstain when cleanup leaves either side empty.
- Duplicate questions retain breakout evidence regardless of seed order.
- Trending filters inspect up to ten headlines independently of the display limit.
- Malformed Wikipedia and Trends payloads, including cached payloads, now return
  actionable tool errors instead of opaque crashes. Missing and non-finite series values
  are handled without producing invalid JSON.
- Geo-specific explore calls no longer leak their language into suggestions/categories
  or cache those results under the wrong locale. Stale related data is used without a
  network attempt only while the Trends source itself is cooling down.
- Corrected AEO's reported worst-case request budget and retry delays when a longer
  cooldown is already active. Invalid numeric environment values now return tool errors.
- Transport failures are no longer mislabeled as rate limiting, and disabling cooldown
  no longer skips later work. Localized mining rejects ambiguous mid-query matches;
  multi-part Wikipedia editions such as `zh-min-nan` are accepted.

## [0.6.1] - 2026-11-04

### Added
- Localized question prefixes and verdict-angle detection for supported languages.
- Post-429 cooldowns with `Retry-After`, source status and per-result rate-limit metadata.
- Optional stale-cache fallback, retained for seven days and explicitly labeled.
- Jittered request pacing and detailed level-shift warnings across trend tools.

### Fixed
- Improved filtering of spam and off-topic queries in results and archived history.
- More conservative site-coverage matching and correct Jaccard scores.
- Better handling of level shifts, expired disk cache entries and cooldown cache hits.

## [0.6.0] - 2026-11-04

### Added
- Ambiguous-seed detection in discovery and AEO, with clarification guidance.
- `compare_keywords` roles, content implications, winner recommendation and explanation.
- `recurring_topics` for repeated breakout episodes in the local archive.
- Off-topic filtering, localized realness ranking for questions and actionable first-run
  `what_changed` results.

### Changed
- Site coverage matches common spelling variants, plurals and technical synonyms.
- Domain-linked spam names are flagged together.

### Fixed
- Breakout episodes are deduplicated across seeds and short polling intervals.
- Archive readers ignore malformed or future-dated rows and handle same-second snapshots.
- AEO guidance no longer recommends entity IDs to sources that accept only text.
- `compare_keywords` handles dead leaders, unavailable terms and low-interest ties accurately.

## [0.5.0] - 2026-10-11

`schema_version` changed from 2 to 3 because opportunity score components changed.

### Added
- Local observation archive plus `what_changed` and `trend_history`.
- `seasonality`, `compare_channels` and `weekly_review`.
- Site-coverage gaps and `exclude_covered` for discovery and AEO.
- Explainable 0-100 opportunity scores shared by both composites.
- Topic filtering for `trending_now` and scoring explanations for discovered topics.

### Changed
- Trends tools share recording paths so fresh observations consistently feed the archive.
- Composite trends always use a 12-month window for recent direction and growth context.

## [0.4.1] - 2026-10-02

### Added
- `direction_now` for recent momentum and explicit `new` trend labels.

### Fixed
- More accurate falling/new trends and recent-slope scoring in AEO.
- Spam-like breakout filtering and rejection of partial-word Autocomplete completions.
- Composite trend growth now uses a 12-month window.
- Rate-limit failures allow partial composite results, and upstream error text no longer
  exposes session tokens or full query URLs.

## [0.4.0] - 2026-09-29

### Added
- `aeo_opportunities`, `news_coverage`, `wiki_attention` and `trendzeist_status`.
- `answer_brief` and `content_brief` prompts, angle-specific citability hints and `SKILL.md`.
- Separate Wikimedia throttling and configurable cache/pacing settings.

### Changed
- Shared source plumbing for Trends, Autocomplete, News and Wikipedia.
- Python library callers now use `Hub` and source-specific attributes.

### Fixed
- Wikipedia results distinguish exact-title articles from related suggestions.
- Google News editions remain consistent with the requested market.
- Invalid cache and formatter environment values produce actionable errors.

## [0.3.0] - 2026-09-15

### Added
- `mine_questions`, question extraction and title-angle tagging.
- 3/12-month growth windows and plain-English trend insights.
- Geo-derived request language and per-call schema/cache metadata.

### Changed
- Clamped limits and empty results are explained.
- Cookie failures and transient upstream errors have clearer tool messages.

## [0.2.2] - 2026-09-12

### Fixed
- Prevented cross-query widget contamination and worldwide queries inheriting prior geo.
- Applied throttling to cookie refreshes and deduplicated concurrent cache misses.
- Made disk cache writes process-safe and cache keys locale-aware.
- Rejected unsupported regional resolutions and improved partial results after 429.

## [0.2.1] - 2026-09-09

- Added MCP Registry ownership metadata. No functional changes.

## [0.2.0] - 2026-09-09

### Added
- Persistent JSON disk cache with bounded memory use and restrictive directory permissions.

### Fixed
- Clean stdio output, correct worldwide queries, empty comparisons and proxy support.
- Request-level throttling, shared clients, deterministic ranking and intraday timestamps.
- Strict custom-date validation and clearer MCP Registry metadata.

## [0.1.0] - 2026-09-07

- Initial release with nine Google Trends tools, an ideation prompt, throttling,
  persistent caching and PyPI/Docker/MCP Registry distribution.
