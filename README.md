# trendzeist-mcp

<!-- mcp-name: io.github.phalkmin/trendzeist-mcp -->

**Turn Google Trends into your next 10 blog posts — in one call.**

trendzeist-mcp gives your AI assistant ranked **breakout / rising / evergreen** topics,
interest curves, related searches, regional demand and real-time trends. Free, local,
private. No API key, no account, no browser.

```
You:   Give me blog post ideas about home espresso for US readers.
Agent: → discover_topics(["espresso", "espresso machine"], geo="US")
       ← 1 breakout, 14 rising, 14 evergreen candidates with growth %
       → compare_keywords(["how to descale espresso machine", "best coffee beans for espresso"])
       "1. How to Descale Your Espresso Machine (rising +120%, publish now) ..."
```

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
| `discover_topics` | Ranked blog topics from 1-5 seeds: breakout > rising > evergreen, deduped |
| `interest_over_time` | 0-100 interest curve with mean, peak and direction |
| `compare_keywords` | Head-to-head share and winner for 2-5 keywords |
| `related_queries` | Top & rising related searches with breakout flags |
| `related_topics` | Top & rising Knowledge-Graph topics (best-effort) |
| `interest_by_region` | Where demand lives: COUNTRY / REGION / CITY / DMA |
| `suggest_keywords` | Disambiguate a term into Google entities (title, type, mid) |
| `trending_now` | What's trending right now, with news headlines |
| `list_categories` | Find Google Trends category ids to narrow any query |

Prompt: `blog_ideas_from_trends(topic, audience, geo)` — a guided ideation workflow.

## Why this one?

| | trendzeist-mcp | typical alternatives |
|---|---|---|
| Ranked topic discovery in one call | ✅ `discover_topics` | ❌ raw primitives only |
| Guided ideation prompt | ✅ `blog_ideas_from_trends` | ❌ |
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
| `TRENDZEIST_HL` | `en-US` | UI language for Google Trends |
| `TRENDZEIST_TZ` | `360` | Timezone offset in minutes |
| `TRENDZEIST_MIN_INTERVAL` | `2.0` | Minimum seconds between *every* HTTP request to Google (cookie, token, data, RSS) |
| `TRENDZEIST_RETRIES` | `3` | Retry attempts on transient errors |
| `TRENDZEIST_BACKOFF` | `1.5` | Exponential backoff factor |
| `TRENDZEIST_PROXIES` | — | Comma-separated proxy URLs (rotated for explore calls; first one used for RSS) |
| `TRENDZEIST_CACHE_DIR` | OS user cache dir | Persistent JSON cache location (`0700`); `off` to disable |
| `TRENDZEIST_LOG_LEVEL` | `WARNING` | Python logging level (stderr) |

## Notes & limitations

- Google rate-limits aggressively (HTTP 429). Every HTTP request is serialised and
  throttled; results are cached (15 min explore, 5 min RSS, 24 h categories) as plain JSON
  on disk so client restarts don't re-fetch. Memory cache is bounded and expired files are
  swept automatically. Errors come back as tool errors with guidance.
- Values are Google's relative 0–100 index, not absolute search volume.
- `related_topics` frequently returns nothing from Google; `related_queries` is reliable.
- Google's legacy daily `trending_searches` endpoint is gone (404); `trending_now` uses the RSS feed.
- Camoufox/browser mode from pytrends-modern is intentionally not used.

## Disclaimer

This server talks to the same undocumented endpoints the trends.google.com frontend uses.
They are unofficial and may change, rate-limit or disappear without notice. A weekly
[live canary](.github/workflows/live-canary.yml) runs in CI to catch breakage early.
This project is not affiliated with, endorsed by, or sponsored by Google LLC.
"Google Trends" is a trademark of Google LLC. You are responsible for complying with
Google's terms of service in your jurisdiction.

## Contributing

Issues and PRs welcome. Read [`AGENTS.md`](AGENTS.md) for architecture and conventions
(also useful if you point a coding agent at the repo). Data-shape corrections after a Google
change are the most valuable contribution — include the call you made and what came back.

## License

MIT — see [LICENSE](LICENSE). Built on [pytrends-modern](https://pypi.org/project/pytrends-modern/) (MIT).
