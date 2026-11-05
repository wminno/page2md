#!/usr/bin/env python3
"""docproc - page2md extension: URL -> clean markdown + text chunks (PDF & HTML).

Additive module for page2md (see app.py). Stdlib-only except pypdf (optional;
graceful degradation: PDF requests return 503 if pypdf is unavailable).
"""
import io, re, socket, ipaddress, urllib.request, urllib.parse

MAX_BYTES = 25 * 1024 * 1024   # 25MB fetch cap
MAX_PAGES = 500
CHUNK_SIZE = 900
MAX_CHUNKS = 400

try:
    import warnings
    import pypdf
    warnings.filterwarnings("ignore")
    from pypdf import PdfReader
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False


class BlockedHost(ValueError):
    pass


def fetch_bytes(url):
    """SSRF-guarded fetch returning (data, content_type). Mirrors app.fetch guards."""
    if not url.startswith("http"):
        url = "https://" + url
    u = urllib.parse.urlparse(url)
    if not u.hostname:
        raise ValueError("bad url")
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                raise BlockedHost("blocked host")
    except socket.gaierror:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; page2md-docproc/1.0)"})
    data = bytearray()
    with urllib.request.urlopen(req, timeout=25) as r:
        ctype = r.headers.get("Content-Type", "")
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("payload exceeds %d bytes" % MAX_BYTES)
    return bytes(data), ctype


def pdf_to_markdown(data):
    """PDF bytes -> (markdown, npages). Page breaks marked with an HTML comment."""
    if not HAVE_PYPDF:
        raise LookupError("pdf support unavailable (pypdf not installed)")
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > MAX_PAGES:
        raise ValueError("pdf has %d pages (max %d)" % (len(reader.pages), MAX_PAGES))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            t = (page.extract_text() or "").strip()
        except Exception:
            t = ""
        pages.append("<!-- page %d -->\n%s" % (i + 1, t))
    return "\n\n".join(pages), len(reader.pages)


def chunk_text(md, size=CHUNK_SIZE):
    """Split markdown into paragraph-aligned chunks <= size chars, splitting
    oversized paragraphs on sentences. Returns list of plain strings."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", md) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(p) > size:
            # flush current, then sentence-split the monster paragraph
            if cur:
                out.append(cur); cur = ""
            for snt in re.split(r"(?<=[.!?]) +", p):
                if len(snt) > size:
                    snt = snt[:size]
                if len(cur) + len(snt) + 1 <= size:
                    cur = (cur + " " + snt).strip()
                else:
                    if cur:
                        out.append(cur)
                    cur = snt
        elif len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur:
                out.append(cur)
            cur = p
        if len(out) >= MAX_CHUNKS:
            break
    if cur and len(out) < MAX_CHUNKS:
        out.append(cur)
    return out


def process(url, want_chunks=False):
    """URL -> result dict. Raises ValueError/LookupError/BlockHost/OSError on failure."""
    data, ctype = fetch_bytes(url)
    import hashlib
    sha = hashlib.sha256(data).hexdigest()[:16]
    is_pdf = data[:5] == b"%PDF-" or "pdf" in ctype.lower()
    if is_pdf:
        md, npages = pdf_to_markdown(data)
        doc_type = "application/pdf"
    else:
        html = data.decode("utf-8", "replace")
        from app import MD, clean
        p = MD(url)
        p.feed(html)
        md = clean(p.result())
        npages = None
        doc_type = ctype or "text/html"
    res = {"url": url, "type": doc_type, "sha256_16": sha, "chars": len(md)}
    if npages:
        res["pages"] = npages
    if want_chunks:
        ch = chunk_text(md)
        res["chunks"] = ch
        res["n_chunks"] = len(ch)
    else:
        res["markdown"] = md
    return res
