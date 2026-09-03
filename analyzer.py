"""
analyzer.py
-----------
Runs sentiment analysis on review text and produces a Buy / Don't Buy / Mixed
recommendation, combining sentiment with the product's star rating.
"""

import nltk
from nltk.sentiment.vader import SentimentIntensityAnalyzer

# Download VADER lexicon quietly on first run
try:
    nltk.data.find("sentiment/vader_lexicon.zip")
except LookupError:
    nltk.download("vader_lexicon", quiet=True)

_sia = SentimentIntensityAnalyzer()

POSITIVE_KEYWORDS = [
    "great", "excellent", "love", "amazing", "perfect", "durable", "worth",
    "sturdy", "comfortable", "quality", "recommend", "easy", "fast", "reliable",
]
NEGATIVE_KEYWORDS = [
    "broke", "broken", "waste", "cheap", "flimsy", "defective", "disappointed",
    "poor", "terrible", "returned", "refund", "stopped working", "scam", "bad",
]


def analyze_reviews(reviews: list) -> dict:
    """
    Score each review with VADER sentiment, then aggregate.
    Returns dict with per-review scores, averages, and top keyword hits.
    """
    if not reviews:
        return {
            "count": 0,
            "avg_compound": 0.0,
            "positive_pct": 0.0,
            "negative_pct": 0.0,
            "neutral_pct": 0.0,
            "top_positive_words": [],
            "top_negative_words": [],
            "scored_reviews": [],
        }

    scored = []
    pos_count = neg_count = neu_count = 0
    compound_sum = 0.0
    pos_word_hits = {}
    neg_word_hits = {}

    for text in reviews:
        scores = _sia.polarity_scores(text)
        compound = scores["compound"]
        compound_sum += compound

        if compound >= 0.05:
            label = "positive"
            pos_count += 1
        elif compound <= -0.05:
            label = "negative"
            neg_count += 1
        else:
            label = "neutral"
            neu_count += 1

        lower = text.lower()
        for w in POSITIVE_KEYWORDS:
            if w in lower:
                pos_word_hits[w] = pos_word_hits.get(w, 0) + 1
        for w in NEGATIVE_KEYWORDS:
            if w in lower:
                neg_word_hits[w] = neg_word_hits.get(w, 0) + 1

        scored.append({"text": text, "compound": compound, "label": label})

    n = len(reviews)
    top_pos = sorted(pos_word_hits.items(), key=lambda x: -x[1])[:5]
    top_neg = sorted(neg_word_hits.items(), key=lambda x: -x[1])[:5]

    return {
        "count": n,
        "avg_compound": round(compound_sum / n, 3),
        "positive_pct": round(100 * pos_count / n, 1),
        "negative_pct": round(100 * neg_count / n, 1),
        "neutral_pct": round(100 * neu_count / n, 1),
        "top_positive_words": top_pos,
        "top_negative_words": top_neg,
        "scored_reviews": scored,
    }


def build_experience_summary(summary: dict) -> dict:
    """
    Turns the raw numbers into a written summary of what buyers actually
    experienced, plus a couple of representative example reviews for
    each side. This is what makes the app feel like an analysis rather
    than just a percentage breakdown.
    """
    if summary["count"] == 0:
        return {"text": "No reviews were available to summarize.", "positive_examples": [], "negative_examples": []}

    scored = summary["scored_reviews"]
    positives = sorted([r for r in scored if r["label"] == "positive"], key=lambda r: -r["compound"])
    negatives = sorted([r for r in scored if r["label"] == "negative"], key=lambda r: r["compound"])

    lines = []
    n = summary["count"]

    if summary["positive_pct"] >= 60:
        lines.append(f"Most buyers ({summary['positive_pct']}% of {n} reviews analyzed) reported a good experience.")
    elif summary["negative_pct"] >= 60:
        lines.append(f"Most buyers ({summary['negative_pct']}% of {n} reviews analyzed) reported a poor experience.")
    else:
        lines.append(
            f"Experiences are mixed: {summary['positive_pct']}% positive, "
            f"{summary['negative_pct']}% negative, {summary['neutral_pct']}% neutral, across {n} reviews analyzed."
        )

    if summary["top_positive_words"]:
        themes = ", ".join(w for w, _ in summary["top_positive_words"][:3])
        lines.append(f"Common praise centers on: {themes}.")

    if summary["top_negative_words"]:
        themes = ", ".join(w for w, _ in summary["top_negative_words"][:3])
        lines.append(f"Common complaints center on: {themes}.")

    def _trim(t, limit=180):
        return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "..."

    pos_examples = [_trim(r["text"]) for r in positives[:2]]
    neg_examples = [_trim(r["text"]) for r in negatives[:2]]

    return {"text": " ".join(lines), "positive_examples": pos_examples, "negative_examples": neg_examples}


def recommend(product_rating, sentiment_summary: dict) -> dict:
    """
    Combine star rating + review sentiment into a final recommendation.
    Weighting: 50% star rating, 50% review sentiment (when both available).
    """
    sentiment_score = None
    if sentiment_summary["count"] > 0:
        # map compound (-1..1) to 0..5 scale to match star rating
        sentiment_score = (sentiment_summary["avg_compound"] + 1) / 2 * 5

    if product_rating is not None and sentiment_score is not None:
        final_score = 0.5 * product_rating + 0.5 * sentiment_score
    elif product_rating is not None:
        final_score = product_rating
    elif sentiment_score is not None:
        final_score = sentiment_score
    else:
        return {
            "verdict": "Not enough data",
            "final_score": None,
            "reason": "Couldn't retrieve a star rating or any reviews to analyze.",
        }

    final_score = round(final_score, 2)

    if final_score >= 4.0:
        verdict = "✅ Buy"
        reason = "Strong star rating and mostly positive review sentiment."
    elif final_score >= 3.0:
        verdict = "⚠️ Mixed — Buy with caution"
        reason = "Decent rating but noticeable negative feedback in reviews. Check the negative themes below."
    else:
        verdict = "❌ Don't Buy"
        reason = "Low rating and/or predominantly negative review sentiment."

    return {"verdict": verdict, "final_score": final_score, "reason": reason}