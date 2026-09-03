"""
recommendations.py
-------------------
Suggests similar/alternative products by searching the SAME e-commerce site
for the product's key terms, then extracting whatever product cards it can
find (name, price, rating, link).

Honest scope note (put this in your report):
This does NOT rank alternatives by "which is objectively better" — doing
that properly would require scraping and sentiment-analyzing full reviews
for every candidate too, which is too slow and fragile for a live demo.
Instead it surfaces a handful of similar listings from the same search, so
the user can compare price and star rating at a glance. Think of it as
"here are similar things people also sell" rather than "here is the
provably superior choice."
"""

import re
import json
from urllib.parse import quote_plus, urlparse
from bs4 import BeautifulSoup

from scraper import _get_soup, _render_with_selenium, SELENIUM_AVAILABLE, ScrapeError, get_site

_STOPWORDS = {
    "with", "and", "for", "the", "a", "an", "of", "in", "on", "to", "by",
    "pack", "combo", "pieces", "piece", "set", "new", "genuine",
}

# One search-URL pattern per known site. Sites not listed here are skipped
# for recommendations rather than guessed at — better to show nothing than
# a wrong/broken search link.
SEARCH_URL_BUILDERS = {
    "amazon": lambda domain, q: f"https://{domain}/s?k={q}",
    "flipkart": lambda domain, q: f"https://www.flipkart.com/search?q={q}",
    "croma": lambda domain, q: f"https://www.croma.com/searchB?q={q}%3Arelevance&text={q}",
    "reliancedigital": lambda domain, q: f"https://www.reliancedigital.in/search?q={q}:relevance",
}


def build_search_query(title: str, max_words: int = 6) -> str:
    """Turn a product title into a short, generic search query."""
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    keep = [w for w in words if w.lower() not in _STOPWORDS]
    return quote_plus(" ".join(keep[:max_words]))


def _find_jsonld_itemlist_products(soup: BeautifulSoup) -> list:
    """Some search/listing pages embed an ItemList of Products via JSON-LD
    for SEO — this is far more reliable than guessing CSS classes."""
    results = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            elements = item.get("itemListElement") if item.get("@type") == "ItemList" else None
            if not elements:
                continue
            for el in elements:
                product = el.get("item") if isinstance(el, dict) else None
                if not isinstance(product, dict):
                    continue
                name = product.get("name")
                url = product.get("url") or product.get("@id")
                price = None
                offers = product.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price")
                rating = None
                agg = product.get("aggregateRating")
                if isinstance(agg, dict):
                    try:
                        rating = float(agg.get("ratingValue"))
                    except (TypeError, ValueError):
                        rating = None
                if name:
                    results.append({"title": name, "price": price, "rating": rating, "url": url})
    return results


def _heuristic_search_results(soup: BeautifulSoup, base_domain: str, max_results: int) -> list:
    """Fallback: scan anchor tags that look like product links with a price
    nearby in the same card. Fragile by nature — works on some sites, misses
    others. That's an inherent limitation of guessing generic markup."""
    results = []
    price_pattern = re.compile(r"[₹$]\s?[\d,]+")
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue
        parent = a.find_parent()
        parent_text = parent.get_text(" ", strip=True) if parent else ""
        price_match = price_pattern.search(parent_text)
        if price_match:
            full_url = href if href.startswith("http") else f"https://{base_domain}{href}"
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            results.append({"title": text[:120], "price": price_match.group(0), "rating": None, "url": full_url})
        if len(results) >= max_results * 3:
            break

    return results[:max_results]


def fetch_similar_products(product_title: str, source_url: str, max_results: int = 5) -> list:
    """
    Search the same site for similar products to the one analyzed.
    Returns a list of dicts: {title, price, rating, url}, excluding the
    original product itself where detectable. Best-effort — returns an
    empty list for sites with no known search pattern or no usable markup,
    rather than guessing and returning junk.
    """
    site = get_site(source_url)
    domain = urlparse(source_url).netloc or ""
    query = build_search_query(product_title)

    builder = SEARCH_URL_BUILDERS.get(site)
    if not builder or not query:
        return []

    search_url = builder(domain, query)

    soup = None
    try:
        soup = _get_soup(search_url, use_cache=False)
    except ScrapeError:
        pass

    results = []
    if soup:
        results = _find_jsonld_itemlist_products(soup)
        if not results:
            results = _heuristic_search_results(soup, domain, max_results)

    if not results and SELENIUM_AVAILABLE:
        try:
            html = _render_with_selenium(search_url)
            soup = BeautifulSoup(html, "lxml")
            results = _find_jsonld_itemlist_products(soup)
            if not results:
                results = _heuristic_search_results(soup, domain, max_results)
        except ScrapeError:
            pass

    # Exclude the exact same product and de-dupe by normalized title
    filtered = []
    seen_titles = set()
    norm_original = (product_title or "").strip().lower()
    for r in results:
        norm_title = (r["title"] or "").strip().lower()
        if not norm_title or norm_title == norm_original or norm_title in seen_titles:
            continue
        seen_titles.add(norm_title)
        filtered.append(r)
        if len(filtered) >= max_results:
            break

    return filtered