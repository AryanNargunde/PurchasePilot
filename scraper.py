"""
scraper.py
-----------
Multi-site best-effort scraper for e-commerce product pages
(Amazon, Flipkart, Croma, and others via generic structured-data extraction).

Two-tier strategy:
  1. FAST PATH: requests + BeautifulSoup (no browser). Works when the site
     embeds data in raw HTML (JSON-LD, Amazon's server-rendered review blocks).
  2. SLOW PATH (optional): Selenium, used ONLY as a fallback when the fast
     path finds nothing. Selenium runs a real headless browser, so it can see
     content that JavaScript adds after the page loads (e.g. Croma's review
     widget, or Amazon layouts that lazy-load reviews on scroll).

Selenium is optional. If it (or a Chrome/Chromium driver) isn't installed,
the app still works — it just can't use the slow path, and will tell the
user to paste reviews manually for JS-heavy pages.

IMPORTANT (put this in your project report too):
- Amazon actively blocks automated scraping (CAPTCHAs, rate limiting, layout
  changes). This code does NOT attempt to solve CAPTCHAs or evade blocking —
  when Amazon serves a CAPTCHA, we surface that clearly and stop, rather than
  trying to work around it. That's a deliberate, ethical line, not a missing
  feature.
- Many sites embed "JSON-LD" structured data in their HTML for SEO purposes —
  this includes product name, price, rating, and sometimes review text. We
  use this as the primary, most robust extraction method since it doesn't
  depend on guessing ever-changing CSS class names.
- Some sites render their review sections with JavaScript AFTER the page
  loads. The fast path can't see that; the optional Selenium slow path can,
  because it actually renders the page in a browser.
"""

import re
import json
import time
import random
import requests
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

# --- Optional Selenium slow path -------------------------------------------
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import WebDriverException, TimeoutException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Simple in-process cache so hitting "Analyze" twice on the same URL in one
# session doesn't re-fetch from scratch (faster, and gentler on the site).
_page_cache = {}
_CACHE_TTL_SECONDS = 600


class ScrapeError(Exception):
    """Raised when the page can't be fetched or parsed (e.g. blocked, or JS-rendered content)."""
    pass


def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }


def get_site(url: str) -> str:
    """Identify which known site this URL belongs to, or 'generic' otherwise."""
    domain = urlparse(url).netloc.lower()
    if "amazon." in domain:
        return "amazon"
    if "flipkart." in domain:
        return "flipkart"
    if "croma." in domain:
        return "croma"
    if "reliancedigital." in domain:
        return "reliancedigital"
    return "generic"


def extract_asin(url: str) -> str:
    """
    Pull the ASIN (product ID) out of an Amazon URL.
    Handles the common link shapes across Amazon locales:
      /dp/ASIN, /gp/product/ASIN, /product/ASIN, /gp/aw/d/ASIN,
      and ?asin=ASIN or ?ASIN=ASIN query params.
    """
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/gp/aw/d/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"/product-reviews/([A-Z0-9]{10})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)

    query = parse_qs(urlparse(url).query)
    for key in ("asin", "ASIN"):
        if key in query and query[key]:
            candidate = query[key][0]
            if re.fullmatch(r"[A-Z0-9]{10}", candidate):
                return candidate

    raise ScrapeError("Couldn't find a product ID (ASIN) in that URL. Make sure it's a normal Amazon product link.")


def _looks_like_captcha(html_text: str) -> bool:
    """Check a handful of language-agnostic and multi-locale CAPTCHA signals."""
    lowered = html_text.lower()
    signals = [
        "enter the characters you see",       # en
        "type the characters you see",        # en variant
        "captcha",                            # generic, present in class/id names across locales
        "robot check",
        "ingrese los caracteres",             # es
        "geben sie die zeichen ein",          # de
        "saisissez les caractères",           # fr
    ]
    return any(s in lowered for s in signals)


def _fetch_with_retry(url: str, retries: int = 3, backoff_base: float = 1.5) -> requests.Response:
    """GET a URL with a couple of retries and exponential backoff for
    transient failures (timeouts, connection resets, 429/503)."""
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_headers(), timeout=10)
            if resp.status_code in (429, 503) and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0, 0.5))
                continue
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0, 0.5))
    raise ScrapeError(f"Network error while fetching the page after {retries} attempts: {last_exc}")


