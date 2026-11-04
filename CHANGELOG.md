# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.6.0] - 2026-11-04

"Knows *which one* and *what keeps coming back*". Additive; `schema_version` stays 3.

### Added
- **Ambiguous-seed flag** in `discover_topics` and `aeo_opportunities`: a seed whose
  exact title matches several Google entities of different types (Apple the company vs
  the fruit) gets `ambiguous: [{title, type, mid}]` and a `clarify` hint; the call lists
  them in `ambiguous_seeds`. Detection only — the seed is still queried as typed. Uses
  the cached suggestions endpoint; skipped once the call is rate limited; a failure is an
  `entities:<seed>` error, never a failed call.
- **`compare_keywords` editorial layer:** per-keyword `growth_3m`, `role` (leader /
  rising challenger / declining / niche / steady) and `content_implication`; top-level
  `winner` (`{keyword, reason}` — a rising challenger beats a falling leader) and a
  one-sentence `seo_interpretation`. `leader` is unchanged. When no keyword has measurable
  interest `winner` is `null` with a `winner_reason` (a tie at zero is not a target); an
  unknown recent slope is called unknown, never "not falling". `growth_note` explains a
  null `growth_3m`.
- **`recurring_topics(geo, days=90, min_episodes=3, seeds, limit=20)`:** archive-only list
  of topics with repeated breakout episodes, with first/last breakout, seeds and a
  *durable page* action; `coverage_days` + note when the archive is younger than the window.
  Like `trend_history`, it counts a topic only under *other* seeds.
- `TrendsClient.suggestions` now shapes Google's answer to `[{title, type, mid}]` (string
  or null fields); a malformed payload is an "Unexpected response" `TrendsError`, so the
  ambiguity check can never crash a composite.
- **Off-topic rising queries** (tutorial run 2026-11-03): Google's rising list for an
  AI-tool seed carried "how to bake a cake" and "best time to visit maldives" as the top
  breakouts (prompts pasted into the search box, rising from a tiny base). A rising query
  with no word in common with the seed, the other seeds of the call or the seed's top
  queries is now `off_topic: true` (+ `off_topic_reason`) in `related_queries`;
  `discover_topics` moves it to `filtered_out[]` (kind, growth, breakout flag, reason)
  and out of `topics`, `questions` and the score, with `counts.off_topic`,
  `seeds[].off_topic_count` and, from two such items, a `note` suggesting `category=`;
  `aeo_opportunities` drops it from the clusters and lists it in
  `seeds[].off_topic_dropped`. Nothing disappears silently. A legitimate neighbour that
  shares no word with the seed ("moka pot" under "espresso") lands there too: scan
  `filtered_out` before dismissing it.
- **Level-shift warning** in every trend summary (`interest_over_time`,
  `compare_keywords`, `compare_channels`, the seed `trend` of `discover_topics` /
  `aeo_opportunities`): a sustained step (4-week means differ by ≥ 30 %, every later
  window stays ≥ 15 % on the new side, the level before was steady and the new level is
  new) is reported as `level_shift {date, change_pct, before_mean, after_mean,
  post_shift_change_pct, growth_affected, warning}`; `interest_over_time` /
  `compare_keywords` add `warnings[]` grouping series that stepped the same way in the
  same week (`shared_by`, `likely_artifact` when two or more did). On 2026-07-12 five
  unrelated 12-month series stepped down 30-50 % in one week with no change in Search
  Console; growth figures spanning that week were partly artifact and nothing said so.
