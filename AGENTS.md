# AGENTS.md — working on this repo with a coding agent

## What this is
A Python MCP server (`mcp` SDK **2.x**, `MCPServer` API — not the 1.x `FastMCP`)
wrapping `pytrends-modern` (Google Trends) plus keyless Google Autocomplete, Google News RSS
and Wikimedia sources for content ideation and AEO.

## Layout
```
src/trendzeist_mcp/
  server.py      MCP registration only (tools + prompts). Thin; delegates to tools.py / discovery.py / aeo.py.
  tools.py       Tool logic. Pure functions taking a Hub first. Unit-tested with a fake hub.
  discovery.py   discover_topics composite (rank breakout > rising > evergreen).
  aeo.py         aeo_opportunities composite (questions x trend x news x Wikipedia x citability).
  client.py      Shared plumbing: Settings, TrendsError, _TTLCache, Lane (per-host lock+interval),
                 CallStats, Source base (_get/_cached/_guarded/status) and TrendsClient (Google Trends).
  sources.py     AutocompleteSource, GoogleNewsSource, WikipediaSource and Hub (composes all sources;
                 hub.trends / hub.autocomplete / hub.news / hub.wikipedia).
  formatters.py  DataFrame/list -> compact JSON; citability hints. Keep payloads small (LLM context).
  validation.py  Strict whitelists. Raise ValidationError with actionable text.
tests/           Offline by default. tests/test_live_canary.py is `-m live` only.
```

## Rules
- **Errors to the model must be `ToolError`** (`mcp.server.mcpserver.exceptions`).
  Any other exception is hidden as an opaque "crash". `server._tool` does the translation.
- Every new tool: validate in `validation.py` → implement in `tools.py` (or a composite
  module) → register in `server.py` → fake-hub test in `tests/test_tools.py` (extend
  `FakeClient`, which stands in for every source) → README table row.
- Upstream traffic (Google Trends, RSS, Autocomplete, News, Wikimedia) goes only through a
  `Source` subclass: `Source._get` (or `_throttle()` before any other HTTP call) inside
  `_cached` / `_guarded`; never call `TrendReq` or `requests` from tools. New upstream =
  new `Source` subclass in `sources.py` + attribute on `Hub`; Google hosts share
  `Hub._google`, others get their own `Lane`.
- Keep outputs JSON-native (no numpy scalars, no Timestamps) — use `formatters._to_native`.
- Every tool result gets `schema_version` + `_meta` via `tools.finalize` (called by
  `server._tool`). Bump `tools.SCHEMA_VERSION` only when a field is removed or changes
  meaning; adding fields is compatible.
- Report silent adjustments: clamped limits → `note` (`validation.limit_note`), empty
  results → `reason`.
- No new runtime deps without a reason; the target is `uvx trendzeist-mcp` starting in <2 s.
- **Git is the maintainer's job.** Agents never create branches, commits or tags. Edit the
  working tree, run the tests, and report; the maintainer commits and tags on GitHub.

## Commands
```bash
uv sync --group dev          # install
uv run pytest -q             # offline tests (must pass)
uv run pytest -q -m live     # live canary against Google (slow, may 429)
uv run trendzeist-mcp          # start stdio server
npx @modelcontextprotocol/inspector uv run trendzeist-mcp   # interactive
uv build && uvx twine check dist/*                        # packaging check
```

## Release
Bump `version` in **three** places — `pyproject.toml`, `server.json` (two fields) and
`src/trendzeist_mcp/__init__.py` (`__version__`, reported to MCP clients) — update
`CHANGELOG.md`, `git tag vX.Y.Z && git push --tags`. `publish.yml` verifies the first two
against the tag and handles PyPI (Trusted Publishing), GHCR image and the GitHub release.
