#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مانیتورینگ آگهی‌های مالک شخصی دیوار - مشهد
نسخه زمان‌محور + جلوگیری از تکراری (GitHub Actions)
"""

import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
import requests
from telegram import Bot
import config

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS_RAW = os.getenv("CHAT_IDS", "")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN تنظیم نشده است!")

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

DATA_DIR = Path("data")
LAST_RUN_FILE = DATA_DIR / "last_run.txt"
SEEN_FILE = DATA_DIR / "seen_tokens.txt"
MAX_PAGES = 8          # سقف ایمنی صفحات
MAX_SEEN_KEEP = 8000   # حداکثر تعداد توکن ذخیره‌شده


def ensure_data_dir():
    DATA_DIR.mkdir(exist_ok=True)


def load_last_run() -> datetime:
    """زمان آخرین اسکرپ را می‌خواند. اگر نبود، ۲ ساعت قبل را برمی‌گرداند."""
    ensure_data_dir()
    if LAST_RUN_FILE.exists():
        try:
            text = LAST_RUN_FILE.read_text(encoding="utf-8").strip()
            return datetime.fromisoformat(text)
        except Exception:
            pass
    return datetime.now(timezone.utc) - timedelta(hours=2)


def save_last_run(dt: datetime):
    ensure_data_dir()
    LAST_RUN_FILE.write_text(dt.isoformat(), encoding="utf-8")


def load_seen() -> set:
    ensure_data_dir()
    if not SEEN_FILE.exists():
        return set()
    try:
        lines = SEEN_FILE.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip()}
    except Exception:
        return set()


def save_seen(seen: set):
    ensure_data_dir()
    # فقط آخرین‌ها را نگه می‌داریم تا فایل خیلی بزرگ نشود
    items = list(seen)[-MAX_SEEN_KEEP:]
    SEEN_FILE.write_text("\n".join(items) + "\n", encoding="utf-8")


def search_divar(category: str, page: int = 1, last_post_date=None) -> dict:
    url = "https://api.divar.ir/v8/postlist/w/search"
    payload = {
        "city_ids": [config.CITY_ID],
        "search_data": {
            "form_data": {
                "data": {
                    "category": {"str": {"value": category}}
                }
            }
        }
    }
    if page > 1 and last_post_date is not None:
        payload["pagination_data"] = {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "last_post_date": last_post_date,
            "page": page,
            "layer_page": page
        }
    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Search failed for {category} page {page}: {e}")
        return {}


def extract_candidates(data: dict, category: str, cat_label: str) -> list:
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
    return results


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
        await bot.send_message(chat_id=chat_id, text=message, disable_web_page_preview=False)
        print(f"  ✅ پیام ارسال شد به {chat_id}")
    except Exception as e:
        print(f"  [ERROR] ارسال به {chat_id} ناموفق: {e}")


async def run_scraper():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] شروع اسکرپ دیوار...")
    print(f"  تعداد مشترکین: {len(CHAT_IDS)}")

    last_run = load_last_run()
    seen = load_seen()
    print(f"  آخرین اسکرپ: {last_run.isoformat()}")
    print(f"  تعداد توکن‌های ذخیره‌شده: {len(seen)}")

    bot = Bot(token=BOT_TOKEN)
    all_candidates = []
    run_started = datetime.now(timezone.utc)

    for cat in config.CATEGORIES:
        print(f"  → جستجو در دسته: {cat['label']}")
        last_post_date = None
        reached_old = False

        for page in range(1, MAX_PAGES + 1):
            print(f"     صفحه {page}...")
            data = search_divar(cat["category"], page=page, last_post_date=last_post_date)
            if not data:
                print(f"     صفحه {page} خالی یا خطا داشت.")
                break

            # استخراج last_post_date برای صفحه بعدی
            last_post_date = data.get("last_post_date") or (data.get("pagination") or {}).get("last_post_date")

            filtered = extract_candidates(data, cat["category"], cat["label"])
            print(f"     → {len(filtered)} آگهی بعد از فیلتر محله")

            # اگر آگهی جدیدی (از نظر زمان) نبود، می‌توانیم زودتر متوقف شویم
            # ولی چون API زمان دقیق هر ویجت را همیشه نمی‌دهد، فعلاً تا MAX_PAGES ادامه می‌دهیم
            all_candidates.extend(filtered)

            # اگر صفحه خالی از POST_ROW بود، توقف
            widgets = data.get("list_widgets") or []
            post_rows = [w for w in widgets if w.get("widget_type") == "POST_ROW"]
            if not post_rows:
                print("     دیگر آگهی جدیدی در این دسته نیست.")
                break

            await asyncio.sleep(3)

        await asyncio.sleep(2)

    # حذف تکراری بین دسته‌ها
    unique = {c["token"]: c for c in all_candidates}
    candidates = list(unique.values())
    random.shuffle(candidates)
    candidates = candidates[: config.MAX_PER_RUN]

    print(f"  تعداد کل کاندید بعد از فیلتر محله: {len(candidates)}")

    new_posts = []
    for cand in candidates:
        if cand["token"] in seen:
            continue

        details = fetch_post_details(cand["token"])
        formatted = is_owner_and_format(details, cand)

        if formatted:
            seen.add(formatted["post_token"])
            new_posts.append(formatted)
            print(f"  ✅ آگهی جدید: {formatted['title'][:50]}...")

        await asyncio.sleep(config.DELAY_BETWEEN_POSTS)

    print(f"  تعداد آگهی جدید برای ارسال: {len(new_posts)}")

    for post in new_posts:
        for chat_id in CHAT_IDS:
            await send_message(bot, chat_id, post["message"])
            await asyncio.sleep(0.5)

    # ذخیره وضعیت برای اجرای بعدی
    save_last_run(run_started)
    save_seen(seen)
    print(f"  وضعیت ذخیره شد (last_run + {len(seen)} توکن)")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] اسکرپ تمام شد.\n")


if __name__ == "__main__":
    asyncio.run(run_scraper())