- **`mine_questions` realness ranking:** each question carries `score` (3 per prefix it
  surfaced under = `prefix_hits`; +2 for a verb, +3 for a comparison word, +1 for a top-3
  Autocomplete position, +1 when it adds two or more words) and the list is sorted by it.
  Completions with no verb that add at most one word to prefix + seed ("how long llms.txt
  last", "when llms.txt file") go to `filtered_out[]` (+ `filtered_count`) instead of the
  list: on the llms.txt run more than a third of the 40 "questions" were such padding.
- **`what_changed` first run:** a seed with no earlier snapshot gets `first_run: true` and
  `first_seen[]` (the current breakouts and rising queries with the action suffixed
  "(no history yet)") next to the baseline `reason`, so the first `weekly_review` is not
  an empty meeting. When a snapshot exists but none is `since_days` old, the comparison
  uses the oldest one and says so (`baseline_at`, `baseline_age_days`, `baseline_note`:
  "compared with one from 2 days ago (7 requested)"). The `weekly_review` prompt branches
  on both.

### Changed
- **Site coverage matches near-variants.** `site_coverage` folds plurals and a small
  synonym map (mac/macos, wp/wordpress, js/javascript, …) and matches tokens on a 4+
  letter prefix, so "claude code mac" against a site query "claude code macos" is
  `covered` (was `gap`, recommending a page the site already had). The partial threshold
  (token Jaccard ≥ 0.6) and exact behaviour are unchanged; `matched` still names the site
  query behind the verdict.
- **Spam names linked to a flagged domain.** When the suspect heuristic flags a domain-like
  rising query (`abrahamquirosvillalba .com`) it now also flags every rising query carrying
  the domain label as separate words ("… abraham quiros villalba"), even when fewer than
  three queries carry it. One-word labels (a brand) do not link.

### Fixed
- `trend_history.breakout_episodes` counted one breakout twice when two seeds saw it in
  the same week. Breakout runs that overlap, or start within 7 days of the previous
  episode's **last** breakout (under any seed, the same one included), are now one
  episode: a breakout that flickers off and back on between daily polls counts once, and
  adding a new seed to a live breakout no longer manufactures a "recurrence";
  `trend_history` and `recurring_topics` share the counter.
- Archive readers (`history`, `rising_items`, `snapshots`) are bounded by *now*: rows
  stamped in the future (clock correction, imported data) are no longer history, and
  `coverage_days` can no longer be negative. Rows whose text columns hold a non-string
  (SQLite TEXT is not strict) are skipped and reported via `_meta.archive_error` instead
  of raising. Same-second snapshots are read in insertion order, so an "empty then
  breakout" pair written in one second no longer splits an episode.
- `recurring_topics` skips items without enough breakout observations before touching
  snapshot headers (a 50k-row archive with no breakouts: 11 s → 0.2 s).
- `aeo_opportunities` no longer advises re-running with a `mid`: News, Wikipedia and
  Autocomplete take search text, not a Google entity id. Its `clarify` asks for a more
  specific phrase instead; `discover_topics` keeps the `mid` hint (Trends-only).
- (review round 2) `aeo_opportunities` crashed with an opaque `AttributeError` when the
  first exact-title entity Google returned had a `null` type (a legal payload: the
  ambiguity rule only needs two named types somewhere in the list). The `clarify` example
  now uses the first *named* type and is omitted when there is none.
- (review round 2) `compare_keywords` called a leader whose recent interest is **zero**
  (`direction_now: no_interest` — big early in the window, dead now) "highest interest
  and not falling" and recommended it for the title/H1. `no_interest` now ranks below
  `falling`: a rising challenger beats a dead leader, the leader's implication says its
  mean comes from earlier in the window, non-leaders with `no_interest` are `declining`
  (not `niche`), and `seo_interpretation` says "no recent interest". When every term's
  mean is below 5 (`WINNER_NOISE_FLOOR`, same floor as `compare_channels`) `winner.reason`
  is qualified as weak evidence instead of asserted.
- (review round 2) Docs: SKILL/README request budgets include the ambiguity lookup
  (`discover_topics` 1 + N plus one cached lookup per new seed; `aeo_opportunities`
  1 + 11 per seed); `recurring_topics` / `trend_history` guidance states that two
  breakouts are only separable when a poll between them saw the topic *not* breaking out
  (sparse polling undercounts); SKILL.md gains `ambiguous`, `winner` and `recurring_topics`
  steps; `server.json` / README archive consumer lists include `recurring_topics`.
- (review round 3) The ambiguity lookup runs in the server language (`TRENDZEIST_HL`),
  not the geo's, so a non-English seed was silently "not ambiguous". Seed reports now
  carry `ambiguity_note` when the check found nothing for a geo whose language differs
  from the server's, and when it was skipped because the call was rate limited (a seed
  served from cache during cooling previously lost its `ambiguous` field without a
  trace). Documented in README / SKILL.
- (review round 3) `compare_keywords`: every keyword unavailable now yields a
  `winner_reason` (was the only null-winner case without one); one keyword with data
  plus unavailable others is labelled "the only term with data (N unavailable), not a
  comparison" in `winner.reason` and `seo_interpretation`.
- (review round 3) `weekly_review` prompt step 6 calls `recurring_topics` first and
  step 5 tells the model to stop and ask on an `ambiguous` seed; `compare_keywords`
  description mentions the dead-leader rule and `winner_reason`.

## [0.5.0] - 2026-10-11

