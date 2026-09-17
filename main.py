#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مانیتورینگ آگهی‌های مالک شخصی دیوار - مشهد
نسخه مخصوص GitHub Actions (پشتیبانی از چند مشترک)
"""

import os
import time
import random
import sqlite3
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import requests
from telegram import Bot
import config

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS_RAW = os.getenv("CHAT_IDS", "")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN تنظیم نشده است!")

# تبدیل رشته آیدی‌ها به لیست
CHAT_IDS = [cid.strip() for cid in CHAT_IDS_RAW.split(",") if cid.strip()]

if not CHAT_IDS:
    raise ValueError("هیچ CHAT_IDS تنظیم نشده است!")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Origin": "https://divar.ir",
    "Referer": "https://divar.ir/s/mashhad",
    "Content-Type": "application/json",
}


def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/seen_posts.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            post_token TEXT PRIMARY KEY,
            title TEXT,
            district TEXT,
            category TEXT,
            seen_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_seen(token: str) -> bool:
    conn = sqlite3.connect("data/seen_posts.db")
    cur = conn.execute("SELECT 1 FROM seen WHERE post_token = ?", (token,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def mark_seen(token: str, title: str, district: str, category: str):
    conn = sqlite3.connect("data/seen_posts.db")
    conn.execute(
        "INSERT OR IGNORE INTO seen (post_token, title, district, category, seen_at) VALUES (?, ?, ?, ?, ?)",
        (token, title, district, category, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def search_divar(category: str, page: int = 1) -> dict:
    url = "https://api.divar.ir/v8/postlist/w/search"
    payload = {
        "city_ids": [config.CITY_ID],
        "pagination_data": {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "page": page
        },
        "search_data": {
            "form_data": {
                "data": {
                    "category": {"str": {"value": category}}
                }
            }
        }
    }
    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Search failed for {category} page {page}: {e}")
        return {}


def filter_districts(data: dict, category: str, cat_label: str) -> list:
    results = []
    widgets = data.get("list_widgets", [])

    for w in widgets:
        if w.get("widget_type") != "POST_ROW":
            continue

        payload = ((w.get("data") or {}).get("action") or {}).get("payload") or {}
        token = payload.get("token")
        if not token:
            continue

        web_info = payload.get("web_info") or {}
        district = web_info.get("district_persian") or ""
        title = web_info.get("title") or (w.get("data") or {}).get("title") or ""

        if any(t in district for t in config.TARGET_DISTRICTS):
            results.append({
                "token": token,
                "district": district,
                "title": title,
                "category": category,
                "catLabel": cat_label
            })

    unique = {r["token"]: r for r in results}
    unique_list = list(unique.values())
    random.shuffle(unique_list)
    return unique_list[:config.MAX_PER_RUN]


def fetch_post_details(token: str) -> dict:
    url = f"https://api.divar.ir/v8/posts-v2/web/{token}"
    headers = HEADERS.copy()
    headers["Referer"] = f"https://divar.ir/v/{token}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Details failed for {token}: {e}")
        return {}


def is_owner_and_format(details: dict, cand: dict):
    we = details.get("webengage") or {}
    if we.get("business_type") != "personal":
        return None

    token = cand["token"]
    title = cand["title"]
    district = cand["district"]
    cat_label = cand["catLabel"]

    desc = ""
    rows = []

    for section in details.get("sections", []):
        for w in section.get("widgets", []):
            dt = w.get("data") or {}
            t = dt.get("@type", "")

            if t.endswith("DescriptionRowData") and dt.get("text"):
                desc = dt["text"]

            if t.endswith("GroupInfoRow"):
                for it in dt.get("items", []):
                    if it.get("title") and it.get("value"):
                        rows.append(f"{it['title']}: {it['value']}")

            if t.endswith("UnexpandableRowData") and dt.get("title") and dt.get("value"):
                rows.append(f"{dt['title']}: {dt['value']}")

    haystack = (title + " " + desc).replace("‌", " ")
    if any(k in haystack for k in config.AGENCY_KEYWORDS):
        return None

    lines = [
        f"🏠 {title or 'آگهی جدید'}",
        f"📂 دسته: {cat_label}",
        f"📍 محله: {district} - مشهد",
        "👤 نوع آگهی‌دهنده: مالک (شخصی)",
    ]
    if rows:
        lines.append("")
        lines.append("📋 مشخصات:")
        lines.extend(rows)
    if desc:
        lines.append("")
        lines.append("📝 توضیحات:")
        lines.append(desc)
    lines.append("")
    lines.append(f"🔗 https://divar.ir/v/{token}")

    return {
        "post_token": token,
        "title": title,
        "district": district,
        "category": cand["category"],
        "message": "\n".join(lines)
    }


async def send_message(bot: Bot, chat_id: str, message: str):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            disable_web_page_preview=False
        )
        print(f"  ✅ پیام ارسال شد به {chat_id}")
    except Exception as e:
        print(f"  [ERROR] ارسال به {chat_id} ناموفق: {e}")


async def run_scraper():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] شروع اسکرپ دیوار...")
    print(f"  تعداد مشترکین: {len(CHAT_IDS)}")

    init_db()
    bot = Bot(token=BOT_TOKEN)

    all_candidates = []

        for cat in config.CATEGORIES:
        print(f"  → جستجو در دسته: {cat['label']}")
        for page in [1, 2, 3]:
            print(f"     صفحه {page}...")
            data = search_divar(cat["category"], page=page)
            filtered = filter_districts(data, cat["category"], cat["label"])
            all_candidates.extend(filtered)
            await asyncio.sleep(3)   # فاصله بین صفحات
        await asyncio.sleep(2)

    unique = {c["token"]: c for c in all_candidates}
    candidates = list(unique.values())
    random.shuffle(candidates)
    candidates = candidates[:config.MAX_PER_RUN]

    print(f"  تعداد کاندید بعد از فیلتر محله: {len(candidates)}")

    new_posts = []
    for cand in candidates:
        if is_seen(cand["token"]):
            continue

        details = fetch_post_details(cand["token"])
        formatted = is_owner_and_format(details, cand)

        if formatted:
            mark_seen(
                formatted["post_token"],
                formatted["title"],
                formatted["district"],
                formatted["category"]
            )
            new_posts.append(formatted)
            print(f"  ✅ آگهی جدید: {formatted['title'][:50]}...")

        await asyncio.sleep(config.DELAY_BETWEEN_POSTS)

    print(f"  تعداد آگهی جدید برای ارسال: {len(new_posts)}")

    for post in new_posts:
        for chat_id in CHAT_IDS:
            await send_message(bot, chat_id, post["message"])
            await asyncio.sleep(0.5)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] اسکرپ تمام شد.\n")


if __name__ == "__main__":
    asyncio.run(run_scraper())
