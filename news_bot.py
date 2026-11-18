
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import arabic_reshaper
import feedparser
import requests
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

STATE_FILE = Path("seen_news.json")
MAX_POSTS_PER_MARKET = 3
MAX_AGE_HOURS = 12

MARKETS = {
    "saudi": {
        "label": "السوق السعودي",
        "query": (
            "(تاسي OR تداول OR الأسهم السعودية "
            "OR أرامكو OR الراجحي) when:1d"
        ),
        "webhook": "SAUDI_NEWS_WEBHOOK",
    },
    "us": {
        "label": "السوق الأمريكي",
        "query": (
            "(وول ستريت OR ناسداك OR ستاندرد آند بورز "
            "OR الفيدرالي الأمريكي OR أسهم التكنولوجيا) "
            "when:1d"
        ),
        "webhook": "US_NEWS_WEBHOOK",
    },
}

FONT_PATH = os.getenv(
    "ARABIC_FONT",
    "/usr/share/fonts/truetype/noto/"
    "NotoNaskhArabic-Regular.ttf",
)


def arabic(text):
    return get_display(
        arabic_reshaper.reshape(str(text))
    )


def clean_title(title, source):
    title = re.sub(r"\s+", " ", title).strip()

    if source:
        suffix = " - " + source
        if title.endswith(suffix):
            title = title[:-len(suffix)]

    return title


def news_id(title):
    normalized = re.sub(
        r"\s+", " ", title
    ).strip().casefold()

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def load_state():
    if not STATE_FILE.exists():
        return {"saudi": [], "us": []}

    with STATE_FILE.open(
        "r", encoding="utf-8"
    ) as file:
        data = json.load(file)

    return {
        market: data.get(market, [])
        for market in MARKETS
    }


def save_state(state):
    with STATE_FILE.open(
        "w", encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )


def fetch_news(query):
    response = requests.get(
        "https://news.google.com/rss/search",
        params={
            "q": query,
            "hl": "ar",
            "gl": "SA",
            "ceid": "SA:ar",
        },
        headers={
            "User-Agent": "SahmMarketNews/1.0"
        },
        timeout=30,
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            "Could not parse Google News RSS feed"
        )

    return feed.entries


def get_date(entry):
    raw = entry.get("published")

    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        candidate = (
            f"{current} {word}".strip()
        )

        rendered = arabic(candidate)

        width = draw.textbbox(
            (0, 0), rendered, font=font
        )[2]

        if width <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines[:5]


def make_card(market, headline, source):
    width, height = 1000, 560

    image = Image.new(
        "RGB", (width, height), "#101c24"
    )
    draw = ImageDraw.Draw(image)

    draw.rectangle(
        (0, 0, width, 13),
        fill="#17a66a",
    )

    heading_font = ImageFont.truetype(
        FONT_PATH, 46
    )
    title_font = ImageFont.truetype(
        FONT_PATH, 39
    )
    footer_font = ImageFont.truetype(
        FONT_PATH, 27
    )

    draw.text(
        (width - 55, 55),
        arabic("ديوانية سهم"),
        font=heading_font,
        fill="#42dc97",
        anchor="ra",
    )

    draw.text(
        (width - 55, 125),
        arabic(MARKETS[market]["label"]),
        font=footer_font,
        fill="#d2e5dd",
        anchor="ra",
    )

    lines = wrap_text(
        draw, headline, title_font, 880
    )

    y = 205

    for line in lines:
        draw.text(
            (width - 55, y),
            arabic(line),
            font=title_font,
            fill="white",
            anchor="ra",
        )
        y += 60

    draw.line(
        (55, 475, 945, 475),
        fill="#345447",
        width=2,
    )

    draw.text(
        (width - 55, 492),
        arabic(f"المصدر: {source}"),
        font=footer_font,
        fill="#a9c4b7",
        anchor="ra",
    )

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def send_news(market, item):
    webhook = os.getenv(
        MARKETS[market]["webhook"]
    )

    if not webhook:
        raise RuntimeError(
            f"Missing webhook for {market}"
        )

    title = item["title"]
    source = item["source"]
    link = item["link"]

    # Google News RSS descriptions often contain
    # lists of links, not article summaries.
    # Do not invent facts that aren't in the feed.
    description = (
        "للاطلاع على تفاصيل الخبر، "
        "يمكنكم زيارة المصدر الأصلي."
    )

    message = (
        f"📰 **{MARKETS[market]['label']} "
        "| ديوانية سهم**\n\n"
        f"**{title}**\n\n"
        f"{description}\n\n"
        f"🔗 **المصدر:** {source}\n"
        f"{link}"
    )

    card = make_card(
        market, title, source
    )

    payload = {
        "content": message,
        "allowed_mentions": {"parse": []},
    }

    response = requests.post(
        webhook,
        data={
            "payload_json": json.dumps(
                payload,
                ensure_ascii=False,
            )
        },
        files={
            "files[0]": (
                "market-news.png",
                card,
                "image/png",
            )
        },
        timeout=30,
    )

    response.raise_for_status()


def main():
    state = load_state()
    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(hours=MAX_AGE_HOURS)

    for market, config in MARKETS.items():
        print(f"Checking {market} news")

        try:
            entries = fetch_news(config["query"])
        except Exception as error:
            print(
                f"Feed error for {market}: {error}"
            )
            continue

        candidates = []

        for entry in entries:
            date = get_date(entry)

            if date is None or date < cutoff:
                continue

            source = entry.get(
                "source", {}
            ).get("title", "Google News")

            title = clean_title(
                entry.get("title", ""),
                source,
            )

            link = entry.get("link", "")

            if not title or not link:
                continue

            item_id = news_id(title)

            if item_id in state[market]:
                continue

            candidates.append({
                "id": item_id,
                "title": title,
                "source": source,
                "link": link,
                "date": date,
            })

        candidates.sort(
            key=lambda x: x["date"]
        )

        posted = 0

        for item in candidates:
            if posted >= MAX_POSTS_PER_MARKET:
                break

            try:
                send_news(market, item)
                state[market].append(item["id"])
                save_state(state)
                posted += 1
                print(
                    f"Posted {market}: "
                    f"{item['title']}"
                )
            except Exception as error:
                print(
                    f"Post error for {market}: "
                    f"{error}"
                )

        # Keep recent identifiers only.
        state[market] = state[market][-500:]
        save_state(state)

        print(
            f"{market}: {posted} new posts"
        )


if __name__ == "__main__":
    main()
