# trendzeist-mcp

<!-- mcp-name: io.github.phalkmin/trendzeist-mcp -->

**Turn Google Trends into your next 10 blog posts — in one call.**

A free, local MCP server for content ideation and answer-engine optimization (AEO).
It combines Google Trends, Autocomplete, Google News and Wikipedia to find ranked
topics, real questions, content gaps and supporting sources. No API key, account or
browser. Results stay on your machine apart from requests to those public services.

It helps decide **what to write**. Measuring your visibility inside ChatGPT, Perplexity
or AI Overviews is out of scope.

```
You:   Give me blog post ideas about home espresso for US readers.
Agent: → discover_topics(["espresso", "espresso machine"], geo="US")
       ← breakout, rising and evergreen candidates with growth, angles and questions
       → mine_questions("espresso machine", geo="US")
       ← long-tail questions for titles, H2s and FAQs
       → compare_keywords(["how to descale espresso machine", "best coffee beans for espresso"])
       ← relative interest, recent direction and a recommended target
```

## Quick start

```bash
# any one of these
uvx trendzeist-mcp
pipx run trendzeist-mcp
pip install trendzeist-mcp && trendzeist-mcp
docker run -i --rm ghcr.io/phalkmin/trendzeist-mcp
```

**Claude Desktop** — add under `mcpServers` in `claude_desktop_config.json`:

```json
"trendzeist": { "command": "uvx", "args": ["trendzeist-mcp"] }
```

**Claude Code:** `claude mcp add trendzeist -- uvx trendzeist-mcp`

For Cursor, VS Code, Codex and other clients, see [`llms-install.md`](llms-install.md).

## Tools

| Tool | Use it for |
|---|---|
| `discover_topics` | Ranked breakout, rising and evergreen topics from 1-5 seeds, with angles, questions, scores and optional site-coverage gaps |
| `mine_questions` | Long-tail Autocomplete questions for titles, H2s, FAQs and AEO |
| `aeo_opportunities` | Question clusters scored with trend, news, Wikipedia, site coverage and citability evidence |
| `what_changed` | New, promoted, cooled and dropped queries since a local snapshot |
| `seasonality` | Peak months, seasonal strength and a publish-by date |
| `compare_channels` | Momentum on web, YouTube, News, Images and Shopping, with a format hint |
| `trend_history` | Local observations and breakout history for one keyword; no network requests |
| `recurring_topics` | Topics with repeated breakout episodes in the local archive; no network requests |
| `news_coverage` | Recent headlines, publishers and coverage recency from Google News |
| `wiki_attention` | Exact-title Wikipedia match and pageview trend |
| `interest_over_time` | Relative 0-100 interest curves, recent direction and 3/12-month growth |
| `compare_keywords` | Relative share and editorial recommendation for 2-5 keywords |
| `related_queries` | Top and rising related searches, breakout flags, angles and questions |
| `related_topics` | Best-effort Google Knowledge-Graph topics |
| `interest_by_region` | Demand by country, region, city or US metro area |
| `suggest_keywords` | Google entity suggestions for disambiguation |
| `trending_now` | Real-time trending searches and linked headlines |
| `list_categories` | Category IDs used to narrow Trends queries |
| `trendzeist_status` | Source health, cooldowns, cache and archive status; no network requests |

Prompts: `blog_ideas_from_trends`, `content_brief`, `answer_brief` and `weekly_review`.
A ready-made agent skill is in [`SKILL.md`](SKILL.md).

## Reading results

- Every result includes `schema_version` (currently `3`) and `_meta` request/cache
  counters. Empty results include a `reason`; clamped inputs include a `note`.
- `direction` describes the whole window. `direction_now` describes the recent slope;
  use the latter when deciding whether to publish now. Interest is relative 0-100,
  not absolute search volume. `growth_3m` needs `today 12-m`; `growth_12m` needs
  `today 5-y`.
- `score` and `score_breakdown` are transparent heuristics for ranking within one
  call, not measurements of traffic or ranking probability.
- `suspect` marks spam-like rising queries. `off_topic` marks likely unrelated ones;
  inspect `filtered_out` before discarding a possible adjacent topic.
- `level_shifts` and `warnings` flag sustained steps that may distort growth figures.
  A `flattened` warning means a keyword should be queried separately.
- Pass `site_queries` to `discover_topics` or `aeo_opportunities` to mark coverage as
  `covered`, `partial` or `gap`. `exclude_covered=true` removes fully covered items.
- Language follows `geo` unless `TRENDZEIST_HL` is set. Localized question prefixes
  exist for Portuguese, Spanish, French, German, Italian, Polish and Dutch; other
  languages use English prefixes and return a `language_note`.

### Local observation archive

