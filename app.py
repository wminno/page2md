#!/usr/bin/env python3
"""page2md - webpage -> clean markdown API. Python stdlib only."""
import json, os, re, socket, ipaddress, sqlite3, time, urllib.request, urllib.parse, http.client, socketserver, threading, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser

BASE = os.environ.get("PAGE2MD_BASE", "/srv/app/clients/charlie2-page2md/") + ("/" if not os.environ.get("PAGE2MD_BASE","/srv/app/clients/charlie2-page2md/").endswith("/") else "")
DB = BASE + "page2md.db"
WALLET = "0x6CA3C09ff703dEf98fa3741F255e9eb2a69718ba"
PRICE_USDC = 1  # 1 USDC buys 1000 credits
CACHE = {}
CACHE_TTL = 3600
cache_lock = threading.Lock()
RPCS = [("https://sepolia.base.org", "0x036CbD53842c5146688310f7FaceDc5dA97E1ab1"),
        ("https://mainnet.base.org", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952b7e8109879f9d9cef3a5a97e30ba9"
FREE_PER_IP_DAY = 10

lock = threading.Lock()

def db():
    c = sqlite3.connect(DB, timeout=15)
    c.execute("CREATE TABLE IF NOT EXISTS keys(key TEXT PRIMARY KEY, credits INTEGER, created REAL, note TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS trials(ip TEXT PRIMARY KEY, day TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS usage(day TEXT, ident TEXT, cnt INTEGER, PRIMARY KEY(day, ident))")
    c.execute("CREATE TABLE IF NOT EXISTS claims(tx TEXT PRIMARY KEY, key TEXT, credits INTEGER, ts REAL)")
    c.commit()
    return c

# ---------------- HTML -> markdown ----------------
SKIP = {"script","style","noscript","svg","nav","footer","aside","form","iframe","select","button",
        "head","meta","link","path","polygon","symbol","use","option","datalabel","template","audio","video","source"}
BLOCK = {"p","div","section","article","main","li","ul","ol","table","tr","hr","pre","blockquote","figure","header","h1","h2","h3","h4","h5","h6","br"}

class MD(HTMLParser):
    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.out, self.skips = [], []   # stack of open SKIP tags
        self.href, self.astart, self.base = None, 0, base
    @property
    def skip(self): return len(self.skips)
    def _pop_skip(self, tag):
        if tag in self.skips:
            del self.skips[len(self.skips) - 1 - self.skips[::-1].index(tag):]
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP:
            self.skips.append(tag); return
        if tag == "body": self.skips.clear()          # head region over, recover from unclosed tags
        if self.skip: return
        if tag == "a" and a.get("href"):
            self.href = urllib.parse.urljoin(self.base, a["href"]); self.astart = len(self.out); self.out.append("[")
        elif tag in ("h1","h2","h3","h4","h5","h6"): self.out.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in ("p","div","section","article","main","blockquote","li","tr"): self.out.append("\n")
        elif tag == "hr": self.out.append("\n\n---\n")
        elif tag == "br": self.out.append("\n")
        elif tag in ("b","strong"): self.out.append("**")
        elif tag in ("i","em"): self.out.append("*")
        elif tag == "code": self.out.append("`")
        elif tag in ("td","th"): self.out.append(" | ")
        elif tag == "img" and a.get("src"): self.out.append("![" + (a.get("alt") or "img") + "](" + urllib.parse.urljoin(self.base, a["src"]) + ")")
    def handle_endtag(self, tag):
        if tag in SKIP:
            self._pop_skip(tag); return
        if self.skip: return
        if tag == "a":
            if self.href:
                text = "".join(self.out[self.astart:]).strip()
                if text: self.out.append("](" + self.href + ")")
                else: del self.out[self.astart:]      # empty link -> drop
            self.href = None
        elif tag in ("b","strong"): self.out.append("**")
        elif tag in ("i","em"): self.out.append("*")
        elif tag == "code": self.out.append("`")
        elif tag in BLOCK: self.out.append("\n")
    def handle_data(self, d):
        if not self.skip: self.out.append(d)
    def result(self):
        return "".join(self.out)

def clean(md):
    md = re.sub(r"[ \t]+", " ", md)
    md = re.sub(r" ?\n ?", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"\*\*\s+", "** ", md)
    md = re.sub(r"\n## ", "\n\n## ", md)
    return md.strip()[:200000]

def fetch(url):
    if not url.startswith("http"): url = "https://" + url
    u = urllib.parse.urlparse(url)
    if not u.hostname: raise ValueError("bad url")
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                raise ValueError("blocked host")
    except socket.gaierror:
        pass
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (compatible; page2md/1.0)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")

# ---------------- rpc ----------------
def rpc(call, params):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":call,"params":params}).encode()
    req = urllib.request.Request(RPCS[0][0], data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def topic_addr(a):
    return "0x" + "0"*24 + a.lower().replace("0x","")

def verify_tx(tx):
    """Check tx sent USDC (any of our chains) to our wallet. Returns (credits, chain)."""
    txh = tx.strip().lower().replace("0x","")
    if not txh.isalnum() or len(txh) != 64: return None, None
    for i,(rp, token) in enumerate(RPCS):
        try:
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_getTransactionReceipt","params":["0x"+txh]}).encode()
            req = urllib.request.Request(rp, data=body, headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                rec = json.loads(r.read().decode()).get("result")
            if not rec or rec.get("status") != "0x1": continue
            to_addr = None
            for lg in rec.get("logs", []):
                if len(lg.get("topics",[])) >= 3 and lg["topics"][0].lower() == TRANSFER_TOPIC:
                    if lg["address"].lower() == token.lower():
                        frm, to = lg["topics"][1], lg["topics"][2]
                        if to.lower() == topic_addr(WALLET):
                            raw = lg.get("data","0x")
                            n = int(raw,16) if raw and raw != "0x" else 0
                            return n // 1, i
        except Exception:
            continue
    return None, None

# ---------------- http ----------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args):
        try:
            with open(BASE + "server.log", "a") as f:
                f.write("%s %s %s %s\n" % (self.log_date_time_string(time.time()), self.ip(), self.command, fmt % args))
        except Exception: pass
    def ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
    def send(self, code, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, indent=1).encode()
        # drain only an UNREAD request body (keep-alive sync). Handlers that
        # already read the body set _consumed; draining again blocks forever.
        if not getattr(self, "_consumed", 0):
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
                while ln > 0:
                    chunk = self.rfile.read(min(ln, 65536))
                    if not chunk: break
                    ln -= len(chunk)
            except Exception:
                pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not getattr(self, '_head', False): self.wfile.write(b)
        self.log_hit(code)
    def log_hit(self, code):
        # fleet standard (#167): "ts ip path method code" per request, X-Forwarded-For honored
        if self.path == "/health": return
        try:
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.ip()
            open(BASE + "hits.log", "a").write("%s %s %s %s %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ip[:45], self.path[:120], self.command, code))
        except Exception as e:
            try: open(BASE + "hits.err", "a").write(repr(e))
            except Exception: pass
    def page(self, body, code=200, ctype="text/html; charset=utf-8"):
        self.send(code, body.encode(), ctype)
    def usage_ok(self, ident):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with lock:
            c = db()
            row = c.execute("SELECT cnt FROM usage WHERE day=? AND ident=?", (day, ident)).fetchone()
            n = (row[0] if row else 0)
            allowed = n < FREE_PER_IP_DAY
            if allowed: c.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?)", (day, ident, n+1))
            c.commit(); c.close()
        return allowed
    def send_raw_md(self, md):
        b = md.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.log_hit(200)
        if not getattr(self, '_head', False): self.wfile.write(b)
    def spend(self, key, ident):
        with lock:
            c = db()
            if key and key_valid(key):
                c.execute("UPDATE keys SET credits=credits-1 WHERE key=?", (key,))
                c.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day,ident) DO UPDATE SET cnt=cnt+1", (time.strftime('%Y-%m-%d'), ident))
            c.commit()
            left = c.execute("SELECT credits FROM keys WHERE key=?", (key,)).fetchone() if (key and key_valid(key)) else None
            c.close()
        return left[0] if left else None
    def do_HEAD(self):
        # HEAD = GET without a body; link checkers (e.g. HN) treat 501 as dead link
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def _mcp(self):
        # ---- MCP streamable-HTTP JSON-RPC (fleet-standard shape) ----
        def rpc(code, message, rid=None):
            return self.send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}, "application/json")
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(ln) if ln else b""
        except Exception:
            return rpc(-32700)
        self._consumed = ln
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return rpc(-32700, "parse error")
        m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"page2md-mcp","version":"1.0"},"instructions":"page2md: turn any webpage into clean Markdown for LLM/agent pipelines. Free tier 10 req/day/IP; paid 1 USDC = 1000 req (on-chain, or $3 card link on the landing page). Free 1000-req key: the trial_key tool."}})
        if m == "ping":
            return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{}})
        if m == "tools/list":
            return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"tools":[
                {"name":"convert","description":"Fetch a webpage and return it as clean Markdown. Use for any URL an LLM/agent needs to read.","inputSchema":{"type":"object","properties":{"url":{"type":"string","description":"http(s) URL of the page to convert"},"key":{"type":"string","description":"page2md key (optional; free tier applies without one)"}},"required":["url"]}},
                {"name":"convert_raw","description":"Convert raw HTML you already have into clean Markdown (no fetching).","inputSchema":{"type":"object","properties":{"html":{"type":"string","description":"raw HTML to convert"},"key":{"type":"string"}},"required":["html"]}},
                {"name":"trial_key","description":"Get a free 1000-request page2md key (1 per IP per day).","inputSchema":{"type":"object","properties":{},"required":[]}}]}})
        if m == "tools/call":
            a = req.get("params", {}).get("arguments") or {}
            tool = req.get("params", {}).get("name")
            ip = self.ip()
            try:
                if tool == "trial_key":
                    path, body, method = "/v1/trial", b"", "GET"
                elif tool == "convert":
                    u = str(a.get("url") or "")
                    if not u.startswith(("http://", "https://")):
                        return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"error: url must be an http(s) URL"}],"isError":True}})
                    qs = {"url": u}
                    if a.get("key"): qs["key"] = str(a["key"])
                    path, body, method = "/v1/convert?" + urllib.parse.urlencode(qs), b"", "GET"
                elif tool == "convert_raw":
                    h = str(a.get("html") or "")
                    if not h.strip():
                        return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"error: html is required"}],"isError":True}})
                    qs = {"url": "about:input"}
                    if a.get("key"): qs["key"] = str(a["key"])
                    path, body, method = "/v1/html?" + urllib.parse.urlencode(qs), h.encode("utf-8"), "POST"
                else:
                    return rpc(-32602, "unknown tool: %s" % tool, rid)
                con = http.client.HTTPConnection("127.0.0.1", int(os.environ.get("PORT", "8077")), timeout=60)
                con.request(method, path, body=body, headers={"X-Forwarded-For": ip, "Accept": "application/json", "Content-Length": str(len(body))})
                r = con.getresponse(); data = r.read(); con.close()
                try:
                    j = json.loads(data)
                except Exception:
                    return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"upstream %s: %s" % (r.status, data[:200].decode("utf-8","replace"))}],"isError":True}})
                if r.status != 200 or j.get("error"):
                    return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"error (%s): %s" % (r.status, j.get("error") or data[:200].decode("utf-8","replace"))}],"isError":True}})
                if "markdown" in j:
                    return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":j["markdown"]}],"metadata":{"url":j.get("url"),"credits_left":j.get("credits_left")}}})
                return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(j)}]}})
            except Exception as ex:
                return self.send(200, {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"error: %s" % ex}],"isError":True}})
        return rpc(-32601, "unknown method: %s" % m, rid)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/" or u.path == "/index.html":
            return self.page(open(BASE+"index.html").read())
        if u.path == "/v1/stats":
            with lock:
                c = db()
                trials = c.execute("SELECT COUNT(*) FROM keys WHERE note LIKE 'trial:%'").fetchone()[0]
                notes = [r[0] for r in c.execute("SELECT note FROM keys WHERE note LIKE 'trial:%'").fetchall()]
                kc = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
                pc = c.execute("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM claims").fetchone()
                c.close()
            srcs = {}
            for n in notes:
                p = n.split(":")
                s = p[2] if len(p) > 2 and p[2] else "direct"
                srcs[s] = srcs.get(s, 0) + 1
            anon = {"total": 0, "endpoints": {}}
            try:
                cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
                eps = {}
                for line in open(BASE + "hits.log"):
                    p = line.split()
                    if len(p) < 5 or p[4] != "200" or p[2] in ("/health", "/v1/stats"):
                        continue
                    if p[0] < cut:
                        continue
                    e = eps.setdefault(p[2], {"calls": 0, "ips": set()})
                    e["calls"] += 1
                    e["ips"].add(p[1])
                for v in eps.values():
                    v["ips"] = len(v["ips"])
                anon["total"] = sum(v["calls"] for v in eps.values())
                anon["endpoints"] = eps
            except Exception:
                pass
            return self.send(200, {"service": "page2md", "trials_issued": trials,
                "trial_sources": srcs, "keys_issued": kc, "anonymous_calls": anon,
                "paid_claims": {"count": pc[0], "credits": pc[1]},
                "price_usd_per_1000": 1, "trial_limit": "10/day/IP, 1000 requests",
                "registry": "net.bigfloppy.page2md/page2md",
                "wallet": "0x6CA3C09ff703dEf98fa3741F255e9eb2a69718ba",
                "card_link": "https://buy.stripe.com/eVq14o3Cicys37f43U8Vi0n"})
        if u.path == "/health":
            try:
                with lock:
                    c = db()
                    notes = [r[0] for r in c.execute("SELECT note FROM keys WHERE note LIKE 'trial:%'").fetchall()]
                    c.close()
                srcs = {}
                for n in notes:
                    parts = n.split(":")
                    s_ = parts[2] if len(parts) > 2 else "direct"
                    srcs[s_] = srcs.get(s_, 0) + 1
            except Exception:
                srcs = {}
            return self.send(200, {"ok":True,"service":"page2md","wallet":WALLET,"price_usdc":PRICE_USDC,"trials_issued":len(notes) if 'notes' in dir() else None,"trial_sources":srcs})
        if u.path == "/v1/convert":
            url = (q.get("url") or [None])[0]
            key = (q.get("key") or [None])[0]
            ident = key if key and key_valid(key) else ("ip:" + self.ip())
            if not url: return self.send(400, {"error":"?url= required"})
            if not (key and key_valid(key)) and not self.usage_ok(ident):
                return self.send(429, {"error":"free limit reached (10/day/IP). Paid: 1 USDC = 1000 requests - send USDC on Base to 0x6CA3C09ff703dEf98fa3741F255e9eb2a69718ba then POST /v1/claim {\"tx\":\"0xHASH\"}"})
            html = None
            with cache_lock:
                e = CACHE.get(url)
            if e and time.time() - e[0] < CACHE_TTL:
                html = e[1]
            if html is None:
                try:
                    html = fetch(url)
                except Exception as e:
                    return self.send(502, {"error":"fetch failed: %s" % e})
                with cache_lock:
                    if len(CACHE) > 200: CACHE.clear()
                    CACHE[url] = (time.time(), html)
            p = MD(url); p.feed(html)
            left = self.spend(key, ident)
            md = clean(p.result())
            if "text/markdown" in (self.headers.get("Accept") or ""):
                return self.send_raw_md(md)
            return self.send(200, {"url":url, "credits_left": left, "markdown": md})
        if u.path == "/v1/balance":
            key = (q.get("key") or [None])[0]
            if not key_valid(key): return self.send(404, {"error":"unknown key"})
            c = db(); row = c.execute("SELECT credits FROM keys WHERE key=?", (key,)).fetchone(); c.close()
            return self.send(200, {"key":key, "credits": row[0]})
        if u.path == "/v1/trial":
            src = (q.get("src") or [None])[0]
            src = src if src and re.match(r"^[A-Za-z0-9_-]{1,24}$", src) else None
            ip = self.ip()
            day = time.strftime("%Y-%m-%d", time.gmtime())
            with lock:
                c = db()
                row = c.execute("SELECT day FROM trials WHERE ip=?", (ip,)).fetchone()
                if row and row[0] == day:
                    c.close()
                    return self.send(429, {"error":"trial key already issued from this IP today (keys are free, 1000 requests each)"})
                if row:
                    c.execute("UPDATE trials SET day=? WHERE ip=?", (day, ip))
                else:
                    c.execute("INSERT INTO trials VALUES(?,?)", (ip, day))
                key = "k_" + secrets.token_hex(16)
                c.execute("INSERT INTO keys VALUES(?,?,?,?)", (key, 1000, time.time(), "trial:" + ip + (":" + src if src else "")))
                c.commit(); c.close()
            return self.send(200, {"ok": True, "key": key, "credits": 1000, "note": "free trial key, 1000 requests; renewal is paid: 1 USDC = 1000 requests via POST /v1/claim, or $3 card on the landing page"})

        if u.path == "/.well-known/mcp-registry-auth":
            try: return self.send(200, open(BASE+".well-known/mcp-registry-auth","rb").read(), "application/octet-stream")
            except OSError: return self.send(404, {"error":"not available"})

        return self.send(404, {"error":"not found", "docs":"/"})
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/mcp":
            return self._mcp()
        if u.path == "/v1/html":
            ln = int(self.headers.get("Content-Length", 0))
            if ln > 3000000: return self.send(413, {"error":"body too large (max 3MB)"})
            html = self.rfile.read(ln).decode("utf-8", "replace")
            self._consumed = ln
            if not html.strip(): return self.send(400, {"error":"empty body; send raw HTML"})
            q = urllib.parse.parse_qs(u.query)
            url = (q.get("url") or [None])[0] or "about:input"
            key = (q.get("key") or [None])[0]
            ident = key if key and key_valid(key) else ("ip:" + self.ip())
            if not (key and key_valid(key)) and not self.usage_ok(ident):
                return self.send(429, {"error":"free limit reached (10/day/IP). Paid: 1 USDC = 1000 requests - send USDC on Base to 0x6CA3C09ff703dEf98fa3741F255e9eb2a69718ba then POST /v1/claim {\"tx\":\"0xHASH\"}"})
            p = MD(url); p.feed(html)
            left = self.spend(key, ident)
            return self.send(200, {"url":url, "credits_left": left, "markdown": clean(p.result())})
        if self.path == "/v1/claim":
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._consumed = ln
            tx = (body.get("tx") or "").strip()
            credits, chain = verify_tx(tx)
            if not credits:
                return self.send(402, {"error":"no qualifying USDC transfer to our wallet found for that tx"})
            if credits < 1000000:
                return self.send(402, {"error":"amount too small (min 1 USDC)", "credits":0})
            n = credits // 1000000
            with lock:
                c = db()
                if c.execute("SELECT 1 FROM claims WHERE tx=?", (tx.lower(),)).fetchone():
                    c.close(); return self.send(409, {"error":"tx already claimed"})
                key = "k_" + secrets.token_hex(16)
                c.execute("INSERT INTO claims VALUES(?,?,?,?)", (tx.lower(), key, n*1000, time.time()))
                c.execute("INSERT INTO keys VALUES(?,?,?,?)", (key, n*1000, time.time(), "tx:"+tx[:12]))
                c.commit(); c.close()
            return self.send(200, {"ok":True, "key":key, "credits": n*1000})
        return self.send(404, {"error":"not found"})

def key_valid(k):
    if not k or not k.startswith("k_"): return False
    c = db(); r = c.execute("SELECT credits FROM keys WHERE key=?", (k,)).fetchone(); c.close()
    return bool(r and r[0] > 0)

def main():
    open(BASE+'app.pid','w').write(str(os.getpid()))
    port = int(os.environ.get("PORT", "8077"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.serve_forever()

if __name__ == "__main__":
    main()
