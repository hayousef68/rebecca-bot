#!/usr/bin/env python3
"""وب‌پنل مدیریت ربکا (FastAPI). رمز از config.json (هش‌شده) خوانده می‌شود، نه از داخل کد."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

import common as C
from common import api, load_config, update_config

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("rebecca.web")

INDEX = os.path.join(C.BASE_DIR, "index.html")
_html_cache = {"mtime": 0.0, "data": ""}
_bg: set = set()
_bot = {"token": None, "obj": None}


@asynccontextmanager
async def lifespan(_app):
    yield
    await api.aclose()


app = FastAPI(title="Rebecca Panel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()
_fails: dict[str, list[float]] = {}


def guard(request: Request, cred: HTTPBasicCredentials = Depends(security)):
    if not C.web_configured():
        raise HTTPException(503, "رمز وب‌پنل تنظیم نشده است. روی سرور اجرا کنید: rebecca-bot passwd")
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 10:
        raise HTTPException(429, "تلاش ناموفق زیاد؛ چند دقیقه بعد دوباره امتحان کنید.")
    if not C.check_web_login(cred.username, cred.password):
        recent.append(now)
        _fails[ip] = recent
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است.", headers={"WWW-Authenticate": 'Basic realm="Rebecca"'})
    _fails.pop(ip, None)
    # ضد CSRF: مرورگر بدون preflight نمی‌تواند هدر سفارشی بفرستد
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "rebecca":
        raise HTTPException(403, "درخواست نامعتبر.")


@app.exception_handler(C.RebeccaError)
async def rebecca_error(_req, exc: C.RebeccaError):
    code = exc.status if exc.status in (400, 404, 409, 422) else 502
    return JSONResponse({"detail": str(exc)}, status_code=code)


@app.exception_handler(ValueError)
async def value_error(_req, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def need_panel():
    if not api.is_configured():
        raise HTTPException(409, "ابتدا از تب تنظیمات، اتصال پنل را کامل کنید.")


# ───────────────────────────── صفحه ─────────────────────────────
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(guard)])
async def index():
    m = os.path.getmtime(INDEX)
    if m != _html_cache["mtime"]:
        with open(INDEX, encoding="utf-8") as f:
            _html_cache.update(mtime=m, data=f.read())
    return HTMLResponse(_html_cache["data"], headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


@app.get("/api/state", dependencies=[Depends(guard)])
async def state():
    cfg = load_config()
    try:
        services = await api.list_services() if api.is_configured() else cfg["services"]
    except Exception:  # noqa: BLE001
        services = cfg["services"]
    return {
        "configured": api.is_configured(),
        "bot": bool(cfg["bot_token"] and cfg["admin_ids"]),
        "templates": cfg["templates"],
        "active": cfg["active_template"],
        "services": services,
        "max_batch": cfg["max_batch"],
    }


@app.get("/api/stats", dependencies=[Depends(guard)])
async def stats():
    need_panel()
    return await api.request("GET", "/api/system")


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


def _tg_bot(token: str):
    from telegram import Bot
    if _bot["token"] != token:
        _bot.update(token=token, obj=Bot(token))
    return _bot["obj"]


@app.post("/api/create", dependencies=[Depends(guard)])
async def create(req: CreateReq):
    need_panel()
    cfg = load_config()
    name = req.template or cfg["active_template"]
    tpl = cfg["templates"].get(name)
    if not tpl:
        raise HTTPException(404, "تمپلت پیدا نشد.")
    names = C.make_names(req.base, req.start, req.count, cfg["max_batch"])
    results, failed = await C.create_users(names, tpl, req.service_id, concurrency=6)
    tg = "off"
    if req.telegram and results:
        if cfg["bot_token"] and cfg["admin_ids"]:
            svc = next((s["name"] for s in await api.list_services() if s["id"] == req.service_id), req.service_id)
            header = f"💾 {tpl['gb'] or 'نامحدود'} GB | 📅 {tpl['days'] or 'نامحدود'} روز ({tpl['mode']}) | 📂 {svc}"
            t = asyncio.create_task(_safe_send(cfg, results, header))
            _bg.add(t)
            t.add_done_callback(_bg.discard)
            tg = "queued"
        else:
            tg = "no_bot"
    return {"ok": results, "failed": [{"username": n, "reason": r} for n, r in failed], "telegram": tg}


async def _safe_send(cfg, results, header):
    try:
        await C.send_results(_tg_bot(cfg["bot_token"]), cfg["admin_ids"][0], results, header, cfg["max_qr"])
    except Exception:  # noqa: BLE001
        log.exception("ارسال به تلگرام ناموفق بود")


# ───────────────────────────── کاربران ─────────────────────────────
def _slim(u: dict) -> dict:
    st = u.get("status")
    return {
        "username": u.get("username"), "status": st,
        "used": u.get("used_traffic") or 0, "limit": u.get("data_limit") or 0,
        "expire_text": C.fmt_expire(u.get("expire"), st, u.get("on_hold_expire_duration")),
        "sub": api.full_sub(u.get("subscription_url", "")),
    }


@app.get("/api/users", dependencies=[Depends(guard)])
async def users(search: str = "", status: str = "", offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    need_panel()
    params = {"offset": offset, "limit": limit}
    if search.strip():
        params["search"] = search.strip()
    if status:
        params["status"] = status
    res = await api.request("GET", "/api/users", params=params)
    return {"users": [_slim(u) for u in res.get("users", [])], "total": res.get("total", 0)}


class EditReq(BaseModel):
    gb: float | None = Field(default=None, ge=0)
    days: int | None = Field(default=None, ge=0)


@app.post("/api/user/{uname}/{action}", dependencies=[Depends(guard)])
async def user_action(uname: str, action: str, body: EditReq | None = None):
    need_panel()
    p = api.upath(uname)
    if action == "reset":
        await api.request("POST", p + "/reset")
        return {"ok": True}
    if action == "revoke":
        return _slim(await api.request("POST", p + "/revoke_sub"))
    if action == "toggle":
        cur = await api.request("GET", p)
        new = "active" if cur.get("status") == "disabled" else "disabled"
        return _slim(await api.request("PUT", p, json={"status": new}))
    if action == "edit":
        b = body or EditReq()
        return _slim(await C.apply_user_edit(uname, b.gb, b.days))
    if action == "delete":
        await api.request("DELETE", p)
        return {"ok": True}
    raise HTTPException(404, "عملیات ناشناخته")


@app.post("/api/cleanup", dependencies=[Depends(guard)])
async def cleanup(dry: bool = True):
    need_panel()
    targets = [u["username"] for u in await api.iter_users("expired") + await api.iter_users("limited")]
    if dry:
        return {"count": len(targets)}
    return {"count": len(targets), "deleted": await C.delete_users(targets)}


@app.post("/api/core/restart", dependencies=[Depends(guard)])
async def core_restart():
    need_panel()
    await api.request("POST", "/api/core/restart")
    return {"ok": True}


# ───────────────────────────── تمپلت‌ها ─────────────────────────────
class TplReq(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    old_name: str | None = None
    gb: float = Field(ge=0, le=100000)
    days: int = Field(ge=0, le=36500)
    mode: str = Field(pattern="^(on_hold|active)$")


@app.post("/api/templates", dependencies=[Depends(guard)])
async def tpl_save(r: TplReq):
    gb = int(r.gb) if float(r.gb).is_integer() else r.gb

    def mut(c):
        old = r.old_name or r.name
        if r.name != old and r.name in c["templates"]:
            raise ValueError("نام تمپلت تکراری است.")
        items = [(r.name if k == old else k, v) for k, v in c["templates"].items()]
        d = dict(items)
        d[r.name] = {"gb": gb, "days": r.days, "mode": r.mode}
        c["templates"] = d
        if c["active_template"] == old or c["active_template"] not in d:
            c["active_template"] = r.name
    update_config(mut)
    return {"ok": True}


class NameReq(BaseModel):
    name: str


@app.post("/api/templates/active", dependencies=[Depends(guard)])
async def tpl_active(r: NameReq):
    def mut(c):
        if r.name not in c["templates"]:
            raise ValueError("تمپلت پیدا نشد.")
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
    return {"panel_url": c["panel_url"], "sub_url": c["sub_url"], "username": c["username"], "has_password": bool(c["password"]),
            "verify_ssl": c["verify_ssl"], "bot_token": _mask(c["bot_token"]), "admin_ids": c["admin_ids"], "web_user": c["web"].get("user", "admin")}


class SettingsReq(BaseModel):
    panel_url: str | None = None
    sub_url: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    bot_token: str | None = None
    admin_ids: str | None = None
    web_password: str | None = Field(default=None, min_length=8, max_length=128)


@app.post("/api/settings", dependencies=[Depends(guard)])
async def settings_save(r: SettingsReq):
    restart_bot = False

    def mut(c):
        nonlocal restart_bot
        if r.panel_url is not None:
            u = r.panel_url.strip().rstrip("/")
            if u and not u.startswith(("http://", "https://")):
                raise ValueError("آدرس پنل باید با http:// یا https:// شروع شود.")
            c["panel_url"] = u
        if r.sub_url is not None:
            c["sub_url"] = r.sub_url.strip().rstrip("/")
        if r.username is not None:
            c["username"] = r.username.strip()
        if r.password:
            c["password"] = r.password
        if r.verify_ssl is not None:
            c["verify_ssl"] = r.verify_ssl
        if r.bot_token and "…" not in r.bot_token and r.bot_token.strip() != c["bot_token"]:
            if ":" not in r.bot_token:
                raise ValueError("فرمت توکن ربات صحیح نیست.")
            c["bot_token"] = r.bot_token.strip()
            restart_bot = True
        if r.admin_ids is not None:
            ids = [int(x) for x in r.admin_ids.replace("،", ",").replace(" ", ",").split(",") if x.strip().lstrip("-").isdigit()]
            c["admin_ids"] = ids
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


@app.post("/api/settings/test", dependencies=[Depends(guard)])
async def settings_test():
    need_panel()
    await api.login()
    s = await api.request("GET", "/api/system")
    svc = await api.list_services(force=True)
    return {"ok": True, "total_user": s.get("total_user", 0), "services": len(svc)}


@app.post("/api/services/refresh", dependencies=[Depends(guard)])
async def services_refresh():
    need_panel()
    return {"services": await api.list_services(force=True)}
