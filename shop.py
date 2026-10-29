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
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import unquote, urlsplit

from telegram import (Bot, BotCommand, BotCommandScopeChat, InlineKeyboardButton as B, InlineKeyboardMarkup as M, MenuButtonCommands,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import common as C
import sales
from common import esc

log = logging.getLogger("rebecca.shop")
HTML = ParseMode.HTML

MAX_OPEN = 3                 # حداکثر سفارش باز (منتظر رسید یا منتظر تأیید) برای هر مشتری
STUCK_SECONDS = 600          # «در حال انجام» بیشتر از این = نیمه‌کاره مانده
REMIND_SECONDS = 3 * 3600    # یادآوری رسیدهای بررسی‌نشده به ادمین
KIND_FA = {"buy": "خرید کانفیگ", "renew": "تمدید سرویس", "topup": "شارژ کیف پول"}
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


def _build_order(c: dict, uid: int, kind: str, plan_id: str = "", amount: int = 0, username: str = "") -> tuple[dict, dict, dict]:
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
    if kind in ("buy", "renew"):
        p = plan_get(s, plan_id)
        if not p or not p["active"]:
            raise ValueError("این پلن فعال نیست.")
        if p["template"] not in c["templates"]:
            raise ValueError("تمپلت این پلن در پنل پیدا نشد؛ به پشتیبانی اطلاع دهید.")
        if p["price"] <= 0:
            raise ValueError("قیمت این پلن تنظیم نشده است.")
        amount = p["price"]
        o.update(plan=p["id"], pname=p["name"], tpl=p["template"])
    else:
        amount = int(amount)
        if not s["min_topup"] <= amount <= s["max_topup"]:
            raise ValueError(f"مبلغ شارژ باید بین {money(s['min_topup'])} و {money(s['max_topup'])} تومان باشد.")
    if kind == "renew":
        if not any(x["u"] == username for x in cu["services"]):
            raise ValueError("این سرویس متعلق به شما نیست.")
        if any(x["uid"] == uid and x["kind"] == "renew" and x.get("username") == username and x["status"] in ("new", "pending", "processing", "failed") for x in s["orders"]):
            raise ValueError("برای این سرویس یک سفارش تمدید در جریان دارد.")
        o["username"] = username
    o["amount"] = int(amount)
    return s, cu, o


def _push(s: dict, o: dict) -> None:
    s["orders"].append(o)
    s["orders"] = s["orders"][-C.SHOP_ORDER_MAX:]


def order_new(uid: int, kind: str, plan_id: str = "", amount: int = 0, username: str = "") -> dict:
    """سفارشِ منتظر رسید (کارت‌به‌کارت)."""
    out: dict = {}

    def mut(c):
        s, _, o = _build_order(c, uid, kind, plan_id, amount, username)
        _push(s, o)
        out.update(o)
    C.update_config(mut)
    return out


def order_pay_wallet(uid: int, kind: str, plan_id: str, username: str = "") -> dict:
    """کسر از کیف پول + ساخت سفارشِ «در حال انجام» در یک عملیات اتمیک (یا هر دو انجام می‌شود یا هیچ‌کدام)."""
    out: dict = {}

    def mut(c):
        s, cu, o = _build_order(c, uid, kind, plan_id, 0, username)
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
        if o["status"] != "new":
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


async def missing_channels(bot: Bot, uid: int) -> list[dict]:
    """کانال‌هایی که کاربر عضو نیست. اگر ربات نتواند بررسی کند (ادمین کانال نیست…) آن کانال نادیده گرفته می‌شود تا فروشگاه قفل نشود."""
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
            if time.time() - _force_warned.get(ch["chat"], 0) > 6 * 3600:
                _force_warned[ch["chat"]] = time.time()
                await _notify_admins(bot, f"⚠️ بررسی عضویت اجباری در {esc(ch['chat'])} ممکن نشد ({esc(e.__class__.__name__)}). ربات فروش را <b>ادمین همان کانال</b> کنید؛ تا آن موقع این کانال نادیده گرفته می‌شود.")
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
    async with _lock():
        prefix = t["prefix"]
        nx = await sales.next_number(api, prefix, len(cfg["panels"]) <= 1)
        name = C.make_names(prefix, int(nx.get("next") or 1), 1, 1)[0]
        results, failed = None, []
        for _ in range(4):
            try:
                results, failed = await C.create_users(api, [name], tpl, svc_id, concurrency=1)
            except Exception:  # noqa: BLE001
                log.exception("shop test create")
                results, failed = [], [(name, "خطای پنل")]
            if results or not failed or "قبل" not in failed[0][1]:
                break
            m = re.fullmatch(r"(.*?)(\d+)", name)
            if not m:
                break
            name = C.make_names(m.group(1), int(m.group(2)) + 1, 1, 1)[0]
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
    await asyncio.to_thread(_test_set_user, uid, uname)
    return {"results": results, "header": C.build_info(api.name, tpl, svc_name), "username": uname, "tpl": tpl}


def _test_set_user(uid: int, uname: str) -> None:
    def mut(c):
        cu = c["shop"]["customers"].get(str(uid))
        if cu is not None:
            cu["test_user"] = uname
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
        await context.bot.send_message(u.id, "🎁 تست رایگان شما آماده است. لینک بالا را در برنامه‌ی خود وارد کنید.\nبعداً می‌توانید از «🔄 سرویس‌های من» تمدیدش کنید.",
                                       reply_markup=back_kb())
    except Exception:  # noqa: BLE001
        log.exception("send test result")
        await _tell(context.bot, u.id, f"🎁 <b>تست رایگان شما</b>\n\n<code>{esc(res['results'][0]['sub'])}</code>", back_kb())
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
            w = await asyncio.to_thread(wallet_apply, uid, o["amount"], "topup", f"شارژ با رسید #{oid}", "topup:" + oid)
            await asyncio.to_thread(order_update, oid, status="done", dts=int(time.time()), result=f"موجودی: {money(w['balance'])}")
            await _tell(bot, uid, f"✅ <b>کیف پول شما شارژ شد</b>\nمبلغ: <b>{money(o['amount'])}</b> تومان\nموجودی فعلی: <b>{money(w['balance'])}</b> تومان")
            await _after(bot, oid, f"✅ شارژ انجام شد — موجودی مشتری: {money(w['balance'])}")
            return
        res = await (_do_buy(oid) if o["kind"] == "buy" else _do_renew(oid))
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
        else:
            await _tell(bot, uid, f"✅ <b>سرویس {esc(res['username'])} تمدید شد</b>\n📦 {esc(_plan_line(res['tpl']))}")
    except Exception:  # noqa: BLE001
        log.exception("send result to customer %s", uid)
        sent = False
        if o["kind"] == "buy":                 # اگر QR/آلبوم نرفت (مثلاً خطای ساخت تصویر)، حداقل لینک به‌صورت متن
            links = "\n\n".join(f"👤 <b>{esc(r['username'])}</b>\n<code>{esc(r['sub'])}</code>" for r in res["results"])
            sent = await _tell(bot, uid, f"✅ <b>خرید شما انجام شد</b>\n\n{links}")
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
    if o["kind"] in ("buy", "renew"):
        what += f" — {esc(o.get('pname') or '')}"
    if o["kind"] == "renew":
        what += f" ({esc(o.get('username') or '')})"
    rows = [f"🧾 <b>سفارش #{o['id']}</b>", f"👤 {label(cu, o['uid'])}", f"📌 {what}", f"💰 مبلغ: <b>{money(o['amount'])}</b> تومان"]
    if cu:
        rows.append(f"👛 موجودی مشتری: {money(cu['balance'])}")
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


# ═════════════════════════════ نگهداری پس‌زمینه ═════════════════════════════
async def maintain(bot: Bot, startup: bool = False) -> None:
    now = int(time.time())
    hours = shop_cfg()["receipt_hours"] * 3600
    stale: list[dict] = []
    stuck: list[dict] = []
    remind: list[dict] = []

    def mut(c):
        for o in c["shop"]["orders"]:
            if o["status"] == "new" and now - o["ts"] > hours:
                o["status"] = "cancelled"
                stale.append(dict(o))
            elif o["status"] == "processing" and (startup or now - int(o.get("cts") or o["ts"]) > STUCK_SECONDS):
                o.update(status="failed", err="عملیات نیمه‌کاره ماند (ریستارت یا قطع ارتباط)", unsure=True)
                stuck.append(dict(o))
            elif o["status"] == "pending" and not o.get("rem") and now - int(o.get("rts") or o["ts"]) > REMIND_SECONDS:
                o["rem"] = now
                remind.append(dict(o))
    C.update_config(mut)
    for o in stale:
        await _tell(bot, o["uid"], f"🚫 سفارش #{o['id']} چون رسید ارسال نشد لغو شد.")
    for o in stuck:
        await _after(bot, o["id"], "⚠️ نیمه‌کاره ماند؛ قبل از «تلاش مجدد» وضعیت کاربر را در پنل ببینید.")
    if remind:
        await _notify_admins(bot, f"⏰ {len(remind)} رسید بیش از ۳ ساعت است منتظر بررسی شماست.\n/admin ← رسیدهای در انتظار")


async def maintenance_loop(bot: Bot) -> None:
    first = True
    while True:
        try:
            await maintain(bot, startup=first)
            first = False
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


def main_kb(admin: bool, uid: int | None = None) -> M:
    rows = [[B("🛒 خرید کانفیگ", callback_data="buy"), B("🔄 سرویس‌های من", callback_data="svc")],
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
    await home(update, context)


def _card_text(o: dict) -> str:
    card = shop_cfg()["card"]
    if not card["number"]:
        return "⚠️ شماره کارت هنوز توسط ادمین تنظیم نشده است."
    num = " ".join(card["number"][i:i + 4] for i in range(0, len(card["number"]), 4))
    return (f"💳 <b>کارت‌به‌کارت</b>\n\nمبلغ دقیق: <b>{money(o['amount'])}</b> تومان\nشماره کارت:\n<code>{esc(num)}</code>\n"
            + (f"به نام: <b>{esc(card['holder'])}</b>\n" if card["holder"] else "") + (f"بانک: {esc(card['bank'])}\n" if card["bank"] else ""))


async def ask_receipt(update: Update, context, o: dict) -> None:
    context.user_data["aw"] = ("receipt", o["id"])
    what = KIND_FA[o["kind"]] + (f" — {esc(o.get('pname') or '')}" if o.get("pname") else "")
    await show(update, context,
               f"🧾 <b>سفارش #{o['id']}</b> — {what}\n\n{_card_text(o)}\n📸 بعد از پرداخت، <b>عکس رسید</b> را همین‌جا بفرستید "
               f"(یا شماره پیگیری را بنویسید).\n⏳ تا {shop_cfg()['receipt_hours']} ساعت فرصت دارید.",
               M([[B("❌ لغو سفارش", callback_data=f"oc:{o['id']}")], [B("🔙 منوی اصلی", callback_data="home")]]))


def _plans_kb(prefix: str, extra: str = "") -> M | None:
    s = shop_cfg()
    rows = [[B(f"{p['name']} — {money(p['price'])} تومان", callback_data=f"{prefix}:{extra}{p['id']}")] for p in s["plans"] if p["active"] and p["price"] > 0]
    return M(rows + [[B("🔙 منوی اصلی", callback_data="home")]]) if rows else None


async def show_plan(update: Update, context, pid: str, idx: int | None = None) -> None:
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
    head = f"🔄 تمدید <b>{esc(sv['u'])}</b>" if renew else "🛒 خرید"
    txt = (f"{head}\n\n📦 {esc(p['name'])}\n📐 {esc(_plan_line(tpl))}\n💰 قیمت: <b>{money(p['price'])}</b> تومان\n"
           f"👛 موجودی شما: {money(cu.get('balance', 0))} تومان\n\nروش پرداخت را انتخاب کنید:")
    wcb, ccb = (f"rw:{idx}:{pid}", f"rc:{idx}:{pid}") if renew else (f"bw:{pid}", f"bc:{pid}")
    rows = []
    if cu.get("balance", 0) >= p["price"]:
        rows.append([B("👛 پرداخت از کیف پول (آنی)", callback_data=wcb)])
    rows.append([B("💳 کارت‌به‌کارت (با رسید)", callback_data=ccb)])
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
           f"⏳ اعتبار: {C.fmt_expire(u.get('expire'), st, u.get('on_hold_expire_duration'))}\n\n🔗 <code>{esc(sub)}</code>")
    await show(update, context, txt, back_kb([[B("🖼 QR", callback_data=f"sq:{idx}"), B("🔄 تمدید", callback_data=f"rn:{idx}")], [B("🔙 سرویس‌ها", callback_data="svc")]]))


async def _launch_card(update: Update, context, kind: str, plan_id: str = "", amount: int = 0, username: str = "") -> None:
    try:
        o = await asyncio.to_thread(order_new, update.effective_user.id, kind, plan_id, amount, username)
    except ValueError as e:
        await show(update, context, f"❌ {esc(e)}", back_kb())
        return
    await ask_receipt(update, context, o)


async def _launch_wallet(update: Update, context, kind: str, plan_id: str, username: str = "") -> None:
    try:
        o = await asyncio.to_thread(order_pay_wallet, update.effective_user.id, kind, plan_id, username)
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
    await m.reply_html(f"✅ رسید سفارش <b>#{oid}</b> دریافت شد.\nبعد از بررسی ادمین، نتیجه همین‌جا به شما اعلام می‌شود.", reply_markup=back_kb())
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
    if aw and aw[0] == "amount":
        digits = re.sub(r"\D", "", text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
        if not digits:
            await m.reply_html("مبلغ را به <b>تومان</b> و فقط با عدد بفرستید. مثال: <code>200000</code>")
            return
        try:
            o = await asyncio.to_thread(order_new, u.id, "topup", "", int(digits))
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
        mine = [o for o in shop_cfg()["orders"] if o["uid"] == u.id and o["status"] == "new"]
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
        await show(update, context, "🛒 <b>پلن مورد نظر را انتخاب کنید:</b>" if kb else "هنوز پلنی برای فروش تعریف نشده است.", kb or back_kb())
    elif cmd == "bp":
        await show_plan(update, context, p[1])
    elif cmd == "bw":
        await _launch_wallet(update, context, "buy", p[1])
    elif cmd == "bc":
        await _launch_card(update, context, "buy", p[1])
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
        elif o["status"] == "new":
            await ask_receipt(update, context, o)
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
        title = f"🔄 <b>پلن تمدید برای {esc(svcs[i]['u'])}</b>" if kb else "پلنی برای تمدید تعریف نشده است."
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
        context.user_data["aw"] = ("a_card",)
        await show(update, context, "💳 اطلاعات کارت را در <b>سه خط</b> بفرستید:\n\n<code>6037991234567890\nعلی رضایی\nبانک ملی</code>\n\n(خط دوم و سوم اختیاری است)", _adm_back())
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
        await asyncio.to_thread(C.update_config, lambda c: c["shop"].update(card=card))
        C.audit_event(f"ادمین فروشگاه ({admin.id})", "تغییر کارت فروشگاه", "", f"****{num[-4:]}", cat="sys")
        context.user_data.pop("aw", None)
        await m.reply_html("✅ کارت ذخیره شد.", reply_markup=_adm_back())
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
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
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
        log.info("✅ Shop bot started (@%s)", self.username)

    async def _stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
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
