"""
Universal product extractor: give it a product URL from (almost) any shop.

How it works (cheapest first, results are MERGED so each source fills gaps):
  1. Normalize URL  -> strip tracking junk, resolve platform
  2. Fetch HTML     -> direct HTTP, or scraper API first for hard sites
                       (redirects are re-checked against the SSRF filter)
  3. Extract        -> JSON-LD, platform parser (Amazon / Shopify),
                       microdata, OpenGraph/meta, __NEXT_DATA__
  4. Caller input   -> target_audience and tone are set by the user, not scraped

Usage:
    python product_extractor.py URL [URL ...]
    python product_extractor.py -f urls.txt -o results.json -w 3
    python product_extractor.py URL --audience "students" --tone casual --tone witty

Deps: pip install httpx beautifulsoup4 pydantic
Env:  SCRAPEDO_TOKEN or SCRAPER_API_KEY  (needed for Amazon/Flipkart/etc.)
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

TEST_URLS = [
    "https://www.amazon.in/dp/B08HNGK3B7",
    "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
]


# =====================================================================
# Model
# =====================================================================
class ProductData(BaseModel):
    product_name: str = ""
    category: str = ""
    description: str = ""
    key_features: list[str] = []
    url: str = ""         # final URL after redirects


MERGE_FIELDS = ("product_name", "category", "description",
                "key_features")


# =====================================================================
# Platforms
# =====================================================================
PLATFORM_HOSTS = {
    "amazon": ("amazon.", "amzn."),
    "flipkart": ("flipkart.com", "fkrt."),
    "myntra": ("myntra.com", "myntr."),
    "meesho": ("meesho.com",),
    "ajio": ("ajio.com",),
    "nykaa": ("nykaa.com", "nykaafashion.com"),
    "snapdeal": ("snapdeal.com",),
    "tatacliq": ("tatacliq.com",),
    "croma": ("croma.com",),
    "reliancedigital": ("reliancedigital.in",),
    "walmart": ("walmart.com",),
    "ebay": ("ebay.",),
    "aliexpress": ("aliexpress.",),
    "etsy": ("etsy.com",),
}
# These block plain HTTP clients aggressively: go to the scraper API first.
HARD_PLATFORMS = {"amazon", "flipkart", "myntra", "meesho", "ajio", "nykaa",
                  "walmart", "ebay", "aliexpress", "etsy"}
# Product id lives in the path; the query string is only tracking.
STRIP_ALL_QUERY = {"myntra", "meesho", "ajio"}


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, needles in PLATFORM_HOSTS.items():
        if any(n in host for n in needles):
            return name
    return "generic"


# =====================================================================
# URL normalization
# =====================================================================
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "yclid", "mc_cid", "mc_eid",
    "ref", "ref_", "referrer", "spm", "tag", "cmpid", "affid", "affextparam1",
    "affextparam2", "_encoding", "psc", "th", "qid", "sr", "aref", "content-id",
}
TRACKING_PREFIXES = ("utm_", "pd_rd_", "pf_rd_", "hsa_")
_AMAZON_ASIN = re.compile(r"/(?:dp|gp/aw/d|gp/product|gp/offer-listing)/([A-Z0-9]{10})(?:[/?]|$)")


def normalize_url(url: str) -> str:
    """One stable form per product: https, lowercase host, no tracking params,
    sorted query, no fragment/credentials/default port/trailing slash.
    Amazon -> /dp/<ASIN>; Flipkart keeps only `pid`; Myntra/Meesho/Ajio drop
    the whole query string."""
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url

    p = urlparse(url)
    scheme, host = p.scheme.lower(), (p.hostname or "").lower()
    if not host:
        return url

    platform = detect_platform(url)
    if platform == "amazon":
        m = _AMAZON_ASIN.search(p.path + "/")
        if m:
            return f"https://{host}/dp/{m.group(1)}"

    try:
        port = p.port
    except ValueError:
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"

    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    pairs = parse_qsl(p.query, keep_blank_values=True)
    if platform in STRIP_ALL_QUERY:
        pairs = []
    elif platform == "flipkart":
        pairs = [(k, v) for k, v in pairs if k == "pid"]
    else:
        pairs = [(k, v) for k, v in pairs
                 if k.lower() not in TRACKING_PARAMS
                 and not k.lower().startswith(TRACKING_PREFIXES)]
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(pairs)), ""))


# =====================================================================
# Safety (SSRF)
# =====================================================================
def is_safe_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except socket.gaierror:
        return False
    return True


# =====================================================================
# Fetching
# =====================================================================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}
BLOCK_MARKERS = ("captcha", "robot check", "access denied", "unusual traffic")


def _looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code != 200:
        return True
    text = resp.text
    return len(text) < 500 or any(m in text[:5000].lower() for m in BLOCK_MARKERS)


def _get(client: httpx.Client, url: str, max_hops: int = 5) -> httpx.Response:
    """GET that follows redirects manually so EVERY hop passes the SSRF check."""
    for _ in range(max_hops):
        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL: {url}")
        r = client.get(url)
        loc = r.headers.get("location")
        if r.is_redirect and loc:
            url = urljoin(str(r.url), loc)
            continue
        return r
    raise RuntimeError("Too many redirects")


def _fetch_direct(url: str, log: list[str]) -> Optional[tuple[str, str]]:
    try:
        with httpx.Client(headers=HEADERS, timeout=15) as c:
            r = _get(c, url)
            if not _looks_blocked(r):
                return r.text, str(r.url)
            log.append(f"direct: blocked (status {r.status_code})")
    except (httpx.HTTPError, RuntimeError) as e:
        log.append(f"direct: {type(e).__name__}: {e}")
    return None


def _fetch_scraper(url: str, log: list[str], platform: str = "generic") -> Optional[tuple[str, str]]:
    """Scraping-API fallback. Uses Scrape.do if SCRAPEDO_TOKEN is set,
    otherwise ScraperAPI if SCRAPER_API_KEY is set."""
    token="403dafca012342cfa277355e3b5b15bbb2ada0d2f61"
    if (token):
        # Scrape.do: httpx URL-encodes `url` for us (never pre-quote it).
        params = {"token": token, "url": url}
        if platform != "amazon":          # Amazon pages are server-rendered
            params["render"] = "true"     # JS rendering costs more credits
        if os.getenv("SCRAPEDO_SUPER") == "1":
            params["super"] = "true"      # residential proxies (higher cost / plan)
        if os.getenv("SCRAPEDO_GEO"):
            params["geoCode"] = os.getenv("SCRAPEDO_GEO")  # e.g. "in" (may need a paid plan)
        api, name = "https://api.scrape.do/", "scrape.do"
    elif (key := os.getenv("SCRAPER_API_KEY")):
        params = {"api_key": key, "url": url, "render": "true", "country_code": "in"}
        api, name = "https://api.scraperapi.com/", "scraperapi"
    else:
        log.append("scraper: neither SCRAPEDO_TOKEN nor SCRAPER_API_KEY is set")
        return None
    try:
        r = httpx.get(api, params=params, timeout=70)
        if not _looks_blocked(r):
            return r.text, url
        log.append(f"{name}: blocked or failed (status {r.status_code}) {r.text[:150]!r}")
    except httpx.HTTPError as e:
        log.append(f"{name}: {type(e).__name__}: {e}")
    return None


def fetch_html(url: str, platform: str) -> tuple[str, str]:
    """Returns (html, final_url). Hard sites try the scraper API first."""
    log: list[str] = []
    def scraper(u: str, lg: list[str]):
        return _fetch_scraper(u, lg, platform)

    order = ([scraper, _fetch_direct] if platform in HARD_PLATFORMS
             else [_fetch_direct, scraper])
    for fn in order:
        if (res := fn(url, log)):
            return res
    raise RuntimeError(f"Could not fetch {url} :: " + " | ".join(log))


def _get_json(url: str) -> Optional[dict]:
    try:
        with httpx.Client(headers={**HEADERS, "Accept": "application/json"}, timeout=15) as c:
            r = _get(c, url)
            if r.status_code == 200:
                return r.json()
    except (httpx.HTTPError, RuntimeError, ValueError):
        pass
    return None


# =====================================================================
# Helpers
# =====================================================================
def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _name_of(value) -> str:
    """brand/category can be a string or {'name': ...}."""
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list) and value:
        return _name_of(value[0])
    return _text(value)


# =====================================================================
# Extractors   (soup, url, html) -> Optional[ProductData]
# =====================================================================
def _iter_jsonld(obj) -> Iterator[dict]:
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_jsonld(o)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
            if k in obj:
                yield from _iter_jsonld(obj[k])


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(x in ("Product", "ProductGroup") for x in types)


def _from_jsonld(soup, url, html) -> Optional[ProductData]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _iter_jsonld(data):
            if not _is_product(item) or not _text(item.get("name")):
                continue
            return ProductData(
                product_name=_text(item.get("name")),
                category=_name_of(item.get("category")),
                description=_text(item.get("description")),
            )
    return None


def _li_texts(nodes) -> list[str]:
    out: list[str] = []
    for n in nodes:
        t = n.get_text(" ", strip=True)
        if len(t) > 2 and t not in out:
            out.append(t)
    return out


def _amazon_features(soup) -> list[str]:
    """'About this item' bullets across Amazon's different page layouts."""
    for sel in ("#feature-bullets li span.a-list-item",
                "#feature-bullets ul li",
                "#productFactsDesktopExpander ul li span.a-list-item",
                "#productFactsDesktopExpander ul li"):
        feats = _li_texts(soup.select(sel))
        if feats:
            return feats[:10]
    for h in soup.find_all(["h1", "h2", "h3", "h4", "span"]):
        if h.get_text(strip=True).lower() in ("about this item", "key features"):
            ul = h.find_next("ul")
            if ul and (feats := _li_texts(ul.find_all("li"))):
                return feats[:10]
    rows = []  # last resort: spec table as "Key: value"
    for tr in soup.select("#productDetails_techSpec_section_1 tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            rows.append(f"{th.get_text(' ', strip=True)}: {td.get_text(' ', strip=True)}")
    return rows[:10]


def _from_amazon(soup, url, html) -> Optional[ProductData]:
    """Amazon rarely ships JSON-LD, so read its stable element IDs."""
    title_el = soup.select_one("#productTitle")
    if not title_el:
        return None
    crumbs = [a.get_text(strip=True)
              for a in soup.select("#wayfinding-breadcrumbs_feature_div a")]
    desc_el = soup.select_one("#productDescription")
    return ProductData(
        product_name=title_el.get_text(strip=True),
        category=" > ".join(crumbs),
        description=desc_el.get_text(" ", strip=True) if desc_el else "",
        key_features=_amazon_features(soup),
    )


def _from_shopify(soup, url, html) -> Optional[ProductData]:
    """Any Shopify store exposes /products/<handle>.js with clean JSON."""
    if "cdn.shopify.com" not in html and "Shopify.shop" not in html:
        return None
    p = urlparse(url)
    m = re.match(r"^(.*?/products/[^/?#]+)", p.path)
    if not m:
        return None
    d = _get_json(f"{p.scheme}://{p.netloc}{m.group(1)}.js")
    if not d or not d.get("title"):
        return None
    return ProductData(
        product_name=d["title"],
        category=_text(d.get("type")),
        description=BeautifulSoup(d.get("description") or "", "html.parser").get_text(" ", strip=True),
    )


def _from_microdata(soup, url, html) -> Optional[ProductData]:
    scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)})
    if not scope:
        return None

    def prop(name: str) -> str:
        el = scope.find(attrs={"itemprop": name})
        if not el:
            return ""
        return (el.get("content") or el.get("href") or el.get("src")
                or el.get_text(" ", strip=True) or "").strip()

    name = prop("name")
    if not name:
        return None
    return ProductData(product_name=name, description=prop("description"))


