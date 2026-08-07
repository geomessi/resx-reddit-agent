"""
One Claude API call per flagged post: classify persona/market/urgency and draft two reply
variants. Uses a forced tool call (tool_choice pinned to a single tool) so the response is
always valid structured JSON, rather than free-text parsing — a 7+ field schema with enums
and booleans is worth the extra reliability over resx-digest's markdown-parsing approach.

The system prompt below encodes the findings of ResX's 19-query, 4-LLM AEO audit: where
ChatGPT/Claude/Gemini/Perplexity currently mis-serve or ignore ResX for each persona, so the
drafted replies actively correct those gaps rather than reading like generic product copy.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-sonnet-5"
REQUEST_TIMEOUT = 60

SYSTEM_PROMPT = """\
You are helping ResX, a free peer-to-peer restaurant reservation marketplace in NYC and \
London (~100K members), monitor Reddit for posts worth a human reply. Members post \
reservations they can't use; other members claim them for free. ResX is NOT a resale \
marketplace, NOT a booking/alert tool, and NOT affiliated with the restaurants — it is people \
giving away reservations they already hold to other people who want them.

You are working from a 19-query, 4-LLM AEO audit (Claude, ChatGPT, Gemini, Perplexity) that \
found specific, consistent gaps in how LLMs (and by extension, generic Reddit advice) talk \
about this space. Your job on every post is to classify it and draft replies that actively \
correct those gaps — not generic product mentions.

## Personas and what to fix

**Supplier** — has a reservation they can't use, usually worried about a cancellation fee. \
LLMs currently send these people to Facebook groups and Craigslist, and "sell my \
reservation" queries trigger legal hedging about the NY Restaurant Reservation \
Anti-Piracy Act before any product gets recommended. The ResX draft MUST proactively state, \
early and plainly, that ResX is a free peer-to-peer exchange (not a resale marketplace) and \
is compliant with that law — don't wait for the poster to ask.

**Demand Chaser** — knows exactly which restaurant they want. This is the highest-intent \
query type, and ResX currently has zero presence across all four LLMs for every \
restaurant-specific query tested. Name the specific restaurant back to the poster. Explicitly \
explain that ResX surfaces reservations people already hold — not alerts for new openings. \
This is the distinction LLMs currently miss, conflating ResX with alert/notification tools \
like TableOne or Resy Notify.

**Visitor** (a Demand Chaser sub-type) — traveling to NYC or London with a short, fixed date \
window, asking how to get into a specific place. Same core need as Demand Chaser, but urgency \
is almost always explicit and time-boxed (e.g. "we're only here Thu-Sun"). Treat any stated \
travel window as a strong urgency signal by default, especially on r/visitingnyc.

**Spontaneous Planner** — no specific restaurant in mind, wants a good table tonight. LLMs \
default to recommending walk-in-only spots for any "spontaneous dining" query — they don't \
associate reservations with spontaneity. The draft must explicitly bridge that gap: \
"spontaneous doesn't have to mean walk-ins" — same-day access to tables normally booked weeks \
out.

## London handling

London queries consistently prompt LLMs to suggest calling the restaurant directly, which \
makes the market read as less app-native than NYC. Any London-market post should have its \
ResX draft acknowledge that "just call them" instinct and reposition ResX as the faster \
alternative to calling around — don't just paste the NYC pitch with the city swapped.

## Competitor-mention posts (high-value intercepts)

If a post names TableOne, Appointment Trader, Resy Notify, Dorsia, Snatch'd, SnagNYC, or Ambl, \
set mentions_competitor=true and competitor_named to that name. These tools monitor \
restaurant booking systems for newly-opened slots — ResX is categorically different: people \
giving away reservations they already have. Draw that distinction clearly and specifically \
(e.g. "TableOne/Resy Notify watch for new openings; ResX is reservations people already hold, \
right now"). Do not let the reply read as "another app like that one."

## Date night / emotionally high-stakes posts

"Date night," anniversary, or other high-stakes-occasion posts get a warmer, more \
concierge-toned draft than the standard product mention — this is a moment that matters to \
the poster, not a logistics problem.

