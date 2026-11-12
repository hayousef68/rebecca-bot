#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""فروشگاه: ربات فروش جدا برای مشتری‌های ادمین.

- خرید کانفیگ: پرداخت از کیف پول (آنی) یا کارت‌به‌کارت با عکس رسید ← تأیید ادمین با یک دکمه ← ساخت خودکار اکانت
- کیف پول مشتری: شارژ با رسید کارت‌به‌کارت (تأیید ادمین) یا شارژ دستی ادمین
- سرویس‌های من: مصرف، لینک، QR و تمدید (کیف پول یا رسید)
- مدیریت داخل همین ربات (/admin): رسیدها، سفارش‌های ناموفق، مشتری‌ها، پلن‌ها، کارت، متن‌ها

همه‌ی داده‌ها در config.json > shop ذخیره می‌شود (پس بکاپ و بازیابی موجود، فروشگاه را هم شامل می‌شود).
ربات فروش داخل همان پروسه‌ی ربات مدیریت اجرا می‌شود و فقط یک توکن جدا می‌خواهد (دستور /setshop در ربات مدیریت).

حفاظت مالی:
- هر سفارش یک شناسه‌ی یکتا دارد و فقط یک‌بار از «در انتظار» به «در حال انجام» می‌رود (دو ادمین هم‌زمان = فقط یکی موفق می‌شود).
- کسر کیف پول و ساخت سفارش در یک عملیات اتمیک است؛ بازگشت وجه و شارژ با شناسه‌ی تراکنش یکتا (تکرار = بی‌اثر).
- اگر ساخت اکانت قطعاً شکست بخورد، پرداخت با کیف پول خودکار برمی‌گردد؛ اگر نتیجه مبهم باشد (قطع اتصال وسط کار) پولی جابه‌جا نمی‌شود و ادمین «تلاش مجدد» یا «اعتبار به کیف پول» را می‌زند.
- تلاش مجدد ساخت، اگر اکانتِ همان سفارش از قبل ساخته شده باشد، دوباره نمی‌سازد.
- یک عکس رسید فقط برای یک سفارش پذیرفته می‌شود.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import unquote, urlsplit

from telegram import (Bot, BotCommand, BotCommandScopeChat, InlineKeyboardButton as B, InlineKeyboardMarkup as M, MenuButtonCommands,
                      MenuButtonWebApp, ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import common as C
import sales
import tlsutil
import zarinpal as Z
from common import esc

log = logging.getLogger("rebecca.shop")
HTML = ParseMode.HTML

MAX_OPEN = 3                 # حداکثر سفارش باز (منتظر رسید یا منتظر تأیید) برای هر مشتری
STUCK_SECONDS = 600          # «در حال انجام» بیشتر از این = نیمه‌کاره مانده
REMIND_SECONDS = 3 * 3600    # یادآوری رسیدهای بررسی‌نشده به ادمین
AUTO_GRACE = 900             # تأخیر مجاز پیامک بانک بعد از پایان مهلت سفارش خودکار (ثانیه)
AUTO_QUARANTINE = 3 * 3600   # مبلغ یکتای هر سفارش خودکار تا این مدت بعد از ساخت برای سفارش دیگری استفاده نمی‌شود
ZP_GRACE = 600               # بعد از پایان مهلت سفارش پرداخت آنلاین، این‌قدر صبر می‌کنیم تا سفارش لغو شود (پرداخت دیرهنگام)
KIND_FA = {"buy": "خرید کانفیگ", "renew": "تمدید سرویس", "topup": "شارژ کیف پول", "reserve": "رزرو اشتراک"}
STATUS_FA = {"new": "⏳ منتظر رسید", "pending": "🕓 در انتظار تأیید", "processing": "⚙️ در حال انجام", "done": "✅ انجام شد",
             "rejected": "❌ رد شد", "cancelled": "🚫 لغو شد", "failed": "⚠️ نیاز به بررسی ادمین", "refunded": "↩️ مبلغ برگشت داده شد",
             "credited": "💰 به کیف پول اضافه شد"}
REJECT_REASONS = {"a": "رسید نامعتبر یا ناخوانا است", "b": "مبلغ رسید با مبلغ سفارش یکی نیست", "c": "پرداخت در حساب ما دیده نشد"}


# ═════════════════════════════ لایه‌ی داده (همگام، اتمیک روی config.json) ═════════════════════════════
def shop_cfg() -> dict:
    return C.load_config_ro()["shop"]


def money(n) -> str:
    return f"{int(n):,}"


def label(cu: dict | None, uid) -> str:
    cu = cu or {}
    nm = esc(cu.get("name") or "بدون‌نام")
    un = f" @{esc(cu['username'])}" if cu.get("username") else ""
    return f"{nm}{un} (<code>{uid}</code>)"


def customer_get(uid) -> dict | None:
    r = shop_cfg()["customers"].get(str(uid))
    return dict(r) if r else None


def customer_touch(uid, name: str, username: str) -> dict:
    """مشتری را (در صورت نبود) می‌سازد و نام/یوزرنیم را به‌روز نگه می‌دارد."""
    name, username = (name or "")[:60], (username or "")[:40]
    cur = shop_cfg()["customers"].get(str(uid))
    if cur and cur["name"] == name and cur["username"] == username:
        return dict(cur)

    def mut(c):
        r = c["shop"]["customers"].setdefault(str(uid), {"balance": 0, "name": "", "username": "", "joined": int(time.time()),
                                                        "banned": False, "services": [], "log": [], "test": 0, "test_user": ""})
        r["name"], r["username"] = name, username
    return dict(C.update_config(mut)["shop"]["customers"][str(uid)])


def wallet_apply(uid, amount: int, kind: str, note: str = "", txid: str | None = None) -> dict:
    """تغییر اتمیک موجودی مشتری. amount مثبت = شارژ، منفی = کسر. با txid تکراری هیچ تغییری نمی‌دهد (dup=True).
    خروجی: {"balance", "dup"}."""
    out: dict = {}
    amount = int(amount)

    def mut(c):
        s = c["shop"]
        r = s["customers"].get(str(uid))
        if r is None:
            raise ValueError("مشتری پیدا نشد.")
        if txid and any(t["id"] == txid for t in reversed(s["tx"])):
            out.update(balance=r["balance"], dup=True)
            return
        nb = r["balance"] + amount
        if nb < 0:
            raise ValueError("موجودی کافی نیست.")
        if nb > C.SHOP_BAL_MAX:
            raise ValueError("سقف موجودی رد می‌شود.")
        r["balance"] = nb
        r["log"] = (r["log"] + [{"ts": int(time.time()), "type": kind, "amount": amount, "balance": nb, "note": str(note)[:120]}])[-C.SHOP_LOG_MAX:]
        s["tx"].append({"id": txid or "x" + secrets.token_hex(6), "ts": int(time.time()), "uid": int(uid), "kind": kind, "amount": amount, "balance": nb})
        s["tx"] = s["tx"][-C.SHOP_TX_MAX:]
        out.update(balance=nb, dup=False)
    C.update_config(mut)
    if not out.get("dup"):
        C.audit_event("فروشگاه", "تغییر کیف پول مشتری", str(uid), f"{kind}" + (f" — {note}" if note else ""), amount=amount, balance=out["balance"], cat="wallet")
    return out


def _find(s: dict, oid: str) -> dict | None:
    return next((o for o in s["orders"] if o["id"] == oid), None)


def order_get(oid: str) -> dict | None:
    o = _find(shop_cfg(), oid)
    return dict(o) if o else None


def plan_get(s: dict, pid: str) -> dict | None:
    return next((p for p in s["plans"] if p["id"] == pid), None)


_CODE_DEAD = ("cancelled", "rejected", "refunded", "credited")      # سفارش‌هایی که استفاده از کد را «مصرف‌شده» حساب نمی‌کنند


def code_apply(s: dict, uid: int, code: str, plan: dict) -> tuple[str, int]:
    """اعتبارسنجی کد تخفیف روی یک پلن. خروجی: (کد نرمال‌شده, مبلغ تخفیف). خطا: ValueError با پیام فارسی.
    تعداد استفاده از روی خودِ سفارش‌ها شمرده می‌شود (سفارش لغو/ردشده حساب نمی‌شود)؛ پس نیازی به شمارنده‌ی جدا نیست."""
    code = re.sub(r"\s", "", str(code)).upper()
    d = s["codes"].get(code)
    if not d or not d["active"]:
        raise ValueError("کد تخفیف نامعتبر است.")
    if d.get("uid") and int(d["uid"]) != int(uid):                # کد شخصی (مثل کد بازگشت مشتری) فقط برای صاحبش
        raise ValueError("کد تخفیف نامعتبر است.")
    if d["exp"] and time.time() > d["exp"]:
        raise ValueError("مهلت این کد تخفیف تمام شده است.")
    if d["plans"] and plan["id"] not in d["plans"]:
        raise ValueError("این کد برای این پلن قابل استفاده نیست.")
    used = [o for o in s["orders"] if o.get("code") == code and o["status"] not in _CODE_DEAD]
    if d["max"] and len(used) >= d["max"]:
        raise ValueError("ظرفیت این کد تخفیف تمام شده است.")
    if sum(1 for o in used if o["uid"] == uid) >= d["per_user"]:
        raise ValueError("شما قبلاً از این کد استفاده کرده‌اید.")
    price = int(plan["price"])
    off = price * d["value"] // 100 if d["kind"] == "percent" else d["value"]
    off = max(0, min(off, price - min(1000, price)))            # مبلغ نهایی هرگز کمتر از ۱٬۰۰۰ تومان نمی‌شود
    return code, off


def code_check(uid: int, code: str, plan_id: str) -> tuple[str, int, int]:
    """پیش‌نمایش برای مشتری (چیزی ثبت نمی‌شود): (کد, تخفیف, قیمت نهایی)."""
    s = shop_cfg()
    p = plan_get(s, plan_id)
    if not p or not p["active"]:
        raise ValueError("این پلن فعال نیست.")
    c, off = code_apply(s, uid, code, p)
    return c, off, int(p["price"]) - off


def _build_order(c: dict, uid: int, kind: str, plan_id: str = "", amount: int = 0, username: str = "", code: str = "", mode: str = "manual") -> tuple[dict, dict, dict]:
    """اعتبارسنجی و ساخت رکورد سفارش (بدون افزودن به لیست). خروجی: (shop, customer, order)."""
    s = c["shop"]
    cu = s["customers"].get(str(uid))
    if not cu:
        raise ValueError("ابتدا /start را بزنید.")
    if cu["banned"]:
        raise ValueError("دسترسی شما محدود شده است.")
    if kind not in KIND_FA:
        raise ValueError("نوع سفارش نامعتبر است.")
    if sum(1 for o in s["orders"] if o["uid"] == uid and o["status"] in ("new", "pending")) >= MAX_OPEN:
        raise ValueError(f"شما {MAX_OPEN} سفارش باز دارید؛ ابتدا آن‌ها را تکمیل یا لغو کنید.")
    oid = secrets.token_hex(3).upper()
    while any(o["id"] == oid for o in s["orders"]):
        oid = secrets.token_hex(3).upper()
    o = {"id": oid, "uid": int(uid), "kind": kind, "status": "new", "ts": int(time.time()), "method": "card"}
    if kind == "reserve" and not s["reserve"]["enabled"]:
        raise ValueError("رزرو اشتراک فعلاً در دسترس نیست.")
    if kind in ("buy", "renew", "reserve"):
        p = plan_get(s, plan_id)
        if not p or not p["active"]:
            raise ValueError("این پلن فعال نیست.")
        if p["template"] not in c["templates"]:
            raise ValueError("تمپلت این پلن در پنل پیدا نشد؛ به پشتیبانی اطلاع دهید.")
        if p["price"] <= 0:
            raise ValueError("قیمت این پلن تنظیم نشده است.")
        amount = p["price"]
        o.update(plan=p["id"], pname=p["name"], tpl=p["template"])
        if code:
            cc, off = code_apply(s, uid, code, p)             # داخل همان قفلِ ساخت سفارش؛ دو سفارش هم‌زمان نمی‌توانند ظرفیت را رد کنند
            o.update(code=cc, disc=off, orig=int(amount))
            amount = int(amount) - off
    else:
        amount = int(amount)
        if not s["min_topup"] <= amount <= s["max_topup"]:
            raise ValueError(f"مبلغ شارژ باید بین {money(s['min_topup'])} و {money(s['max_topup'])} تومان باشد.")
    if kind in ("renew", "reserve"):
        if not any(x["u"] == username for x in cu["services"]):
            raise ValueError("این سرویس متعلق به شما نیست.")
        if any(x["uid"] == uid and x["kind"] == kind and x.get("username") == username and x["status"] in ("new", "pending", "processing", "failed") for x in s["orders"]):
            raise ValueError(f"برای این سرویس یک سفارش {'تمدید' if kind == 'renew' else 'رزرو'} در جریان دارد.")
        o["username"] = username
    if mode == "zp":
        _zp_prepare(s, o, int(amount))
    elif mode == "auto":
        _auto_prepare(s, o, int(amount), c)
        if kind == "topup":
            amount = o["pay_amount"]               # کیف پول دقیقاً به اندازه‌ی مبلغ واریزی شارژ می‌شود
    o["amount"] = int(amount)
    return s, cu, o


def _rep_taken(c: dict | None, now: int) -> set:
    """مبلغ‌های یکتای درخواست‌های شارژ خودکارِ نماینده‌ها که هنوز باز یا در قرنطینه‌اند (تداخل با سفارش‌های فروشگاه نشود)."""
    return {int(t["pay_amount"]) for t in ((c or {}).get("topups") or [])
            if t.get("mode") == "auto" and t.get("pay_amount") and (t["status"] == "pending" or now - int(t.get("ts") or 0) < AUTO_QUARANTINE)}


def _rep_used(c: dict | None, now: int) -> int:
    """جمع شارژهای خودکارِ نماینده‌ها در ۲۴ ساعت اخیر (در سقف روزانه‌ی تأیید خودکار حساب می‌شود)."""
    return sum(int(t.get("pay_amount") or 0) for t in ((c or {}).get("topups") or []) if t.get("auto_ok") and int(t.get("cts") or 0) > now - 86400)


def rep_topup_auto_new(rid: str, base: int, note: str = "") -> dict:
    """درخواست شارژ خودکار کیف پول نماینده: مبلغ یکتا (۱ تا ۹۹۹ تومان بیشتر) + مهلت. تأیید با پیامک بانک (همان وب‌هوک فروشگاه)."""
    out: dict = {}

    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        s = c["shop"]
        if int(base) < s["min_topup"] or int(base) > s["max_topup"]:
            raise ValueError(f"مبلغ شارژ خودکار باید بین {money(s['min_topup'])} و {money(s['max_topup'])} تومان باشد.")
        if sum(1 for t in c["topups"] if t["rid"] == rid and t["status"] == "pending") >= 5:
            raise ValueError("۵ درخواست در انتظار دارید؛ یکی را لغو کنید یا صبر کنید.")
        o: dict = {}
        _auto_prepare(s, o, int(base), c)
        rec = {"id": "t" + secrets.token_hex(4), "rid": rid, "amount": int(o["pay_amount"]), "note": (str(note).strip() or "شارژ خودکار")[:200],
               "ts": int(time.time()), "status": "pending", "done": 0, "mode": "auto", "base": int(base), "pay_amount": int(o["pay_amount"]),
               "exp": int(o["exp"]), "hold": "", "auto_ok": False, "cts": 0}
        c["topups"].append(rec)
        out.update(rec)
        out["cards"] = [dict(x) for x in s["cards"]]
    C.update_config(mut)
    C.audit_event("نماینده", "درخواست شارژ خودکار", rid, f"مبلغ: {money(out['pay_amount'])} تومان")
    return out


def _auto_prepare(s: dict, o: dict, base: int, c: dict | None = None) -> None:
    """سفارش را «تأیید خودکار» می‌کند: مبلغ یکتا (۱ تا ۹۹۹ تومان بیشتر) و مهلت واریز. داخل قفل ساخت سفارش اجرا می‌شود."""
    a = s["auto"]
    if not a["enabled"]:
        raise ValueError("تأیید خودکار فعلاً در دسترس نیست؛ «کارت‌به‌کارت با رسید» را انتخاب کنید.")
    if not s["cards"]:
        raise ValueError("شماره کارت هنوز توسط ادمین تنظیم نشده است.")
    if base > a["max_amount"]:
        raise ValueError(f"مبلغ بیشتر از سقف تأیید خودکار ({money(a['max_amount'])} تومان) است؛ «کارت‌به‌کارت با رسید» را انتخاب کنید.")
    now = int(time.time())
    taken = {int(x["pay_amount"]) for x in s["orders"]
             if x.get("mode") == "auto" and x.get("pay_amount") and (x["status"] in ("new", "pending", "processing") or now - int(x.get("ts") or 0) < AUTO_QUARANTINE)} | _rep_taken(c, now)
    pool = [d for d in range(1, 1000) if base + d not in taken]
    if not pool:
        raise ValueError("ظرفیت تأیید خودکار برای این مبلغ پر است؛ چند دقیقه بعد دوباره امتحان کنید یا «با رسید» پرداخت کنید.")
    pay = base + secrets.choice(pool)
    used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400) + _rep_used(c, now)
    if used + pay > a["daily_max"]:
        raise ValueError("سقف روزانه‌ی تأیید خودکار پر شده است؛ «کارت‌به‌کارت با رسید» را انتخاب کنید.")
    o.update(mode="auto", base=int(base), pay_amount=int(pay), exp=now + a["minutes"] * 60)


def _zp_prepare(s: dict, o: dict, amount: int) -> None:
    """سفارش را «پرداخت آنلاین زرین‌پال» می‌کند (داخل قفل ساخت سفارش)."""
    z = s["zp"]
    if not Z.shop_ready(z):
        raise ValueError("پرداخت آنلاین فعلاً در دسترس نیست؛ روش دیگری را انتخاب کنید.")
    if amount < Z.MIN_TOMAN:
        raise ValueError(f"حداقل مبلغ پرداخت آنلاین {money(Z.MIN_TOMAN)} تومان است.")
    if amount > z["max_amount"]:
        raise ValueError(f"حداکثر مبلغ پرداخت آنلاین {money(z['max_amount'])} تومان است؛ روش دیگری را انتخاب کنید.")
    o.update(mode="zp", method="zp", exp=int(time.time()) + z["minutes"] * 60)


def _push(s: dict, o: dict) -> None:
    s["orders"].append(o)
    s["orders"] = s["orders"][-C.SHOP_ORDER_MAX:]


def order_new(uid: int, kind: str, plan_id: str = "", amount: int = 0, username: str = "", code: str = "", mode: str = "manual") -> dict:
    """سفارشِ منتظر پرداخت (کارت‌به‌کارت). mode=manual: منتظر رسید و تأیید ادمین — mode=auto: تأیید خودکار با پیامک بانک."""
    out: dict = {}

    def mut(c):
        s, _, o = _build_order(c, uid, kind, plan_id, amount, username, code, mode)
        _push(s, o)
        out.update(o)
    C.update_config(mut)
    return out


def order_pay_wallet(uid: int, kind: str, plan_id: str, username: str = "", code: str = "") -> dict:
    """کسر از کیف پول + ساخت سفارشِ «در حال انجام» در یک عملیات اتمیک (یا هر دو انجام می‌شود یا هیچ‌کدام)."""
    out: dict = {}

    def mut(c):
        s, cu, o = _build_order(c, uid, kind, plan_id, 0, username, code)
        if kind == "topup":
            raise ValueError("شارژ کیف پول با کیف پول ممکن نیست.")
        if cu["balance"] < o["amount"]:
            raise ValueError(f"موجودی کافی نیست. قیمت: {money(o['amount'])} — موجودی شما: {money(cu['balance'])} تومان")
        cu["balance"] -= o["amount"]
        cu["log"] = (cu["log"] + [{"ts": int(time.time()), "type": "purchase", "amount": -o["amount"], "balance": cu["balance"],
                                    "note": f"{KIND_FA[kind]} #{o['id']}"}])[-C.SHOP_LOG_MAX:]
        s["tx"].append({"id": "pay:" + o["id"], "ts": int(time.time()), "uid": int(uid), "kind": "purchase", "amount": -o["amount"], "balance": cu["balance"]})
        s["tx"] = s["tx"][-C.SHOP_TX_MAX:]
        o.update(method="wallet", status="processing", paid=True, cts=int(time.time()))
        _push(s, o)
        out.update(o)
    C.update_config(mut)
    C.audit_event("فروشگاه", "پرداخت با کیف پول مشتری", str(uid), f"{KIND_FA[kind]} #{out['id']}", amount=-out["amount"], cat="wallet")
    return out