def _from_meta(soup, url, html) -> Optional[ProductData]:
    def meta(*names: str) -> str:
        for n in names:
            t = soup.find("meta", property=n) or soup.find("meta", attrs={"name": n})
            if t and (t.get("content") or "").strip():
                return t["content"].strip()
        return ""

    h1 = soup.find("h1")
    name = (meta("og:title", "twitter:title")
            or (h1.get_text(" ", strip=True) if h1 else "")
            or (soup.title.string.strip() if soup.title and soup.title.string else ""))
    if not name:
        return None
    desc = meta("og:description", "description", "twitter:description")
    # Amazon's meta description is generic sales text ("Buy X ... at Amazon.in")
    # and sometimes belongs to a different variant: ignore it.
    if re.match(r"^Buy\b.*\bat Amazon\.", desc, re.I | re.S):
        desc = ""
    return ProductData(product_name=name, description=desc)


_PRICE_KEYS = ("sellingPrice", "selling_price", "offerPrice", "discountedPrice",
               "price", "mrp", "min_product_price")


def _find_product_like(obj, depth: int = 0) -> Optional[dict]:
    if depth > 12:
        return None
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("title") or obj.get("productName")
        if isinstance(name, str) and name and any(k in obj for k in _PRICE_KEYS):
            return obj
        for v in obj.values():
            if (r := _find_product_like(v, depth + 1)):
                return r
    elif isinstance(obj, list):
        for v in obj[:50]:
            if (r := _find_product_like(v, depth + 1)):
                return r
    return None


