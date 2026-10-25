# AdsCloud MCP (read-only)

An MCP server that exposes the scraped Meta Ad Library bank to AI agents
(ChatGPT, Claude, opencode) for analysis and competitor research.

- **Read-only**: it cannot scrape, tag, or modify data.
- Uses the same database as the dashboard (`DATABASE_URL` / local SQLite).
- Runs in two modes: **stdio** (local) and **Streamable HTTP** (remote, bearer token).

## Tools

| Tool | What it returns |
|---|---|
| `stats_overview` | totals + top keywords |
| `search_ads` | filtered ads (text / keyword / store / page / dates / active / min_days / sort / grouped) |
| `search_by_attributes` | text search for a product described by attributes (image workflow) |
| `get_ad` | full details of one ad (body, video, advertiser, keywords, page/store totals) |
| `list_competitors` | stores ranked by ads / winning / active / pages |
| `store_insights` | totals + top ads for a store domain |
| `page_insights` | totals + store domains + top ads for a page id |
| `keyword_performance` | per-keyword: total, store, winning, fresh, last run |
| `find_winning_ads` | active ads running for >= N days |
| `product_group` | all ads of one product group |

## 1) Local (stdio) — opencode / Claude Desktop

**opencode** (`~/.config/opencode/opencode.json` or project `opencode.json`):

```json
{
  "mcp": {
    "adscloud": {
      "type": "local",
      "command": ["E:\\opencode\\adscloud\\.venv\\Scripts\\python.exe", "E:\\opencode\\adscloud\\mcp_server.py"],
      "enabled": true
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "adscloud": {
      "command": "E:\\opencode\\adscloud\\.venv\\Scripts\\python.exe",
      "args": ["E:\\opencode\\adscloud\\mcp_server.py"]
    }
  }
}
```

stdio mode reads the local database (SQLite `E:\opencode\meta_ads.sqlite`, or
`DATABASE_URL` if set).

## 2) Remote (Streamable HTTP)

Run:

```bash
MCP_TOKEN="<long-random-token>" python mcp_server.py --http --port 5003
# endpoint: POST /mcp      health: GET /health
```

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

On Coolify: create a second application from the same repo (or add a second
process) with `MCP_TOKEN` set, port `5003`, and a domain such as
`mcp.ads.botdz.com`.

**opencode** (remote):

```json
{
  "mcp": {
    "adscloud": {
      "type": "remote",
      "url": "https://mcp.ads.botdz.com/mcp",
      "headers": { "Authorization": "Bearer <token>" },
      "enabled": true
    }
  }
}
```

**Claude Desktop** (remote via `mcp-remote`):

```json
{
  "mcpServers": {
    "adscloud": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://mcp.ads.botdz.com/mcp",
        "--header", "Authorization: Bearer <token>"
      ]
    }
  }
}
```

**ChatGPT**: custom connectors currently require OAuth or no authentication;
a plain bearer header is not offered in the UI. For ChatGPT use one of:
- expose the endpoint behind Cloudflare Access / an OAuth proxy, or
- wait for the OAuth phase of this server (planned).

## 3) Image → competitors (how to prompt the agent)

The server cannot receive images. The agent uses **its own vision**, then calls
a text tool:

> Look at the attached product photo. Describe the product in short Arabic and
> French keywords (type, material, use, distinctive features). Then call
> `search_by_attributes` with those keywords, and `list_competitors` with the
> best keyword to rank the competing stores.

Example attributes: `مكنسة بخار محمولة منظف بالبخار nettoyeur vapeur`.

## Security

- HTTP mode requires `Authorization: Bearer <MCP_TOKEN>` on every request.
- The token is compared in constant time; `/health` is the only unauthenticated route.
- Keep `MCP_TOKEN` in Coolify env vars only (never in git).
- The server has **admin scope** over the data in Phase 1; per-user scoping is
  planned with OAuth.
