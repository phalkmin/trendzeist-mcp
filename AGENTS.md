# AGENTS.md — working on this repo with a coding agent

## What this is
A Python MCP server (`mcp` SDK **2.x**, `MCPServer` API — not the 1.x `FastMCP`)
wrapping `pytrends-modern` to expose Google Trends for content ideation.

## Layout
```
src/trendzeist_mcp/
  server.py      MCP registration only (tools + prompt). Thin; delegates to tools.py.
  tools.py       Tool logic. Pure functions taking a TrendsClient first. Unit-tested with a fake client.
  discovery.py   discover_topics composite (rank breakout > rising > evergreen).
  client.py      TrendsClient: global lock, throttle, disk+memory TTL cache, error mapping -> TrendsError.
  formatters.py  DataFrame -> compact JSON. Keep payloads small (LLM context).
  validation.py  Strict whitelists. Raise ValidationError with actionable text.
tests/           Offline by default. tests/test_live_canary.py is `-m live` only.
```

## Rules
- **Errors to the model must be `ToolError`** (`mcp.server.mcpserver.exceptions`).
  Any other exception is hidden as an opaque "crash". `server._tool` does the translation.
- Every new tool: validate in `validation.py` → implement in `tools.py` → register in
  `server.py` → fake-client test in `tests/test_tools.py` → README table row.
- Google traffic goes only through `TrendsClient._guarded`; never call `TrendReq` directly.
- Keep outputs JSON-native (no numpy scalars, no Timestamps) — use `formatters._to_native`.
- No new runtime deps without a reason; the target is `uvx trendzeist-mcp` starting in <2 s.

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
Bump `version` in `pyproject.toml` **and** `server.json`, update `CHANGELOG.md`,
`git tag vX.Y.Z && git push --tags`. `publish.yml` handles PyPI (Trusted
Publishing), GHCR image and the GitHub release.