def _from_next_data(soup, url, html) -> Optional[ProductData]:
    """Best-effort for Next.js shops (e.g. Meesho). Heuristic: finds the first
    dict that has a name and a price-like key."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    try:
        data = json.loads(tag.string or tag.get_text() or "")
    except json.JSONDecodeError:
        return None
    node = _find_product_like(data)
    if not node:
        return None
    return ProductData(
        product_name=_text(node.get("name") or node.get("title") or node.get("productName")),
        description=_text(node.get("description")),
    )


# Order = priority. Earlier extractors win; later ones only fill blanks.
Extractor = Callable[[BeautifulSoup, str, str], Optional[ProductData]]
EXTRACTORS: list[Extractor] = [
    _from_jsonld, _from_amazon, _from_shopify,
    _from_microdata, _from_meta, _from_next_data,
]


def register_extractor(fn: Extractor, first: bool = False) -> Extractor:
    """Add your own platform parser (e.g. for Myntra):
        @register_extractor
        def _from_myntra(soup, url, html): ...
    """
    EXTRACTORS.insert(0 if first else len(EXTRACTORS), fn)
    return fn


def _merge(parts: list[Optional[ProductData]]) -> ProductData:
    """First non-empty value wins per field, so earlier parts have priority."""
    out = ProductData()
    for p in parts:
        if p is None:
            continue
        for f in MERGE_FIELDS:
            if not getattr(out, f) and getattr(p, f):
                setattr(out, f, getattr(p, f))
    return out


def extract_from_html(html: str, url: str) -> ProductData:
    """Pure function (no network except Shopify's .js). Easy to unit-test."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for fn in EXTRACTORS:
        try:
            results.append(fn(soup, url, html))
        except Exception as e:  # one broken parser must not kill the rest
            print(f"[extract] {fn.__name__} failed: {type(e).__name__}: {e}")
    return _merge(results)


# =====================================================================
# Public entry point
# =====================================================================
def get_product_from_url(url: str) -> ProductData:
    """Scrape product facts from `url`. `target_audience` and `tone` are
    supplied by the caller (never scraped) and are not cached, so one user's
    settings can't leak to another user who submits the same URL."""
    url = normalize_url(url)
    if not is_safe_url(url):
        raise ValueError("Invalid or disallowed URL.")

    cache_key = "product:" + hashlib.sha256(url.encode()).hexdigest()
    product: Optional[ProductData] = None
    

    if product is None:
        platform = detect_platform(url)
        html, final_url = fetch_html(url, platform)
        if os.getenv("SAVE_HTML"):
            fname = "debug_" + hashlib.sha256(url.encode()).hexdigest()[:10] + ".html"
            with open(fname, "w", encoding="utf-8") as fh:
                fh.write(html)
            print(f"[debug] saved fetched HTML to {fname}")

        product = extract_from_html(html, final_url)
        if not product.product_name:
            raise ValueError("Could not find product details on this page.")
        product.url = final_url

       

    result = product.model_copy(deep=True)
   
    return result


