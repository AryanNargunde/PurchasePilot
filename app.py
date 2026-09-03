"""
PurchasePilot
-------------
Paste a product link, and PurchasePilot scrapes the product page,
runs sentiment analysis on the reviews, and gives you a Buy / Don't Buy
recommendation.

Run with:  streamlit run app.py
"""

import streamlit as st
from scraper import fetch_product_info, fetch_reviews, get_site, diagnose, ScrapeError, SELENIUM_AVAILABLE
from analyzer import analyze_reviews, recommend, build_experience_summary
from recommendations import fetch_similar_products

st.set_page_config(page_title="PurchasePilot", page_icon="assets/logo.png", layout="centered")

# ---------------------------------------------------------------------------
# Global styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@600;700&family=Inter:wght@400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}
h1, h2, h3 { font-family: 'Poppins', sans-serif; }

/* Hero header */
.pp-hero {
    display: flex;
    align-items: center;
    gap: 0.9rem;
    padding: 0.25rem 0 0.75rem 0;
}
.pp-hero img { border-radius: 14px; }
.pp-hero-title {
    font-family: 'Poppins', sans-serif;
    font-size: 2rem;
    font-weight: 700;
    background: linear-gradient(90deg, #EC4899, #3B82F6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0;
    line-height: 1.1;
}
.pp-hero-sub {
    color: #6B7280;
    font-size: 0.95rem;
    margin-top: 2px;
}

/* Verdict badge */
.pp-verdict {
    padding: 1.1rem 1.4rem;
    border-radius: 14px;
    font-family: 'Poppins', sans-serif;
    font-size: 1.5rem;
    font-weight: 700;
    text-align: center;
    margin: 0.5rem 0 0.25rem 0;
}
.pp-verdict-buy { background: #ECFDF5; color: #047857; border: 1px solid #A7F3D0; }
.pp-verdict-mixed { background: #FFFBEB; color: #B45309; border: 1px solid #FDE68A; }
.pp-verdict-avoid { background: #FEF2F2; color: #B91C1C; border: 1px solid #FECACA; }
.pp-verdict-unknown { background: #F3F4F6; color: #374151; border: 1px solid #E5E7EB; }

.pp-score-caption {
    text-align: center;
    color: #6B7280;
    font-size: 0.95rem;
    margin-bottom: 0.75rem;
}

/* Section labels */
.pp-section-label {
    font-family: 'Poppins', sans-serif;
    font-weight: 600;
    font-size: 1.05rem;
    margin: 1.2rem 0 0.4rem 0;
    display: flex;
    align-items: center;
    gap: 0.4rem;
}

/* Buttons */
.stButton > button {
    border-radius: 10px;
    font-weight: 600;
    padding: 0.55rem 1.2rem;
}

/* Footer note */
.pp-footer {
    color: #9CA3AF;
    font-size: 0.82rem;
    text-align: center;
    margin-top: 1.5rem;
}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Hero header
# ---------------------------------------------------------------------------
col_logo, col_text = st.columns([1, 5])
with col_logo:
    st.image("assets/logo.png", width=90)
with col_text:
    st.markdown(
        '<div class="pp-hero-title">PurchasePilot</div>'
        '<div class="pp-hero-sub">AI-powered buy / don\'t-buy verdicts from real customer reviews</div>',
        unsafe_allow_html=True,
    )

st.write("")

if not SELENIUM_AVAILABLE:
    st.caption(
        "ℹ️ Running fast-scrape mode only (Selenium not installed), so JavaScript-rendered "
        "review sections won't be visible. Install `selenium` + a Chrome driver for a fallback, "
        "or use the manual-paste box below."
    )

url = st.text_input("Product URL", placeholder="https://www.amazon.com/dp/XXXXXXXXXX")

with st.expander("🔧 Debug: check why a link isn't returning data"):
    if st.button("Run diagnostics on the URL above"):
        if not url.strip():
            st.warning("Paste a URL above first.")
        else:
            with st.spinner("Diagnosing..."):
                info = diagnose(url)
            st.json(info)
            if info["error"]:
                st.error(f"Request failed: {info['error']}")
            elif info["status_code"] != 200:
                st.error(f"Site returned HTTP {info['status_code']} — likely blocking automated requests.")
            elif info["captcha_detected"]:
                st.error("A CAPTCHA page was served — Amazon/site detected this as a bot.")
            elif not info["jsonld_product_found"] and not info["amazon_producttitle_found"]:
                msg = (
                    "No product data found in the raw HTML — this page likely loads its content with "
                    "JavaScript after the initial load, which the fast scraper can't see."
                )
                if info["selenium_available"]:
                    msg += " The Selenium fallback will be tried automatically when you click Analyze."
                else:
                    msg += " Install Selenium for an automatic fallback, or paste reviews manually."
                st.warning(msg)
            elif info["jsonld_review_count"] == 0 and not info["amazon_producttitle_found"]:
                st.info("Product info was found, but no review text is embedded in the raw HTML.")
            elif info["site"] == "amazon" and info["amazon_producttitle_found"] and \
                    info["amazon_review_body_count"] == 0 and info["amazon_review_fallback_count"] == 0:
                msg = (
                    "The product page loaded fine, but Amazon isn't including any review text in this page's "
                    "raw HTML — no `data-hook='review-body'` elements were found. This usually means Amazon "
                    "is showing reviews via a different layout for this product/region, or requires scrolling "
                    "interaction (JavaScript) to load them."
                )
                if info["selenium_available"]:
                    msg += " Clicking Analyze will automatically retry this with a headless-browser render."
                else:
                    msg += " Try the manual-paste box for this product, or install Selenium for an automatic retry."
                st.warning(msg)
            else:
                st.success("Data was found — if the main analysis still isn't showing it, let me know.")

with st.expander("Reviews not loading? Paste them manually instead"):
    manual_reviews_text = st.text_area(
        "One review per line",
        height=150,
        placeholder="Great product, works as expected...\nBroke after two weeks...\n",
    )

analyze_clicked = st.button("Analyze", type="primary")

if analyze_clicked:
    if not url.strip() and not manual_reviews_text.strip():
        st.warning("Paste a product link, or add some reviews manually below.")
        st.stop()

    product_info = None
    reviews = []

    # 1. Try live scraping if a URL was given
    if url.strip():
        site = get_site(url)
        if site == "croma" and not SELENIUM_AVAILABLE:
            st.info("Heads up: Croma loads its reviews with JavaScript, and Selenium isn't installed here, "
                     "so reviews will likely need the manual-paste box below.")

        with st.spinner(f"Fetching product page ({site})..."):
            try:
                product_info = fetch_product_info(url)
            except ScrapeError as e:
                st.error(f"Couldn't fetch product info: {e}")

        with st.spinner("Fetching reviews..." + (" (may fall back to browser rendering)" if SELENIUM_AVAILABLE else "")):
            try:
                reviews = fetch_reviews(url)
            except ScrapeError as e:
                st.warning(f"Couldn't fetch reviews automatically: {e}")

    # 2. Fall back to / supplement with manually pasted reviews
    if manual_reviews_text.strip():
        manual_lines = [line.strip() for line in manual_reviews_text.splitlines() if line.strip()]
        reviews.extend(manual_lines)

    if not reviews and not product_info:
        st.error("No data to analyze. Try a different link or paste some reviews manually.")
        st.stop()

    # --- Display product info ---
    if product_info:
        st.markdown("---")
        st.markdown(f"### {product_info['title']}")
        cols = st.columns(3)
        cols[0].metric("💰 Price", product_info["price"] or "N/A")
        cols[1].metric("⭐ Star Rating", f"{product_info['rating']}/5" if product_info["rating"] else "N/A")
        cols[2].metric("📝 Total Reviews", product_info["review_count"] or "N/A")

    # --- Sentiment analysis ---
    summary = analyze_reviews(reviews)

    if summary["count"] > 0:
        st.markdown('<div class="pp-section-label">🧭 What buyers experienced</div>', unsafe_allow_html=True)
        exp = build_experience_summary(summary)
        st.write(exp["text"])

        positive_reviews = [r["text"] for r in summary["scored_reviews"] if r["label"] == "positive"]
        negative_reviews = [r["text"] for r in summary["scored_reviews"] if r["label"] == "negative"]

        col_pos, col_neg = st.columns(2)
        with col_pos:
            st.markdown(f"**✅ Positive reviews ({len(positive_reviews)})**")
            if positive_reviews:
                for txt in positive_reviews:
                    st.success(txt)
            else:
                st.write("None detected")
        with col_neg:
            st.markdown(f"**❌ Negative reviews ({len(negative_reviews)})**")
            if negative_reviews:
                for txt in negative_reviews:
                    st.error(txt)
            else:
                st.write("None detected")

        st.markdown('<div class="pp-section-label">📊 Review Sentiment</div>', unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("Positive", f"{summary['positive_pct']}%")
        c2.metric("Neutral", f"{summary['neutral_pct']}%")
        c3.metric("Negative", f"{summary['negative_pct']}%")

        st.bar_chart({
            "Positive": summary["positive_pct"],
            "Neutral": summary["neutral_pct"],
            "Negative": summary["negative_pct"],
        })

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Common positive themes**")
            if summary["top_positive_words"]:
                for w, c in summary["top_positive_words"]:
                    st.write(f"- {w} ({c})")
            else:
                st.write("None detected")
        with col_b:
            st.markdown("**Common negative themes**")
            if summary["top_negative_words"]:
                for w, c in summary["top_negative_words"]:
                    st.write(f"- {w} ({c})")
            else:
                st.write("None detected")

        with st.expander(f"See all {summary['count']} analyzed reviews"):
            for r in summary["scored_reviews"]:
                emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}[r["label"]]
                st.write(f"{emoji} ({r['compound']:+.2f}) {r['text']}")
    else:
        st.info("No review text was available to analyze — verdict below is based on star rating only.")

    # --- Final verdict ---
    product_rating = product_info["rating"] if product_info else None
    result = recommend(product_rating, summary)

    verdict_class = "pp-verdict-unknown"
    if result["final_score"] is not None:
        if result["final_score"] >= 4.0:
            verdict_class = "pp-verdict-buy"
        elif result["final_score"] >= 3.0:
            verdict_class = "pp-verdict-mixed"
        else:
            verdict_class = "pp-verdict-avoid"

    st.markdown('<div class="pp-section-label">🏁 Verdict</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="pp-verdict {verdict_class}">{result["verdict"]}</div>', unsafe_allow_html=True)
    if result["final_score"] is not None:
        st.markdown(f'<div class="pp-score-caption">Combined score: {result["final_score"]} / 5</div>',
                    unsafe_allow_html=True)
    st.write(result["reason"])

    # --- Similar / alternative products ---
    if product_info and url.strip():
        st.markdown('<div class="pp-section-label">🔎 Similar options on the same site</div>', unsafe_allow_html=True)
        with st.spinner("Looking for similar products..."):
            try:
                alternatives = fetch_similar_products(product_info["title"], url)
                debug_error = None
            except Exception as e:
                alternatives = []
                debug_error = str(e)

        with st.expander("🔧 Debug: why no recommendations?"):
            st.write(f"**Site detected:** {get_site(url)}")
            st.write(f"**Product title used for search:** {product_info['title']}")
            if debug_error:
                st.error(f"Exception raised: {debug_error}")
            st.write(f"**Results found:** {len(alternatives)}")

        if alternatives:
            st.caption(
                "These are similar listings from the same search, for price/rating comparison — "
                "not a ranked 'this is objectively better' claim."
            )
            for alt in alternatives:
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        st.markdown(f"**{alt['title']}**")
                        if alt.get("rating"):
                            st.write(f"⭐ {alt['rating']}/5")
                    with c2:
                        if alt.get("price"):
                            st.write(f"💰 {alt['price']}")
                        if alt.get("url"):
                            st.markdown(f"[View]({alt['url']})")
        else:
            st.caption("No comparable listings found for this site/search — this feature works best on "
                       "Amazon, Flipkart, Croma, and Reliance Digital.")

st.markdown(
    '<div class="pp-footer">Amazon actively blocks automated scraping, so live fetching may occasionally '
    'fail or get blocked. Use the manual-paste option as a reliable fallback for demos.</div>',
    unsafe_allow_html=True,
)