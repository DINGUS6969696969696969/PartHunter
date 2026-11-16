#!/usr/bin/env python3
"""Serve PartHunter and run live searches across AU parts stores and the wider web."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import argparse
import base64
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser


APP_DIR = Path(__file__).resolve().parent
APP_VERSION = "5"
DEFAULT_PORT = 8765
PORT_FILE = APP_DIR / ".parthunter-port"
# Shopify stores answer /search/suggest.json. More can be added from the app's "Search sources" panel.
SEARCH_STORES = (
    ("A1 Autoparts Niddrie", "https://www.a1autopartsniddrie.com.au"),
    ("Auto One", "https://www.autoone.com.au"),
    ("Sunbury Auto Parts", "https://www.sunburyautoparts.com.au"),
)
# Non-Shopify stores. Each is read the way its site allows, so these are best effort: sites can change
# their pages or block automated requests, and the app then reports the store as not responding.
STORE_META = {
    "Supercheap Auto": ("https://www.supercheapauto.com.au", "html", "/search?q={q}"),
    "Repco": ("https://www.repco.com.au", "html", "/en/search?q={q}"),
    "Autobarn": ("https://www.autobarn.com.au", "html", "/search?q={q}"),
    "eBay Australia": ("https://www.ebay.com.au", "ebay", ""),
}
ALL_STORES = SEARCH_STORES + tuple((name, meta[0]) for name, meta in STORE_META.items())
NET_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError)
STATIC_PATHS = {"/", "/index.html", "/sw.js", "/manifest.webmanifest"}
MAX_QUERY_LENGTH = 80
MAX_EXTRA_STORES = 8
CACHE_TTL = 180
_cache = {}
_cache_lock = threading.Lock()
UA = "Mozilla/5.0 PartHunter parts search"


# ---------- matching ----------

class NameQuery(str):
    """A search by part name (e.g. "oil filter") rather than by part number."""


FANOUT_BRANDS = ("Ryco", "Bosch", "Sakura", "Wesfil", "Fram", "Repco", "Bendix", "Ferodo", "Castrol", "NGK", "Denso", "Penrite")
KNOWN_BRANDS = FANOUT_BRANDS + (
    "Mann", "Hengst", "Mahle", "Brembo", "Mobil", "Valvoline", "Toyota", "Nology", "Exide", "Century", "Optima", "Narva",
    "Osram", "Philips", "Monroe", "KYB", "Gates", "Dayco", "Nulon", "Motul", "Shell", "Liqui Moly", "Ridex", "Protex",
    "Trico", "ACDelco", "Hella", "Valeo", "Continental", "SKF", "Sachs", "Lucas", "Champion", "Delphi", "Gud", "Sca",
)


def detect_brand(title):
    for brand in KNOWN_BRANDS:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(brand) + r"(?![A-Za-z0-9])", title or "", re.I):
            return brand
    return ""


def pick_brand(title, vendor, store_name):
    found = detect_brand(title)
    if found:
        return found
    v, st = normalized(vendor), normalized(store_name)
    return vendor.strip() if v and v not in st and st not in v else "Other"


def normalized(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def tokens_of(text):
    return re.findall(r"[A-Za-z0-9]+", text or "")


def is_keyword_query(query):
    if isinstance(query, NameQuery):
        return True
    """'ryco oil filter' is a keyword search; 'Z436' or '3 397 007 298' is a part code."""
    return sum(1 for t in tokens_of(query) if t.isalpha() and len(t) > 2) >= 2


def part_code_pattern(query):
    pieces = tokens_of(query)
    if not pieces:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9])" + r"[\s._/-]*".join(map(re.escape, pieces)) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def is_parenthetical_match(title, pattern):
    matches = list(pattern.finditer(title))
    if not matches:
        return False
    for match in matches:
        prefix, suffix = title[:match.start()], title[match.end():]
        in_parentheses = (
            prefix.rfind("(") > prefix.rfind(")")
            and suffix.find(")") >= 0
            and (suffix.find("(") < 0 or suffix.find(")") < suffix.find("("))
        )
        if not in_parentheses:
            return False
    return True


def strip_html(value):
    return re.sub(r"<[^>]*>", " ", str(value or ""))


def find_match_kind(query, product):
    title = str(product.get("title") or "")
    body = strip_html(product.get("body"))
    if is_keyword_query(query):
        words = [w.lower() for w in tokens_of(query)]
        if all(w in title.lower() for w in words):
            return "exact"
        if all(w in f"{title} {body}".lower() for w in words):
            return "related"
        return None
    pattern = part_code_pattern(query)
    if pattern and pattern.search(title) and not is_parenthetical_match(title, pattern):
        return "exact"
    if pattern and pattern.search(f"{title} {body}"):
        return "related"
    q = normalized(query)
    if q and q in normalized(title):
        return "related"
    return None


def vehicle_hint(vehicle, text):
    """'mentions' when the make and model words appear in the listing text. This is not fitment proof."""
    words = [w.lower() for w in tokens_of(vehicle) if not re.fullmatch(r"(?:19|20)\d\d", w)][:2]
    if not words:
        return None
    hay = text.lower()
    if all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", hay) for w in words):
        return "mentions"
    return None


def valid_price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not price.is_finite() or price <= 0 or price > Decimal("1000000"):
        return None
    return price


# ---------- store search (Shopify) ----------

def parse_product(store_name, base_url, product, query, vehicle):
    # Anything the store itself returns for the query is kept; weak matches are labelled "similar".
    kind = find_match_kind(query, product) or "similar"
    price = valid_price(product.get("price"))
    if price is None:
        return None
    url = urljoin(base_url + "/", product.get("url") or "")
    if urlparse(url).netloc.lower().replace("www.", "") != urlparse(base_url).netloc.lower().replace("www.", ""):
        return None
    tags = product.get("tags") if isinstance(product.get("tags"), list) else []
    if not product.get("available"):
        availability, state = "Listed as unavailable online", "out"
    elif any("SPECIAL_ORDER" in str(tag).upper() for tag in tags):
        availability, state = "Listed as special order", "order"
    else:
        availability, state = "Listed as available online · confirm checkout", "listed"
    high = valid_price(product.get("price_max"))
    brand = pick_brand(str(product.get("title") or ""), str(product.get("vendor") or ""), store_name)
    text = f"{product.get('title') or ''} {strip_html(product.get('body'))} {' '.join(map(str, tags))}"
    return {
        "retailer": store_name,
        "title": str(product.get("title") or "Part listing"),
        "vendor": str(product.get("vendor") or ""),
        "brand": brand,
        "category": str(product.get("type") or ""),
        "price": float(price),
        "variable_price": high is not None and high != price,
        "available": bool(product.get("available")),
        "availability": availability,
        "state": state,
        "match_kind": kind,
        "vehicle_hint": vehicle_hint(vehicle, text),
        "url": url,
    }


def query_variants(query):
    """Z-436, Z 436 and Z436 are the same code, so stores are asked for each spelling."""
    compact = "".join(tokens_of(query))
    if is_keyword_query(query) or not compact or compact.lower() == query.lower():
        return [query]
    return [query, compact]


def name_variants(query):
    """Shopify suggest returns only 10 items per request, so a name search also asks for each common brand."""
    return [str(query)] + [f"{brand} {query}" for brand in FANOUT_BRANDS]


def search_shopify(store_name, base_url, query, vehicle):
    variants = name_variants(query) if isinstance(query, NameQuery) else query_variants(query)
    first = search_store_once(store_name, base_url, query, variants[0], vehicle)  # failure here marks the store as down
    more = []
    if len(variants) > 1:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in [pool.submit(search_store_once, store_name, base_url, query, t, vehicle) for t in variants[1:]]:
                try:
                    more.extend(future.result())
                except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                    pass
    merged, seen = [], set()
    for item in first + more:
        if item["url"] not in seen:
            seen.add(item["url"])
            merged.append(item)
    return merged


def search_store_once(store_name, base_url, query, term, vehicle):
    endpoint = base_url + "/search/suggest.json?" + urlencode({"q": term, "resources[type]": "product", "resources[limit]": 10})
    request = Request(endpoint, headers={"Accept": "application/json", "User-Agent": UA})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_500_000).decode("utf-8"))
    products = payload.get("resources", {}).get("results", {}).get("products", [])
    return [p for p in (parse_product(store_name, base_url, item, query, vehicle) for item in products) if p]


def public_host(host):
    """Only allow public internet hosts for user-added stores."""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        return False
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except (socket.gaierror, ValueError):
        return False


# ---------- other store types: WooCommerce, ordinary search pages, eBay ----------

LD_RE = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'


def ld_products(page):
    for block in re.findall(LD_RE, page, re.S | re.I):
        try:
            data = json.loads(unescape(block).strip())
        except ValueError:
            continue
        for node in walk_ld(data):
            kind = node.get("@type")
            if "Product" in (kind if isinstance(kind, list) else [kind]):
                yield node


def node_price(node):
    offers = node.get("offers") or {}
    best = None
    for offer in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification") if isinstance(offer.get("priceSpecification"), dict) else {}
        price = valid_price(offer.get("price") or offer.get("lowPrice") or spec.get("price"))
        currency = str(offer.get("priceCurrency") or spec.get("priceCurrency") or "AUD").upper()
        if price is None or currency != "AUD":
            continue
        if best is None or price < best[0]:
            best = (price, "OutOfStock" not in str(offer.get("availability") or ""), str(offer.get("url") or ""))
    return best


def search_html_store(store_name, base_url, template, query, vehicle):
    """Read a store's ordinary search page: product data on the page, else follow its product links."""
    final, page = fetch_text(base_url + template.format(q=quote_plus(str(query))), limit=1_500_000)
    results = []
    for node in ld_products(page):
        got = node_price(node)
        url = str(node.get("url") or (got[2] if got else ""))
        if not got or not url:
            continue
        brand = node.get("brand")
        vendor = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
        item = parse_product(store_name, base_url, {
            "title": unescape(str(node.get("name") or "")), "body": str(node.get("description") or ""),
            "price": str(got[0]), "available": got[1], "url": url, "vendor": vendor, "tags": [],
        }, query, vehicle)
        if item:
            results.append(item)
    if results:
        return results[:20]
    if re.search(r"captcha|access denied|are you a robot|unusual traffic", page[:6000], re.I):
        raise OSError("The site blocked the request")
    host = urlparse(base_url).netloc.lower().replace("www.", "")
    links, seen = [], set()
    for href in re.findall(r'<a[^>]+href=["\']([^"\'#]+)', page):
        full = urljoin(final, unescape(href))
        parts = urlparse(full)
        key = parts.netloc + parts.path
        if (parts.netloc.lower().replace("www.", "") == host and key not in seen and "search" not in parts.path
                and re.search(r"/(?:p|product|products|item)/|\.html?$", parts.path)):
            seen.add(key)
            links.append(full)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in [pool.submit(read_page, url, query, vehicle) for url in links[:8]]:
            try:
                item = future.result()
            except NET_ERRORS:
                continue
            if item:
                item["retailer"] = store_name
                results.append(item)
    return results


