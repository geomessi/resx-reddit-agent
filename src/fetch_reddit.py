"""
Read-only Reddit access via PRAW. This agent never posts, comments, or votes — it only reads
submissions, so the client is built without a username/password (PRAW's read-only mode).
"""

from __future__ import annotations

import os

import praw

FETCH_LIMIT = 30  # newest submissions checked per subreddit per run


def get_reddit_client() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )


def _to_dict(submission) -> dict:
    return {
        "id": submission.id,
        "subreddit": str(submission.subreddit),
        "title": submission.title or "",
        "selftext": submission.selftext or "",
        "permalink": f"https://reddit.com{submission.permalink}",
        "created_utc": submission.created_utc,
        "score": submission.score,
        "link_flair_text": submission.link_flair_text or "",
        "author": str(submission.author) if submission.author else "[deleted]",
    }


def fetch_new_posts(reddit: praw.Reddit, subreddit_name: str, last_seen_id: str | None) -> tuple[list[dict], str | None]:
    """
    Returns (new_posts_oldest_first, newest_id_seen). `new_posts` excludes anything at or
    before `last_seen_id`. If `last_seen_id` is None (first run for this subreddit), returns
    the current top of /new up to FETCH_LIMIT — there's nothing to diff against yet.

    Isolated per-subreddit: raises nothing on Reddit API errors, caller decides how to log.
    """
    posts = []
    newest_id = last_seen_id
    for i, submission in enumerate(reddit.subreddit(subreddit_name).new(limit=FETCH_LIMIT)):
        if i == 0:
            newest_id = submission.id
        if last_seen_id is not None and submission.id == last_seen_id:
            break
        posts.append(_to_dict(submission))

    posts.reverse()  # oldest first, so Slack output and logs read chronologically
    return posts, newest_id
