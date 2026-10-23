#!/usr/bin/env python3
"""وب‌پنل مدیریت ربکا (FastAPI). رمز از config.json (هش‌شده) خوانده می‌شود، نه از داخل کد."""
from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

import common as C
import sales
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


@asynccontextmanager
async def lifespan(_app):
    watcher = asyncio.create_task(_cert_watch()) if os.environ.get("REBECCA_SSL_ACTIVE") == "1" else None
    yield
    if watcher:
        watcher.cancel()
    for c in list(C._clients.values()):
        await c.aclose()


app = FastAPI(title="Rebecca Panel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _basic_ok(request: Request) -> bool:
    """سازگاری با اسکریپت‌های قدیمی (curl -u)؛ مرورگر دیگر از Basic استفاده نمی‌کند."""
    import base64
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
    cfg = load_config()
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
            recent.append(now)
            _fails[ip] = recent
            raise HTTPException(423, "این حساب به‌خاطر چند بار رمز اشتباه موقتاً غیرفعال شده است؛ با مدیر تماس بگیرید.")
        if st in ("ok", "blocked"):
            if st == "blocked":
                raise HTTPException(403, "حساب شما مسدود شده است؛ با مدیر تماس بگیرید.")
            _fails.pop(ip, None)
            tok, ttl = C.make_rep_session(rid)
            resp = JSONResponse({"ok": True, "role": "rep"})
            resp.set_cookie(COOKIE_REP, tok, max_age=ttl, httponly=True, samesite="strict", secure=secure, path="/")
            resp.delete_cookie(COOKIE, path="/")
            return resp
        recent.append(now)
        _fails[ip] = recent
        await asyncio.sleep(0.6)   # کند کردن حدس رمز
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است.")
    _fails.pop(ip, None)
    tok, ttl = C.make_session(user, r.remember)
    resp = JSONResponse({"ok": True, "role": "admin"})
    resp.set_cookie(COOKIE, tok, max_age=ttl, httponly=True, samesite="strict", secure=secure, path="/")
    resp.delete_cookie(COOKIE_REP, path="/")
    return resp


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
    cfg = load_config()
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


@app.get("/api/sales/settings", dependencies=[Depends(guard)])
async def sales_settings_get():
    sc = load_config()["sales"]
    return {"hamkari_per_gb": sc["hamkari_per_gb"], "buy_per_tb": sc.get("buy_per_tb", 750000),
            "hamkari_usage_multiplier": sc.get("hamkari_usage_multiplier", 1.5),
            "hamkari_multiplier_enabled": sc.get("hamkari_multiplier_enabled", True),
            "hamkari_service_names": ", ".join(sc.get("hamkari_service_names", [])),
            "ignore_service_names": ", ".join(sc.get("ignore_service_names", [])),
            "fallback_rate": round(sales.fallback_rate(sc)),
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
                          single_prices=plans)
    update_config(mut)
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
    results, failed = await C.create_users(api, names, tpl, req.service_id, concurrency=6)
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
                d = _slim_rep(await api.request("GET", api.upath(u)), api)
                d["pid"], d["note"], d["paid"] = pid, note_of(pid, u), bool(cfg["rep_paid"].get(pid, {}).get(u))
                return d
            except C.RebeccaError:        # حذف‌شده از پنل یا خطای موقت
                return None
    res = [x for x in await asyncio.gather(*(one(p, u) for p, u in page)) if x]
    return {"users": res, "total": len(items), "missing": len(page) - len(res)}


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
    return {"balance": rp["balance"],
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


@app.post("/api/rep/buy")
async def rep_buy(r: RepBuyReq, rep=Depends(rep_guard)):
    """ساخت اکانت با تمپلت مجاز نماینده؛ قیمت از کیف پول کم می‌شود و اگر ساخت ناموفق بود برمی‌گردد."""
    rid = rep[0]
    async with _money_guard(rid):
        return await _rep_buy(r, rid)


async def _rep_buy(r: RepBuyReq, rid: str):
    cfg = load_config()
    rp = cfg["reps"][rid]
    tpl = _rep_templates(cfg, rp).get(r.template)
    if not tpl:
        raise HTTPException(403, "این تمپلت برای شما فعال نیست.")
    pid = rp["panel"] if rp["panel"] and get_panel(cfg, rp["panel"]) else cfg["active_panel"]
    api = get_api(pid)
    if not api.is_configured():
        raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
    price = int(tpl["price"])
    if rp["balance"] < price:
        raise HTTPException(402, f"موجودی کافی نیست. قیمت: {price:,} تومان — موجودی شما: {rp['balance']:,} تومان")
    svc = C.pick_default_service(await api.list_services(), tpl.get("kind") == "group")
    if svc is None:
        raise HTTPException(502, "سرویس مناسب در پنل پیدا نشد؛ به ادمین اطلاع دهید.")
    name = ""                                                          # نام کاربری را نماینده تعیین نمی‌کند؛ همیشه شماره‌ی بعدیِ پنل
    prefix = cfg["default_prefix"]
    nx = await sales.next_number(api, prefix, len(cfg["panels"]) <= 1)
    names = C.make_names(prefix, int(nx.get("next") or 1), 1, 1)
    t_start = time.time()
    try:
        await asyncio.to_thread(C.wallet_adjust, rid, -price, "purchase", f"ساخت اکانت {names[0]} — {r.template}")
    except ValueError:
        raise HTTPException(402, "موجودی کافی نیست.")
    results, failed = [], []
    for attempt in range(4 if not name else 1):                        # نام خودکار: اگر هم‌زمان کسی همان شماره را گرفت، شماره‌ی بعدی
        try:
            results, failed = await C.create_users(api, names, tpl, svc, concurrency=1)
        except Exception:  # noqa: BLE001
            results, failed = [], [(names[0], "خطای پنل")]
        if results or name or not failed or "قبل" not in failed[0][1]:
            break
        m = re.fullmatch(r"(.*?)(\d+)", names[0])
        names = C.make_names(m.group(1), int(m.group(2)) + 1, 1, 1) if m else names
    if not results:
        await asyncio.to_thread(C.wallet_adjust, rid, price, "refund", f"بازگشت وجه — ساخت {names[0]} ناموفق بود")
        raise HTTPException(400, "ساخت اکانت ناموفق بود: " + (failed[0][1] if failed else "نامشخص") + " — مبلغ برگشت داده شد.")
    await _hide_from_sales(results[0]["username"], t_start)
    await asyncio.to_thread(C.assign_users, rid, pid, [results[0]["username"]])
    if r.note.strip():
        try:
            await asyncio.to_thread(C.set_rep_note, rid, pid, results[0]["username"], r.note)
        except ValueError:
            pass
    bal = load_config()["reps"][rid]["balance"]
    await _notify_admins(cfg, f"🛒 نماینده <b>{C.esc(rp['label'] or rp['name'])}</b> اکانت <code>{C.esc(results[0]['username'])}</code> ساخت ({C.esc(r.template)} — {price:,} تومان). موجودی باقی‌مانده: {bal:,}")
    return {"ok": True, "username": results[0]["username"], "sub": results[0]["sub"], "balance": bal, "gb": tpl["gb"], "days": tpl["days"]}


class RepRenewReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pid: str = Field(default="", max_length=20)
    template: str = Field(min_length=1, max_length=40)


@app.post("/api/rep/renew")
async def rep_renew(r: RepRenewReq, rep=Depends(rep_guard)):
    """تمدید کاربرِ خود نماینده با تمپلت مجاز؛ قیمت تمپلت از کیف پول کم می‌شود (ناموفق ← برگشت وجه)."""
    rid = rep[0]
    async with _money_guard(rid):
        cfg = load_config()
        rp = cfg["reps"][rid]
        pids = [p for p, m in cfg["rep_users"].items() if m.get(r.username) == rid and (not r.pid or p == r.pid)]
        if not pids:
            raise HTTPException(404, "این کاربر در لیست شما نیست.")
        pid = pids[0]
        tpl = _rep_templates(cfg, rp).get(r.template)
        if not tpl:
            raise HTTPException(403, "این تمپلت برای شما فعال نیست.")
        api = get_api(pid)
        if not api.is_configured():
            raise HTTPException(502, "پنل در دسترس نیست؛ به ادمین اطلاع دهید.")
        price = int(tpl["price"])
        t_start = time.time()
        try:
            await asyncio.to_thread(C.wallet_adjust, rid, -price, "purchase", f"تمدید {r.username} — {r.template}")
        except ValueError:
            raise HTTPException(402, f"موجودی کافی نیست. قیمت: {price:,} تومان — موجودی شما: {rp['balance']:,} تومان")
        try:
            await C.renew_user(api, r.username, dict(tpl), reset_usage=True)
            res = await api.request("GET", api.upath(r.username))
        except Exception as e:  # noqa: BLE001
            await asyncio.to_thread(C.wallet_adjust, rid, price, "refund", f"بازگشت وجه — تمدید {r.username} ناموفق بود")
            raise HTTPException(400, "تمدید ناموفق بود؛ مبلغ برگشت داده شد." + (f" ({e})" if isinstance(e, C.RebeccaError) else ""))
        await _hide_from_sales(r.username, t_start)
        await asyncio.to_thread(C.unpaid_clear, pid, r.username)
        bal = load_config()["reps"][rid]["balance"]
        await _notify_admins(cfg, f"🔄 نماینده <b>{C.esc(rp['label'] or rp['name'])}</b> کاربر <code>{C.esc(r.username)}</code> را تمدید کرد ({C.esc(r.template)} — {price:,} تومان). موجودی باقی‌مانده: {bal:,}")
        return {"ok": True, "balance": bal, "user": _slim_rep(res, api)}


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
    cfg = load_config()
    cnt: dict[str, int] = {}
    for m in cfg["rep_users"].values():
        for r in m.values():
            cnt[r] = cnt.get(r, 0) + 1
    return {"reps": [{"id": i, "name": r["name"], "label": r["label"], "blocked": r["blocked"], "locked": r["locked"], "fails": r["fails"], "locked_at": r["locked_at"],
                      "count": cnt.get(i, 0), "created": r["created"], "balance": r["balance"], "tpls": r["tpls"], "panel": r["panel"]}
                     for i, r in cfg["reps"].items()],
            "topups": [{**t, "rep": cfg["reps"][t["rid"]]["label"] or cfg["reps"][t["rid"]]["name"]} for t in cfg["topups"] if t["status"] == "pending"]}


@app.post("/api/reps", dependencies=[Depends(guard)])
async def reps_create(r: RepCreateReq):
    return await asyncio.to_thread(C.create_rep, r.name, r.password, r.label)


@app.post("/api/reps/{rid}/password", dependencies=[Depends(guard)])
async def reps_password(rid: str, r: RepPwReq):
    return {"password": await asyncio.to_thread(C.reset_rep_password, rid, r.password)}


@app.post("/api/reps/{rid}/block", dependencies=[Depends(guard)])
async def reps_block(rid: str, r: RepBlockReq):
    await asyncio.to_thread(C.set_rep_blocked, rid, r.blocked)
    return {"ok": True}


@app.post("/api/reps/{rid}/unlock", dependencies=[Depends(guard)])
async def reps_unlock(rid: str):
    await asyncio.to_thread(C.unlock_rep, rid)
    return {"ok": True}


@app.post("/api/reps/{rid}/delete", dependencies=[Depends(guard)])
async def reps_delete(rid: str):
    await asyncio.to_thread(C.delete_rep, rid)
    return {"ok": True}


class WalletReq(BaseModel):
    amount: int = Field(ge=-10 ** 10, le=10 ** 10)
    note: str = Field(default="", max_length=120)


@app.post("/api/reps/{rid}/wallet", dependencies=[Depends(guard)])
async def reps_wallet(rid: str, r: WalletReq):
    """شارژ (مبلغ مثبت) یا کسر (مبلغ منفی) موجودی نماینده توسط ادمین."""
    if r.amount == 0:
        raise HTTPException(400, "مبلغ را وارد کنید.")
    b = await asyncio.to_thread(C.wallet_adjust, rid, r.amount, "charge" if r.amount > 0 else "deduct", r.note or ("شارژ توسط ادمین" if r.amount > 0 else "کسر توسط ادمین"))
    return {"ok": True, "balance": b}


class RepTplsReq(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=200)


@app.post("/api/reps/{rid}/templates", dependencies=[Depends(guard)])
async def reps_templates(rid: str, r: RepTplsReq):
    return {"ok": True, "tpls": await asyncio.to_thread(C.set_rep_templates, rid, r.names)}


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
    return {"ok": True}


class TopupResolveReq(BaseModel):
    approve: bool
    amount: int | None = Field(default=None, ge=1, le=10 ** 10)


@app.post("/api/topups/{tid}", dependencies=[Depends(guard)])
async def topup_resolve(tid: str, r: TopupResolveReq):
    t = await asyncio.to_thread(C.topup_resolve, tid, r.approve, r.amount)
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
    return {"added": n}


@app.post("/api/reps/{rid}/unassign", dependencies=[Depends(guard)])
async def reps_unassign(rid: str, r: RepSpecReq, panel: str | None = Query(None, max_length=20)):
    names = C.parse_user_spec(r.prefix, r.start, r.end, r.names)
    if not names:
        raise HTTPException(400, "نام کاربری یا بازه را وارد کنید.")
    return {"removed": await asyncio.to_thread(C.unassign_users, rid, _rep_pid(panel), names)}


# ───────────────────────────── کاربران ─────────────────────────────
def _slim(u: dict, api, pend: dict | None = None) -> dict:
    st = u.get("status")
    if pend is None:
        pend = C.unpaid_map(getattr(api, "pid", ""))
    rec = pend.get(u.get("username") or "")
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
    return _slim(await C.set_service(api, uname, r.service_id), api)


@app.post("/api/user/{uname}/note", dependencies=[Depends(guard)])
async def user_note(uname: str, r: NoteReq, api: RebeccaClient = Depends(P)):
    await C.set_note(api, uname, r.note.strip())
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
    return _slim(res, api)


@app.post("/api/user/{uname}/paid", dependencies=[Depends(guard)])
async def user_paid(uname: str, api: RebeccaClient = Depends(P)):
    """«واریز شد»: شمارش معکوس غیرفعال‌سازی لغو می‌شود."""
    await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
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
    return {"user": _slim(after, api), "warnings": warns}


@app.post("/api/user/{uname}/{action}", dependencies=[Depends(guard)])
async def user_action(uname: str, action: str, body: EditReq | None = None, api: RebeccaClient = Depends(P)):
    p = api.upath(uname)
    if action == "reset":
        try:                     # یک GET سبک برای نقطه‌ی برگشت؛ اگر ناموفق بود ریست بدون آن انجام می‌شود
            await asyncio.to_thread(C.undo_snapshot, api.pid, uname, await api.request("GET", p), "reset")
        except Exception:  # noqa: BLE001
            log.warning("undo snapshot before reset failed")
        await api.request("POST", p + "/reset")
        return {"ok": True}
    if action == "revoke":
        return _slim(await api.request("POST", p + "/revoke_sub"), api)
    if action == "toggle":
        cur = await api.request("GET", p)
        new = "active" if cur.get("status") == "disabled" else "disabled"
        return _slim(await api.request("PUT", p, json={"status": new}), api)
    if action == "edit":
        b = body or EditReq()
        before = await api.request("GET", p) if b.add_gb else None
        res = await C.apply_user_edit(api, uname, b.gb, b.days, b.add_gb, b.add_days)
        if before is not None and int(res.get("data_limit") or 0) > int(before.get("data_limit") or 0):   # فقط وقتی حجم واقعاً اضافه شد
            try:
                await asyncio.to_thread(sales.record_attach, uname, res.get("service_id"), b.add_gb, b.add_days or 0)
            except Exception:  # noqa: BLE001
                log.exception("record_attach")
        return _slim(res, api)
    if action == "delete":
        await api.request("DELETE", p)
        await asyncio.to_thread(C.unpaid_clear, api.pid, uname)
        return {"ok": True}
    raise HTTPException(404, "عملیات ناشناخته")


@app.post("/api/cleanup", dependencies=[Depends(guard)])
async def cleanup(dry: bool = True, api: RebeccaClient = Depends(P)):
    targets = [u["username"] for u in await api.iter_users("expired") + await api.iter_users("limited")]
    if dry:
        return {"count": len(targets)}
    return {"count": len(targets), "deleted": await C.delete_users(api, targets)}


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
    return {"ok": True}


@app.post("/api/templates/delete", dependencies=[Depends(guard)])
async def tpl_delete(r: NameReq):
    def mut(c):
        if len(c["templates"]) <= 1:
            raise ValueError("حداقل یک تمپلت باید باقی بماند.")
        c["templates"].pop(r.name, None)
    update_config(mut)
    return {"ok": True}


# ───────────────────────────── تنظیمات ─────────────────────────────
def _mask(tok: str) -> str:
    return "" if not tok else tok[:5] + "…" + tok[-4:]


@app.get("/api/settings", dependencies=[Depends(guard)])
async def settings_get():
    c = load_config()
    panels = [{"id": p["id"], "name": p["name"], "panel_url": p["panel_url"], "sub_url": p["sub_url"], "username": p["username"],
               "has_password": bool(p["password"]), "verify_ssl": p["verify_ssl"]} for p in c["panels"]]
    return {"panels": panels, "active_panel": c["active_panel"], "bot_token": _mask(c["bot_token"]),
            "admin_ids": c["admin_ids"], "web_user": c["web"].get("user", "admin"), "web_port": c["web_port"],
            "backup_enabled": c["backup"]["enabled"], "backup_interval": c["backup"]["interval_min"], "default_prefix": c["default_prefix"],
            "unpaid_hours": c["unpaid"]["hours"], "unpaid_count": sum(len(m) for m in c["unpaid"]["pending"].values())}


class SettingsReq(BaseModel):
    bot_token: str | None = None
    admin_ids: str | None = None
    web_password: str | None = Field(default=None, min_length=8, max_length=128)
    default_prefix: str | None = Field(default=None, max_length=28)       # اسم پیش‌فرض کاربر جدید (مثلاً MS_)
    unpaid_hours: int | None = Field(default=None, ge=1, le=24 * 365)     # مهلت غیرفعال‌سازی خودکارِ «واریز نشده» (ساعت)


@app.post("/api/settings", dependencies=[Depends(guard)])
async def settings_save(r: SettingsReq):
    restart_bot = False

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
    update_config(mut)
    if restart_bot:
        try:
            proc = await asyncio.create_subprocess_exec("systemctl", "restart", "rebecca-bot")
            asyncio.create_task(proc.wait())
        except Exception:  # noqa: BLE001
            log.warning("ریستارت خودکار ربات ممکن نشد")
    return {"ok": True, "bot_restarted": restart_bot}


# ───────────────────────────── SSL و پورت وب‌پنل ─────────────────────────────
def _panel_domain(cfg) -> str:
    p = get_panel(cfg, cfg["active_panel"]) or (cfg["panels"][0] if cfg["panels"] else None)
    return tlsutil._domain_of(p["panel_url"]) if p else ""


def _web_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host}" + ("" if (scheme, port) in (("https", 443), ("http", 80)) else f":{port}")


@app.get("/api/ssl", dependencies=[Depends(guard)])
async def ssl_get(request: Request):
    c = load_config()
    ss = c["ssl"]
    return {"enabled": ss["enabled"], "active": os.environ.get("REBECCA_SSL_ACTIVE") == "1", "cert": ss["cert"], "key": ss["key"],
            "domain": ss["domain"] or _panel_domain(c), "error": ss["error"], "port": c["web_port"],
            "info": tlsutil.cert_info(ss["cert"]) if ss["cert"] else {}, "scheme": request.url.scheme}


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
    if changed:
        t = asyncio.create_task(_restart_self())
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    return {"ok": True, "restart": changed, "url": url}


# ───────────────────────────── بکاپ و بازیابی ─────────────────────────────
class BackupSettingsReq(BaseModel):
    enabled: bool
    interval_min: int | None = Field(default=None, ge=5, le=1440)       # ۱۴۴۰ = روزانه؛ کمتر = هر N دقیقه (فقط در صورت تغییر)


class RestoreReq(BaseModel):
    content: str = Field(min_length=2, max_length=2_000_000)


@app.get("/api/backup", dependencies=[Depends(guard)])
async def backup_download():
    try:
        data = await asyncio.to_thread(C.config_backup_bytes)
    except OSError:
        raise HTTPException(404, "فایل تنظیمات پیدا نشد.")
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="rebecca-config-{sales.tehran_day()}.json"'})


@app.post("/api/backup/settings", dependencies=[Depends(guard)])
async def backup_settings(r: BackupSettingsReq):
    def mut(c):
        c["backup"]["enabled"] = r.enabled
        if r.interval_min is not None:
            c["backup"]["interval_min"] = r.interval_min
    update_config(mut)
    return {"ok": True}


@app.post("/api/backup/send", dependencies=[Depends(guard)])
async def backup_send():
    cfg = load_config()
    if not (cfg["bot_token"] and cfg["admin_ids"]):
        raise HTTPException(400, "توکن ربات یا ادمین تنظیم نشده")
    n = await sales.send_backup(_tg_bot(cfg["bot_token"]), cfg["admin_ids"], "💾 بکاپ تنظیمات — جای امن نگه دارید.")
    if not n:
        raise HTTPException(502, "ارسال به تلگرام ناموفق بود (ربات را در تلگرام Start کرده‌اید؟)")
    return {"sent": n}


@app.post("/api/backup/restore", dependencies=[Depends(guard)])
async def backup_restore(r: RestoreReq):
    """فایل بکاپ را اعتبارسنجی، از وضعیت فعلی نسخه‌ی پشتیبان می‌گیرد، اعمال می‌کند و ربات را ریستارت می‌کند."""
    import json
    import shutil
    try:
        data = json.loads(r.content)
    except ValueError:
        raise HTTPException(400, "فایل JSON معتبر نیست.")
    if not isinstance(data, dict) or not (set(data) & {"panels", "panel_url", "bot_token", "sales", "templates"}):
        raise HTTPException(400, "این فایل شبیه بکاپ تنظیمات ربات نیست.")
    new = C._normalize(data)
    cur = load_config()
    if not (new.get("web") or {}).get("hash"):          # بکاپ بدون رمز وب: رمز فعلی حفظ شود
        new["web"] = cur["web"]
    if "reps" not in data:                              # بکاپ قدیمی (قبل از نماینده‌ها): نماینده‌های فعلی پاک نشوند
        new["reps"], new["rep_users"], new["rep_notes"], new["rep_paid"] = cur["reps"], cur["rep_users"], cur["rep_notes"], cur["rep_paid"]
    web_changed = (new["web"].get("hash"), new["web"].get("user")) != (cur["web"].get("hash"), cur["web"].get("user"))
    safety = None
    if os.path.exists(C.CONFIG_FILE):
        safety = f"{C.CONFIG_FILE}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(C.CONFIG_FILE, safety)
        os.chmod(safety, 0o600)
    C.save_config(new)
    for p in {p["id"] for p in cur["panels"]} | {p["id"] for p in new["panels"]}:
        C.drop_api(p)
    _bot.update(token=None, obj=None)
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "restart", "rebecca-bot")
        asyncio.create_task(proc.wait())
    except Exception:  # noqa: BLE001
        log.warning("ریستارت خودکار ربات ممکن نشد")
    return {"ok": True, "panels": len(new["panels"]), "web_changed": web_changed, "safety": os.path.basename(safety) if safety else None,
            "reps": len(new["reps"]), "rep_users": C.backup_summary(new)["rep_users"], "reps_kept": "reps" not in data,
            "balance": sum(x["balance"] for x in new["reps"].values()), "topups": len(new["topups"])}


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
    return {"ok": True, "id": out["id"]}


@app.post("/api/panels/delete", dependencies=[Depends(guard)])
async def panel_delete(r: NameReq):  # name = panel id
    def mut(c):
        c["panels"] = [p for p in c["panels"] if p["id"] != r.name]
    update_config(mut)
    C.drop_api(r.name)
    return {"ok": True}


@app.post("/api/panels/active", dependencies=[Depends(guard)])
async def panel_active(r: NameReq):
    def mut(c):
        if not get_panel(c, r.name):
            raise ValueError("پنل پیدا نشد.")
        c["active_panel"] = r.name
    update_config(mut)
    return {"ok": True}


@app.post("/api/settings/test", dependencies=[Depends(guard)])
async def settings_test(api: RebeccaClient = Depends(P)):
    await api.login()
    s = await api.request("GET", "/api/system")
    svc = await api.list_services(force=True)
    return {"ok": True, "total_user": s.get("total_user", 0), "services": len(svc)}