def search_woo(store_name, base_url, query, vehicle):
    url = base_url + "/wp-json/wc/store/v1/products?" + urlencode({"search": str(query), "per_page": 20})
    data = json.loads(fetch_text(url, limit=1_500_000)[1])
    if not isinstance(data, list):
        raise ValueError("Not a WooCommerce store")
    results = []
    for p in data:
        prices = p.get("prices") or {}
        if prices.get("currency_code") not in (None, "", "AUD"):
            continue
        try:
            price = Decimal(str(prices.get("price"))) / (Decimal(10) ** int(prices.get("currency_minor_unit", 2)))
        except (InvalidOperation, TypeError, ValueError):
            continue
        cats = ", ".join(c.get("name", "") for c in p.get("categories", []) if isinstance(c, dict))
        brands = p.get("brands") or []
        item = parse_product(store_name, base_url, {
            "title": unescape(str(p.get("name") or "")), "body": p.get("short_description") or p.get("description"),
            "price": str(price), "available": bool(p.get("is_in_stock", True)), "url": p.get("permalink") or "",
            "vendor": brands[0].get("name", "") if brands and isinstance(brands[0], dict) else "", "type": cats, "tags": [],
        }, query, vehicle)
        if item:
            results.append(item)
    return results


def search_ebay(store_name, base_url, query, vehicle):
    final, page = fetch_text(base_url + "/sch/i.html?" + urlencode({
        "_nkw": str(query), "LH_BIN": 1, "LH_ItemCondition": 1000, "_ipg": 60}), limit=1_500_000)
    results = []
    for m in re.finditer(r'<li[^>]*class="[^"]*s-(?:item|card)\b[^"]*"[^>]*>(.*?)</li>', page, re.S):
        block = m.group(1)
        link = re.search(r'href="(https://www\.ebay\.com\.au/itm/\d+)', block)
        title = re.search(r's-(?:item|card)__title[^>]*>(?:\s*<span[^>]*>)*([^<]+)', block)
        price = re.search(r's-(?:item|card)__price[^>]*>(?:\s*<span[^>]*>)*\s*(?:AU\s*)?\$\s*([\d,]+\.\d\d)', block)
        if not (link and title and price) or unescape(title.group(1)).strip().lower().startswith("shop on ebay"):
            continue
        item = parse_product(store_name, base_url, {
            "title": unescape(title.group(1)).strip(), "price": price.group(1).replace(",", ""),
            "available": True, "url": link.group(1), "vendor": "", "tags": [],
        }, query, vehicle)
        if item:
            item["availability"] = "Buy It Now · new condition · check seller"
            results.append(item)
    if not results and re.search(r"captcha|Pardon Our Interruption|are you a robot", page, re.I):
        raise OSError("eBay blocked the request")
    return results


