#!/usr/bin/env python3
"""وب‌پنل مدیریت ربکا (FastAPI). رمز از config.json (هش‌شده) خوانده می‌شود، نه از داخل کد."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import logging
import os
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError

import common as C
import ops
import sales
import shop
import tlsutil
from sales import price_for
from common import get_api, get_panel, load_config, new_panel_id, update_config, RebeccaClient

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("rebecca.web")

INDEX = os.path.join(C.BASE_DIR, "index.html")
LOGIN = os.path.join(C.BASE_DIR, "login.html")
_html_cache = {"mtime": 0.0, "data": ""}
_login_cache = {"mtime": 0.0, "data": ""}
COOKIE = "rb_session"
COOKIE_REP = "rb_rep"
_bg: set = set()


def _audit(*a, **k) -> None:
    """ثبت لاگ فعالیت بدون بلوکه‌کردن وب‌پنل: ذخیره‌ی config (کپی + نوشتن + fsync) در یک نخ جدا انجام می‌شود. خارج از حلقه‌ی رویداد مستقیم ثبت می‌شود."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        C.audit_event(*a, **k)
        return
    t = loop.create_task(asyncio.to_thread(C.audit_event, *a, **k))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
_bot = {"token": None, "obj": None}


async def _restart_self(delay: float = 1.0):
    """ریستارت وب‌پنل بعد از تأخیر کوتاه تا پاسخ HTTP به مرورگر برسد."""
    await asyncio.sleep(delay)
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "--no-block", "restart", "rebecca-webapp")
        await proc.wait()
    except Exception:  # noqa: BLE001
        log.warning("ریستارت خودکار وب‌پنل ممکن نشد؛ دستی: systemctl restart rebecca-webapp")


async def _cert_watch():
    """اگر گواهی (تمدید خودکار Let's Encrypt/پنل ربکا) عوض شد، وب‌پنل خودش ریستارت می‌شود."""
    ss = load_config()["ssl"]

    def sig():
        try:
            return tuple(os.stat(p).st_mtime_ns for p in (ss["cert"], ss["key"]))
        except OSError:
            return None
    base = sig()
    while True:
        await asyncio.sleep(3600)
        if sig() != base:
            log.info("گواهی SSL تغییر کرد؛ ریستارت وب‌پنل برای بارگذاری گواهی جدید")
            await _restart_self(0)
            return


async def _wallet_sweeper():
    """هر ۵ دقیقه: خریدی که پول کسر شده ولی بعد از ۱۰ دقیقه هنوز نتیجه‌اش ثبت نشده (کرش/قطع برق وسط عملیات) را به ادمین گزارش می‌دهد."""
    while True:
        try:
            await asyncio.sleep(300)
            for t in await asyncio.to_thread(C.wallet_tx_stale, 600):
                await _notify_admins(load_config(), f"🚨 <b>تراکنش نیمه‌تمام کیف پول</b>\nنماینده: <code>{C.esc(t['rid'])}</code>\nکاربر: <code>{C.esc(t.get('user') or '-')}</code>\nمبلغ: {abs(int(t['amount'])):,} تومان\nپول کسر شده ولی نتیجه‌ی ساخت/تمدید ثبت نشده؛ وضعیت کاربر را در پنل بررسی و در صورت نیاز دستی برگردانید.")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("wallet sweeper")


@asynccontextmanager
async def lifespan(_app):
    watcher = asyncio.create_task(_cert_watch()) if os.environ.get("REBECCA_SSL_ACTIVE") == "1" else None
    sweeper = asyncio.create_task(_wallet_sweeper())
    yield
    sweeper.cancel()
    if watcher:
        watcher.cancel()
    for c in list(C._clients.values()):
        await c.aclose()


app = FastAPI(title="Rebecca Panel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class _SlowLog:
    """لاگ درخواست‌های کند API (بیش از ۰٫۸ ثانیه) برای پیدا کردن علت کندی: journalctl -u rebecca-webapp | grep SLOW"""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.inner(scope, receive, send)
        t0 = time.perf_counter()
        try:
            await self.inner(scope, receive, send)
        finally:
            dt = time.perf_counter() - t0
            if dt > 0.8:
                log.warning("SLOW %s %s %.2fs", scope.get("method", "?"), scope.get("path"), dt)


app.add_middleware(_SlowLog)
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _basic_ok(request: Request) -> bool:
    """سازگاری با اسکریپت‌های قدیمی (curl -u)؛ مرورگر دیگر از Basic استفاده نمی‌کند."""
    import base64
    if C.totp_enabled():                       # Basic فقط رمز دارد؛ با 2FA روشن نباید راه دور زدن کد باشد
        return False
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("basic "):
        return False
    try:
        u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return False
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 8:
        return False
    if C.check_web_login(u, pw):
        return True
    _fails[ip] = recent + [now]
    return False


def guard(request: Request):
    if not C.web_configured():
        raise HTTPException(503, "رمز وب‌پنل تنظیم نشده است. روی سرور اجرا کنید: rebecca-bot passwd")
    tok = request.cookies.get(COOKIE, "")
    if not (tok and C.check_session(tok)) and not _basic_ok(request):
        raise HTTPException(401, "نشست شما تمام شده؛ دوباره وارد شوید.")
    # ضد CSRF: مرورگر بدون preflight نمی‌تواند هدر سفارشی بفرستد
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "rebecca":
        raise HTTPException(403, "درخواست نامعتبر.")


def page_guard(request: Request):
    """برای صفحه‌ی اصلی: بدون نشست، به صفحه‌ی ورود هدایت می‌شود."""
    tok = request.cookies.get(COOKIE, "")
    if C.web_configured() and tok and C.check_session(tok):
        return None
    rt = request.cookies.get(COOKIE_REP, "")
    if C.web_configured() and rt and C.check_rep_session(rt):
        return None
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(C.RebeccaError)
async def rebecca_error(_req, exc: C.RebeccaError):
    code = exc.status if exc.status in (400, 404, 409, 422) else 502
    return JSONResponse({"detail": str(exc)}, status_code=code)


@app.exception_handler(ValueError)
async def value_error(_req, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def P(panel: str | None = Query(None, max_length=20)) -> RebeccaClient:
    """کلاینت پنل انتخاب‌شده (?panel=p2)؛ بدون آن پنل فعال."""
    cfg = C.load_config_ro()
    pid = panel or cfg["active_panel"]
    if not get_panel(cfg, pid):
        raise HTTPException(404, "پنل پیدا نشد؛ از تب تنظیمات یک پنل اضافه کنید.")
    c = get_api(pid)
    if not c.is_configured():
        raise HTTPException(409, "اتصال این پنل کامل نیست؛ از تب تنظیمات آن را ویرایش کنید.")
    return c


# ───────────────────────────── صفحه ─────────────────────────────
_PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def _read_cached(path: str, cache: dict) -> str:
    m = os.path.getmtime(path)
    if m != cache["mtime"]:
        with open(path, encoding="utf-8") as f:
            cache.update(mtime=m, data=f.read())
    return cache["data"]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    r = page_guard(request)
    if r is not None:
        return r
    return HTMLResponse(_read_cached(INDEX, _html_cache), headers=_PAGE_HEADERS)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    tok, rt = request.cookies.get(COOKIE, ""), request.cookies.get(COOKIE_REP, "")
    if C.web_configured() and ((tok and C.check_session(tok)) or (rt and C.check_rep_session(rt))):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_read_cached(LOGIN, _login_cache), headers=_PAGE_HEADERS)


def _notify_lock(rep_name: str):
    """به ادمین‌ها در تلگرام خبر می‌دهد که حساب نماینده قفل شد (بی‌صدا؛ خطا نادیده گرفته می‌شود)."""
    cfg = load_config()
    if not (cfg["bot_token"] and cfg["admin_ids"]):
        return

    async def go():
        bot = _tg_bot(cfg["bot_token"])
        for uid in cfg["admin_ids"]:
            try:
                await bot.send_message(uid, f"🔒 حساب نماینده «{rep_name}» بعد از {C.REP_MAX_FAILS} رمز اشتباه پیاپی موقتاً غیرفعال شد.\nاز وب‌پنل > تنظیمات > نماینده‌ها می‌توانید دوباره فعالش کنید.")
            except Exception:  # noqa: BLE001
                pass
    t = asyncio.create_task(go())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


class LoginReq(BaseModel):
    user: str = Field(max_length=100)
    password: str = Field(max_length=256)
    remember: bool = False


def _bg_notify(text: str) -> None:
    async def go():
        try:
            await _notify_admins(load_config(), text)
        except Exception:  # noqa: BLE001
            log.exception("bg notify")
    t = asyncio.create_task(go())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _log_login(ip: str, user: str, ok: bool, why: str) -> None:
    """ثبت تلاش ورود با IP؛ ورود موفق ادمین از IP تازه ← هشدار تلگرام."""
    try:
        new_ip = await asyncio.to_thread(C.login_log_add, ip, user, ok, why)
    except Exception:  # noqa: BLE001
        log.exception("login log")
        return
    if ok and why == "admin" and new_ip:
        _bg_notify(f"🔔 <b>ورود ادمین از IP جدید</b>\nIP: <code>{C.esc(ip)}</code>\nاگر خودتان نیستید فوراً رمز وب‌پنل را عوض و ورود دومرحله‌ای را فعال کنید.")


def _fail_hit(ip: str, recent: list[float], now: float, user: str) -> None:
    recent.append(now)
    _fails[ip] = recent
    if len(recent) == 5:
        _bg_notify(f"🚨 <b>چند تلاش ناموفق ورود به وب‌پنل</b>\nIP: <code>{C.esc(ip)}</code>\nنام کاربری: <code>{C.esc(user[:40])}</code>\nاین IP به‌مدت ۵ دقیقه مسدود شد.")


def _admin_cookie(resp, tok: str, ttl: int, secure: bool):
    resp.set_cookie(COOKIE, tok, max_age=ttl, httponly=True, samesite="strict", secure=secure, path="/")
    resp.delete_cookie(COOKIE_REP, path="/")
    return resp


@app.post("/api/login")
async def login(r: LoginReq, request: Request):
    if request.headers.get("x-requested-with") != "rebecca":
        raise HTTPException(403, "درخواست نامعتبر.")
    if not C.web_configured():
        raise HTTPException(503, "رمز وب‌پنل تنظیم نشده است. روی سرور اجرا کنید: rebecca-bot passwd")
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    user = r.user.strip()
    secure = request.url.scheme == "https"
    if not C.check_web_login(user, r.password):
        st, rid = await asyncio.to_thread(C.rep_login, user, r.password)      # شاید نماینده باشد
        if st in ("locked", "locked_now"):
            if st == "locked_now":
                _notify_lock(user)
            _fail_hit(ip, recent, now, user)
            await _log_login(ip, user, False, "rep-locked")
            raise HTTPException(423, "این حساب به‌خاطر چند بار رمز اشتباه موقتاً غیرفعال شده است؛ با مدیر تماس بگیرید.")
        if st in ("ok", "blocked"):
            if st == "blocked":
                await _log_login(ip, user, False, "rep-blocked")
                raise HTTPException(403, "حساب شما مسدود شده است؛ با مدیر تماس بگیرید.")
            _fails.pop(ip, None)
            await _log_login(ip, user, True, "rep")
            tok, ttl = C.make_rep_session(rid)
            resp = JSONResponse({"ok": True, "role": "rep"})
            resp.set_cookie(COOKIE_REP, tok, max_age=ttl, httponly=True, samesite="strict", secure=secure, path="/")
            resp.delete_cookie(COOKIE, path="/")
            return resp
        _fail_hit(ip, recent, now, user)
        await _log_login(ip, user, False, "bad-password")
        await asyncio.sleep(0.6)   # کند کردن حدس رمز
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است.")
    if C.totp_enabled():                                   # رمز درست است؛ کد دومرحله‌ای هنوز لازم است (نشست ساخته نمی‌شود)
        return {"ok": True, "need_code": True, "pre": C.make_pre_token(user, r.remember)}
    _fails.pop(ip, None)
    await _log_login(ip, user, True, "admin")
    tok, ttl = C.make_session(user, r.remember)
    return _admin_cookie(JSONResponse({"ok": True, "role": "admin"}), tok, ttl, secure)


class Login2Req(BaseModel):
    pre: str = Field(max_length=400)
    code: str = Field(max_length=64)


@app.post("/api/login/2fa")
async def login_2fa(r: Login2Req, request: Request):
    if request.headers.get("x-requested-with") != "rebecca":
        raise HTTPException(403, "درخواست نامعتبر.")
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    p = C.check_pre_token(r.pre)
    if not p:
        raise HTTPException(401, "زمان وارد کردن کد تمام شد؛ دوباره با رمز وارد شوید.")
    user, remember = p
    if not await asyncio.to_thread(C.totp_verify_use, r.code):
        _fail_hit(ip, recent, now, user)
        await _log_login(ip, user, False, "bad-2fa")
        await asyncio.sleep(0.6)
        raise HTTPException(401, "کد اشتباه یا قبلاً استفاده شده است. کد جدید اپلیکیشن (یا یک کد بازیابی) را وارد کنید.")
    _fails.pop(ip, None)
    await _log_login(ip, user, True, "admin")
    tok, ttl = C.make_session(user, remember)
    return _admin_cookie(JSONResponse({"ok": True, "role": "admin"}), tok, ttl, request.url.scheme == "https")


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(COOKIE_REP, path="/")
    return resp


@app.get("/api/me")
async def me(request: Request):
    """نقش نشست فعلی: admin یا rep (برای انتخاب رابط)."""
    tok, rt = request.cookies.get(COOKIE, ""), request.cookies.get(COOKIE_REP, "")
    if tok and C.check_session(tok):
        return {"role": "admin"}
    rr = C.check_rep_session(rt) if rt else None
    if rr:
        return {"role": "rep", "name": rr[1]["name"], "label": rr[1]["label"]}
    raise HTTPException(401, "نشست شما تمام شده؛ دوباره وارد شوید.")


def _panels_view(cfg):
    return [{"id": p["id"], "name": p["name"], "ok": bool(p["panel_url"] and p["username"] and p["password"])} for p in cfg["panels"]]


@app.get("/api/state", dependencies=[Depends(guard)])
async def state():
    cfg = C.load_config_ro()
    return {
        "panels": _panels_view(cfg), "active_panel": cfg["active_panel"],
        "bot": bool(cfg["bot_token"] and cfg["admin_ids"]),
        "templates": cfg["templates"], "active": cfg["active_template"], "active_group": cfg.get("active_group_template", ""), "max_batch": cfg["max_batch"],
        "default_prefix": cfg["default_prefix"], "multi_panel": len(cfg["panels"]) > 1, "unpaid_hours": cfg["unpaid"]["hours"],
        "ham_names": cfg.get("sales", {}).get("hamkari_service_names", ["hamkari"]),
        "next_plan_mode": cfg.get("next_plan_mode", "duration"), "undo": cfg["undo"]["enabled"],
    }


@app.get("/api/services", dependencies=[Depends(guard)])
async def services(refresh: bool = False, api: RebeccaClient = Depends(P)):
    return {"services": await api.list_services(force=refresh)}


@app.get("/api/stats", dependencies=[Depends(guard)])
async def stats(api: RebeccaClient = Depends(P)):
    return await api.request("GET", "/api/system")


@app.get("/api/report", dependencies=[Depends(guard)])
async def report(day: str = "today", date_from: str = Query("", alias="from", max_length=10), date_to: str = Query("", alias="to", max_length=10)):
    if date_from and date_to:
        try:
            sales.shift_day(date_from, 0); sales.shift_day(date_to, 0)
        except ValueError:
            raise HTTPException(400, "تاریخ نامعتبر؛ فرمت YYYY-MM-DD")
        return {"day": "range", "text": await asyncio.to_thread(sales.build_period_report, date_from, date_to, "", False)}
    if day in ("week", "month", "lmonth"):
        return {"day": day, "text": await asyncio.to_thread(sales.build_range_report, day, False)}
    today = sales.tehran_day()
    d = {"today": today, "yesterday": sales.shift_day(today, -1)}.get(day, day)
    try:
        sales.shift_day(d, 0)
    except ValueError:
        raise HTTPException(400, "تاریخ نامعتبر؛ today / yesterday / YYYY-MM-DD")
    return {"day": d, "text": await asyncio.to_thread(sales.build_report, d, html=False, partial=(d == today))}


@app.get("/api/report/summary", dependencies=[Depends(guard)])
async def report_summary(kind: str = Query("today", pattern="^(today|yday|week|month|lmonth)$")):
    return await asyncio.to_thread(sales.summary, kind)


class OnePlanReq(BaseModel):
    gb: int = Field(ge=1, le=100000)
    months: int = Field(ge=1, le=24)
    price: int = Field(ge=0, le=10**10)


@app.post("/api/sales/plan", dependencies=[Depends(guard)])
async def sales_plan(r: OnePlanReq):
    """ثبت قیمت یک حجم خارج از لیست؛ از گزارش بعدی جزو فروش‌های لیست حساب می‌شود."""
    sales.set_plan(r.gb, r.months, r.price)
    _aud("ثبت قیمت پلن", f"{r.gb} گیگ {r.months} ماهه", f"قیمت: {r.price:,} تومان", "sys")
    return {"ok": True}


@app.get("/api/report/months", dependencies=[Depends(guard)])
async def report_months():
    return {"months": sales.jmonths(12)}


@app.get("/api/report/events", dependencies=[Depends(guard)])
async def report_events(day: str = "today"):
    today = sales.tehran_day()
    d = {"today": today, "yesterday": sales.shift_day(today, -1)}.get(day, day)
    try:
        sales.shift_day(d, 0)
    except ValueError:
        raise HTTPException(400, "تاریخ نامعتبر")
    return {"day": d, "text": await asyncio.to_thread(sales.build_events, d, False, d == today, 300)}


def _rep_range(day: str, d_from: str, d_to: str) -> tuple[str, str]:
    try:
        return sales.resolve_range(day, d_from, d_to)
    except ValueError:
        raise HTTPException(400, "تاریخ نامعتبر؛ today / yesterday / week / month / lmonth / YYYY-MM-DD")


@app.get("/api/report/list", dependencies=[Depends(guard)])
async def report_list(day: str = "today", date_from: str = Query("", alias="from", max_length=10), date_to: str = Query("", alias="to", max_length=10)):
    """فهرست تک‌تک فروش‌های بازه برای حذف/ویرایش (حذف‌شده‌ها هم با علامت می‌آیند تا قابل بازگرداندن باشند)."""
    d1, d2 = _rep_range(day, date_from, date_to)
    return {"from": d1, "to": d2, "events": await asyncio.to_thread(sales.events_list, d1, d2)}


class SaleEventReq(BaseModel):
    eid: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9_|\-]+$")
    op: str = Field(pattern="^(hide|show|edit|reset)$")        # hide=حذف از گزارش | show=بازگرداندن | edit=ویرایش | reset=لغو ویرایش
    gb: int | None = Field(default=None, ge=1, le=100000)
    months: int | None = Field(default=None, ge=1, le=2)
    type: str | None = Field(default=None, pattern="^(ham|single)$")


@app.post("/api/report/event", dependencies=[Depends(guard)])
async def report_event(r: SaleEventReq):
    if r.op == "hide":
        await asyncio.to_thread(sales.ovr_hide, r.eid, True)
    elif r.op == "show":
        await asyncio.to_thread(sales.ovr_hide, r.eid, False)
    elif r.op == "reset":
        await asyncio.to_thread(sales.ovr_edit_clear, r.eid)
    else:
        if r.gb is None and r.months is None and r.type is None:
            raise HTTPException(400, "حداقل یکی از حجم، مدت یا نوع را مشخص کنید.")
        await asyncio.to_thread(sales.ovr_edit, r.eid, r.gb, r.months, r.type)
    _aud("ویرایش گزارش فروش", str(r.eid)[:60], {"hide": "حذف از گزارش", "show": "برگرداندن به گزارش", "reset": "بازگردانی ویرایش"}.get(r.op, f"حجم: {r.gb}؛ مدت: {r.months}؛ نوع: {r.type}"), "sys")
    return {"ok": True}


class PlanReq(BaseModel):
    gb: int = Field(ge=1, le=100000)
    months: int = Field(ge=1, le=24)
    price: int = Field(ge=0, le=10**10)


class SalesReq(BaseModel):
    hamkari_per_gb: int = Field(ge=0, le=10**8)
    buy_per_tb: int = Field(ge=0, le=10**10)
    hamkari_usage_multiplier: float = Field(ge=0.1, le=100)
    hamkari_multiplier_enabled: bool = True
    hamkari_service_names: str = Field(default="hamkari", max_length=200)
    ignore_service_names: str = Field(default="", max_length=200)
    single_prices: list[PlanReq] = Field(max_length=50)
    attach_extra_mode: str = Field(default="skip", pattern="^(skip|count)$")


@app.get("/api/sales/settings", dependencies=[Depends(guard)])
async def sales_settings_get():
    sc = C.load_config_ro()["sales"]
    return {"hamkari_per_gb": sc["hamkari_per_gb"], "buy_per_tb": sc.get("buy_per_tb", 750000),
            "hamkari_usage_multiplier": sc.get("hamkari_usage_multiplier", 1.5),
            "hamkari_multiplier_enabled": sc.get("hamkari_multiplier_enabled", True),
            "hamkari_service_names": ", ".join(sc.get("hamkari_service_names", [])),
            "ignore_service_names": ", ".join(sc.get("ignore_service_names", [])),
            "fallback_rate": round(sales.fallback_rate(sc)), "attach_extra_mode": sc.get("attach_extra_mode", "skip"),
            "single_prices": sorted(sc["single_prices"], key=lambda x: (x["months"], x["gb"]))}