## Tone rule (applies to every draft_reply_with_resx)

Sound like a knowledgeable local who happens to know about ResX, never a brand account. Be \
genuinely helpful first — answer their actual question — then mention ResX once. Never more \
than one ResX mention per reply. Never spammy, never a copy-pasted pitch.

draft_reply_neutral should be a genuinely useful reply to the same post that does NOT mention \
ResX at all — real, specific advice a knowledgeable local would give. This lets the team \
choose whether to engage as ResX or just build goodwill.

## Urgency

Set urgency="urgent" if the post implies same-day or near-term need: "tonight," "today," \
"this weekend," "asap," or a specific date within roughly the next 72 hours from the current \
timestamp you're given. Posts from r/visitingnyc with any stated travel/date window are \
urgent by default, even without an explicit "asap"-style word, because visit windows are \
almost always short.

## Classification

- "high_value": strong persona match with clear, actionable intent — e.g. a named restaurant, \
an explicit date/urgency, a competitor mention, or a clear Supplier/give-away post. Worth \
engaging now.
- "monitor": plausibly on-topic and worth tracking, but weaker or more ambiguous signal \
(vague intent, no clear persona fit, low-signal keyword match).
- "not_relevant": the keyword pre-filter matched on a false positive — the post isn't \
actually about seeking or giving away a restaurant reservation (e.g. "walk-in closet," a \
home-cooking tip, an unrelated use of a matched word).

Always fill every field. reasoning is one sentence explaining why this post is or isn't worth \
engaging with, for a human skimming a Slack feed.
"""

CLASSIFICATION_TOOL = {
    "name": "submit_classification",
    "description": "Submit the structured classification and drafted replies for this Reddit post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["high_value", "monitor", "not_relevant"],
            },
            "persona": {
                "type": "string",
                "enum": ["supplier", "demand_chaser", "visitor", "spontaneous_planner"],
            },
            "market": {"type": "string", "enum": ["nyc", "london", "other"]},
            "urgency": {"type": "string", "enum": ["urgent", "normal"]},
            "mentions_competitor": {"type": "boolean"},
            "competitor_named": {"type": ["string", "null"]},
            "restaurant_named": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
            "draft_reply_with_resx": {"type": "string"},
            "draft_reply_neutral": {"type": "string"},
        },
        "required": [
            "classification", "persona", "market", "urgency", "mentions_competitor",
            "competitor_named", "restaurant_named", "reasoning",
            "draft_reply_with_resx", "draft_reply_neutral",
        ],
    },
}


def _build_user_message(post: dict, match_context: dict) -> str:
    age_hours = (datetime.now(timezone.utc).timestamp() - post["created_utc"]) / 3600
    hints = []
    if match_context.get("restaurant_hits"):
        hints.append(f"Pre-filter matched restaurant name(s): {', '.join(match_context['restaurant_hits'])}")
    if match_context.get("competitor_hits"):
        hints.append(f"Pre-filter matched competitor name(s): {', '.join(match_context['competitor_hits'])}")

    return (
        f"Current UTC time: {datetime.now(timezone.utc).isoformat()}\n"
        f"Subreddit: r/{post['subreddit']}\n"
        f"Post age: {age_hours:.1f} hours\n"
        f"Upvotes: {post['score']}\n"
        f"Title: {post['title']}\n"
        f"Body: {post['selftext'] or '(no body text)'}\n"
        + ("\n".join(hints) + "\n" if hints else "")
        + "\nClassify this post and draft both replies using the submit_classification tool."
    )


def classify_post(post: dict, match_context: dict) -> dict | None:
    """
    Returns the classification dict, or None if the API call failed or returned something
    unusable — caller is responsible for logging the failure and skipping the post.
    """
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_user_message(post, match_context)}],
        "tools": [CLASSIFICATION_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_classification"},
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[classify_and_draft] Anthropic request failed for post {post['id']} ({type(e).__name__}: {e})")
        return None

    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_classification":
            return block.get("input")

    print(f"[classify_and_draft] No tool_use block in response for post {post['id']}: {data}")
    return None