def search_auto(store_name, base_url, query, vehicle):
    """For stores the user adds: try Shopify, then WooCommerce, then the ordinary search page."""
    answered, last = False, None
    for fn in (search_shopify, search_woo):
        try:
            found = fn(store_name, base_url, query, vehicle)
            answered = True
            if found:
                return found
        except NET_ERRORS as exc:
            last = exc
    try:
        return search_html_store(store_name, base_url, "/search?q={q}", query, vehicle)
    except NET_ERRORS as exc:
        if answered:
            return []
        raise last or exc


def search_store(store_name, base_url, query, vehicle):
    meta = STORE_META.get(store_name)
    if meta and meta[1] == "ebay":
        return search_ebay(store_name, base_url, query, vehicle)
    if meta:
        return search_html_store(store_name, base_url, meta[2], query, vehicle)
    if (store_name, base_url) in SEARCH_STORES:
        return search_shopify(store_name, base_url, query, vehicle)
    return search_auto(store_name, base_url, query, vehicle)


# ---------- keep web results to car parts ----------

AUTO_RE = re.compile(
    r"\b(?:car|cars|auto|automotive|vehicle|truck|ute|4wd|4x4|suv|engine|oil filter|fuel filter|cabin filter|brake|brakes|rotor|"
    r"spark plug|wiper|alternator|radiator|clutch|suspension|shock absorber|exhaust|gearbox|timing belt|serpentine|tyre|tyres|"
    r"headlight|toyota|mazda|holden|ford|nissan|mitsubishi|subaru|honda|hyundai|kia|volkswagen|vw|bmw|mercedes|audi|suzuki|"
    r"isuzu|jeep|landcruiser|hilux|ranger|commodore|falcon|camry|corolla|wrx|navara|triton|pajero|lexus|volvo|peugeot|renault)\b", re.I)
STRONG_CAR_RE = re.compile(r"\b(?:car|cars|auto|automotive|vehicle|truck|ute|4wd|4x4|suv)\b", re.I)
NON_AUTO_RE = re.compile(
    r"\b(?:fridge|refrigerator|water filter|pool|coffee|vacuum|air purifier|hepa|kitchen|furnace|hvac|aquarium|bicycle|bike|"
    r"phone case|laptop|toy|doll|jewellery|jewelry)\b", re.I)
AUTO_SELLER_RE = re.compile(r"supercheap|repco|autobarn|burson|auto ?one|autopro|sparesbox|autospares|auto ?parts|autoparts", re.I)


