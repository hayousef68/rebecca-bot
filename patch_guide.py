#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""پچ «📚 آموزش اتصال» برای ربات فروش (برنامه‌ی پیشنهادی: Happ).

اجرا:  cd /opt/rebecca-bot && python3 patch_guide.py
فقط وقتی همه‌ی نقطه‌ها پیدا شوند اعمال می‌شود؛ قبلش از shop.py نسخه‌ی shop.py.bak-guide می‌گیرد.
"""
import os
import py_compile
import shutil
import sys

D = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
P = os.path.join(D, "shop.py")

HELPERS = '''# ═════════════════════════════ آموزش اتصال ═════════════════════════════
# لینک‌های رسمی Happ (happ.su). برای عوض‌کردن برنامه یا لینک، فقط همین دیکشنری و متن‌های پایین را ویرایش کنید.
GUIDE_LINKS = {
    "play": "https://play.google.com/store/apps/details?id=com.happproxy",
    "apk": "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk",
    "ios": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "win": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
    "mac": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
}
_GUIDE_COMMON = (
    "\\n<b>مراحل اتصال:</b>\\n"
    "1️⃣ برنامه‌ی Happ را با دکمه‌های پایین نصب کنید.\\n"
    "2️⃣ در همین ربات به «🔄 سرویس‌های من» بروید و <b>لینک اشتراک</b> سرویس را کپی کنید.\\n"
    "3️⃣ Happ را باز کنید و دکمه‌ی <b>➕</b> بالای صفحه را بزنید، بعد «افزودن از کلیپ‌بورد» (Import from clipboard) را انتخاب کنید. "
    "می‌توانید به‌جای کپی لینک، QR سرویس را هم اسکن کنید.\\n"
    "4️⃣ بعد از اضافه‌شدن، یک سرور را انتخاب کنید و دکمه‌ی <b>اتصال</b> را بزنید. اگر گوشی اجازه‌ی VPN خواست، تأیید کنید.\\n"
    "\\n🔄 اگر بعداً سرویس را تمدید کردید، در Happ روی اشتراک بروید و آن را «به‌روزرسانی» کنید.\\n"
    "<i>نام دکمه‌ها ممکن است بسته به نسخه‌ی برنامه کمی فرق کند.</i>"
)
_GUIDE_TXT = {
    "a": "🤖 <b>آموزش اتصال — اندروید</b>\\n\\nبرنامه‌ی پیشنهادی: <b>Happ</b> (رایگان).\\n"
         "اگر Google Play ندارید یا باز نمی‌شود، از دکمه‌ی «دانلود مستقیم (APK)» استفاده کنید.\\n" + _GUIDE_COMMON,
    "i": "🍎 <b>آموزش اتصال — آیفون و آیپد</b>\\n\\nبرنامه‌ی پیشنهادی: <b>Happ</b> (رایگان).\\n"
         "اگر لینک در اپ‌استور شما باز نشد، به پشتیبانی پیام بدهید.\\n" + _GUIDE_COMMON,
    "p": "💻 <b>آموزش اتصال — ویندوز و مک</b>\\n\\nبرنامه‌ی پیشنهادی: <b>Happ</b> (رایگان).\\n"
         "نسخه‌ی ویندوز را مستقیم دانلود و نصب کنید؛ نسخه‌ی مک از اپ‌استور است.\\n" + _GUIDE_COMMON,
}
_GUIDE_BTNS = {
    "a": [("📥 دانلود از Google Play", "play"), ("📦 دانلود مستقیم (APK)", "apk")],
    "i": [("📥 دانلود از App Store", "ios")],
    "p": [("🪟 دانلود برای ویندوز", "win"), ("🍏 دانلود برای مک (App Store)", "mac")],
}


def guide_menu_kb() -> M:
    return M([[B("🤖 اندروید", callback_data="gd:a"), B("🍎 آیفون", callback_data="gd:i")],
              [B("💻 ویندوز / مک", callback_data="gd:p")],
              [B("☎️ پشتیبانی", callback_data="sup")],
              [B("🔙 منوی اصلی", callback_data="home")]])


async def guide_show(update: Update, context, kind: str = "") -> None:
    if kind not in _GUIDE_TXT:
        await show(update, context, "📚 <b>آموزش اتصال</b>\\n\\nبرای استفاده از سرویس به یک برنامه‌ی کلاینت نیاز دارید. "
                                    "ما برنامه‌ی <b>Happ</b> را پیشنهاد می‌کنیم (رایگان، برای اندروید و آیفون).\\n\\nدستگاه خود را انتخاب کنید:", guide_menu_kb())
        return
    rows = [[B(t, url=GUIDE_LINKS[k])] for t, k in _GUIDE_BTNS[kind]]
    rows += [[B("🔄 سرویس‌های من", callback_data="svc")], [B("☎️ پشتیبانی", callback_data="sup")],
             [B("🔙 آموزش اتصال", callback_data="gd"), B("🏠 منوی اصلی", callback_data="home")]]
    await show(update, context, _GUIDE_TXT[kind], M(rows))


def main_kb(admin: bool, uid: int | None = None) -> M:'''

EDITS = [
    ('def main_kb(admin: bool, uid: int | None = None) -> M:', HELPERS, 1),
    ('''            [B("🔗 اتصال سرویس قبلی (با لینک ساب)", callback_data="lnk")],
            [B("☎️ پشتیبانی", callback_data="sup")]]''',
     '''            [B("🔗 اتصال سرویس قبلی (با لینک ساب)", callback_data="lnk")],
            [B("📚 آموزش اتصال", callback_data="gd"), B("☎️ پشتیبانی", callback_data="sup")]]''', 1),
    ('''    elif cmd == "home":
        await home(update, context)
''',
     '''    elif cmd == "home":
        await home(update, context)
    elif cmd == "gd":                                  # آموزش اتصال
        await guide_show(update, context, p[1] if len(p) > 1 else "")
''', 1),
]


def main() -> None:
    if not os.path.isfile(P):
        sys.exit(f"❌ shop.py پیدا نشد (مسیر: {D})")
    s = open(P, encoding="utf-8").read()
    if "def guide_show" in s:
        sys.exit("ℹ️ آموزش اتصال قبلاً اضافه شده؛ تغییری داده نشد.")
    for old, new, count in EDITS:
        n = s.count(old)
        if n != count:
            sys.exit(f"❌ نقطه‌ی پچ پیدا نشد (انتظار {count}، یافت {n}): {old.strip().splitlines()[0][:80]}\nنسخه‌ی شما با نسخه‌ی من فرق دارد؛ هیچ فایلی تغییر نکرد.")
        s = s.replace(old, new)
    shutil.copy2(P, P + ".bak-guide")
    open(P, "w", encoding="utf-8").write(s)
    try:
        py_compile.compile(P, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(P + ".bak-guide", P)
        sys.exit(f"❌ خطای نحوی بعد از پچ؛ فایل برگردانده شد: {e}")
    print("✅ آموزش اتصال اضافه شد (shop.py).")
    print("حالا اجرا کن:  systemctl restart rebecca-bot")


if __name__ == "__main__":
    main()
