# trendzeist-mcp

<!-- mcp-name: io.github.phalkmin/trendzeist-mcp -->

**Turn Google Trends into your next 10 blog posts — in one call.**

trendzeist-mcp gives your AI assistant ranked **breakout / rising / evergreen** topics,
the **questions people actually ask**, an **AEO opportunity finder** that scores question
clusters with trend, news coverage, Wikipedia presence and citability hints, plus interest
curves, regional demand, real-time trends, **who covers a topic in Google News** and
**Wikipedia attention**. Free, local, private. No API key, no account, no browser.

*SEO and AEO:* find the questions people ask — and answer engines answer — then make your
site the source they cite. Measuring your own visibility inside ChatGPT / Perplexity /
AI Overviews is **out of scope**; this server tells you *what to write*, not *who cites you*.

```
You:   Give me blog post ideas about home espresso for US readers.
Agent: → discover_topics(["espresso", "espresso machine"], geo="US")
       ← 1 breakout, 14 rising, 14 evergreen candidates with growth %, angles and 6 questions
       → mine_questions("espresso machine", geo="US")
       ← 30 long-tail questions: how-to 18, comparison 6, definition 4, listicle 2
       → compare_keywords(["how to descale espresso machine", "best coffee beans for espresso"])
       ← "Interest in 'how to descale espresso machine' rose 120% ... peaking 2026-08-30 (rising)."
       "1. How to Descale Your Espresso Machine (how-to, rising +120%, publish now) ..."
```

## Built with