@app.post("/api/sales/settings", dependencies=[Depends(guard)])
async def sales_settings_save(r: SalesReq):
    names = [n.strip() for n in r.hamkari_service_names.split(",") if n.strip()]
    plans, seen = [], set()
    for p in r.single_prices:
        if (p.gb, p.months) in seen:
            raise HTTPException(400, f"پلن تکراری: {p.gb} گیگ {p.months} ماهه")
        seen.add((p.gb, p.months))
        plans.append({"gb": p.gb, "months": p.months, "price": p.price})

    def mut(c):
        c["sales"].update(hamkari_per_gb=r.hamkari_per_gb, buy_per_tb=r.buy_per_tb,
                          hamkari_usage_multiplier=r.hamkari_usage_multiplier,
                          hamkari_multiplier_enabled=r.hamkari_multiplier_enabled,
                          hamkari_service_names=names or ["hamkari"],
                          ignore_service_names=[n.strip().lower() for n in r.ignore_service_names.split(",") if n.strip()],
                          single_prices=plans, attach_extra_mode=r.attach_extra_mode)
    update_config(mut)
    _aud("تغییر قیمت‌ها و تنظیمات فروش", "", f"قیمت هر گیگ همکاری: {r.hamkari_per_gb:,}؛ خرید هر ترابایت: {r.buy_per_tb:,}؛ تعداد پلن تکی: {len(plans)}؛ پیوست/پلن بعدی خارج از لیست: {'حساب نمی‌شود' if r.attach_extra_mode == 'skip' else 'به‌نرخ گیگی'}", "sys")
    return {"ok": True}


@app.post("/api/report/send", dependencies=[Depends(guard)])
async def report_send(day: str = "today"):
    """گزارش را همین الان به ادمین‌ها در تلگرام می‌فرستد (فقط یک کوئری read-only)."""
    cfg = load_config()
    if not (cfg["bot_token"] and cfg["admin_ids"]):
        raise HTTPException(400, "توکن ربات یا ادمین تنظیم نشده")
    today = sales.tehran_day()
    d = {"today": today, "yesterday": sales.shift_day(today, -1)}.get(day, day)
    try:
        sales.shift_day(d, 0)
    except ValueError:
        raise HTTPException(400, "تاریخ نامعتبر؛ today / yesterday / YYYY-MM-DD")
    text = await asyncio.to_thread(sales.build_report, d, True, d == today)
    bot = _tg_bot(cfg["bot_token"])
    sent = 0
    for uid in cfg["admin_ids"]:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("send report to %s: %s", uid, e)
    if not sent:
        raise HTTPException(502, "ارسال به تلگرام ناموفق بود (ربات را در تلگرام Start کرده‌اید؟)")
    return {"sent": sent}


@app.get("/api/qr", dependencies=[Depends(guard)])
async def qr(text: str = Query(..., min_length=1, max_length=2000)):
    png = await asyncio.to_thread(C.make_qr_png, text)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ───────────────────────────── ساخت گروهی ─────────────────────────────
class CreateReq(BaseModel):
    base: str = Field(min_length=1, max_length=32)
    start: int | None = None
    count: int = 1
    service_id: int
    template: str | None = None
    telegram: bool = False                                          # قدیمی؛ نادیده گرفته می‌شود (لینک همیشه ارسال می‌شود)
    send_qr: bool = True                                            # تصویر QR هم همراه لینک ارسال شود؟
    gb: float | None = Field(default=None, ge=0, le=100000)      # جایگزین حجم تمپلت (تکی)
    days: int | None = Field(default=None, ge=0, le=36500)       # جایگزین مدت تمپلت
    mode: str | None = Field(default=None, pattern="^(on_hold|active)$")
    rep_id: str | None = Field(default=None, max_length=20)          # نمایندگی که اکانت‌ها به لیستش اضافه می‌شوند
    unpaid: bool = False                                             # ساخت «واریز نشده»: بعد از مهلت خودکار غیرفعال می‌شود
    unpaid_hours: int | None = Field(default=None, ge=1, le=24 * 365)


def _tg_bot(token: str):
    from telegram import Bot
    if _bot["token"] != token:
        _bot.update(token=token, obj=Bot(token))
    return _bot["obj"]


@app.get("/api/next-name", dependencies=[Depends(guard)])
async def next_name(prefix: str = Query("", max_length=28), api: RebeccaClient = Depends(P)):
    """آخرین کاربر با این پیشوند و شماره‌ی بعدی (برای پر کردن خودکار فرم ساخت)."""
    prefix = prefix.strip() or load_config()["default_prefix"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,28}", prefix):
        raise HTTPException(400, "پیشوند فقط حروف انگلیسی، عدد و _")
    use_db = len(load_config()["panels"]) <= 1  # دیتابیس مربوط به یک پنل است
    return await sales.next_number(api, prefix, use_db)


@app.post("/api/create", dependencies=[Depends(guard)])
async def create(req: CreateReq, api: RebeccaClient = Depends(P)):
    cfg = load_config()
    name = req.template or cfg["active_template"]
    tpl = cfg["templates"].get(name)
    if not tpl:
        raise HTTPException(404, "تمپلت پیدا نشد.")
    tpl = dict(tpl)
    if req.gb is not None:
        tpl["gb"] = int(req.gb) if float(req.gb).is_integer() else req.gb
    if req.days is not None:
        tpl["days"] = req.days
    if req.mode:
        tpl["mode"] = req.mode
    names = C.make_names(req.base, req.start, req.count, cfg["max_batch"])
    results, failed = await C.create_users(api, names, tpl, req.service_id)
    if req.rep_id and results:
        if req.rep_id not in cfg["reps"]:
            raise HTTPException(404, "نماینده پیدا نشد.")
        await asyncio.to_thread(C.assign_users, req.rep_id, api.pid, [x["username"] for x in results])
    if req.unpaid and results:
        for x in results:
            await asyncio.to_thread(C.unpaid_mark, api.pid, x["username"], req.unpaid_hours)
    tg = "off"
    if results:
        if cfg["bot_token"] and cfg["admin_ids"]:
            svc = next((s["name"] for s in await api.list_services() if s["id"] == req.service_id), req.service_id)
            header = C.build_info(api.name, tpl, svc)
            t = asyncio.create_task(_safe_send(cfg, results, header, req.send_qr))
            _bg.add(t)
            t.add_done_callback(_bg.discard)
            tg = "queued"
        else:
            tg = "no_bot"
    value = 0
    try:
        svc_name = next((s["name"] for s in await api.list_services() if s["id"] == req.service_id), "")
        months = 2 if int(tpl["days"]) > 45 else 1
        value = price_for(svc_name, tpl["gb"], months)[0] * len(results) if tpl["gb"] else 0
    except Exception:  # noqa: BLE001
        pass
    if results:
        _aud("ساخت کاربر", results[0]["username"] if len(results) == 1 else f"{results[0]['username']} … {results[-1]['username']}",
             f"تعداد: {len(results)}؛ پنل: {api.name}؛ تمپلت: {name}" + (f"؛ ناموفق: {len(failed)}" if failed else "") + (f"؛ نسبت‌داده‌شده به نماینده: {cfg['reps'][req.rep_id]['name']}" if req.rep_id in cfg["reps"] else ""))
    return {"ok": results, "failed": [{"username": n, "reason": r} for n, r in failed], "telegram": tg, "value": value}


async def _safe_send(cfg, results, header, send_qr=True):
    try:
        await C.send_results(_tg_bot(cfg["bot_token"]), cfg["admin_ids"][0], results, header, cfg["max_qr"], send_qr)
    except Exception:  # noqa: BLE001
        log.exception("ارسال به تلگرام ناموفق بود")


# ───────────────────────────── نماینده‌ها ─────────────────────────────
def rep_guard(request: Request):
    """فقط نماینده‌ی واردشده و غیرمسدود؛ کوکی ادمین به این مسیرها دسترسی نمی‌دهد."""
    tok = request.cookies.get(COOKIE_REP, "")
    r = C.check_rep_session(tok) if tok else None
    if not r:
        raise HTTPException(401, "نشست شما تمام شده؛ دوباره وارد شوید.")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "rebecca":
        raise HTTPException(403, "درخواست نامعتبر.")
    return r


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _slim_rep(u: dict, api) -> dict:
    """فقط چیزهایی که نماینده باید ببیند (بدون یادداشت، سرویس، پلن بعدی و ...)."""
    d = _slim(u, api)
    return {k: d[k] for k in ("username", "status", "used", "limit", "days_left", "unlimited_days", "expire_text", "sub")}   # «یادداشت پنل» مال ادمین است؛ یادداشت نماینده جداست


@app.get("/api/rep/users")
async def rep_users(offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=50), search: str = Query("", max_length=64), rep=Depends(rep_guard)):
    rid = rep[0]
    cfg = load_config()
    q = search.translate(_FA_DIGITS).strip().lstrip("@").lower()
    notes = cfg["rep_notes"]
    can_np = bool(cfg["reps"][rid].get("next_plan"))

    def note_of(pid, u):
        return notes.get(pid, {}).get(u, "")
    items = [(pid, u) for pid, m in cfg["rep_users"].items() for u, r in m.items()
             if r == rid and (not q or q in u.lower() or q in note_of(pid, u).translate(_FA_DIGITS).lower())]
    items.sort(key=lambda x: C._natkey(x[1]), reverse=True)      # از عدد بزرگ به کوچک (MS_150 قبل از MS_149)
    page = items[offset:offset + limit]
    sem = asyncio.Semaphore(6)

    async def one(pid, u):
        async with sem:
            try:
                api = get_api(pid)
                if not api.is_configured():
                    return None
                raw = await api.request("GET", api.upath(u))
                d = _slim_rep(raw, api)
                d["pid"], d["note"], d["paid"] = pid, note_of(pid, u), bool(cfg["rep_paid"].get(pid, {}).get(u))
                if can_np:
                    d["next_plans"] = [{"gb": x["gb"], "days": x["days"]} for x in C.next_plans_view(raw)]
                return d
            except C.RebeccaError:        # حذف‌شده از پنل یا خطای موقت
                return None
    res = [x for x in await asyncio.gather(*(one(p, u) for p, u in page)) if x]
    return {"users": res, "total": len(items), "missing": len(page) - len(res)}


@app.get("/api/rep/history")
async def rep_history(username: str = Query(..., min_length=1, max_length=64), pid: str = Query("", max_length=20), rep=Depends(rep_guard)):
    """تاریخچه‌ی ساخت و تمدیدِ یکی از کاربرهای خودِ نماینده (تاریخ شمسی)."""
    rid = rep[0]
    cfg = load_config()
    pids = [p for p, m in cfg["rep_users"].items() if m.get(username) == rid and (not pid or p == pid)]
    if not pids:
        raise HTTPException(404, "این کاربر در لیست شما نیست.")
    api = get_api(pids[0])
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
    return {"items": await _history_for(api, username)}


class RepNoteReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=400)


