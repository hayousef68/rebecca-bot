#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""درگاه پرداخت آنلاین زرین‌پال (API نسخه‌ی ۴) برای ربات فروش و پنل نماینده‌ها.

جریان کار:
1. ساخت پرداخت: درخواست به زرین‌پال ← «Authority» و لینک پرداخت؛ یک رکورد «created» در دفتر پرداخت‌ها (config.json > shop > zp > pays) ثبت می‌شود.
2. کاربر در صفحه‌ی زرین‌پال پرداخت می‌کند و مرورگرش به /api/zp/callback برمی‌گردد.
3. بازگشت: همیشه با verify تأیید می‌شود (پارامتر Status فقط یک اشاره است، نه مدرک). فقط اگر کد ۱۰۰ یا ۱۰۱ بود اثر مالی اعمال می‌شود.
4. اگر کاربر مرورگر را بست و بازگشتی نیامد، «پایشگر» (shop.zp_loop) پرداخت‌های باز را دوباره verify می‌کند.

حفاظت مالی:
- تبدیل «created → paid» و اعمال اثر مالی (شارژ کیف پول یا شروع انجام سفارش) در یک عملیات اتمیک روی config.json انجام می‌شود؛
  تکرار callback، رفرش صفحه یا هم‌زمانی callback و پایشگر هیچ‌وقت دو بار شارژ نمی‌کند.
- اگر پرداخت برای سفارشی برسد که دیگر «منتظر پرداخت» نیست (لغو/منقضی/…)، مبلغ به کیف پول مشتری برمی‌گردد (پول گم نمی‌شود).
- اگر ارتباط با زرین‌پال وسط verify قطع شود، چیزی اعمال نمی‌شود و بعداً دوباره بررسی می‌شود.
- مبلغ verify همیشه از دفتر ما خوانده می‌شود، نه از ورودی کاربر.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from urllib.parse import quote

import httpx

import common as C

log = logging.getLogger("rebecca.zp")

OFFICIAL_API = "https://payment.zarinpal.com"
SANDBOX_API = "https://sandbox.zarinpal.com"
MIN_TOMAN = 1000                 # حداقل مبلغ زرین‌پال (۱۰٬۰۰۰ ریال)
RECON_MIN_AGE = 90               # پایشگر پرداخت‌های تازه‌تر از این (ثانیه) را رها می‌کند؛ شاید کاربر هنوز در صفحه‌ی بانک باشد
RECON_MAX_AGE = 6 * 3600         # بعد از این مدت پرداخت باز «منقضی» می‌شود
RECON_GAP = 240                  # حداقل فاصله‌ی دو بررسی برای یک پرداخت
OPEN_REP_MAX = 5                 # حداکثر پرداخت بازِ هم‌زمان برای هر نماینده

CODE_FA = {
    -9: "اطلاعات ارسالی به زرین‌پال نامعتبر است (مبلغ یا آدرس بازگشت را بررسی کنید).",
    -10: "آی‌پی سرور یا مرچنت‌کد پذیرفته نشد (آی‌پی سرور باید در پنل زرین‌پال مجاز باشد؛ سرور خارج از ایران ممکن است مسدود باشد).",
    -11: "درخواست پیدا نشد یا پذیرنده فعال نیست.",
    -12: "تعداد تلاش بیش از حد؛ کمی بعد دوباره امتحان کنید.",
    -15: "درگاه پذیرنده در زرین‌پال معلق شده است.",
    -16: "سطح تأیید پذیرنده برای این عملیات کافی نیست.",
    -50: "مبلغ پرداخت‌شده با مبلغ درخواست یکی نیست.",
    -51: "پرداخت ناموفق بود.",
    -53: "این پرداخت متعلق به این پذیرنده نیست.",
    -54: "کد پرداخت نامعتبر است.",
}


class ZPError(Exception):
    """خطای قطعیِ درگاه (مثلاً مرچنت نامعتبر)."""

    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class ZPNetError(ZPError):
    """ارتباط قطع شد یا پاسخ نامفهوم بود؛ نتیجه‌ی عملیات نامشخص است."""


# ═════════════════════════════ تنظیمات ═════════════════════════════
def zp_cfg() -> dict:
    return C.load_config_ro()["shop"]["zp"]


def shop_ready(zp: dict | None = None) -> bool:
    zp = zp or zp_cfg()
    return bool(zp["shop_on"] and zp["merchant"])


