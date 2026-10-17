# rebecca-bot

ربات تلگرام + وب‌پنل برای ساخت **گروهی** اکانت در پنل ربکا، همراه با لینک ساب و QR Code.

## نصب (تک‌خطی)

```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh)
```

نصب بدون سؤال:

```bash
BOT_TOKEN=123456:ABC... ADMIN_ID=111111,222222 WEB_PORT=8686 bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh)
```

در پایان آدرس وب‌پنل، نام کاربری و رمز تصادفی نمایش داده می‌شود. سپس در تلگرام `/start` بزنید و «اتصال به پنل ربکا» را انجام دهید (یا از وب‌پنل > تنظیمات).

## حذف (تک‌خطی)

```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh) uninstall -y
```

(برای نگه‌داشتن نسخه پشتیبان تنظیمات: `uninstall -y --keep-config`)

## مدیریت

بعد از نصب دستور `rebecca-bot` ساخته می‌شود:

| دستور | کار |
|---|---|
| `rebecca-bot update` | به‌روزرسانی (config حفظ می‌شود) |
| `rebecca-bot restart` | ریستارت سرویس‌ها |
| `rebecca-bot status` / `logs` | وضعیت / لاگ زنده |
| `rebecca-bot passwd` | رمز جدید وب‌پنل |
| `rebecca-bot info` | آدرس و پورت وب‌پنل |
| `rebecca-bot uninstall` | حذف کامل |

## ساخت گروهی

در ربات یا وب‌پنل: نام پایه + عدد شروع + تعداد. مثال: `ali 1 10` یعنی `ali1` تا `ali10`.
QRها در آلبوم‌های ۱۰تایی به تلگرام فرستاده می‌شوند و یک فایل txt از همه لینک‌ها هم ارسال می‌شود.

## امنیت

- رمز وب‌پنل هش‌شده (PBKDF2) در `config.json` با دسترسی 600 ذخیره می‌شود، نه داخل کد.
- وب‌پنل را پشت HTTPS (Nginx/Caddy) بگذارید یا پورت را فقط برای IP خودتان باز کنید.
- هیچ توکن/رمزی را داخل ریپوی عمومی نگذارید.