def order_update(oid: str, **fields) -> dict | None:
    out: dict = {}

    def mut(c):
        o = _find(c["shop"], oid)
        if o is not None:
            o.update(fields)
            out.update(o)
    C.update_config(mut)
    return out or None


def order_receipt(oid: str, uid: int, file_id: str, fuid: str, text: str) -> dict:
    out: dict = {}

    def mut(c):
        s = c["shop"]
        o = _find(s, oid)
        if not o or o["uid"] != uid:
            raise ValueError("سفارش پیدا نشد.")
        if o["status"] != "new" or o.get("mode") == "zp":
            raise ValueError("این سفارش دیگر منتظر رسید نیست.")
        if fuid and any(x["id"] != oid and x.get("fuid") == fuid and x["status"] != "cancelled" for x in s["orders"]):
            raise ValueError("این عکس قبلاً برای یک سفارش دیگر ثبت شده است. رسید تازه بفرستید.")
        o.update(status="pending", rts=int(time.time()), file=file_id, fuid=fuid, text=(text or "")[:300])
        out.update(o)
    C.update_config(mut)
    return out


def order_cancel(oid: str, uid: int) -> dict:
    out: dict = {}

    def mut(c):
        o = _find(c["shop"], oid)
        if not o or o["uid"] != uid:
            raise ValueError("سفارش پیدا نشد.")
        if o["status"] != "new":
            raise ValueError("فقط سفارشِ منتظر رسید قابل لغو است.")
        o["status"] = "cancelled"
        out.update(o)
    C.update_config(mut)
    return out


def order_claim(oid: str, admin_id: int, allowed: tuple = ("pending",)) -> dict:
    """گذار اتمیک به «در حال انجام». اگر سفارش در وضعیت مجاز نباشد (مثلاً ادمین دیگر زودتر زده) ValueError می‌دهد."""
    out: dict = {}

    def mut(c):
        o = _find(c["shop"], oid)
        if not o:
            raise ValueError("سفارش پیدا نشد.")
        if o["status"] not in allowed:
            raise ValueError(f"این سفارش قبلاً بررسی شده است ({STATUS_FA.get(o['status'], o['status'])}).")
        o.update(status="processing", by=int(admin_id), cts=int(time.time()))
        out.update(o)
    C.update_config(mut)
    return out


def order_reject(oid: str, admin_id: int, reason: str) -> dict:
    out: dict = {}

    def mut(c):
        o = _find(c["shop"], oid)
        if not o:
            raise ValueError("سفارش پیدا نشد.")
        if o["status"] != "pending":
            raise ValueError(f"این سفارش قبلاً بررسی شده است ({STATUS_FA.get(o['status'], o['status'])}).")
        o.update(status="rejected", by=int(admin_id), dts=int(time.time()), reason=reason[:200])
        out.update(o)
    C.update_config(mut)
    return out


def customer_add_service(uid: int, pid: str, username: str) -> None:
    def mut(c):
        r = c["shop"]["customers"].get(str(uid))
        if r is not None and not any(x["u"] == username and x["pid"] == pid for x in r["services"]):
            r["services"].append({"pid": pid, "u": username, "ts": int(time.time())})
    C.update_config(mut)


# ═════════════════════════════ اتصال سرویس قبلی با لینک ساب ═════════════════════════════
MENU_BTN = "🏠 منوی اصلی"
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_tok_cache: dict = {"ts": 0.0, "map": {}}


def sub_token(link: str) -> str:
    """توکن ساب از لینک (…/sub/TOKEN یا …/sub/TOKEN/v2ray و …)."""
    path = unquote(urlsplit(link.strip()).path or "")
    parts = [x for x in path.split("/") if x]
    if "sub" in parts and parts.index("sub") + 1 < len(parts):
        return parts[parts.index("sub") + 1]
    return parts[-1] if parts else ""


def _token_username(token: str) -> str | None:
    """نام کاربری داخل توکن ساب (فرمت مرزبان/ربکا: base64(username,ts)+امضا یا JWT). فقط حدس؛ اعتبار با مقایسه‌ی توکن در پنل بررسی می‌شود."""
    def b64(x: str) -> bytes:
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    try:
        if token.count(".") == 2:
            sub = json.loads(b64(token.split(".")[1])).get("sub") or json.loads(b64(token.split(".")[1])).get("username")
            return str(sub) if sub else None
    except Exception:  # noqa: BLE001
        pass
    try:
        name = b64(token[:-10]).decode("utf-8").split(",")[0]
        return name or None
    except Exception:  # noqa: BLE001
        return None


async def _scan_token(token: str) -> tuple[str, str, dict] | None:
    """راه جایگزین: فهرست کاربران هر پنل را می‌خواند و توکن را تطبیق می‌دهد (نتیجه ۵ دقیقه کش می‌شود)."""
    now = time.time()
    if now - _tok_cache["ts"] > 300:
        m: dict = {}
        for pn in C.load_config_ro()["panels"]:
            api = C.get_api(pn["id"])
            if not api.is_configured():
                continue
            try:
                users = await asyncio.wait_for(api.iter_users(), 90)
            except Exception:  # noqa: BLE001
                continue
            for u in users:
                t = sub_token(u.get("subscription_url") or "")
                if t:
                    m[t] = (pn["id"], u.get("username"), u)
        _tok_cache.update(ts=now, map=m)
    hit = _tok_cache["map"].get(token)
    return (hit[0], hit[1], hit[2]) if hit else None


async def find_service_by_link(link: str) -> tuple[str, str, dict] | None:
    """لینک ساب ← (panel_id, username, user). فقط وقتی توکنِ لینک دقیقاً با توکن کاربر در پنل یکی باشد (اثبات مالکیت)."""
    token = sub_token(link)
    if len(token) < 8:
        return None
    guess = _token_username(token)
    cfg = C.load_config_ro()
    if guess:
        for pn in cfg["panels"]:
            api = C.get_api(pn["id"])
            if not api.is_configured():
                continue
            try:
                u = await asyncio.wait_for(api.request("GET", api.upath(guess)), 20)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(u, dict) and sub_token(u.get("subscription_url") or "") == token:
                return pn["id"], u.get("username") or guess, u
    return await _scan_token(token)


def customer_link_service(uid: int, pid: str, username: str) -> str:
    """سرویس را به مشتری وصل می‌کند. خروجی: ok | dup (قبلاً وصل بوده) | taken (متعلق به مشتری دیگر)."""
    out = {"r": "ok"}

    def mut(c):
        cs = c["shop"]["customers"]
        me = cs.get(str(uid))
        if me is None:
            raise ValueError("ابتدا /start را بزنید.")
        if any(x["u"] == username and (x["pid"] == pid or not x["pid"]) for x in me["services"]):
            out["r"] = "dup"
            return
        if any(k != str(uid) and any(x["u"] == username and (x["pid"] == pid or not x["pid"]) for x in r["services"]) for k, r in cs.items()):
            out["r"] = "taken"
            return
        if len(me["services"]) >= 50:
            raise ValueError("سقف تعداد سرویس‌ها پر است.")
        me["services"].append({"pid": pid, "u": username, "ts": int(time.time())})
    C.update_config(mut)
    if out["r"] == "ok":
        C.audit_event("فروشگاه", "اتصال سرویس با لینک ساب", str(uid), username, cat="user")
    return out["r"]


async def handle_link(update: Update, context, text: str) -> bool:
    """اگر پیام شامل لینک ساب معتبر بود سرویس را وصل می‌کند و True برمی‌گرداند."""
    m = update.effective_message
    links = _URL_RE.findall(text)
    if not links:
        return False
    wait = await m.reply_html("⏳ در حال بررسی لینک…")
    u = update.effective_user
    results, found = [], 0
    for link in links[:3]:
        try:
            hit = await find_service_by_link(link)
        except Exception:  # noqa: BLE001
            log.exception("link lookup")
            hit = None
        if not hit:
            results.append("❌ این لینک در سیستم پیدا نشد. لینک ساب کامل را بفرستید.")
            continue
        pid, uname, _ = hit
        try:
            r = await asyncio.to_thread(customer_link_service, u.id, pid, uname)
        except ValueError as e:
            results.append(f"❌ {esc(e)}")
            continue
        if r == "taken":
            results.append(f"⛔ سرویس <b>{esc(uname)}</b> قبلاً به حساب دیگری وصل شده است؛ به پشتیبانی پیام بدهید.")
        elif r == "dup":
            results.append(f"ℹ️ سرویس <b>{esc(uname)}</b> از قبل در لیست شماست.")
            found += 1
        else:
            results.append(f"✅ سرویس <b>{esc(uname)}</b> وصل شد.")
            found += 1
    context.user_data.pop("aw", None)
    kb = back_kb([[B("🔄 سرویس‌های من / تمدید", callback_data="svc")]]) if found else back_kb()
    try:
        await wait.delete()
    except TelegramError:
        pass
    await m.reply_html("\n".join(results) + ("\n\nحالا از «سرویس‌های من» می‌توانید تمدید کنید." if found else ""), reply_markup=kb)
    return True


# ═════════════════════════════ عضویت اجباری در کانال ═════════════════════════════
_member_ok: dict[int, float] = {}          # uid ← زمان آخرین تأیید عضویت (کش ۶۰ ثانیه‌ای؛ جلوگیری از درخواست زیاد به تلگرام)
_force_warned: dict[str, float] = {}


def force_channels() -> list[dict]:
    f = shop_cfg()["force"]
    return f["channels"] if f["enabled"] else []


def _chan_url(ch: dict) -> str:
    if ch.get("link"):
        return ch["link"]
    return f"https://t.me/{ch['chat'][1:]}" if ch["chat"].startswith("@") else ""


_trust: dict[int, float] = {}               # کاربرهایی که وقتی بررسی ممکن نبود «عضو شدم» زدند (۲۴ ساعت اعتماد)


async def missing_channels(bot: Bot, uid: int) -> list[dict]:
    """کانال‌هایی که کاربر عضو نیست. اگر ربات نتواند بررسی کند (ادمین کانال نیست…)، پیام عضویت باز هم نشان داده می‌شود
    (فروشگاه قفل نمی‌ماند: با زدن «عضو شدم» ۲۴ ساعت اجازه می‌گیرد) و علت دقیق به ادمین‌ها گزارش می‌شود."""
    chans = force_channels()
    if not chans or C.is_admin(uid):
        return []
    if time.time() - _member_ok.get(uid, 0) < 60:
        return []
    miss = []
    for ch in chans:
        chat = int(ch["chat"]) if ch["chat"].lstrip("-").isdigit() else ch["chat"]
        try:
            m = await asyncio.wait_for(bot.get_chat_member(chat, uid), 10)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True)):
                miss.append(ch)
        except Exception as e:  # noqa: BLE001
            log.warning("force-join check %s: %s", ch["chat"], e)
            if time.time() - _force_warned.get(ch["chat"], 0) > 3600:
                _force_warned[ch["chat"]] = time.time()
                await _notify_admins(bot, f"⚠️ بررسی عضویت اجباری در {esc(ch['chat'])} ممکن نشد:\n<code>{esc(str(e)[:200])}</code>\nربات فروش را <b>ادمین همان کانال</b> کنید و یوزرنیم/شناسه کانال را چک کنید. تا آن موقع به مشتری فقط پیام عضویت نشان داده می‌شود و با «✅ عضو شدم» رد می‌شود.")
            if time.time() - _trust.get(uid, 0) > 86400:
                miss.append(ch)
    if not miss:
        _member_ok[uid] = time.time()
        if len(_member_ok) > 5000:
            _member_ok.clear()
    return miss


async def force_join_ok(update: Update, context) -> bool:
    """False یعنی کاربر عضو نیست و پیام عضویت نشان داده شد."""
    u = update.effective_user
    miss = await missing_channels(context.bot, u.id)
    if not miss:
        return True
    rows = [[B(f"📢 عضویت در {ch['chat'] if ch['chat'].startswith('@') else 'کانال'}", url=_chan_url(ch))] for ch in miss if _chan_url(ch)]
    rows.append([B("✅ عضو شدم", callback_data="chk")])
    text = "🔒 <b>قبل از استفاده از ربات، در کانال زیر عضو شوید.</b>\n\nبعد از عضویت، دکمه‌ی «✅ عضو شدم» را بزنید تا منو باز شود."
    q = update.callback_query
    if q:
        await _ans(q, "ابتدا در کانال عضو شوید.", show_alert=True)
    await show(update, context, text, M(rows))
    return False


# ═════════════════════════════ تست رایگان ═════════════════════════════
def test_available(cfg: dict | None = None) -> bool:
    cfg = cfg or C.load_config_ro()
    t = cfg["shop"]["test"]
    return bool(t["enabled"] and t["template"] in cfg["templates"])


def _test_claim(uid: int, grant: bool) -> str:
    """grant=True: تست را اتمیک «مصرف‌شده» می‌کند. خروجی: ok | used | off. grant=False: برگرداندن (وقتی ساخت قطعاً شکست خورد)."""
    out = {"r": "ok"}

    def mut(c):
        cu = c["shop"]["customers"].get(str(uid))
        if cu is None:
            raise ValueError("ابتدا /start را بزنید.")
        if not grant:
            cu["test"] = 0
            return
        t = c["shop"]["test"]
        if not (t["enabled"] and t["template"] in c["templates"]):
            out["r"] = "off"
        elif cu["test"]:
            out["r"] = "used"
        else:
            cu["test"] = int(time.time())
    C.update_config(mut)
    return out["r"]


async def _create_test(uid: int) -> dict:
    cfg = C.load_config()
    t = cfg["shop"]["test"]
    tpl = dict(cfg["templates"][t["template"]])
    tpl["hours"] = int(t.get("hours") or 24)              # مدت تست بر حسب ساعت (از تنظیمات)؛ مدت تمپلت نادیده گرفته می‌شود
    pid = shop_panel(cfg)
    api = C.get_api(pid)
    if not api.is_configured():
        raise FulfilError("پنل در دسترس نیست.", True)
    try:
        services = await api.list_services()
    except C.RebeccaError as e:
        raise FulfilError(str(e), True) from e
    svc_id = C.pick_default_service(services, tpl.get("kind") == "group")
    if svc_id is None:
        raise FulfilError("سرویس مناسب در پنل پیدا نشد.", True)
    svc_name = next((x["name"] for x in services if x["id"] == svc_id), str(svc_id))
    # نام تست کدِ تصادفی است (مثل test_k7x2q9) و از شماره‌گذاری اشتراک‌های پنل جداست
    results, failed, name = None, [], ""
    for _ in range(5):
        name = f"{t['prefix']}_" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
        try:
            results, failed = await C.create_users(api, [name], tpl, svc_id, concurrency=1)
        except Exception:  # noqa: BLE001
            log.exception("shop test create")
            results, failed = [], [(name, "خطای پنل")]
        if results or not failed or "قبل" not in failed[0][1]:
            break
    if not results:
        msg = failed[0][1] if failed else "نامشخص"
        try:
            u = await api.request("GET", api.upath(name))
        except C.RebeccaError as e:
            raise FulfilError(msg, e.status == 404) from e
        except Exception as e:  # noqa: BLE001
            raise FulfilError(msg, False) from e
        if isinstance(u, dict) and u.get("username"):
            results = [{"username": name, "sub": api.full_sub(u.get("subscription_url", ""))}]
        else:
            raise FulfilError(msg, False)
    uname = results[0]["username"]
    await asyncio.to_thread(customer_add_service, uid, pid, uname)
    await asyncio.to_thread(_test_set_user, uid, uname, pid, int(time.time()) + tpl["hours"] * 3600)
    return {"results": results, "header": C.build_info(api.name, tpl, svc_name), "username": uname, "tpl": tpl}


def _hours_fa(h: int) -> str:
    return f"{h // 24} روز" if h % 24 == 0 and h >= 24 else f"{h} ساعت"


async def cleanup_tests(bot: Bot) -> None:
    """تست‌های تمام‌شده را از پنل (دیتابیس) پاک می‌کند و از لیست مشتری برمی‌دارد.
    اگر مشتری قبل از پایان تست تمدید کرده باشد (انقضای پنل جلوتر است) حذف نمی‌شود. شکست موقت = دفعه‌ی بعد دوباره."""
    now = int(time.time())
    due = [(uid, dict(cu)) for uid, cu in shop_cfg()["customers"].items() if cu.get("test_user") and cu.get("test_exp") and not cu.get("test_del") and now > cu["test_exp"] + 120]
    for uid, cu in due:
        uname = cu["test_user"]
        cfg = C.load_config_ro()
        pid = cu["test_pid"] if cu["test_pid"] and C.get_panel(cfg, cu["test_pid"]) else shop_panel(cfg)
        api = C.get_api(pid)
        if not api.is_configured():
            continue
        state = "deleted"
        try:
            u = await asyncio.wait_for(api.request("GET", api.upath(uname)), 20)
            exp = int((u or {}).get("expire") or 0) if isinstance(u, dict) else 0
            if exp > now + 300 or (isinstance(u, dict) and u.get("status") == "on_hold"):
                state = "kept"                                   # تمدید شده؛ دیگر تست نیست
            else:
                await asyncio.wait_for(api.request("DELETE", api.upath(uname)), 30)
        except C.RebeccaError as e:
            if e.status != 404:
                log.warning("test cleanup %s: %s", uname, e)
                continue
        except Exception as e:  # noqa: BLE001
            log.warning("test cleanup %s: %s", uname, e)
            continue

        def mut(c, uid=uid, uname=uname, state=state):
            r = c["shop"]["customers"].get(uid)
            if r is not None:
                r["test_del"] = 1 if state == "deleted" else 2
                if state == "deleted":
                    r["services"] = [x for x in r["services"] if x["u"] != uname]
        await asyncio.to_thread(C.update_config, mut)
        if state == "deleted":
            C.audit_event("فروشگاه", "حذف خودکار تست رایگان", str(uid), uname, cat="del")
            await _tell(bot, int(uid), "⏰ زمان تست رایگان شما تمام شد و حذف شد.\nبرای ادامه از «🛒 خرید کانفیگ» استفاده کنید.")


def _test_set_user(uid: int, uname: str, pid: str = "", exp: int = 0) -> None:
    def mut(c):
        cu = c["shop"]["customers"].get(str(uid))
        if cu is not None:
            cu["test_user"], cu["test_pid"], cu["test_exp"], cu["test_del"] = uname, pid, exp, 0
    C.update_config(mut)


async def give_test(update: Update, context) -> None:
    """مشتری «تست رایگان» را زد: یک‌بار برای هر مشتری، اتمیک (دوبار زدن = فقط یک اکانت)."""
    u = update.effective_user
    cfg = C.load_config_ro()
    if not test_available(cfg):
        await show(update, context, "🎁 تست رایگان فعلاً فعال نیست.", back_kb())
        return
    r = await asyncio.to_thread(_test_claim, u.id, True)
    if r == "used":
        await show(update, context, "🎁 شما قبلاً تست رایگان گرفته‌اید (هر نفر فقط یک‌بار).\nبرای ادامه از «🛒 خرید کانفیگ» استفاده کنید.", back_kb())
        return
    if r == "off":
        await show(update, context, "🎁 تست رایگان فعلاً فعال نیست.", back_kb())
        return
    await show(update, context, "⏳ در حال ساخت تست رایگان شما…", None)
    try:
        res = await _create_test(u.id)
    except FulfilError as e:
        if e.clean:
            await asyncio.to_thread(_test_claim, u.id, False)       # قطعاً چیزی ساخته نشده؛ حق تست برمی‌گردد
            await show(update, context, "❌ ساخت تست الان ممکن نشد؛ کمی بعد دوباره امتحان کنید.", back_kb())
        else:
            await show(update, context, "⚠️ ساخت تست با مشکل روبه‌رو شد؛ پشتیبانی بررسی می‌کند.", back_kb())
            await _notify_admins(context.bot, f"⚠️ ساخت تست رایگان برای {label(customer_get(u.id), u.id)} نتیجه‌ی مبهم داشت ({esc(e)}). در پنل بررسی کنید؛ در صورت نیاز در وب‌پنل «بازنشانی تست» بزنید.")
        return
    except Exception:  # noqa: BLE001
        log.exception("give_test")
        await show(update, context, "⚠️ خطای غیرمنتظره؛ پشتیبانی بررسی می‌کند.", back_kb())
        await _notify_admins(context.bot, f"⚠️ ساخت تست رایگان برای {label(customer_get(u.id), u.id)} با خطا مواجه شد.")
        return
    try:
        await C.send_results(context.bot, u.id, res["results"], res["header"], cfg["max_qr"], True)
        await context.bot.send_message(u.id, f"🎁 تست رایگان شما آماده است.\n⏳ اعتبار: {_hours_fa(cfg['shop']['test']['hours'])} (بعد از آن خودکار حذف می‌شود؛ اگر قبل از پایان از «🔄 سرویس‌های من» تمدیدش کنید حذف نمی‌شود).\nلینک بالا را در برنامه‌ی خود وارد کنید.",
                                       reply_markup=back_kb())
    except Exception:  # noqa: BLE001
        log.exception("send test result")
        _hu = C.happ_page_url(res["results"][0]["sub"])
        await _tell(context.bot, u.id, f"🎁 <b>تست رایگان شما</b>\n\n<code>{esc(res['results'][0]['sub'])}</code>", back_kb([[B("📲 افزودن به Happ", url=_hu)]] if _hu else None))
    await _notify_admins(context.bot, f"🎁 تست رایگان: <code>{esc(res['username'])}</code> برای {label(customer_get(u.id), u.id)}")