def _get_soup(url: str, use_cache: bool = True) -> BeautifulSoup:
    now = time.time()
    if use_cache and url in _page_cache:
        cached_html, cached_at = _page_cache[url]
        if now - cached_at < _CACHE_TTL_SECONDS:
            return BeautifulSoup(cached_html, "lxml")

    resp = _fetch_with_retry(url)

    if resp.status_code != 200:
        raise ScrapeError(f"Site returned status {resp.status_code}. It may be blocking this request.")

    if _looks_like_captcha(resp.text):
        raise ScrapeError("The site served a CAPTCHA page (it detected automated access). Try again later, "
                           "use a different network, or paste reviews manually instead.")

    if use_cache:
        _page_cache[url] = (resp.text, now)

    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# Optional Selenium slow path — only used when the fast path comes up empty.
# ---------------------------------------------------------------------------

def _make_driver():
    """Build a headless Chrome driver. Raises ScrapeError if unavailable."""
    if not SELENIUM_AVAILABLE:
        raise ScrapeError(
            "Selenium isn't installed, so JavaScript-rendered pages can't be scraped. "
            "Install it with `pip install selenium webdriver-manager` and re-run, "
            "or use the manual review-paste box instead."
        )
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1800")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as e:
        raise ScrapeError(
            f"Couldn't start a headless Chrome browser for JS rendering: {e}. "
            "Make sure Chrome/Chromium + a matching driver are installed, or use "
            "`webdriver-manager` to auto-manage the driver, or paste reviews manually."
        )
    driver.set_page_load_timeout(20)
    return driver


def _render_with_selenium(url: str, wait_css_selector: str = None, scroll_passes: int = 4) -> str:
    """
    Load a URL in a real (headless) browser, optionally scroll a few times to
    trigger lazy-loaded content, and return the fully rendered HTML.
    """
    driver = _make_driver()
    try:
        driver.get(url)

        if wait_css_selector:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_css_selector))
                )
            except TimeoutException:
                pass  # fall through and use whatever rendered — better than nothing

        # Scroll incrementally: many sites lazy-load reviews as they enter the viewport.
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(scroll_passes):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        return driver.page_source
    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Generic extraction via JSON-LD structured data (schema.org "Product")
# ---------------------------------------------------------------------------

