---
name: trendzeist-aeo-content
description: Use when planning blog posts or answer-engine (AEO) content with the trendzeist MCP server. Covers the discover -> mine questions -> find AEO opportunities -> validate -> brief workflow, how to read the outputs, and how to stay under Google's rate limits.
---

# Content ideation and AEO with trendzeist-mcp

trendzeist-mcp exposes Google Trends, Google Autocomplete, Google News and Wikipedia
as MCP tools. No API key. Every result carries `schema_version` and `_meta`
(`requests_made`, `cache_hit`); every silent adjustment is reported as `note` or `reason`.

## Workflow

1. **Discover** — `discover_topics(seed_keywords=[1-5 seeds], geo, timeframe="today 3-m")`.
   Start a session with 1-2 seeds (Google 429s the first burst hardest). Read `topics[]`
   (signal: breakout > rising > evergreen; `angle`: how-to | comparison | listicle |
   definition | news) and `questions[]`. Ignore `suspect[]`: those rising queries look
   like injected spam and must not become posts.
2. **Mine questions** — `mine_questions(seed, geo, limit=30)` on the 1-2 strongest seeds.
   Up to 16 throttled requests; cached 24 h. Native-language questions need a seed in that language.
3. **Find AEO opportunities** — `aeo_opportunities(seeds, geo, limit=10)`. Each opportunity is a
   cluster of questions for one seed and angle with `score` (transparent heuristic, see
   `score_breakdown`), `trend`, `news` (coverage + `top_publishers`), `wikipedia`
   (`has_article`, direction) and `citability_hints` (evidence that makes the answer citable).
   Follow `brief`.
4. **Validate** — `interest_over_time` / `compare_keywords` with `timeframe="today 12-m"`.
   Use `summary.<kw>.insight`, `growth_3m`, `growth_12m` verbatim; never do the arithmetic yourself.
   `direction` is the whole window; **`direction_now` is the recent slope and decides
   "write about it now"**. `direction: new` + `direction_now: falling` = a topic that
   spiked and faded; say so instead of calling it rising. A `growth_note` explains a
   `null` growth field (too short a timeframe).
5. **Brief** — prompts `content_brief(topic, audience, geo)` for a full outline or
   `answer_brief(question, geo)` for one citable answer (direct 40-60-word answer, one
   statistic, one quote, sources, FAQ, schema). `news_coverage` gives dated headlines and
   publishers to quote or pitch; `wiki_attention` gives the definition anchor or flags a gap.

## Reading the signals

- `breakout` = brand-new or exploding demand (time-sensitive). `rising` = growing.
  `evergreen` = consistently popular (pillar content). A rising item with `suspect: true`
  (one unrelated name repeated across breakouts, or a `.com`) is manipulation, not demand.
- `coverage` in `news_coverage`: none / low / moderate / high over 30 days. High = crowded;
  low with rising search interest = open field.
- `has_article=false` in `wiki_attention` = no encyclopedic anchor exists; a well-cited
  definition page can become the reference.
- Angle -> evidence: how-to = steps + numbers; comparison = table + quotes; listicle =
  criterion + a source per item; definition = one-sentence definition + primary source;
  news = dated publisher quotes.

## Budget rules

- Google rate-limits hard. Prefer `discover_topics` (1 + N requests) over N separate
  `related_queries` calls; reuse cached results (`_meta.cache_hit`).
- If a tool error says HTTP 429, wait a minute. Cached queries still answer.
  `trendzeist_status` (free) shows which source is erroring; Wikipedia keeps working
  during a Google cool-down.
- Do not loop `mine_questions` over many seeds in one turn; 3 seeds is ~50 requests.
  `aeo_opportunities` is worst case 1 + 10 requests per seed.

## Anti-patterns

- Inventing statistics or quotes when the tools returned none. Say what to look up instead.
- Treating Google's 0-100 index as search volume.
- Keyword stuffing: it does nothing for answer engines (GEO paper, KDD 2024).