def looks_automotive(title, seller, query):
    """Drop shopping results that are clearly not car parts. Exact part codes are specific enough to keep."""
    text = title or ""
    if NON_AUTO_RE.search(text) and not STRONG_CAR_RE.search(text):
        return False
    if AUTO_RE.search(text) or AUTO_SELLER_RE.search(seller or ""):
        return True
    return not isinstance(query, NameQuery) and find_match_kind(query, {"title": text}) == "exact"


def web_query(query):
    return f"{query} car auto part" if isinstance(query, NameQuery) else f"{query} auto part"


# ---------- web search (Google Shopping through a search API) ----------

def web_results(payload, query, vehicle):
    results = []
    for item in payload.get("shopping_results", []):
        price = valid_price(item.get("extracted_price"))
        if price is None:
            m = re.search(r"(\d[\d,]*\.?\d*)", str(item.get("price") or ""))
            price = valid_price(m.group(1).replace(",", "")) if m else None
        if price is None:
            continue
        title = str(item.get("title") or "Shopping result")
        link = item.get("offers_link") or item.get("product_link") or item.get("link")
        if not link or urlparse(str(link)).scheme not in {"https", "http"}:
            continue
        if not looks_automotive(title, str(item.get("seller") or item.get("source") or ""), query):
            continue
        delivery = item.get("delivery")
        if isinstance(delivery, dict):
            delivery = delivery.get("text") or delivery.get("description")
        results.append({
            "retailer": str(item.get("seller") or item.get("source") or item.get("domain") or "Web seller"),
            "title": title,
            "vendor": str(item.get("source") or "Google Shopping"),
            "brand": detect_brand(title) or "Other",
            "category": "Web shopping result",
            "price": float(price),
            "variable_price": False,
            "available": True,
            "availability": str(delivery or "Check availability at seller"),
            "state": "listed",
            "match_kind": find_match_kind(query, {"title": title}) or "related",
            "vehicle_hint": vehicle_hint(vehicle, title),
            "url": str(link),
            "web_result": True,
        })
    return results


def search_searchapi(query, vehicle, key):
    url = "https://www.searchapi.io/api/v1/search?" + urlencode({"engine": "google_shopping", "q": web_query(query), "gl": "au", "hl": "en"})
    request = Request(url, headers={"Accept": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "PartHunter/4.0"})
    with urlopen(request, timeout=20) as response:
        return web_results(json.loads(response.read(2_000_000).decode("utf-8")), query, vehicle)


def search_serpapi(query, vehicle, key):
    url = "https://serpapi.com/search.json?" + urlencode({
        "engine": "google_shopping", "q": web_query(query), "gl": "au", "hl": "en",
        "google_domain": "google.com.au", "api_key": key,
    })
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "PartHunter/4.0"})
    with urlopen(request, timeout=20) as response:
        return web_results(json.loads(response.read(2_000_000).decode("utf-8")), query, vehicle)


# ---------- open-web crawl (no key needed) ----------
# Finds product pages on any site via search engines, then reads each page's price from its
# structured product data (JSON-LD, or product meta tags). Search engines can block automated
# requests at times, so this is best effort and reported as its own source.

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SKIP_HOSTS = ("google.", "bing.", "duckduckgo.", "youtube.", "facebook.", "instagram.", "pinterest.",
              "reddit.", "wikipedia.", "tiktok.", "twitter.", "linkedin.", "quora.")


def fetch_text(url, limit=700_000, timeout=10):
    request = Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                                    "Accept-Language": "en-AU,en;q=0.9"})
    with urlopen(request, timeout=timeout) as response:
        final = response.geturl()
        raw = response.read(limit)
        charset = response.headers.get_content_charset() or "utf-8"
    return final, raw.decode(charset, "replace")


def ddg_links(page):
    links = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"', page):
        href = unescape(m.group(1))
        target = parse_qs(urlparse(href).query).get("uddg")
        links.append(target[0] if target else href)
    return links


def bing_links(page):
    links = []
    for m in re.finditer(r'<li class="b_algo".*?<a[^>]+href="([^"]+)"', page, re.S):
        href = unescape(m.group(1))
        coded = parse_qs(urlparse(href).query).get("u")
        if coded and coded[0].startswith("a1"):
            try:
                data = coded[0][2:]
                href = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
        links.append(href)
    return links


def ollama_urls(query, key):
    """Ollama's hosted web search (needs a free ollama.com API key); returns page links only."""
    body = json.dumps({"query": f"{query} auto part buy price australia", "max_results": 10}).encode("utf-8")
    request = Request("https://ollama.com/api/web_search", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "PartHunter/4.0"})
    with urlopen(request, timeout=20) as response:
        data = json.loads(response.read(1_000_000).decode("utf-8"))
    return [r["url"] for r in data.get("results", []) if isinstance(r, dict) and r.get("url")]


def discover_urls(query, ollama_key="", engines=True):
    urls, tried, failed, first_error = [], 0, 0, None
    if ollama_key:
        tried += 1
        try:
            urls.extend(ollama_urls(query, ollama_key))
        except NET_ERRORS as exc:
            failed, first_error = failed + 1, exc
    if engines and not urls:
        q = f"{query} auto part buy price australia"
        for endpoint, parser in (
            ("https://html.duckduckgo.com/html/?" + urlencode({"q": q, "kl": "au-en"}), ddg_links),
            ("https://www.bing.com/search?" + urlencode({"q": q, "setmkt": "en-AU", "cc": "AU"}), bing_links),
        ):
            tried += 1
            try:
                urls.extend(parser(fetch_text(endpoint, timeout=10)[1]))
            except NET_ERRORS as exc:
                failed, first_error = failed + 1, first_error or exc
    if tried and failed == tried:
        raise OSError(f"Page search failed: {str(first_error)[:120]}")
    chosen, seen = [], set()
    for url in urls:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host or host in ("x.com", "www.x.com") or any(h in host for h in SKIP_HOSTS):
            continue
        key = (host, parts.path)
        if key not in seen:
            seen.add(key)
            chosen.append(url)
    chosen.sort(key=lambda u: not (urlparse(u).hostname or "").endswith(".au"))
    return chosen[:14]


