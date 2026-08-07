"""
Orchestrates one run: fetch new posts per subreddit -> keyword pre-filter -> Claude
classify+draft for anything flagged -> queue results -> flush to Slack every ~30 minutes.

Run every 15 minutes by .github/workflows/reddit-agent.yml. `--dry-run` (or DRY_RUN=1) prints
what would be sent to Slack and writes no state, so test runs never burn through real posts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import classify_and_draft
import fetch_reddit
import filter_posts
import post_to_slack

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"

LAST_SEEN_PATH = STATE_DIR / "last_seen.json"
LAST_BATCH_SENT_PATH = STATE_DIR / "last_batch_sent.json"
PENDING_BATCH_PATH = STATE_DIR / "pending_batch.json"
DROPPED_LOG_PATH = LOGS_DIR / "dropped_posts.jsonl"

BATCH_INTERVAL_SECONDS = 30 * 60

SUBREDDITS = [
    # Core NYC
    "AskNYC", "FoodNYC", "NYCrestaurants", "nyc",
    # Niche NYC lifestyle/foodie
    "NYCbitcheswithtaste",
    # Tourist/visitor
    "visitingnyc",
    # Core London
    "london", "AskLondon",
    # Expansion
    "unitedkingdom", "AskUK", "NYCbars", "Londonbars", "datingadvice",
]


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def log_dropped(post: dict, classification: dict | None = None, error: str | None = None) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "post": post,
        "classification": classification,
        "error": error,
    }
    with DROPPED_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def process_subreddit(reddit, subreddit_name: str, last_seen: dict, restaurant_data: dict, pending_batch: list) -> None:
    try:
        posts, newest_id = fetch_reddit.fetch_new_posts(reddit, subreddit_name, last_seen.get(subreddit_name))
    except Exception as e:
        print(f"[main] Failed to fetch r/{subreddit_name}: {type(e).__name__}: {e}")
        return

    if newest_id is not None:
        last_seen[subreddit_name] = newest_id

    for post in posts:
        decision = filter_posts.passes_filter(post, restaurant_data)
        filter_posts.log_prefilter_decision(post, decision)
        if not decision["passed"]:
            continue

        classification = classify_and_draft.classify_post(post, decision)
        if classification is None:
            log_dropped(post, error="claude_error")
            continue

        if classification.get("classification") == "not_relevant":
            log_dropped(post, classification=classification)
            continue

        pending_batch.append({"post": post, "classification": classification, "match_context": decision})
        print(f"[main] Queued r/{subreddit_name} post {post['id']} ({classification['classification']}, {classification['persona']})")


def maybe_flush_batch(pending_batch: list, last_batch_sent: dict, dry_run: bool) -> tuple[list, dict]:
    now = datetime.now(timezone.utc)
    last_sent_ts = last_batch_sent.get("timestamp")
    elapsed = (now - datetime.fromisoformat(last_sent_ts)).total_seconds() if last_sent_ts else None
    should_flush = last_sent_ts is None or elapsed >= BATCH_INTERVAL_SECONDS

    if not should_flush:
        print(f"[main] {BATCH_INTERVAL_SECONDS - elapsed:.0f}s until next batch window; {len(pending_batch)} post(s) queued so far.")
        return pending_batch, last_batch_sent

    if not pending_batch:
        print("[main] Batch window open but nothing new since last batch; skipping Slack post.")
        return pending_batch, ({"timestamp": now.isoformat()} if not dry_run else last_batch_sent)

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    if dry_run:
        print(f"[main] DRY RUN: would flush {len(pending_batch)} post(s) to Slack")
        post_to_slack.post_batch_to_slack(pending_batch, webhook_url, dry_run=True)
        return pending_batch, last_batch_sent

    if not webhook_url:
        print("[main] SLACK_WEBHOOK_URL not set; leaving batch queued for next run.")
        return pending_batch, last_batch_sent

    try:
        post_to_slack.post_batch_to_slack(pending_batch, webhook_url, dry_run=False)
    except Exception as e:
        print(f"[main] Slack post failed ({type(e).__name__}: {e}); leaving batch queued for next run.")
        return pending_batch, last_batch_sent

    print(f"[main] Flushed {len(pending_batch)} post(s) to Slack")
    return [], {"timestamp": now.isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN") == "1")
    args = parser.parse_args()

    last_seen = load_json(LAST_SEEN_PATH, {})
    pending_batch = load_json(PENDING_BATCH_PATH, [])
    last_batch_sent = load_json(LAST_BATCH_SENT_PATH, {"timestamp": None})
    restaurant_data = filter_posts.load_restaurant_data()

    reddit = fetch_reddit.get_reddit_client()

    for subreddit_name in SUBREDDITS:
        process_subreddit(reddit, subreddit_name, last_seen, restaurant_data, pending_batch)

    pending_batch, last_batch_sent = maybe_flush_batch(pending_batch, last_batch_sent, args.dry_run)

    if args.dry_run:
        print("[main] Dry run: not writing state.")
    else:
        save_json(LAST_SEEN_PATH, last_seen)
        save_json(PENDING_BATCH_PATH, pending_batch)
        save_json(LAST_BATCH_SENT_PATH, last_batch_sent)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[main] FATAL: {type(e).__name__}: {e}")
        sys.exit(1)