trendzeist-mcp is a thin MCP layer over **[pytrends-modern](https://github.com/yiromo/pytrends-modern)**,
which handles all Google Trends requests. Trendzeist adds the MCP tools and prompt, request
throttling, a persistent disk cache, strict input validation, LLM-friendly JSON output and the
ranked `discover_topics` workflow. Other runtime dependencies: [`mcp`](https://github.com/modelcontextprotocol/python-sdk)
(official MCP Python SDK), `pandas`, `platformdirs` and `requests`. All MIT/BSD/Apache licensed.
Google Autocomplete, Google News RSS and the Wikimedia REST API are called directly with
`requests` (keyless).

## Quick start

```bash
# any one of these
uvx trendzeist-mcp
pipx run trendzeist-mcp
pip install trendzeist-mcp && trendzeist-mcp
docker run -i --rm ghcr.io/phalkmin/trendzeist-mcp
```

**Claude Desktop** — add under `mcpServers` in `claude_desktop_config.json`
(macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`, Linux `~/.config/Claude/`):

```json
"trendzeist": { "command": "uvx", "args": ["trendzeist-mcp"] }
```

**Claude Code:** `claude mcp add trendzeist -- uvx trendzeist-mcp`
**Cursor / VS Code / Codex:** same `command`/`args` shape — see [`llms-install.md`](llms-install.md)
(written so you can paste it to an AI assistant and let it do the install).

## Tools

| Tool | What you get |
|---|---|
| `discover_topics` | Ranked blog topics from 1-5 seeds: breakout > rising > evergreen, deduped, each with a title `angle`; plus `questions[]` people ask |
| `mine_questions` | Long-tail questions about a seed from Google Autocomplete (`how to`, `why`, `what is`, `vs` …), deduped and angle-tagged — FAQ / AEO fuel |
| `aeo_opportunities` | Question clusters per seed × angle, scored with trend direction, Google News coverage (publishers to quote / pitch), Wikipedia presence and citability hints — one call, partial failures per source |
| `news_coverage` | Who covers a topic in Google News: headlines, publisher frequency table, recency histogram, coverage label |
| `wiki_attention` | Exact-title Wikipedia article and daily pageviews (direction, growth); `has_article=false` and optional `related_article` when only a different search hit is found |
| `trendzeist_status` | Health of every source (live / error / idle), cache usage, effective settings — free, no requests |
| `interest_over_time` | 0-100 interest curve with mean, peak, direction, `growth_3m` / `growth_12m` and a plain-English `insight` |
| `compare_keywords` | Head-to-head share and winner for 2-5 keywords |
| `related_queries` | Top & rising related searches with breakout flags, angles and `questions[]` |
| `related_topics` | Top & rising Knowledge-Graph topics (best-effort) |
| `interest_by_region` | Where demand lives: COUNTRY (worldwide), REGION (within a country), CITY / DMA (US or worldwide) |
| `suggest_keywords` | Disambiguate a term into Google entities (title, type, mid) |
| `trending_now` | What's trending right now, with news headlines |
| `list_categories` | Find Google Trends category ids to narrow any query |

Prompts: `blog_ideas_from_trends(topic, audience, geo)` (guided ideation),
`content_brief(topic, audience, geo)` (titles, outline, FAQ, regions, publishers) and
`answer_brief(question, geo)` (a citable answer: direct answer, statistic, quote, sources,
FAQ, schema). A ready-made agent skill lives in [`SKILL.md`](SKILL.md).

**Output conventions**

- Every result carries `schema_version` (currently `2`; bumped only when a field is removed
  or changes meaning) and `_meta` with `requests_made`, `cache_hit`, `cache_hits`,
  `cache_misses` for that call — so the model knows when to slow down.
- `angle` is one of `how-to | comparison | listicle | definition | news` (or `null`) —
  the title format the query suggests. Pair it with evidence: how-to → steps + numbers,
  comparison → table + quotes, definition → cite a primary source, news → dated publisher quotes.
- Anything silently adjusted is reported: clamped limits add a `note`, empty results add a
  `reason`.
- `citability_hints` (on `aeo_opportunities`) encode the GEO paper (Aggarwal et al., KDD
  2024): citing sources, quotations and statistics each lifted AI-engine visibility 30-40%;
  keyword stuffing did nothing.
- **Language follows the market.** With `TRENDZEIST_HL` unset, `geo="BR"` queries Google
  with `hl=pt-BR` (≈50 countries mapped), so related queries come back in Portuguese. The
  effective language is echoed as `query.hl`. Google News always uses the market's
  mapped edition (e.g. `BR:pt`), even when `TRENDZEIST_HL` pins the Trends UI language.
- **Authority channels:** pass `gprop="news"` or `gprop="youtube"` to any explore tool to see
  what news outlets and video audiences are searching for — the sources answer engines cite
  most. `gprop="images"` and `"froogle"` (Shopping) also work.

## Why this one?

| | trendzeist-mcp | typical alternatives |
|---|---|---|
| Ranked topic discovery in one call | ✅ `discover_topics` | ❌ raw primitives only |
| Guided ideation prompt | ✅ `blog_ideas_from_trends` | ❌ |
| Multi-source, keyless | ✅ Trends + Autocomplete + News + Wikipedia | single source, or paid aggregators |
| Related queries + breakout detection | ✅ | often missing in hosted/paid servers |
| Cost / auth | free, none | API key, monthly quota |
| Browser required | no | Chrome for some Python libraries |
| Cache survives client restarts | ✅ safe JSON disk cache | usually in-memory or none |
| Rate-limit friendly | ✅ throttled per HTTP request | ❌ bursts, frequent 429s |

## Run from source

```bash
git clone https://github.com/phalkmin/trendzeist-mcp && cd trendzeist-mcp
uv sync --group dev
uv run pytest -q                 # offline tests
uv run pytest -q -m live         # optional: live canary against Google
uv run trendzeist-mcp              # stdio server
npx @modelcontextprotocol/inspector uv run trendzeist-mcp   # interactive debugging
```

Point a client at the clone with
`"command": "uv", "args": ["--directory", "/path/to/trendzeist-mcp", "run", "trendzeist-mcp"]`.

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `TRENDZEIST_HL` | *(unset → follows `geo`)* | Pin the Trends/Autocomplete UI language (e.g. `en-US`); Google News uses its country edition independently. When unset, language follows `geo` (`BR` → `pt-BR`, unknown → `en-US`) |
| `TRENDZEIST_TZ` | `360` | Timezone offset in minutes |
| `TRENDZEIST_MIN_INTERVAL` | `2.0` | Minimum seconds between *every* HTTP request to **Google** hosts (Trends cookie / token / data, RSS, Autocomplete, News) |
| `TRENDZEIST_WIKI_MIN_INTERVAL` | `0.5` | Minimum seconds between Wikimedia requests (separate lane from Google) |
| `TRENDZEIST_EXPLORE_TTL` | `900` | Cache seconds for Trends explore, News search |
| `TRENDZEIST_RSS_TTL` | `300` | Cache seconds for the trending RSS feed |
| `TRENDZEIST_STATIC_TTL` | `86400` | Cache seconds for categories, suggestions, Autocomplete, Wikipedia |
| `TRENDZEIST_MAX_MEMORY_ENTRIES` | `256` | In-memory cache entries (disk is unbounded, swept on expiry) |
| `TRENDZEIST_MAX_SERIES_POINTS` | `60` | Downsample interest curves to at most this many points (positive integer) |
| `TRENDZEIST_RETRIES` | `3` | Retry attempts on transient errors |
| `TRENDZEIST_BACKOFF` | `1.5` | Exponential backoff factor |
| `TRENDZEIST_PROXIES` | — | Comma-separated proxy URLs. Rotated per request for explore calls; **RSS, Autocomplete, News and Wikipedia always use the first one** |
| `TRENDZEIST_CACHE_DIR` | OS user cache dir | Persistent JSON cache location (`0700`); `off` to disable |
| `TRENDZEIST_LOG_LEVEL` | `WARNING` | Python logging level (stderr) |

## Notes & limitations

- Google rate-limits aggressively (HTTP 429). Every HTTP request is serialised and
  throttled; results are cached (15 min explore / News, 5 min RSS, 24 h categories /
  Autocomplete / Wikipedia)
  as plain JSON on disk so client restarts don't re-fetch. Memory cache is bounded and
  expired files are swept automatically. Errors come back as tool errors with guidance.
- `mine_questions` issues up to 16 Autocomplete requests per uncached call (one per
  question prefix); at the default 2 s interval that is ~30 s worst case. It stops as soon
  as `limit` is met and returns partial results if Google starts refusing.
- Values are Google's relative 0–100 index, not absolute search volume. `growth_3m` /
  `growth_12m` compare the mean of the last window with the window before it and are
  `null` when the timeframe is too short (use `today 12-m` / `today 5-y`).
- Question prefixes in `mine_questions` are English; for native-language questions in a
  non-English market, pass a seed already phrased in that language.
- `related_topics` frequently returns nothing from Google; `related_queries` is reliable.
- Google's legacy daily `trending_searches` endpoint is gone (404); `trending_now` uses the RSS feed.
- `aeo_opportunities` is the most expensive call: worst case 1 + 10 requests per seed
  (Wikipedia runs on its own lane). Google requests stop at the first 429; Wikipedia data
  is still returned. `trendzeist_status` shows which source is erroring without any request.
- `wiki_attention.has_article` confirms an exact-title search match only; a different
  Wikipedia search hit appears as `related_article`, not as a citation or proof of a gap.
  Verify related titles and redirects manually before claiming an encyclopedic content gap.
- Cache TTLs, memory-entry count and series-point cap must be positive integers; invalid
  values produce an actionable tool error rather than an opaque formatter failure.
- Camoufox/browser mode from pytrends-modern is intentionally not used.

## Disclaimer

This server talks to the same undocumented endpoints the trends.google.com frontend uses
(plus Google Autocomplete and Google News RSS). They are unofficial and may change,
rate-limit or disappear without notice. A weekly
[live canary](.github/workflows/live-canary.yml) runs in CI to catch breakage early.
This project is not affiliated with, endorsed by, or sponsored by Google LLC.
"Google Trends" is a trademark of Google LLC. You are responsible for complying with
Google's terms of service in your jurisdiction.

## Contributing

Issues and PRs welcome. Read [`AGENTS.md`](AGENTS.md) for architecture and conventions
(also useful if you point a coding agent at the repo). Data-shape corrections after a Google
change are the most valuable contribution — include the call you made and what came back.

## License

MIT — see [LICENSE](LICENSE). Built on [pytrends-modern](https://github.com/yiromo/pytrends-modern) (MIT).