def walk_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from walk_ld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity", "hasVariant"):
            if key in node:
                yield from walk_ld(node[key])


def page_offer(page):
    """Return (name, price, available) from structured product data, or None."""
    best = None
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    for block in blocks:
        try:
            data = json.loads(unescape(block).strip())
        except ValueError:
            continue
        for node in walk_ld(data):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" not in kinds:
                continue
            offers = node.get("offers") or {}
            for offer in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(offer, dict):
                    continue
                spec = offer.get("priceSpecification") if isinstance(offer.get("priceSpecification"), dict) else {}
                price = valid_price(offer.get("price") or offer.get("lowPrice") or spec.get("price"))
                currency = str(offer.get("priceCurrency") or spec.get("priceCurrency") or "AUD").upper()
                if price is None or currency != "AUD":
                    continue
                available = "OutOfStock" not in str(offer.get("availability") or "")
                if best is None or price < best[1]:
                    best = (str(node.get("name") or ""), price, available)
    if best:
        return best
    amount = re.search(r'property=["\']product:price:amount["\'][^>]*content=["\']([\d.,]+)', page, re.I) or \
        re.search(r'content=["\']([\d.,]+)["\'][^>]*property=["\']product:price:amount', page, re.I)
    currency = re.search(r'property=["\']product:price:currency["\'][^>]*content=["\'](\w+)', page, re.I)
    title = re.search(r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)', page, re.I)
    price = valid_price(amount.group(1).replace(",", "")) if amount else None
    if price is not None and (not currency or currency.group(1).upper() == "AUD"):
        return (unescape(title.group(1)) if title else "", price, True)
    return None


def read_page(url, query, vehicle):
    host = (urlparse(url).hostname or "").lower()
    if not public_host(host):
        return None
    final, page = fetch_text(url)
    if not public_host((urlparse(final).hostname or "").lower()):
        return None
    offer = page_offer(page)
    if offer is None:
        return None
    name, price, available = offer
    if not name:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        name = unescape(m.group(1)).strip() if m else final
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    kind = find_match_kind(query, {"title": name, "body": strip_html(text)[:30000]})
    if kind is None or not looks_automotive(name, host, query):
        return None
    return {
        "retailer": host[4:] if host.startswith("www.") else host,
        "title": name[:160],
        "vendor": "Found on the web",
        "brand": detect_brand(name) or "Other",
        "category": "Open-web result",
        "price": float(price),
        "variable_price": False,
        "available": available,
        "availability": "Listed as available on the page" if available else "Listed as out of stock",
        "state": "listed" if available else "out",
        "match_kind": kind,
        "vehicle_hint": vehicle_hint(vehicle, f"{name} {strip_html(text)[:5000]}"),
        "url": final,
        "web_result": True,
    }


def crawl_web(query, vehicle, ollama_key="", engines=True):
    urls = discover_urls(query, ollama_key, engines)
    found = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read_page, url, query, vehicle) for url in urls]
        for future in as_completed(futures):
            try:
                item = future.result()
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                continue
            if item:
                found.append(item)
    return found


# ---------- orchestration ----------

def clean_key(value):
    value = (value or "").strip()
    return value if value and len(value) <= 300 and re.fullmatch(r"[\x21-\x7e]+", value) else ""