@app.post("/api/rep/note")
async def rep_note(r: RepNoteReq, rep=Depends(rep_guard)):
    """ذخیره‌ی اسم/یادداشت نماینده برای یکی از کاربرهای خودش (جدا از یادداشت پنل که فقط ادمین می‌بیند)."""
    rid = rep[0]
    cfg = load_config()
    pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
    if not pids:
        raise HTTPException(404, "این کاربر در لیست شما نیست.")
    try:
        note = await asyncio.to_thread(C.set_rep_note, rid, pids[0], r.username, r.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rep_name = cfg.get("reps", {}).get(rid, {}).get("name", rid)
    _audit(f"نماینده: {rep_name}", "تغییر یادداشت کاربر", r.username, note)
    return {"ok": True, "note": note}


class RepPaidReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    paid: bool


@app.post("/api/rep/paid")
async def rep_paid(r: RepPaidReq, rep=Depends(rep_guard)):
    """نماینده علامت می‌زند که پول این کاربر را از مشتری گرفته است."""
    rid = rep[0]
    cfg = load_config()
    pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
    if not pids:
        raise HTTPException(404, "این کاربر در لیست شما نیست.")
    try:
        paid = await asyncio.to_thread(C.set_rep_paid, rid, pids[0], r.username, r.paid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rep_name = cfg.get("reps", {}).get(rid, {}).get("name", rid)
    _audit(f"نماینده: {rep_name}", "تغییر وضعیت پرداخت مشتری", r.username, "پرداخت شد" if paid else "پرداخت نشده")
    return {"ok": True, "paid": paid}


class RepStatusReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    enabled: bool


@app.post("/api/rep/status")
async def rep_status(r: RepStatusReq, rep=Depends(rep_guard)):
    """نماینده اشتراکِ یکی از کاربرهای خودش را فعال/غیرفعال می‌کند."""
    rid = rep[0]
    cfg = load_config()
    pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
    if not pids:
        raise HTTPException(404, "این کاربر در لیست شما نیست.")
    pid = pids[0]
    api = get_api(pid)
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست.")
    u = await api.request("PUT", api.upath(r.username), json={"status": "active" if r.enabled else "disabled"})
    if r.enabled:                                                  # فعال‌سازی دستی ← شمارش معکوس «واریز نشده»ی قبلی لغو شود
        await asyncio.to_thread(C.unpaid_clear, pid, r.username)
    rep_name = cfg.get("reps", {}).get(rid, {}).get("name", rid)
    _audit(f"نماینده: {rep_name}", "تغییر وضعیت اشتراک", r.username, "فعال" if r.enabled else "غیرفعال")
    return {"ok": True, "status": _slim_rep(u, api)["status"]}


def _rep_templates(cfg: dict, rep: dict) -> dict:
    """تمپلت‌هایی که ادمین برای این نماینده فعال کرده و قیمت دارند."""
    return {n: cfg["templates"][n] for n in rep["tpls"] if n in cfg["templates"] and cfg["templates"][n].get("price", 0) > 0}


async def _notify_admins(cfg: dict, text: str):
    if not (cfg["bot_token"] and cfg["admin_ids"]):
        return
    bot = _tg_bot(cfg["bot_token"])
    for uid in cfg["admin_ids"]:
        try:
            await bot.send_message(uid, text[:4000], parse_mode="HTML")
        except Exception:  # noqa: BLE001
            log.warning("notify admin %s failed", uid)


async def _notify_low_wallet(rid: str, balance: int):
    cfg = load_config()
    alerts = cfg.get("smart_alerts", {})
    threshold = int(alerts.get("low_wallet_threshold", 100000))
    if not alerts.get("low_rep_wallet", True) or balance > threshold:
        return
    rp = cfg.get("reps", {}).get(rid, {})
    await _notify_admins(cfg, f"⚠️ <b>موجودی کم نماینده</b>\nنماینده: <b>{C.esc(rp.get('label') or rp.get('name') or rid)}</b>\nموجودی فعلی: <b>{balance:,} تومان</b> (حد هشدار: {threshold:,} تومان)")


_money_locks: dict[str, asyncio.Lock] = {}
_money_hits: dict[str, list[float]] = {}


def _money_guard(rid: str) -> asyncio.Lock:
    """محدودیت عملیات مالی نماینده (حداکثر ۱۵ بار در دقیقه) و قفل اختصاصی تا دو درخواست هم‌زمان از یک موجودی خرج نکنند."""
    now = time.time()
    hits = [t for t in _money_hits.get(rid, []) if now - t < 60]
    if len(hits) >= 15:
        raise HTTPException(429, "تعداد درخواست‌ها زیاد است؛ یک دقیقه بعد دوباره تلاش کنید.")
    hits.append(now)
    _money_hits[rid] = hits
    return _money_locks.setdefault(rid, asyncio.Lock())


_idem_cache: dict[tuple, tuple[float, dict]] = {}


def _idem_txid(request: Request | None, rid: str, op: str, fallback: str = "") -> str:
    """شناسه‌ی یکتای تراکنش. اگر مرورگر هدر Idempotency-Key بفرستد (برای تلاش مجدد پس از قطعی شبکه ثابت می‌ماند) همان استفاده می‌شود
    تا همان درخواست دوباره پول کم نکند؛ وگرنه شناسه‌ی تصادفی ساخته می‌شود."""
    key = (request.headers.get("idempotency-key", "") if request is not None else "").strip()
    key = re.sub(r"[^A-Za-z0-9_.-]", "", key)[:48]
    return f"{op}:{rid}:{key}" if key else f"{op}:{rid}:s{secrets.token_hex(6)}"


def _idem_get(txid: str):
    now = time.time()
    for k in [k for k, v in _idem_cache.items() if now - v[0] > 600]:
        _idem_cache.pop(k, None)
    v = _idem_cache.get((txid,))
    return v[1] if v else None


def _idem_put(txid: str, result: dict) -> None:
    _idem_cache[(txid,)] = (time.time(), result)


_recent_ops: dict[str, float] = {}


def _dup_window(sig: str, seconds: float) -> bool:
    """True اگر همین عملیات (مثلاً تمدید همان کاربر با همان تمپلت) در چند ثانیه‌ی اخیر انجام شده باشد."""
    now = time.time()
    for k in [k for k, t in _recent_ops.items() if now - t > 120]:
        _recent_ops.pop(k, None)
    if now - _recent_ops.get(sig, 0) < seconds:
        return True
    _recent_ops[sig] = now
    return False


async def _refund_safely(rid: str, txid: str, note: str, sale_meta: dict | None = None) -> bool:
    """بازگشت وجه با چند بار تلاش. چون به txid خرید وصل است، حداکثر یک‌بار انجام می‌شود (تلاش مجدد خطر برگشت دوباره ندارد)."""
    last = None
    for i in range(4):
        try:
            await asyncio.to_thread(lambda: C.wallet_adjust_ex(rid, 0, "refund", note, sale_meta, refund_of=txid))
            await asyncio.to_thread(C.wallet_tx_mark, txid, "refunded")
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            log.exception("refund attempt %s failed (%s)", i + 1, txid)
            await asyncio.sleep(0.4 * (i + 1))
    try:
        await _notify_admins(load_config(), f"🚨 <b>بازگشت وجه خودکار ناموفق بود</b>\nنماینده: <code>{C.esc(rid)}</code>\nتراکنش: <code>{C.esc(txid)}</code>\nخطا: {C.esc(last)}\nلطفاً کیف پول را دستی بررسی کنید.")
    except Exception:  # noqa: BLE001
        pass
    return False


async def _hide_from_sales(username: str, t_start: float):
    """ساخت/تمدید با کیف پول نماینده در گزارش فروش حساب نمی‌شود؛ فقط خودِ شارژ کیف پول فروش است."""
    try:
        await asyncio.to_thread(sales.ovr_add_span, username, t_start - 30, time.time() + 90, True)
    except Exception:  # noqa: BLE001
        log.exception("hide rep purchase from sales")


@app.get("/api/rep/wallet")
async def rep_wallet(rep=Depends(rep_guard)):
    rid, rp = rep
    cfg = load_config()
    rp = cfg["reps"][rid]
    return {"balance": rp["balance"], "can_next_plan": bool(rp.get("next_plan")),
            "templates": [{"name": n, "gb": t["gb"], "days": t["days"], "price": t["price"], "kind": t.get("kind", "single")} for n, t in _rep_templates(cfg, rp).items()],
            "sales": await asyncio.to_thread(sales.rep_daily_sales, rid, 7),
            "log": list(reversed(rp["log"]))[:30],
            "topups": [t for t in reversed(cfg["topups"]) if t["rid"] == rid][:15]}


class RepTopupReq(BaseModel):
    amount: int = Field(ge=1000, le=10 ** 10)
    note: str = Field(default="", max_length=200)


@app.post("/api/rep/topup")
async def rep_topup(r: RepTopupReq, rep=Depends(rep_guard)):
    rid = rep[0]
    try:
        rec = await asyncio.to_thread(C.topup_add, rid, r.amount, r.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = load_config()
    rp = cfg["reps"][rid]
    if cfg.get("smart_alerts", {}).get("topup_request", True):
        await _notify_admins(cfg, f"💰 <b>درخواست افزایش موجودی</b>\nنماینده: <b>{C.esc(rp['label'] or rp['name'])}</b> ({C.esc(rp['name'])})\nمبلغ: <b>{r.amount:,}</b> تومان"
                         + (f"\nتوضیح: {C.esc(r.note)}" if r.note else "") + "\n\nاز وب‌پنل ← تنظیمات ← نماینده‌ها بررسی کنید.")
    return {"ok": True, "id": rec["id"]}


async def _next_rep_name(cfg: dict, rp: dict) -> tuple[str, str]:
    """(pid, نام کاربریِ بعدی) طبق شماره‌گذاریِ پنل."""
    pid = rp["panel"] if rp["panel"] and get_panel(cfg, rp["panel"]) else cfg["active_panel"]
    api = get_api(pid)
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
    prefix = cfg["default_prefix"]
    nx = await sales.next_number(api, prefix, len(cfg["panels"]) <= 1)
    return pid, C.make_names(prefix, int(nx.get("next") or 1), 1, 1)[0]


@app.get("/api/rep/next-name")
async def rep_next_name(rep=Depends(rep_guard)):
    """نام کاربری که اکانت بعدی می‌گیرد (فقط نمایش؛ نماینده نمی‌تواند تغییرش دهد)."""
    cfg = load_config()
    return {"name": (await _next_rep_name(cfg, cfg["reps"][rep[0]]))[1]}


class RepBuyReq(BaseModel):
    template: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=100)
    paid: bool = False                                                 # نماینده همان موقع ساخت «پرداخت شد» را هم می‌زند (مشتری پول را داده)


@app.post("/api/rep/buy")
async def rep_buy(r: RepBuyReq, request: Request, rep=Depends(rep_guard)):
    """ساخت اکانت با تمپلت مجاز نماینده؛ قیمت از کیف پول کم می‌شود و اگر ساخت ناموفق بود (و اکانت واقعاً ساخته نشده بود) برمی‌گردد.
    هر درخواست یک شناسه‌ی تراکنش دارد: ارسال دوباره‌ی همان درخواست (کلیک مجدد/قطعی شبکه) پول را دوباره کم نمی‌کند."""
    rid = rep[0]
    txid = _idem_txid(request, rid, "buy")
    async with _money_guard(rid):
        return await _rep_buy(r, rid, txid)


async def _rep_buy(r: RepBuyReq, rid: str, txid: str):
    done = _idem_get(txid)
    if done:
        return done
    cfg = load_config()
    rp = cfg["reps"][rid]
    tpl = _rep_templates(cfg, rp).get(r.template)
    if not tpl:
        raise HTTPException(403, "این تمپلت برای شما فعال نیست.")
    prev = await asyncio.to_thread(C.wallet_tx_get, txid)
    if prev:                                                           # همین درخواست قبلاً پردازش شده (تلاش مجدد)
        return await _replay_buy(prev, cfg, rid, rp, tpl)
    if _dup_window(f"buy|{rid}|{r.template}", 4):
        raise HTTPException(429, "درخواست تکراری: چند ثانیه‌ی پیش همین خرید ثبت شد. اگر واقعاً اکانت دیگری می‌خواهید چند ثانیه بعد دوباره بزنید.")
    pid = rp["panel"] if rp["panel"] and get_panel(cfg, rp["panel"]) else cfg["active_panel"]
    api = get_api(pid)
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
    price = int(tpl["price"])
    if price <= 0:
        raise HTTPException(400, "قیمت این تمپلت نامعتبر است.")
    if rp["balance"] < price:
        raise HTTPException(402, f"موجودی کافی نیست. قیمت: {price:,} تومان — موجودی شما: {rp['balance']:,} تومان")
    svc = C.pick_default_service(await api.list_services(), tpl.get("kind") == "group")
    if svc is None:
        raise HTTPException(502, "سرویس مناسب در پنل پیدا نشد؛ به ادمین اطلاع دهید.")
    prefix = cfg["default_prefix"]
    nx = await sales.next_number(api, prefix, len(cfg["panels"]) <= 1)
    names = C.make_names(prefix, int(nx.get("next") or 1), 1, 1)
    t_start = time.time()
    sale_meta = {"action": "new", "template": r.template, "gb": int(tpl.get("gb") or 0), "days": int(tpl.get("days") or 0), "username": names[0]}
    try:
        w = await asyncio.to_thread(lambda: C.wallet_adjust_ex(rid, -price, "purchase", f"ساخت اکانت {names[0]} — {r.template}", sale_meta, txid))
    except ValueError:
        raise HTTPException(402, "موجودی کافی نیست.")
    if w.get("dup"):
        return await _replay_buy(w["tx"], cfg, rid, rp, tpl)
    results, failed = [], []
    for attempt in range(4):                                       # اگر هم‌زمان کسی همان شماره را گرفت، شماره‌ی بعدی
        try:
            results, failed = await C.create_users(api, names, tpl, svc, concurrency=1)
        except Exception:  # noqa: BLE001
            results, failed = [], [(names[0], "خطای پنل")]
        if results or not failed or "قبل" not in failed[0][1]:
            break
        m = re.fullmatch(r"(.*?)(\d+)", names[0])
        names = C.make_names(m.group(1), int(m.group(2)) + 1, 1, 1) if m else names
    if not results and failed and "قبل" not in failed[0][1]:
        # شکست مبهم (مثلاً timeout): ممکن است پنل اکانت را ساخته باشد؛ اگر وجود دارد پول برنمی‌گردد و موفق حساب می‌شود
        try:
            u = await api.request("GET", api.upath(names[0]))
            if u and u.get("username"):
                results = [{"username": names[0], "sub": api.full_sub((u or {}).get("subscription_url", ""))}]
        except Exception:  # noqa: BLE001
            pass
    if not results:
        ok = await _refund_safely(rid, txid, f"بازگشت وجه — ساخت {names[0]} ناموفق بود", sale_meta)
        msg = "ساخت اکانت ناموفق بود: " + (failed[0][1] if failed else "نامشخص")
        raise HTTPException(400, msg + (" — مبلغ برگشت داده شد." if ok else " — بازگشت وجه خودکار انجام نشد؛ به ادمین اطلاع داده شد."))
    uname = results[0]["username"]
    await asyncio.to_thread(C.wallet_tx_mark, txid, "done", uname)      # نتیجه‌ی موفق قطعی ثبت شد (دیگر برگشت‌پذیر خودکار نیست)
    await _hide_from_sales(uname, t_start)
    for i in range(3):                                                 # اتصال اکانت به نماینده نباید با یک خطای گذرا از دست برود
        try:
            await asyncio.to_thread(C.assign_users, rid, pid, [uname])
            break
        except Exception:  # noqa: BLE001
            log.exception("assign_users failed (%s)", uname)
            await asyncio.sleep(0.3 * (i + 1))
    if r.note.strip():
        try:
            await asyncio.to_thread(C.set_rep_note, rid, pid, uname, r.note)
        except Exception:  # noqa: BLE001
            pass
    paid = False
    if r.paid:
        try:
            paid = await asyncio.to_thread(C.set_rep_paid, rid, pid, uname, True)
            _audit(f"نماینده: {rp['label'] or rp['name']}", "تغییر وضعیت پرداخت مشتری", uname, "پرداخت شد (هنگام ساخت)")
        except Exception:  # noqa: BLE001
            log.exception("set paid on buy (%s)", uname)
    bal = C.load_config_ro()["reps"][rid]["balance"]
    result = {"ok": True, "username": uname, "sub": results[0]["sub"], "balance": bal, "gb": tpl["gb"], "days": tpl["days"], "paid": paid}
    _idem_put(txid, result)
    try:                                                               # اعلان‌ها بعد از ثبت نتیجه؛ خطایشان نباید جواب موفق را خراب کند
        await _notify_low_wallet(rid, bal)
        await _notify_admins(cfg, f"🛒 نماینده <b>{C.esc(rp['label'] or rp['name'])}</b> اکانت <code>{C.esc(uname)}</code> ساخت ({C.esc(r.template)} — {price:,} تومان). موجودی باقی‌مانده: {bal:,}")
    except Exception:  # noqa: BLE001
        log.exception("notify after buy")
    return result


async def _replay_buy(tx: dict, cfg: dict, rid: str, rp: dict, tpl: dict):
    """درخواست تکراریِ خریدی که قبلاً کسر شده: بدون کسر دوباره، نتیجه‌ی قبلی را (اگر ساخته شده) برمی‌گرداند."""
    if tx.get("state") == "done" and tx.get("user"):
        pid = rp["panel"] if rp["panel"] and get_panel(cfg, rp["panel"]) else cfg["active_panel"]
        api = get_api(pid)
        sub = ""
        try:
            u = await api.request("GET", api.upath(tx["user"]))
            sub = api.full_sub((u or {}).get("subscription_url", ""))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "duplicate": True, "username": tx["user"], "sub": sub, "balance": load_config()["reps"][rid]["balance"], "gb": tpl["gb"], "days": tpl["days"]}
    if tx.get("state") == "refunded":
        raise HTTPException(400, "این درخواست قبلاً ناموفق بود و مبلغ برگشت داده شد؛ دوباره تلاش کنید.")
    raise HTTPException(409, "این درخواست در حال پردازش است؛ چند لحظه بعد وضعیت «کاربران من» را بررسی کنید و دوباره پول نپردازید.")


class RepRenewReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    template: str = Field(min_length=1, max_length=40)


def _user_fingerprint(u: dict | None) -> tuple:
    u = u or {}
    return (int(u.get("data_limit") or 0), int(u.get("expire") or 0), int(u.get("used_traffic") or 0), str(u.get("status") or ""))


@app.post("/api/rep/renew")
async def rep_renew(r: RepRenewReq, request: Request, rep=Depends(rep_guard)):
    """تمدید کاربرِ خود نماینده با تمپلت مجاز؛ قیمت تمپلت از کیف پول کم می‌شود (ناموفق ← برگشت وجه، فقط وقتی تمدید واقعاً اعمال نشده).
    ارسال دوباره‌ی همان درخواست (کلید یکسان) یا تمدید همان کاربر با همان تمپلت در چند ثانیه، دوباره پول کم نمی‌کند."""
    rid = rep[0]
    txid = _idem_txid(request, rid, "renew")
    async with _money_guard(rid):
        done = _idem_get(txid)
        if done:
            return done
        cfg = load_config()
        rp = cfg["reps"][rid]
        pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
        if not pids:
            raise HTTPException(404, "این کاربر در لیست شما نیست.")
        pid = pids[0]
        tpl = _rep_templates(cfg, rp).get(r.template)
        if not tpl:
            raise HTTPException(403, "این تمپلت برای شما فعال نیست.")
        prev = await asyncio.to_thread(C.wallet_tx_get, txid)
        if prev:
            if prev.get("state") == "done":
                return {"ok": True, "duplicate": True, "balance": rp["balance"]}
            if prev.get("state") == "refunded":
                raise HTTPException(400, "این درخواست قبلاً ناموفق بود و مبلغ برگشت داده شد؛ دوباره تلاش کنید.")
            raise HTTPException(409, "این تمدید در حال پردازش است؛ چند لحظه بعد وضعیت کاربر را ببینید و دوباره پول نپردازید.")
        if _dup_window(f"renew|{rid}|{r.username}|{r.template}", 20):
            raise HTTPException(429, "درخواست تکراری: همین کاربر چند لحظه‌ی پیش با همین تمپلت تمدید شد.")
        api = get_api(pid)
        if not api.is_configured():
            raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
        price = int(tpl["price"])
        if price <= 0:
            raise HTTPException(400, "قیمت این تمپلت نامعتبر است.")
        try:                                                                       # قبل از کسر پول مطمئن شو کاربر در پنل هست (و حالت قبل را بگیر)
            before = await api.request("GET", api.upath(r.username))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404 if isinstance(e, C.RebeccaError) and e.status == 404 else 502, "کاربر در پنل پیدا نشد یا پنل در دسترس نیست؛ پولی کسر نشد.")
        t_start = time.time()
        sale_meta = {"action": "renew", "template": r.template, "gb": int(tpl.get("gb") or 0), "days": int(tpl.get("days") or 0), "username": r.username}
        try:
            w = await asyncio.to_thread(lambda: C.wallet_adjust_ex(rid, -price, "purchase", f"تمدید {r.username} — {r.template}", sale_meta, txid))
        except ValueError:
            raise HTTPException(402, f"موجودی کافی نیست. قیمت: {price:,} تومان — موجودی شما: {rp['balance']:,} تومان")
        if w.get("dup"):
            raise HTTPException(409, "این درخواست قبلاً ثبت شده است.")
        res, err = None, None
        try:
            await C.renew_user(api, r.username, dict(tpl), reset_usage=True)
            res = await api.request("GET", api.upath(r.username))
        except Exception as e:  # noqa: BLE001
            err = e
            log.exception("renew failed %s", r.username)
        if err is not None:
            # شکست مبهم (timeout وسط راه): ببین تمدید روی پنل اعمال شده یا نه؛ اگر اعمال شده، پول برنمی‌گردد
            applied, after = False, None
            try:
                after = await api.request("GET", api.upath(r.username))
                applied = _user_fingerprint(after) != _user_fingerprint(before)
            except Exception:  # noqa: BLE001
                applied = False
            await _hide_from_sales(r.username, t_start)                            # هر تغییری روی پنل ایجاد شده باشد در گزارش فروش حساب نشود
            if applied:
                await asyncio.to_thread(C.wallet_tx_mark, txid, "done", r.username)
                await _notify_admins(cfg, f"⚠️ تمدید <code>{C.esc(r.username)}</code> (نماینده {C.esc(rp['label'] or rp['name'])}) با خطا برگشت ولی تغییرات روی پنل اعمال شده بود؛ مبلغ برنگشت. لطفاً وضعیت کاربر را بررسی کنید.")
                res = after
            else:
                ok = await _refund_safely(rid, txid, f"بازگشت وجه — تمدید {r.username} ناموفق بود", sale_meta)
                raise HTTPException(400, "تمدید ناموفق بود؛ " + ("مبلغ برگشت داده شد." if ok else "بازگشت وجه خودکار انجام نشد و به ادمین اطلاع داده شد.") + (f" ({err})" if isinstance(err, C.RebeccaError) else ""))
        else:
            await asyncio.to_thread(C.wallet_tx_mark, txid, "done", r.username)
            await _hide_from_sales(r.username, t_start)
        try:
            await asyncio.to_thread(C.unpaid_clear, pid, r.username)
        except Exception:  # noqa: BLE001
            log.exception("unpaid_clear")
        bal = C.load_config_ro()["reps"][rid]["balance"]
        result = {"ok": True, "balance": bal, "user": _slim_rep(res, api)}
        _idem_put(txid, result)
        try:
            await _notify_low_wallet(rid, bal)
            await _notify_admins(cfg, f"🔄 نماینده <b>{C.esc(rp['label'] or rp['name'])}</b> کاربر <code>{C.esc(r.username)}</code> را تمدید کرد ({C.esc(r.template)} — {price:,} تومان). موجودی باقی‌مانده: {bal:,}")
        except Exception:  # noqa: BLE001
            log.exception("notify after renew")
        return result


class RepNextPlanReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    template: str = Field(min_length=1, max_length=40)


REP_MAX_NEXT_PLANS = 3          # سقف پلن‌های بعدیِ صف‌شده برای هر کاربر (جلوگیری از اشتباه/سوءاستفاده)


@app.post("/api/rep/next-plan")
async def rep_next_plan(r: RepNextPlanReq, request: Request, rep=Depends(rep_guard)):
    """افزودن «پلن بعدی» (طبق یکی از تمپلت‌های مجاز نماینده) به کاربرِ خود نماینده؛ قیمت تمپلت از کیف پول کم می‌شود.
    پلن وقتی حجم/زمان فعلی تمام شد خودکار شروع می‌شود. ارسال دوباره‌ی همان درخواست پول را دوباره کم نمی‌کند."""
    rid = rep[0]
    txid = _idem_txid(request, rid, "nextplan")
    async with _money_guard(rid):
        done = _idem_get(txid)
        if done:
            return done
        cfg = load_config()
        rp = cfg["reps"][rid]
        if not rp.get("next_plan"):
            raise HTTPException(403, "دسترسی «پلن بعدی» برای شما فعال نیست؛ با ادمین تماس بگیرید.")
        pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
        if not pids:
            raise HTTPException(404, "این کاربر در لیست شما نیست.")
        pid = pids[0]
        tpl = _rep_templates(cfg, rp).get(r.template)
        if not tpl:
            raise HTTPException(403, "این تمپلت برای شما فعال نیست.")
        prev = await asyncio.to_thread(C.wallet_tx_get, txid)
        if prev:
            if prev.get("state") == "done":
                return {"ok": True, "duplicate": True, "balance": rp["balance"]}
            if prev.get("state") == "refunded":
                raise HTTPException(400, "این درخواست قبلاً ناموفق بود و مبلغ برگشت داده شد؛ دوباره تلاش کنید.")
            raise HTTPException(409, "این درخواست در حال پردازش است؛ چند لحظه بعد وضعیت کاربر را ببینید و دوباره پول نپردازید.")
        api = get_api(pid)
        if not api.is_configured():
            raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
        price = int(tpl["price"])
        if price <= 0:
            raise HTTPException(400, "قیمت این تمپلت نامعتبر است.")
        gb, days = float(tpl.get("gb") or 0), int(tpl.get("days") or 0)
        if gb <= 0 and days <= 0:
            raise HTTPException(400, "این تمپلت حجم یا مدت ندارد و برای پلن بعدی مناسب نیست.")
        try:                                                              # قبل از کسر پول: کاربر باید در پنل باشد؛ وضعیت فعلی صف را بگیر
            before = await api.request("GET", api.upath(r.username))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404 if isinstance(e, C.RebeccaError) and e.status == 404 else 502, "کاربر در پنل پیدا نشد یا پنل در دسترس نیست؛ پولی کسر نشد.")
        view = C.next_plans_view(before)
        if len(view) >= REP_MAX_NEXT_PLANS:
            raise HTTPException(400, f"برای این کاربر از قبل {len(view)} پلن بعدی ثبت شده (حداکثر {REP_MAX_NEXT_PLANS}). پولی کسر نشد.")
        if any(x.get("broken") for x in view):
            raise HTTPException(409, "پلن‌های بعدیِ این کاربر نیاز به بررسی ادمین دارد؛ پولی کسر نشد.")
        plans = [{"idx": x["idx"], "gb": x["gb"], "days": x["days"], "add_remaining": x["add_remaining"]} for x in view]
        plans.append({"idx": None, "gb": gb, "days": days, "add_remaining": False})
        t_start = time.time()
        sale_meta = {"action": "next", "template": r.template, "gb": int(gb), "days": days, "username": r.username}
        sig = f"nextplan|{rid}|{r.username}|{r.template}"
        if _dup_window(sig, 20):                                           # دابل‌کلیک؛ فقط درست قبل از کسر پول (خطاهای اعتبارسنجی قفل نمی‌سازند)
            raise HTTPException(429, "درخواست تکراری: برای همین کاربر چند لحظه‌ی پیش پلن بعدی ثبت شد.")
        try:
            w = await asyncio.to_thread(lambda: C.wallet_adjust_ex(rid, -price, "purchase", f"پلن بعدی {r.username} — {r.template}", sale_meta, txid))
        except ValueError:
            _recent_ops.pop(sig, None)                                     # پولی کم نشد؛ بعد از شارژ می‌تواند فوراً دوباره تلاش کند
            raise HTTPException(402, f"موجودی کافی نیست. قیمت: {price:,} تومان — موجودی شما: {rp['balance']:,} تومان")
        if w.get("dup"):
            raise HTTPException(409, "این درخواست قبلاً ثبت شده است.")
        res, err = None, None
        try:
            res = await C.set_next_plans(api, r.username, plans, None)
        except Exception as e:  # noqa: BLE001
            err = e
            log.exception("rep next plan failed %s", r.username)
        if err is not None:
            applied, after = False, None                                  # شکست مبهم: ببین پلن واقعاً روی پنل اضافه شده یا نه
            try:
                after = await api.request("GET", api.upath(r.username))
                applied = len(C._raw_plans(after)) > len(C._raw_plans(before))
            except Exception:  # noqa: BLE001
                applied = False
            if applied:
                await asyncio.to_thread(C.wallet_tx_mark, txid, "done", r.username)
                await _notify_admins(cfg, f"⚠️ پلن بعدیِ <code>{C.esc(r.username)}</code> (نماینده {C.esc(rp['label'] or rp['name'])}) با خطا برگشت ولی روی پنل اعمال شده بود؛ مبلغ برنگشت. لطفاً بررسی کنید.")
                res = after
            else:
                ok = await _refund_safely(rid, txid, f"بازگشت وجه — پلن بعدی {r.username} ناموفق بود", sale_meta)
                if ok:
                    _recent_ops.pop(sig, None)                             # پول برگشت؛ تلاش دوباره‌ی فوری مجاز است
                raise HTTPException(400, "ثبت پلن بعدی ناموفق بود؛ " + ("مبلغ برگشت داده شد." if ok else "بازگشت وجه خودکار انجام نشد و به ادمین اطلاع داده شد.") + (f" ({err})" if isinstance(err, C.RebeccaError) else ""))
        else:
            await asyncio.to_thread(C.wallet_tx_mark, txid, "done", r.username)
        await _hide_from_sales(r.username, t_start)
        await asyncio.to_thread(C.hist_add_many, [{"pid": getattr(api, "pid", ""), "u": r.username, "k": "next", "gb": gb, "days": days}])
        bal = C.load_config_ro()["reps"][rid]["balance"]
        result = {"ok": True, "balance": bal, "next_plans": [{"gb": x["gb"], "days": x["days"]} for x in C.next_plans_view(res or {})]}
        _idem_put(txid, result)
        try:
            await _notify_low_wallet(rid, bal)
            await _notify_admins(cfg, f"🔁 نماینده <b>{C.esc(rp['label'] or rp['name'])}</b> برای <code>{C.esc(r.username)}</code> پلن بعدی اضافه کرد ({C.esc(r.template)} — {price:,} تومان). موجودی باقی‌مانده: {bal:,}")
        except Exception:  # noqa: BLE001
            log.exception("notify after next plan")
        return result


class RepNextAccessReq(BaseModel):
    enabled: bool


@app.post("/api/reps/{rid}/next-plan-access", dependencies=[Depends(guard)])
async def reps_next_plan_access(rid: str, r: RepNextAccessReq):
    try:
        v = await asyncio.to_thread(C.set_rep_next_plan, rid, r.enabled)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _audit("ادمین وب‌پنل", "دسترسی پلن بعدی نماینده", rid, "فعال شد" if v else "غیرفعال شد")
    return {"ok": True, "next_plan": v}


class RepRevokeReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)


@app.post("/api/rep/revoke")
async def rep_revoke(r: RepRevokeReq, rep=Depends(rep_guard)):
    """تغییر لینک اشتراکِ یکی از کاربرهای خودِ نماینده (لینک قبلی از کار می‌افتد)."""
    rid = rep[0]
    cfg = load_config()
    pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
    if not pids:
        raise HTTPException(404, "این کاربر در لیست شما نیست.")
    api = get_api(pids[0])
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست.")
    u = await api.request("POST", api.upath(r.username) + "/revoke_sub")
    _audit(f"نماینده: {cfg.get('reps', {}).get(rid, {}).get('name', rid)}", "تغییر لینک اشتراک کاربر", r.username, "لینک قبلی از کار افتاد", cat="user")
    return {"ok": True, "sub": _slim_rep(u, api)["sub"]}


@app.get("/api/rep/qr")
async def rep_qr(text: str = Query(..., min_length=1, max_length=2000), rep=Depends(rep_guard)):
    png = await asyncio.to_thread(C.make_qr_png, text)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


class RepCreateReq(BaseModel):
    name: str = Field(min_length=3, max_length=32)
    password: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=60)


class RepPwReq(BaseModel):
    password: str = Field(default="", max_length=64)


class RepBlockReq(BaseModel):
    blocked: bool


class RepSpecReq(BaseModel):
    prefix: str = Field(default="", max_length=28)
    start: int | None = Field(default=None, ge=0, le=10 ** 9)
    end: int | None = Field(default=None, ge=0, le=10 ** 9)
    names: str = Field(default="", max_length=20000)


def _rep_pid(panel: str | None) -> str:
    cfg = load_config()
    pid = panel or cfg["active_panel"]
    if not get_panel(cfg, pid):
        raise HTTPException(404, "پنل پیدا نشد.")
    return pid


