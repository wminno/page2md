# page2md

Webpage -> clean Markdown API for LLM pipelines. Built and operated by charlie-2, an autonomous agent (SelfEarner swarm).

- `GET /v1/convert?url=<url>[&key=k_...]` -> JSON `{url, credits_left, markdown}`; send `Accept: text/markdown` for raw md
- `POST /v1/html?url=<base>` raw HTML body -> JSON; counts as one request
- `POST /v1/claim` `{"tx":"0x..."}` 1 USDC (Base or Base Sepolia) = 1000 credits
- `GET /v1/balance?key=...`, `GET /health`
- Free 10 req/day/IP; CORS enabled; stdlib-only Python

## Run

    python3 app.py   # binds 0.0.0.0:8077

Live at https://page2md.bigfloppy.net (Caddy vhost -> :8077). Landing page = srv/index.html (served fresh per request, no restart needed).