def run_search(query, vehicle, stores, web_keys, crawl=True, limit=50):
    jobs = [(name, "store", lambda n=name, u=url: search_store(n, u, query, vehicle)) for name, url in stores]
    if (crawl or web_keys.get("ollama")) and not isinstance(query, NameQuery):
        label = "Open web (Ollama search)" if web_keys.get("ollama") else "Open web (any site)"
        jobs.append((label, "web", lambda: crawl_web(query, vehicle, web_keys.get("ollama", ""), crawl)))
    if web_keys.get("searchapi"):
        jobs.append(("Google Shopping (SearchAPI)", "web", lambda: search_searchapi(query, vehicle, web_keys["searchapi"])))
    if web_keys.get("serpapi"):
        jobs.append(("Google Shopping (SerpApi)", "web", lambda: search_serpapi(query, vehicle, web_keys["serpapi"])))

    results, errors, sources = [], [], []
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {pool.submit(fn): (name, kind) for name, kind, fn in jobs}
        for future in as_completed(futures):
            name, kind = futures[future]
            try:
                found = future.result()
                results.extend(found)
                sources.append({"name": name, "kind": kind, "ok": True, "count": len(found)})
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                message = str(exc)[:180]
                errors.append({"retailer": name, "message": message})
                sources.append({"name": name, "kind": kind, "ok": False, "count": 0, "error": message})

    seen, unique = set(), []
    for item in results:
        key = (item["retailer"].lower(), item["url"].split("?")[0], item["price"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    order = {"exact": 0, "related": 1, "similar": 2}
    unique.sort(key=lambda r: (order.get(r["match_kind"], 1), r["state"] == "out", r["price"]))
    strict = [r for r in unique if r["match_kind"] != "similar"]
    unique = strict if strict else unique[:10]
    store_jobs = [s for s in sources if s["kind"] == "store"]
    return {
        "query": query,
        "checked_at": datetime.now().astimezone().isoformat(timespec="minutes"),
        "stores_checked": sum(1 for s in store_jobs if s["ok"]),
        "store_count": len(store_jobs),
        "results": unique[:limit],
        "errors": errors,
        "sources": sources,
        "web_search_enabled": any(s["kind"] == "web" for s in sources),
        "web_search_error": any(s["kind"] == "web" and not s["ok"] for s in sources),
    }


# ---------- the PartHunter API ----------

class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


API_KEYS = {k.strip() for k in os.environ.get("PARTHUNTER_API_KEYS", "").split(",") if k.strip()}
PUBLIC = os.environ.get("PARTHUNTER_PUBLIC", "").lower() in ("1", "true", "yes")  # run as a public website
TRUST_PROXY = os.environ.get("PARTHUNTER_TRUST_PROXY", "").lower() in ("1", "true", "yes")  # behind a host's proxy
CORS_ORIGIN = os.environ.get("PARTHUNTER_CORS_ORIGIN", "*")
try:
    RATE_LIMIT = max(1, int(os.environ.get("PARTHUNTER_RATE_LIMIT", "30")))
except ValueError:
    RATE_LIMIT = 30
_hits, _hits_lock = {}, threading.Lock()


def rate_ok(client):
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(client, []) if now - t < 60]
        allowed = len(recent) < RATE_LIMIT
        if allowed:
            recent.append(now)
        _hits[client] = recent
        if len(_hits) > 5000:
            _hits.clear()
        return allowed


def authorized(headers):
    if not API_KEYS:
        return True
    auth = headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("X-API-Key", "").strip()
    return any(hmac.compare_digest(token, key) for key in API_KEYS)


def client_ip(handler):
    if TRUST_PROXY:
        forwarded = handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return handler.client_address[0]


def is_loopback(address):
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


def execute_search(params, headers):
    """Shared by the app and the public API. Raises ApiError for bad requests."""
    query = params.get("q", [""])[0].strip()
    vehicle = params.get("vehicle", [""])[0].strip()[:80]
    if not query:
        raise ApiError(400, "missing_query", "Enter a part number or part name to search.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ApiError(400, "query_too_long", f"Searches must be {MAX_QUERY_LENGTH} characters or fewer.")
    name_mode = params.get("mode", [""])[0] == "name"
    if name_mode:
        query = NameQuery(query)
    web_keys = {
        "searchapi": clean_key(headers.get("X-Search-API-Key")) or clean_key(os.environ.get("PARTHUNTER_SEARCHAPI_KEY")),
        "serpapi": clean_key(headers.get("X-Serp-API-Key")) or clean_key(os.environ.get("PARTHUNTER_SERPAPI_KEY")),
        "ollama": clean_key(headers.get("X-Ollama-API-Key")) or clean_key(os.environ.get("OLLAMA_API_KEY")),
    }
    crawl = params.get("crawl", ["1"])[0].lower() not in ("0", "false", "no") and not name_mode
    off = {n for n in params.get("off", [""])[0].split("|") if n}
    include = {n.strip() for n in params.get("stores", [""])[0].split(",") if n.strip()}
    stores = [(n, u) for n, u in ALL_STORES if n not in off and (not include or n in include)]
    skipped = []
    for host in [h.strip().lower() for h in params.get("extra", [""])[0].split(",") if h.strip()][:MAX_EXTRA_STORES]:
        if host in off:
            continue
        if public_host(host):
            stores.append((host, "https://" + host))
        else:
            skipped.append({"retailer": host, "message": "Not a reachable public store address."})
    if not stores and not crawl and not any(web_keys.values()):
        raise ApiError(400, "no_sources", "Turn on at least one store or add a web search key under Search sources.")
    cache_key = (query.lower(), vehicle.lower(), tuple(n for n, _ in stores), bool(web_keys["searchapi"]),
                 bool(web_keys["serpapi"]), bool(web_keys["ollama"]), crawl, name_mode)
    with _cache_lock:
        hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    payload = run_search(query, vehicle, stores, web_keys, crawl, 150 if name_mode else 50)
    payload["errors"].extend(skipped)
    if payload["results"] or not payload["errors"]:
        with _cache_lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[cache_key] = (time.time(), payload)
    return payload


def shape_v1(payload, params, mode):
    def number(name):
        try:
            return float(params.get(name, [""])[0])
        except ValueError:
            return None
    items = [dict(r) for r in payload["results"]]
    prices = sorted(r["price"] for r in items)
    cuts = (prices[(len(prices) - 1) // 3], prices[(len(prices) - 1) * 2 // 3]) if len(prices) >= 3 else None
    for r in items:
        r["tier"] = None if not cuts else "budget" if r["price"] <= cuts[0] else "mid" if r["price"] <= cuts[1] else "premium"
    lo, hi = number("min_price"), number("max_price")
    brands = {b.strip().lower() for b in params.get("brand", [""])[0].split(",") if b.strip()}
    if lo is not None:
        items = [r for r in items if r["price"] >= lo]
    if hi is not None:
        items = [r for r in items if r["price"] <= hi]
    if params.get("in_stock", [""])[0].lower() in ("1", "true", "yes"):
        items = [r for r in items if r["state"] != "out"]
    if brands:
        items = [r for r in items if (r.get("brand") or "").lower() in brands]
    if params.get("match", [""])[0] == "exact":
        items = [r for r in items if r["match_kind"] == "exact"]
    tier = params.get("tier", [""])[0]
    if tier in ("budget", "mid", "premium"):
        items = [r for r in items if r["tier"] == tier]
    sort = params.get("sort", ["price"])[0]
    keys = {"brand": lambda r: ((r.get("brand") or "~").lower(), r["price"]),
            "retailer": lambda r: (r["retailer"].lower(), r["price"])}
    items.sort(key=keys.get(sort, lambda r: (r["state"] == "out", r["price"])))
    try:
        limit = min(150, max(1, int(params.get("limit", ["50"])[0])))
    except ValueError:
        limit = 50
    by_brand = {}
    for r in items:
        entry = by_brand.setdefault(r.get("brand") or "Other", {"brand": r.get("brand") or "Other", "count": 0, "lowest_price": r["price"]})
        entry["count"] += 1
        entry["lowest_price"] = min(entry["lowest_price"], r["price"])
    in_stock = [r for r in items if r["state"] != "out"]
    cheapest = min(in_stock or items, key=lambda r: r["price"], default=None)
    return {
        "query": payload["query"], "mode": mode, "currency": "AUD", "checked_at": payload["checked_at"],
        "count": len(items), "cheapest": cheapest,
        "brands": sorted(by_brand.values(), key=lambda b: b["brand"].lower()),
        "results": items[:limit], "sources": payload["sources"], "errors": payload["errors"],
    }


def openapi_spec(host):
    def q(name, desc, schema=None, required=False):
        return {"name": name, "in": "query", "required": required, "description": desc, "schema": schema or {"type": "string"}}
    listing = {"type": "object", "properties": {
        "retailer": {"type": "string"}, "title": {"type": "string"}, "brand": {"type": "string"}, "vendor": {"type": "string"},
        "category": {"type": "string"}, "price": {"type": "number", "description": "AUD, excluding delivery"},
        "tier": {"type": "string", "nullable": True, "enum": ["budget", "mid", "premium"]},
        "available": {"type": "boolean"}, "availability": {"type": "string"}, "state": {"type": "string", "enum": ["listed", "order", "out"]},
        "match_kind": {"type": "string", "enum": ["exact", "related", "similar"]},
        "vehicle_hint": {"type": "string", "nullable": True, "description": "'mentions' when the listing names the vehicle; not a fitment check"},
        "web_result": {"type": "boolean"}, "url": {"type": "string"}}}
    error = {"type": "object", "properties": {"error": {"type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "PartHunter API", "version": APP_VERSION,
                 "description": "Search Australian car-part listings by part number or part name and compare prices."},
        "servers": [{"url": f"http://{host}/api/v1"}],
        "paths": {
            "/search": {"get": {"summary": "Search for a part", "parameters": [
                q("q", "Part number (e.g. Z436) or, with mode=name, a part name (e.g. oil filter).", required=True),
                q("mode", "part (default) or name.", {"type": "string", "enum": ["part", "name"]}),
                q("vehicle", "Optional car, e.g. '2012 Mazda 3'. Listings that mention it are flagged."),
                q("stores", "Comma-separated store names to include (see /stores). Default: all."),
                q("extra", "Comma-separated extra store addresses to try (max 8)."),
                q("crawl", "Set false to skip open-web page search.", {"type": "boolean"}),
                q("min_price", "Minimum price (AUD).", {"type": "number"}), q("max_price", "Maximum price (AUD).", {"type": "number"}),
                q("tier", "budget, mid or premium (cheapest, middle, top third of prices found).", {"type": "string", "enum": ["budget", "mid", "premium"]}),
                q("brand", "Comma-separated brands."), q("in_stock", "Hide unavailable listings.", {"type": "boolean"}),
                q("match", "exact to return exact matches only.", {"type": "string", "enum": ["exact"]}),
                q("sort", "price (default), brand or retailer.", {"type": "string", "enum": ["price", "brand", "retailer"]}),
                q("limit", "Max results (1-150, default 50).", {"type": "integer"})],
                "responses": {"200": {"description": "Search results", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "query": {"type": "string"}, "mode": {"type": "string"}, "currency": {"type": "string"}, "checked_at": {"type": "string"},
                    "count": {"type": "integer"}, "cheapest": listing,
                    "brands": {"type": "array", "items": {"type": "object", "properties": {"brand": {"type": "string"}, "count": {"type": "integer"}, "lowest_price": {"type": "number"}}}},
                    "results": {"type": "array", "items": listing},
                    "sources": {"type": "array", "items": {"type": "object"}}, "errors": {"type": "array", "items": {"type": "object"}}}}}}},
                    "400": {"description": "Bad request", "content": {"application/json": {"schema": error}}},
                    "401": {"description": "Missing or invalid API key"}, "429": {"description": "Rate limit reached"}}}},
            "/stores": {"get": {"summary": "List the stores searched directly", "responses": {"200": {"description": "Store names"}}}},
            "/health": {"get": {"summary": "Health check (no key needed)", "responses": {"200": {"description": "OK"}}}},
        },
    }
    if API_KEYS:
        spec["components"] = {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}}
        spec["security"] = [{"bearer": []}]
    return spec


