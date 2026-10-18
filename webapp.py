#!/usr/bin/env python3
"""وب‌پنل مدیریت ربکا (FastAPI). رمز از config.json (هش‌شده) خوانده می‌شود، نه از داخل کد."""
from __future__ import annotations

import asyncio
import re
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

import common as C
import ops
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
    tok = request.cookies.get(COOKIE, "")
    if C.web_configured() and tok and C.check_session(tok):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_read_cached(LOGIN, _login_cache), headers=_PAGE_HEADERS)


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
    if len(recent) >= 8:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    if not C.check_web_login(r.user.strip(), r.password):
        recent.append(now)
        _fails[ip] = recent
        await asyncio.sleep(0.6)   # کند کردن حدس رمز
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است.")
    _fails.pop(ip, None)
    tok, ttl = C.make_session(r.user.strip(), r.remember)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, tok, max_age=ttl, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", path="/")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


def _panels_view(cfg):
    return [{"id": p["id"], "name": p["name"], "ok": bool(p["panel_url"] and p["username"] and p["password"])} for p in cfg["panels"]]


@app.get("/api/state", dependencies=[Depends(guard)])
async def state():
    cfg = load_config()
    return {
        "panels": _panels_view(cfg), "active_panel": cfg["active_panel"],
        "bot": bool(cfg["bot_token"] and cfg["admin_ids"]),
        "templates": cfg["templates"], "active": cfg["active_template"], "active_group": cfg.get("active_group_template", ""), "max_batch": cfg["max_batch"],
        "default_prefix": cfg["default_prefix"], "multi_panel": len(cfg["panels"]) > 1,
        "ham_names": cfg.get("sales", {}).get("hamkari_service_names", ["hamkari"]),
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
    telegram: bool = False
    gb: float | None = Field(default=None, ge=0, le=100000)      # جایگزین حجم تمپلت (تکی)
    days: int | None = Field(default=None, ge=0, le=36500)       # جایگزین مدت تمپلت
    mode: str | None = Field(default=None, pattern="^(on_hold|active)$")


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
    tg = "off"
    if req.telegram and results:
        if cfg["bot_token"] and cfg["admin_ids"]:
            svc = next((s["name"] for s in await api.list_services() if s["id"] == req.service_id), req.service_id)
            header = C.build_info(api.name, tpl, svc)
            t = asyncio.create_task(_safe_send(cfg, results, header))
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


async def _safe_send(cfg, results, header):
    try:
        await C.send_results(_tg_bot(cfg["bot_token"]), cfg["admin_ids"][0], results, header, cfg["max_qr"])
    except Exception:  # noqa: BLE001
        log.exception("ارسال به تلگرام ناموفق بود")


# ───────────────────────────── کاربران ─────────────────────────────
def _slim(u: dict, api) -> dict:
    st = u.get("status")
    return {
        "username": u.get("username"), "status": st,
        "used": u.get("used_traffic") or 0, "limit": u.get("data_limit") or 0,
        "expire_text": C.fmt_expire(u.get("expire"), st, u.get("on_hold_expire_duration")),
        "sub": api.full_sub(u.get("subscription_url", "")),
        "service_id": u.get("service_id"), "note": u.get("note") or "",
    }


@app.get("/api/users", dependencies=[Depends(guard)])
async def users(search: str = "", status: str = "", offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                api: RebeccaClient = Depends(P)):
    params = {"offset": offset, "limit": limit}
    if search.strip():
        params["search"] = search.strip()
    if status:
        params["status"] = status
    res = await api.request("GET", "/api/users", params=params)
    return {"users": [_slim(u, api) for u in res.get("users", [])], "total": res.get("total", 0)}


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
    return _slim(await api.request("GET", api.upath(uname)), api)


@app.get("/api/soon", dependencies=[Depends(guard)])
async def soon(api: RebeccaClient = Depends(P)):
    rc = load_config()["reminders"]
    items = await ops.find_soon(api, rc["days"], rc["low_pct"])
    return {"days": rc["days"], "low_pct": rc["low_pct"], "total": len(items),
            "items": [{"username": i["username"], "reason": i["reason"], "user": _slim(i["user"], api)} for i in items[:100]]}


@app.post("/api/user/{uname}/{action}", dependencies=[Depends(guard)])
async def user_action(uname: str, action: str, body: EditReq | None = None, api: RebeccaClient = Depends(P)):
    p = api.upath(uname)
    if action == "reset":
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
        return _slim(await C.apply_user_edit(api, uname, b.gb, b.days, b.add_gb, b.add_days), api)
    if action == "delete":
        await api.request("DELETE", p)
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
        d[r.name] = {"gb": gb, "days": r.days, "mode": r.mode, "kind": r.kind}
        c["templates"] = d
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
            "backup_enabled": c["backup"]["enabled"]}


class SettingsReq(BaseModel):
    bot_token: str | None = None
    admin_ids: str | None = None
    web_password: str | None = Field(default=None, min_length=8, max_length=128)


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
    update_config(lambda c: c["backup"].__setitem__("enabled", r.enabled))
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
    return {"ok": True, "panels": len(new["panels"]), "web_changed": web_changed, "safety": os.path.basename(safety) if safety else None}


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
