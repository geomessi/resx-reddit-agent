"""
Cheap keyword pre-filter, run before any Claude call. Only posts that pass get classified —
this is what keeps Claude API usage to "one call per flagged post, never per raw post."

Every decision (matched or dropped) is logged to logs/prefilter_log.jsonl for later
filter-tuning — dropped posts are never silently lost, just not sent to Claude.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

RESERVATION_INTENT_PHRASES = [
    "reservation", "table at", "table for", "can't make it", "cancel my reservation",
    "cancellation fee", "how do i get into", "how do i get a table", "hard to get",
    "book a table", "walk in", "walk-in only", "spontaneous", "last minute dinner",
    "date night",
]

VISITOR_PHRASES = [
    "visiting", "in town", "trip to", "only here", "while we're there",
]

# Loose heuristic for "specific date ranges" (spec section 4) — e.g. "Aug 12-15", "8/12-8/15",
# "Thu-Sun", "next weekend". Not exhaustive; meant to catch the common visitor phrasing.
DATE_RANGE_PATTERNS = [
    re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*[-–]\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I),
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\s*[-–]\s*\d{1,2}\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}\s*[-–]\s*\d{1,2}/\d{1,2}\b"),
    re.compile(r"\bnext weekend\b", re.I),
    re.compile(r"\bthis weekend\b", re.I),
]

SUPPLY_PHRASES = [
    "give away my reservation", "don't need my reservation", "can't use my reservation",
    "no longer need", "up for grabs",
]

DATE_NIGHT_PHRASES = ["date night", "anniversary dinner", "romantic dinner"]

# r/unitedkingdom is a broad, non-food subreddit — spec requires gating to food-tagged
# threads only, on top of the standard keyword match, to avoid flooding on off-topic posts.
FOOD_FLAIR_KEYWORDS = ["food", "restaurant", "dining", "drink", "eating out"]

ALL_PHRASES = RESERVATION_INTENT_PHRASES + VISITOR_PHRASES + SUPPLY_PHRASES

RESTAURANT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "top_restaurants.json"
PREFILTER_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "prefilter_log.jsonl"


def load_restaurant_data(path: Path = RESTAURANT_DATA_PATH) -> dict:
    return json.loads(path.read_text())


def _text_of(post: dict) -> str:
    return f"{post['title']} {post['selftext']}".lower()


def _find_matches(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term.lower() in text]


def evaluate_post(post: dict, restaurant_data: dict) -> dict:
    """
    Base keyword evaluation, ignoring any per-subreddit overrides. Returns match details
    so callers (Slack formatting, subreddit overrides) can see *what* matched, not just
    whether something did.
    """
    text = _text_of(post)

    phrase_hits = _find_matches(text, ALL_PHRASES)
    date_range_hit = any(p.search(text) for p in DATE_RANGE_PATTERNS)

    restaurant_names = restaurant_data["nyc"] + restaurant_data["london"] + restaurant_data["high_recognition"]
    restaurant_hits = _find_matches(text, restaurant_names)
    competitor_hits = _find_matches(text, restaurant_data["competitors"])

    matched = bool(phrase_hits or date_range_hit or restaurant_hits or competitor_hits)

    return {
        "matched": matched,
        "phrase_hits": phrase_hits,
        "date_range_hit": date_range_hit,
        "restaurant_hits": restaurant_hits,
        "competitor_hits": competitor_hits,
    }


def _passes_subreddit_override(post: dict, base: dict, text: str) -> tuple[bool, str]:
    """Returns (passes, reason) for subreddits with stricter-than-default gating."""
    sub = post["subreddit"].lower()

    if sub == "unitedkingdom":
        flair = post.get("link_flair_text", "").lower()
        if not any(kw in flair for kw in FOOD_FLAIR_KEYWORDS):
            return False, "unitedkingdom: no food-related flair"
        if not base["matched"]:
            return False, "unitedkingdom: food-flaired but no keyword/restaurant match"
        return True, "unitedkingdom: food flair + keyword match"

    if sub == "datingadvice":
        has_restaurant = bool(base["restaurant_hits"])
        has_date_night = any(p in text for p in DATE_NIGHT_PHRASES)
        if has_restaurant and has_date_night:
            return True, "datingadvice: restaurant + date-night combo"
        return False, "datingadvice: missing restaurant+date-night combo"

    return base["matched"], "default: keyword/restaurant/competitor match" if base["matched"] else "default: no match"


def passes_filter(post: dict, restaurant_data: dict) -> dict:
    """
    Full filter decision for one post, applying subreddit-specific overrides on top of the
    base keyword evaluation. Returns a dict with `passed`, `reason`, and the underlying match
    details (used downstream for Slack "tracked restaurant"/"competitor" flags).
    """
    base = evaluate_post(post, restaurant_data)
    text = _text_of(post)
    passed, reason = _passes_subreddit_override(post, base, text)
    return {"passed": passed, "reason": reason, **base}


def log_prefilter_decision(post: dict, decision: dict) -> None:
    PREFILTER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "subreddit": post["subreddit"],
        "post_id": post["id"],
        "title": post["title"],
        "permalink": post["permalink"],
        "passed": decision["passed"],
        "reason": decision["reason"],
        "phrase_hits": decision["phrase_hits"],
        "restaurant_hits": decision["restaurant_hits"],
        "competitor_hits": decision["competitor_hits"],
    }
    with PREFILTER_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
