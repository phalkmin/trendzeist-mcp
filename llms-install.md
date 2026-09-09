# trendzeist-mcp — installation guide for AI agents

You are installing an MCP server called **trendzeist-mcp**. It runs locally over
stdio, needs **no API key, no account and no browser**. Requirements: Python 3.11+
and one of `uvx`, `pipx`, `pip` or Docker.

## 1. Pick a launch command

| If the user has… | Use this `command` / `args` |
|---|---|
| `uv` / `uvx` (preferred) | `"command": "uvx", "args": ["trendzeist-mcp"]` |
| `pipx` | `"command": "pipx", "args": ["run", "trendzeist-mcp"]` |
| plain Python | run `pip install trendzeist-mcp`, then `"command": "trendzeist-mcp"` (or `"command": "python", "args": ["-m", "trendzeist_mcp"]`) |
| Docker | `"command": "docker", "args": ["run", "-i", "--rm", "ghcr.io/phalkmin/trendzeist-mcp"]` |
| a local clone | `"command": "uv", "args": ["--directory", "<clone path>", "run", "trendzeist-mcp"]` |

Check availability with `command -v uvx pipx docker` (macOS/Linux) or
`where uvx pipx docker` (Windows). Fall back down the table.

## 2. Add to the client config

**Claude Desktop** — edit the file, keep other entries, add under `mcpServers`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "trendzeist": {
      "command": "uvx",
      "args": ["trendzeist-mcp"]
    }
  }
}
```

**Claude Code**: `claude mcp add trendzeist -- uvx trendzeist-mcp`

**Cursor** (`~/.cursor/mcp.json`) and **VS Code** (`.vscode/mcp.json`, under
`"servers"`) accept the same `command`/`args` object.

**Codex CLI** (`~/.codex/config.toml`):
```toml
[mcp_servers.trendzeist]
command = "uvx"
args = ["trendzeist-mcp"]
```

## 3. Optional environment variables

Add an `"env"` object next to `args` only if the user asks:

| Variable | Default | Purpose |
|---|---|---|
| `TRENDZEIST_HL` | `en-US` | Google Trends language (`pt-BR`, `es-ES`, …) |
| `TRENDZEIST_TZ` | `360` | Timezone offset in minutes |
| `TRENDZEIST_MIN_INTERVAL` | `2.0` | Seconds between Google requests; raise if 429s occur |
| `TRENDZEIST_CACHE_DIR` | OS cache dir | Persistent cache location; `off` disables |
| `TRENDZEIST_PROXIES` | — | Comma-separated proxy URLs |
| `TRENDZEIST_LOG_LEVEL` | `WARNING` | Logging to stderr |

## 4. Verify

Restart the client. The server exposes 9 tools (`discover_topics`,
`interest_over_time`, `compare_keywords`, `related_queries`, `related_topics`,
`interest_by_region`, `suggest_keywords`, `trending_now`, `list_categories`) and
one prompt (`blog_ideas_from_trends`). A good first call:

```
discover_topics(seed_keywords=["espresso"], geo="US")
```

If tools error with "rate limit reached (HTTP 429)", wait ~1 minute; results are
cached so repeated questions do not re-hit Google.