def _find_jsonld_product(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") in ("Product", ["Product"]):
                return item
    return {}


def _generic_product_info(soup: BeautifulSoup) -> dict:
    product = _find_jsonld_product(soup)

    title = product.get("name")
    price = None
    offers = product.get("offers")
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("lowPrice")
    elif isinstance(offers, list) and offers:
        price = offers[0].get("price")

    rating = None
    review_count = None
    agg = product.get("aggregateRating")
    if isinstance(agg, dict):
        try:
            rating = float(agg.get("ratingValue")) if agg.get("ratingValue") else None
        except (TypeError, ValueError):
            rating = None
        try:
            review_count = int(agg.get("reviewCount") or agg.get("ratingCount") or 0) or None
        except (TypeError, ValueError):
            review_count = None

    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

    return {
        "title": title or "Unknown product",
        "price": str(price) if price else None,
        "rating": rating,
        "review_count": review_count,
    }


def _generic_reviews_from_jsonld(soup: BeautifulSoup) -> list:
    product = _find_jsonld_product(soup)
    reviews = []
    review_field = product.get("review")
    if isinstance(review_field, dict):
        review_field = [review_field]
    if isinstance(review_field, list):
        for r in review_field:
            body = r.get("reviewBody") or r.get("description")
            if body:
                reviews.append(body.strip())
    return reviews


def _generic_reviews_heuristic(soup: BeautifulSoup, max_reviews: int) -> list:
    """
    Last-resort fallback: scan for elements whose class/id/data-attributes
    hint at review content. Works on some sites, misses others — that's an
    inherent limitation of static HTML, not a bug.
    """
    reviews = []
    pattern = re.compile(r"review|comment|feedback", re.I)
    for tag in soup.find_all(True):
        attrs_text = " ".join([
            " ".join(tag.get("class", [])) if tag.get("class") else "",
            tag.get("id", "") or "",
            tag.get("data-testid", "") or "",
        ])
        if pattern.search(attrs_text):
            text = tag.get_text(" ", strip=True)
            if text and 25 <= len(text) <= 1000:
                reviews.append(text)
        if len(reviews) >= max_reviews * 3:
            break

    seen = set()
    unique = []
    for r in reviews:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique[:max_reviews]


# ---------------------------------------------------------------------------
# Amazon-specific extraction (most reliable of the three, still best-effort)
# ---------------------------------------------------------------------------

def _amazon_product_info(soup: BeautifulSoup) -> dict:
    title_tag = soup.select_one("#productTitle")
    title = title_tag.get_text(strip=True) if title_tag else None

    price = None
    for sel in [".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice", ".a-price-whole"]:
        tag = soup.select_one(sel)
        if tag:
            price = tag.get_text(strip=True)
            break

    rating = None
    rating_tag = soup.select_one("span.a-icon-alt")
    if rating_tag:
        m = re.search(r"([\d.]+)\s+out of", rating_tag.get_text())
        if m:
            rating = float(m.group(1))

    review_count = None
    count_tag = soup.select_one("#acrCustomerReviewText")
    if count_tag:
        m = re.search(r"([\d,]+)", count_tag.get_text())
        if m:
            review_count = int(m.group(1).replace(",", ""))

    if not title:
        return _generic_product_info(soup)

    return {"title": title, "price": price, "rating": rating, "review_count": review_count}


# Amazon injects this boilerplate into review text for its mobile
# expand/collapse toggle. It's not part of the actual review, so strip it
# before it pollutes sentiment analysis.
_AMAZON_REVIEW_BOILERPLATE = [
    "Brief content visible, double tap to read full content.",
    "Full content visible, double tap to read brief content.",
    "Read more",
    "Read less",
]


def _clean_amazon_review_text(text: str) -> str:
    cleaned = text
    for phrase in _AMAZON_REVIEW_BOILERPLATE:
        cleaned = cleaned.replace(phrase, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_amazon_reviews_from_soup(soup: BeautifulSoup) -> list:
    texts = []
    # Current Amazon markup (as of this scraper's last check) uses
    # data-hook="reviewText" for the actual review body.
    for block in soup.select("[data-hook='reviewText']"):
        text = _clean_amazon_review_text(block.get_text(" ", strip=True))
        if text:
            texts.append(text)
    # Older/alternate markup used data-hook="review-body" directly.
    if not texts:
        for block in soup.select("[data-hook='review-body']"):
            text = _clean_amazon_review_text(block.get_text(" ", strip=True))
            if text:
                texts.append(text)
    # Oldest fallback selector, kept for older cached/mirrored pages.
    if not texts:
        for block in soup.select(".review-text-content span"):
            text = _clean_amazon_review_text(block.get_text(" ", strip=True))
            if text:
                texts.append(text)
    return texts


def _fetch_amazon_reviews(url: str, max_reviews: int, max_pages: int, allow_js_fallback: bool = True) -> list:
    reviews = []
    seen = set()

    def _add(texts):
        for t in texts:
            if t not in seen:
                seen.add(t)
                reviews.append(t)

    # Fast path: reviews sometimes embedded right on the product page.
    try:
        soup = _get_soup(url)
        _add(_extract_amazon_reviews_from_soup(soup))
    except ScrapeError:
        pass

    try:
        asin = extract_asin(url)
    except ScrapeError:
        asin = None

    # Use the SAME domain as the URL the user gave us (amazon.com, amazon.in,
    # amazon.co.uk, etc.) instead of assuming .com — otherwise pagination
    # silently hits the wrong country site and returns nothing/unrelated data.
    domain = urlparse(url).netloc or "www.amazon.com"

    if asin:
        base = f"https://{domain}/product-reviews/{asin}/"
        for page in range(1, max_pages + 1):
            if len(reviews) >= max_reviews:
                break
            page_url = f"{base}?pageNumber={page}"
            try:
                soup = _get_soup(page_url)
            except ScrapeError:
                break
            new_texts = _extract_amazon_reviews_from_soup(soup)
            if not new_texts:
                break
            _add(new_texts)
            time.sleep(random.uniform(1.0, 2.0))

    # Slow path: fast path found nothing (reviews likely lazy-loaded via JS).
    if not reviews and allow_js_fallback and SELENIUM_AVAILABLE and asin:
        try:
            review_url = f"https://{domain}/product-reviews/{asin}/"
            html = _render_with_selenium(review_url, wait_css_selector="[data-hook='review-body']")
            soup = BeautifulSoup(html, "lxml")
            _add(_extract_amazon_reviews_from_soup(soup))
        except ScrapeError:
            pass  # genuinely nothing more we can do without manual paste

    return reviews[:max_reviews]


def diagnose(url: str) -> dict:
    """
    Debug helper: fetches the URL and reports what actually happened, without
    raising. Use this to see WHY a site isn't returning data.
    """
    site = get_site(url)
    result = {
        "site": site,
        "status_code": None,
        "html_length": 0,
        "captcha_detected": False,
        "page_title_tag": None,
        "jsonld_scripts_found": 0,
        "jsonld_product_found": False,
        "jsonld_review_count": 0,
        "amazon_producttitle_found": False,
        "amazon_review_body_count": 0,
        "amazon_reviewtext_count": 0,
        "amazon_review_fallback_count": 0,
        "selenium_available": SELENIUM_AVAILABLE,
        "error": None,
    }
    try:
        resp = _fetch_with_retry(url, retries=1)
        result["status_code"] = resp.status_code
        result["html_length"] = len(resp.text)
        result["captcha_detected"] = _looks_like_captcha(resp.text)

        soup = BeautifulSoup(resp.text, "lxml")
        title_tag = soup.find("title")
        result["page_title_tag"] = title_tag.get_text(strip=True) if title_tag else None

        ld_scripts = soup.find_all("script", {"type": "application/ld+json"})
        result["jsonld_scripts_found"] = len(ld_scripts)

        product = _find_jsonld_product(soup)
        result["jsonld_product_found"] = bool(product)
        review_field = product.get("review") if product else None
        if isinstance(review_field, dict):
            result["jsonld_review_count"] = 1
        elif isinstance(review_field, list):
            result["jsonld_review_count"] = len(review_field)

        result["amazon_producttitle_found"] = soup.select_one("#productTitle") is not None
        result["amazon_review_body_count"] = len(soup.select("[data-hook='review-body']"))
        result["amazon_reviewtext_count"] = len(soup.select("[data-hook='reviewText']"))
        result["amazon_review_fallback_count"] = len(soup.select(".review-text-content span"))

    except ScrapeError as e:
        result["error"] = str(e)
    except requests.RequestException as e:
        result["error"] = str(e)

    return result


def fetch_product_info(url: str) -> dict:
    """
    Fetch title, price, rating, review_count from any supported product URL.
    Falls back to Selenium rendering if the fast path finds nothing usable
    and Selenium is available.
    """
    site = get_site(url)

    try:
        soup = _get_soup(url)
        if site == "amazon":
            info = _amazon_product_info(soup)
        else:
            info = _generic_product_info(soup)
    except ScrapeError:
        info = {"title": None, "price": None, "rating": None, "review_count": None}

    needs_js_fallback = info["title"] in (None, "Unknown product") and info["rating"] is None
    if needs_js_fallback and SELENIUM_AVAILABLE:
        try:
            html = _render_with_selenium(url)
            soup = BeautifulSoup(html, "lxml")
            info = _amazon_product_info(soup) if site == "amazon" else _generic_product_info(soup)
        except ScrapeError:
            pass

    if info["title"] in (None, "Unknown product") and info["rating"] is None:
        hint = (
            "Selenium was tried and still found nothing — this page may require login, "
            "region-specific access, or block headless browsers too."
            if SELENIUM_AVAILABLE else
            "Install Selenium (`pip install selenium webdriver-manager`) to enable a "
            "JavaScript-rendering fallback, or"
        )
        raise ScrapeError(
            f"Couldn't extract product info from this {site} page. It may render its content with "
            f"JavaScript, or block automated requests. {hint} use the manual review-paste option instead."
        )
    return info


def fetch_reviews(url: str, max_reviews: int = 40, max_pages: int = 4) -> list:
    """
    Fetch review text from any supported product URL.
    - Amazon: paginates through dedicated review pages, then tries a
      Selenium render as a last resort if nothing was found.
    - Others: tries JSON-LD embedded reviews, then a generic HTML heuristic,
      then a Selenium render as a last resort.
    """
    site = get_site(url)

    if site == "amazon":
        return _fetch_amazon_reviews(url, max_reviews, max_pages)

    try:
        soup = _get_soup(url)
        reviews = _generic_reviews_from_jsonld(soup)
        if len(reviews) < max_reviews:
            heuristic = _generic_reviews_heuristic(soup, max_reviews - len(reviews))
            for r in heuristic:
                if r not in reviews:
                    reviews.append(r)
    except ScrapeError:
        reviews = []

    if not reviews and SELENIUM_AVAILABLE:
        try:
            html = _render_with_selenium(url)
            soup = BeautifulSoup(html, "lxml")
            reviews = _generic_reviews_from_jsonld(soup)
            if len(reviews) < max_reviews:
                heuristic = _generic_reviews_heuristic(soup, max_reviews - len(reviews))
                for r in heuristic:
                    if r not in reviews:
                        reviews.append(r)
        except ScrapeError:
            pass

    return reviews[:max_reviews]