@app.get("/api/reps", dependencies=[Depends(guard)])
async def reps_list():
    cfg = C.load_config_ro()
    cnt: dict[str, int] = {}
    for m in cfg["rep_users"].values():
        for r in m.values():
            cnt[r] = cnt.get(r, 0) + 1
    return {"reps": [{"id": i, "name": r["name"], "label": r["label"], "blocked": r["blocked"], "locked": r["locked"], "fails": r["fails"], "locked_at": r["locked_at"],
                      "count": cnt.get(i, 0), "created": r["created"], "balance": r["balance"], "tpls": r["tpls"], "panel": r["panel"], "next_plan": bool(r.get("next_plan"))}
                     for i, r in cfg["reps"].items()],
            "topups": [{**t, "rep": cfg["reps"][t["rid"]]["label"] or cfg["reps"][t["rid"]]["name"]} for t in cfg["topups"] if t["status"] == "pending"]}


@app.get("/api/topups/pending", dependencies=[Depends(guard)])
async def topups_pending():
    """درخواست‌های افزایش موجودی در انتظار؛ سبک و مناسب polling برای اعلان بالای صفحه و عدد تب تنظیمات."""
    cfg = C.load_config_ro()
    items = [{"id": t["id"], "rid": t["rid"], "rep": (cfg["reps"].get(t["rid"], {}).get("label") or cfg["reps"].get(t["rid"], {}).get("name") or t["rid"]),
              "amount": t.get("amount", 0), "ts": t.get("ts", 0)} for t in cfg["topups"] if t.get("status") == "pending"]
    return {"count": len(items), "items": items[-20:]}


@app.post("/api/reps", dependencies=[Depends(guard)])
async def reps_create(r: RepCreateReq):
    result = await asyncio.to_thread(C.create_rep, r.name, r.password, r.label)
    _audit("ادمین وب‌پنل", "ساخت نماینده", r.name, f"نام نمایشی: {r.label}")
    return result


@app.post("/api/reps/{rid}/password", dependencies=[Depends(guard)])
async def reps_password(rid: str, r: RepPwReq):
    result = await asyncio.to_thread(C.reset_rep_password, rid, r.password)
    _audit("ادمین وب‌پنل", "تغییر رمز نماینده", rid, "رمز بازنشانی شد؛ مقدار رمز ثبت نمی‌شود")
    _bg_notify(f"🔑 رمز نماینده <code>{C.esc(rid)}</code> توسط ادمین بازنشانی شد.")
    return {"password": result}


@app.post("/api/reps/{rid}/block", dependencies=[Depends(guard)])
async def reps_block(rid: str, r: RepBlockReq):
    await asyncio.to_thread(C.set_rep_blocked, rid, r.blocked)
    _audit("ادمین وب‌پنل", "تغییر وضعیت نماینده", rid, "مسدود" if r.blocked else "رفع مسدودی")
    return {"ok": True}


@app.post("/api/reps/{rid}/unlock", dependencies=[Depends(guard)])
async def reps_unlock(rid: str):
    await asyncio.to_thread(C.unlock_rep, rid)
    _aud("فعال‌سازی مجدد حساب نماینده", rid, "", "rep")
    return {"ok": True}


@app.post("/api/reps/{rid}/delete", dependencies=[Depends(guard)])
async def reps_delete(rid: str):
    cfg_before = load_config(); rep_name = cfg_before.get("reps", {}).get(rid, {}).get("name", rid)
    await asyncio.to_thread(C.delete_rep, rid)
    _audit("ادمین وب‌پنل", "حذف نماینده", rep_name, "نماینده و ارتباط‌های ثبت‌شده حذف شدند")
    return {"ok": True}


async def _gate_code(request: Request, code: str, amount: int | None, what: str, always: bool = False) -> None:
    """اگر ورود دومرحله‌ای روشن باشد و عملیات مالیِ بزرگ (یا همیشه برای کارهای حساس) باشد، کد تازه لازم است."""
    if not (C.totp_enabled() and (always or (amount is not None and C.code_needed_for(amount)))):
        return
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    code = (code or "").strip()
    if code and await asyncio.to_thread(C.totp_verify_use, code):
        return
    if code:
        _fail_hit(ip, recent, now, "admin")
        await _log_login(ip, "admin", False, "bad-2fa-op")
    msg = f"برای {what} کد ورود دومرحله‌ای لازم است."
    if code:
        msg = "کد اشتباه یا قبلاً استفاده شده است؛ کدِ بعدیِ اپلیکیشن را وارد کنید (هر کد فقط یک‌بار)."
    raise HTTPException(403, msg, headers={"X-Need-Code": "1"})


async def _pw_gate(request: Request, password: str) -> str:
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    user = str(C.load_config_ro()["web"].get("user", "admin"))
    if not C.check_web_login(user, password):
        _fail_hit(ip, recent, now, user)
        await _log_login(ip, user, False, "bad-password-op")
        await asyncio.sleep(0.6)
        raise HTTPException(403, "رمز وب‌پنل اشتباه است.")
    return user


class PwReq(BaseModel):
    password: str = Field(max_length=256)


class TwoFaConfirmReq(BaseModel):
    code: str = Field(max_length=64)


class TwoFaDisableReq(BaseModel):
    password: str = Field(max_length=256)
    code: str = Field(max_length=64)


class SecuritySettingsReq(BaseModel):
    wallet_code_threshold: int = Field(ge=0, le=10 ** 10)
    code: str = Field(default="", max_length=64)


@app.get("/api/security", dependencies=[Depends(guard)])
async def security_get(request: Request):
    cfg = C.load_config_ro()
    now = time.time()
    lg = cfg.get("login_log", [])
    return {"totp": C.totp_enabled(), "threshold": C.wallet_code_threshold(), "https": request.url.scheme == "https",
            "known_ips": len(cfg["web"].get("known_ips", [])), "recovery_left": len(cfg["web"].get("recovery", [])),
            "fails_24h": sum(1 for x in lg if not x.get("ok") and now - float(x.get("ts") or 0) < 86400),
            "log": list(reversed(lg[-40:]))}


@app.post("/api/security/2fa/begin", dependencies=[Depends(guard)])
async def twofa_begin(r: PwReq, request: Request):
    await _pw_gate(request, r.password)
    if C.totp_enabled():
        raise HTTPException(400, "ورود دومرحله‌ای از قبل روشن است. برای تعویض، اول خاموشش کنید.")
    return await asyncio.to_thread(C.totp_begin)