DOCS_HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PartHunter API</title><style>body{font:15px/1.6 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#17272f}
code,pre{background:#eef3f2;border-radius:6px;padding:2px 6px}pre{padding:12px;overflow:auto}a{color:#0b625d}</style>
<h1>PartHunter API</h1><p>Search Australian car-part listings by part number or name. Full spec: <a href="/api/v1/openapi.json">openapi.json</a>.</p>
<h2>Endpoints</h2><ul><li><code>GET /api/v1/search?q=Z436</code></li><li><code>GET /api/v1/search?q=oil filter&amp;mode=name&amp;tier=budget&amp;sort=brand</code></li>
<li><code>GET /api/v1/stores</code></li><li><code>GET /api/v1/health</code></li></ul>
<h2>Example</h2><pre>curl -H "Authorization: Bearer YOUR_KEY" \\
  "http://HOST/api/v1/search?q=Z436&amp;max_price=30&amp;in_stock=true"</pre>
<p>If the server has <code>PARTHUNTER_API_KEYS</code> set, send a key as <code>Authorization: Bearer</code> or <code>X-API-Key</code>. Prices are in AUD and exclude delivery; confirm fitment with the seller.</p>"""


class PartHunterHandler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-API-Key, X-Search-API-Key, X-Serp-API-Key, X-Ollama-API-Key")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path.rstrip("/") or "/"
        if path.startswith("/api/v1/"):
            return self.handle_v1(path[len("/api/v1/"):], parse_qs(parts.query))
        if path == "/docs":
            body = DOCS_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        # Routes used by the PartHunter web app itself.
        if path == "/api/health":
            return self.send_json(200, {"app": "PartHunter", "version": APP_VERSION})
        if path == "/api/stores":
            return self.send_json(200, {"stores": [name for name, _ in ALL_STORES]})
        if path == "/api/search":
            ip = client_ip(self)
            if not (PUBLIC or is_loopback(ip)):
                return self.send_json(403, {"error": "The web app routes only work on the same computer. Use /api/v1/search."})
            if not is_loopback(ip) and not rate_ok(ip):
                return self.send_json(429, {"error": f"Too many searches. Limit is {RATE_LIMIT} per minute; try again shortly."})
            try:
                return self.send_json(200, execute_search(parse_qs(parts.query), self.headers))
            except ApiError as exc:
                return self.send_json(exc.status, {"error": exc.message})
        if path in STATIC_PATHS or path.startswith("/icons/"):
            return super().do_GET()
        self.send_error(404)

    def handle_v1(self, route, params):
        try:
            if route == "health":
                return self.send_json(200, {"app": "PartHunter", "version": APP_VERSION, "api": "v1"}, v1=True)
            if route == "openapi.json":
                return self.send_json(200, openapi_spec(self.headers.get("Host", "localhost")), v1=True)
            if not authorized(self.headers):
                raise ApiError(401, "unauthorized", "Send a valid key as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
                               {"WWW-Authenticate": "Bearer"})
            if route == "stores":
                return self.send_json(200, {"stores": [{"name": n, "url": u} for n, u in ALL_STORES]}, v1=True)
            if route == "search":
                if not rate_ok(client_ip(self)):
                    raise ApiError(429, "rate_limited", f"Limit is {RATE_LIMIT} searches per minute.", {"Retry-After": "30"})
                mode = "name" if params.get("mode", [""])[0] == "name" else "part"
                return self.send_json(200, shape_v1(execute_search(params, self.headers), params, mode), v1=True)
            raise ApiError(404, "not_found", "Unknown endpoint. See /api/v1/openapi.json.")
        except ApiError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, v1=True, extra=exc.headers)

    def send_json(self, status, payload, v1=False, extra=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if v1:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="PartHunter app and API server")
    parser.add_argument("--host", default=os.environ.get("PARTHUNTER_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"), help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ["PORT"]) if os.environ.get("PORT", "").isdigit() else None, help="port to listen on (default: first free port from 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the app in a browser")
    args = parser.parse_args()
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not local and not API_KEYS and not PUBLIC:
        sys.exit("Before listening on a non-local address, set PARTHUNTER_API_KEYS (comma-separated keys) or PARTHUNTER_PUBLIC=1 to run as a public website.")

    os.chdir(APP_DIR)
    candidates = []
    if args.port:
        candidates = [args.port]
    else:
        try:
            saved_port = int(PORT_FILE.read_text().strip())
            if DEFAULT_PORT <= saved_port <= DEFAULT_PORT + 20:
                candidates.append(saved_port)
        except (OSError, ValueError):
            pass
        candidates.extend(p for p in range(DEFAULT_PORT, DEFAULT_PORT + 21) if p not in candidates)

    server = selected_port = None
    for port in candidates:
        url = f"http://localhost:{port}/"
        if local and not args.port:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.4) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("app") == "PartHunter" and health.get("version") == APP_VERSION:
                    print(f"PartHunter is already running at {url}")
                    if not args.no_browser:
                        webbrowser.open(url)
                    return
            except Exception:
                pass
        try:
            server = ThreadingHTTPServer((args.host, port), PartHunterHandler)
            selected_port = port
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("No free PartHunter port found. Close an older PartHunter server or pass --port.")

    if local and not args.port:
        PORT_FILE.write_text(str(selected_port))
    shown = "localhost" if local else args.host
    print(f"PartHunter is running at http://{shown}:{selected_port}/")
    print(f"API: http://{shown}:{selected_port}/api/v1/search?q=Z436   Docs: http://{shown}:{selected_port}/docs")
    print("Public website mode: searches are rate limited per visitor." if PUBLIC else "API keys are required." if API_KEYS else "No API keys set: the API is open to this computer only.")
    print("Searches send the entered code to your selected stores and any search services you configured.")
    print("Keep this window open while using it. Press Ctrl+C to stop.")
    if local and not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(f"http://localhost:{selected_port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPartHunter server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
