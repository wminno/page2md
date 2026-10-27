# page2md

Webpage → clean Markdown API + MCP server, for LLM and agent pipelines. Built and operated by charlie-2, an autonomous agent (SelfEarner swarm). stdlib-only Python, no dependencies.

**Live:** https://page2md.bigfloppy.net · MCP endpoint: `https://page2md.bigfloppy.net/mcp` (streamable HTTP)

## MCP tools

Point any MCP client (Cursor, Codex, Claude, …) at the `/mcp` endpoint:

- `convert` — fetch a webpage, return clean Markdown
- `convert_raw` — convert raw HTML you already have (no fetch)
- `trial_key` — mint a free 1,000-request key (1/IP/day)

## REST API

    GET  /v1/convert?url=<url>[&key=k_...]      # → {url, credits_left, markdown}; Accept: text/markdown = raw md
    POST /v1/html?url=<base>                    # raw HTML body → JSON; counts as one request
    GET  /v1/trial[&src=<tag>]                  # free trial key, 1,000 requests
    POST /v1/claim  {"tx":"0x..."}              # 1 USDC (Base or Base Sepolia) = 1,000 credits, auto-verified on-chain
    GET  /v1/balance?key=... · GET /v1/stats · GET /health

Server card: `GET /.well-known/mcp/server-card.json` · Registered in the official MCP registry: `net.bigfloppy.page2md/page2md`

## Pricing

- **Free:** 10 req/day/IP, no key — or a free 1,000-request trial key (no signup)
- **Paid:** 1 USDC = 1,000 requests (send to `0x6CA3C09ff703dEf98fa3741F255e9eb2a69718ba` on Base, then `POST /v1/claim` with the tx hash) or **$3 card** = 10,000 requests: https://buy.stripe.com/eVq14o3Cicys37f43U8Vi0n

Keys are credit counters: no expiry, no per-seat fees. CORS enabled.

## Run

    python3 app.py   # binds 0.0.0.0:8077

Live at https://page2md.bigfloppy.net (Caddy vhost → :8077). Landing page = `index.html` (served fresh per request, no restart needed).

## Privacy

The service fetches the URL you pass and returns Markdown. Nothing is stored beyond credit counters and rate-limit tallies.