def rep_ready(zp: dict | None = None) -> bool:
    zp = zp or zp_cfg()
    return bool(zp["rep_on"] and zp["merchant"])


def public_base() -> str:
    """آدرس عمومی وب‌پنل برای بازگشت از درگاه: ابتدا مقدار تنظیم‌شده، وگرنه دامنه‌ی SSL وب‌پنل."""
    zp = zp_cfg()
    if zp["base_url"]:
        return zp["base_url"]
    c = C.load_config_ro()
    ss = c.get("ssl") or {}
    if not ss.get("enabled") or ss.get("error"):
        return ""
    dom = ss.get("domain") or ""
    if not dom:
        try:
            import tlsutil
            p = C.get_panel(c, c["active_panel"]) or (c["panels"][0] if c["panels"] else None)
            dom = tlsutil._domain_of(p["panel_url"]) if p else ""
        except Exception:  # noqa: BLE001
            dom = ""
    if not dom:
        return ""
    port = int(c.get("web_port") or 8686)
    return f"https://{dom}" + ("" if port == 443 else f":{port}")


def callback_url() -> str:
    base = public_base()
    if not base:
        raise ZPError("آدرس عمومی وب‌پنل برای بازگشت از درگاه تنظیم نشده است (وب‌پنل ← فروشگاه ← زرین‌پال ← آدرس عمومی).")
    return base + "/api/zp/callback"


def pay_url(authority: str) -> str:
    zp = zp_cfg()
    return (SANDBOX_API if zp["sandbox"] else OFFICIAL_API) + "/pg/StartPay/" + quote(authority, safe="")


# ═════════════════════════════ ارتباط با زرین‌پال ═════════════════════════════
def _parse(j) -> tuple[int | None, dict, str]:
    """(کد, data, پیام). ساختار پاسخ: {data:{code,...}, errors:[]} یا {data:[], errors:{code,message}}."""
    if not isinstance(j, dict):
        return None, {}, ""
    data, errs = j.get("data"), j.get("errors")
    if isinstance(data, dict) and data.get("code") is not None:
        try:
            return int(data["code"]), data, str(data.get("message") or "")
        except (TypeError, ValueError):
            pass
    for e in ([errs] if isinstance(errs, dict) else (errs if isinstance(errs, list) else [])):
        if isinstance(e, dict) and e.get("code") is not None:
            try:
                return int(e["code"]), {}, str(e.get("message") or "")
            except (TypeError, ValueError):
                pass
    return None, {}, ""


def _fa(code: int | None, msg: str) -> str:
    base = CODE_FA.get(code or 0) or (msg[:120] if msg else "پاسخ نامشخص از زرین‌پال")
    return f"{base} (کد {code})" if code is not None else base


async def _post(path: str, body: dict, attempts: int = 3) -> dict:
    """تماس با API زرین‌پال. خطای شبکه، HTTP 5xx و پاسخ غیر-JSON تا `attempts` بار با فاصله‌ی فزاینده تکرار می‌شود
    (verify تکراری بی‌خطر است: کد ۱۰۱ می‌دهد و دفتر پرداخت‌ها از شارژ دوباره جلوگیری می‌کند). پاسخ JSON خود زرین‌پال (حتی خطا) هرگز تکرار نمی‌شود."""
    zp = zp_cfg()
    base = zp["api_base"] or (SANDBOX_API if zp["sandbox"] else OFFICIAL_API)
    kw: dict = {"timeout": httpx.Timeout(20, connect=10), "follow_redirects": False}
    if zp.get("proxy"):
        kw["proxy"] = zp["proxy"]
    hdr = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "rebecca-bot"}
    last: ZPNetError | None = None
    for i in range(max(1, attempts)):
        if i:
            await asyncio.sleep(1.5 * i)
        try:
            async with httpx.AsyncClient(**kw) as cl:
                r = await cl.post(base + path, json=body, headers=hdr)
        except httpx.HTTPError as e:
            last = ZPNetError(f"ارتباط با زرین‌پال برقرار نشد ({e.__class__.__name__}). اگر سرور خارج از ایران است، «آدرس رله‌ی API» یا «پروکسی» را در تنظیمات بگذارید.")
            last.__cause__ = e
            continue
        try:
            j = r.json()
        except ValueError:
            last = ZPNetError(f"پاسخ نامعتبر از زرین‌پال (HTTP {r.status_code}).")
            continue
        if isinstance(j, dict):
            return j
        last = ZPNetError(f"پاسخ نامفهوم از زرین‌پال (HTTP {r.status_code}).")
    raise last or ZPNetError("ارتباط با زرین‌پال برقرار نشد.")