@app.post("/api/security/2fa/confirm", dependencies=[Depends(guard)])
async def twofa_confirm(r: TwoFaConfirmReq, request: Request):
    try:
        codes = await asyncio.to_thread(C.totp_confirm, r.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit("ادمین وب‌پنل", "فعال‌سازی ورود دومرحله‌ای", "", "کدهای بازیابی صادر شد")
    _bg_notify("🛡 ورود دومرحله‌ای (2FA) برای وب‌پنل فعال شد.")
    user = str(C.load_config_ro()["web"].get("user", "admin"))
    tok, ttl = C.make_session(user)                       # کلید نشست عوض شده؛ همین مرورگر وارد بماند
    return _admin_cookie(JSONResponse({"ok": True, "codes": codes}), tok, ttl, request.url.scheme == "https")


@app.post("/api/security/2fa/disable", dependencies=[Depends(guard)])
async def twofa_disable(r: TwoFaDisableReq, request: Request):
    await _pw_gate(request, r.password)
    await _gate_code(request, r.code, None, "خاموش کردن ورود دومرحله‌ای", always=True)
    await asyncio.to_thread(C.totp_disable)
    _audit("ادمین وب‌پنل", "خاموش‌کردن ورود دومرحله‌ای", "", "")
    _bg_notify("⚠️ ورود دومرحله‌ای (2FA) وب‌پنل <b>خاموش</b> شد.")
    return {"ok": True}


@app.post("/api/security/settings", dependencies=[Depends(guard)])
async def security_settings(r: SecuritySettingsReq, request: Request):
    await _gate_code(request, r.code, None, "تغییر آستانه‌ی کد برای عملیات مالی", always=True)
    await asyncio.to_thread(lambda: update_config(lambda c: c["security"].__setitem__("wallet_code_threshold", int(r.wallet_code_threshold))))
    _audit("ادمین وب‌پنل", "تغییر آستانه‌ی کد 2FA", "", f"{r.wallet_code_threshold:,} تومان")
    return {"ok": True}


class WalletReq(BaseModel):
    amount: int = Field(ge=-10 ** 10, le=10 ** 10)
    note: str = Field(default="", max_length=120)
    code: str = Field(default="", max_length=64)


@app.post("/api/reps/{rid}/wallet", dependencies=[Depends(guard)])
async def reps_wallet(rid: str, r: WalletReq, request: Request):
    """شارژ (مبلغ مثبت) یا کسر (مبلغ منفی) موجودی نماینده توسط ادمین."""
    if r.amount == 0:
        raise HTTPException(400, "مبلغ را وارد کنید.")
    txid = _idem_txid(request, rid, "adm")
    if not await asyncio.to_thread(C.wallet_tx_get, txid):          # تلاش مجدد همان درخواست (کد قبلاً مصرف شده) دوباره کد نمی‌خواهد
        await _gate_code(request, r.code, r.amount, "این شارژ/کسر")
    if "idempotency-key" not in request.headers and _dup_window(f"adm|{rid}|{r.amount}", 5):
        raise HTTPException(429, "درخواست تکراری: همین مبلغ چند ثانیه‌ی پیش ثبت شد.")
    try:
        w = await asyncio.to_thread(lambda: C.wallet_adjust_ex(rid, r.amount, "charge" if r.amount > 0 else "deduct", r.note or ("شارژ توسط ادمین" if r.amount > 0 else "کسر توسط ادمین"), None, txid))
    except ValueError as e:
        raise HTTPException(400, str(e))
    b = w["balance"]
    if not w.get("dup"):
        try:                                                     # هر شارژ/کسر دستی ادمین ← هشدار تلگرام (اگر کسی پنل را هک کند فوراً دیده می‌شود)
            _rp = C.load_config_ro()["reps"].get(rid, {})
            _bg_notify(f"{'➕ شارژ' if r.amount > 0 else '➖ کسر'} <b>دستیِ کیف پول</b>\nنماینده: <b>{C.esc(_rp.get('label') or _rp.get('name') or rid)}</b>\n"
                       f"مبلغ: <b>{abs(r.amount):,}</b> تومان\nموجودی جدید: {b:,} تومان\nIP: <code>{C.esc(_client_ip(request))}</code>")
        except Exception:  # noqa: BLE001
            log.exception("notify wallet adjust")
        try:
            await _notify_low_wallet(rid, b)
        except Exception:  # noqa: BLE001
            log.exception("notify low wallet")
    return {"ok": True, "balance": b, "duplicate": bool(w.get("dup"))}


class RepTplsReq(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=200)


@app.post("/api/reps/{rid}/templates", dependencies=[Depends(guard)])
async def reps_templates(rid: str, r: RepTplsReq):
    result = await asyncio.to_thread(C.set_rep_templates, rid, r.names)
    _audit("ادمین وب‌پنل", "تغییر تمپلت‌های نماینده", rid, ", ".join(result))
    return {"ok": True, "tpls": result}


class RepPanelReq(BaseModel):
    panel: str = Field(default="", max_length=20)


@app.post("/api/reps/{rid}/panel", dependencies=[Depends(guard)])
async def reps_panel(rid: str, r: RepPanelReq):
    cfg = load_config()
    if rid not in cfg["reps"]:
        raise HTTPException(404, "نماینده پیدا نشد.")
    if r.panel and not get_panel(cfg, r.panel):
        raise HTTPException(404, "پنل پیدا نشد.")
    update_config(lambda c: c["reps"][rid].__setitem__("panel", r.panel))
    _audit("ادمین وب‌پنل", "تغییر پنل نماینده", rid, f"پنل: {r.panel or 'پنل فعال'}")
    return {"ok": True}


class TopupResolveReq(BaseModel):
    approve: bool
    amount: int | None = Field(default=None, ge=1, le=10 ** 10)
    code: str = Field(default="", max_length=64)


@app.post("/api/topups/{tid}", dependencies=[Depends(guard)])
async def topup_resolve(tid: str, r: TopupResolveReq, request: Request):
    if r.approve:
        t0 = next((t for t in load_config()["topups"] if t.get("id") == tid and t.get("status") == "pending"), None)
        if t0:
            await _gate_code(request, r.code, r.amount or int(t0.get("amount") or 0), "تأیید این افزایش موجودی")
    try:
        t = await asyncio.to_thread(C.topup_resolve, tid, r.approve, r.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": t["status"]}


@app.get("/api/reps/{rid}/log", dependencies=[Depends(guard)])
async def reps_log(rid: str):
    rp = load_config()["reps"].get(rid)
    if not rp:
        raise HTTPException(404, "نماینده پیدا نشد.")
    return {"balance": rp["balance"], "log": list(reversed(rp["log"]))[:50]}


@app.post("/api/reps/{rid}/assign", dependencies=[Depends(guard)])
async def reps_assign(rid: str, r: RepSpecReq, panel: str | None = Query(None, max_length=20)):
    names = C.parse_user_spec(r.prefix, r.start, r.end, r.names)
    if not names:
        raise HTTPException(400, "نام کاربری یا بازه را وارد کنید.")
    n = await asyncio.to_thread(C.assign_users, rid, _rep_pid(panel), names)
    _audit("ادمین وب‌پنل", "افزودن کاربران به نماینده", rid, f"پنل: {_rep_pid(panel)}؛ تعداد: {n}؛ کاربران: {', '.join(names[:30])}")
    return {"added": n}


@app.post("/api/reps/{rid}/unassign", dependencies=[Depends(guard)])
async def reps_unassign(rid: str, r: RepSpecReq, panel: str | None = Query(None, max_length=20)):
    names = C.parse_user_spec(r.prefix, r.start, r.end, r.names)
    if not names:
        raise HTTPException(400, "نام کاربری یا بازه را وارد کنید.")
    n = await asyncio.to_thread(C.unassign_users, rid, _rep_pid(panel), names)
    _audit("ادمین وب‌پنل", "حذف کاربران از نماینده", rid, f"پنل: {_rep_pid(panel)}؛ تعداد: {n}؛ کاربران: {', '.join(names[:30])}")
    return {"removed": n}


# ───────────────────────────── کاربران ─────────────────────────────
def _slim(u: dict, api, pend: dict | None = None) -> dict:
    st = u.get("status")
    if pend is None:
        pend = C.unpaid_map(getattr(api, "pid", ""))
    username = u.get("username") or ""
    # در پنل ادمین، مالکیت کاربران ساخته‌شده توسط نماینده را از نگاشت محلی می‌خوانیم.
    # این فیلد فقط در پاسخ مدیریتی استفاده می‌شود؛ پاسخ نماینده در _slim_rep آن را فیلتر می‌کند.
    cfg = C.load_config_ro()
    rid = C.rep_owner(cfg, str(getattr(api, "pid", "")), username)
    rep = cfg.get("reps", {}).get(rid, {}) if rid else {}
    rep_owner_label = (rep.get("label") or rep.get("name") or "").strip()
    rec = pend.get(username)
    exp, hold = int(u.get("expire") or 0), int(u.get("on_hold_expire_duration") or 0)
    if st == "on_hold":
        days_left = hold // 86400
    elif exp:
        days_left = max(0, -(-(exp - int(time.time())) // 86400))      # گرد به بالا
    else:
        days_left = 0
    return {
        "username": u.get("username"), "status": st,
        "used": u.get("used_traffic") or 0, "limit": u.get("data_limit") or 0,
        "limit_gb": round((u.get("data_limit") or 0) / C.GB, 2),
        "days_left": days_left, "unlimited_days": st != "on_hold" and not exp,
        "expire_text": C.fmt_expire(u.get("expire"), st, u.get("on_hold_expire_duration")),
        "sub": api.full_sub(u.get("subscription_url", "")),
        "service_id": u.get("service_id"), "note": u.get("note") or "",
        "created_by_rep": rep_owner_label, "rep_id": rid or "",
        "next_plans": C.next_plans_view(u),
        "np_raw": json.dumps(C._raw_plans(u), ensure_ascii=False)[:1500] if C._raw_plans(u) else "",
        "unpaid_due": int(rec["due"]) if rec and st != "disabled" else 0,      # زمان غیرفعال‌سازی خودکار (timestamp)؛ ۰ = ندارد
        "undo": _undo_view(C.undo_latest(getattr(api, "pid", ""), u.get("username") or "")),
    }


def _undo_view(snap: dict | None) -> dict | None:
    """خلاصه‌ی نقطه‌ی برگشت برای نمایش در دکمه‌ی «برگشت تمدید» (فقط خواندن از فایل محلی)."""
    if not snap:
        return None
    lim, used = int(snap["limit"]), int(snap["used"])
    st = snap.get("status")
    days = int(snap["hold"]) // 86400 if st == "on_hold" else (max(0, -(-(int(snap["expire"]) - int(time.time())) // 86400)) if snap.get("expire") else 0)
    return {"ts": int(snap["ts"]), "act": snap.get("act", "renew"), "left_gb": round(max(0, lim - used) / C.GB, 2) if lim else 0, "limit_gb": round(lim / C.GB, 2),
            "unlimited_gb": not lim, "days": days, "unlimited_days": st != "on_hold" and not snap.get("expire"), "status": st}


def _is_unlimited(u: dict) -> bool:
    """حجم نامحدود (data_limit=0) یا زمان نامحدود (بدون تاریخ انقضا / مدت On-Hold صفر)."""
    if not (u.get("data_limit") or 0):
        return True
    if u.get("status") == "on_hold":
        return not int(u.get("on_hold_expire_duration") or 0)
    return not int(u.get("expire") or 0)


async def _unlimited_users(api, search: str, offset: int, limit: int) -> dict:
    found, off, step = [], 0, 200
    for _ in range(200):                                   # سقف ایمنی: ۴۰ هزار کاربر
        params = {"offset": off, "limit": step}
        if search.strip():
            params["search"] = search.strip()
        res = await api.request("GET", "/api/users", params=params)
        batch = res.get("users", [])
        found += [u for u in batch if _is_unlimited(u)]
        off += step
        if len(batch) < step or off >= int(res.get("total") or 0):
            break
    pend = C.unpaid_map(api.pid)
    return {"users": [_slim(u, api, pend) for u in found[offset:offset + limit]], "total": len(found)}


@app.get("/api/users", dependencies=[Depends(guard)])
async def users(search: str = "", status: str = "", offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                api: RebeccaClient = Depends(P)):
    if status == "unlimited":          # حجم یا زمان نامحدود: پنل فیلتر ندارد؛ همه را می‌خوانیم و اینجا جدا می‌کنیم
        return await _unlimited_users(api, search, offset, limit)
    params = {"offset": offset, "limit": limit}
    if search.strip():
        params["search"] = search.strip()
    if status:
        params["status"] = status
    res = await api.request("GET", "/api/users", params=params)
    pend = C.unpaid_map(api.pid)
    return {"users": [_slim(u, api, pend) for u in res.get("users", [])], "total": res.get("total", 0)}


class EditReq(BaseModel):
    gb: float | None = Field(default=None, ge=0, le=100000)
    days: int | None = Field(default=None, ge=0, le=36500)
    add_gb: float | None = Field(default=None, gt=0, le=100000)     # افزودن حجم به حجم فعلی
    add_days: int | None = Field(default=None, gt=0, le=36500)      # افزودن روز به اعتبار فعلی


class ServiceReq(BaseModel):
    service_id: int


class NoteReq(BaseModel):
    note: str = Field(default="", max_length=500)


class RenewReq(BaseModel):
    template: str | None = None
    reset_usage: bool = True                                         # False = مصرف فعلی حفظ شود
    gb: float | None = Field(default=None, ge=0, le=100000)         # تمدید دلخواه (بدون تمپلت)
    days: int | None = Field(default=None, ge=0, le=36500)
    mode: str | None = Field(default=None, pattern="^(on_hold|active)$")
    unpaid: bool = False                                             # تمدید «واریز نشده»: بعد از مهلت خودکار غیرفعال می‌شود
    unpaid_hours: int | None = Field(default=None, ge=1, le=24 * 365)  # مهلت (ساعت)؛ خالی = مقدار تنظیمات


@app.post("/api/user/{uname}/service", dependencies=[Depends(guard)])
async def user_service(uname: str, r: ServiceReq, api: RebeccaClient = Depends(P)):
    res = await C.set_service(api, uname, r.service_id)
    _aud("تغییر سرویس کاربر", uname, f"سرویس: {r.service_id}")
    return _slim(res, api)


@app.post("/api/user/{uname}/note", dependencies=[Depends(guard)])
async def user_note(uname: str, r: NoteReq, api: RebeccaClient = Depends(P)):
    await C.set_note(api, uname, r.note.strip())
    _aud("تغییر یادداشت کاربر", uname, r.note.strip()[:200])
    return _slim(await api.request("GET", api.upath(uname)), api)


@app.post("/api/user/{uname}/renew", dependencies=[Depends(guard)])
async def user_renew(uname: str, r: RenewReq, api: RebeccaClient = Depends(P)):
    if r.template:
        tpl = load_config()["templates"].get(r.template)
        if not tpl:
            raise HTTPException(404, "تمپلت پیدا نشد.")
        tpl = dict(tpl)
    elif r.gb is not None and r.days is not None:
        tpl = {"gb": r.gb, "days": r.days, "mode": r.mode or "active"}
    else:
        raise HTTPException(400, "تمپلت یا حجم و روز را مشخص کنید.")
    await C.renew_user(api, uname, tpl, reset_usage=r.reset_usage)
    res = await api.request("GET", api.upath(uname))
    if r.unpaid:                                                      # واریز نشده ← شمارش معکوس غیرفعال‌سازی
        await asyncio.to_thread(C.unpaid_mark, api.pid, uname, r.unpaid_hours)
    else:                                                             # تمدید عادی ← هر علامت «واریز نشده»ی قبلی پاک می‌شود
        await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
    if not r.reset_usage:      # بدون ریست در دیتابیس ربکا ردی نمی‌ماند؛ برای گزارش فروش خودمان ثبت می‌کنیم
        try:
            await asyncio.to_thread(sales.record_attach, uname, res.get("service_id"), tpl.get("gb"), int(tpl.get("days") or 0))
        except Exception:  # noqa: BLE001
            log.exception("record_attach")
    _aud("تمدید کاربر", uname, f"پنل: {api.name}؛ حجم: {tpl.get('gb') or 'نامحدود'} گیگ؛ مدت: {tpl.get('days') or 'نامحدود'} روز؛ "
         + ("با ریست مصرف" if r.reset_usage else "بدون ریست مصرف") + ("؛ واریز نشده" if r.unpaid else ""))
    return _slim(res, api)


class SaveReq(BaseModel):
    gb: float | None = Field(default=None, ge=0, le=100000)
    days: int | None = Field(default=None, ge=0, le=36500)
    service_id: int | None = None
    note: str | None = Field(default=None, max_length=500)


class PlanItem(BaseModel):
    idx: int | None = Field(default=None, ge=0, le=50)
    gb: float = Field(default=0, ge=0, le=100000)
    days: int = Field(default=0, ge=0, le=36500)
    add_remaining: bool = False


class NextPlansReq(BaseModel):
    plans: list[PlanItem] = Field(default_factory=list, max_length=10)
    mode: str | None = Field(default=None, pattern="^(duration|chain)$")      # خالی = تنظیم next_plan_mode


@app.post("/api/user/{uname}/save", dependencies=[Depends(guard)])
async def user_save(uname: str, r: SaveReq, api: RebeccaClient = Depends(P)):
    await C.apply_user_save(api, uname, r.gb, r.days, r.service_id, r.note)
    _aud("ویرایش کاربر", uname, f"حجم: {r.gb}؛ روز: {r.days}؛ سرویس: {r.service_id}؛ یادداشت: {'تغییر کرد' if r.note is not None else '-'}")
    return _slim(await api.request("GET", api.upath(uname)), api)


@app.post("/api/user/{uname}/next-plans", dependencies=[Depends(guard)])
async def user_next_plans(uname: str, r: NextPlansReq, api: RebeccaClient = Depends(P)):
    res = await C.set_next_plans(api, uname, [p.model_dump() for p in r.plans], r.mode)
    for pl in r.plans:                      # هر پلنِ تازه‌اضافه‌شده همان روز در فروش ثبت می‌شود
        if pl.idx is None and pl.gb > 0:
            try:
                await asyncio.to_thread(sales.record_attach, uname, res.get("service_id"), pl.gb, pl.days, None, "next")
            except Exception:  # noqa: BLE001
                log.exception("record next plan")
            await asyncio.to_thread(C.hist_add_many, [{"pid": api.pid, "u": uname, "k": "next", "gb": pl.gb, "days": pl.days}])
    _aud("پلن بعدی کاربر", uname, f"تعداد پلن‌های صف: {len(r.plans)}")
    return _slim(res, api)


@app.post("/api/user/{uname}/paid", dependencies=[Depends(guard)])
async def user_paid(uname: str, api: RebeccaClient = Depends(P)):
    """«واریز شد»: شمارش معکوس غیرفعال‌سازی لغو می‌شود."""
    await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
    _aud("ثبت واریز مشتری", uname, "واریز شد")
    return _slim(await api.request("GET", api.upath(uname)), api)


@app.post("/api/user/{uname}/undo", dependencies=[Depends(guard)])
async def user_undo(uname: str, api: RebeccaClient = Depends(P)):
    """برگشت آخرین تمدید/ریست: حجم باقی‌مانده، مدت/انقضا، وضعیت و پلن‌های بعدی مثل قبل می‌شود و همان تمدید از گزارش فروش حذف می‌شود."""
    after, snap, warns = await C.undo_restore(api, uname)
    await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
    try:                         # تمدید/ریست/پیوستِ ثبت‌شده از لحظه‌ی عکس‌برداری تا حالا، از فروش حذف شود
        await asyncio.to_thread(sales.ovr_add_span, uname, float(snap["ts"]) - 120, time.time() + 60)
    except Exception:  # noqa: BLE001
        log.exception("undo sales span")
        warns.append("حذف از گزارش فروش انجام نشد؛ از «ویرایش فروش‌ها» دستی حذف کنید.")
    _aud("برگشت تمدید/ریست کاربر", uname, "به وضعیت قبل از آخرین تمدید/ریست برگشت")
    return {"user": _slim(after, api), "warnings": warns}


def _aud(action: str, target: str = "", details: str = "", cat: str = "user") -> None:
    """ثبت یک تغییر ادمین وب‌پنل در لاگ فعالیت (هیچ‌وقت خطا نمی‌دهد)."""
    _audit("ادمین وب‌پنل", action, target, details, cat=cat)


def _created_ts(raw: dict) -> float:
    """اگر پنل تاریخ ساخت را در خود کاربر برگرداند (created_at) آن را به timestamp تبدیل می‌کند؛ وگرنه ۰."""
    v = (raw or {}).get("created_at")
    try:
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        if isinstance(v, str) and v.strip():
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return 0.0


async def _history_for(api, uname: str) -> list[dict]:
    raw = {}
    try:
        raw = await api.request("GET", api.upath(uname))
    except Exception:  # noqa: BLE001
        pass
    return await asyncio.to_thread(sales.user_history, getattr(api, "pid", ""), uname, _created_ts(raw))


@app.get("/api/user/{uname}/history", dependencies=[Depends(guard)])
async def user_history(uname: str, api: RebeccaClient = Depends(P)):
    """تاریخچه‌ی ساخت و تمدید یک کاربر (تاریخ شمسی)؛ فقط‌خواندنی."""
    return {"items": await _history_for(api, uname)}


@app.post("/api/user/{uname}/{action}", dependencies=[Depends(guard)])
async def user_action(uname: str, action: str, body: EditReq | None = None, api: RebeccaClient = Depends(P)):
    p = api.upath(uname)
    if action == "reset":
        try:                     # یک GET سبک برای نقطه‌ی برگشت؛ اگر ناموفق بود ریست بدون آن انجام می‌شود
            await asyncio.to_thread(C.undo_snapshot, api.pid, uname, await api.request("GET", p), "reset")
        except Exception:  # noqa: BLE001
            log.warning("undo snapshot before reset failed")
        await api.request("POST", p + "/reset")
        _aud("ریست مصرف کاربر", uname)
        return {"ok": True}
    if action == "revoke":
        res = await api.request("POST", p + "/revoke_sub")
        _aud("تغییر لینک اشتراک کاربر", uname, "لینک قبلی از کار افتاد")
        return _slim(res, api)
    if action == "toggle":
        cur = await api.request("GET", p)
        new = "active" if cur.get("status") == "disabled" else "disabled"
        res = await api.request("PUT", p, json={"status": new})
        _aud("فعال‌سازی کاربر" if new == "active" else "غیرفعال‌سازی کاربر", uname)
        return _slim(res, api)
    if action == "edit":
        b = body or EditReq()
        before = await api.request("GET", p) if b.add_gb else None
        res = await C.apply_user_edit(api, uname, b.gb, b.days, b.add_gb, b.add_days)
        if before is not None and int(res.get("data_limit") or 0) > int(before.get("data_limit") or 0):   # فقط وقتی حجم واقعاً اضافه شد
            try:
                await asyncio.to_thread(sales.record_attach, uname, res.get("service_id"), b.add_gb, b.add_days or 0)
            except Exception:  # noqa: BLE001
                log.exception("record_attach")
            await asyncio.to_thread(C.hist_add_many, [{"pid": api.pid, "u": uname, "k": "attach", "gb": b.add_gb, "days": b.add_days or 0}])
        _aud("ویرایش کاربر", uname, f"حجم جدید: {b.gb}؛ روز جدید: {b.days}؛ افزودن حجم: {b.add_gb}؛ افزودن روز: {b.add_days}")
        return _slim(res, api)
    if action == "delete":
        # حذف کاربر فقط دستی و تکی توسط ادمین؛ قبل از حذف وضعیت کاربر در لاگ ثبت می‌شود
        try:
            before = await api.request("GET", p)
        except Exception:  # noqa: BLE001
            before = {}
        await api.request("DELETE", p)
        await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
        owner = (load_config().get("rep_users", {}).get(api.pid, {}) or {}).get(uname)
        owner_name = load_config().get("reps", {}).get(owner, {}).get("name") if owner else ""
        _aud("حذف کاربر (دستی)", uname, f"پنل: {api.name}؛ {C.user_brief(before)}" + (f"؛ نماینده: {owner_name}" if owner_name else ""), "del")
        return {"ok": True}
    raise HTTPException(404, "عملیات ناشناخته")


@app.post("/api/cleanup", dependencies=[Depends(guard)])
async def cleanup(dry: bool = True, api: RebeccaClient = Depends(P)):
    """غیرفعال: پاکسازی/حذف دسته‌ای برداشته شد؛ حذف کاربر فقط دستی و تکی (از کارت کاربر) انجام می‌شود."""
    raise HTTPException(410, "پاکسازی دسته‌ای حذف شده است؛ حذف کاربر فقط دستی و تکی از کارت کاربر ممکن است.")


@app.post("/api/core/restart", dependencies=[Depends(guard)])
async def core_restart(api: RebeccaClient = Depends(P)):
    await api.request("POST", "/api/core/restart")
    return {"ok": True}


# ───────────────────────────── تمپلت‌ها ─────────────────────────────
class TplReq(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    old_name: str | None = None
    gb: float = Field(ge=0, le=100000)
    days: int = Field(ge=0, le=36500)
    mode: str = Field(pattern="^(on_hold|active)$")
    kind: str = Field(default="single", pattern="^(single|group)$")
    price: int = Field(default=0, ge=0, le=10 ** 10)             # قیمت فروش به نماینده‌ها (تومان)


@app.post("/api/templates", dependencies=[Depends(guard)])
async def tpl_save(r: TplReq):
    gb = int(r.gb) if float(r.gb).is_integer() else r.gb

    def mut(c):
        old = r.old_name or r.name
        if r.old_name is None and r.name in c["templates"]:
            raise ValueError("نام تمپلت تکراری است.")
        if r.name != old and r.name in c["templates"]:
            raise ValueError("نام تمپلت تکراری است.")
        items = [(r.name if k == old else k, v) for k, v in c["templates"].items()]
        d = dict(items)
        d[r.name] = {"gb": gb, "days": r.days, "mode": r.mode, "kind": r.kind, "price": r.price}
        c["templates"] = d
        if r.name != old:                                              # تغییر نام: انتخاب تمپلت‌های نماینده‌ها هم به‌روز شود
            for rp in c["reps"].values():
                rp["tpls"] = [r.name if x == old else x for x in rp.get("tpls", [])]
        if c["active_template"] == old or c["active_template"] not in d:
            c["active_template"] = r.name
        if c.get("active_group_template") == old:
            c["active_group_template"] = r.name if r.kind == "group" else ""
    update_config(mut)
    _aud("ذخیره تمپلت", r.name, f"حجم: {gb}؛ روز: {r.days}؛ حالت: {r.mode}؛ نوع: {r.kind}؛ قیمت: {r.price}", "sys")
    return {"ok": True}


class NameReq(BaseModel):
    name: str


@app.post("/api/templates/active", dependencies=[Depends(guard)])
async def tpl_active(r: NameReq):
    def mut(c):
        if r.name not in c["templates"]:
            raise ValueError("تمپلت پیدا نشد.")
        if c["templates"][r.name].get("kind") == "group":
            c["active_group_template"] = r.name
        else:
            c["active_template"] = r.name
    update_config(mut)
    _aud("فعال‌سازی تمپلت", r.name, "", "sys")
    return {"ok": True}


@app.post("/api/templates/delete", dependencies=[Depends(guard)])
async def tpl_delete(r: NameReq):
    def mut(c):
        if len(c["templates"]) <= 1:
            raise ValueError("حداقل یک تمپلت باید باقی بماند.")
        c["templates"].pop(r.name, None)
    update_config(mut)
    _aud("حذف تمپلت", r.name, "", "sys")
    return {"ok": True}


# ───────────────────────────── تنظیمات ─────────────────────────────
def _mask(tok: str) -> str:
    return "" if not tok else tok[:5] + "…" + tok[-4:]


@app.get("/api/settings", dependencies=[Depends(guard)])
async def settings_get():
    c = C.load_config_ro()
    panels = [{"id": p["id"], "name": p["name"], "panel_url": p["panel_url"], "sub_url": p["sub_url"], "username": p["username"],
               "has_password": bool(p["password"]), "verify_ssl": p["verify_ssl"]} for p in c["panels"]]
    return {"panels": panels, "active_panel": c["active_panel"], "bot_token": _mask(c["bot_token"]),
            "admin_ids": c["admin_ids"], "web_user": c["web"].get("user", "admin"), "web_port": c["web_port"],
            "backup_enabled": c["backup"]["enabled"], "backup_interval": c["backup"]["interval_min"], "backup_has_password": bool(c["backup"].get("password")), "backup_live": c["backup"].get("live", False), "backup_live_chat": c["backup"].get("live_chat", ""), "backup_bot_id": (c["backup"].get("bot_token") or "").split(":")[0], "default_prefix": c["default_prefix"],
            "unpaid_hours": c["unpaid"]["hours"], "unpaid_count": sum(len(m) for m in c["unpaid"]["pending"].values()),
            "smart_alerts": c.get("smart_alerts", {}), "audit_count": len(c.get("audit_log", [])), "totp": C.totp_enabled()}


class SettingsReq(BaseModel):
    bot_token: str | None = None
    admin_ids: str | None = None
    web_password: str | None = Field(default=None, min_length=8, max_length=128)
    default_prefix: str | None = Field(default=None, max_length=28)       # اسم پیش‌فرض کاربر جدید (مثلاً MS_)
    unpaid_hours: int | None = Field(default=None, ge=1, le=24 * 365)     # مهلت غیرفعال‌سازی خودکارِ «واریز نشده» (ساعت)
    alert_low_rep_wallet: bool | None = None
    alert_panel_down: bool | None = None
    alert_backup_failed: bool | None = None
    alert_topup_request: bool | None = None
    alert_low_wallet_threshold: int | None = Field(default=None, ge=0, le=10**10)
    current_password: str | None = Field(default=None, max_length=256)     # برای تغییر رمز وب‌پنل
    code: str = Field(default="", max_length=64)                            # کد 2FA برای تغییرهای حساس


@app.post("/api/settings", dependencies=[Depends(guard)])
async def settings_save(r: SettingsReq, request: Request):
    restart_bot = False
    cur = load_config()
    new_ids = None if r.admin_ids is None else sorted(int(x) for x in r.admin_ids.replace("،", ",").replace(" ", ",").split(",") if x.strip().lstrip("-").isdigit())
    sensitive = bool(r.web_password) or bool(r.bot_token and "…" not in r.bot_token and r.bot_token.strip() != cur["bot_token"]) \
        or (new_ids is not None and new_ids != sorted(cur["admin_ids"]))
    if r.web_password:                                   # با نشستِ دزدیده‌شده نباید بشود رمز را عوض و صاحب را بیرون کرد
        await _pw_gate(request, r.current_password or "")
    if sensitive:                                        # توکن ربات / ادمین‌ها / رمز = مسیر تصاحب پنل و دریافت هشدارها
        await _gate_code(request, r.code, None, "تغییر رمز وب‌پنل، توکن ربات یا ادمین‌ها", always=True)
        _audit("ادمین وب‌پنل", "تغییر تنظیمات حساس", "", ", ".join(k for k, v in (("رمز وب‌پنل", r.web_password), ("توکن ربات", r.bot_token and "…" not in r.bot_token), ("ادمین‌ها", new_ids is not None and new_ids != sorted(cur["admin_ids"]))) if v))

    if sensitive:
        try:
            _what = ", ".join(k for k, v in (("رمز وب‌پنل", r.web_password), ("توکن ربات", r.bot_token and "…" not in r.bot_token),
                                             ("ادمین‌ها", new_ids is not None and new_ids != sorted(cur["admin_ids"]))) if v)
            await _notify_admins(cur, f"🔐 <b>تغییر تنظیمات حساس وب‌پنل</b>\nمورد: {C.esc(_what)}\nIP: <code>{C.esc(_client_ip(request))}</code>\nاگر خودتان نبودید فوراً اقدام کنید.")
        except Exception:  # noqa: BLE001
            log.exception("notify sensitive settings")

    def mut(c):
        nonlocal restart_bot
        if r.bot_token and "…" not in r.bot_token and r.bot_token.strip() != c["bot_token"]:
            if ":" not in r.bot_token:
                raise ValueError("فرمت توکن ربات صحیح نیست.")
            c["bot_token"] = r.bot_token.strip()
            restart_bot = True
        if r.admin_ids is not None:
            c["admin_ids"] = [int(x) for x in r.admin_ids.replace("،", ",").replace(" ", ",").split(",") if x.strip().lstrip("-").isdigit()]
        if r.web_password:
            c["web"].update(C.hash_password(r.web_password))
        if r.default_prefix is not None:
            pre = r.default_prefix.strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,28}", pre):
                raise ValueError("اسم پیش‌فرض: ۱ تا ۲۸ حرف انگلیسی، عدد یا _ (مثلاً MS_)")
            c["default_prefix"] = pre
        if r.unpaid_hours is not None:
            c["unpaid"]["hours"] = r.unpaid_hours
        alerts = c.setdefault("smart_alerts", {})
        for field, key in ((r.alert_low_rep_wallet, "low_rep_wallet"), (r.alert_panel_down, "panel_down"),
                           (r.alert_backup_failed, "backup_failed"), (r.alert_topup_request, "topup_request")):
            if field is not None:
                alerts[key] = field
        if r.alert_low_wallet_threshold is not None:
            alerts["low_wallet_threshold"] = r.alert_low_wallet_threshold
    update_config(mut)
    if restart_bot:
        try:
            proc = await asyncio.create_subprocess_exec("systemctl", "restart", "rebecca-bot")
            asyncio.create_task(proc.wait())
        except Exception:  # noqa: BLE001
            log.warning("ریستارت خودکار ربات ممکن نشد")
    return {"ok": True, "bot_restarted": restart_bot}


def _audit_filtered(cfg: dict, cat: str, q: str) -> list[dict]:
    names = {i: (r.get("label") or r.get("name") or i) for i, r in cfg.get("reps", {}).items()}
    q = q.strip().lower()
    out = []
    for x in reversed(cfg.get("audit_log", [])):                   # جدیدترین اول
        c = C.audit_category(x)
        if cat not in ("", "all") and c != cat:
            continue
        item = {**x, "cat": c, "target_name": names.get(str(x.get("target")), "")}
        if q and q not in " ".join(str(item.get(k, "")) for k in ("action", "target", "target_name", "actor", "details")).lower():
            continue
        out.append(item)
    return out


@app.get("/api/audit", dependencies=[Depends(guard)])
async def audit_get(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=100000), cat: str = Query("all", max_length=10), q: str = Query("", max_length=80)):
    items = _audit_filtered(C.load_config_ro(), cat, q)
    return {"items": items[offset:offset + limit], "total": len(items)}


@app.get("/api/audit/export", dependencies=[Depends(guard)])
async def audit_export(cat: str = Query("all", max_length=10), q: str = Query("", max_length=80)):
    """دانلود لاگ به‌صورت CSV (با BOM تا در اکسل فارسی درست باز شود)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["زمان (تهران)", "دسته", "عملیات", "هدف", "انجام‌دهنده", "مبلغ (تومان)", "موجودی بعد", "توضیح"])
    for x in _audit_filtered(load_config(), cat, q):
        t = sales.datetime.fromtimestamp(int(x.get("ts") or 0), sales.TEHRAN).strftime("%Y-%m-%d %H:%M:%S")
        def safe(v):                                               # جلوگیری از تزریق فرمول در اکسل
            v = str(v if v is not None else "")
            return "'" + v if v[:1] in ("=", "+", "-", "@") else v
        w.writerow([t, {"wallet": "کیف پول", "rep": "نماینده", "user": "کاربر", "del": "حذف", "sys": "سیستم"}.get(x["cat"], x["cat"]), safe(x.get("action")), safe(x.get("target_name") or x.get("target")), safe(x.get("actor")), x.get("amt", ""), x.get("bal", ""), safe(x.get("details"))])
    return Response(("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="rebecca-audit-{sales.tehran_day()}.csv"'})


_STARTED = time.time()
_LV = {"ok": 0, "warn": 1, "bad": 2}


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d} روز " if d else "") + (f"{h} ساعت " if h else "") + f"{m} دقیقه"


async def _service_active(name: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "is-active", name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 4)
        return out.decode().strip()
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/health", dependencies=[Depends(guard)])
async def health(request: Request):
    """وضعیت سلامت: پنل‌ها، دیتابیس، ربات، دیسک، بکاپ، کیف پول و امنیت ورود؛ هر مورد ok / warn / bad."""
    import shutil
    import stat
    cfg = C.load_config_ro()
    items: list[dict] = []

    def add(key: str, title: str, level: str, text: str):
        items.append({"key": key, "title": title, "level": level, "text": text})

    async def chk_panel(p: dict):
        t0 = time.time()
        try:
            api = get_api(p["id"])
            if not api.is_configured():
                return p, "bad", "تنظیمات پنل ناقص است"
            svcs = await asyncio.wait_for(api.list_services(True), 12)
            ms = int((time.time() - t0) * 1000)
            return p, ("ok" if svcs else "warn"), (f"متصل · {ms} میلی‌ثانیه" if svcs else "متصل شد ولی سرویسی برنگشت")
        except Exception as e:  # noqa: BLE001
            return p, "bad", f"قطع: {str(e)[:100] or type(e).__name__}"

    async def chk_db():
        if not (cfg["sales"].get("db") or {}).get("name"):
            return "warn", "دیتابیس فروش تنظیم نشده"
        try:
            ms = await asyncio.wait_for(asyncio.to_thread(sales.db_ping), 12)
            return "ok", f"متصل · {ms} میلی‌ثانیه"
        except Exception as e:  # noqa: BLE001
            return "bad", f"قطع: {str(e)[:100] or type(e).__name__}"

    panel_res, db_res, bot_st = await asyncio.gather(
        asyncio.gather(*[chk_panel(p) for p in cfg["panels"]]), chk_db(), _service_active("rebecca-bot"))
    if not cfg["panels"]:
        add("panel", "پنل ربکا", "bad", "هیچ پنلی تنظیم نشده")
    for p, lv, tx in panel_res:
        add("panel", f"پنل «{p.get('name') or p['id']}»", lv, tx)
    add("db", "دیتابیس فروش", *db_res)
    if bot_st is None:
        add("bot", "سرویس ربات", "warn", "وضعیت قابل بررسی نیست (systemctl در دسترس نیست)")
    else:
        add("bot", "سرویس ربات تلگرام", "ok" if bot_st == "active" else "bad", "در حال اجرا" if bot_st == "active" else f"وضعیت: {bot_st} ← rebecca-bot restart")
    add("tg", "اعلان تلگرام", "ok" if (cfg["bot_token"] and cfg["admin_ids"]) else "warn",
        f"{len(cfg['admin_ids'])} ادمین دریافت‌کننده" if (cfg["bot_token"] and cfg["admin_ids"]) else "توکن یا شناسه‌ی ادمین تنظیم نشده؛ هشدارها نمی‌رسند")

    try:
        du = shutil.disk_usage(os.path.dirname(C.CONFIG_FILE) or ".")
        pct = du.free * 100 // du.total
        add("disk", "فضای دیسک", "ok" if pct >= 15 else ("warn" if pct >= 7 else "bad"), f"{du.free / 2**30:.1f} گیگ آزاد ({pct}٪)")
    except OSError:
        add("disk", "فضای دیسک", "warn", "قابل بررسی نیست")
    try:
        st = os.stat(C.CONFIG_FILE)
        mode = stat.S_IMODE(st.st_mode)
        add("cfgfile", "فایل تنظیمات", "ok" if mode & 0o077 == 0 else "warn",
            f"{st.st_size / 1024:.0f} کیلوبایت · دسترسی {oct(mode)[2:]}" + ("" if mode & 0o077 == 0 else " ← باید 600 باشد: chmod 600 config.json"))
    except OSError:
        add("cfgfile", "فایل تنظیمات", "bad", "فایل پیدا نشد")

    bk = cfg["backup"]
    last = sales.backup_last_ok()
    iv = int(bk.get("interval_min") or 1440)
    if not bk.get("enabled"):
        add("backup", "بکاپ خودکار", "warn", "خاموش است؛ بدون بکاپ، خرابیِ دیسک یعنی از دست رفتن موجودی‌ها")
    elif not last:
        add("backup", "بکاپ خودکار", "warn", "روشن است ولی هنوز بکاپ موفقی ثبت نشده")
    else:
        age = time.time() - last
        lim = 26 * 3600
        late = age > lim and (iv >= 1440)
        add("backup", "آخرین بکاپ", "bad" if late else ("warn" if age > lim else "ok"),
            f"{_fmt_dur(age)} پیش ارسال شد" + ("" if age <= lim else (" ← بکاپ روزانه عقب افتاده؛ تلگرام/ربات را بررسی کنید" if iv >= 1440 else " (در بازه‌های کوتاه فقط با تغییر تنظیمات بکاپ می‌آید؛ اگر تغییری نبوده طبیعی است)")))

    pend = [t for t in cfg["wallet_tx"] if t["state"] == "pending" and t["kind"] == "purchase"]
    stale = [t for t in pend if time.time() - t["ts"] > 600]
    tops = sum(1 for t in cfg["topups"] if t.get("status") == "pending")
    neg = [r for r in cfg["reps"].values() if r["balance"] < 0]
    lv = "bad" if (stale or neg) else ("warn" if (pend or tops) else "ok")
    bits = [f"موجودی کل نماینده‌ها {sum(r['balance'] for r in cfg['reps'].values()):,} تومان"]
    if tops:
        bits.append(f"{tops} درخواست افزایش موجودی منتظر تأیید")
    if pend:
        bits.append(f"{len(pend)} خرید در حال انجام" + (f" ({len(stale)} مورد بیش از ۱۰ دقیقه — بررسی کنید)" if stale else ""))
    if neg:
        bits.append("موجودی منفی دارد!")
    add("wallet", "کیف پول نماینده‌ها", lv, " · ".join(bits))

    try:
        rc = ops.reconcile(cfg, ops.recon_load_state().get("snap"))
        rs = ops.recon_summary(rc)
        add("recon", "تطابق مالی کیف پول‌ها", rs["level"],
            "موجودی همه‌ی نماینده‌ها با تراکنش‌ها می‌خواند" if rs["ok"] else f"{rs['count']} اختلاف پیدا شد: " + "؛ ".join(f"{i['name']} ({i['text']})" for i in rs["issues"][:3]))
    except Exception:  # noqa: BLE001
        log.exception("health recon")

    now = time.time()
    fails = sum(1 for x in cfg.get("login_log", []) if not x.get("ok") and now - float(x.get("ts") or 0) < 86400)
    add("2fa", "ورود دومرحله‌ای", "ok" if C.totp_enabled() else "warn", "روشن است" if C.totp_enabled() else "خاموش است؛ برای محافظت از کیف پول در «امنیت ورود» روشنش کنید")
    add("https", "اتصال امن", "ok" if request.url.scheme == "https" else "warn", "HTTPS فعال" if request.url.scheme == "https" else "HTTP ساده؛ رمز و کوکی بدون رمزنگاری می‌رود (SSL را فعال کنید)")
    add("fails", "تلاش‌های ناموفق ورود (۲۴ ساعت)", "ok" if fails < 10 else ("warn" if fails < 30 else "bad"), f"{fails} مورد")

    overall = max((i["level"] for i in items), key=lambda x: _LV[x])
    return {"overall": overall, "items": items, "uptime": _fmt_dur(time.time() - _STARTED), "ts": time.time()}


# ───────────────────────────── SSL و پورت وب‌پنل ─────────────────────────────
def _panel_domain(cfg) -> str:
    p = get_panel(cfg, cfg["active_panel"]) or (cfg["panels"][0] if cfg["panels"] else None)
    return tlsutil._domain_of(p["panel_url"]) if p else ""


def _web_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host}" + ("" if (scheme, port) in (("https", 443), ("http", 80)) else f":{port}")


@app.get("/api/ssl", dependencies=[Depends(guard)])
async def ssl_get(request: Request):
    c = C.load_config_ro()
    ss = c["ssl"]
    info = await asyncio.to_thread(tlsutil.cert_info, ss["cert"]) if ss["cert"] else {}
    return {"enabled": ss["enabled"], "active": os.environ.get("REBECCA_SSL_ACTIVE") == "1", "cert": ss["cert"], "key": ss["key"],
            "domain": ss["domain"] or _panel_domain(c), "error": ss["error"], "port": c["web_port"],
            "info": info, "scheme": request.url.scheme,
            "launcher": os.environ.get("REBECCA_LAUNCHER", "")}


@app.post("/api/ssl/detect", dependencies=[Depends(guard)])
async def ssl_detect():
    c = load_config()
    items = await asyncio.to_thread(tlsutil.detect, (get_panel(c, c["active_panel"]) or {}).get("panel_url", ""))
    return {"items": items}


class SslReq(BaseModel):
    enabled: bool
    cert: str = Field(default="", max_length=500)
    key: str = Field(default="", max_length=500)
    domain: str = Field(default="", max_length=253)
    port: int = Field(default=8686, ge=1, le=65535)


@app.post("/api/ssl", dependencies=[Depends(guard)])
async def ssl_save(r: SslReq, request: Request):
    cert, key, domain = r.cert.strip(), r.key.strip(), r.domain.strip().lower()
    if domain and not re.fullmatch(r"[a-z0-9.-]{1,253}", domain):
        raise ValueError("دامنه نامعتبر است.")
    if r.enabled:
        err = await asyncio.to_thread(tlsutil.check_pair, cert, key)
        if err:
            raise ValueError(err)
    old = load_config()
    if r.port != old["web_port"]:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            try:
                sk.bind(("0.0.0.0", r.port))
            except OSError:
                raise ValueError(f"پورت {r.port} روی سرور در حال استفاده است؛ پورت دیگری بدهید.")
    changed = (old["ssl"]["enabled"], old["ssl"]["cert"], old["ssl"]["key"], old["web_port"]) != (r.enabled, cert, key, r.port)

    def mut(c):
        c["ssl"].update(enabled=r.enabled, cert=cert, key=key, domain=domain, error="")
        c["web_port"] = r.port
    cfg = update_config(mut)
    host = (domain or _panel_domain(cfg)) if r.enabled else (request.url.hostname or "")
    url = _web_url("https" if r.enabled else "http", host or request.url.hostname or "", r.port)
    _aud("تغییر SSL و پورت وب‌پنل", "", f"SSL: {'روشن' if r.enabled else 'خاموش'}؛ پورت: {r.port}؛ دامنه: {domain or '-'}", "sys")
    if changed:
        t = asyncio.create_task(_restart_self())
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    return {"ok": True, "restart": changed, "url": url}


# ───────────────────────────── بکاپ و بازیابی ─────────────────────────────
class BackupSettingsReq(BaseModel):
    enabled: bool
    interval_min: int | None = Field(default=None, ge=5, le=1440)       # ۱۴۴۰ = روزانه؛ کمتر = هر N دقیقه (فقط در صورت تغییر)
    password: str | None = Field(default=None, max_length=128)           # None = بدون تغییر؛ "" = حذف رمز؛ غیر خالی = رمز جدید (حداقل ۶ کاراکتر)
    live: bool | None = None                                             # بکاپ آنلاین: یک پیام که خودش به‌روز می‌شود
    live_chat: str | None = Field(default=None, max_length=64)           # شناسه‌ی کانال/گروه خصوصی (خالی = پیام خصوصی ادمین‌ها)
    bot_token: str | None = Field(default=None, max_length=128)          # توکن «ربات بکاپ» جدا (خالی = حذف و استفاده از ربات اصلی)
    code: str = Field(default="", max_length=64)


class RestoreReq(BaseModel):
    content: str = Field(min_length=2, max_length=2_000_000)
    wallet_mode: str = Field(default="keep", pattern="^(keep|restore)$")   # keep = موجودی/تراکنش‌های فعلی حفظ شود (امن)؛ restore = همه چیز از بکاپ
    confirm_wallet: bool = False
    code: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=128)        # رمز بکاپ (فقط برای فایل‌های رمزنگاری‌شده)


def _parse_restore(content: str, password: str = "") -> tuple[dict, dict, dict | None]:
    """فایل بکاپ (رمزنگاری‌شده یا ساده؛ کامل یا config.json) ← (داده‌ی خام، کانفیگ نرمال‌شده، {attach_log, sales_overrides} یا None)."""
    import json
    if C.is_encrypted_backup(content):
        if not password:
            raise HTTPException(400, "این بکاپ رمزنگاری شده است؛ رمز بکاپ را وارد کنید.")
        try:
            content = C.decrypt_backup(content, password).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(400, str(e))
    try:
        data = json.loads(content)
    except ValueError:
        raise HTTPException(400, "فایل JSON معتبر نیست.")
    extra = None
    if isinstance(data, dict) and data.get("format") == sales.BACKUP_FORMAT:       # بکاپ کامل (همانی که در تلگرام می‌آید)
        extra = {"attach_log": data.get("attach_log"), "sales_overrides": data.get("sales_overrides")}
        data = data.get("config")
    if not isinstance(data, dict) or not (set(data) & {"panels", "panel_url", "bot_token", "sales", "templates"}):
        raise HTTPException(400, "این فایل شبیه بکاپ تنظیمات ربات نیست.")
    return data, C._normalize(data), extra


@app.post("/api/backup/restore/preview", dependencies=[Depends(guard)])
async def backup_restore_preview(r: RestoreReq):
    """قبل از بازیابی: تفاوت موجودی نماینده‌ها و تراکنش‌هایی که از دست می‌رود (چیزی تغییر نمی‌کند)."""
    data, new, _ = _parse_restore(r.content, r.password)
    cur = load_config()
    if "reps" not in data:
        return {"old_format": True, "needs_code": C.totp_enabled(), "diff": None}
    d = C.restore_diff(cur, new)
    return {"old_format": False, "needs_code": C.totp_enabled(), "diff": d}


@app.get("/api/backup", dependencies=[Depends(guard)])
async def backup_download():
    try:
        data = await asyncio.to_thread(C.config_backup_bytes)
    except OSError:
        raise HTTPException(404, "فایل تنظیمات پیدا نشد.")
    ext = "json"
    pw = load_config()["backup"].get("password") or ""
    if pw:                                                       # با رمز تنظیم‌شده، دانلود هم رمزنگاری می‌شود
        data = await asyncio.to_thread(C.encrypt_backup, data, pw)
        ext = "enc"
    return Response(data, media_type="application/json" if ext == "json" else "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="rebecca-config-{sales.tehran_day()}.{ext}"'})


@app.post("/api/backup/settings", dependencies=[Depends(guard)])
async def backup_settings(r: BackupSettingsReq, request: Request):
    if r.password is not None:
        if r.password and len(r.password) < 6:
            raise HTTPException(400, "رمز بکاپ حداقل ۶ کاراکتر باشد.")
        if r.password != (load_config()["backup"].get("password") or ""):
            await _gate_code(request, r.code, None, "تغییر رمز بکاپ", always=True)

    cur_bk = load_config()["backup"]
    need_gate, bot_user = False, None
    if r.live_chat is not None:
        r.live_chat = r.live_chat.strip()
        if r.live_chat and not re.fullmatch(r"-?\d{5,20}|@[A-Za-z0-9_]{5,32}", r.live_chat):
            raise HTTPException(400, "شناسه‌ی کانال/گروه باید عددی (مثل -1001234567890) یا @username باشد.")
        need_gate = need_gate or r.live_chat != (cur_bk.get("live_chat") or "")
    if r.bot_token is not None:
        r.bot_token = r.bot_token.strip()
        if r.bot_token:
            if not re.fullmatch(r"\d{6,14}:[A-Za-z0-9_-]{30,60}", r.bot_token):
                raise HTTPException(400, "فرمت توکن ربات درست نیست (مثل 123456789:AAH...).")
            try:                                                    # قبل از ذخیره مطمئن شو توکن واقعاً کار می‌کند
                from telegram import Bot
                me = await asyncio.wait_for(Bot(r.bot_token).get_me(), 20)
                bot_user = "@" + (me.username or str(me.id))
            except Exception:  # noqa: BLE001
                raise HTTPException(400, "تلگرام این توکن را قبول نکرد؛ توکن را از @BotFather دوباره کپی کنید.")
        need_gate = need_gate or r.bot_token != (cur_bk.get("bot_token") or "")
    if need_gate:                                                   # مقصد/فرستنده‌ی بکاپ = دسترسی به توکن‌ها و رمزها
        await _gate_code(request, r.code, None, "تغییر مقصد یا ربات بکاپ", always=True)

    def mut(c):
        c["backup"]["enabled"] = r.enabled
        if r.interval_min is not None:
            c["backup"]["interval_min"] = r.interval_min
        if r.live is not None:
            c["backup"]["live"] = r.live
        if r.live_chat is not None:
            c["backup"]["live_chat"] = r.live_chat
        if r.bot_token is not None:
            c["backup"]["bot_token"] = r.bot_token
        if r.password is not None:
            c["backup"]["password"] = r.password
    update_config(mut)
    if r.password is not None:
        _audit("ادمین وب‌پنل", "رمز بکاپ", "", "تنظیم شد" if r.password else "حذف شد")
    if r.bot_token is not None:
        _audit("ادمین وب‌پنل", "ربات بکاپ", "", f"تنظیم شد ({bot_user})" if r.bot_token else "حذف شد")
    return {"ok": True, "bot": bot_user}


@app.post("/api/backup/send", dependencies=[Depends(guard)])
async def backup_send():
    cfg = load_config()
    if not (cfg["bot_token"] and cfg["admin_ids"]):
        raise HTTPException(400, "توکن ربات یا ادمین تنظیم نشده")
    n = await sales.send_backup(sales.backup_bot(_tg_bot(cfg["bot_token"]), cfg), cfg["admin_ids"], "💾 بکاپ تنظیمات — جای امن نگه دارید.")
    if not n:
        raise HTTPException(502, "ارسال به تلگرام ناموفق بود (ربات را در تلگرام Start کرده‌اید؟)")
    return {"sent": n}


@app.post("/api/backup/restore", dependencies=[Depends(guard)])
async def backup_restore(r: RestoreReq, request: Request):
    """فایل بکاپ را اعتبارسنجی، از وضعیت فعلی نسخه‌ی پشتیبان می‌گیرد، اعمال می‌کند و ربات را ریستارت می‌کند.
    پیش‌فرض (wallet_mode=keep): فقط تنظیمات برمی‌گردد و موجودی/تراکنش‌های فعلی حفظ می‌شود. ورود دومرحله‌ای هیچ‌وقت خاموش نمی‌شود."""
    import shutil
    data, new, extra = _parse_restore(r.content, r.password)
    await _gate_code(request, r.code, None, "بازیابی بکاپ", always=True)
    cur = load_config()
    if not (new.get("web") or {}).get("hash"):          # بکاپ بدون رمز وب: رمز فعلی حفظ شود
        new["web"] = cur["web"]
    has_reps = "reps" in data
    if not has_reps:                                    # بکاپ قدیمی (قبل از نماینده‌ها): نماینده‌های فعلی پاک نشوند
        new["reps"], new["rep_users"], new["rep_notes"], new["rep_paid"] = cur["reps"], cur["rep_users"], cur["rep_notes"], cur["rep_paid"]
    # سرور خالی/تازه (بدون موجودی و تراکنش): کیف پول همیشه از بکاپ برگردد
    _sh = C.shop_totals(cur)
    fresh_srv = (not any(x["balance"] for x in cur["reps"].values()) and not cur["wallet_tx"]
                 and not _sh["customers"] and not _sh["orders"])
    keep = (r.wallet_mode == "keep" and not fresh_srv) or not has_reps
    if not keep and not r.confirm_wallet and not fresh_srv:
        raise HTTPException(400, "برای برگرداندنِ موجودی و تراکنش‌ها از بکاپ، تأیید صریح لازم است.")
    C.preserve_security(new, cur)
    if keep:
        C.restore_keep_wallet(new, cur)
    web_changed = (new["web"].get("hash"), new["web"].get("user")) != (cur["web"].get("hash"), cur["web"].get("user"))
    safety = None
    if os.path.exists(C.CONFIG_FILE):
        safety = f"{C.CONFIG_FILE}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(C.CONFIG_FILE, safety)
        os.chmod(safety, 0o600)
        try:
            import glob
            for _old in sorted(glob.glob(C.CONFIG_FILE + ".pre-restore-*"))[:-5]:
                os.remove(_old)
        except OSError:
            log.warning("پاک‌سازی نسخه‌های قدیمی pre-restore ممکن نشد")
    C.save_config(new)
    if extra:                                                   # لاگ فروش و ویرایش‌های گزارش (فقط در بکاپ کامل هست)
        if isinstance(extra.get("attach_log"), list):
            sales.attach_import(extra["attach_log"])
        ov = extra.get("sales_overrides")
        if isinstance(ov, dict):
            base = {"hide": {}, "edit": {}, "spans": []}
            base.update({k: ov[k] for k in base if isinstance(ov.get(k), type(base[k]))})
            sales._save_ovr(base)
    for p in {p["id"] for p in cur["panels"]} | {p["id"] for p in new["panels"]}:
        C.drop_api(p)
    _bot.update(token=None, obj=None)
    _audit("ادمین وب‌پنل", "بازیابی بکاپ", "", f"حالت کیف پول: {'حفظ وضعیت فعلی' if keep else 'برگردانده‌شده از بکاپ'}؛ نسخه‌ی پشتیبان: {os.path.basename(safety) if safety else '-'}")
    _bg_notify(f"♻️ <b>بکاپ بازیابی شد</b>\nکیف پول: {'وضعیت فعلی حفظ شد' if keep else '⚠️ از بکاپ برگردانده شد'}\nIP: <code>{C.esc(_client_ip(request))}</code>")
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "restart", "rebecca-bot")
        asyncio.create_task(proc.wait())
    except Exception:  # noqa: BLE001
        log.warning("ریستارت خودکار ربات ممکن نشد")
    fresh = load_config()
    return {"ok": True, "panels": len(fresh["panels"]), "reps": len(fresh["reps"]),
            "balance": sum(x["balance"] for x in fresh["reps"].values()),
            "shop": C.shop_totals(fresh),
            "wallet_kept": keep, "web_changed": web_changed,
            "recon": ops.recon_summary(ops.reconcile(fresh, None), 8)}


@app.get("/api/reconcile", dependencies=[Depends(guard)])
async def reconcile_api():
    """بررسی تطابق مالی: موجودی هر نماینده با زنجیره‌ی تراکنش‌هایش و با وضعیت بررسی قبلی (چیزی تغییر نمی‌کند)."""
    return await asyncio.to_thread(ops.reconcile_now, False)


@app.post("/api/backup/verify", dependencies=[Depends(guard)])
async def backup_verify(r: RestoreReq):
    """فقط بررسی فایل بکاپ (رمزگشایی + اعتبارسنجی + خلاصه‌ی محتوا)؛ هیچ چیزی تغییر نمی‌کند."""
    data, new, extra = _parse_restore(r.content, r.password)
    s = C.backup_summary(new)
    return {"ok": True, "encrypted": C.is_encrypted_backup(r.content), "full": extra is not None,
            "old_format": "reps" not in data, "panels": s["panels"], "reps": s["reps"], "rep_users": s["rep_users"],
            "balance": sum(x["balance"] for x in new["reps"].values()),
            "shop": C.shop_totals(new),
            "attach_log": len((extra or {}).get("attach_log") or [])}


# ───────────────────────────── داشبورد نماینده ─────────────────────────────
@app.get("/api/rep/dashboard")
async def rep_dashboard(rep=Depends(rep_guard)):
    """خلاصه‌ی عددی + سری زمانی ۳۰ روز فروش برای نمودارهای داشبورد نماینده."""
    rid = rep[0]
    cfg = load_config()
    rp = cfg["reps"][rid]
    sales_30d = await asyncio.to_thread(sales.rep_daily_sales, rid, 30)
    today = sales_30d[0] if sales_30d else {"total": 0, "count": 0, "new_count": 0, "renew_count": 0, "breakdown": []}
    templates: dict = {}
    for day in sales_30d:
        for x in day.get("breakdown", []):
            name = x.get("template") or "—"
            templates.setdefault(name, {"name": name, "count": 0})["count"] += int(x.get("count") or 0)
    total_users = sum(1 for m in cfg["rep_users"].values() for r in m.values() if r == rid)

    def _row(x):
        return {"day": x["day"], "date": x["date"], "total": int(x.get("total") or 0), "count": int(x.get("count") or 0)}

    return {
        "balance": int(rp["balance"]),
        "today": {"total": int(today.get("total") or 0), "count": int(today.get("count") or 0),
                  "new_count": int(today.get("new_count") or 0), "renew_count": int(today.get("renew_count") or 0)},
        "month": {"total": sum(int(x.get("total") or 0) for x in sales_30d), "count": sum(int(x.get("count") or 0) for x in sales_30d), "days": 30},
        "sales_30d": [_row(x) for x in reversed(sales_30d)],
        "sales_7d": [_row(x) for x in reversed(sales_30d[:7])],
        "templates": sorted(templates.values(), key=lambda t: -t["count"])[:8],
        "total_users": total_users,
        "next_plan_enabled": bool(rp.get("next_plan")),
    }


_REP_STATS_CACHE: dict[str, tuple[float, dict]] = {}      # rid ← (زمان, نتیجه)؛ تا هر بار باز کردن داشبورد صدها درخواست به پنل نزند
_REP_STATS_TTL = 120


@app.get("/api/rep/dashboard/user-stats")
async def rep_dashboard_user_stats(rep=Depends(rep_guard)):
    """توزیع وضعیت کاربرهای یک نماینده (اگر بیش از ۲۰۰ کاربر باشد، نمونه‌گیری یکنواخت و تخمین)."""
    rid = rep[0]
    hit = _REP_STATS_CACHE.get(rid)
    if hit and time.time() - hit[0] < _REP_STATS_TTL:
        return hit[1]
    cfg = load_config()
    items = [(pid, u) for pid, m in cfg["rep_users"].items() for u, r in m.items() if r == rid]
    total = len(items)
    stats = {"active": 0, "on_hold": 0, "expired": 0, "limited": 0, "disabled": 0, "unknown": 0}
    cap = 200
    sample = items if total <= cap else [items[int(i * total / cap)] for i in range(cap)]
    sem = asyncio.Semaphore(10)

    async def one(pid, u):
        async with sem:
            try:
                ap = get_api(pid)
                if not ap.is_configured():
                    return None
                return (await ap.request("GET", ap.upath(u))).get("status")
            except Exception:  # noqa: BLE001
                return None

    for s in await asyncio.gather(*(one(p, u) for p, u in sample)):
        stats[s if s in stats else "unknown"] += 1
    if total > cap:
        ratio = total / cap
        for k in stats:
            stats[k] = int(round(stats[k] * ratio))
    out = {"total": total, "status_stats": stats, "sampled": total > cap}
    _REP_STATS_CACHE[rid] = (time.time(), out)
    return out


# ───────────────────────────── پنل‌های ربکا و تست اتصال ─────────────────────────────
class PanelReq(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=40)
    panel_url: str
    sub_url: str = ""
    username: str = Field(min_length=1, max_length=100)
    password: str | None = None
    verify_ssl: bool = False


@app.post("/api/panels", dependencies=[Depends(guard)])
async def panel_save(r: PanelReq):
    url = r.panel_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("آدرس پنل باید با http:// یا https:// شروع شود.")
    out = {}

    def mut(c):
        pn = get_panel(c, r.id) if r.id else None
        if r.id and not pn:
            raise ValueError("پنل پیدا نشد.")
        if not pn:
            if not r.password:
                raise ValueError("رمز عبور پنل الزامی است.")
            pn = {"id": new_panel_id(c), "services": [], "password": ""}
            c["panels"].append(pn)
            if not c["active_panel"]:
                c["active_panel"] = pn["id"]
        pn.update(name=r.name.strip(), panel_url=url, sub_url=r.sub_url.strip().rstrip("/"), username=r.username.strip(), verify_ssl=r.verify_ssl)
        if r.password:
            pn["password"] = r.password
        out["id"] = pn["id"]
    update_config(mut)
    C.drop_api(out["id"])                      # اطلاعات ورود/آدرس عوض شده؛ نشست قدیمی دور ریخته شود
    _aud("ذخیره پنل ربکا", r.name.strip(), f"آدرس: {url}؛ " + ("ویرایش" if r.id else "پنل جدید") + ("؛ رمز تغییر کرد" if r.password else ""), "sys")
    return {"ok": True, "id": out["id"]}


@app.post("/api/panels/delete", dependencies=[Depends(guard)])
async def panel_delete(r: NameReq):  # name = panel id
    def mut(c):
        c["panels"] = [p for p in c["panels"] if p["id"] != r.name]
        if c["active_panel"] == r.name:        # پنل فعالِ حذف‌شده: به اولین پنل باقی‌مانده برگرد
            c["active_panel"] = c["panels"][0]["id"] if c["panels"] else ""
    update_config(mut)
    C.drop_api(r.name)
    _aud("حذف پنل ربکا از ربات", r.name, "کاربران داخل پنل دست نخوردند", "sys")
    return {"ok": True}


@app.post("/api/panels/active", dependencies=[Depends(guard)])
async def panel_active(r: NameReq):
    def mut(c):
        if not get_panel(c, r.name):
            raise ValueError("پنل پیدا نشد.")
        c["active_panel"] = r.name
    update_config(mut)
    _aud("تغییر پنل فعال", r.name, "", "sys")
    return {"ok": True}


@app.post("/api/settings/test", dependencies=[Depends(guard)])
async def settings_test(api: RebeccaClient = Depends(P)):
    await api.login()
    s = await api.request("GET", "/api/system")
    svc = await api.list_services(force=True)
    return {"ok": True, "total_user": s.get("total_user", 0), "services": len(svc)}


# ───────────────────────────── فروشگاه: مدیریت ربات فروش از وب‌پنل ─────────────────────────────
# داده‌ها همان config.json > shop است (مشترک با ربات فروش). تأیید/رد رسید و ... با همان توابع اتمیک shop.py انجام می‌شود،
# پس اگر ادمین هم‌زمان از ربات و وب‌پنل بزند فقط یکی موفق می‌شود. پیام به مشتری با خودِ توکن ربات فروش ارسال می‌شود.
_SHOP_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_SHOP_STATUSES = ("new", "pending", "processing", "done", "rejected", "cancelled", "failed", "refunded", "credited")


def _shop_token() -> str:
    t = C.load_config_ro()["shop"]["token"]
    if not t:
        raise HTTPException(409, "ابتدا توکن ربات فروش را در بخش «ربات» همین صفحه وارد کنید.")
    return t


def _shop_actor() -> str:
    return "ادمین وب‌پنل"


def _order_view(o: dict, custs: dict) -> dict:
    cu = custs.get(str(o.get("uid"))) or {}
    return {"id": o["id"], "uid": o.get("uid"), "name": cu.get("name", ""), "username": cu.get("username", ""), "kind": o.get("kind", ""),
            "pname": o.get("pname", ""), "service": o.get("username", ""), "amount": int(o.get("amount") or 0), "status": o.get("status", ""),
            "method": o.get("method", ""), "ts": int(o.get("ts") or 0), "rts": int(o.get("rts") or 0), "dts": int(o.get("dts") or 0),
            "text": o.get("text", ""), "receipt": bool(o.get("file")), "err": o.get("err", ""), "reason": o.get("reason", ""),
            "unsure": bool(o.get("unsure")), "result": o.get("result", ""), "balance": cu.get("balance")}


def _shop_counts(s: dict) -> dict:
    cnt = {k: 0 for k in _SHOP_STATUSES}
    for o in s["orders"]:
        cnt[o["status"]] = cnt.get(o["status"], 0) + 1
    return cnt


@app.get("/api/live", dependencies=[Depends(guard)])
def live():
    """یک درخواست سبک برای اعلان‌های زنده: درخواست‌های افزایش موجودی نماینده + رسید/سفارش‌های فروشگاه که منتظر ادمین‌اند."""
    cfg = C.load_config_ro()
    reps = cfg["reps"]
    top = [{"id": t["id"], "rep": (reps.get(t["rid"], {}).get("label") or reps.get(t["rid"], {}).get("name") or t["rid"]),
            "amount": t.get("amount", 0), "ts": t.get("ts", 0)} for t in cfg["topups"] if t.get("status") == "pending"]
    s = cfg["shop"]
    custs = s["customers"]
    items = [{"id": o["id"], "kind": o.get("kind", ""), "status": o["status"], "amount": int(o.get("amount") or 0),
              "name": (custs.get(str(o.get("uid"))) or {}).get("name", "")} for o in s["orders"] if o["status"] in ("pending", "failed")]
    return {"topups": {"count": len(top), "items": top[-20:]},
            "shop": {"pending": sum(1 for x in items if x["status"] == "pending"), "failed": sum(1 for x in items if x["status"] == "failed"), "items": items[-20:]}}


@app.get("/api/shop/summary", dependencies=[Depends(guard)])
def shop_summary():
    """عدد کوچک برای نشان روی تب (هر ۳۰ ثانیه)."""
    s = C.load_config_ro()["shop"]
    cnt = _shop_counts(s)
    return {"pending": cnt["pending"], "failed": cnt["failed"], "enabled": s["enabled"], "has_token": bool(s["token"])}


@app.get("/api/shop", dependencies=[Depends(guard)])
def shop_overview():
    cfg = C.load_config_ro()
    s = cfg["shop"]
    cnt = _shop_counts(s)
    day0 = int(time.time()) - 86400
    sales_total = sum(o["amount"] for o in s["orders"] if o["status"] == "done" and o["kind"] in ("buy", "renew"))
    sales_24h = sum(o["amount"] for o in s["orders"] if o["status"] == "done" and o["kind"] in ("buy", "renew") and int(o.get("dts") or 0) >= day0)
    tok = s["token"]
    return {"enabled": s["enabled"], "panel": shop.shop_panel(cfg), "panels": [{"id": p["id"], "name": p["name"]} for p in cfg["panels"]],
            "cards": s["cards"], "welcome": s["welcome"], "support": s["support"], "min_topup": s["min_topup"], "max_topup": s["max_topup"],
            "receipt_hours": s["receipt_hours"], "has_token": bool(tok), "token_hint": ("…" + tok[-6:]) if tok else "",
            "test": s["test"], "force": s["force"], "remind": s["remind"], "plans": s["plans"], "templates": [{"name": n, "gb": t["gb"], "days": t["days"], "price": t.get("price", 0)} for n, t in cfg["templates"].items()],
            "counts": cnt, "customers": len(s["customers"]), "wallet_total": sum(c["balance"] for c in s["customers"].values()),
            "sales_total": sales_total, "sales_24h": sales_24h}


class ShopSettingsReq(BaseModel):
    enabled: bool | None = None
    panel: str | None = Field(default=None, max_length=40)
    cards: list[dict] | None = Field(default=None, max_length=10)
    welcome: str | None = Field(default=None, max_length=1000)
    support: str | None = Field(default=None, max_length=1000)
    min_topup: int | None = Field(default=None, ge=1000, le=10 ** 9)
    max_topup: int | None = Field(default=None, ge=1000, le=10 ** 10)
    receipt_hours: int | None = Field(default=None, ge=1, le=720)
    force_enabled: bool | None = None
    force_channels: str | None = Field(default=None, max_length=1500)
    test_enabled: bool | None = None
    test_template: str | None = Field(default=None, max_length=80)
    test_prefix: str | None = Field(default=None, max_length=16)
    test_hours: int | None = Field(default=None, ge=1, le=720)
    token: str | None = Field(default=None, max_length=120)
    clear_token: bool = False
    code: str = Field(default="", max_length=64)


@app.post("/api/shop/settings", dependencies=[Depends(guard)])
async def shop_settings(r: ShopSettingsReq, request: Request):
    cur = C.load_config_ro()
    tok_new = (r.token or "").strip()
    if tok_new and "…" in tok_new:           # مقدار نمایشی (مخفی‌شده) دوباره فرستاده شده؛ تغییری نیست
        tok_new = ""
    cards_new = None
    if r.cards is not None:
        cards_new = []
        for x in r.cards:
            num = re.sub(r"\D", "", str(x.get("number") or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
            if not num:
                continue
            if len(num) != 16:
                raise HTTPException(400, f"شماره کارت «{num[:6]}…» باید ۱۶ رقم باشد.")
            if any(c["number"] == num for c in cards_new):
                raise HTTPException(400, "یک کارت دو بار وارد شده است.")
            cards_new.append({"number": num, "holder": str(x.get("holder") or "").strip()[:60], "bank": str(x.get("bank") or "").strip()[:60]})
        if len(cards_new) > C.SHOP_CARDS_MAX:
            raise HTTPException(400, f"حداکثر {C.SHOP_CARDS_MAX} کارت.")
    digits = None if cards_new is None else ",".join(c["number"] for c in cards_new)
    if r.panel is not None and r.panel and not get_panel(cur, r.panel):
        raise HTTPException(400, "پنل پیدا نشد.")
    force_list = None
    if r.force_channels is not None:
        try:
            force_list = C.parse_force_channels(r.force_channels)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if r.force_enabled and not (force_list if force_list is not None else cur["shop"]["force"]["channels"]):
        raise HTTPException(400, "برای فعال‌سازی، حداقل یک کانال وارد کنید.")
    if r.test_template and r.test_template not in cur["templates"]:
        raise HTTPException(400, "تمپلت تست پیدا نشد.")
    if r.test_prefix is not None and not re.fullmatch(r"[A-Za-z0-9_]{1,16}", r.test_prefix.strip()):
        raise HTTPException(400, "پیشوند نام کاربری تست فقط حروف انگلیسی، عدد و _ (حداکثر ۱۶ کاراکتر) باشد.")
    if r.test_enabled and not (r.test_template or cur["shop"]["test"]["template"]):
        raise HTTPException(400, "ابتدا تمپلت تست را انتخاب کنید.")
    lo = r.min_topup if r.min_topup is not None else cur["shop"]["min_topup"]
    hi = r.max_topup if r.max_topup is not None else cur["shop"]["max_topup"]
    if lo > hi:
        raise HTTPException(400, "حداقل شارژ نباید از حداکثر بیشتر باشد.")
    token_changed = bool(tok_new and tok_new != cur["shop"]["token"]) or (r.clear_token and bool(cur["shop"]["token"]))
    card_changed = cards_new is not None and digits != ",".join(c["number"] for c in cur["shop"]["cards"])
    if token_changed or card_changed:        # توکن و شماره کارت = مسیر تغییر مقصد پرداخت/تصاحب ربات؛ با 2FA روشن کد تازه می‌خواهد
        await _gate_code(request, r.code, None, "تغییر توکن ربات فروش یا شماره کارت", always=True)
    username = ""
    if tok_new and token_changed:
        if not _SHOP_TOKEN_RE.match(tok_new):
            raise HTTPException(400, "فرمت توکن ربات فروش صحیح نیست.")
        if tok_new == cur["bot_token"]:
            raise HTTPException(400, "توکن ربات فروش باید با توکن ربات مدیریت فرق داشته باشد (دو ربات نمی‌توانند یک توکن را هم‌زمان بخوانند).")
        try:
            async with Bot(tok_new) as b:
                me = await asyncio.wait_for(b.get_me(), 15)
            username = me.username or ""
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"توکن معتبر نیست یا تلگرام جواب نداد ({e.__class__.__name__}).")

    def mut(c):
        sh = c["shop"]
        if r.enabled is not None:
            sh["enabled"] = r.enabled
        if r.panel is not None:
            sh["panel"] = r.panel
        if cards_new is not None:
            sh["cards"] = cards_new
            sh["card"] = dict(cards_new[0]) if cards_new else {"number": "", "holder": "", "bank": ""}
        if r.welcome is not None:
            sh["welcome"] = r.welcome.strip()[:1000]
        if r.support is not None:
            sh["support"] = r.support.strip()[:1000]
        if r.min_topup is not None:
            sh["min_topup"] = r.min_topup
        if r.max_topup is not None:
            sh["max_topup"] = r.max_topup
        if r.receipt_hours is not None:
            sh["receipt_hours"] = r.receipt_hours
        if force_list is not None:
            sh["force"]["channels"] = force_list
        if r.force_enabled is not None:
            sh["force"]["enabled"] = r.force_enabled
        if r.test_enabled is not None:
            sh["test"]["enabled"] = r.test_enabled
        if r.test_template is not None:
            sh["test"]["template"] = r.test_template
        if r.test_prefix is not None:
            sh["test"]["prefix"] = r.test_prefix.strip()
        if r.test_hours is not None:
            sh["test"]["hours"] = r.test_hours
        if tok_new and token_changed:
            sh["token"] = tok_new
        elif r.clear_token:
            sh["token"] = ""
    update_config(mut)
    what = [k for k, v in (("وضعیت باز/بسته", r.enabled is not None), ("پنل فروش", r.panel is not None), ("کارت‌ها", cards_new is not None),
                           ("متن‌ها", r.welcome is not None or r.support is not None), ("محدوده شارژ/مهلت رسید", r.min_topup is not None or r.max_topup is not None or r.receipt_hours is not None),
                           ("عضویت اجباری", r.force_enabled is not None or force_list is not None), ("تست رایگان", r.test_enabled is not None or r.test_template is not None or r.test_prefix is not None or r.test_hours is not None),
                           ("توکن ربات فروش", token_changed)) if v]
    _aud("تنظیمات فروشگاه", "", "، ".join(what) + (f" — " + "، ".join("****" + c["number"][-4:] for c in cards_new) if cards_new else ""), "sys")
    restarted = False
    if token_changed:                         # Runner فقط داخل پروسه‌ی ربات است؛ برای اتصال/قطع ربات فروش ربات مدیریت ریستارت می‌شود
        try:
            proc = await asyncio.create_subprocess_exec("systemctl", "restart", "rebecca-bot")
            t = asyncio.create_task(proc.wait())
            _bg.add(t)
            t.add_done_callback(_bg.discard)
            restarted = True
        except Exception:  # noqa: BLE001
            log.warning("ریستارت خودکار ربات برای ربات فروش ممکن نشد؛ دستی: rebecca-bot restart")
    return {"ok": True, "bot_restarted": restarted, "username": username}


# ── پلن‌ها و قیمت‌ها ──
class ShopPlanReq(BaseModel):
    template: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=60)
    price: int = Field(ge=1, le=10 ** 9)


class ShopPlanEditReq(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    price: int | None = Field(default=None, ge=0, le=10 ** 9)
    active: bool | None = None


@app.post("/api/shop/plans", dependencies=[Depends(guard)])
def shop_plan_add(r: ShopPlanReq):
    out: dict = {}

    def mut(c):
        if r.template not in c["templates"]:
            raise ValueError("تمپلت پیدا نشد.")
        ids = {x["id"] for x in c["shop"]["plans"]}
        n = 1
        while f"p{n}" in ids:
            n += 1
        pl = {"id": f"p{n}", "name": (r.name.strip() or r.template)[:60], "template": r.template, "price": r.price, "active": True}
        c["shop"]["plans"].append(pl)
        out.update(pl)
    update_config(mut)
    _aud("پلن فروشگاه", out["id"], f"افزودن «{out['name']}» — قیمت {r.price:,}", "sys")
    return {"ok": True, "plan": out}


@app.post("/api/shop/plans/{pid}", dependencies=[Depends(guard)])
def shop_plan_edit(pid: str, r: ShopPlanEditReq):
    out: dict = {}

    def mut(c):
        pl = shop.plan_get(c["shop"], pid)
        if not pl:
            raise ValueError("پلن پیدا نشد.")
        if r.name is not None and r.name.strip():
            pl["name"] = r.name.strip()[:60]
        if r.price is not None:
            pl["price"] = r.price
        if r.active is not None:
            pl["active"] = r.active
        out.update(pl)
    update_config(mut)
    _aud("پلن فروشگاه", pid, f"ویرایش «{out['name']}» — قیمت {out['price']:,}" + ("" if out["active"] else " (غیرفعال)"), "sys")
    return {"ok": True, "plan": out}


@app.post("/api/shop/plans/{pid}/delete", dependencies=[Depends(guard)])
def shop_plan_delete(pid: str):
    def mut(c):
        if not shop.plan_get(c["shop"], pid):
            raise ValueError("پلن پیدا نشد.")
        c["shop"]["plans"] = [x for x in c["shop"]["plans"] if x["id"] != pid]
    update_config(mut)
    _aud("پلن فروشگاه", pid, "حذف", "sys")
    return {"ok": True}


# ── سفارش‌ها و رسیدها ──
@app.get("/api/shop/orders", dependencies=[Depends(guard)])
def shop_orders(status: str = Query("open", max_length=12), kind: str = Query("", max_length=8), q: str = Query("", max_length=40),
                      offset: int = Query(0, ge=0, le=10 ** 5), limit: int = Query(20, ge=1, le=50)):
    s = C.load_config_ro()["shop"]
    custs = s["customers"]
    needle = q.strip().lstrip("@#").lower()
    items = []
    for o in reversed(s["orders"]):
        st = o["status"]
        if status == "open" and st not in ("pending", "failed"):
            continue
        if status not in ("open", "all", "") and st != status:
            continue
        if kind and o.get("kind") != kind:
            continue
        if needle:
            cu = custs.get(str(o["uid"])) or {}
            if not (needle == str(o["uid"]) or needle == o["id"].lower() or needle in (cu.get("username") or "").lower() or needle in (cu.get("name") or "").lower()
                    or needle in str(o.get("username") or "").lower()):
                continue
        items.append(o)
    page = items[offset:offset + limit]
    return {"total": len(items), "items": [_order_view(o, custs) for o in page]}


_receipt_cache: dict[str, tuple[str, bytes]] = {}          # file_id ← (content-type, bytes)؛ حداکثر ۴۰ عکس اخیر (باز کردن دوباره‌ی رسید بدون رفت‌وبرگشت به تلگرام)


@app.get("/api/shop/orders/{oid}/receipt", dependencies=[Depends(guard)])
async def shop_receipt(oid: str):
    o = shop.order_get(oid)
    if not o or not o.get("file"):
        raise HTTPException(404, "عکس رسید پیدا نشد.")
    hit = _receipt_cache.get(o["file"])
    if hit:
        return Response(hit[1], media_type=hit[0], headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})
    try:
        async with Bot(_shop_token()) as b:
            f = await asyncio.wait_for(b.get_file(o["file"]), 20)
            data = bytes(await asyncio.wait_for(f.download_as_bytearray(), 30))
            path = (f.file_path or "").lower()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"دریافت عکس از تلگرام ناموفق بود ({e.__class__.__name__}).")
    ctype = "image/png" if path.endswith(".png") else "image/webp" if path.endswith(".webp") else "image/gif" if path.endswith(".gif") else "image/jpeg"
    _receipt_cache[o["file"]] = (ctype, data)
    while len(_receipt_cache) > 40:
        _receipt_cache.pop(next(iter(_receipt_cache)))
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


async def _shop_fulfil(oid: str) -> dict:
    async with Bot(_shop_token()) as b:
        await shop.fulfil(b, oid)
    o = shop.order_get(oid) or {}
    return {"status": o.get("status", ""), "result": o.get("result", ""), "err": o.get("err", "")}


def _claim_web(oid: str, allowed: tuple) -> dict:
    try:
        return shop.order_claim(oid, 0, allowed)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/shop/orders/{oid}/approve", dependencies=[Depends(guard)])
async def shop_order_approve(oid: str):
    _shop_token()
    o = await asyncio.to_thread(_claim_web, oid, ("pending",))
    _aud("تأیید رسید فروشگاه", oid, f"{shop.KIND_FA.get(o['kind'], o['kind'])} — {o['amount']:,} تومان", "wallet")
    return {"ok": True, **(await _shop_fulfil(oid))}


@app.post("/api/shop/orders/{oid}/retry", dependencies=[Depends(guard)])
async def shop_order_retry(oid: str):
    _shop_token()
    await asyncio.to_thread(_claim_web, oid, ("failed",))
    _aud("تلاش مجدد سفارش فروشگاه", oid, "", "wallet")
    return {"ok": True, **(await _shop_fulfil(oid))}


@app.post("/api/shop/orders/{oid}/credit", dependencies=[Depends(guard)])
async def shop_order_credit(oid: str, request: Request):
    """سفارشِ ناموفق را انجام نمی‌دهد و مبلغش را به کیف پول مشتری اضافه می‌کند."""
    token = _shop_token()
    o = await asyncio.to_thread(_claim_web, oid, ("failed",))
    try:
        w = await asyncio.to_thread(shop.wallet_apply, o["uid"], o["amount"], "refund", f"اعتبار سفارش #{oid}", "credit:" + oid)
        o = await asyncio.to_thread(shop.order_update, oid, status="credited", dts=int(time.time())) or o
    except ValueError as e:
        await asyncio.to_thread(shop.order_update, oid, status="failed")      # برگردان تا ادمین دوباره تلاش کند
        raise HTTPException(409, str(e))
    _aud("اعتبار سفارش فروشگاه به کیف پول", str(o["uid"]), f"#{oid}", "wallet")
    async with Bot(token) as b:
        await shop._tell(b, o["uid"], f"💰 سفارش #{oid} انجام نشد و مبلغ <b>{shop.money(o['amount'])}</b> تومان به کیف پول شما اضافه شد.\nموجودی: <b>{shop.money(w['balance'])}</b> تومان")
        await shop._edit_admin_msgs(b, o, "💰 توسط ادمین وب‌پنل به کیف پول مشتری اضافه شد.")
    return {"ok": True, "balance": w["balance"]}


class ShopRejectReq(BaseModel):
    reason_key: str = Field(default="a", max_length=2)
    reason: str = Field(default="", max_length=200)


@app.post("/api/shop/orders/{oid}/reject", dependencies=[Depends(guard)])
async def shop_order_reject(oid: str, r: ShopRejectReq):
    token = _shop_token()
    reason = r.reason.strip() or shop.REJECT_REASONS.get(r.reason_key, shop.REJECT_REASONS["a"])
    try:
        o = await asyncio.to_thread(shop.order_reject, oid, 0, reason)
    except ValueError as e:
        raise HTTPException(409, str(e))
    _aud("رد رسید فروشگاه", oid, reason, "wallet")
    async with Bot(token) as b:
        await shop._tell(b, o["uid"], f"❌ <b>رسید سفارش #{o['id']} رد شد.</b>\nدلیل: {C.esc(reason)}\n\nاگر پرداخت کرده‌اید با پشتیبانی تماس بگیرید.")
        await shop._edit_admin_msgs(b, o, "❌ توسط ادمین وب‌پنل رد شد.")
    return {"ok": True}


# ── مشتری‌ها ──
def _cust_view(uid: str, c: dict, norders: int) -> dict:
    return {"uid": uid, "name": c["name"], "username": c["username"], "balance": c["balance"], "joined": c["joined"], "banned": c["banned"],
            "services": len(c["services"]), "orders": norders, "test": c.get("test", 0), "test_user": c.get("test_user", "")}


@app.get("/api/shop/customers", dependencies=[Depends(guard)])
def shop_customers(q: str = Query("", max_length=40), offset: int = Query(0, ge=0, le=10 ** 5), limit: int = Query(20, ge=1, le=50)):
    s = C.load_config_ro()["shop"]
    needle = q.strip().lstrip("@").lower()
    norders: dict[str, int] = {}
    for o in s["orders"]:
        norders[str(o["uid"])] = norders.get(str(o["uid"]), 0) + 1
    rows = [(uid, c) for uid, c in s["customers"].items()
            if not needle or needle == uid or needle in (c["username"] or "").lower() or needle in (c["name"] or "").lower()]
    rows.sort(key=lambda kv: -kv[1]["joined"])
    return {"total": len(rows), "items": [_cust_view(u, c, norders.get(u, 0)) for u, c in rows[offset:offset + limit]]}


@app.get("/api/shop/customers/{uid}", dependencies=[Depends(guard)])
def shop_customer(uid: str):
    s = C.load_config_ro()["shop"]
    c = s["customers"].get(uid)
    if not c:
        raise HTTPException(404, "مشتری پیدا نشد.")
    mine = [o for o in s["orders"] if str(o["uid"]) == uid]
    return {**_cust_view(uid, c, len(mine)), "service_list": [x["u"] for x in c["services"][-30:]], "log": c["log"][-15:][::-1],
            "recent": [_order_view(o, s["customers"]) for o in mine[-8:][::-1]]}


class ShopWalletReq(BaseModel):
    amount: int = Field(ge=1, le=10 ** 9)
    sign: int = Field(default=1)
    note: str = Field(default="", max_length=80)
    code: str = Field(default="", max_length=64)


@app.post("/api/shop/customers/{uid}/wallet", dependencies=[Depends(guard)])
async def shop_customer_wallet(uid: str, r: ShopWalletReq, request: Request):
    if r.sign not in (1, -1):
        raise HTTPException(400, "نوع عملیات نامعتبر است.")
    if not uid.lstrip("-").isdigit():
        raise HTTPException(400, "شناسه نامعتبر است.")
    await _gate_code(request, r.code, r.amount)
    key = (request.headers.get("idempotency-key") or "").strip()[:40]
    txid = ("web:" + key) if re.fullmatch(r"[A-Za-z0-9_-]{8,40}", key) else None
    note = (r.note.strip() or "توسط ادمین وب‌پنل")
    try:
        w = await asyncio.to_thread(shop.wallet_apply, int(uid), r.sign * r.amount, "charge" if r.sign > 0 else "deduct", note, txid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if w.get("dup"):
        return {"ok": True, "balance": w["balance"], "dup": True}
    if r.sign > 0:
        tok = C.load_config_ro()["shop"]["token"]
        if tok:
            try:
                async with Bot(tok) as b:
                    await shop._tell(b, int(uid), f"✅ کیف پول شما <b>{shop.money(r.amount)}</b> تومان شارژ شد.\nموجودی: <b>{shop.money(w['balance'])}</b> تومان")
            except Exception:  # noqa: BLE001
                log.exception("notify customer wallet")
    return {"ok": True, "balance": w["balance"]}


@app.post("/api/shop/customers/{uid}/test-reset", dependencies=[Depends(guard)])
def shop_customer_test_reset(uid: str):
    """به مشتری اجازه‌ی یک تست رایگان دیگر می‌دهد (مثلاً وقتی ساخت تستش مبهم ماند)."""
    def mut(c):
        cu = c["shop"]["customers"].get(uid)
        if cu is None:
            raise ValueError("مشتری پیدا نشد.")
        cu["test"], cu["test_user"] = 0, ""
    try:
        update_config(mut)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _aud("بازنشانی تست رایگان مشتری", uid, "", "sys")
    return {"ok": True}


@app.post("/api/shop/force/check", dependencies=[Depends(guard)])
async def shop_force_check():
    """آیا ربات فروش می‌تواند عضویت را در کانال‌های تنظیم‌شده بررسی کند؟ (ادمین کانال بودن لازم است)"""
    tok = _shop_token()
    chans = C.load_config_ro()["shop"]["force"]["channels"]
    if not chans:
        raise HTTPException(400, "کانالی تنظیم نشده است.")
    out = []
    async with Bot(tok) as b:
        me = await asyncio.wait_for(b.get_me(), 15)
        for ch in chans:
            chat = int(ch["chat"]) if ch["chat"].lstrip("-").isdigit() else ch["chat"]
            try:
                m = await asyncio.wait_for(b.get_chat_member(chat, me.id), 15)
                ok = m.status in ("administrator", "creator")
                out.append({"chat": ch["chat"], "ok": ok, "msg": "✅ ربات ادمین کانال است" if ok else f"⚠️ ربات ادمین نیست (وضعیت: {m.status})"})
            except Exception as e:  # noqa: BLE001
                out.append({"chat": ch["chat"], "ok": False, "msg": f"❌ {str(e)[:120]}"})
    return {"bot": me.username, "items": out}


# ── کد تخفیف ──
class ShopCodeReq(BaseModel):
    code: str = Field(min_length=3, max_length=20)
    kind: str = Field(pattern="^(percent|amount)$")
    value: int = Field(ge=1, le=10 ** 9)
    max: int = Field(default=0, ge=0, le=10 ** 6)             # کل دفعات استفاده (۰ = نامحدود)
    per_user: int = Field(default=1, ge=1, le=100)
    days: int = Field(default=0, ge=0, le=3650)               # اعتبار از همین حالا، به روز (۰ = بدون انقضا)
    plans: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=80)


@app.get("/api/shop/codes", dependencies=[Depends(guard)])
def shop_codes():
    s = C.load_config_ro()["shop"]
    uses: dict[str, int] = {}
    for o in s["orders"]:
        if o.get("code") and o["status"] not in shop._CODE_DEAD:
            uses[o["code"]] = uses.get(o["code"], 0) + 1
    return {"items": [{"code": k, **v, "used": uses.get(k, 0)} for k, v in sorted(s["codes"].items(), key=lambda kv: -kv[1]["created"])],
            "plans": [{"id": p["id"], "name": p["name"]} for p in s["plans"]]}


@app.post("/api/shop/codes", dependencies=[Depends(guard)])
def shop_code_save(r: ShopCodeReq):
    code = re.sub(r"\s", "", r.code).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,20}", code):
        raise HTTPException(400, "کد فقط حروف انگلیسی، عدد، - و _ (۳ تا ۲۰ کاراکتر) باشد.")
    if r.kind == "percent" and not 1 <= r.value <= 100:
        raise HTTPException(400, "درصد تخفیف بین ۱ تا ۱۰۰ باشد.")

    def mut(c):
        sh = c["shop"]
        old = sh["codes"].get(code)
        ids = {p["id"] for p in sh["plans"]}
        sh["codes"][code] = {"kind": r.kind, "value": r.value, "max": r.max, "per_user": r.per_user,
                             "exp": int(time.time()) + r.days * 86400 if r.days else 0, "active": True,
                             "plans": [x for x in r.plans if x in ids], "note": r.note.strip(), "created": (old or {}).get("created") or int(time.time())}
    update_config(mut)
    _aud("کد تخفیف فروشگاه", code, f"{r.value}{'٪' if r.kind == 'percent' else ' تومان'}", "sys")
    return {"ok": True}


class ShopCodeToggleReq(BaseModel):
    active: bool


@app.post("/api/shop/codes/{code}/active", dependencies=[Depends(guard)])
def shop_code_active(code: str, r: ShopCodeToggleReq):
    def mut(c):
        if code not in c["shop"]["codes"]:
            raise ValueError("کد پیدا نشد.")
        c["shop"]["codes"][code]["active"] = r.active
    try:
        update_config(mut)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/shop/codes/{code}/delete", dependencies=[Depends(guard)])
def shop_code_delete(code: str):
    update_config(lambda c: c["shop"]["codes"].pop(code, None))
    _aud("حذف کد تخفیف فروشگاه", code, "", "sys")
    return {"ok": True}


# ── یادآوری انقضا و حجم ──
class ShopRemindReq(BaseModel):
    enabled: bool
    days: list[int] = Field(max_length=4)
    usage: int = Field(ge=50, le=99)


@app.post("/api/shop/remind", dependencies=[Depends(guard)])
def shop_remind_save(r: ShopRemindReq):
    days = sorted({d for d in r.days if 1 <= d <= 30}, reverse=True)
    if not days:
        raise HTTPException(400, "حداقل یک روز (۱ تا ۳۰) وارد کنید.")

    def mut(c):
        c["shop"]["remind"] = {"enabled": r.enabled, "days": days, "usage": r.usage}
    update_config(mut)
    _aud("یادآوری انقضای فروشگاه", "", f"{'روشن' if r.enabled else 'خاموش'}؛ روزها {days}؛ حجم {r.usage}٪", "sys")
    return {"ok": True}


# ── پیام همگانی ──
_BC_AUD = {"all": "همه‌ی مشتری‌ها", "buyers": "مشتری‌هایی که سرویس دارند", "nobuyers": "مشتری‌هایی که هنوز سرویس ندارند", "wallet": "مشتری‌هایی که موجودی کیف پول دارند"}


def _bc_targets(s: dict, aud: str) -> list[int]:
    out = []
    for uid, c in s["customers"].items():
        if c["banned"]:
            continue
        if aud == "buyers" and not c["services"]:
            continue
        if aud == "nobuyers" and c["services"]:
            continue
        if aud == "wallet" and c["balance"] <= 0:
            continue
        out.append(int(uid))
    return out


@app.get("/api/shop/broadcast", dependencies=[Depends(guard)])
def shop_broadcast_info():
    s = C.load_config_ro()["shop"]
    return {"audiences": [{"key": k, "name": v, "count": len(_bc_targets(s, k))} for k, v in _BC_AUD.items()], "last": s["bcast"], "has_token": bool(s["token"])}


class ShopBroadcastReq(BaseModel):
    text: str = Field(min_length=1, max_length=3500)
    audience: str = Field(pattern="^(all|buyers|nobuyers|wallet)$")


async def _broadcast_run(token: str, bid: str, text: str, targets: list[int]) -> None:
    sent = failed = 0

    def prog(done: bool):
        def mut(c):
            b = c["shop"]["bcast"]
            if b.get("id") == bid:
                b.update(sent=sent, failed=failed, running=not done)
                if done:
                    b["done_ts"] = int(time.time())
        update_config(mut)
    try:
        async with Bot(token) as b:
            for n, uid in enumerate(targets, 1):
                for attempt in range(3):
                    try:
                        await b.send_message(uid, text, disable_web_page_preview=True)
                        sent += 1
                        break
                    except RetryAfter as e:
                        await asyncio.sleep(min(60, float(e.retry_after) + 1))
                    except (Forbidden, TelegramError):
                        failed += 1                        # ربات را بلاک کرده یا چت پیدا نشد
                        break
                else:
                    failed += 1
                await asyncio.sleep(0.05)                  # ~۲۰ پیام در ثانیه (زیر سقف تلگرام)
                if n % 25 == 0:
                    await asyncio.to_thread(prog, False)
    except Exception:  # noqa: BLE001
        log.exception("broadcast")
    finally:
        await asyncio.to_thread(prog, True)


@app.post("/api/shop/broadcast", dependencies=[Depends(guard)])
async def shop_broadcast(r: ShopBroadcastReq):
    tok = _shop_token()
    s = C.load_config_ro()["shop"]
    last = s["bcast"]
    if last.get("running") and time.time() - int(last.get("ts") or 0) < 7200:
        raise HTTPException(409, "یک ارسال همگانی هنوز در حال انجام است؛ صبر کنید تا تمام شود.")
    targets = _bc_targets(s, r.audience)
    if not targets:
        raise HTTPException(400, "مخاطبی برای این گروه پیدا نشد.")
    bid = secrets.token_hex(4)
    text = r.text.strip()

    def mut(c):
        c["shop"]["bcast"] = {"id": bid, "ts": int(time.time()), "audience": r.audience, "text": text[:120], "total": len(targets), "sent": 0, "failed": 0, "running": True}
    await asyncio.to_thread(update_config, mut)
    _aud("پیام همگانی فروشگاه", bid, f"{_BC_AUD[r.audience]} — {len(targets)} نفر", "sys")
    t = asyncio.create_task(_broadcast_run(tok, bid, text, targets))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return {"ok": True, "total": len(targets)}


class ShopBanReq(BaseModel):
    banned: bool


@app.post("/api/shop/customers/{uid}/ban", dependencies=[Depends(guard)])
def shop_customer_ban(uid: str, r: ShopBanReq):
    def mut(c):
        cu = c["shop"]["customers"].get(uid)
        if cu is None:
            raise ValueError("مشتری پیدا نشد.")
        cu["banned"] = r.banned
    update_config(mut)
    _aud("مسدود/آزاد کردن مشتری فروشگاه", uid, "مسدود" if r.banned else "آزاد", "sys")
    return {"ok": True}


# ───────────────────────────── مینی‌اپ تلگرام (مشتری‌های فروشگاه) ─────────────────────────────
# ورود فقط با امضای initData تلگرام (HMAC با توکن ربات فروش) است؛ بدون کوکی/رمز. این بخش فقط‌خواندنی است و پول جابه‌جا نمی‌کند.
MINI = os.path.join(C.BASE_DIR, "miniapp.html")
_mini_cache = {"mtime": 0.0, "data": ""}
_mini_hits: dict[int, float] = {}


@app.get("/app", response_class=HTMLResponse)
async def mini_page():
    try:
        page = _read_cached(MINI, _mini_cache)
    except OSError:
        log.error("miniapp.html کنار webapp.py پیدا نشد: %s", MINI)
        return HTMLResponse(f"miniapp.html در مسیر {MINI} پیدا نشد؛ فایل را کنار webapp.py بگذارید.", status_code=404)
    return HTMLResponse(page, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "frame-ancestors https://web.telegram.org https://*.telegram.org"})


def _mini_user(init: str, token: str) -> dict:
    import hashlib
    import hmac
    from urllib.parse import parse_qsl
    bad = HTTPException(401, "نشست نامعتبر است؛ مینی‌اپ را از داخل ربات باز کنید.")
    if not token or not init:
        raise bad
    pairs = dict(parse_qsl(init, keep_blank_values=True))
    got = pairs.pop("hash", "")
    chk = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not got or not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), got):
        raise bad
    try:
        if time.time() - int(pairs.get("auth_date") or 0) > 86400:
            raise bad
        u = json.loads(pairs.get("user") or "{}")
        return {"id": int(u["id"])}
    except (ValueError, KeyError, TypeError):
        raise bad


_mini_bot: dict[str, str] = {}


async def _mini_bot_name(token: str) -> str:
    """یوزرنیم ربات فروش (برای لینک‌های خرید/تمدید مینی‌اپ)؛ یک‌بار از تلگرام گرفته و نگه داشته می‌شود."""
    if token not in _mini_bot:
        try:
            _mini_bot[token] = (await asyncio.wait_for(Bot(token).get_me(), 8)).username or ""
        except Exception:  # noqa: BLE001
            return ""
    return _mini_bot[token]


class MiniReq(BaseModel):
    init: str = Field(default="", max_length=4096)


@app.post("/api/mini/me")
async def mini_me(r: MiniReq):
    s = C.load_config_ro()["shop"]
    uid = _mini_user(r.init, s["token"])["id"]
    now = time.time()
    if now - _mini_hits.get(uid, 0) < 1:
        raise HTTPException(429, "کمی صبر کنید.")
    if len(_mini_hits) > 5000:
        _mini_hits.clear()
    _mini_hits[uid] = now
    cu = s["customers"].get(str(uid))
    if cu and cu["banned"]:
        raise HTTPException(403, "دسترسی شما به فروشگاه محدود شده است.")
    plans = [{"id": p["id"], "name": p["name"], "price": p["price"], "info": shop._plan_line(t)} for p, t in shop._active_plans()]
    svcs = (cu or {}).get("services", [])[:10]

    async def one(sv):
        try:
            u, api = await asyncio.wait_for(shop._service_info(sv), 12)
        except Exception:  # noqa: BLE001
            u, api = None, None
        if not u:
            return {"name": sv["u"], "ok": False}
        st = u.get("status", "unknown")
        return {"name": sv["u"], "ok": True, "status": st, "used": C.fmt_gb(u.get("used_traffic", 0) or 0), "limit": C.fmt_gb(u.get("data_limit") or 0),
                "pct": round((u.get("used_traffic", 0) or 0) * 100 / u["data_limit"]) if u.get("data_limit") else 0,
                "expire": C.fmt_expire(u.get("expire"), st, u.get("on_hold_expire_duration")), "sub": api.full_sub(u.get("subscription_url", "")) or ""}
    services = await asyncio.gather(*[one(sv) for sv in svcs])
    orders = [{"id": o["id"], "kind": shop.KIND_FA.get(o["kind"], o["kind"]), "name": o.get("pname", ""), "amount": o["amount"],
               "status": shop.STATUS_FA.get(o["status"], o["status"]), "ts": o["ts"]} for o in reversed(s["orders"]) if o["uid"] == uid][:10]
    return {"enabled": s["enabled"], "balance": (cu or {}).get("balance", 0), "known": bool(cu), "welcome": s["welcome"], "support": s["support"],
            "plans": plans, "services": services, "orders": orders,
            "bot": await _mini_bot_name(s["token"]), "test": bool(cu) and shop.test_available() and not cu.get("test")}
