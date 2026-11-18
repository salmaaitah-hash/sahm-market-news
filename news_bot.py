
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import feedparser
import requests


# ================================================
# SAHM MARKET NEWS — LIVE DISCORD AUTOMATION
# ================================================

STATE_FILE = Path("seen_news.json")

MAX_POSTS_PER_MARKET = 2
MAX_ARTICLE_AGE_HOURS = 12
MAX_SAVED_IDS = 1000

MARKETS = {
    "saudi": {
        "name": "🇸🇦 أخبار السوق السعودي",
        "color": 0x1FA36B,
        "webhook_secret": "SAUDI_NEWS_WEBHOOK",
        "query": (
            "(تاسي OR تداول OR أرامكو OR الراجحي "
            "OR الأسهم السعودية) when:1d"
        ),
    },
    "us": {
        "name": "🇺🇸 أخبار السوق الأمريكي",
        "color": 0x3478C0,
        "webhook_secret": "US_NEWS_WEBHOOK",
        "query": (
            "(وول ستريت OR ناسداك OR S&P 500 "
            "OR الفيدرالي الأمريكي OR الأسهم الأمريكية) "
            "when:1d"
        ),
    },
}


# ================================================
# LOAD PREVIOUSLY PUBLISHED NEWS
# ================================================

def load_state():
    if not STATE_FILE.exists():
        return {
            market: []
            for market in MARKETS
        }

    with STATE_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        saved = json.load(file)

    return {
        market: saved.get(market, [])
        for market in MARKETS
    }


# ================================================
# SAVE PUBLISHED NEWS HISTORY
# ================================================

def save_state(state):
    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_file.replace(STATE_FILE)


# ================================================
# FETCH ARABIC FINANCIAL NEWS
# ================================================

def fetch_news(query):
    parameters = urlencode({
        "q": query,
        "hl": "ar",
        "gl": "SA",
        "ceid": "SA:ar",
    })

    url = (
        "https://news.google.com/rss/search?"
        + parameters
    )

    response = requests.get(
        url,
        headers={
            "User-Agent": "SahmMarketNews/1.0"
        },
        timeout=30,
    )

    response.raise_for_status()

    feed = feedparser.parse(
        response.content
    )

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            "Unable to parse Google News RSS"
        )

    return feed.entries


# ================================================
# ARTICLE PUBLICATION DATE
# ================================================

def get_publication_date(entry):
    raw_date = entry.get("published")

    if not raw_date:
        return None

    try:
        published = parsedate_to_datetime(
            raw_date
        )

        if published.tzinfo is None:
            published = published.replace(
                tzinfo=timezone.utc
            )

        return published.astimezone(
            timezone.utc
        )

    except (ValueError, TypeError):
        return None


# ================================================
# CLEAN ARTICLE TITLE
# ================================================

def clean_title(raw_title, source):
    title = re.sub(
        r"\s+",
        " ",
        raw_title,
    ).strip()

    if source:
        suffix = f" - {source}"

        if title.endswith(suffix):
            title = title[:-len(suffix)]

    return title.strip()


# ================================================
# CREATE UNIQUE ARTICLE IDENTIFIER
# ================================================

def get_article_id(title):
    normalized = re.sub(
        r"\s+",
        " ",
        title.strip().casefold(),
    )

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# ================================================
# CREATE PROFESSIONAL DISCORD EMBED
# ================================================

def create_embed(market, article):
    config = MARKETS[market]

    description = (
        "📌 **تفاصيل الخبر**\n"
        "للاطلاع على التفاصيل الكاملة، "
        "اضغط على عنوان الخبر لزيارة المصدر."
    )

    return {
        "title": article["title"][:256],
        "url": article["link"],
        "description": description,
        "color": config["color"],
        "author": {
            "name": config["name"],
        },
        "fields": [
            {
                "name": "📰 المصدر",
                "value": article["source"][:1024],
                "inline": True,
            },
        ],
        "footer": {
            "text": (
                "ديوانية سهم | أخبار الأسواق"
            ),
        },
        "timestamp": (
            article["published_at"].isoformat()
        ),
    }


