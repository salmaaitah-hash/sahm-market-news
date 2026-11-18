
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


STATE_FILE = Path("seen_news.json")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

MAX_POSTS_PER_MARKET = 2
MAX_ARTICLE_AGE_HOURS = 12
MAX_SAVED_IDS = 1000

MARKETS = {
    "saudi": {
        "name": "🇸🇦 أخبار السوق السعودي",
        "color": 0x1FA36B,
        "secret": "SAUDI_NEWS_WEBHOOK",
        "query": (
            "(تاسي OR تداول OR أرامكو OR الراجحي "
            "OR الأسهم السعودية) when:1d"
        ),
    },
    "us": {
        "name": "🇺🇸 أخبار السوق الأمريكي",
        "color": 0x3478C0,
        "secret": "US_NEWS_WEBHOOK",
        "query": (
            "(وول ستريت OR ناسداك OR S&P 500 "
            "OR الفيدرالي الأمريكي OR الأسهم الأمريكية) "
            "when:1d"
        ),
    },
}


def load_state():
    if not STATE_FILE.exists():
        return {"saudi": [], "us": []}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        saved = json.load(file)

    return {
        key: saved.get(key, [])
        for key in MARKETS
    }


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary.replace(STATE_FILE)


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
        timeout=30,
        headers={
            "User-Agent": "SahmMarketNews/1.0"
        },
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)

    if feed.bozo and not feed.entries:
        raise RuntimeError("RSS feed could not be parsed")

    return feed.entries


def published_at(entry):
    raw = entry.get("published")

    if not raw:
        return None

    try:
        date = parsedate_to_datetime(raw)

        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        return date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def clean_title(raw_title, source):
    title = re.sub(r"\s+", " ", raw_title).strip()

    suffix = f" - {source}"

    if source and title.endswith(suffix):
        title = title[:-len(suffix)]

    return title.strip()


def article_identifier(title):
    normalized = re.sub(
        r"\s+",
        " ",
        title.strip().casefold(),
    )

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def build_embed(market, article):
    config = MARKETS[market]

    # Google News RSS doesn't reliably provide
    # a factual article summary. Avoid inventing one.
    description = (
        "📌 **متابعة الخبر**\n"
        "اطّلع على التفاصيل الكاملة من المصدر "
        "عبر الرابط الموجود في عنوان الخبر."
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
            "text": "ديوانية سهم | أخبار الأسواق",
        },
        "timestamp": article["date"].isoformat(),
    }


def send_to_discord(market, article):
    secret_name = MARKETS[market]["secret"]
    webhook = os.getenv(secret_name)

    if not webhook:
        raise RuntimeError(
            f"Missing GitHub secret: {secret_name}"
        )

    payload = {
        "embeds": [build_embed(market, article)],
        "allowed_mentions": {
            "parse": []
        },
    }

    # Do not send username or avatar_url.
    # Discord will use the webhook's saved identity.

    for attempt in range(3):
        response = requests.post(
            webhook,
            params={"wait": "true"},
            json=payload,
            timeout=30,
        )

        if response.status_code == 429:
            delay = float(
                response.json().get("retry_after", 2)
            )
            time.sleep(min(delay, 30))
            continue

        response.raise_for_status()

        message = response.json()

        if not message.get("id"):
            raise RuntimeError(
                "Discord did not confirm the message"
            )

        return

    raise RuntimeError(
        "Discord rate limit retries exhausted"
    )


def process_market(market, state):
    config = MARKETS[market]

    print(f"Checking: {config['name']}")

    entries = fetch_news(config["query"])

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(hours=MAX_ARTICLE_AGE_HOURS)

    seen = set(state[market])
    candidates = []
    current_batch = set()

    for entry in entries:
        date = published_at(entry)

        if date is None or date < cutoff:
            continue

        source_data = entry.get("source", {})
        source = source_data.get(
            "title", "ناشر غير محدد"
        )

        title = clean_title(
            entry.get("title", ""),
            source,
        )

        link = entry.get("link", "")

        if not title or not link.startswith("https://"):
            continue

        identifier = article_identifier(title)

        if identifier in seen:
            continue

        if identifier in current_batch:
            continue

        current_batch.add(identifier)

        candidates.append({
            "id": identifier,
            "title": title,
            "source": source,
            "link": link,
            "date": date,
        })

    # Publish newest items first.
    candidates.sort(
        key=lambda item: item["date"],
        reverse=True,
    )

    count = 0

    for article in candidates[:MAX_POSTS_PER_MARKET]:
        if DRY_RUN:
            print(
                f"TEST ONLY [{market}]: "
                f"{article['title']}"
            )
            continue

        send_to_discord(market, article)

        state[market].append(article["id"])
        state[market] = state[market][-MAX_SAVED_IDS:]

        save_state(state)

        count += 1

        print(
            f"POSTED [{market}]: "
            f"{article['title']}"
        )

    print(
        f"{market}: {count} articles published"
    )


def main():
    state = load_state()
    errors = []

    for market in MARKETS:
        try:
            process_market(market, state)
        except Exception as error:
            print(
                f"ERROR [{market}]: {error}",
                file=sys.stderr,
            )
            errors.append(market)

    if errors:
        raise RuntimeError(
            "Failed markets: " + ", ".join(errors)
        )


if __name__ == "__main__":
    main()