"Knows *when* and *what's worth it*": what changed since last week, seasonal publish-by
dates, which channel a topic grows on, and opportunities checked against your own site.
`schema_version` **2 → 3** (see Changed).

### Added
- **Observation archive** (`archive.py`, stdlib `sqlite3`, `archive.sqlite3` next to the
  disk cache). Every *fresh* fetch (never a cache hit) of related queries (top + clean
  rising), trend labels (`direction`, `direction_now`, `growth_3m`), trending searches and
  mined questions becomes one **snapshot** (uuid header row — an empty Google answer is a
  snapshot too) with normalised observation rows keyed by seed / geo / timeframe.
  Related-query snapshots store the *complete* lists Google returned, independent of the
  caller's display `limit`. Suspect rising queries never enter it. Normalisation is the
  same as the question de-duper (`Wi-Fi` ≡ `wi fi`). Opened lazily (cold start
  unchanged), WAL, one locked connection, 365-day retention (`TRENDZEIST_ARCHIVE_DAYS`),
  opt-out `TRENDZEIST_ARCHIVE=0`. Archive errors (including malformed stored timestamps)
  never fail a tool: they appear as `_meta.archive_error` and in `trendzeist_status.archive`.
- **`what_changed(seeds, geo, since_days=7)`**: refreshes current data, then compares with
  the newest snapshot at least `since_days` old. Per seed: `new_breakouts` and `promoted`
  (a known query that became a breakout; action `investigate now`), `new_rising`
  (`consider`), `cooled` (rising → top: `refresh existing post` when it was already a top
  query, else `review: no longer rising …`), `dropped` (`review: left Google's list …`),
  `rank_moves` (|Δ| ≥ 3), `direction_flip` on `direction_now`, `first_seen` per item,
  `baseline_at` / `current_at`, `current_items` / `baseline_items`. The tool never says
  "retire": leaving a relative, bounded list is not evidence of falling demand; guidance
  tells the model to confirm with `interest_over_time` first. An empty snapshot on either
  side yields a `reason`, not a mass drop. No baseline yet → `reason` with the rerun date.
- **`trend_history(keyword, geo, days=90)`**: archive-only (no requests). `first_seen`,
  `last_seen`, `breakout_episodes` (separate breakout runs — polling one breakout three
  times is one episode), `breakout_observations`, `rising_observations`,
  `seen_under_seeds`, `direction_timeline` keyed by timeframe, `latest_rank`,
  `questions_recorded`.
