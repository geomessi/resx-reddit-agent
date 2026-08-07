# resx-reddit-agent

Monitors Reddit for posts where ResX (a free peer-to-peer restaurant reservation marketplace
in NYC + London) is relevant, classifies them, and drafts reply variants for the social team
to review in Slack (`#social`). **It never posts, comments, or votes on Reddit** — it only
reads, and everything it produces is a draft for a human to send or ignore.

Grounded in a 19-query, 4-LLM AEO audit of how ChatGPT/Claude/Gemini/Perplexity currently
talk about this space — the classification logic and reply prompts in
[`src/classify_and_draft.py`](src/classify_and_draft.py) actively correct the specific gaps
that audit found (see the system prompt in that file for the full detail).

## How it works

Every 15 minutes (`.github/workflows/reddit-agent.yml`):

1. Fetch the newest posts per subreddit via the Reddit API (read-only PRAW client).
2. Skip anything already processed (`state/last_seen.json` tracks the last-seen post id per
   subreddit).
3. Run a cheap keyword pre-filter (`src/filter_posts.py`) — only posts that match a
   reservation-intent phrase, a tracked restaurant name, or a competitor name go any further.
   Every match/drop decision is logged to `logs/prefilter_log.jsonl`.
4. Anything that passes gets **one** Claude API call (`src/classify_and_draft.py`,
   `claude-sonnet-5`) that classifies persona/market/urgency and drafts two reply variants.
   `not_relevant` results and failed API calls are logged to `logs/dropped_posts.jsonl`, not
   surfaced in Slack.
5. Everything else queues up in `state/pending_batch.json`.
6. Every ~30 minutes (i.e. every other 15-minute run), if the queue is non-empty, it's
   formatted as Slack Block Kit and posted to `#social` in one batch, then cleared. If nothing
   new came in, the batch window is skipped silently — no empty Slack messages.

The 30-minute check is time-based (compares against `state/last_batch_sent.json`), not a
fixed "every 2nd run" counter, so a late or skipped cron run doesn't throw off the cadence.

State (`state/*.json`) and logs (`logs/*.jsonl`) are committed back to the repo by the
workflow after each real (non-dry-run) run.

## Setup

1. **Reddit API app** — go to <https://www.reddit.com/prefs/apps>, create a **script**-type
   app. This gives you `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`. `REDDIT_USER_AGENT` is
   just a descriptive string, e.g. `resx-reddit-agent:v1.0 (by /u/yourusername)`. No Reddit
   username/password is needed — the client only ever reads.
2. **Anthropic API key** — `ANTHROPIC_API_KEY`.
3. **Slack webhook** — `SLACK_WEBHOOK_URL`, an Incoming Webhook pointed at `#social`. If
   there's already a Slack app posting to `#social` (e.g. from a sibling project), you likely
   don't need a new one — just reuse that webhook URL.
4. Add all five as repo secrets: **Settings → Secrets and variables → Actions**.
5. For local development, copy `.env.example` to `.env` and fill it in (load it into your
   shell however you prefer — the scripts read plain `os.environ`, there's no built-in
   dotenv loading).

## Running locally / testing

```bash
pip install -r requirements.txt
python src/main.py --dry-run
```

`--dry-run` (or `DRY_RUN=1`) prints what would be posted to Slack instead of posting, and
writes no state — safe to run repeatedly without burning through real posts or advancing
`last_seen`.

To test the full GitHub Actions path without waiting for the cron:

```bash
gh workflow run reddit-agent.yml -f dry_run=true
```

## Adding subreddits or keywords

- Subreddits: edit `SUBREDDITS` in [`src/main.py`](src/main.py).
- Reservation-intent / visitor / supply-side phrases: edit the lists at the top of
  [`src/filter_posts.py`](src/filter_posts.py).
- Restaurant and competitor names: edit `data/top_restaurants.json` (see below).
- Two subreddits have stricter, hand-coded filters because they're broad/off-topic by
  default — see `_passes_subreddit_override` in `src/filter_posts.py`:
  - `r/unitedkingdom` requires a food-related post flair *and* a standard keyword match.
  - `r/datingadvice` requires a tracked restaurant name *and* a date-night phrase together.

Check `logs/prefilter_log.jsonl` periodically — it records every matched *and* dropped post,
which is the main input for tuning these lists over time.

## Refreshing `data/top_restaurants.json`

This file drives what the pre-filter watches for by name. It's currently seeded from ResX's
top-performing restaurants by claim volume (trailing 180 days) at the time this repo was
built. To refresh:

1. Pull fresh 180-day claim-volume leaderboards for NYC and London from ResX's analytics.
2. Update the `nyc` and `london` arrays (ranked lists of restaurant names as they'd plausibly
   be typed on Reddit — no need for exact legal names).
3. Leave `high_recognition` (broadly recognizable names likely to appear in casual posts even
   at lower ResX volume) and `competitors` alone unless the competitive landscape changes.

No code changes needed — `filter_posts.py` and `classify_and_draft.py` both read this file at
runtime.

## Error handling

- A failed fetch for one subreddit is logged and skipped; the run continues with the rest.
- A failed Claude call for one post is logged to `logs/dropped_posts.jsonl` (with
  `error: "claude_error"`) and skipped; the run continues.
- A failed Slack post leaves `state/pending_batch.json` untouched so the same batch is
  retried on the next run, instead of being silently lost.
- Anything unexpected at the top level exits non-zero with a clear `FATAL` log line rather
  than failing silently.