Fresh Trends, related-query, trending and question results are recorded in
`archive.sqlite3` beside the disk cache. Cache hits and stale fallbacks are not
recorded. This powers `what_changed`, `trend_history` and `recurring_topics`.

The archive needs repeated use to become useful: run `what_changed` weekly to build
baselines and distinguish breakout episodes. Sparse polling undercounts recurrence.
Leaving a bounded related-query list is not proof of falling demand; confirm with
`interest_over_time` before retiring a topic. Set `TRENDZEIST_ARCHIVE=0` to opt out.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TRENDZEIST_HL` | unset | Pin Trends/Autocomplete language (for example `en-US`); otherwise follow `geo` |
| `TRENDZEIST_TZ` | `360` | Trends timezone offset in minutes |
| `TRENDZEIST_MIN_INTERVAL` | `2.0` | Minimum seconds between Google HTTP requests |
| `TRENDZEIST_WIKI_MIN_INTERVAL` | `0.5` | Minimum seconds between Wikimedia requests |
| `TRENDZEIST_JITTER` | `0.5` | Random extra spacing, as a fraction of the minimum interval |
| `TRENDZEIST_EXPLORE_TTL` | `900` | Trends and News cache seconds |
| `TRENDZEIST_RSS_TTL` | `300` | Trending RSS cache seconds |
| `TRENDZEIST_STATIC_TTL` | `86400` | Categories, suggestions, Autocomplete and Wikipedia cache seconds |
| `TRENDZEIST_MAX_MEMORY_ENTRIES` | `256` | Bounded in-memory cache size |
| `TRENDZEIST_MAX_SERIES_POINTS` | `60` | Maximum returned interest-curve points |
| `TRENDZEIST_RETRIES` | `3` | Retries for transient upstream errors |
| `TRENDZEIST_BACKOFF` | `1.5` | Exponential retry backoff factor |
| `TRENDZEIST_COOLDOWN` | `60` | Fail-fast seconds after HTTP 429; `Retry-After` takes precedence, capped at 600; `0` disables |
| `TRENDZEIST_STALE_FALLBACK` | `1` | Serve flagged expired cache data during rate limiting; `0` disables |
| `TRENDZEIST_PROXIES` | unset | Comma-separated proxy URLs; Trends rotates them, other sources use the first |
| `TRENDZEIST_CACHE_DIR` | OS cache dir | JSON cache location; `off` disables disk cache and archive |
| `TRENDZEIST_ARCHIVE` | `1` | Set to `0` to disable observation history |
| `TRENDZEIST_ARCHIVE_DAYS` | `365` | Archive retention in days |
| `TRENDZEIST_LOG_LEVEL` | `WARNING` | Python logging level on stderr |

## Limits and reliability

- Google rate-limits aggressively. Start composites with 1-2 seeds. After HTTP 429,
  uncached requests fail fast during cooldown; cached and flagged stale results remain
  available, and other sources continue to work. Check `trendzeist_status` for retry timing.
- `mine_questions` makes up to 16 Autocomplete requests. `aeo_opportunities` is the
  most expensive tool: up to `1 + 11 × number_of_seeds` requests, including entity
  checks and Wikipedia pageviews. `compare_channels` makes up to five requests.
- `related_topics` often returns no data; use `related_queries` instead. Google News,
  Autocomplete and Wikipedia results are best-effort and may be incomplete.
- An exact-title Wikipedia match is confirmed; a `related_article` is only a suggestion
  to verify, not proof of a citation opportunity or content gap.
- `seasonality` requires at least three full calendar years of non-zero data.
  Channel interest values use separate 0-100 scales; compare their growth, not means.
- Google Trends, Autocomplete and Google News use undocumented public endpoints that
  may change, rate-limit or disappear. This project is not affiliated with Google;
  you are responsible for complying with applicable terms of service.

## Run from source

```bash
git clone https://github.com/phalkmin/trendzeist-mcp && cd trendzeist-mcp
uv sync --group dev
uv run pytest -q
uv run trendzeist-mcp
```

Optional checks: `uv run pytest -q -m live` exercises Google, and
`npx @modelcontextprotocol/inspector uv run trendzeist-mcp` opens interactive debugging.

To point a client at a clone, use `"command": "uv", "args": ["--directory",
"/path/to/trendzeist-mcp", "run", "trendzeist-mcp"]`.

## Contributing and license

Issues and PRs are welcome. See [`AGENTS.md`](AGENTS.md) for architecture and commands.
A weekly [live canary](.github/workflows/live-canary.yml) checks upstream compatibility.

MIT — see [LICENSE](LICENSE). Built with [pytrends-modern](https://github.com/yiromo/pytrends-modern)
and the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