- **`seasonality(keyword, geo)`**: one `today 5-y` request → `monthly_index`,
  `peak_months` (top-2 in ≥ 3 full years and ≥ 120 % of the year's mean),
  `seasonal_strength`, `seasonal`, `next_peak`, `next_peak_in_days`, `publish_by`
  (−42 days or `"now"`), `in_peak_now` / `peak_month_now` when the current month is a
  peak, `insight`. Profile and strength use full years only (a partial year cannot
  overturn them); zero off-season interest is `seasonal: true` with `strength_note`
  instead of a division by zero. Fewer than 3 full years → `seasonal: null` + `reason`.
- **`compare_channels(keyword, geo, timeframe)`**: web, YouTube, News, Images, Shopping
  (five requests, web first; the rest are `skipped` on the first 429). Per channel
  `direction`, `direction_now`, `growth_3m`, `mean`, `latest`, `format_hint`;
  `fastest_channel` among channels with `mean ≥ 5`; `momentum`
  (`growing | flat | all_declining | unknown`) so a channel that is merely falling least is
  never described as growing; `insight`.
- **`site_queries` / `exclude_covered`** on `discover_topics` and `aeo_opportunities`
  (≤ 500 queries, each ≤ 200 chars). Topics / clusters get `coverage`
  `covered | partial | gap` (+ `matched`), `counts.covered/partial/gap`. A cluster is
  `covered` only when *every* question is covered (`coverage_counts` shows the mix), so
  `exclude_covered` can never hide an unanswered question.
- `discover_topics.topics[].momentum_seed`: which seed's `direction_now` fed a shared
  topic's score (the most favourable one, so results do not depend on seed order).
- **`trending_now(topic_filter="")`**: keeps trends whose title or headlines contain every
  word; `unfiltered_count`; empty → `reason` naming the filter.
- **`weekly_review(seeds, geo, site_queries)`** prompt: `what_changed` → `seasonality` →
  `compare_channels` → `aeo_opportunities` with `site_queries` → PUBLISH NOW / PLAN FOR
  <date> / REFRESH / RETIRE.
- `discover_topics.topics[]` now carry `score` / `score_breakdown`; `score_note` on both
  composites; `trendzeist_status.archive`; `_meta.archive_error`.
- Live canary probes for `seasonality` and `compare_channels`.

### Changed
- **Unified explainable score** (`formatters.opportunity_score`), shared by
  `discover_topics` and `aeo_opportunities`. `score_breakdown` is now
  `{component: {points, max, reason}}` with components `demand` 25, `momentum` 25
  (`direction_now`: rising 15 / stable·new 8 / falling 0 / unknown 5; +10 breakout,
  +5 rising query), `freshness` 15, `gap` 20 (neutral 10 without `site_queries`), `aeo` 15.
  The old `questions / trend / news / wikipedia` integer components are gone →
  **`schema_version` 3**. `aeo_opportunities[]` gains `heuristic: true`.
- `related_queries` / `discover_topics` / `aeo_opportunities` share one fetch path
  (`tools.fetch_related`, `tools.fetch_trend`) so every trend lookup feeds the archive.

## [0.4.1] - 2026-10-02

Hotfix for errors found while writing posts from 0.4.0 output (an agent quoted "rising"
for topics that had collapsed, and spam reached the breakout bucket). All changes are
additive: `schema_version` stays `2`.

### Fixed
- **`direction` no longer says "rising" for topics in steep decline.** The zero-base
  fallback (`base = 1.0`) is gone: a keyword with no interest in the first third of the
  window is now `new`, never `rising`. The `insight` sentence mentions the recent move when
  it disagrees with the whole-window label ("rose 108% …, but fell 59% in the last quarter
  of the period (falling)").
- **`aeo_opportunities` scores the recent slope** (`direction_now`, below) instead of the
  whole-window `direction`, so a topic that fell 70% since its peak no longer collects
  +20 "rising" points. `score_note` updated.
- **Spam no longer passes as breakout.** Rising queries that carry the same 2-3-word
  marker across ≥ 3 unrelated items (absent from the top list) or that look like a domain
  are flagged `suspect: true` / `suspect_reason`. `related_queries` keeps them in `rising`
  but out of `questions`; `discover_topics` moves them to `suspect[]` (plus
  `counts.suspect`, `seeds[].suspect_count`); `aeo_opportunities` ignores them entirely.
  `is_breakout` still reports Google's number.
- **`mine_questions` no longer returns completions of a partial word** ("wordpress ai" →
  "wordpress airplay"). Autocomplete queries are sent with a trailing space, and a
  suggestion is kept only if the seed survives as whole words.
- **`growth_3m` is now a number in composites.** `discover_topics` and `aeo_opportunities`
  fetch the seed trend on `today 12-m` regardless of the related-queries `timeframe`
  (echoed as `query.trend_timeframe`; same single request). Elsewhere a `null` growth
  field for coverage reasons carries a `growth_note` naming the timeframe to use.
- **A 429 on the trend-context step no longer aborts `discover_topics` /
  `aeo_opportunities`.** Related queries for the seeds are still attempted; cooling-down
  (skipping uncached seeds) only starts when a seed's own request is refused. The skip
  message now says "Retry in ~60 s with 1-2 seeds."
- **Error strings no longer carry the full Trends URL and session token.** Upstream
  exceptions are reduced to "HTTP 429 from trends.google.com after retries" (or a
  ≤ 200-char message with query strings stripped); the raw exception is logged at debug.
- "when … released / launched / comes out" is tagged `news` instead of `how-to`.
- Legacy pre-0.2 `*.pkl` cache files are deleted on the next disk sweep.

### Added
- `direction_now` on every per-keyword summary (`interest_over_time`,
  `compare_keywords.ranking[]`, `aeo_opportunities[].trend`, `discover_topics.seeds[].trend`,
  `wiki_attention.pageviews`): last quarter of the window vs. the quarter before it, same
  labels as `direction`. `new` is a new `direction` value (born inside the window).
- `growth_note`, `suspect`, `suspect_reason`, `suspect[]`, `suspect_count`,
  `query.trend_timeframe` fields as described above.
- README "Two trend labels" / "Suspect rising queries" conventions; SKILL.md tells agents to
  decide on `direction_now`, to ignore `suspect[]` and to open a session with 1-2 seeds.
- AGENTS.md: git (branches, commits, tags) is the maintainer's job, never the agent's.

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