async def request_payment(amount: int, desc: str) -> tuple[str, str]:
    """ساخت پرداخت. amount به تومان. خروجی: (Authority, لینک پرداخت)."""
    zp = zp_cfg()
    if not zp["merchant"]:
        raise ZPError("مرچنت‌کد زرین‌پال تنظیم نشده است.")
    amount = int(amount)
    if amount < MIN_TOMAN:
        raise ZPError(f"حداقل مبلغ پرداخت آنلاین {MIN_TOMAN:,} تومان است.")
    if amount > zp["max_amount"]:
        raise ZPError(f"حداکثر مبلغ پرداخت آنلاین {zp['max_amount']:,} تومان است.")
    body = {"merchant_id": zp["merchant"], "amount": amount * 10, "callback_url": callback_url(), "description": re.sub(r"\s+", " ", desc)[:200] or "پرداخت"}
    j = await _post("/pg/v4/payment/request.json", body)
    code, data, msg = _parse(j)
    auth = str(data.get("authority") or "")
    if code != 100 or not auth:
        raise ZPError(_fa(code, msg), code)
    return auth, pay_url(auth)


async def verify(authority: str, amount: int) -> dict:
    """تأیید پرداخت. خروجی: {ok, net, code, ref_id, card, msg}. net=True یعنی نتیجه نامشخص است (قطع ارتباط)."""
    zp = zp_cfg()
    try:
        j = await _post("/pg/v4/payment/verify.json", {"merchant_id": zp["merchant"], "amount": int(amount) * 10, "authority": authority})
    except ZPNetError as e:
        return {"ok": False, "net": True, "code": None, "ref_id": "", "card": "", "msg": str(e)}
    code, data, msg = _parse(j)
    if code in (100, 101):
        return {"ok": True, "net": False, "code": code, "ref_id": str(data.get("ref_id") or ""), "card": str(data.get("card_pan") or "")[:24], "msg": ""}
    if code is None:                                      # پاسخ نامفهوم؛ قطعی نیست
        return {"ok": False, "net": True, "code": None, "ref_id": "", "card": "", "msg": "پاسخ نامفهوم از زرین‌پال"}
    if code in (-12, -15, -16):                           # محدودیت موقت یا مشکل سمت پذیرنده؛ پرداخت ممکن است واقعی باشد، قطعی «ناموفق» نیست
        return {"ok": False, "net": True, "code": code, "ref_id": "", "card": "", "msg": _fa(code, msg)}
    return {"ok": False, "net": False, "code": code, "ref_id": "", "card": "", "msg": _fa(code, msg)}


async def unverified() -> list[dict]:
    """پرداخت‌هایی که مشتری در بانک پرداخت کرده ولی verify نشده‌اند (مثلاً مرورگر هیچ‌وقت به callback برنگشته)؛ مستقیم از زرین‌پال.
    خروجی: [{authority, amount (ریال), ...}]. خطای قطعی ZPError می‌دهد؛ قطع ارتباط ZPNetError."""
    zp = zp_cfg()
    if not zp["merchant"]:
        return []
    j = await _post("/pg/v4/payment/unVerified.json", {"merchant_id": zp["merchant"]})
    code, data, msg = _parse(j)
    if code != 100:
        raise ZPError(_fa(code, msg), code)
    au = data.get("authorities")
    return [x for x in au if isinstance(x, dict)] if isinstance(au, list) else []


# ═════════════════════════════ دفتر پرداخت‌ها ═════════════════════════════
def pay_find(authority: str) -> dict | None:
    if not authority:
        return None
    p = next((x for x in zp_cfg()["pays"] if x["auth"] == authority), None)
    return dict(p) if p else None


def pay_get(pay_id: str) -> dict | None:
    p = next((x for x in zp_cfg()["pays"] if x["id"] == pay_id), None)
    return dict(p) if p else None


def rep_open_count(rid: str, now: int | None = None) -> int:
    now = now or int(time.time())
    return sum(1 for p in zp_cfg()["pays"] if p["kind"] == "rep" and p["ref"] == rid and p["status"] == "created" and now - p["ts"] < 1800)