# ═════════════════════════════ انجام سفارش (ساخت اکانت / تمدید / شارژ) ═════════════════════════════
class FulfilError(Exception):
    """clean=True یعنی مطمئنیم هیچ تغییری در پنل انجام نشده (برگشت وجه خودکار امن است)."""

    def __init__(self, msg: str, clean: bool = False):
        super().__init__(msg)
        self.clean = clean


_create_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _create_lock
    if _create_lock is None:
        _create_lock = asyncio.Lock()
    return _create_lock


def shop_panel(cfg: dict) -> str:
    pid = cfg["shop"].get("panel") or ""
    return pid if pid and C.get_panel(cfg, pid) else cfg["active_panel"]


async def _do_buy(oid: str) -> dict:
    o = order_get(oid)
    cfg = C.load_config()
    tpl = cfg["templates"].get(o.get("tpl", ""))
    if not tpl:
        raise FulfilError("تمپلت این پلن دیگر وجود ندارد.", True)
    tpl = dict(tpl)
    pid = o.get("pid") or shop_panel(cfg)
    api = C.get_api(pid)
    if not api.is_configured():
        raise FulfilError("پنل در دسترس نیست.", True)
    try:
        services = await api.list_services()
    except C.RebeccaError as e:
        raise FulfilError(str(e), True) from e
    svc_id = C.pick_default_service(services, tpl.get("kind") == "group")
    if svc_id is None:
        raise FulfilError("سرویس مناسب در پنل پیدا نشد.", True)
    svc_name = next((s["name"] for s in services if s["id"] == svc_id), str(svc_id))
    prev_name = o.get("username") or ""      # اگر قبلاً نامی برای همین سفارش رزرو شده، یعنی این تلاش مجدد است
    results = None
    async with _lock():
        name = prev_name
        if not name:
            prefix = cfg["default_prefix"]
            nx = await sales.next_number(api, prefix, len(cfg["panels"]) <= 1)
            name = C.make_names(prefix, int(nx.get("next") or 1), 1, 1)[0]
            order_update(oid, username=name, pid=pid)
        else:                                 # تلاش مجدد: شاید اکانت همان سفارش قبلاً ساخته شده باشد
            try:
                u = await api.request("GET", api.upath(name))
                if isinstance(u, dict) and u.get("username"):
                    results = [{"username": name, "sub": api.full_sub(u.get("subscription_url", ""))}]
            except C.RebeccaError as e:
                if e.status != 404:
                    raise FulfilError(f"بررسی پنل ناموفق بود: {e}", False) from e
        failed: list = []
        if results is None:
            for _ in range(4):                # اگر هم‌زمان کسی همان شماره را گرفت، شماره‌ی بعدی
                try:
                    results, failed = await C.create_users(api, [name], tpl, svc_id, concurrency=1)
                except Exception:  # noqa: BLE001
                    log.exception("shop create")
                    results, failed = [], [(name, "خطای پنل")]
                if results or not failed or "قبل" not in failed[0][1]:
                    break
                m = re.fullmatch(r"(.*?)(\d+)", name)
                if not m:
                    break
                name = C.make_names(m.group(1), int(m.group(2)) + 1, 1, 1)[0]
                order_update(oid, username=name)
            if not results:
                msg = failed[0][1] if failed else "نامشخص"
                try:                          # شکست مبهم (مثلاً timeout): شاید پنل اکانت را ساخته باشد
                    u = await api.request("GET", api.upath(name))
                except C.RebeccaError as e:
                    raise FulfilError(msg, e.status == 404) from e
                except Exception as e:  # noqa: BLE001
                    raise FulfilError(msg, False) from e
                if isinstance(u, dict) and u.get("username"):
                    results = [{"username": name, "sub": api.full_sub(u.get("subscription_url", ""))}]
                else:
                    raise FulfilError(msg, False)
    uname = results[0]["username"]
    await asyncio.to_thread(customer_add_service, o["uid"], pid, uname)
    return {"results": results, "header": C.build_info(api.name, tpl, svc_name), "username": uname, "tpl": tpl}


async def _do_renew(oid: str) -> dict:
    o = order_get(oid)
    cfg = C.load_config()
    cu = cfg["shop"]["customers"].get(str(o["uid"]))
    sv = next((x for x in (cu or {}).get("services", []) if x["u"] == o.get("username")), None)
    if not sv:
        raise FulfilError("این سرویس در لیست مشتری نیست.", True)
    tpl = cfg["templates"].get(o.get("tpl", ""))
    if not tpl:
        raise FulfilError("تمپلت این پلن دیگر وجود ندارد.", True)
    pid = sv["pid"] if sv["pid"] and C.get_panel(cfg, sv["pid"]) else cfg["active_panel"]
    api = C.get_api(pid)
    if not api.is_configured():
        raise FulfilError("پنل در دسترس نیست.", True)
    try:
        await C.renew_user(api, sv["u"], dict(tpl), reset_usage=True)
    except C.RebeccaError as e:
        raise FulfilError(str(e), e.status == 404) from e
    except Exception as e:  # noqa: BLE001
        raise FulfilError(f"{e.__class__.__name__}", False) from e
    return {"username": sv["u"], "tpl": dict(tpl)}


async def _do_reserve(oid: str) -> dict:
    """رزرو اشتراک = افزودن یک «پلن بعدی» به سرویس مشتری؛ وقتی حجم یا زمان فعلی تمام شد، پنل خودش آن را شروع می‌کند.
    تلاش مجدد بعد از شکستِ مبهم دوباره اضافه نمی‌کند (تعداد پلن‌های صف قبل از نوشتن در سفارش ثبت می‌شود)."""
    o = order_get(oid)
    cfg = C.load_config()
    cu = cfg["shop"]["customers"].get(str(o["uid"]))
    sv = next((x for x in (cu or {}).get("services", []) if x["u"] == o.get("username")), None)
    if not sv:
        raise FulfilError("این سرویس در لیست مشتری نیست.", True)
    tpl = cfg["templates"].get(o.get("tpl", ""))
    if not tpl:
        raise FulfilError("تمپلت این پلن دیگر وجود ندارد.", True)
    gb, days = float(tpl.get("gb") or 0), int(tpl.get("days") or 0)
    if gb <= 0 and days <= 0:
        raise FulfilError("این پلن حجم یا مدت ندارد و برای رزرو مناسب نیست.", True)
    pid = sv["pid"] if sv["pid"] and C.get_panel(cfg, sv["pid"]) else cfg["active_panel"]
    api = C.get_api(pid)
    if not api.is_configured():
        raise FulfilError("پنل در دسترس نیست.", True)
    ph = api.upath(sv["u"])
    try:
        before = await api.request("GET", ph)
    except C.RebeccaError as e:
        raise FulfilError(str(e), e.status == 404) from e
    except Exception as e:  # noqa: BLE001
        raise FulfilError(e.__class__.__name__, False) from e
    raw = C._raw_plans(before)
    if o.get("rsv_n") is not None and len(raw) > int(o["rsv_n"]):          # تلاش قبلی در واقع انجام شده بود
        return {"username": sv["u"], "tpl": dict(tpl), "queue": len(raw)}
    view = C.next_plans_view(before)
    mx = cfg["shop"]["reserve"]["max"]
    if len(view) >= mx:
        raise FulfilError(f"برای این سرویس از قبل {len(view)} اشتراک رزرو شده است (حداکثر {mx}).", True)
    if any(x.get("broken") for x in view):
        raise FulfilError("اشتراک‌های رزروشده‌ی قبلیِ این سرویس نیاز به بررسی ادمین دارد.", True)
    plans = [{"idx": x["idx"], "gb": x["gb"], "days": x["days"], "add_remaining": x["add_remaining"]} for x in view]
    plans.append({"idx": None, "gb": gb, "days": days, "add_remaining": False})
    await asyncio.to_thread(order_update, oid, rsv_n=len(raw))
    try:
        after = await C.set_next_plans(api, sv["u"], plans, None)
    except Exception as e:  # noqa: BLE001
        log.exception("shop reserve %s", oid)
        try:                                   # شکست مبهم: ببین رزرو واقعاً روی پنل اعمال شده یا نه
            cur = await api.request("GET", ph)
        except Exception:  # noqa: BLE001
            raise FulfilError(e.__class__.__name__, False) from e
        if len(C._raw_plans(cur)) > len(raw):
            after = cur
        else:
            raise FulfilError(str(e) if isinstance(e, C.RebeccaError) else e.__class__.__name__, True) from e
    try:
        await asyncio.to_thread(sales.record_attach, sv["u"], after.get("service_id"), gb, days, None, "next")
        await asyncio.to_thread(C.hist_add_many, [{"pid": getattr(api, "pid", ""), "u": sv["u"], "k": "next", "gb": gb, "days": days}])
    except Exception:  # noqa: BLE001
        log.exception("record reserve")
    return {"username": sv["u"], "tpl": dict(tpl), "queue": len(C._raw_plans(after))}


def _plan_line(tpl: dict) -> str:
    gb = "نامحدود" if not tpl.get("gb") else f"{C.fmt_gb(tpl['gb'] * C.GB)}"
    days = "نامحدود" if not tpl.get("days") else f"{tpl['days']} روز"
    return f"{gb} / {days}"


async def fulfil(bot: Bot, oid: str) -> None:
    """سفارشِ «در حال انجام» را انجام می‌دهد و نتیجه را به مشتری و ادمین‌ها اطلاع می‌دهد. خطایش هیچ‌وقت بالا نمی‌رود."""
    o = order_get(oid)
    if not o or o["status"] != "processing":
        return
    uid = o["uid"]
    try:
        if o["kind"] == "topup":
            w = await asyncio.to_thread(wallet_apply, uid, o["amount"], "topup", f"شارژ {'آنلاین' if o.get('mode') == 'zp' else 'با رسید'} #{oid}", "topup:" + oid)
            await asyncio.to_thread(order_update, oid, status="done", dts=int(time.time()), result=f"موجودی: {money(w['balance'])}")
            await _tell(bot, uid, f"✅ <b>کیف پول شما شارژ شد</b>\nمبلغ: <b>{money(o['amount'])}</b> تومان\nموجودی فعلی: <b>{money(w['balance'])}</b> تومان")
            await _after(bot, oid, f"✅ شارژ انجام شد — موجودی مشتری: {money(w['balance'])}")
            return
        res = await {"buy": _do_buy, "renew": _do_renew, "reserve": _do_reserve}[o["kind"]](oid)
    except FulfilError as e:
        await _on_fail(bot, oid, str(e), e.clean)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("fulfil %s", oid)
        await _on_fail(bot, oid, f"خطای غیرمنتظره: {e.__class__.__name__}", False)
        return
    await asyncio.to_thread(order_update, oid, status="done", dts=int(time.time()), result=res["username"])
    cfg = C.load_config_ro()
    try:
        if o["kind"] == "buy":
            await C.send_results(bot, uid, res["results"], res["header"], cfg["max_qr"], True)
            await _tell(bot, uid, "🎉 خرید شما کامل شد. لینک بالا را در برنامه‌ی خود وارد کنید.\nسرویس‌ها در بخش «🔄 سرویس‌های من» هستند.")
        elif o["kind"] == "reserve":
            await _tell(bot, uid, f"✅ <b>اشتراک بعدی برای {esc(res['username'])} رزرو شد</b>\n📦 {esc(_plan_line(res['tpl']))}\n\n"
                                  f"بعد از تمام شدن حجم یا زمان فعلی، این اشتراک خودکار شروع می‌شود؛ لازم نیست کاری بکنید.")
        else:
            await _tell(bot, uid, f"✅ <b>سرویس {esc(res['username'])} تمدید شد</b>\n📦 {esc(_plan_line(res['tpl']))}")
    except Exception:  # noqa: BLE001
        log.exception("send result to customer %s", uid)
        sent = False
        if o["kind"] == "buy":                 # اگر QR/آلبوم نرفت (مثلاً خطای ساخت تصویر)، حداقل لینک به‌صورت متن
            links = "\n\n".join(f"👤 <b>{esc(r['username'])}</b>\n<code>{esc(r['sub'])}</code>" for r in res["results"])
            sent = await _tell(bot, uid, f"✅ <b>خرید شما انجام شد</b>\n\n{links}", C.happ_kb(res["results"][0]["sub"]) if len(res["results"]) == 1 else None)
        if not sent:
            await _notify_admins(bot, f"⚠️ سفارش #{oid} انجام شد (<code>{esc(res['username'])}</code>) ولی ارسال به مشتری ناموفق بود؛ لینک را از ربات مدیریت برای او بفرستید.")
    await _after(bot, oid, f"✅ انجام شد — <code>{esc(res['username'])}</code>")


async def _on_fail(bot: Bot, oid: str, why: str, clean: bool) -> None:
    o = order_get(oid)
    uid = o["uid"]
    if clean and o["method"] == "wallet":
        w = await asyncio.to_thread(wallet_apply, uid, o["amount"], "refund", f"بازگشت وجه #{oid}", "refund:" + oid)
        await asyncio.to_thread(order_update, oid, status="refunded", dts=int(time.time()), err=why[:200])
        await _tell(bot, uid, f"❌ <b>{KIND_FA[o['kind']]} انجام نشد</b> و مبلغ به کیف پول شما برگشت.\nموجودی: <b>{money(w['balance'])}</b> تومان\nدلیل: {esc(why)}")
        await _notify_admins(bot, f"↩️ سفارش #{oid} ناموفق بود و مبلغ به کیف پول مشتری برگشت.\nدلیل: {esc(why)}")
        return
    await asyncio.to_thread(order_update, oid, status="failed", err=why[:200], unsure=not clean)
    if o["method"] == "wallet":
        await _tell(bot, uid, f"⚠️ سفارش #{oid} با مشکل روبه‌رو شد و ادمین آن را بررسی می‌کند. مبلغ شما حفظ شده و از بین نمی‌رود.")
    else:
        await _tell(bot, uid, f"⚠️ رسید شما تأیید شد ولی انجام سفارش #{oid} با مشکل روبه‌رو شد. ادمین موضوع را پیگیری می‌کند؛ نیازی به پرداخت دوباره نیست.")
    await _after(bot, oid, f"⚠️ ناموفق: {esc(why)}" + (" (نتیجه مبهم؛ اول وضعیت پنل را بررسی کنید)" if not clean else ""))


