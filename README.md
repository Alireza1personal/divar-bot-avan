# divar-bot-avan

ربات دیوار (مشهد) → تلگرام + صف بررسی پنل مادر (Neway)

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | توضیح |
|--------|--------|
| `TELEGRAM_BOT_TOKEN` | توکن ربات تلگرام |
| `CHAT_ID` | آیدی گروه (مثلاً `-100...`) |
| `TOPIC_SELL` | آیدی عددی تاپیک فروش |
| `TOPIC_RENT` | آیدی عددی تاپیک اجاره |
| `NEWAY_API_URL` | پایه API بدون `/` آخر (مثلاً `https://api.example.com`) |
| `NEWAY_BOT_TOKEN` | توکن `nwb_...` از پنل (super_admin → ربات‌ها) |

## اجرا

Actions → Divar Scraper → Run workflow

لاگ موفق:
```
پنل مادر: فعال → https://...
✅ پنل مادر ← ثبت شد
✅ تلگرام → تاپیک ...
```

## API پنل

`POST {NEWAY_API_URL}/admin/mother-properties`  
Header: `X-Bot-Token: nwb_...`