# ================================================
# SEND NEWS TO DISCORD
# ================================================

def send_to_discord(market, article):
    secret_name = MARKETS[market][
        "webhook_secret"
    ]

    webhook_url = os.getenv(secret_name)

    if not webhook_url:
        raise RuntimeError(
            f"Missing GitHub secret: {secret_name}"
        )

    payload = {
        "embeds": [
            create_embed(market, article)
        ],
        "allowed_mentions": {
            "parse": []
        },
    }

    # Discord automatically uses the webhook
    # name and avatar configured in Discord.
    # Do not override username or avatar_url.

    for attempt in range(3):
        response = requests.post(
            webhook_url,
            params={
                "wait": "true"
            },
            json=payload,
            timeout=30,
        )

        if response.status_code == 429:
            try:
                delay = float(
                    response.json().get(
                        "retry_after",
                        2,
                    )
                )
            except (
                ValueError,
                TypeError,
            ):
                delay = 2

            time.sleep(
                min(max(delay, 1), 30)
            )
            continue

        response.raise_for_status()

        result = response.json()

        if not result.get("id"):
            raise RuntimeError(
                "Discord did not confirm the post"
            )

        return result["id"]

    raise RuntimeError(
        "Discord rate limit retries exhausted"
    )


# ================================================
# PROCESS MARKET NEWS
# ================================================

def process_market(market, state):
    config = MARKETS[market]

    print(
        f"\nChecking {config['name']}"
    )

    entries = fetch_news(
        config["query"]
    )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=MAX_ARTICLE_AGE_HOURS
        )
    )

    already_posted = set(
        state[market]
    )

    current_batch = set()
    candidates = []

    for entry in entries:
        published_at = get_publication_date(
            entry
        )

        if published_at is None:
            continue

        if published_at < cutoff:
            continue

        source_data = entry.get(
            "source",
            {},
        )

        source = source_data.get(
            "title",
            "مصدر غير محدد",
        )

        title = clean_title(
            entry.get("title", ""),
            source,
        )

        link = entry.get(
            "link",
            "",
        )

        if not title:
            continue

        if not link.startswith(
            "https://"
        ):
            continue

        article_id = get_article_id(
            title
        )

        if article_id in already_posted:
            continue

        if article_id in current_batch:
            continue

        current_batch.add(
            article_id
        )

        candidates.append({
            "id": article_id,
            "title": title,
            "source": source,
            "link": link,
            "published_at": published_at,
        })

    # Publish newest articles first
    candidates.sort(
        key=lambda article: (
            article["published_at"]
        ),
        reverse=True,
    )

    selected = candidates[
        :MAX_POSTS_PER_MARKET
    ]

    print(
        f"Found {len(selected)} "
        f"new articles for {market}"
    )

    published_count = 0

    for article in selected:

        # LIVE DISCORD PUBLISHING
        message_id = send_to_discord(
            market,
            article,
        )

        # Save identifier only after successful post
        state[market].append(
            article["id"]
        )

        state[market] = state[
            market
        ][-MAX_SAVED_IDS:]

        save_state(
            state
        )

        published_count += 1

        print(
            f"POSTED [{market}] "
            f"{article['title']} "
            f"(Discord ID: {message_id})"
        )

    print(
        f"{market}: "
        f"{published_count} posts published"
    )


# ================================================
# MAIN — RUN BOTH MARKETS
# ================================================

def main():
    print(
        "================================="
    )
    print(
        "SAHM MARKET NEWS — LIVE"
    )
    print(
        "================================="
    )

    state = load_state()

    failed_markets = []

    for market in MARKETS:
        try:
            process_market(
                market,
                state,
            )

        except Exception as error:
            print(
                f"ERROR [{market}]: {error}",
                file=sys.stderr,
            )

            failed_markets.append(
                market
            )

    if failed_markets:
        raise RuntimeError(
            "Failed markets: "
            + ", ".join(
                failed_markets
            )
        )

    print(
        "\nLive news automation completed."
    )


if __name__ == "__main__":
    main()