def pay_new(kind: str, ref: str, uid: int, amount: int, authority: str) -> dict:
    """ثبت پرداخت تازه. برای سفارش، شناسه‌ی پرداخت داخل خود سفارش هم می‌نشیند."""
    out: dict = {}

    def mut(c):
        zp = c["shop"]["zp"]
        now = int(time.time())
        if kind == "rep" and sum(1 for p in zp["pays"] if p["kind"] == "rep" and p["ref"] == ref and p["status"] == "created" and now - p["ts"] < 1800) >= OPEN_REP_MAX:
            raise ValueError("چند پرداخت باز دارید؛ چند دقیقه صبر کنید یا آن‌ها را کامل کنید.")
        pid = "z" + secrets.token_hex(5)
        while any(p["id"] == pid for p in zp["pays"]):
            pid = "z" + secrets.token_hex(5)
        rec = {"id": pid, "kind": kind, "ref": str(ref), "uid": int(uid), "amount": int(amount), "auth": authority, "status": "created", "ts": now,
               "chk": 0, "pts": 0, "ref_id": "", "card": "", "msg": "", "fx": ""}
        zp["pays"].append(rec)
        zp["pays"] = zp["pays"][-C.SHOP_ZP_PAYS_MAX:]
        if kind == "order":
            o = next((x for x in c["shop"]["orders"] if x["id"] == ref), None)
            if o is not None:
                o.update(zp=authority, pay_id=pid)
        out.update(rec)
    C.update_config(mut)
    return out


def pay_mark(authority: str, status: str, msg: str = "", only_if: str = "created") -> dict | None:
    """وضعیت را (فقط اگر هنوز only_if باشد) عوض می‌کند. خروجی: رکورد جدید یا None."""
    out: dict = {}

    def mut(c):
        p = next((x for x in c["shop"]["zp"]["pays"] if x["auth"] == authority), None)
        if p is not None and p["status"] == only_if:
            p.update(status=status, msg=msg[:160], chk=int(time.time()))
            out.update(p)
    C.update_config(mut)
    return out or None


def _credit_customer(c: dict, uid: int, amount: int, txid: str, note: str) -> int | None:
    """داخل update_config: کیف پول مشتری فروشگاه را شارژ می‌کند (مثل shop.wallet_apply). خروجی: موجودی جدید یا None اگر مشتری نیست/سقف."""
    s = c["shop"]
    cu = s["customers"].get(str(uid))
    if cu is None or any(t["id"] == txid for t in reversed(s["tx"])):
        return None
    nb = cu["balance"] + int(amount)
    if nb > C.SHOP_BAL_MAX:
        return None
    cu["balance"] = nb
    cu["log"] = (cu["log"] + [{"ts": int(time.time()), "type": "topup", "amount": int(amount), "balance": nb, "note": note[:120]}])[-C.SHOP_LOG_MAX:]
    s["tx"].append({"id": txid, "ts": int(time.time()), "uid": int(uid), "kind": "topup", "amount": int(amount), "balance": nb})
    s["tx"] = s["tx"][-C.SHOP_TX_MAX:]
    return nb


def settle(authority: str, ref_id: str, card: str) -> dict:
    """اعمال اتمیک یک پرداخت تأییدشده. خروجی state:
    missing | dup (قبلاً اعمال شده) | rep_ok | order_ok (سفارش به «در حال انجام» رفت) | credited (مبلغ به کیف پول مشتری رفت) | stuck (پول گرفته شد ولی اعمال نشد؛ ادمین)."""
    out: dict = {}

    def mut(c):
        zp = c["shop"]["zp"]
        p = next((x for x in zp["pays"] if x["auth"] == authority), None)
        if p is None:
            out.update(state="missing")
            return
        if p["status"] == "paid":
            out.update(state="dup", pay=dict(p))
            return
        now, amt = int(time.time()), int(p["amount"])
        p.update(status="paid", pts=now, ref_id=str(ref_id)[:32], card=str(card)[:24], msg="", chk=now)
        if p["kind"] == "rep":
            rid = p["ref"]
            if rid not in c["reps"] or c["reps"][rid]["balance"] + amt > C.WALLET_MAX:
                p["fx"] = "stuck"
                out.update(state="stuck", pay=dict(p), why="نماینده پیدا نشد" if rid not in c["reps"] else "سقف موجودی کیف پول رد می‌شود")
                return
            rec = {"id": "z" + p["id"], "rid": rid, "amount": amt, "note": "پرداخت آنلاین زرین‌پال", "ts": now, "status": "pending", "done": 0}
            c["topups"].append(rec)
            C.topup_credit_in(c, rec, amt, f"پرداخت آنلاین زرین‌پال — پیگیری {ref_id}")
            out.update(state="rep_ok", pay=dict(p), rid=rid, balance=c["reps"][rid]["balance"])
            return
        o = next((x for x in c["shop"]["orders"] if x["id"] == p["ref"]), None)
        if o is not None and o["status"] == "new":
            o.update(status="processing", paid=True, cts=now, method="zp", zp_ref=str(ref_id)[:32])
            out.update(state="order_ok", pay=dict(p), order=dict(o))
            return
        # سفارش دیگر منتظر پرداخت نیست (لغو/منقضی/…): پول نباید گم شود؛ به کیف پول مشتری می‌رود
        nb = _credit_customer(c, int(p["uid"]), amt, "zpcredit:" + p["id"], f"پرداخت آنلاین سفارش #{p['ref']} (سفارش دیگر باز نبود)")
        if nb is None:
            p["fx"] = "stuck"
            out.update(state="stuck", pay=dict(p), why="مشتری پیدا نشد یا سقف موجودی رد می‌شود")
            return
        p["fx"] = "credited"
        if o is not None and o["status"] in ("cancelled", "new"):
            o.update(status="credited", dts=now)
        out.update(state="credited", pay=dict(p), uid=int(p["uid"]), balance=nb, oid=p["ref"])
    C.update_config(mut)
    return out


