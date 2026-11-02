#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات دیوار (مشهد) → تلگرام + صف بررسی پنل مادر (Neway)

API:
  POST {NEWAY_API_URL}/admin/mother-properties
  Header: X-Bot-Token: nwb_...
  Bot → status=pending, category=new, listingSource=telegram
"""

from __future__ import annotations

import os
import re
import random
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import Bot

import config
from location_resolver import LocationResolver

load_dotenv()

# ── Telegram (اجباری) ────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
TOPIC_SELL = (os.getenv("TOPIC_SELL") or "").strip()
TOPIC_RENT = (os.getenv("TOPIC_RENT") or "").strip()

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN تنظیم نشده است!")
if not CHAT_ID:
    raise ValueError("CHAT_ID گروه تنظیم نشده است!")
if not TOPIC_SELL or not TOPIC_RENT:
    raise ValueError("TOPIC_SELL و TOPIC_RENT باید تنظیم شوند!")

TOPIC_SELL_ID = int(TOPIC_SELL)
TOPIC_RENT_ID = int(TOPIC_RENT)

# ── Neway panel (اختیاری؛ بدون این فقط تلگرام) ─────────────────────────
NEWAY_API_URL = (os.getenv("NEWAY_API_URL") or "").rstrip("/")
NEWAY_BOT_TOKEN = (os.getenv("NEWAY_BOT_TOKEN") or "").strip()
PANEL_ENABLED = bool(NEWAY_API_URL and NEWAY_BOT_TOKEN)
# نگاشت پویا (متن خام → placeId) + درخت مکان‌ها؛ فقط در حافظه، config.* دست‌نخورده
MAPPER = LocationResolver(NEWAY_API_URL, NEWAY_BOT_TOKEN, os.getenv("NEWAY_MAPPINGS_PATH") or "/locations/mappings")

SELL_CATEGORIES = {"apartment-sell", "house-villa-sell"}
RENT_CATEGORIES = {"apartment-rent", "house-villa-rent"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Origin": "https://divar.ir",
    "Referer": "https://divar.ir/s/mashhad",
    "Content-Type": "application/json",
}

DATA_DIR = Path("data")
LAST_RUN_FILE = DATA_DIR / "last_run.txt"
SEEN_FILE = DATA_DIR / "seen_tokens.txt"
MAX_PAGES = 15
MAX_SEEN_KEEP = 8000


# ── persistence ───────────────────────────────────────────────────────

def ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def load_last_run() -> datetime:
    ensure_data_dir()
    if LAST_RUN_FILE.exists():
        try:
            return datetime.fromisoformat(LAST_RUN_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    return datetime.now(timezone.utc) - timedelta(hours=2)


def save_last_run(dt: datetime) -> None:
    ensure_data_dir()
    LAST_RUN_FILE.write_text(dt.isoformat(), encoding="utf-8")


def load_seen() -> set:
    ensure_data_dir()
    if not SEEN_FILE.exists():
        return set()
    try:
        tokens = [ln.strip() for ln in SEEN_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(tokens) > MAX_SEEN_KEEP:
            tokens = tokens[-MAX_SEEN_KEEP:]
        return set(tokens)
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    ensure_data_dir()
    items = list(seen)
    if len(items) > MAX_SEEN_KEEP:
        items = items[-MAX_SEEN_KEEP:]
    SEEN_FILE.write_text("\n".join(items) + "\n", encoding="utf-8")


def get_topic_id(category: str) -> int:
    return TOPIC_SELL_ID if category in SELL_CATEGORIES else TOPIC_RENT_ID


# ── text helpers ──────────────────────────────────────────────────────

def fold_fa(s: str) -> str:
    if not s:
        return ""
    s = str(s).replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", s).strip().lower()


def contains_any(text: str, keywords) -> bool:
    t = fold_fa(text)
    return any(fold_fa(k) in t for k in keywords)


def parse_int_fa(raw):
    if raw is None:
        return None
    s = str(raw)
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    s = s.translate(trans).replace(",", "").replace("٬", "").replace("،", "")
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def parse_price_toman(raw):
    return parse_int_fa(raw)


# ── Divar ─────────────────────────────────────────────────────────────

def _jittered_sleep(base_seconds: float) -> None:
    """Sleep base_seconds * uniform(0.7, 1.4) to avoid predictable cadence."""
    import time
    time.sleep(base_seconds * random.uniform(0.7, 1.4))


def _divar_request(method: str, url: str, headers: dict | None = None, **kwargs) -> "requests.Response | None":
    """
    Single Divar HTTP call with one retry on 429/503.
    Back-off: 30–60 s jittered sleep before the single retry.
    Returns the Response on success, None on failure.
    """
    timeout = kwargs.pop("timeout", 25)
    hdrs = headers if headers is not None else HEADERS
    for attempt in range(2):
        try:
            resp = requests.request(method, url, headers=hdrs, timeout=timeout, **kwargs)
            if resp.status_code in (429, 503) and attempt == 0:
                wait = 30 + random.uniform(0, 30)  # 30–60 s
                print(f"[WARN] Divar {resp.status_code} — back-off {wait:.0f}s then retry once")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except Exception as e:
            if attempt == 0:
                # network blip / timeout — one quick retry after short jitter
                _jittered_sleep(2)
                continue
            print(f"[ERROR] Divar request failed ({method} {url}): {e}")
            return None
    return None


def search_divar(category: str, page: int = 1, last_post_date=None) -> dict:
    url = "https://api.divar.ir/v8/postlist/w/search"
    payload = {
        "city_ids": [config.CITY_ID],
        "pagination_data": {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "page": page,
            "layer_page": page,
        },
        "search_data": {
            "form_data": {
                "data": {
                    "category": {"str": {"value": category}},
                }
            }
        },
    }
    if last_post_date is not None:
        payload["pagination_data"] = {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "last_post_date": last_post_date,
            "page": page,
            "layer_page": page,
        }
    resp = _divar_request("POST", url, json=payload, timeout=25)
    if resp is None:
        print(f"[ERROR] Search failed for {category} page {page}")
        return {}
    try:
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Search JSON parse failed for {category} page {page}: {e}")
        return {}


def extract_candidates(data: dict, category: str, cat_label: str) -> list:
    results = []
    for w in data.get("list_widgets", []):
        if w.get("widget_type") != "POST_ROW":
            continue
        d = w.get("data") or {}
        payload = ((d.get("action") or {}).get("payload") or {})
        token = payload.get("token")
        if not token:
            continue
        web_info = payload.get("web_info") or {}
        district = web_info.get("district_persian") or ""
        title = web_info.get("title") or d.get("title") or ""
        list_text = " ".join([
            str(title or ""),
            str(d.get("top_description_text") or ""),
            str(d.get("middle_description_text") or ""),
            str(d.get("bottom_description_text") or ""),
            str(web_info.get("title") or ""),
        ])
        if contains_any(list_text, config.LIST_SKIP_KEYWORDS):
            continue
        if any(t in district for t in config.TARGET_DISTRICTS) or MAPPER.knows_raw(district):
            results.append({
                "token": token,
                "district": district,
                "title": title,
                "category": category,
                "catLabel": cat_label,
            })
    return results


def fetch_post_details(token: str) -> dict:
    url = f"https://api.divar.ir/v8/posts-v2/web/{token}"
    headers = {**HEADERS, "Referer": f"https://divar.ir/v/{token}"}
    resp = _divar_request("GET", url, headers=headers, timeout=15)
    if resp is None:
        print(f"[ERROR] Details failed for {token}")
        return {}
    try:
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Details JSON parse failed for {token}: {e}")
        return {}


def extract_rows_and_desc(details: dict):
    rows = {}
    desc = ""
    for section in details.get("sections", []):
        for w in section.get("widgets", []):
            dt = w.get("data") or {}
            t = dt.get("@type", "")
            if t.endswith("DescriptionRowData") and dt.get("text"):
                desc = dt["text"]
            if t.endswith("GroupInfoRow"):
                for it in dt.get("items", []):
                    if it.get("title") and it.get("value") is not None:
                        rows[str(it["title"]).strip()] = str(it["value"]).strip()
            if t.endswith("UnexpandableRowData") and dt.get("title") and dt.get("value") is not None:
                rows[str(dt["title"]).strip()] = str(dt["value"]).strip()
    return rows, desc


def pick_row(rows: dict, *keys):
    folded = {fold_fa(k): v for k, v in rows.items()}
    for k in keys:
        fk = fold_fa(k)
        if fk in folded:
            return folded[fk]
        for rk, rv in folded.items():
            if fk in rk or rk in fk:
                return rv
    return None


# ── نگاشت مطابق createSchema پنل مادر ─────────────────────────────────
# DEAL_TYPES:     رهن و اجاره | خرید و فروش | پیش‌فروش | مشارکت
# PROPERTY_TYPES: آپارتمان | ویلایی | ویلا (باغ) | تجاری | اداری
# DIRECTIONS:     شمالی | جنوبی | دونبش | سه‌نبش | دوممر | شرقی‌غربی
# DOC_STATUS:     ready | inprogress | promissory
# AMENITIES:      elevator | parking | terrace | storage
# Location resolution is handled exclusively by LocationResolver (location_resolver.py).


def map_deal_type(category: str) -> str:
    if category in SELL_CATEGORIES:
        return "خرید و فروش"
    return "رهن و اجاره"


def map_property_type(category: str, rows: dict, title: str, desc: str):
    blob = fold_fa(f"{category} {title} {desc} {' '.join(rows.values())}")
    if "apartment" in category or "آپارتمان" in blob or "اپارتمان" in blob:
        return "آپارتمان"
    if "تجاری" in blob or "مغازه" in blob:
        return "تجاری"
    if "اداری" in blob or "دفتر کار" in blob:
        return "اداری"
    if "باغ" in blob and "ویلا" in blob:
        return "ویلا (باغ)"
    if "ویلا" in blob or "villa" in category:
        return "ویلایی"
    if "house-villa" in category or "خانه" in blob or "کلنگی" in blob or "ویلایی" in blob:
        return "ویلایی"
    return None


def map_direction(rows: dict, desc: str, title: str):
    blob = fold_fa(
        f"{title} {desc} {' '.join(rows.values())} "
        f"{pick_row(rows, 'جهت', 'جهت ساختمان', 'بر') or ''}"
    )
    if any(k in blob for k in ["سه نبش", "سه‌نبش", "سهنبش"]):
        return "سه‌نبش"
    if any(k in blob for k in ["دونبش", "دو نبش"]):
        return "دونبش"
    if any(k in blob for k in ["دو مر", "دوممر", "دو ممر"]):
        return "دوممر"
    if "شرقی غربی" in blob or "شرقی‌غربی" in blob or ("شرقی" in blob and "غربی" in blob):
        return "شرقی‌غربی"
    if "شمالی" in blob:
        return "شمالی"
    if "جنوبی" in blob:
        return "جنوبی"
    return None


def map_doc_status(rows: dict, desc: str, title: str):
    blob = fold_fa(
        f"{title} {desc} {' '.join(rows.values())} "
        f"{pick_row(rows, 'سند', 'وضعیت سند', 'مدرک') or ''}"
    )
    if any(k in blob for k in ["قولنامه", "قول نامه", "قول‌نامه"]):
        return "promissory"
    if any(k in blob for k in ["در دست اقدام", "در جریان", "نقص سند", "در حال انتقال"]):
        return "inprogress"
    if any(k in blob for k in ["سند دار", "سنددار", "تک برگ", "تک‌برگ", "ششدانگ", "شش دانگ", "سند رسمی"]):
        return "ready"
    return None


def map_amenities(rows: dict, desc: str, title: str):
    blob = fold_fa(f"{title} {desc} {' '.join(rows.values())}")
    found = {}
    if any(k in blob for k in ["آسانسور", "اسانسور"]):
        found["elevator"] = True
    if any(k in blob for k in ["پارکینگ", "پارکنگ"]):
        found["parking"] = True
    if any(k in blob for k in ["تراس", "بالکن"]):
        found["terrace"] = True
    if any(k in blob for k in ["انباری", "انبار "]):
        found["storage"] = True
    return found or None


def map_units_per_floor(rows: dict):
    return parse_int_fa(pick_row(rows, "تعداد واحد در طبقه", "واحد در طبقه", "واحدها در طبقه"))


def is_owner_and_format(details: dict, cand: dict):
    """فقط personal + فیلتر سخت. خروجی: message تلگرام + panel_payload برای API."""
    we = details.get("webengage") or {}
    if we.get("business_type") != "personal":
        return None

    token = cand["token"]
    title = cand["title"] or ""
    district = cand["district"] or ""
    cat_label = cand["catLabel"]
    category = cand["category"]

    rows, desc = extract_rows_and_desc(details)
    haystack = f"{title} {desc} {' '.join(rows.values())}"

    if contains_any(haystack, config.AGENCY_KEYWORDS):
        return None
    if contains_any(haystack, config.SEEKER_KEYWORDS):
        return None
    if contains_any(haystack, config.ROOMMATE_KEYWORDS):
        return None
    if contains_any(haystack, config.LIST_SKIP_KEYWORDS):
        return None

    deal_type = map_deal_type(category)
    property_type = map_property_type(category, rows, title, desc)

    area_m = parse_int_fa(pick_row(rows, "متراژ", "متر", "زیربنا", "مساحت"))
    rooms = parse_int_fa(pick_row(rows, "تعداد اتاق", "اتاق", "خواب", "تعداد خواب", "تعداد اتاق‌ها"))
    year_built = parse_int_fa(pick_row(rows, "سال ساخت", "ساخت"))
    floor = parse_int_fa(pick_row(rows, "طبقه", "طبقه ملک"))
    units_per_floor = map_units_per_floor(rows)
    direction = map_direction(rows, desc, title)
    doc_status = map_doc_status(rows, desc, title)
    amenities = map_amenities(rows, desc, title)

    # خرید و فروش → فقط price | رهن و اجاره → price=ودیعه + rentPrice=اجاره
    price = None
    rent_price = None
    if category in SELL_CATEGORIES:
        price = parse_price_toman(pick_row(rows, "قیمت کل", "قیمت", "مبلغ", "قیمت فروش"))
    else:
        price = parse_price_toman(pick_row(rows, "ودیعه", "رهن", "پیش‌پرداخت", "پیش پرداخت", "مبلغ ودیعه"))
        rent_price = parse_price_toman(pick_row(rows, "اجاره ماهانه", "اجارهٔ ماهانه", "اجاره", "مبلغ اجاره"))
        if price is None and rent_price is None:
            price = parse_price_toman(pick_row(rows, "قیمت", "مبلغ"))

    MAPPER.refresh_if_stale()
    loc = MAPPER.resolve(district, haystack)
    city, region, neighborhood = loc.city, loc.region, loc.neighborhood
    needs_review = loc.needs_review
    if needs_review:
        # حدس نمی‌زنیم: متن خام محله دست‌نخورده، منطقه خالی، پرچم بررسی برای ادمین
        print(f"  ⚠️ needs_review «{district}» ({loc.reason})")
    ad_link = f"https://divar.ir/v/{token}"

    lines = [
        f"🏠 {title or 'آگهی جدید'}",
        f"📂 دسته: {cat_label}",
        (f"📍 {city} › ⚠️ نیاز به بررسی › {neighborhood}" if needs_review
         else f"📍 {city} › منطقه {region} › {neighborhood}"),
        "👤 نوع آگهی‌دهنده: مالک (شخصی)",
    ]
    if rows:
        lines.append("")
        lines.append("📋 مشخصات:")
        for k, v in rows.items():
            lines.append(f"{k}: {v}")
    if desc:
        lines.append("")
        lines.append("📝 توضیحات:")
        lines.append(desc)
    lines.append("")
    lines.append(f"🔗 {ad_link}")

    # مطابق createSchema — فقط فیلدهای موجود؛ سرور برای bot اجباراً pending/new/telegram می‌گذارد
    panel_payload = {
        "title": (title or f"آگهی دیوار {token}")[:200],
        "city": city,                 # مشهد
        "region": region,             # ۹ یا ۱۱ — خالی اگر نامشخص (needs_review)
        "neighborhood": neighborhood, # نام دقیق محله پنل — یا متن خام دیوار اگر needs_review
        "category": "new",
        "dealType": deal_type,
        "adLink": ad_link,
        "listingSource": "telegram",
        "status": "pending",
        # ثبت بدون شماره در پنل ممکن نیست → شماره ثابت ربات
        "ownerPhone": config.BOT_OWNER_PHONE,
    }
    if needs_review:
        panel_payload["needs_review"] = True
    if desc:
        panel_payload["description"] = desc[:5000]
    if property_type:
        panel_payload["propertyType"] = property_type
    if price is not None:
        panel_payload["price"] = price
    if rent_price is not None:
        panel_payload["rentPrice"] = rent_price
    if area_m is not None:
        panel_payload["areaMeters"] = area_m
    if rooms is not None:
        panel_payload["rooms"] = rooms
    if year_built is not None:
        panel_payload["yearBuilt"] = year_built
    if floor is not None:
        panel_payload["floor"] = floor
    if units_per_floor is not None:
        panel_payload["unitsPerFloor"] = units_per_floor
    if direction:
        panel_payload["direction"] = direction
    if doc_status:
        panel_payload["docStatus"] = doc_status
    if amenities:
        panel_payload["amenities"] = amenities
    # ownerPhone / familyName / view360 عمداً ارسال نمی‌شوند

    return {
        "post_token": token,
        "title": title,
        "district": district,
        "category": category,
        "message": "\n".join(lines),
        "panel_payload": panel_payload,
    }


# ── ارسال‌ها ──────────────────────────────────────────────────────────

async def send_telegram(bot: Bot, message: str, category: str) -> None:
    topic_id = get_topic_id(category)
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            message_thread_id=topic_id,
            disable_web_page_preview=False,
        )
        print(f"  ✅ تلگرام → تاپیک {topic_id}")
    except Exception as e:
        print(f"  [ERROR] ارسال تلگرام ناموفق: {e}")


def submit_to_panel(payload: dict) -> str:
    """
    POST /admin/mother-properties
    Auth: X-Bot-Token (نقش bot در API)

    Returns one of:
      "ok"        – accepted (200/201)
      "duplicate" – known duplicate link (400/409)
      "error"     – client/validation error (other 4xx)
      "server"    – temporary failure (429 SERVER_BUSY, 5xx, or network)
                     → caller must NOT mark the token as seen
    """
    if not PANEL_ENABLED:
        return "ok"  # panel disabled → treat as success so Telegram still proceeds

    url = f"{NEWAY_API_URL}/admin/mother-properties"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Bot-Token": NEWAY_BOT_TOKEN,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            print(f"  ✅ پنل مادر ← ثبت شد ({payload.get('title', '')[:40]})")
            return "ok"

        body = (resp.text or "")[:500]
        if resp.status_code in (400, 409) and (
            "قبلاً" in body or "duplicate" in body.lower() or "لینک" in body
        ):
            print("  ℹ️ پنل: لینک تکراری — رد شد")
            return "duplicate"

        if resp.status_code == 429 or resp.status_code >= 500:
            print(f"  ⚠️ پنل موقتی ({resp.status_code}) — توکن در seen ذخیره نمی‌شود تا در اجرای بعد دوباره تلاش شود")
            print(f"  متن پاسخ سرور: {body}")
            return "server"

        print(f"  ❌ خطا در ارسال به نیوی! کد: {resp.status_code}")
        print(f"  متن پاسخ سرور: {body}")
        return "error"
    except Exception as e:
        print(f"  [ERROR] ارتباط با پنل مادر (network): {e}")
        return "server"


# ── main ──────────────────────────────────────────────────────────────

async def run_scraper() -> None:
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] شروع اسکرپ دیوار...")
    print(f"  گروه: {CHAT_ID}")
    print(f"  تاپیک فروش: {TOPIC_SELL_ID} | تاپیک اجاره: {TOPIC_RENT_ID}")
    if PANEL_ENABLED:
        print(f"  پنل مادر: فعال → {NEWAY_API_URL}")
        print(f"  endpoint: POST {NEWAY_API_URL}/admin/mother-properties")
    else:
        print("  پنل مادر: غیرفعال (NEWAY_API_URL یا NEWAY_BOT_TOKEN خالی است)")

    if PANEL_ENABLED:
        MAPPER.sync()

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
        stop_category = False
        consecutive_seen = 0

        for page in range(1, MAX_PAGES + 1):
            if stop_category:
                break

            print(f"     صفحه {page}...")
            data = search_divar(cat["category"], page=page, last_post_date=last_post_date)
            if not data:
                print(f"     صفحه {page} خالی یا خطا داشت.")
                break

            last_post_date = data.get("last_post_date") or (data.get("pagination") or {}).get("last_post_date")
            filtered = extract_candidates(data, cat["category"], cat["label"])
            print(f"     → {len(filtered)} آگهی بعد از فیلتر محله/لیست")

            for cand in filtered:
                if cand["token"] in seen:
                    consecutive_seen += 1
                    if consecutive_seen >= 2:
                        print("     به دومین آگهی تکراری رسیدیم؛ توقف این دسته.")
                        stop_category = True
                        break
                else:
                    consecutive_seen = 0
                    all_candidates.append(cand)

            if stop_category:
                break

            widgets = data.get("list_widgets") or []
            post_rows = [w for w in widgets if w.get("widget_type") == "POST_ROW"]
            if not post_rows:
                print("     دیگر آگهی جدیدی در این دسته نیست.")
                break

            # jittered page delay (base 3 s)
            await asyncio.get_event_loop().run_in_executor(None, _jittered_sleep, 3)

        # jittered inter-category delay (base 2 s)
        await asyncio.get_event_loop().run_in_executor(None, _jittered_sleep, 2)

    unique = {c["token"]: c for c in all_candidates}
    candidates = list(unique.values())
    random.shuffle(candidates)
    candidates = candidates[: config.MAX_PER_RUN]
    print(f"  تعداد کل کاندید: {len(candidates)}")

    new_posts = []
    for cand in candidates:
        if cand["token"] in seen:
            continue
        details = fetch_post_details(cand["token"])
        formatted = is_owner_and_format(details, cand)
        if formatted:
            new_posts.append(formatted)
            print(f"  ✅ آگهی جدید: {formatted['title'][:50]}...")
        # jittered detail delay (base DELAY_BETWEEN_POSTS = 8 s)
        await asyncio.get_event_loop().run_in_executor(
            None, _jittered_sleep, config.DELAY_BETWEEN_POSTS
        )

    print(f"  تعداد آگهی جدید برای ارسال: {len(new_posts)}")

    for post in new_posts:
        # اول پنل مادر، بعد تلگرام
        # اگر پنل 5xx/network برگرداند توکن را در seen نمی‌گذاریم تا در اجرای بعد دوباره تلاش شود
        panel_ok = True
        if PANEL_ENABLED and post.get("panel_payload"):
            status = submit_to_panel(post["panel_payload"])
            panel_ok = status != "server"

        if panel_ok:
            seen.add(post["post_token"])

        await send_telegram(bot, post["message"], post["category"])
        await asyncio.sleep(0.5)

    save_last_run(run_started)
    save_seen(seen)
    print(f"  وضعیت ذخیره شد (last_run + {len(seen)} توکن)")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] اسکرپ تمام شد.\n")


if __name__ == "__main__":
    asyncio.run(run_scraper())