# ═════════════════════════════ ارسال پیام ═════════════════════════════
async def _tell(bot: Bot, uid: int, text: str, kb=None) -> bool:
    try:
        await bot.send_message(uid, text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
        return True
    except TelegramError as e:
        log.warning("send to %s failed: %s", uid, e)
        return False


async def _main_bot_note(text: str) -> None:
    """اگر ادمین هنوز ربات فروش را /start نکرده، از ربات مدیریت خبر می‌دهیم."""
    cfg = C.load_config_ro()
    if not cfg["bot_token"]:
        return
    try:
        b = Bot(cfg["bot_token"])
        for a in cfg["admin_ids"]:
            try:
                await b.send_message(a, text, parse_mode=HTML)
            except TelegramError:
                pass
    except Exception:  # noqa: BLE001
        log.exception("main bot note")


async def _notify_admins(bot: Bot, text: str, kb=None, photo: str | None = None) -> list[list]:
    sent, missed = [], False
    for a in C.load_config_ro()["admin_ids"]:
        try:
            if photo:
                m = await bot.send_photo(a, photo, caption=text, parse_mode=HTML, reply_markup=kb)
            else:
                m = await bot.send_message(a, text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
            sent.append([a, m.message_id, bool(photo)])
        except TelegramError as e:
            log.warning("notify admin %s failed: %s", a, e)
            missed = True
            if photo:                          # عکس نرفت (مثلاً file_id) ← حداقل متن
                try:
                    m = await bot.send_message(a, text, parse_mode=HTML, reply_markup=kb)
                    sent.append([a, m.message_id, False])
                    missed = False
                except TelegramError:
                    pass
    if missed:
        await _main_bot_note("⚠️ یک اعلان فروشگاه به شما نرسید. ربات فروش را باز کنید و <code>/start</code> بزنید تا اعلان‌ها و رسیدها را بگیرید.")
    return sent


def order_text(o: dict, with_status: bool = True) -> str:
    s = shop_cfg()
    cu = s["customers"].get(str(o["uid"]))
    what = KIND_FA.get(o["kind"], o["kind"])
    if o["kind"] in ("buy", "renew", "reserve"):
        what += f" — {esc(o.get('pname') or '')}"
    if o["kind"] in ("renew", "reserve"):
        what += f" ({esc(o.get('username') or '')})"
    rows = [f"🧾 <b>سفارش #{o['id']}</b>", f"👤 {label(cu, o['uid'])}", f"📌 {what}", f"💰 مبلغ: <b>{money(o['amount'])}</b> تومان"]
    if o.get("code"):
        rows.append(f"🏷 کد تخفیف <code>{esc(o['code'])}</code>: {money(o.get('disc', 0))} تومان تخفیف (قیمت اصلی {money(o.get('orig', 0))})")
    if cu:
        rows.append(f"👛 موجودی مشتری: {money(cu['balance'])}")
    if o.get("mode") == "auto":
        rows.append(f"⚡ تأیید خودکار — مبلغ واریزی: <code>{int(o.get('pay_amount') or o['amount']) * 10}</code> ریال" + (" ✅ با پیامک بانک تأیید شد" if o.get("auto") else ""))
    if o.get("mode") == "zp":
        rows.append("💳 پرداخت آنلاین زرین‌پال" + (f" — پیگیری <code>{esc(o['zp_ref'])}</code> ✅" if o.get("zp_ref") else " — منتظر پرداخت"))
    if o.get("hold"):
        rows.append(f"⚠️ پرداخت پیدا شد ولی تأیید خودکار انجام نشد: {esc(o['hold'])} — لطفاً دستی بررسی کنید.")
    rows.append(f"🕒 {sales.jdatetime(o.get('rts') or o['ts'])}")
    if o.get("text"):
        rows.append(f"📝 {esc(o['text'])}")
    if with_status:
        rows.append(f"وضعیت: {STATUS_FA.get(o['status'], o['status'])}")
    if o.get("err"):
        rows.append(f"علت: {esc(o['err'])}")
    if o.get("reason"):
        rows.append(f"دلیل رد: {esc(o['reason'])}")
    return "\n".join(rows)


def order_kb(o: dict) -> M | None:
    oid = o["id"]
    if o["status"] == "pending":
        return M([[B("✅ تأیید", callback_data=f"ad:ok:{oid}"), B("❌ رد", callback_data=f"ad:nom:{oid}")]])
    if o["status"] == "failed":
        return M([[B("🔁 تلاش مجدد", callback_data=f"ad:retry:{oid}")], [B("💰 اعتبار به کیف پول مشتری", callback_data=f"ad:cr:{oid}")]])
    return None


async def _edit_admin_msgs(bot: Bot, o: dict, extra: str = "") -> None:
    text = order_text(o) + (f"\n\n{extra}" if extra else "")
    kb = order_kb(o)
    for chat, mid, is_photo in o.get("adm") or []:
        try:
            if is_photo:
                await bot.edit_message_caption(chat, mid, caption=text[:1020], parse_mode=HTML, reply_markup=kb)
            else:
                await bot.edit_message_text(text, chat, mid, parse_mode=HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.debug("edit admin msg: %s", e)
        except TelegramError as e:
            log.debug("edit admin msg: %s", e)


async def _after(bot: Bot, oid: str, extra: str) -> None:
    """بعد از انجام/شکست: پیام‌های ادمین به‌روز می‌شود؛ اگر سفارش پیام ادمین نداشت (پرداخت با کیف پول) خبر ساده می‌رود."""
    o = order_get(oid)
    if not o:
        return
    if o.get("adm"):
        await _edit_admin_msgs(bot, o, extra)
        if o["status"] == "failed":            # ویرایش پیام اعلان صوتی ندارد؛ یک اعلان کوتاه جدا برای دیده‌شدن
            await _notify_admins(bot, f"⚠️ سفارش #{oid} نیاز به بررسی دارد (دکمه‌های «تلاش مجدد» و «اعتبار به کیف پول» روی پیام رسید است یا /admin).")
        return
    kb = order_kb(o)
    sent = await _notify_admins(bot, order_text(o) + f"\n\n{extra}", kb)
    if kb and sent:                            # دکمه‌های این پیام‌ها هم بعد از اقدام به‌روز شوند
        await asyncio.to_thread(order_update, oid, adm=sent)


# ═════════════════════════════ تأیید خودکار با پیامک بانک ═════════════════════════════
# پیامک واریز بانک (با اپ فورواردر روی گوشی ادمین) به وب‌هوک وب‌پنل می‌رسد. تأیید فقط وقتی انجام می‌شود که
# پیامک «واریز» باشد، مبلغ آن دقیقاً با مبلغ یکتای یک و فقط یک سفارش بازِ خودکار یکی باشد و هنوز مصرف نشده باشد.
# هر حالت مبهم یا مشکوک (چند سفارش، سقف مبلغ، مشتری مسدود، مبلغ بدون سفارش) تأیید نمی‌شود و به ادمین اعلام می‌شود.
_DIGITS = str.maketrans({**{c: str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}, **{c: str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")},
                         "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "ـ": ""})   # اعداد فارسی/عربی و حروف عربی (ي ك) → فارسی؛ کشیده حذف
_DEP_WORDS = ("واریز", "deposit", "variz", "credit")
_WD_WORDS = ("برداشت", "withdraw", "debit", "کسر", "خرید")
_AMT_PATS = (r"(?:مبلغ|واریز|deposit|amount|credit)\D{0,25}?(?<!\d)(\d{4,13})(?!\d)",
             r"[+＋]\s*(?<!\d)(\d{4,13})(?!\d)",
             r"(?<!\d)(\d{4,13})(?!\d)\s*[+＋]",
             r"(?<!\d)(\d{4,13})(?!\d)\s*(?:ریال|تومان|تومن|rial|rials|rls|irr|toman)")


def sms_norm(text: str) -> str:
    t = str(text or "").translate(_DIGITS)
    t = re.sub(r"[\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069]", " ", t)
    t = re.sub(r"(?<=\d)[,٬،](?=\d)", "", t)                    # جداکننده‌ی هزارگان
    return t.strip()[:1000]


def sms_is_deposit(t: str) -> bool:
    low = t.lower()
    dep = any(w in low for w in _DEP_WORDS) or bool(re.search(r"[+＋]\s*\d|\d\s*[+＋]", t))
    wd = any(w in low for w in _WD_WORDS) or bool(re.search(r"(?<![\w/:.])[-−–]\s*\d{4,}|\d{4,}\s*[-−–](?![\d/:-])", t))
    return dep and not wd


def sms_amounts(t: str, unit: str = "rial") -> list[int]:
    """مبلغ‌های محتمل پیامک بر حسب تومان (به ترتیب اولویت). عددهای بعد از «مانده/موجودی» نادیده گرفته می‌شود."""
    t2 = re.sub(r"(?:مانده|موجودی|balance|bal)[^\n]{0,40}", " ", t, flags=re.I)
    out: list[int] = []
    for pat in _AMT_PATS:
        for m in re.finditer(pat, t2, flags=re.I):
            v = int(m.group(1))
            v = v if unit == "toman" else (v // 10 if v % 10 == 0 else 0)
            if v > 0 and v not in out:
                out.append(v)
    return out


def auto_match(text: str) -> dict:
    """پیامک را (اتمیک روی config.json) با سفارش‌های باز خودکار تطبیق می‌دهد. خروجی: {res, oid, amount, order?}
    res: ok | hold | nomatch | late | ambiguous | ignored | dup | disabled | noamount"""
    now = int(time.time())
    t = sms_norm(text)
    h = hashlib.sha256(t.encode("utf-8")).hexdigest()[:32]
    out: dict = {"res": "ignored", "oid": "", "amount": 0}

    def mut(c):
        s = c["shop"]
        a = s["auto"]

        def log(res: str, oid: str = "", amount: int = 0) -> None:
            out.update(res=res, oid=oid, amount=amount)
            a["log"] = (a["log"] + [{"ts": now, "res": res, "oid": oid, "amount": amount}])[-C.SHOP_AUTO_LOG_MAX:]
        if not a["enabled"]:
            out["res"] = "disabled"
            return
        if any(x["h"] == h for x in a["seen"]):
            out["res"] = "dup"                                       # فوروارد تکراری همان پیامک
            return
        a["seen"] = ([x for x in a["seen"] if now - int(x.get("ts") or 0) < 14 * 86400][-(C.SHOP_AUTO_SEEN_MAX - 1):]) + [{"h": h, "ts": now}]
        musts = [sms_norm(m).lower() for m in re.split(r"[,،|\n]", a["must"]) if m.strip()]          # چند عبارت (مثلاً یکی برای هر بانک)؛ وجود هرکدام کافی است
        if (musts and not any(m in t.lower() for m in musts)) or not sms_is_deposit(t):
            log("ignored")
            return
        vals = sms_amounts(t, a["unit"])
        if not vals:
            log("noamount")
            return
        cand = [o for o in s["orders"] if o.get("mode") == "auto" and o["status"] in ("new", "pending") and int(o.get("pay_amount") or 0) in vals
                and (o["status"] == "pending" or now <= int(o.get("exp") or 0) + AUTO_GRACE)]
        rcand = [x for x in c["topups"] if x.get("mode") == "auto" and x["status"] == "pending" and not x.get("hold")
                 and int(x.get("pay_amount") or 0) in vals and now <= int(x.get("exp") or 0) + AUTO_GRACE]
        if rcand:                                                   # شارژ خودکار کیف پول نماینده
            if cand or len(rcand) > 1:
                log("ambiguous", ",".join([x["id"] for x in cand] + [x["id"] for x in rcand])[:40], int(vals[0]))
                return
            rt = rcand[0]
            amt = int(rt["pay_amount"])
            rp = c["reps"].get(rt["rid"]) or {}
            used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400) + _rep_used(c, now)
            why = ("نماینده مسدود است" if rp.get("blocked") else "مبلغ از سقف تأیید خودکار بیشتر است" if amt > a["max_amount"]
                   else "سقف روزانه‌ی تأیید خودکار پر شده" if used + amt > a["daily_max"]
                   else "سقف موجودی کیف پول رد می‌شود" if int(rp.get("balance") or 0) + amt > C.WALLET_MAX else "")
            if why:
                rt["hold"] = why
                log("hold", rt["id"], amt)
            else:
                C.topup_credit_in(c, rt, amt, "شارژ خودکار با پیامک بانک")
                rt.update(auto_ok=True, cts=now)
                log("ok", rt["id"], amt)
            out["topup"] = dict(rt)
            return
        if not cand:
            late = [o for o in s["orders"] if o.get("mode") == "auto" and o["status"] == "cancelled" and int(o.get("pay_amount") or 0) in vals and now - int(o.get("ts") or 0) < AUTO_QUARANTINE]
            log("late", late[-1]["id"], int(late[-1]["pay_amount"])) if late else log("nomatch", "", vals[0])
            return
        if len(cand) > 1:
            log("ambiguous", ",".join(o["id"] for o in cand)[:40], int(cand[0]["pay_amount"]))
            return
        o = cand[0]
        amt = int(o["pay_amount"])
        cu = s["customers"].get(str(o["uid"])) or {}
        used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400) + _rep_used(c, now)
        why = "مشتری مسدود است" if cu.get("banned") else "مبلغ از سقف تأیید خودکار بیشتر است" if amt > a["max_amount"] else "سقف روزانه‌ی تأیید خودکار پر شده" if used + amt > a["daily_max"] else ""
        if why:
            if o["status"] == "new":
                o.update(status="pending", rts=now)
            o["hold"] = why
            log("hold", o["id"], amt)
            out["order"] = dict(o)
            return
        o.update(status="processing", by=0, cts=now, auto=True, auto_ok=True, sms=h[:12])
        log("ok", o["id"], amt)
        out["order"] = dict(o)
    C.update_config(mut)
    return out


async def auto_confirm(bot: Bot, text: str) -> dict:
    """ورودی وب‌هوک: تطبیق پیامک + انجام سفارش + اطلاع به مشتری و ادمین. خطایش بالا نمی‌رود مگر خطای ذخیره‌سازی."""
    r = await asyncio.to_thread(auto_match, text)
    res, o = r["res"], r.get("order")
    try:
        tp = r.get("topup")
        if tp:                                                      # شارژ خودکار کیف پول نماینده
            _rp = C.load_config_ro()["reps"].get(tp["rid"]) or {}
            who = esc(_rp.get("label") or _rp.get("name") or tp["rid"])
            if res == "ok":
                C.audit_event("تأیید خودکار", "شارژ خودکار کیف پول نماینده", tp["rid"], f"{money(tp['amount'])} تومان", amount=int(tp["amount"]), cat="wallet")
                await _notify_admins(bot, f"✅ <b>شارژ خودکار نماینده</b>\nنماینده: <b>{who}</b>\nمبلغ: <b>{money(tp['amount'])}</b> تومان")
            elif res == "hold":
                await _notify_admins(bot, f"⚠️ <b>واریز نماینده {who} دیده شد ولی شارژ خودکار انجام نشد</b>\nمبلغ: <b>{money(tp['pay_amount'])}</b> تومان\nعلت: {esc(tp.get('hold') or '')}\n\nاز وب‌پنل ← نماینده‌ها، درخواست را دستی تأیید یا رد کنید.")
            return {k: v for k, v in r.items() if k not in ("order", "topup")}
        if res == "ok" and o:
            C.audit_event("تأیید خودکار", "تأیید خودکار کارت‌به‌کارت", o["id"], f"{KIND_FA.get(o['kind'], o['kind'])} — {money(o['pay_amount'])} تومان", amount=int(o["pay_amount"]), cat="wallet")
            await _tell(bot, o["uid"], f"✅ <b>پرداخت شما شناسایی شد</b>\nمبلغ: <b>{money(o['pay_amount'])}</b> تومان\nسفارش <b>#{o['id']}</b> در حال انجام است…")
            await fulfil(bot, o["id"])
        elif res == "hold" and o:
            await _tell(bot, o["uid"], f"🕓 پرداخت سفارش <b>#{o['id']}</b> دریافت شد و برای بررسی نهایی به ادمین رفت.")
            if o.get("adm"):
                await _edit_admin_msgs(bot, o)
            sent = await _notify_admins(bot, order_text(o) + "\n\n⚠️ <b>پرداخت در حساب دیده شد ولی تأیید خودکار انجام نشد؛ دستی تأیید یا رد کنید.</b>", order_kb(o))
            await asyncio.to_thread(order_update, o["id"], adm=sent)
        elif res in ("nomatch", "late", "ambiguous"):
            why = {"nomatch": "سفارش بازِ خودکاری با این مبلغ پیدا نشد", "late": f"این مبلغ مال سفارش لغوشده‌ی #{r['oid']} است (مهلت تمام شده بود)",
                   "ambiguous": f"با چند سفارش هم‌خوان بود ({esc(r['oid'])})"}[res]
            await _notify_admins(bot, f"💳 <b>واریز {money(r['amount'])} تومان دیده شد ولی تأیید خودکار انجام نشد</b>\nعلت: {why}\n\nاگر مال مشتری است، سفارش را از /admin دستی رسیدگی کنید.\n\n<code>{esc(sms_norm(text)[:300])}</code>")
    except Exception:  # noqa: BLE001
        log.exception("auto_confirm %s", res)
    return {k: v for k, v in r.items() if k != "order"}


# ═════════════════════════════ نگهداری پس‌زمینه ═════════════════════════════
async def maintain(bot: Bot, startup: bool = False) -> None:
    now = int(time.time())
    hours = shop_cfg()["receipt_hours"] * 3600
    stale: list[dict] = []
    stuck: list[dict] = []
    remind: list[dict] = []

    def mut(c):
        for t in c["topups"]:                                       # شارژ خودکار نماینده که مهلتش تمام شد
            if t.get("mode") == "auto" and t["status"] == "pending" and not t.get("hold") and now > int(t.get("exp") or 0) + AUTO_GRACE:
                t.update(status="rejected", done=now, hold="مهلت واریز تمام شد")
        for o in c["shop"]["orders"]:
            if o["status"] == "new" and ((now > int(o.get("exp") or 0) + AUTO_GRACE) if o.get("mode") == "auto"
                                         else (now > int(o.get("exp") or 0) + ZP_GRACE) if o.get("mode") == "zp" else now - o["ts"] > hours):
                o["status"] = "cancelled"
                stale.append(dict(o))
            elif o["status"] == "processing" and (startup or now - int(o.get("cts") or o["ts"]) > STUCK_SECONDS):
                o.update(status="failed", err="عملیات نیمه‌کاره ماند (ریستارت یا قطع ارتباط)", unsure=True)
                stuck.append(dict(o))
            elif o["status"] == "pending" and not o.get("rem") and now - int(o.get("rts") or o["ts"]) > REMIND_SECONDS:
                o["rem"] = now
                remind.append(dict(o))
    C.update_config(mut)
    try:
        await cleanup_tests(bot)
    except Exception:  # noqa: BLE001
        log.exception("test cleanup")
    for o in stale:
        if o.get("mode") == "zp":
            await _tell(bot, o["uid"], f"🚫 سفارش #{o['id']} چون پرداخت آنلاین کامل نشد لغو شد.\nاگر مبلغی از حساب شما کسر شده، تا چند دقیقه‌ی بعد خودکار به کیف پول شما اضافه می‌شود.")
        elif o.get("mode") == "auto":
            await _tell(bot, o["uid"], f"🚫 سفارش #{o['id']} چون پرداختی با مبلغ دقیق آن شناسایی نشد لغو شد.\nاگر پرداخت کرده‌اید، عکس رسید را برای پشتیبانی بفرستید.")
        else:
            await _tell(bot, o["uid"], f"🚫 سفارش #{o['id']} چون رسید ارسال نشد لغو شد.")
    for o in stuck:
        await _after(bot, o["id"], "⚠️ نیمه‌کاره ماند؛ قبل از «تلاش مجدد» وضعیت کاربر را در پنل ببینید.")
    if remind:
        await _notify_admins(bot, f"⏰ {len(remind)} رسید بیش از ۳ ساعت است منتظر بررسی شماست.\n/admin ← رسیدهای در انتظار")


# ═════════════════════════════ پرداخت آنلاین زرین‌پال ═════════════════════════════
_zp_warned: dict[str, float] = {}


async def _adm_note(bot: Bot | None, text: str) -> None:
    if bot is not None:
        await _notify_admins(bot, text)
    else:
        await _main_bot_note(text)


async def zp_warn_admins(bot: Bot | None, key: str, text: str) -> None:
    """هشدار خرابی درگاه به ادمین‌ها، حداکثر هر ۳۰ دقیقه یک‌بار برای هر نوع خطا (تا پیام‌ها انبوه نشود)."""
    now = time.time()
    if now - _zp_warned.get(key, 0) < 1800:
        return
    _zp_warned[key] = now
    try:
        await _adm_note(bot, text)
    except Exception:  # noqa: BLE001
        log.exception("zp warn")


async def zp_after(bot: Bot | None, res: dict) -> None:
    """بعد از اعمال یک پرداخت زرین‌پال (از callback یا پایشگر): شروع انجام سفارش، پیام به مشتری و ادمین. خطایش بالا نمی‌رود."""
    st, pay = res.get("state"), res.get("pay") or {}
    try:
        if st == "order_ok" and bot is not None:
            o = res["order"]
            C.audit_event("پرداخت آنلاین", "پرداخت زرین‌پال سفارش فروشگاه", o["id"], f"{KIND_FA.get(o['kind'], o['kind'])} — {money(pay.get('amount') or o['amount'])} تومان — پیگیری {pay.get('ref_id') or '-'}",
                          amount=int(pay.get("amount") or 0), cat="wallet")
            await _tell(bot, o["uid"], f"✅ <b>پرداخت آنلاین شما تأیید شد</b>\nکد پیگیری: <code>{esc(pay.get('ref_id') or '-')}</code>\nسفارش <b>#{o['id']}</b> در حال انجام است…")
            await fulfil(bot, o["id"])
        elif st == "credited":
            C.audit_event("پرداخت آنلاین", "پرداخت دیرهنگام زرین‌پال به کیف پول مشتری", str(res.get("uid")), f"سفارش #{res.get('oid')} — {money(pay.get('amount') or 0)} تومان", amount=int(pay.get("amount") or 0), cat="wallet")
            if bot is not None:
                await _tell(bot, res["uid"], f"💰 پرداخت آنلاین شما تأیید شد ولی سفارش #{res['oid']} دیگر باز نبود؛ مبلغ <b>{money(pay.get('amount') or 0)}</b> تومان به کیف پول شما اضافه شد.\n"
                                             f"موجودی: <b>{money(res['balance'])}</b> تومان")
            await _adm_note(bot, f"💰 پرداخت آنلاین دیرهنگام برای سفارش #{res['oid']} ({money(pay.get('amount') or 0)} تومان) به کیف پول مشتری <code>{res['uid']}</code> اضافه شد.")
        elif st == "rep_ok":
            rp = C.load_config_ro()["reps"].get(res["rid"]) or {}
            C.audit_event("پرداخت آنلاین", "شارژ آنلاین کیف پول نماینده (زرین‌پال)", res["rid"], f"{money(pay.get('amount') or 0)} تومان — پیگیری {pay.get('ref_id') or '-'}", amount=int(pay.get("amount") or 0), cat="wallet")
            await _adm_note(bot, f"💳 <b>شارژ آنلاین نماینده</b>\nنماینده: <b>{esc(rp.get('label') or rp.get('name') or res['rid'])}</b>\nمبلغ: <b>{money(pay.get('amount') or 0)}</b> تومان\nپیگیری: <code>{esc(pay.get('ref_id') or '-')}</code>")
        elif st == "stuck":
            await _adm_note(bot, f"🚨 <b>پرداخت زرین‌پال تأیید شد ولی اعمال نشد</b>\nمبلغ: {money(pay.get('amount') or 0)} تومان — پیگیری <code>{esc(pay.get('ref_id') or '-')}</code>\n"
                                 f"نوع: {'نماینده' if pay.get('kind') == 'rep' else 'سفارش فروشگاه'} <code>{esc(pay.get('ref') or '')}</code>\nعلت: {esc(res.get('why') or '-')}\nدستی رسیدگی کنید.")
    except Exception:  # noqa: BLE001
        log.exception("zp_after %s", st)


_zp_unv_ts = 0.0                    # آخرین بررسی «verify‌نشده‌ها» از زرین‌پال
ZP_UNV_GAP = 600                    # حداقل فاصله‌ی دو بررسی (ثانیه)؛ از محدودیت نرخ زرین‌پال جلوگیری می‌شود
ZP_UNV_WINDOW = 3 * 86400           # فقط وقتی پرداخت بازِ/منقضی/ناموفقِ این بازه در دفتر هست پرسیده می‌شود


async def zp_unverified_pass(b: Bot | None) -> int:
    """از خود زرین‌پال می‌پرسد کدام پرداخت‌ها پرداخت شده ولی verify نشده‌اند و با دفتر ما تطبیق می‌دهد. حتی پرداختی که دفتر ما
    «منقضی/ناموفق» علامت زده ولی مشتری واقعاً پرداخته هم نجات پیدا می‌کند. مبلغ نامنطبق هیچ‌وقت خودکار اعمال نمی‌شود. خروجی: تعداد نجات‌یافته‌ها."""
    global _zp_unv_ts
    now = time.time()
    zp = Z.zp_cfg()
    if not zp["merchant"] or not (zp["shop_on"] or zp["rep_on"]) or now - _zp_unv_ts < ZP_UNV_GAP:
        return 0
    if not any(p["status"] in ("created", "expired", "failed") and now - p["ts"] < ZP_UNV_WINDOW for p in zp["pays"]):
        return 0
    _zp_unv_ts = now
    try:
        items = await Z.unverified()
    except Z.ZPError as e:
        log.warning("zp unverified: %s", e)
        return 0
    done = 0
    for it in items[:20]:
        au = re.sub(r"[^A-Za-z0-9]", "", str(it.get("authority") or ""))[:64]
        pay = await asyncio.to_thread(Z.pay_find, au) if au else None
        if not pay or pay["status"] == "paid" or now - pay["ts"] < Z.RECON_MIN_AGE:
            continue                                         # مال این ربات نیست، قبلاً اعمال شده، یا مشتری هنوز در صفحه‌ی بانک است
        try:
            paid = int(it.get("amount") or 0)
        except (TypeError, ValueError):
            paid = 0
        if paid and paid != int(pay["amount"]) * 10:        # مبلغ نامنطبق: خودکار اعمال نمی‌کنیم؛ ادمین ببیند
            msg = f"مبلغ نامنطبق: زرین‌پال {paid // 10:,} تومان، دفتر ما {int(pay['amount']):,} تومان"
            if pay.get("msg") != msg[:160]:                  # فقط یک‌بار اطلاع بده
                await asyncio.to_thread(Z.pay_mark, au, pay["status"], msg, pay["status"])
                log.error("zp unverified %s: %s", pay["id"], msg)
                if b is not None:
                    try:
                        await _notify_admins(b, f"⚠️ پرداخت زرین‌پال با مبلغ نامنطبق رسید و خودکار اعمال نشد.\n{esc(msg)}\nشناسه: <code>{esc(pay['id'])}</code> — دستی بررسی کنید.")
                    except Exception:  # noqa: BLE001
                        log.exception("zp unverified notify")
            continue
        res = await Z.finalize(au, None)
        if res["state"] in ("order_ok", "credited", "stuck", "rep_ok"):
            done += 1
            log.info("zp unverified: پرداخت %s نجات پیدا کرد (%s)", pay["id"], res["state"])
            await zp_after(b, res)
        await asyncio.sleep(0.3)
    return done


async def zp_reconcile() -> None:
    """پرداخت‌های بازی که بازگشتشان نرسیده (مرورگر بسته شد و…) را دوباره verify می‌کند؛ پول گرفته‌شده بدون اثر نمی‌ماند.
    علاوه بر دفتر خودمان، لیست «verify‌نشده‌ها» را هم از خود زرین‌پال می‌گیرد (zp_unverified_pass)."""
    pick, _gone = await asyncio.to_thread(Z.recon_pick)

    async def run(b: Bot | None) -> None:
        for p in pick:
            res = await Z.finalize(p["auth"], None)
            if res["state"] in ("order_ok", "credited", "stuck", "rep_ok"):
                await zp_after(b, res)
            await asyncio.sleep(0.3)
        await zp_unverified_pass(b)
    token = shop_cfg()["token"]
    if token:
        async with Bot(token) as b:
            await run(b)
    else:
        await run(None)


async def zp_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await zp_reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("zp reconcile")
        await asyncio.sleep(120)


def winback_issue(uid: int, percent: int, hours: int) -> str:
    """یک کد تخفیف شخصی و یک‌بارمصرف برای مشتری می‌سازد و خودِ کد را برمی‌گرداند."""
    out: dict = {}

    def mut(c):
        codes = c["shop"]["codes"]
        while True:
            code = "WB" + secrets.token_hex(3).upper()
            if code not in codes:
                break
        now = int(time.time())
        codes[code] = {"kind": "percent", "value": int(percent), "max": 1, "per_user": 1, "exp": now + int(hours) * 3600, "active": True,
                       "plans": [], "note": "بازگشت مشتری", "created": now, "uid": int(uid), "auto": True}
        out["code"] = code
    C.update_config(mut)
    return out["code"]


def winback_cleanup(now: float | None = None, keep_days: int = 7) -> int:
    """کدهای خودکارِ بازگشت را که مدت‌ها از انقضایشان گذشته پاک می‌کند (دیکشنری کدها بی‌سقف رشد نکند). تعداد حذف‌شده را برمی‌گرداند."""
    now = now or time.time()
    out = {"n": 0}

    def mut(c):
        codes = c["shop"]["codes"]
        dead = [k for k, d in codes.items() if d.get("auto") and d.get("exp") and d["exp"] < now - keep_days * 86400]
        for k in dead:
            codes.pop(k, None)
        out["n"] = len(dead)
    if any(d.get("auto") and d.get("exp") and d["exp"] < now - keep_days * 86400 for d in shop_cfg()["codes"].values()):
        C.update_config(mut)
    return out["n"]


# ═════════════════════════════ یادآوری انقضا و حجم ═════════════════════════════
async def remind(bot: Bot) -> None:
    """به مشتری‌ها قبل از انقضا / نزدیک اتمام حجم / بعد از انقضا پیام می‌دهد (هر مورد برای هر دوره‌ی اعتبار فقط یک‌بار).
    وضعیت ارسال داخل خودِ سرویس مشتری (rem) نگه داشته می‌شود و با تمدید (تغییر تاریخ انقضا) صفر می‌شود."""
    cfg = C.load_config_ro()
    rm = cfg["shop"]["remind"]
    if not rm["enabled"]:
        return
    refs: list[tuple[str, dict, int, dict]] = []             # (uid, مشتری, اندیس سرویس, سرویس)
    for uid, cu in cfg["shop"]["customers"].items():
        if cu["banned"]:
            continue
        for i, sv in enumerate(cu["services"]):
            if sv["u"] == cu.get("test_user") and cu.get("test_del") != 2:        # تست رایگان تمدیدنشده: یادآوری نمی‌خواهد
                continue
            refs.append((uid, cu, i, sv))
    if not refs:
        return

    def pid_of(sv):
        return sv["pid"] if sv["pid"] and C.get_panel(cfg, sv["pid"]) else cfg["active_panel"]
    info: dict[tuple[str, str], dict] = {}
    by_pid: dict[str, set] = defaultdict(set)
    for _, _, _, sv in refs:
        by_pid[pid_of(sv)].add(sv["u"])
    for pid, names in by_pid.items():
        api = C.get_api(pid)
        if not api.is_configured():
            continue
        try:
            if len(names) <= 40:
                for n in names:
                    try:
                        u = await asyncio.wait_for(api.request("GET", api.upath(n)), 20)
                        if isinstance(u, dict):
                            info[(pid, n)] = u
                    except C.RebeccaError:
                        pass
            else:
                for u in await asyncio.wait_for(api.iter_users(), 180):
                    if u.get("username") in names:
                        info[(pid, u["username"])] = u
        except Exception as e:  # noqa: BLE001
            log.warning("remind fetch %s: %s", pid, e)
    now = time.time()
    updates: list[tuple[str, str, str, dict]] = []
    wb = rm["winback"]
    alive: set[str] = set()                                        # مشتری‌هایی که هنوز یک سرویس فعال دارند ← «رفته» حساب نمی‌شوند
    for uid, cu, i, sv in refs:
        u0 = info.get((pid_of(sv), sv["u"]))
        if u0 and u0.get("status") in ("active", "on_hold") and (not u0.get("expire") or int(u0["expire"]) > now):
            alive.add(uid)
    wb_sent: set[str] = set()                                      # حداکثر یک کد بازگشت برای هر مشتری در هر اجرا
    for uid, cu, i, sv in refs:
        u = info.get((pid_of(sv), sv["u"]))
        if not u:
            continue
        if C._raw_plans(u):                                        # اشتراک رزروشده دارد؛ بعد از پایان فعلی خودکار شروع می‌شود، پس هشدار انقضا/حجم لازم نیست
            continue
        expire = int(u.get("expire") or 0)
        st = u.get("status", "")
        used, lim = u.get("used_traffic") or 0, u.get("data_limit") or 0
        r = dict(sv.get("rem") or {})
        if r.get("exp") != expire:
            r = {"exp": expire}
        texts = []
        if expire and st == "active":
            left = (expire - now) / 86400
            due = [d for d in rm["days"] if left <= d and f"d{d}" not in r]
            if due and left > 0:
                for d in rm["days"]:
                    if d >= min(due):
                        r[f"d{d}"] = 1
                when = "کمتر از یک روز" if left < 1 else f"{int(left) + (1 if left % 1 > .5 else 0) or 1} روز"
                texts.append(f"⏰ <b>اعتبار سرویس {esc(sv['u'])}</b> {('تا ' + when + ' دیگر') if left >= 1 else when + ' دیگر'} تمام می‌شود.")
        if (st == "expired" or (expire and expire < now - 60)) and "x" not in r:
            r["x"] = 1
            texts.append(f"⛔ <b>سرویس {esc(sv['u'])}</b> منقضی شد.")
        if lim:
            pct = used * 100 / lim
            if (pct >= 100 or st == "limited") and "l" not in r:
                r["l"] = r["u"] = 1
                texts.append(f"📵 <b>حجم سرویس {esc(sv['u'])}</b> تمام شد.")
            elif pct >= rm["usage"] and "u" not in r:
                r["u"] = 1
                texts.append(f"📊 <b>حجم سرویس {esc(sv['u'])}</b> رو به اتمام است ({pct:.0f}٪ مصرف شده؛ {C.fmt_gb(max(0, lim - used))} باقی مانده).")
        if (wb["enabled"] and expire > 0 and st not in ("disabled", "on_hold") and now - expire >= wb["after_days"] * 86400 and "wb" not in r
                and uid not in alive and uid not in wb_sent):
            r["wb"] = int(now)                                    # قبل از ارسال علامت می‌خورد: خطا هرگز باعث ارسال تکراری نمی‌شود
            wb_sent.add(uid)
            try:
                code = await asyncio.to_thread(winback_issue, int(uid), wb["percent"], wb["hours"])
                texts.append(f"🎁 <b>دلمان برایتان تنگ شده!</b>\nسرویس {esc(sv['u'])} چند روزی است تمام شده. برای برگشتن، کد تخفیف شخصی شما:\n"
                             f"<code>{code}</code>\n{wb['percent']}٪ تخفیف، فقط یک‌بار و تا {wb['hours']} ساعت دیگر معتبر است.\n"
                             "هنگام پرداخت «🏷 کد تخفیف دارم» را بزنید.")
            except Exception:  # noqa: BLE001
                log.exception("winback issue")
        if texts:
            await _tell(bot, int(uid), "\n\n".join(texts) + "\n\nبرای تمدید دکمه‌ی زیر را بزنید 👇", M([[B("🔄 تمدید سرویس", callback_data=f"rn:{i}")]]))
            await asyncio.sleep(0.06)
        if r != (sv.get("rem") or {}):
            updates.append((uid, sv["u"], sv["pid"], r))
    if updates:
        def mut(c):
            for uid, name, pid, r in updates:
                cu = c["shop"]["customers"].get(uid)
                for x in (cu or {}).get("services", []):
                    if x["u"] == name and x["pid"] == pid:
                        x["rem"] = r
        await asyncio.to_thread(C.update_config, mut)
    await asyncio.to_thread(winback_cleanup)


async def maintenance_loop(bot: Bot) -> None:
    first = True
    n = 0
    while True:
        try:
            await maintain(bot, startup=first)
            first = False
            n += 1
            if n % 6 == 2:                                  # هر ~۳۰ دقیقه (اولین بار ~۱۰ دقیقه بعد از بالا آمدن)
                try:
                    await remind(bot)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("shop remind")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("shop maintenance")
        await asyncio.sleep(300)


# ═════════════════════════════ رابط کاربری ═════════════════════════════
_hits: dict[int, deque] = defaultdict(lambda: deque(maxlen=30))


def _throttled(uid: int) -> bool:
    d, now = _hits[uid], time.time()
    d.append(now)
    return len(d) >= 25 and now - d[0] < 10


async def show(update: Update, context, text: str, kb=None) -> None:
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML, disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except TelegramError:
            pass
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb, parse_mode=HTML, disable_web_page_preview=True)


async def _ans(q, text: str | None = None, show_alert: bool = False) -> None:
    """پاسخ به callback؛ جواب دوباره یا قدیمی نباید هندلر را بشکند."""
    try:
        await q.answer(text, show_alert=show_alert)
    except TelegramError:
        pass


def is_adm(update: Update) -> bool:
    u = update.effective_user
    return bool(u and C.is_admin(u.id))


def mini_url() -> str:
    """آدرس مینی‌اپ تلگرام؛ فقط وقتی SSL وب‌پنل سالم و دامنه مشخص است (تلگرام فقط https می‌پذیرد)."""
    c = C.load_config_ro()
    ss = c.get("ssl") or {}
    if not ss.get("enabled") or ss.get("error"):
        return ""
    dom = ss.get("domain") or ""
    if not dom:
        p = C.get_panel(c, c["active_panel"]) or (c["panels"][0] if c["panels"] else None)
        dom = tlsutil._domain_of(p["panel_url"]) if p else ""
    if not dom:
        return ""
    port = int(c.get("web_port") or 8686)
    return f"https://{dom}" + ("" if port == 443 else f":{port}") + "/app"


def main_kb(admin: bool, uid: int | None = None) -> M:
    mu = mini_url()
    rows = ([[B("📱 مینی‌اپ فروشگاه", web_app=WebAppInfo(url=mu))]] if mu else []) + [[B("🛒 خرید کانفیگ", callback_data="buy"), B("🔄 سرویس‌های من", callback_data="svc")],
            [B("👛 کیف پول", callback_data="wal"), B("🧾 سفارش‌های من", callback_data="ords")],
            [B("🔗 اتصال سرویس قبلی (با لینک ساب)", callback_data="lnk")],
            [B("☎️ پشتیبانی", callback_data="sup")]]
    if uid is not None and test_available() and not (customer_get(uid) or {}).get("test"):
        rows.insert(0, [B("🎁 دریافت تست رایگان", callback_data="tst")])
    if admin:
        rows.append([B("⚙️ مدیریت فروشگاه", callback_data="ad")])
    return M(rows)


def back_kb(extra=None) -> M:
    return M(list(extra or []) + [[B("🔙 منوی اصلی", callback_data="home")]])


async def home(update: Update, context, prefix: str = "") -> None:
    context.user_data.pop("aw", None)
    s = shop_cfg()
    u = update.effective_user
    cu = customer_get(u.id) or {}
    txt = s["welcome"] or "👋 به فروشگاه خوش آمدید.\nاز منوی زیر کانفیگ بخرید، کیف پول را شارژ کنید یا سرویس خود را تمدید کنید."
    txt = f"{prefix}{txt}\n\n👛 موجودی شما: <b>{money(cu.get('balance', 0))}</b> تومان"
    if not s["enabled"]:
        txt += "\n\n🔴 فروشگاه موقتاً بسته است."
    await show(update, context, txt, main_kb(is_adm(update), update.effective_user.id))


def _gate(update: Update) -> str | None:
    """نتیجه‌ی None = اجازه‌ی ادامه. وگرنه متن پیام توقف."""
    u = update.effective_user
    if not u:
        return "؟"
    if is_adm(update):
        return None
    cu = customer_get(u.id)
    if cu and cu["banned"]:
        return "⛔ دسترسی شما به این ربات محدود شده است."
    if not shop_cfg()["enabled"]:
        return "🔴 فروشگاه موقتاً بسته است؛ بعداً سر بزنید."
    return None


def _menu_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_BTN]], resize_keyboard=True, is_persistent=True)


async def deep(update: Update, context, arg: str) -> bool:
    """لینک‌های مینی‌اپ (/start=عمل_مقدار): همان صفحه‌ی ربات را باز می‌کند تا خرید، تمدید و پرداخت با همان منطق امن ربات انجام شود."""
    k, _, v = arg.partition("_")
    s, u = shop_cfg(), update.effective_user
    cu = customer_get(u.id) or {}
    if k == "buy":
        kb = _plans_kb("bp")
        await show(update, context, _plans_text("🛒 <b>پلن مورد نظر را انتخاب کنید:</b>") if kb else "هنوز پلنی برای فروش تعریف نشده است.", kb or back_kb())
    elif k == "bp" and v:
        await show_plan(update, context, v)
    elif k == "rn" and v.isdigit():
        i, svcs = int(v), cu.get("services") or []
        kb = _plans_kb("rp", f"{i}:") if i < len(svcs) else None
        await show(update, context, _plans_text(f"🔄 <b>پلن تمدید برای {esc(svcs[i]['u'])}</b>") if kb else "پلنی برای تمدید تعریف نشده است.", kb or back_kb())
    elif k == "wt":
        context.user_data["aw"] = ("amount",)
        await show(update, context, f"➕ <b>شارژ کیف پول</b>\n\nمبلغ را به <b>تومان</b> بفرستید ({money(s['min_topup'])} تا {money(s['max_topup'])}).", back_kb())
    elif k == "wal":
        await show(update, context, f"👛 <b>کیف پول</b>\n\nموجودی: <b>{money(cu.get('balance', 0))}</b> تومان",
                   back_kb([[B("➕ شارژ کیف پول", callback_data="wt"), B("🧾 تراکنش‌ها", callback_data="wh")]]))
    elif k == "tst":
        await give_test(update, context)
    elif k == "lnk":
        context.user_data["aw"] = ("link",)
        await show(update, context, "🔗 <b>اتصال سرویس قبلی</b>\n\nلینک ساب (اشتراک) سرویس خود را همین‌جا بفرستید تا به حساب شما وصل شود.", back_kb())
    elif k == "sup":
        await show(update, context, "☎️ <b>پشتیبانی</b>\n\n" + (esc(s["support"]) if s["support"] else "پشتیبانی هنوز تنظیم نشده است."), back_kb())
    else:
        return False
    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await asyncio.to_thread(customer_touch, u.id, u.full_name, u.username or "")
    block = _gate(update)
    if block:
        await update.effective_message.reply_html(block)
        return
    if not await force_join_ok(update, context):          # قبل از نمایش منو و دکمه‌ی دائمی
        return
    await update.effective_message.reply_html("⌨️ برای برگشت به منو، هر وقت دکمه‌ی «🏠 منوی اصلی» پایین صفحه را بزنید.", reply_markup=_menu_reply_kb())
    arg = (context.args[0] if getattr(context, "args", None) else "")[:40]
    if arg and await deep(update, context, arg):
        return
    await home(update, context)


def _card_text(o: dict) -> str:
    cards = shop_cfg()["cards"]
    if not cards:
        return "⚠️ شماره کارت هنوز توسط ادمین تنظیم نشده است."
    out = [f"💳 <b>کارت‌به‌کارت</b>\n\nمبلغ دقیق: <b>{money(o['amount'])}</b> تومان\n" + ("به یکی از کارت‌های زیر واریز کنید:\n" if len(cards) > 1 else "شماره کارت:\n")]
    return "\n".join(out + _card_lines(cards))


def _card_lines(cards: list) -> list[str]:
    out: list[str] = []
    for i, card in enumerate(cards, 1):
        num = " ".join(card["number"][k:k + 4] for k in range(0, len(card["number"]), 4))
        out.append((f"<b>{i}.</b> " if len(cards) > 1 else "") + f"<code>{esc(num)}</code>\n"
                   + (f"به نام: <b>{esc(card['holder'])}</b>\n" if card["holder"] else "") + (f"بانک: {esc(card['bank'])}\n" if card["bank"] else ""))
    return out


def auto_text(o: dict) -> str:
    """توضیح کامل پرداخت با تأیید خودکار: مبلغ دقیق (ریال، با لمس کپی می‌شود)، کارت، و اینکه چطور واریز کند تا تأیید شود."""
    s = shop_cfg()
    cards = s["cards"]
    pay, base = int(o["pay_amount"]), int(o.get("base") or o["amount"])
    rial = f"<code>{pay * 10}</code>"          # فقط رقم، بدون جداکننده؛ با لمس در تلگرام کپی می‌شود و مستقیم در اپ بانک جا می‌خورد
    what = KIND_FA[o["kind"]] + (f" — {esc(o.get('pname') or '')}" if o.get("pname") else "")
    extra = (pay - base) * 10
    if o["kind"] == "topup":
        why = f"3️⃣ {money(extra)} ریال اضافه فقط برای شناسایی پرداخت شماست و <b>کل مبلغ واریزی</b> به کیف پول شما اضافه می‌شود."
    else:
        why = f"3️⃣ {money(extra)} ریالِ آخرِ مبلغ فقط برای شناسایی پرداخت شماست؛ لطفاً مبلغ را رُند نکنید و کم یا زیاد هم نفرستید."
    head = (f"⚡ <b>سفارش #{o['id']}</b> — {what}\n\n💳 <b>کارت‌به‌کارت با تأیید خودکار</b>\n\n"
            f"مبلغ دقیق واریز (ریال):\n{rial} ریال\n👆 برای کپی، روی عدد بزنید\n\n" + ("به یکی از کارت‌های زیر واریز کنید:\n" if len(cards) > 1 else "شماره کارت:\n"))
    how = (f"\n📌 <b>برای اینکه پرداخت خودکار تأیید شود:</b>\n"
           f"1️⃣ دقیقاً {rial} <b>ریال</b> را با <b>تمام ارقام</b> واریز کنید؛ حتی یک ریال کم یا زیاد، تأیید خودکار انجام نمی‌شود.\n"
           f"2️⃣ فقط <b>یک‌بار</b> و با کارت‌به‌کارت یا انتقال بانکی به کارت بالا واریز کنید (دو بار واریز نکنید).\n"
           f"{why}\n"
           f"4️⃣ لازم نیست رسید بفرستید؛ معمولاً چند دقیقه بعد از واریز، پیام تأیید می‌گیرید و سفارش خودکار انجام می‌شود.\n\n"
           f"⏳ مهلت واریز: <b>{s['auto']['minutes']} دقیقه</b> (تا {sales.jdatetime(o['exp'])})\n\n"
           f"⚠️ اگر مبلغ دیگری واریز کردید یا مهلت تمام شد، دکمه‌ی «🧾 واریز کردم ولی تأیید نشد» را بزنید و عکس رسید بفرستید تا ادمین دستی بررسی کند.")
    return head + "\n".join(_card_lines(cards)) + how


async def ask_auto(update: Update, context, o: dict) -> None:
    context.user_data.pop("aw", None)
    await show(update, context, auto_text(o),
               M([[B("🔄 بررسی وضعیت پرداخت", callback_data=f"ac:{o['id']}")],
                  [B("🧾 واریز کردم ولی تأیید نشد", callback_data=f"ar:{o['id']}")],
                  [B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")], [B("🔙 منوی اصلی", callback_data="home")]]))


def _auto_ok(s: dict, price: int) -> bool:
    return bool(s["auto"]["enabled"] and s["cards"] and price <= s["auto"]["max_amount"])


async def ask_receipt(update: Update, context, o: dict) -> None:
    context.user_data["aw"] = ("receipt", o["id"])
    what = KIND_FA[o["kind"]] + (f" — {esc(o.get('pname') or '')}" if o.get("pname") else "")
    await show(update, context,
               f"🧾 <b>سفارش #{o['id']}</b> — {what}\n\n{_card_text(o)}\n📸 بعد از پرداخت، <b>عکس رسید</b> را همین‌جا بفرستید "
               f"(یا شماره پیگیری را بنویسید).\n⏳ تا {shop_cfg()['receipt_hours']} ساعت فرصت دارید.",
               M([[B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")], [B("🔙 منوی اصلی", callback_data="home")]]))


def _active_plans() -> list[tuple[dict, dict]]:
    s = shop_cfg()
    tpls = C.load_config_ro()["templates"]
    return [(p, tpls[p["template"]]) for p in s["plans"] if p["active"] and p["price"] > 0 and p["template"] in tpls]


# عرض دکمه‌های تلگرام هم‌اندازه‌ی حباب پیام است؛ یک خط نامرئی پهن، حباب (و دکمه‌ها) را عریض‌تر می‌کند
WIDE = "\u3164" * 42


def _plans_kb(prefix: str, extra: str = "") -> M | None:
    s = shop_cfg()
    rows = [[B(f"{p['name']} — {money(p['price'])} تومان", callback_data=f"{prefix}:{extra}{p['id']}")] for p in s["plans"] if p["active"] and p["price"] > 0]
    return M(rows + [[B("🔙 منوی اصلی", callback_data="home")]]) if rows else None


def _plans_text(title: str) -> str:
    return f"{title}\n{WIDE}"


async def show_plan(update: Update, context, pid: str, idx: int | None = None, reserve: bool = False) -> None:
    s = shop_cfg()
    p = plan_get(s, pid)
    tpl = C.load_config_ro()["templates"].get(p["template"]) if p else None
    if not p or not p["active"] or not tpl:
        await show(update, context, "این پلن در دسترس نیست.", back_kb())
        return
    cu = customer_get(update.effective_user.id) or {}
    renew = idx is not None
    sv = (cu.get("services") or [])[idx] if renew and idx < len(cu.get("services") or []) else None
    if renew and not sv:
        await show(update, context, "سرویس پیدا نشد.", back_kb())
        return
    head = (f"⏰ رزرو اشتراک بعدی برای <b>{esc(sv['u'])}</b>" if reserve else f"🔄 تمدید <b>{esc(sv['u'])}</b>") if renew else "🛒 خرید"
    price, price_line = p["price"], f"💰 قیمت: <b>{money(p['price'])}</b> تومان\n"
    dcode = _disc_code(context, pid)
    if dcode:
        try:
            _, off, price = await asyncio.to_thread(code_check, update.effective_user.id, dcode, pid)
            price_line = f"💰 قیمت: <s>{money(p['price'])}</s> ← <b>{money(price)}</b> تومان\n🏷 کد <code>{esc(dcode)}</code>: {money(off)} تومان تخفیف\n"
        except ValueError:
            context.user_data.pop("disc", None)
            dcode, price = "", p["price"]
    auto = _auto_ok(s, price)
    zp = _zp_ok(s, price)
    how = ("\n\n⚡ <b>خودکار:</b> بدون ارسال رسید؛ مبلغ دقیقِ نمایش‌داده‌شده را واریز کنید تا چند دقیقه بعد خودکار تأیید شود."
           "\n🧾 <b>دستی:</b> بعد از واریز عکس رسید می‌فرستید و ادمین تأیید می‌کند.") if auto else ""
    if reserve:
        how += "\n\n⏰ این اشتراک الان اعمال نمی‌شود؛ بعد از تمام شدن حجم یا زمان فعلی سرویس، خودکار شروع می‌شود."
    txt = (f"{head}\n\n📦 {esc(p['name'])}\n📐 {esc(_plan_line(tpl))}\n{price_line}"
           f"👛 موجودی شما: {money(cu.get('balance', 0))} تومان{how}\n\nروش پرداخت را انتخاب کنید:")
    pre = ("v" if reserve else "r") if renew else "b"
    wcb, ccb, acb, zcb = (f"{pre}w:{idx}:{pid}", f"{pre}c:{idx}:{pid}", f"{pre}a:{idx}:{pid}", f"{pre}z:{idx}:{pid}") if renew else (f"bw:{pid}", f"bc:{pid}", f"ba:{pid}", f"bz:{pid}")
    rows = []
    if cu.get("balance", 0) >= price:
        rows.append([B("👛 پرداخت از کیف پول (آنی)", callback_data=wcb)])
    if zp:
        rows.append([B("💳 پرداخت آنلاین (زرین‌پال)", callback_data=zcb)])
    if auto:
        rows.append([B("⚡ کارت‌به‌کارت (تأیید خودکار)", callback_data=acb)])
    rows.append([B("💳 کارت‌به‌کارت (تأیید دستی با رسید)" if auto else "💳 کارت‌به‌کارت (با رسید)", callback_data=ccb)])
    if any(d["active"] for d in s["codes"].values()):
        rows.append([B("🏷 تغییر کد تخفیف" if dcode else "🏷 کد تخفیف دارم", callback_data=f"dc:{pid}:{(idx + (1000 if reserve else 0)) if renew else -1}")])
    await show(update, context, txt, back_kb(rows))


async def _service_info(sv: dict) -> tuple[dict | None, object]:
    cfg = C.load_config_ro()
    pid = sv["pid"] if sv["pid"] and C.get_panel(cfg, sv["pid"]) else cfg["active_panel"]
    api = C.get_api(pid)
    try:
        u = await asyncio.wait_for(api.request("GET", api.upath(sv["u"])), 20)
        return (u if isinstance(u, dict) else None), api
    except Exception:  # noqa: BLE001
        return None, api


STATUS_ICON = {"active": "🟢", "disabled": "⚫", "expired": "🔴", "limited": "🟠", "on_hold": "⏳"}


async def show_service(update: Update, context, idx: int) -> None:
    cu = customer_get(update.effective_user.id) or {}
    svcs = cu.get("services") or []
    if idx >= len(svcs):
        await show(update, context, "سرویس پیدا نشد.", back_kb())
        return
    sv = svcs[idx]
    u, api = await _service_info(sv)
    if not u:
        await show(update, context, f"⚠️ اطلاعات <b>{esc(sv['u'])}</b> الان در دسترس نیست؛ کمی بعد دوباره امتحان کنید.",
                   back_kb([[B("🔄 تمدید", callback_data=f"rn:{idx}")]]))
        return
    st = u.get("status", "unknown")
    used, lim = u.get("used_traffic", 0) or 0, u.get("data_limit") or 0
    pct = f" ({used * 100 / lim:.0f}%)" if lim else ""
    sub = api.full_sub(u.get("subscription_url", "")) or "نامشخص"
    txt = (f"👤 <b>{esc(sv['u'])}</b>\n{STATUS_ICON.get(st, '❔')} وضعیت: {esc(st)}\n📊 مصرف: {C.fmt_gb(used)} / {C.fmt_gb(lim)}{pct}\n"
           f"⏳ اعتبار: {C.fmt_expire(u.get('expire'), st, u.get('on_hold_expire_duration'))}")
    rsv = shop_cfg()["reserve"]
    queue = C.next_plans_view(u) if rsv["enabled"] or C._raw_plans(u) else []
    if queue:
        txt += "\n⏰ رزرو شده: " + "، ".join(f"{x['gb'] or 'نامحدود'}{'GB' if x['gb'] else ''} / {x['days'] or 'نامحدود'}{' روز' if x['days'] else ''}" for x in queue)
    txt += f"\n\n🔗 <code>{esc(sub)}</code>"
    hu = C.happ_page_url(sub) if sub != "نامشخص" else ""
    rows = ([[B("📲 افزودن به Happ", url=hu)]] if hu else []) + [[B("🖼 QR", callback_data=f"sq:{idx}"), B("🔄 تمدید", callback_data=f"rn:{idx}")]]
    if rsv["enabled"] and len(queue) < rsv["max"] and _active_plans():
        rows.append([B("⏰ رزرو اشتراک بعدی", callback_data=f"rs:{idx}")])
    await show(update, context, txt, back_kb(rows + [[B("🔙 سرویس‌ها", callback_data="svc")]]))


def _disc_code(context, plan_id: str) -> str:
    d = context.user_data.get("disc")
    return d[0] if d and d[1] == plan_id else ""


async def _launch_card(update: Update, context, kind: str, plan_id: str = "", amount: int = 0, username: str = "", mode: str = "manual") -> None:
    try:
        o = await asyncio.to_thread(order_new, update.effective_user.id, kind, plan_id, amount, username, _disc_code(context, plan_id), mode)
        context.user_data.pop("disc", None)
    except ValueError as e:
        await show(update, context, f"❌ {esc(e)}", back_kb())
        return
    if o.get("mode") == "auto":
        await ask_auto(update, context, o)
    else:
        await ask_receipt(update, context, o)


def _zp_ok(s: dict, price: int) -> bool:
    return bool(Z.shop_ready(s["zp"]) and Z.MIN_TOMAN <= price <= s["zp"]["max_amount"])


async def _launch_zp(update: Update, context, kind: str, plan_id: str = "", amount: int = 0, username: str = "") -> None:
    """پرداخت آنلاین زرین‌پال: سفارش «منتظر پرداخت» + لینک پرداخت. تأیید خودکار با بازگشت از درگاه (یا پایشگر) انجام می‌شود."""
    uid = update.effective_user.id
    try:
        o = await asyncio.to_thread(order_new, uid, kind, plan_id, amount, username, _disc_code(context, plan_id), "zp")
    except ValueError as e:
        await show(update, context, f"❌ {esc(e)}", back_kb())
        return
    context.user_data.pop("disc", None)
    what = KIND_FA[o["kind"]] + (f" — {o.get('pname')}" if o.get("pname") else "")
    try:
        auth, url = await Z.request_payment(o["amount"], f"{what} #{o['id']}")
        await asyncio.to_thread(Z.pay_new, "order", o["id"], uid, o["amount"], auth)
    except (Z.ZPError, ValueError) as e:
        log.warning("zarinpal start failed (order %s): %s", o["id"], e)
        try:
            await asyncio.to_thread(order_cancel, o["id"], uid)
        except ValueError:
            pass
        await show(update, context, "❌ ساخت پرداخت آنلاین الان ممکن نشد. لطفاً دقایقی بعد دوباره امتحان کنید یا با کیف پول / کارت‌به‌کارت پرداخت کنید.", back_kb())
        await zp_warn_admins(None, "start", f"⚠️ <b>ساخت پرداخت زرین‌پال ناموفق بود</b>\n{esc(e)}\n\nتنظیمات درگاه را در وب‌پنل ← فروشگاه ← زرین‌پال بررسی کنید (دکمه‌ی «تست اتصال»).")
        return
    context.user_data.pop("aw", None)
    await show(update, context,
               f"💳 <b>پرداخت آنلاین — سفارش #{o['id']}</b>\n\n📌 {esc(what)}\n💰 مبلغ: <b>{money(o['amount'])}</b> تومان\n\n"
               f"دکمه‌ی «پرداخت» را بزنید و پرداخت را در درگاه کامل کنید. بعد از پرداخت، سفارش خودکار انجام می‌شود و همین‌جا خبر می‌گیرید.\n"
               f"⏳ مهلت: {shop_cfg()['zp']['minutes']} دقیقه\n\nاگر پرداخت کردید ولی پیامی نیامد، «🔄 بررسی وضعیت پرداخت» را بزنید.",
               M([[B("💳 پرداخت", url=url)], [B("🔄 بررسی وضعیت پرداخت", callback_data=f"zc:{o['id']}")],
                  [B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")], [B("🔙 منوی اصلی", callback_data="home")]]))


async def _launch_wallet(update: Update, context, kind: str, plan_id: str, username: str = "") -> None:
    try:
        o = await asyncio.to_thread(order_pay_wallet, update.effective_user.id, kind, plan_id, username, _disc_code(context, plan_id))
        context.user_data.pop("disc", None)
    except ValueError as e:
        await show(update, context, f"❌ {esc(e)}", back_kb())
        return
    await show(update, context, f"⚙️ پرداخت از کیف پول انجام شد؛ سفارش #{o['id']} در حال انجام است…", None)
    await fulfil(context.bot, o["id"])
    await context.bot.send_message(update.effective_chat.id, "🏠 منوی اصلی", reply_markup=main_kb(is_adm(update), update.effective_user.id))


async def _submit_receipt(update: Update, context, oid: str) -> None:
    m = update.effective_message
    file_id = fuid = ""
    if m.photo:
        file_id, fuid = m.photo[-1].file_id, m.photo[-1].file_unique_id
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        file_id, fuid = m.document.file_id, m.document.file_unique_id
    text = (m.caption or m.text or "").strip()
    if not file_id and len(text) < 4:
        await m.reply_html("لطفاً <b>عکس رسید</b> یا شماره پیگیری را بفرستید.")
        return
    try:
        o = await asyncio.to_thread(order_receipt, oid, update.effective_user.id, file_id, fuid, text)
    except ValueError as e:
        context.user_data.pop("aw", None)
        await m.reply_html(f"❌ {esc(e)}", reply_markup=back_kb())
        return
    context.user_data.pop("aw", None)
    await m.reply_html(f"✅ رسید سفارش <b>#{oid}</b> دریافت شد.\n" + ("اگر واریز شما در حساب ثبت شود خودکار تأیید می‌شود؛ در غیر این صورت ادمین بررسی می‌کند.\n" if o.get("mode") == "auto" else "")
                       + "نتیجه همین‌جا به شما اعلام می‌شود.", reply_markup=back_kb())
    sent = await _notify_admins(context.bot, order_text(o), order_kb(o), photo=file_id or None)
    await asyncio.to_thread(order_update, oid, adm=sent)


async def on_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    u = update.effective_user
    if not m or not u or update.effective_chat.type != "private":
        return
    if _throttled(u.id):
        return
    await asyncio.to_thread(customer_touch, u.id, u.full_name, u.username or "")
    block = _gate(update)
    if block:
        await m.reply_html(block)
        return
    if not await force_join_ok(update, context):
        return
    aw = context.user_data.get("aw")
    text = (m.text or "").strip()
    if text == MENU_BTN:
        await home(update, context)
        return
    if aw and aw[0] == "receipt":
        await _submit_receipt(update, context, aw[1])
        return
    if aw and aw[0] == "code":
        try:
            c_, _, _ = await asyncio.to_thread(code_check, u.id, text, aw[1])
        except ValueError as e:
            await m.reply_html(f"❌ {esc(e)}\nدوباره بفرستید یا از منو برگردید.", reply_markup=back_kb())
            return
        context.user_data["disc"] = (c_, aw[1])
        context.user_data.pop("aw", None)
        if aw[2] >= 1000:                               # ۱۰۰۰+ = رزرو اشتراک برای سرویس شماره‌ی (n-1000)
            await show_plan(update, context, aw[1], aw[2] - 1000, True)
        else:
            await show_plan(update, context, aw[1], aw[2] if aw[2] >= 0 else None)
        return
    if aw and aw[0] == "amount":
        digits = re.sub(r"\D", "", text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
        if not digits:
            await m.reply_html("مبلغ را به <b>تومان</b> و فقط با عدد بفرستید. مثال: <code>200000</code>")
            return
        amt = int(digits)
        s_ = shop_cfg()
        zp_on, au_on = _zp_ok(s_, amt), _auto_ok(s_, amt)
        if zp_on or au_on:
            if not s_["min_topup"] <= amt <= s_["max_topup"]:
                await m.reply_html(f"❌ مبلغ شارژ باید بین {money(s_['min_topup'])} و {money(s_['max_topup'])} تومان باشد.")
                return
            context.user_data.pop("aw", None)
            info = (["💳 <b>آنلاین:</b> پرداخت با درگاه بانکی؛ بعد از پرداخت، کیف پول فوراً شارژ می‌شود."] if zp_on else []) \
                + (["⚡ <b>خودکار:</b> بدون رسید؛ مبلغ دقیق را کارت‌به‌کارت کنید تا چند دقیقه بعد خودکار تأیید شود."] if au_on else []) \
                + ["🧾 <b>دستی:</b> عکس رسید می‌فرستید و ادمین تأیید می‌کند."]
            rows = ([[B("💳 پرداخت آنلاین (زرین‌پال)", callback_data=f"tz:{amt}")]] if zp_on else []) + ([[B("⚡ تأیید خودکار", callback_data=f"ta:{amt}")]] if au_on else []) \
                + [[B("💳 تأیید دستی با رسید", callback_data=f"tm:{amt}")], [B("🔙 منوی اصلی", callback_data="home")]]
            await m.reply_html(f"➕ <b>شارژ کیف پول — {money(amt)} تومان</b>\n\n" + "\n".join(info) + "\n\nروش پرداخت را انتخاب کنید:", reply_markup=M(rows))
            return
        try:
            o = await asyncio.to_thread(order_new, u.id, "topup", "", amt)
        except ValueError as e:
            await m.reply_html(f"❌ {esc(e)}")
            return
        context.user_data["aw"] = ("receipt", o["id"])
        await m.reply_html(f"🧾 <b>شارژ کیف پول #{o['id']}</b>\n\n{_card_text(o)}\n📸 بعد از پرداخت، <b>عکس رسید</b> را همین‌جا بفرستید.",
                           reply_markup=M([[B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")]]))
        return
    if aw and aw[0].startswith("a_") and is_adm(update):
        await admin_text(update, context, aw)
        return
    if text and not (aw and aw[0].startswith("a_")) and await handle_link(update, context, text):
        return
    if aw and aw[0] == "link":
        await m.reply_html("لینک ساب معتبر نبود. لینک کامل را بفرستید (مثلاً <code>https://…/sub/…</code>).", reply_markup=back_kb())
        return
    if m.photo:                                  # عکسِ بدون حالت: اگر سفارش منتظر رسید دارد همان را می‌گیریم
        mine = [o for o in shop_cfg()["orders"] if o["uid"] == u.id and o["status"] == "new" and o.get("mode") != "zp"]
        if mine:
            await _submit_receipt(update, context, mine[-1]["id"])
            return
    await m.reply_html("از منوی زیر استفاده کنید 👇", reply_markup=main_kb(is_adm(update), update.effective_user.id))


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    u = update.effective_user
    if not q or not u:
        return
    if _throttled(u.id):
        await _ans(q, "کمی آهسته‌تر 🙂")
        return
    data = q.data or ""
    p = data.split(":")
    cmd = p[0]
    if cmd == "ad":
        if not is_adm(update):
            await _ans(q, "⛔ دسترسی ندارید", show_alert=True)
            return
        await admin_cb(update, context, p)
        return
    await asyncio.to_thread(customer_touch, u.id, u.full_name, u.username or "")
    block = _gate(update)
    if block:
        await _ans(q, block, show_alert=True)
        return
    if cmd == "chk":
        _member_ok.pop(u.id, None)
        _trust[u.id] = time.time()          # فقط وقتی اثر دارد که بررسی عضویت خطا بدهد (ربات ادمین کانال نیست)
    if not await force_join_ok(update, context):
        return
    await _ans(q, )
    s = shop_cfg()
    cu = customer_get(u.id) or {}
    if cmd == "chk":
        await context.bot.send_message(update.effective_chat.id, "⌨️ برای برگشت به منو، هر وقت دکمه‌ی «🏠 منوی اصلی» پایین صفحه را بزنید.", reply_markup=_menu_reply_kb())
        await home(update, context, "✅ عضویت شما تأیید شد.\n\n")
    elif cmd == "home":
        await home(update, context)
    elif cmd == "buy":
        kb = _plans_kb("bp")
        await show(update, context, _plans_text("🛒 <b>پلن مورد نظر را انتخاب کنید:</b>") if kb else "هنوز پلنی برای فروش تعریف نشده است.", kb or back_kb())
    elif cmd == "bp":
        await show_plan(update, context, p[1])
    elif cmd == "bw":
        await _launch_wallet(update, context, "buy", p[1])
    elif cmd == "bc":
        await _launch_card(update, context, "buy", p[1])
    elif cmd == "ba":
        await _launch_card(update, context, "buy", p[1], mode="auto")
    elif cmd == "bz":
        await _launch_zp(update, context, "buy", p[1])
    elif cmd == "tz":
        await _launch_zp(update, context, "topup", "", int(p[1]))
    elif cmd in ("rz", "vz", "vw", "vc", "va"):
        i = int(p[1])
        svcs = cu.get("services") or []
        if i >= len(svcs):
            await show(update, context, "سرویس پیدا نشد.", back_kb())
        elif cmd == "rz":
            await _launch_zp(update, context, "renew", p[2], 0, svcs[i]["u"])
        elif cmd == "vz":
            await _launch_zp(update, context, "reserve", p[2], 0, svcs[i]["u"])
        elif cmd == "vw":
            await _launch_wallet(update, context, "reserve", p[2], svcs[i]["u"])
        else:
            await _launch_card(update, context, "reserve", p[2], 0, svcs[i]["u"], mode="auto" if cmd == "va" else "manual")
    elif cmd == "rs":
        i = int(p[1])
        svcs = cu.get("services") or []
        rsv = s["reserve"]
        if i >= len(svcs) or not rsv["enabled"]:
            await show(update, context, "رزرو اشتراک فعلاً در دسترس نیست.", back_kb())
        else:
            info, _api = await _service_info(svcs[i])
            view = C.next_plans_view(info) if info else None
            kb = _plans_kb("vp", f"{i}:")
            if view is None:
                await show(update, context, "⚠️ اطلاعات سرویس الان در دسترس نیست؛ کمی بعد دوباره امتحان کنید.", back_kb())
            elif len(view) >= rsv["max"]:
                await show(update, context, f"برای <b>{esc(svcs[i]['u'])}</b> از قبل {len(view)} اشتراک رزرو شده است (حداکثر {rsv['max']}). بعد از شروع آن‌ها می‌توانید دوباره رزرو کنید.", back_kb([[B("🔙 سرویس", callback_data=f"sv:{i}")]]))
            elif any(x.get("broken") for x in view):
                await show(update, context, "رزروهای قبلی این سرویس نیاز به بررسی ادمین دارد؛ به پشتیبانی اطلاع دهید.", back_kb())
            else:
                await show(update, context, _plans_text(f"⏰ <b>رزرو اشتراک بعدی برای {esc(svcs[i]['u'])}</b>\nبعد از تمام شدن حجم یا زمان فعلی، این اشتراک خودکار شروع می‌شود.") if kb else "پلنی برای رزرو تعریف نشده است.", kb or back_kb())
    elif cmd == "vp":
        await show_plan(update, context, p[2], int(p[1]), True)
    elif cmd == "zc":                                  # بررسی وضعیت پرداخت آنلاین (اگر بازگشت از درگاه نرسیده باشد همین‌جا verify می‌شود)
        o = order_get(p[1])
        if not o or o["uid"] != u.id:
            await show(update, context, "سفارش پیدا نشد.", back_kb())
        elif o["status"] != "new" or not o.get("zp"):
            await show(update, context, order_text(o), back_kb([[B("🧾 سفارش‌ها", callback_data="ords")]]))
        else:
            res = await Z.finalize(o["zp"], None)
            if res["state"] in ("order_ok", "credited", "stuck"):
                await zp_after(context.bot, res)
            elif res["state"] in ("failed", "unpaid", "retry"):
                await _ans(q, "هنوز پرداختی تأیید نشده. اگر پرداخت کرده‌اید چند دقیقه صبر کنید؛ خودکار اعمال می‌شود.", show_alert=True)
            else:
                cur = order_get(p[1])
                await show(update, context, order_text(cur) if cur else "سفارش پیدا نشد.", back_kb([[B("🧾 سفارش‌ها", callback_data="ords")]]))
    elif cmd in ("ta", "tm"):
        await _launch_card(update, context, "topup", "", int(p[1]), mode="auto" if cmd == "ta" else "manual")
    elif cmd == "ar":                                  # واریز کرده ولی خودکار تأیید نشد ← مسیر دستی با رسید
        o = order_get(p[1])
        if not o or o["uid"] != u.id or o["status"] != "new":
            await show(update, context, "این سفارش دیگر منتظر پرداخت نیست.", back_kb())
        else:
            context.user_data["aw"] = ("receipt", o["id"])
            await show(update, context, f"🧾 <b>سفارش #{o['id']}</b>\n\n📸 <b>عکس رسید</b> را همین‌جا بفرستید (یا شماره پیگیری را بنویسید). ادمین بررسی و تأیید می‌کند.",
                       M([[B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")], [B("🔙 منوی اصلی", callback_data="home")]]))
    elif cmd == "ac":                                  # بررسی وضعیت سفارش خودکار
        o = order_get(p[1])
        if not o or o["uid"] != u.id:
            await show(update, context, "سفارش پیدا نشد.", back_kb())
        elif o["status"] == "new":
            await _ans(q, "هنوز پرداختی با این مبلغ شناسایی نشده. بعد از واریز چند دقیقه صبر کنید.", show_alert=True)
        else:
            await show(update, context, order_text(o), back_kb([[B("🧾 سفارش‌ها", callback_data="ords")]]))
    elif cmd == "wal":
        await show(update, context, f"👛 <b>کیف پول</b>\n\nموجودی: <b>{money(cu.get('balance', 0))}</b> تومان",
                   back_kb([[B("➕ شارژ با رسید", callback_data="wt"), B("🧾 تراکنش‌ها", callback_data="wh")]]))
    elif cmd == "wt":
        context.user_data["aw"] = ("amount",)
        await show(update, context, f"➕ <b>شارژ کیف پول</b>\n\nمبلغ را به <b>تومان</b> بفرستید ({money(s['min_topup'])} تا {money(s['max_topup'])}).", back_kb())
    elif cmd == "wh":
        log_ = (cu.get("log") or [])[-10:][::-1]
        rows = [f"{sales.jdatetime(x['ts'])}\n   {'+' if x['amount'] > 0 else ''}{money(x['amount'])} — {esc(x.get('note') or x['type'])}" for x in log_]
        await show(update, context, "🧾 <b>آخرین تراکنش‌ها</b>\n\n" + ("\n".join(rows) if rows else "هنوز تراکنشی ندارید."), back_kb([[B("👛 کیف پول", callback_data="wal")]]))
    elif cmd == "ords":
        mine = [o for o in s["orders"] if o["uid"] == u.id][-8:][::-1]
        btn = [[B(f"#{o['id']} {KIND_FA[o['kind']]} — {STATUS_FA.get(o['status'], '')}", callback_data=f"ord:{o['id']}")] for o in mine]
        await show(update, context, "🧾 <b>سفارش‌های من</b>" if mine else "هنوز سفارشی ندارید.", back_kb(btn))
    elif cmd == "ord":
        o = order_get(p[1])
        if not o or o["uid"] != u.id:
            await show(update, context, "سفارش پیدا نشد.", back_kb())
        elif o["status"] == "new" and o.get("mode") == "zp":
            await show(update, context, order_text(o), back_kb([[B("🔄 بررسی وضعیت پرداخت", callback_data=f"zc:{o['id']}")], [B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")]]))
        elif o["status"] == "new":
            await (ask_auto if o.get("mode") == "auto" else ask_receipt)(update, context, o)
        else:
            await show(update, context, order_text(o), back_kb([[B("🧾 سفارش‌ها", callback_data="ords")]]))
    elif cmd == "oc":
        try:
            await asyncio.to_thread(order_cancel, p[1], u.id)
            context.user_data.pop("aw", None)
            await home(update, context, "🚫 سفارش لغو شد.\n\n")
        except ValueError as e:
            await show(update, context, f"❌ {esc(e)}", back_kb())
    elif cmd == "svc":
        svcs = cu.get("services") or []
        rows = [[B(f"👤 {sv['u']}", callback_data=f"sv:{i}")] for i, sv in enumerate(svcs[:30])]
        await show(update, context, "🔄 <b>سرویس‌های من</b>" if svcs else "هنوز سرویسی ندارید. از «🛒 خرید کانفیگ» شروع کنید، یا لینک ساب سرویس قبلی‌تان را همین‌جا بفرستید.", back_kb(rows))
    elif cmd == "sv":
        await show_service(update, context, int(p[1]))
    elif cmd == "sq":
        svcs = cu.get("services") or []
        i = int(p[1])
        if i < len(svcs):
            info, api = await _service_info(svcs[i])
            sub = api.full_sub((info or {}).get("subscription_url", "")) if info else ""
            if sub:
                png = await asyncio.to_thread(C.make_qr_png, sub)
                await context.bot.send_photo(update.effective_chat.id, png, caption=f"<b>{esc(svcs[i]['u'])}</b>\n<code>{esc(sub)}</code>", parse_mode=HTML)
            else:
                await _ans(q, "لینک در دسترس نیست.", show_alert=True)
    elif cmd == "rn":
        i = int(p[1])
        svcs = cu.get("services") or []
        kb = _plans_kb("rp", f"{i}:") if i < len(svcs) else None
        title = _plans_text(f"🔄 <b>پلن تمدید برای {esc(svcs[i]['u'])}</b>") if kb else "پلنی برای تمدید تعریف نشده است."
        await show(update, context, title, kb or back_kb())
    elif cmd == "rp":
        await show_plan(update, context, p[2], int(p[1]))
    elif cmd in ("rw", "rc"):
        i = int(p[1])
        svcs = cu.get("services") or []
        if i >= len(svcs):
            await show(update, context, "سرویس پیدا نشد.", back_kb())
        elif cmd == "rw":
            await _launch_wallet(update, context, "renew", p[2], svcs[i]["u"])
        else:
            await _launch_card(update, context, "renew", p[2], 0, svcs[i]["u"])
    elif cmd == "ra":
        i = int(p[1])
        svcs = cu.get("services") or []
        if i >= len(svcs):
            await show(update, context, "سرویس پیدا نشد.", back_kb())
        else:
            await _launch_card(update, context, "renew", p[2], 0, svcs[i]["u"], mode="auto")
    elif cmd == "dc":
        context.user_data["aw"] = ("code", p[1], int(p[2]))
        await show(update, context, "🏷 <b>کد تخفیف</b> را بفرستید:", back_kb())
    elif cmd == "tst":
        await give_test(update, context)
    elif cmd == "lnk":
        context.user_data["aw"] = ("link",)
        await show(update, context, "🔗 <b>اتصال سرویس قبلی</b>\n\nلینک ساب (اشتراک) سرویس خود را همین‌جا بفرستید تا به حساب شما وصل شود و بتوانید از «سرویس‌های من» تمدیدش کنید.", back_kb())
    elif cmd == "sup":
        await show(update, context, "☎️ <b>پشتیبانی</b>\n\n" + (esc(s["support"]) if s["support"] else "پشتیبانی هنوز تنظیم نشده است."), back_kb())
    else:
        await _ans(q, "؟")


# ═════════════════════════════ مدیریت (فقط ادمین) ═════════════════════════════
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_adm(update):
        await update.effective_message.reply_html(f"⛔ دسترسی ندارید.\nشناسه شما: <code>{update.effective_user.id}</code>")
        return
    await admin_home(update, context)


async def admin_home(update: Update, context, prefix: str = "") -> None:
    context.user_data.pop("aw", None)
    s = shop_cfg()
    pend = sum(1 for o in s["orders"] if o["status"] == "pending")
    fail = sum(1 for o in s["orders"] if o["status"] == "failed")
    total = sum(c["balance"] for c in s["customers"].values())
    pn = C.get_panel(C.load_config_ro(), shop_panel(C.load_config_ro())) or {}
    txt = (f"{prefix}⚙️ <b>مدیریت فروشگاه</b>\n\n{'🟢 باز' if s['enabled'] else '🔴 بسته'}  |  🖥 پنل فروش: <b>{esc(pn.get('name', '—'))}</b>\n"
           f"👥 مشتری: {len(s['customers'])}  |  📦 پلن فعال: {sum(1 for p in s['plans'] if p['active'])}\n💳 کارت: {'✅ تنظیم شده' if s['card']['number'] else '⚠️ تنظیم نشده'}\n"
           f"👛 مجموع موجودی مشتری‌ها: {money(total)} تومان")
    kb = M([[B(f"📥 رسیدهای در انتظار ({pend})", callback_data="ad:pend")],
            [B(f"⚠️ سفارش‌های ناموفق ({fail})", callback_data="ad:fail")],
            [B("👥 مشتری‌ها", callback_data="ad:cu"), B("📦 پلن‌ها", callback_data="ad:plans")],
            [B("💳 کارت", callback_data="ad:card"), B("🖥 پنل فروش", callback_data="ad:pn")],
            [B("⚡ تأیید خودکار: " + ("🟢 روشن" if s["auto"]["enabled"] else "🔴 خاموش"), callback_data="ad:au")],
            [B("💳 زرین‌پال ربات: " + ("🟢 روشن" if s["zp"]["shop_on"] else "🔴 خاموش"), callback_data="ad:zpt"), B("⏰ رزرو: " + ("🟢 روشن" if s["reserve"]["enabled"] else "🔴 خاموش"), callback_data="ad:rst")],
            [B("✏️ متن خوش‌آمد", callback_data="ad:wel"), B("✏️ متن پشتیبانی", callback_data="ad:sup")],
            [B("🔴 بستن فروشگاه" if s["enabled"] else "🟢 باز کردن فروشگاه", callback_data="ad:tog")],
            [B("🔙 منوی مشتری", callback_data="home")]])
    await show(update, context, txt, kb)


def _adm_back(extra=None) -> M:
    return M(list(extra or []) + [[B("⚙️ مدیریت", callback_data="ad")]])


async def _send_order_admin(context, chat_id: int, o: dict) -> None:
    """پیام کامل یک سفارش (با عکس رسید و دکمه‌ها) برای همین ادمین."""
    kb, text = order_kb(o), order_text(o)
    try:
        if o.get("file"):
            m = await context.bot.send_photo(chat_id, o["file"], caption=text[:1020], parse_mode=HTML, reply_markup=kb)
            mid, ph = m.message_id, True
        else:
            m = await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
            mid, ph = m.message_id, False
    except TelegramError:
        m = await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
        mid, ph = m.message_id, False
    if kb:                                       # این پیام هم بعد از تصمیم به‌روز شود
        def mut(c):
            x = _find(c["shop"], o["id"])
            if x is not None and [chat_id, mid, ph] not in x.get("adm", []):
                x["adm"] = list(x.get("adm", [])) + [[chat_id, mid, ph]]
        await asyncio.to_thread(C.update_config, mut)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, p: list) -> None:
    q = update.callback_query
    admin = update.effective_user
    sub = p[1] if len(p) > 1 else ""
    s = shop_cfg()
    if sub == "":
        await _ans(q, )
        await admin_home(update, context)
    elif sub in ("pend", "fail"):
        await _ans(q, )
        want = "pending" if sub == "pend" else "failed"
        items = [o for o in s["orders"] if o["status"] == want][-15:][::-1]
        rows = [[B(f"#{o['id']} {KIND_FA[o['kind']]} {money(o['amount'])}", callback_data=f"ad:o:{o['id']}")] for o in items]
        await show(update, context, ("📥 <b>رسیدهای در انتظار</b>" if want == "pending" else "⚠️ <b>سفارش‌های ناموفق</b>") + ("" if items else "\n\nمورد بازی نیست ✅"), _adm_back(rows))
    elif sub == "o":
        await _ans(q, )
        o = order_get(p[2])
        if o:
            await _send_order_admin(context, update.effective_chat.id, o)
    elif sub == "ok":
        try:
            o = await asyncio.to_thread(order_claim, p[2], admin.id, ("pending",))
        except ValueError as e:
            await _ans(q, str(e), show_alert=True)
            cur = order_get(p[2])
            if cur:
                await _edit_admin_msgs(context.bot, cur)
            return
        await _ans(q, "در حال انجام…")
        await _edit_admin_msgs(context.bot, o, f"⚙️ در حال انجام توسط {esc(admin.full_name)}…")
        await fulfil(context.bot, o["id"])
    elif sub == "retry":
        try:
            o = await asyncio.to_thread(order_claim, p[2], admin.id, ("failed",))
        except ValueError as e:
            await _ans(q, str(e), show_alert=True)
            return
        await _ans(q, "تلاش مجدد…")
        await _edit_admin_msgs(context.bot, o, f"⚙️ تلاش مجدد توسط {esc(admin.full_name)}…")
        await fulfil(context.bot, o["id"])
    elif sub == "cr":                              # اعتبار به کیف پول به‌جای انجام سفارش
        try:
            o = await asyncio.to_thread(order_claim, p[2], admin.id, ("failed",))
            w = await asyncio.to_thread(wallet_apply, o["uid"], o["amount"], "refund", f"اعتبار سفارش #{o['id']}", "credit:" + o["id"])
            o = await asyncio.to_thread(order_update, o["id"], status="credited", dts=int(time.time())) or o
        except ValueError as e:
            await _ans(q, str(e), show_alert=True)
            return
        await _ans(q, "به کیف پول مشتری اضافه شد.")
        await _tell(context.bot, o["uid"], f"💰 سفارش #{o['id']} انجام نشد و مبلغ <b>{money(o['amount'])}</b> تومان به کیف پول شما اضافه شد.\nموجودی: <b>{money(w['balance'])}</b> تومان")
        await _edit_admin_msgs(context.bot, o, f"💰 توسط {esc(admin.full_name)} به کیف پول مشتری اضافه شد.")
    elif sub == "nom":
        await _ans(q, )
        rows = [[B(txt, callback_data=f"ad:no:{p[2]}:{k}")] for k, txt in REJECT_REASONS.items()]
        rows.append([B("🔙 برگشت", callback_data=f"ad:o:{p[2]}")])
        try:
            await q.edit_message_reply_markup(reply_markup=M(rows))
        except TelegramError:
            pass
    elif sub == "no":
        reason = REJECT_REASONS.get(p[3] if len(p) > 3 else "a", REJECT_REASONS["a"])
        try:
            o = await asyncio.to_thread(order_reject, p[2], admin.id, reason)
        except ValueError as e:
            await _ans(q, str(e), show_alert=True)
            return
        await _ans(q, "رد شد.")
        await _tell(context.bot, o["uid"], f"❌ <b>رسید سفارش #{o['id']} رد شد.</b>\nدلیل: {esc(reason)}\n\nاگر پرداخت کرده‌اید با پشتیبانی تماس بگیرید.")
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "رد رسید", o["id"], reason, cat="wallet")
        await _edit_admin_msgs(context.bot, o, f"❌ توسط {esc(admin.full_name)} رد شد.")
    elif sub in ("au", "aut"):
        a = s["auto"]
        if sub == "aut":
            if not a["enabled"] and len(a["secret"]) < 24:
                await _ans(q, "ابتدا از وب‌پنل (تنظیمات فروشگاه ← تأیید خودکار) رمز وب‌هوک بسازید.", show_alert=True)
                return
            if not a["enabled"] and not s["cards"]:
                await _ans(q, "ابتدا یک کارت ثبت کنید.", show_alert=True)
                return
            await asyncio.to_thread(C.update_config, lambda c: c["shop"]["auto"].update(enabled=not c["shop"]["auto"]["enabled"]))
            C.audit_event(f"ادمین فروشگاه ({admin.id})", "تغییر تأیید خودکار", "", "روشن" if not a["enabled"] else "خاموش", cat="sys")
            s = shop_cfg()
            a = s["auto"]
        await _ans(q, )
        lg = "\n".join(f"{sales.jdatetime(x['ts'])} — {x['res']}" + (f" #{x['oid']}" if x.get("oid") else "") + (f" {money(x['amount'])}" if x.get("amount") else "") for x in a["log"][-6:][::-1])
        txt = (f"⚡ <b>تأیید خودکار کارت‌به‌کارت</b>\n\nوضعیت: {'🟢 روشن' if a['enabled'] else '🔴 خاموش'}\nرمز وب‌هوک: {'✅ ساخته شده' if len(a['secret']) >= 24 else '⚠️ ساخته نشده (از وب‌پنل بسازید)'}\n"
               f"مهلت واریز: {a['minutes']} دقیقه | سقف هر سفارش: {money(a['max_amount'])} | سقف روزانه: {money(a['daily_max'])}\n\n"
               f"آخرین رویدادها:\n{lg or 'هنوز پیامکی نرسیده.'}\n\nتنظیم دقیق و آدرس وب‌هوک: وب‌پنل ← تنظیمات فروشگاه.")
        await show(update, context, txt, _adm_back([[B("🔴 خاموش کردن" if a["enabled"] else "🟢 روشن کردن", callback_data="ad:aut")]]))
    elif sub == "zpt":
        if not s["zp"]["shop_on"] and not s["zp"]["merchant"]:
            await _ans(q, "ابتدا مرچنت‌کد زرین‌پال را از وب‌پنل (فروشگاه ← زرین‌پال) ثبت کنید.", show_alert=True)
            return
        await asyncio.to_thread(C.update_config, lambda c: c["shop"]["zp"].update(shop_on=not c["shop"]["zp"]["shop_on"]))
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "تغییر زرین‌پال ربات فروش", "", "روشن" if not s["zp"]["shop_on"] else "خاموش", cat="sys")
        await _ans(q, )
        await admin_home(update, context)
    elif sub == "rst":
        await asyncio.to_thread(C.update_config, lambda c: c["shop"]["reserve"].update(enabled=not c["shop"]["reserve"]["enabled"]))
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "تغییر رزرو اشتراک", "", "روشن" if not s["reserve"]["enabled"] else "خاموش", cat="sys")
        await _ans(q, )
        await admin_home(update, context)
    elif sub == "tog":
        await _ans(q, )
        await asyncio.to_thread(C.update_config, lambda c: c["shop"].update(enabled=not c["shop"]["enabled"]))
        await admin_home(update, context)
    elif sub == "pn":
        await _ans(q, )
        cfg = C.load_config_ro()
        cur = shop_panel(cfg)
        rows = [[B(("✅ " if pn["id"] == cur else "") + pn["name"], callback_data=f"ad:pns:{pn['id']}")] for pn in cfg["panels"]]
        await show(update, context, "🖥 <b>پنلی که اکانت مشتری‌ها روی آن ساخته می‌شود:</b>", _adm_back(rows))
    elif sub == "pns":
        cfg = C.load_config_ro()
        if not C.get_panel(cfg, p[2]):
            await _ans(q, "پنل پیدا نشد.", show_alert=True)
            return
        await asyncio.to_thread(C.update_config, lambda c: c["shop"].update(panel=p[2]))
        await _ans(q, "ذخیره شد.")
        await admin_home(update, context)
    elif sub == "card":
        await _ans(q, )
        context.user_data.pop("aw", None)
        cards = s["cards"]
        rows = [[B(f"🗑 حذف {i}. ****{c['number'][-4:]}" + (f" — {c['holder']}" if c["holder"] else ""), callback_data=f"ad:cdel:{i - 1}")] for i, c in enumerate(cards, 1)]
        if len(cards) < C.SHOP_CARDS_MAX:
            rows.append([B("➕ افزودن کارت", callback_data="ad:cadd")])
        txt = "💳 <b>کارت‌های فروشگاه</b>\n\n" + ("\n".join(f"{i}. <code>{c['number']}</code> {esc(c['holder'])} {esc(c['bank'])}" for i, c in enumerate(cards, 1)) or "هنوز کارتی ثبت نشده.") + "\n\nمشتری همه‌ی کارت‌ها را می‌بیند و به یکی از آن‌ها واریز می‌کند."
        await show(update, context, txt, _adm_back(rows))
    elif sub == "cadd":
        await _ans(q, )
        context.user_data["aw"] = ("a_card",)
        await show(update, context, "💳 اطلاعات کارت را در <b>سه خط</b> بفرستید:\n\n<code>6037991234567890\nعلی رضایی\nبانک ملی</code>\n\n(خط دوم و سوم اختیاری است)", _adm_back())
    elif sub == "cdel":
        i = int(p[2])
        def _del(c):
            if 0 <= i < len(c["shop"]["cards"]):
                c["shop"]["cards"].pop(i)
                c["shop"]["card"] = dict(c["shop"]["cards"][0]) if c["shop"]["cards"] else {"number": "", "holder": "", "bank": ""}
        await asyncio.to_thread(C.update_config, _del)
        await _ans(q, "حذف شد.")
        await admin_cb(update, context, ["ad", "card"])
    elif sub in ("wel", "sup"):
        await _ans(q, )
        context.user_data["aw"] = ("a_wel" if sub == "wel" else "a_sup",)
        await show(update, context, "✏️ متن جدید را بفرستید (برای پاک کردن بنویسید <code>-</code>).", _adm_back())
    elif sub == "plans":
        await _ans(q, )
        rows = [[B(("🟢 " if x["active"] else "⚪ ") + f"{x['name']} — {money(x['price'])}", callback_data=f"ad:pl:{x['id']}")] for x in s["plans"]]
        rows.append([B("➕ پلن جدید", callback_data="ad:pa")])
        await show(update, context, "📦 <b>پلن‌های فروش</b>\nهر پلن به یک تمپلت وصل است (حجم و مدت از همان می‌آید).", _adm_back(rows))
    elif sub == "pa":
        await _ans(q, )
        tpls = list(C.load_config_ro()["templates"].items())
        rows = [[B(f"{n} ({_plan_line(t)})", callback_data=f"ad:pat:{i}")] for i, (n, t) in enumerate(tpls[:40])]
        await show(update, context, "تمپلتِ این پلن را انتخاب کنید:", _adm_back(rows))
    elif sub == "pat":
        await _ans(q, )
        tpls = list(C.load_config_ro()["templates"].keys())
        i = int(p[2])
        if i >= len(tpls):
            return
        context.user_data["aw"] = ("a_newplan", tpls[i])
        await show(update, context, f"💰 قیمت پلن «{esc(tpls[i])}» را به <b>تومان</b> بفرستید:", _adm_back())
    elif sub in ("pl", "pt", "pd", "pdy", "ppr", "prn"):
        pl = plan_get(s, p[2])
        if not pl:
            await _ans(q, "پلن پیدا نشد.", show_alert=True)
            return
        if sub == "pt":
            await asyncio.to_thread(C.update_config, lambda c: plan_get(c["shop"], p[2]).update(active=not pl["active"]))
        elif sub == "pdy":
            await asyncio.to_thread(C.update_config, lambda c: c["shop"].update(plans=[x for x in c["shop"]["plans"] if x["id"] != p[2]]))
            await _ans(q, "حذف شد.")
            await admin_cb(update, context, ["ad", "plans"])
            return
        elif sub in ("ppr", "prn"):
            await _ans(q, )
            context.user_data["aw"] = ("a_plprice" if sub == "ppr" else "a_plname", p[2])
            await show(update, context, "قیمت جدید (تومان) را بفرستید:" if sub == "ppr" else "نام نمایشی جدید را بفرستید:", _adm_back())
            return
        elif sub == "pd":
            await _ans(q, )
            await show(update, context, f"🗑 پلن «{esc(pl['name'])}» حذف شود؟ (سفارش‌های قبلی دست‌نخورده می‌ماند)",
                       _adm_back([[B("✅ حذف", callback_data=f"ad:pdy:{p[2]}"), B("❌ نه", callback_data=f"ad:pl:{p[2]}")]]))
            return
        await _ans(q, )
        pl = plan_get(shop_cfg(), p[2])
        await show(update, context, f"📦 <b>{esc(pl['name'])}</b>\nتمپلت: {esc(pl['template'])}\nقیمت: {money(pl['price'])} تومان\nوضعیت: {'🟢 فعال' if pl['active'] else '⚪ غیرفعال'}",
                   _adm_back([[B("⚪ غیرفعال/🟢 فعال", callback_data=f"ad:pt:{pl['id']}")], [B("💰 قیمت", callback_data=f"ad:ppr:{pl['id']}"), B("✏️ نام", callback_data=f"ad:prn:{pl['id']}")],
                              [B("🗑 حذف", callback_data=f"ad:pd:{pl['id']}")], [B("📦 پلن‌ها", callback_data="ad:plans")]]))
    elif sub == "cu":
        await _ans(q, )
        context.user_data["aw"] = ("a_cust",)
        recent = sorted(s["customers"].items(), key=lambda kv: -kv[1]["joined"])[:6]
        rows = [[B(f"{c['name'] or 'بدون‌نام'} — {money(c['balance'])}", callback_data=f"ad:c:{uid}")] for uid, c in recent]
        await show(update, context, "👥 <b>مشتری‌ها</b>\nشناسه‌ی عددی، @یوزرنیم یا بخشی از نام را بفرستید، یا از آخرین‌ها انتخاب کنید:", _adm_back(rows))
    elif sub == "c":
        await _ans(q, )
        await _show_customer(update, context, p[2])
    elif sub in ("cc", "cd"):
        await _ans(q, )
        context.user_data["aw"] = ("a_adj", p[2], 1 if sub == "cc" else -1)
        await show(update, context, ("➕ مبلغ <b>شارژ</b>" if sub == "cc" else "➖ مبلغ <b>کسر</b>") + " را به تومان بفرستید:", _adm_back())
    elif sub == "cb":
        await _ans(q, )
        uid = p[2]
        await asyncio.to_thread(C.update_config, lambda c: c["shop"]["customers"][uid].update(banned=not c["shop"]["customers"][uid]["banned"]))
        await _show_customer(update, context, uid)
    else:
        await _ans(q, "؟")


async def _show_customer(update: Update, context, uid: str) -> None:
    cu = customer_get(uid)
    if not cu:
        await show(update, context, "مشتری پیدا نشد.", _adm_back())
        return
    svcs = ", ".join(x["u"] for x in cu["services"][-10:]) or "—"
    n_orders = sum(1 for o in shop_cfg()["orders"] if str(o["uid"]) == str(uid))
    txt = (f"👤 {label(cu, uid)}\n👛 موجودی: <b>{money(cu['balance'])}</b> تومان\n📦 سرویس‌ها: <code>{esc(svcs)}</code>\n🧾 سفارش‌ها: {n_orders}\n"
           f"📅 عضویت: {sales.jdatetime(cu['joined']) if cu['joined'] else '—'}\n{'⛔ مسدود' if cu['banned'] else '🟢 آزاد'}")
    await show(update, context, txt, _adm_back([[B("➕ شارژ", callback_data=f"ad:cc:{uid}"), B("➖ کسر", callback_data=f"ad:cd:{uid}")],
                                               [B("✅ رفع مسدودی" if cu["banned"] else "🚫 مسدود", callback_data=f"ad:cb:{uid}")]]))


def _digits(text: str) -> int | None:
    d = re.sub(r"\D", "", (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
    return int(d) if d else None


async def admin_text(update: Update, context, aw: tuple) -> None:
    m = update.effective_message
    text = (m.text or "").strip()
    kind = aw[0]
    admin = update.effective_user
    if not text:
        await m.reply_html("لطفاً متن بفرستید.")
        return
    if kind == "a_card":
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        num = re.sub(r"\D", "", (lines[0] if lines else "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
        if len(num) != 16:
            await m.reply_html("❌ شماره کارت باید ۱۶ رقم باشد؛ دوباره بفرستید.")
            return
        card = {"number": num, "holder": lines[1][:60] if len(lines) > 1 else "", "bank": lines[2][:60] if len(lines) > 2 else ""}

        def _add(c):
            cs = c["shop"]["cards"]
            if len(cs) >= C.SHOP_CARDS_MAX:
                raise ValueError(f"حداکثر {C.SHOP_CARDS_MAX} کارت.")
            if any(x["number"] == num for x in cs):
                raise ValueError("این کارت قبلاً ثبت شده است.")
            cs.append(card)
            c["shop"]["card"] = dict(cs[0])
        try:
            await asyncio.to_thread(C.update_config, _add)
        except ValueError as e:
            await m.reply_html(f"❌ {esc(e)}", reply_markup=_adm_back())
            context.user_data.pop("aw", None)
            return
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "افزودن کارت فروشگاه", "", f"****{num[-4:]}", cat="sys")
        context.user_data.pop("aw", None)
        await m.reply_html("✅ کارت اضافه شد.", reply_markup=_adm_back([[B("💳 کارت‌ها", callback_data="ad:card")]]))
    elif kind in ("a_wel", "a_sup"):
        key = "welcome" if kind == "a_wel" else "support"
        val = "" if text == "-" else text[:1000]
        await asyncio.to_thread(C.update_config, lambda c: c["shop"].update({key: val}))
        context.user_data.pop("aw", None)
        await m.reply_html("✅ ذخیره شد.", reply_markup=_adm_back())
    elif kind in ("a_newplan", "a_plprice"):
        price = _digits(text)
        if not price or price > 10 ** 9:
            await m.reply_html("❌ قیمت را فقط با عدد (تومان) بفرستید.")
            return
        if kind == "a_newplan":
            def mut(c):
                ids = {x["id"] for x in c["shop"]["plans"]}
                n = 1
                while f"p{n}" in ids:
                    n += 1
                c["shop"]["plans"].append({"id": f"p{n}", "name": aw[1][:60], "template": aw[1], "price": price, "active": True})
        else:
            def mut(c):
                pl = plan_get(c["shop"], aw[1])
                if pl:
                    pl["price"] = price
        await asyncio.to_thread(C.update_config, mut)
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "تغییر پلن فروشگاه", str(aw[1]), f"قیمت: {price:,}", cat="sys")
        context.user_data.pop("aw", None)
        await m.reply_html("✅ ذخیره شد.", reply_markup=_adm_back([[B("📦 پلن‌ها", callback_data="ad:plans")]]))
    elif kind == "a_plname":
        def mut(c):
            pl = plan_get(c["shop"], aw[1])
            if pl:
                pl["name"] = text[:60]
        await asyncio.to_thread(C.update_config, mut)
        context.user_data.pop("aw", None)
        await m.reply_html("✅ ذخیره شد.", reply_markup=_adm_back([[B("📦 پلن‌ها", callback_data="ad:plans")]]))
    elif kind == "a_cust":
        custs = shop_cfg()["customers"]
        needle = text.lstrip("@").lower()
        hits = [(uid, c) for uid, c in custs.items() if needle == uid or needle in (c["username"] or "").lower() or needle in (c["name"] or "").lower()][:8]
        if not hits:
            await m.reply_html("مشتری‌ای پیدا نشد؛ دوباره امتحان کنید.")
            return
        rows = [[B(f"{c['name'] or 'بدون‌نام'} — {money(c['balance'])}", callback_data=f"ad:c:{uid}")] for uid, c in hits]
        await m.reply_html("نتیجه:", reply_markup=_adm_back(rows))
    elif kind == "a_adj":
        uid, sign = aw[1], aw[2]
        amt = _digits(text)
        if not amt or amt > 10 ** 9:
            await m.reply_html("❌ مبلغ را فقط با عدد (تومان) بفرستید.")
            return
        try:
            w = await asyncio.to_thread(wallet_apply, int(uid), sign * amt, "charge" if sign > 0 else "deduct", f"توسط ادمین {admin.id}")
        except ValueError as e:
            await m.reply_html(f"❌ {esc(e)}")
            return
        context.user_data.pop("aw", None)
        if sign > 0:
            await _tell(context.bot, int(uid), f"✅ کیف پول شما <b>{money(amt)}</b> تومان شارژ شد.\nموجودی: <b>{money(w['balance'])}</b> تومان")
        await m.reply_html(f"✅ انجام شد. موجودی جدید: <b>{money(w['balance'])}</b> تومان", reply_markup=_adm_back([[B("👤 مشتری", callback_data=f"ad:c:{uid}")]]))


# ═════════════════════════════ اجرا (داخل پروسه‌ی ربات مدیریت) ═════════════════════════════
async def _on_error(update, context) -> None:
    log.error("خطا در ربات فروش", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.callback_query:
            await _ans(update.callback_query, "❌ خطای داخلی؛ دوباره تلاش کنید.", show_alert=True)
    except TelegramError:
        pass


def build_shop_app(token: str) -> Application:
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.IMAGE, on_msg))
    app.add_error_handler(_on_error)
    return app


async def _setup_menu(bot: Bot) -> None:
    """منوی دستورات (/start و /admin بدون تایپ)، دکمه‌ی منو کنار فیلد پیام و توضیحات صفحه‌ی اول ربات."""
    try:
        await bot.set_my_commands([BotCommand("start", "🏠 منوی اصلی")])
        mu = mini_url()
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🛍 فروشگاه", web_app=WebAppInfo(url=mu)) if mu else MenuButtonCommands())
        s = shop_cfg()
        await bot.set_my_short_description("خرید و تمدید کانفیگ · کیف پول · اتصال سرویس با لینک ساب")
        await bot.set_my_description((s["welcome"] or "به فروشگاه خوش آمدید.") [:400] + "\n\nبرای شروع دکمه‌ی START را بزنید.")
    except TelegramError as e:
        log.warning("shop menu setup: %s", e)
    for aid in C.load_config_ro().get("admin_ids", []):
        try:
            await bot.set_my_commands([BotCommand("start", "🏠 منوی اصلی"), BotCommand("admin", "⚙️ مدیریت فروشگاه")], scope=BotCommandScopeChat(int(aid)))
        except (TelegramError, ValueError, TypeError):
            pass


class Runner:
    """ربات فروش را کنار ربات مدیریت (در همان event loop) بالا می‌آورد و با تغییر توکن دوباره راه می‌اندازد."""

    def __init__(self) -> None:
        self.app: Application | None = None
        self.token = ""
        self.task: asyncio.Task | None = None
        self.zp_task: asyncio.Task | None = None
        self.username = ""
        self.error = ""
        self._lock = asyncio.Lock()

    async def sync(self) -> str:
        """وضعیت را با config هماهنگ می‌کند. خروجی: یوزرنیم ربات (یا رشته‌ی خالی)."""
        async with self._lock:
            want = shop_cfg()["token"]
            if self.app and want != self.token:
                await self._stop()
            if want and not self.app:
                await self._start(want)
            return self.username

    async def _start(self, token: str) -> None:
        self.error = ""
        app = build_shop_app(token)
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
        except Exception as e:  # noqa: BLE001
            self.error = f"{e.__class__.__name__}: {e}"
            log.error("ربات فروش بالا نیامد: %s", self.error)
            try:
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
            return
        self.app, self.token = app, token
        self.username = getattr(app.bot, "username", "") or ""
        await _setup_menu(app.bot)
        self.task = asyncio.create_task(maintenance_loop(app.bot))
        self.zp_task = asyncio.create_task(zp_loop())            # پایشگر پرداخت آنلاین؛ اگر وب‌پنل خاموش باشد هم پرداخت‌های باز verify می‌شوند
        log.info("✅ Shop bot started (@%s)", self.username)

    async def _stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        if self.zp_task:
            self.zp_task.cancel()
            self.zp_task = None
        app, self.app, self.token, self.username = self.app, None, "", ""
        if app:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("shop stop")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()
