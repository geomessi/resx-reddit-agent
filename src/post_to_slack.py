"""
Formats a batch of classified posts as Slack Block Kit and posts it to #social. This is a
live signal feed for the social/community team ("engage with this now"), not an approval
queue — so it needs to be skimmable in seconds, not a wall of text.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

MAX_BLOCKS_PER_MESSAGE = 45  # Slack's hard limit is 50; leave headroom for the header/divider

PERSONA_LABELS = {
    "supplier": "Supplier",
    "demand_chaser": "Demand Chaser",
    "visitor": "Visitor",
    "spontaneous_planner": "Spontaneous Planner",
}
MARKET_LABELS = {"nyc": "NYC", "london": "London", "other": "Other"}


def _format_age(created_utc: float) -> str:
    hours = (datetime.now(timezone.utc).timestamp() - created_utc) / 3600
    if hours < 1:
        return f"{int(hours * 60)}m old"
    if hours < 24:
        return f"{hours:.0f}h old"
    return f"{hours / 24:.0f}d old"


def _entry_blocks(item: dict) -> list[dict]:
    post = item["post"]
    c = item["classification"]
    match = item.get("match_context", {})

    urgency_emoji = "\U0001f534" if c["urgency"] == "urgent" else "⚪️"
    persona_label = PERSONA_LABELS.get(c["persona"], c["persona"])
    market_label = MARKET_LABELS.get(c["market"], c["market"])

    flags = []
    if c.get("mentions_competitor"):
        flags.append(f"⚠️ Competitor mentioned: *{c.get('competitor_named') or '?'}*")
    tracked_restaurant = c.get("restaurant_named") or (match.get("restaurant_hits") or [None])[0]
    if tracked_restaurant:
        flags.append(f"\U0001f4cd Restaurant named: *{tracked_restaurant}*")

    blocks = [
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"{urgency_emoji} *r/{post['subreddit']}* · {persona_label} · {market_label}",
            }],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*<{post['permalink']}|{post['title']}>*"},
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"{_format_age(post['created_utc'])} · {post['score']} upvotes"
                + ("\n" + " · ".join(flags) if flags else ""),
            }],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*With ResX:* {c['draft_reply_with_resx']}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Neutral:* {c['draft_reply_neutral']}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_Why: {c['reasoning']}_"}],
        },
        {"type": "divider"},
    ]
    return blocks


def build_message_chunks(batch: list[dict]) -> list[list[dict]]:
    """Groups batch entries into Slack messages, never splitting one entry across messages."""
    header = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"ResX Reddit Signal — {len(batch)} new post(s)"},
    }]

    entry_groups = [_entry_blocks(item) for item in batch]

    chunks = []
    current = list(header)
    for group in entry_groups:
        if len(current) + len(group) > MAX_BLOCKS_PER_MESSAGE and current != header:
            chunks.append(current)
            current = []
        current.extend(group)
    if current:
        chunks.append(current)
    return chunks


def post_batch_to_slack(batch: list[dict], webhook_url: str, dry_run: bool = False) -> None:
    if not batch:
        return

    for chunk in build_message_chunks(batch):
        if dry_run:
            print(json.dumps({"blocks": chunk}, indent=2))
            continue

        payload = json.dumps({"blocks": chunk}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"[post_to_slack] Slack error {e.code}: {body}")
            raise