async def finalize(authority: str, status_param: str | None = None) -> dict:
    """بازگشت از درگاه یا پایشگر. همیشه verify می‌کند (Status فقط اشاره است). خروجی همان settle است یا یکی از:
    missing | failed (لغو/ناموفق) | retry (نتیجه نامشخص؛ بعداً دوباره) | unpaid (زرین‌پال می‌گوید پرداخت نشده)."""
    pay = await asyncio.to_thread(pay_find, authority)
    if not pay:
        return {"state": "missing"}
    if pay["status"] == "paid":
        return {"state": "dup", "pay": pay}
    cancelled = status_param is not None and status_param.upper() != "OK"
    if cancelled and pay["status"] in ("failed", "expired"):
        return {"state": "failed", "pay": pay}
    v = await verify(authority, pay["amount"])
    if v["ok"]:                                           # حتی با Status=NOK هم اگر زرین‌پال تأیید کرد، پول گرفته شده و باید اعمال شود
        return await asyncio.to_thread(settle, authority, v["ref_id"], v["card"])
    if v["net"]:
        await asyncio.to_thread(_touch, authority, v["msg"])
        return {"state": "retry", "pay": pay, "msg": v["msg"]}
    if cancelled:
        rec = await asyncio.to_thread(pay_mark, authority, "failed", "پرداخت لغو شد یا ناموفق بود")
        return {"state": "failed", "pay": rec or pay}
    return {"state": "unpaid", "pay": pay, "msg": v["msg"]}


def _touch(authority: str, msg: str) -> None:
    def mut(c):
        p = next((x for x in c["shop"]["zp"]["pays"] if x["auth"] == authority), None)
        if p is not None and p["status"] == "created":
            p.update(chk=int(time.time()), msg=msg[:160])
    C.update_config(mut)


# ═════════════════════════════ پایشگر ═════════════════════════════
def recon_pick(now: int | None = None) -> tuple[list[dict], list[dict]]:
    """(پرداخت‌های لازم برای verify مجدد، پرداخت‌های تازه‌منقضی‌شده). چک‌زمان همان لحظه ثبت می‌شود تا دو پایشگر هم‌زمان تکراری نزنند."""
    now = now or int(time.time())
    zp = zp_cfg()
    if not any(p["status"] == "created" for p in zp["pays"]):
        return [], []                                         # چیزی برای رسیدگی نیست؛ بی‌دلیل روی config.json ننویس
    pick: list[dict] = []
    gone: list[dict] = []

    def mut(c):
        for p in c["shop"]["zp"]["pays"]:
            if p["status"] != "created":
                continue
            age = now - p["ts"]
            if age > RECON_MAX_AGE:
                p.update(status="expired", chk=now, msg="مهلت پرداخت تمام شد")
                gone.append(dict(p))
            elif age >= RECON_MIN_AGE and now - p["chk"] >= RECON_GAP and len(pick) < 25:
                p["chk"] = now
                pick.append(dict(p))
    C.update_config(mut)
    return pick, gone
