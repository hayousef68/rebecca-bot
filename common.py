#!/usr/bin/env python3
"""هسته مشترک ربات تلگرام و وب‌پنل ربکا: کانفیگ، کلاینت API، ساخت گروهی، QR و ارسال به تلگرام."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger("rebecca")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("REBECCA_CONFIG") or os.path.join(BASE_DIR, "config.json")
GB = 1024 ** 3
REP_NOTE_MAX = 100
WALLET_LOG_MAX = 100
TOPUP_MAX = 300
WALLET_INCOME_MAX = 20000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")

DEFAULT_TEMPLATES = {
    "پیش‌فرض (30 گیگ - 30 روز)": {"gb": 30, "days": 30, "mode": "on_hold", "kind": "single"},
    "تست (5 گیگ - 3 روز)": {"gb": 5, "days": 3, "mode": "active", "kind": "single"},
}
DEFAULT_SERVICES = [{"id": 6, "name": "Hamkari"}, {"id": 1, "name": "ms"}, {"id": 5, "name": "Tes"}]

DEFAULT_CFG = {
    "bot_token": "",
    "admin_ids": [],
    "panels": [],
    "active_panel": "",
    "templates": DEFAULT_TEMPLATES,
    "active_template": "",
    "default_prefix": "MS_",
    "max_batch": 200,
    "max_qr": 100,
    "web_port": 8686,
    "sales": {
        "enabled": True,
        "poll_seconds": 120,
        "hamkari_per_gb": 3000,
        "buy_per_tb": 750000,
        "tb_gb": 1000,
        "hamkari_usage_multiplier": 1.5,
        "hamkari_multiplier_enabled": True,
        "attach_extra_mode": "skip",
        "db_tz_offset_minutes": 0,
        "db": {},
        "schema": {},
        "hamkari_service_ids": [],
        "hamkari_service_names": ["hamkari"],
        "ignore_service_names": ["tes", "test"],
        "fallback_plan_gb": 30,
        "single_prices": [
            {"gb": 30, "months": 1, "price": 150000},
            {"gb": 60, "months": 1, "price": 250000},
            {"gb": 90, "months": 1, "price": 350000},
            {"gb": 60, "months": 2, "price": 290000},
            {"gb": 90, "months": 2, "price": 390000},
        ],
    },
    # پلن بعدی: chain (پیش‌فرض) = N روز بعد از پایان اشتراک فعلی (پلن وقتی حجم/زمان تمام شد شروع می‌شود؛ پنل: روز باقی‌مانده + N) | duration = N روز از لحظه‌ی ذخیره
    "next_plan_mode": "chain",
    # برگشت تمدید/ریست اشتباهی: فقط یک فایل محلی کوچک (renew_undo.json)، بدون هیچ درخواست اضافه به پنل؛ ریست دسته‌ای بزرگ‌تر از bulk_max عکس‌برداری نمی‌شود
    "undo": {"enabled": True, "keep": 300, "days": 14, "bulk_max": 30},
    "backup": {"enabled": True, "interval_min": 1440},   # interval_min: ۱۴۴۰ = روزانه ساعت ۱۲:۰۰؛ کمتر از آن = هر N دقیقه (فقط وقتی چیزی عوض شده)
    "monitor": {"enabled": True, "interval": 300},
    "reminders": {"enabled": True, "hour": 10, "days": 3, "low_pct": 10},
    "unpaid": {"hours": 24, "pending": {}},   # تمدیدهای «واریز نشده»: pending = {panel_id: {username: {"due": ts, "set": ts}}}
    "web": {"user": "admin", "salt": "", "hash": "", "secret": ""},
    "reps": {},          # نمایندگان: {id: {name, label, salt, hash, blocked, created}}
    "rep_users": {},     # کاربرهای هر نماینده: {panel_id: {username: rep_id}}
    "rep_paid": {},      # کاربرهایی که نماینده «پرداخت شد» زده: {panel_id: {username: timestamp}}
    "wallet_income": [],  # شارژهای کیف پول نماینده‌ها (تأیید درخواست یا شارژ دستی ادمین) که جزو فروش همان روز حساب می‌شوند: [{ts, rid, amount}]
    "topups": [],        # درخواست‌های افزایش موجودی نماینده‌ها: [{id, rid, amount, note, ts, status: pending|approved|rejected, done}]
    "rep_notes": {},     # اسم/یادداشت نماینده برای هر کاربرش: {panel_id: {username: note}}
    "ssl": {"enabled": False, "cert": "", "key": "", "domain": "", "error": ""},
}


# ───────────────────────────── کانفیگ ─────────────────────────────
_lock = threading.RLock()
_cache = {"sig": None, "data": None}


def _num(x):
    v = float(x)
    return int(v) if v.is_integer() else v


def _normalize_panels(cfg: dict, raw: dict) -> list[dict]:
    panels, seen = [], set()
    src = raw.get("panels") if isinstance(raw.get("panels"), list) else []
    if not src and (raw.get("panel_url") or raw.get("username")):  # مهاجرت از نسخه تک‌پنلی
        src = [{"id": "p1", "name": "پنل ۱", "panel_url": raw.get("panel_url", ""), "sub_url": raw.get("sub_url", ""),
                "username": raw.get("username", ""), "password": raw.get("password", ""),
                "verify_ssl": raw.get("verify_ssl", False), "services": raw.get("services") or DEFAULT_SERVICES}]
    for i, p in enumerate(src):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or f"p{i + 1}")
        if pid in seen:
            continue
        seen.add(pid)
        svc = [s for s in (p.get("services") or []) if isinstance(s, dict) and "id" in s]
        panels.append({
            "id": pid, "name": str(p.get("name") or f"پنل {i + 1}")[:40],
            "panel_url": str(p.get("panel_url") or "").strip(), "sub_url": str(p.get("sub_url") or "").strip(),
            "username": str(p.get("username") or "").strip(), "password": str(p.get("password") or ""),
            "verify_ssl": bool(p.get("verify_ssl", False)), "services": svc,
        })
    return panels


def get_panel(cfg: dict, pid: str | None) -> dict | None:
    return next((p for p in cfg["panels"] if p["id"] == pid), None)


def new_panel_id(cfg: dict) -> str:
    nums = [int(p["id"][1:]) for p in cfg["panels"] if p["id"][1:].isdigit()]
    return f"p{max(nums, default=0) + 1}"


def _normalize(data: dict) -> dict:
    cfg = copy.deepcopy(DEFAULT_CFG)
    if isinstance(data, dict):
        cfg.update(data)
    tpls = {}
    for name, t in (cfg.get("templates") or {}).items():
        try:
            mode = t.get("mode") if t.get("mode") in ("on_hold", "active") else "on_hold"
            kind = t.get("kind")
            if kind not in ("single", "group"):   # مهاجرت: نام‌های همکاری ← گروهی، بقیه ← تکی
                kind = "group" if re.search(r"hamkari|همکار", str(name), re.I) else "single"
            tpls[str(name)] = {"gb": max(0, _num(t.get("gb", 0))), "days": max(0, int(t.get("days", 0))), "mode": mode, "kind": kind,
                          "price": max(0, int(_num(t.get("price", 0))))}
        except (TypeError, ValueError, AttributeError):
            continue
    if not tpls:
        tpls = copy.deepcopy(DEFAULT_TEMPLATES)
    cfg["templates"] = tpls
    if cfg.get("active_template") not in tpls:
        cfg["active_template"] = next(iter(tpls))
    if cfg.get("active_group_template") not in tpls:
        cfg["active_group_template"] = next((n for n, t in tpls.items() if t["kind"] == "group"), "")
    ids = []
    for x in cfg.get("admin_ids") or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    cfg["admin_ids"] = ids
    cfg["panels"] = _normalize_panels(cfg, data if isinstance(data, dict) else {})
    for k in ("panel_url", "sub_url", "username", "password", "verify_ssl", "services"):
        cfg.pop(k, None)
    ids = [p["id"] for p in cfg["panels"]]
    if cfg.get("active_panel") not in ids:
        cfg["active_panel"] = ids[0] if ids else ""
    if not isinstance(cfg.get("web"), dict):
        cfg["web"] = {}
    for k, v in DEFAULT_CFG["web"].items():
        cfg["web"].setdefault(k, v)
    ssl_cfg = copy.deepcopy(DEFAULT_CFG["ssl"])
    if isinstance(cfg.get("ssl"), dict):
        ssl_cfg.update({k: v for k, v in cfg["ssl"].items() if k in ssl_cfg})
    ssl_cfg["enabled"] = bool(ssl_cfg["enabled"])
    for k in ("cert", "key", "domain", "error"):
        ssl_cfg[k] = str(ssl_cfg.get(k) or "").strip()
    cfg["ssl"] = ssl_cfg
    sales = copy.deepcopy(DEFAULT_CFG["sales"])
    if isinstance(cfg.get("sales"), dict):
        sales.update(cfg["sales"])
    for k in ("hamkari_service_ids", "hamkari_service_names", "ignore_service_names", "single_prices"):
        if not isinstance(sales.get(k), list):
            sales[k] = copy.deepcopy(DEFAULT_CFG["sales"][k])
    sales["single_prices"] = [p for p in sales["single_prices"] if isinstance(p, dict) and {"gb", "months", "price"} <= p.keys()]
    for k in ("poll_seconds", "hamkari_per_gb"):
        try:
            sales[k] = int(sales[k])
        except (TypeError, ValueError):
            sales[k] = DEFAULT_CFG["sales"][k]
    cfg["sales"] = sales
    pre = str(cfg.get("default_prefix") or "")
    cfg["default_prefix"] = pre if re.fullmatch(r"[A-Za-z0-9_]{0,28}", pre) else DEFAULT_CFG["default_prefix"]
    for key in ("monitor", "reminders", "backup"):
        sec = copy.deepcopy(DEFAULT_CFG[key])
        if isinstance(cfg.get(key), dict):
            sec.update(cfg[key])
        for k, dv in DEFAULT_CFG[key].items():
            if isinstance(dv, bool):
                sec[k] = bool(sec[k])
            else:
                try:
                    sec[k] = max(1, int(sec[k]))
                    if key == "backup" and k == "interval_min":
                        sec[k] = min(1440, max(5, sec[k]))
                except (TypeError, ValueError):
                    sec[k] = dv
        cfg[key] = sec
    if cfg.get("next_plan_mode") not in ("duration", "chain"):
        cfg["next_plan_mode"] = DEFAULT_CFG["next_plan_mode"]
    und = copy.deepcopy(DEFAULT_CFG["undo"])
    if isinstance(cfg.get("undo"), dict):
        und.update({k: v for k, v in cfg["undo"].items() if k in und})
    und["enabled"] = bool(und["enabled"])
    for k in ("keep", "days", "bulk_max"):
        try:
            und[k] = max(1, int(und[k]))
        except (TypeError, ValueError):
            und[k] = DEFAULT_CFG["undo"][k]
    cfg["undo"] = und
    un = cfg.get("unpaid") if isinstance(cfg.get("unpaid"), dict) else {}
    try:
        hrs = min(24 * 365, max(1, int(un.get("hours", DEFAULT_CFG["unpaid"]["hours"]))))
    except (TypeError, ValueError):
        hrs = DEFAULT_CFG["unpaid"]["hours"]
    pend = {}
    for pid, m in (un.get("pending") if isinstance(un.get("pending"), dict) else {}).items():
        if not isinstance(m, dict):
            continue
        for uname, rec in m.items():
            try:
                pend.setdefault(str(pid), {})[str(uname)] = {"due": int(rec["due"]), "set": int(rec.get("set") or 0)}
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    cfg["unpaid"] = {"hours": hrs, "pending": pend}
    reps = {}
    for rid, r in (cfg.get("reps") if isinstance(cfg.get("reps"), dict) else {}).items():
        if isinstance(r, dict) and r.get("name") and r.get("salt") and r.get("hash"):
            reps[str(rid)] = {"name": str(r["name"]), "label": str(r.get("label") or "")[:60], "salt": str(r["salt"]), "hash": str(r["hash"]),
                              "blocked": bool(r.get("blocked")), "created": int(r.get("created") or 0),
                              "locked": bool(r.get("locked")), "fails": max(0, int(r.get("fails") or 0)), "locked_at": int(r.get("locked_at") or 0),
                              "balance": int(r.get("balance") or 0), "panel": str(r.get("panel") or ""),
                              "tpls": [str(x) for x in (r.get("tpls") if isinstance(r.get("tpls"), list) else []) if str(x) in tpls],
                              "log": [e for e in (r.get("log") if isinstance(r.get("log"), list) else []) if isinstance(e, dict)][-WALLET_LOG_MAX:]}
    cfg["reps"] = reps
    tu = []
    for t in (cfg.get("topups") if isinstance(cfg.get("topups"), list) else []):
        try:
            if isinstance(t, dict) and str(t.get("rid")) in reps:
                tu.append({"id": str(t["id"]), "rid": str(t["rid"]), "amount": max(0, int(t.get("amount") or 0)), "note": str(t.get("note") or "")[:200],
                           "ts": int(t.get("ts") or 0), "status": t.get("status") if t.get("status") in ("pending", "approved", "rejected") else "pending",
                           "done": int(t.get("done") or 0)})
        except (TypeError, ValueError, KeyError):
            continue
    cfg["topups"] = tu[-TOPUP_MAX:]
    wi = []
    for x in (cfg.get("wallet_income") if isinstance(cfg.get("wallet_income"), list) else []):
        try:
            if isinstance(x, dict) and int(x.get("amount") or 0) > 0:
                wi.append({"ts": float(x["ts"]), "rid": str(x.get("rid") or ""), "amount": int(x["amount"])})
        except (TypeError, ValueError, KeyError):
            continue
    cfg["wallet_income"] = wi[-WALLET_INCOME_MAX:]
    ru = {}
    for pid, m in (cfg.get("rep_users") if isinstance(cfg.get("rep_users"), dict) else {}).items():
        if isinstance(m, dict):
            ru[str(pid)] = {str(u): str(r) for u, r in m.items() if str(r) in reps}
    cfg["rep_users"] = ru
    rn = {}
    for pid, m in (cfg.get("rep_notes") if isinstance(cfg.get("rep_notes"), dict) else {}).items():
        if isinstance(m, dict):
            v = {str(u): str(n).strip()[:REP_NOTE_MAX] for u, n in m.items() if str(u) in ru.get(str(pid), {}) and str(n).strip()}
            if v:
                rn[str(pid)] = v
    cfg["rep_notes"] = rn
    rp = {}
    for pid, m in (cfg.get("rep_paid") if isinstance(cfg.get("rep_paid"), dict) else {}).items():
        if isinstance(m, dict):
            v = {}
            for u, ts in m.items():
                if str(u) in ru.get(str(pid), {}):
                    try:
                        v[str(u)] = int(ts) or 1
                    except (TypeError, ValueError):
                        v[str(u)] = 1
            if v:
                rp[str(pid)] = v
    cfg["rep_paid"] = rp
    for k in ("max_batch", "max_qr", "web_port"):
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CFG[k]
    return cfg


def load_config() -> dict:
    """کانفیگ را می‌خواند؛ تا وقتی فایل تغییر نکرده از کش استفاده می‌کند (بدون I/O اضافه)."""
    with _lock:
        try:
            st = os.stat(CONFIG_FILE)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if _cache["data"] is None or _cache["sig"] != sig:
            data = {}
            if sig is not None:
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError) as e:
                    log.error("config.json قابل خواندن نیست: %s", e)
            _cache["data"] = _normalize(data)
            _cache["sig"] = sig
        return copy.deepcopy(_cache["data"])


def save_config(cfg: dict) -> None:
    """ذخیره اتمیک (نوشتن در فایل موقت و جایگزینی) با دسترسی 600."""
    with _lock:
        d = os.path.dirname(CONFIG_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, CONFIG_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        _cache["data"] = None


def config_backup_bytes() -> bytes:
    """محتوای فعلی config.json برای بکاپ."""
    with open(CONFIG_FILE, "rb") as f:
        return f.read()


def update_config(mutator) -> dict:
    with _lock:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return load_config()


# ───────────────────────────── تمدید «واریز نشده» ─────────────────────────────
def unpaid_mark(pid: str, username: str, hours: int | None = None) -> int:
    """کاربر را «واریز نشده» ثبت می‌کند؛ بعد از `hours` ساعت (پیش‌فرض از تنظیمات) خودکار غیرفعال می‌شود. زمان سررسید را برمی‌گرداند."""
    due = 0

    def mut(c):
        nonlocal due
        h = int(hours) if hours else c["unpaid"]["hours"]
        h = min(24 * 365, max(1, h))
        now = int(time.time())
        due = now + h * 3600
        c["unpaid"]["pending"].setdefault(pid, {})[username] = {"due": due, "set": now}
    update_config(mut)
    return due


def unpaid_clear(pid: str, usernames) -> int:
    """«واریز شد» یا حذف از صف؛ تعداد ردیف‌های پاک‌شده را برمی‌گرداند."""
    names = [usernames] if isinstance(usernames, str) else list(usernames)
    n = 0

    def mut(c):
        nonlocal n
        m = c["unpaid"]["pending"].get(pid) or {}
        for u in names:
            if m.pop(u, None) is not None:
                n += 1
        if not m:
            c["unpaid"]["pending"].pop(pid, None)
    if names and any(u in (load_config()["unpaid"]["pending"].get(pid) or {}) for u in names):
        update_config(mut)
    return n


def unpaid_map(pid: str) -> dict:
    return load_config()["unpaid"]["pending"].get(pid, {})


def is_admin(uid: int) -> bool:
    return uid in load_config()["admin_ids"]


# ─────────────────────── رمز وب‌پنل (PBKDF2 + کش) ───────────────────────
def hash_password(pw: str, salt: str | None = None) -> dict:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return {"salt": salt, "hash": h}


_auth_ok: dict[str, float] = {}


def web_configured() -> bool:
    return bool(load_config()["web"].get("hash"))


def check_web_login(user: str, pw: str) -> bool:
    w = load_config()["web"]
    if not w.get("hash"):
        return False
    key = hashlib.sha256(f"{user}\0{pw}\0{w['hash']}".encode()).hexdigest()
    now = time.time()
    if _auth_ok.get(key, 0) > now:
        return True
    ok_user = secrets.compare_digest(user.encode(), str(w.get("user", "admin")).encode())
    ok_pass = secrets.compare_digest(hash_password(pw, w["salt"])["hash"], w["hash"])
    if ok_user and ok_pass:
        if len(_auth_ok) > 50:
            _auth_ok.clear()
        _auth_ok[key] = now + 300
        return True
    return False


# ─────────────────────── نشست ورود (کوکی امضاشده) ───────────────────────
SESSION_SHORT = 12 * 3600
SESSION_LONG = 30 * 86400


def _session_key() -> bytes | None:
    """کلید امضا = راز تصادفی + هش رمز؛ با عوض شدن رمز همه‌ی نشست‌های قبلی باطل می‌شوند."""
    cfg = load_config()
    w = cfg["web"]
    if not w.get("hash"):
        return None
    if not w.get("secret"):
        def mut(c):
            if not c["web"].get("secret"):
                c["web"]["secret"] = secrets.token_hex(32)
        cfg = update_config(mut)
        w = cfg["web"]
    return f"{w['secret']}|{w['hash']}".encode()


def make_session(user: str, long: bool = False) -> tuple[str, int]:
    key = _session_key()
    if key is None:
        raise ValueError("رمز وب‌پنل تنظیم نشده است.")
    ttl = SESSION_LONG if long else SESSION_SHORT
    exp = int(time.time()) + ttl
    body = f"{user}|{exp}"
    import hmac
    sig = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}", ttl


def check_session(token: str) -> str | None:
    """نام کاربر را برمی‌گرداند اگر نشست معتبر باشد."""
    import hmac
    try:
        user, exp, sig = token.rsplit("|", 2)
        key = _session_key()
        if key is None or int(exp) < time.time():
            return None
        good = hmac.new(key, f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and secrets.compare_digest(user.encode(), str(load_config()["web"].get("user", "admin")).encode()):
            return user
    except (ValueError, AttributeError):
        pass
    return None


# ───────────────────────────── نمایندگان ─────────────────────────────
REP_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,32}")
REP_COOKIE_TTL = 12 * 3600


def _rep_find(cfg: dict, name: str) -> tuple[str, dict] | None:
    n = (name or "").strip().lower()
    return next(((rid, r) for rid, r in cfg["reps"].items() if r["name"].lower() == n), None)


def create_rep(name: str, password: str = "", label: str = "") -> dict:
    """نماینده‌ی جدید؛ اگر رمز خالی باشد تصادفی ساخته می‌شود. رمز فقط همین یک بار برگردانده می‌شود."""
    name = (name or "").strip()
    if not REP_NAME_RE.fullmatch(name):
        raise ValueError("نام کاربری نماینده: ۳ تا ۳۲ حرف انگلیسی، عدد، _ . -")
    password = password or secrets.token_urlsafe(9)
    if len(password) < 6:
        raise ValueError("رمز نماینده حداقل ۶ کاراکتر باشد.")
    out = {}

    def mut(c):
        if name.lower() == str(c["web"].get("user", "admin")).lower() or _rep_find(c, name):
            raise ValueError("این نام کاربری قبلاً استفاده شده است.")
        rid = "r" + secrets.token_hex(4)
        c["reps"][rid] = {"name": name, "label": (label or "").strip()[:60], **hash_password(password), "blocked": False, "created": int(time.time()),
                          "locked": False, "fails": 0, "locked_at": 0,
                          "balance": 0, "panel": "", "tpls": [], "log": []}
        out.update(id=rid, name=name)
    update_config(mut)
    return {**out, "password": password}


def reset_rep_password(rid: str, password: str = "") -> str:
    password = password or secrets.token_urlsafe(9)
    if len(password) < 6:
        raise ValueError("رمز نماینده حداقل ۶ کاراکتر باشد.")

    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        c["reps"][rid].update(hash_password(password))
        c["reps"][rid].update(locked=False, fails=0, locked_at=0)      # رمز جدید = شروع تازه
    update_config(mut)
    return password


def set_rep_blocked(rid: str, blocked: bool) -> None:
    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        c["reps"][rid]["blocked"] = bool(blocked)
    update_config(mut)


def delete_rep(rid: str) -> None:
    def mut(c):
        c["reps"].pop(rid, None)
        for m in c["rep_users"].values():
            for u in [u for u, r in m.items() if r == rid]:
                del m[u]
        # یادداشت‌های کاربرهای حذف‌شده در نرمال‌سازی پاک می‌شود
    update_config(mut)


def check_rep_login(name: str, pw: str) -> tuple[str, bool] | None:
    """(rep_id, blocked) اگر نام و رمز درست باشد، وگرنه None."""
    f = _rep_find(load_config(), name)
    if not f:
        hash_password(pw or "x", "00" * 16)          # زمان پاسخ برای نام ناموجود هم مشابه باشد
        return None
    rid, r = f
    if secrets.compare_digest(hash_password(pw, r["salt"])["hash"], r["hash"]):
        return rid, r["blocked"]
    return None


REP_MAX_FAILS = 5


def rep_login(name: str, pw: str) -> tuple[str, str | None]:
    """ورود نماینده با قفل حساب: ("ok"|"bad"|"blocked"|"locked"|"locked_now"|"none", rep_id).

    ۵ رمز اشتباه پیاپی روی یک نماینده، همان حساب را موقتاً غیرفعال می‌کند ("locked_now" فقط در همان لحظه‌ی قفل شدن)؛
    تا وقتی مدیر دستی آزادش نکند، حتی با رمز درست هم وارد نمی‌شود. ورود موفق شمارنده را صفر می‌کند.
    """
    f = _rep_find(load_config(), name)
    if not f:
        hash_password(pw or "x", "00" * 16)          # زمان پاسخ برای نام ناموجود هم مشابه باشد
        return "none", None
    rid, r = f
    if r.get("locked"):
        return "locked", rid                          # رمز بررسی نمی‌شود تا حدس‌زدن روی حساب قفل‌شده ممکن نباشد
    if secrets.compare_digest(hash_password(pw, r["salt"])["hash"], r["hash"]):
        if r.get("fails"):
            def ok(c):
                if rid in c["reps"]:
                    c["reps"][rid]["fails"] = 0
            update_config(ok)
        return ("blocked" if r["blocked"] else "ok"), rid
    state = {"v": "bad"}

    def bad(c):
        rr = c["reps"].get(rid)
        if not rr or rr.get("locked"):
            return
        rr["fails"] = int(rr.get("fails") or 0) + 1
        if rr["fails"] >= REP_MAX_FAILS:
            rr["locked"], rr["locked_at"] = True, int(time.time())
            state["v"] = "locked_now"
    update_config(bad)
    return state["v"], rid


def unlock_rep(rid: str) -> None:
    """فعال‌سازی مجدد نماینده‌ای که به‌خاطر رمز اشتباه قفل شده."""
    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        c["reps"][rid].update(locked=False, fails=0, locked_at=0)
    update_config(mut)


def backup_summary(cfg: dict | None = None) -> dict:
    """شمارش نماینده‌ها و کاربرهای تخصیص‌یافته (برای نمایش در بکاپ/بازیابی)."""
    cfg = cfg or load_config()
    return {"panels": len(cfg["panels"]), "reps": len(cfg["reps"]),
            "rep_users": sum(len(m) for m in cfg["rep_users"].values())}


def _rep_key(rep: dict) -> bytes | None:
    k = _session_key()
    return None if k is None else k + b"|rep|" + rep["hash"].encode()      # با عوض شدن رمز نماینده، نشست‌هایش باطل می‌شود


def make_rep_session(rid: str) -> tuple[str, int]:
    import hmac
    rep = load_config()["reps"][rid]
    exp = int(time.time()) + REP_COOKIE_TTL
    body = f"{rid}|{exp}"
    return f"{body}|{hmac.new(_rep_key(rep), body.encode(), hashlib.sha256).hexdigest()}", REP_COOKIE_TTL


def check_rep_session(token: str) -> tuple[str, dict] | None:
    """(rep_id, rep) اگر نشست معتبر و نماینده مسدود نباشد."""
    import hmac
    try:
        rid, exp, sig = token.rsplit("|", 2)
        rep = load_config()["reps"].get(rid)
        key = _rep_key(rep) if rep else None
        if key is None or rep["blocked"] or rep.get("locked") or int(exp) < time.time():
            return None
        if hmac.compare_digest(sig, hmac.new(key, f"{rid}|{exp}".encode(), hashlib.sha256).hexdigest()):
            return rid, rep
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def parse_user_spec(prefix: str = "", start: int | None = None, end: int | None = None, names: str = "") -> list[str]:
    """ورودی ادمین ← فهرست نام کاربری: بازه (پیشوند + از + تا) یا فهرست جداشده با فاصله/کاما/خط."""
    out: list[str] = []
    if names and names.strip():
        out = [x for x in re.split(r"[\s,;،]+", names.strip()) if x]
    elif prefix and start is not None and end is not None:
        if end < start or end - start > 4999:
            raise ValueError("بازه نامعتبر است (حداکثر ۵۰۰۰ کاربر).")
        out = [f"{prefix}{n}" for n in range(start, end + 1)]
    bad = [u for u in out if not USERNAME_RE.fullmatch(u)]
    if bad:
        raise ValueError(f"نام کاربری نامعتبر: {bad[0]}")
    return list(dict.fromkeys(out))


def assign_users(rid: str, pid: str, names: list[str]) -> int:
    """کاربرها را به نماینده نسبت می‌دهد (اگر قبلاً مال نماینده‌ی دیگری بود، منتقل می‌شود). تعداد را برمی‌گرداند."""
    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        m = c["rep_users"].setdefault(pid, {})
        notes = c["rep_notes"].get(pid, {})
        for u in names:
            if m.get(u) != rid:
                notes.pop(u, None)            # کاربر از نماینده‌ی دیگری منتقل شد: یادداشت و «پرداخت شد»ِ قبلی مال او بود
                c["rep_paid"].get(pid, {}).pop(u, None)
            m[u] = rid
    update_config(mut)
    return len(names)


def unassign_users(rid: str, pid: str, names: list[str]) -> int:
    n = []

    def mut(c):
        m = c["rep_users"].get(pid, {})
        for u in names:
            if m.get(u) == rid:
                del m[u]
                c["rep_notes"].get(pid, {}).pop(u, None)
                c["rep_paid"].get(pid, {}).pop(u, None)
                n.append(u)
    update_config(mut)
    return len(n)


def set_rep_note(rid: str, pid: str, username: str, note: str) -> str:
    """یادداشت نماینده برای یکی از کاربرهای خودش را ذخیره می‌کند (خالی = حذف). متن ذخیره‌شده را برمی‌گرداند."""
    note = " ".join(str(note).split())[:REP_NOTE_MAX]

    def mut(c):
        if c["rep_users"].get(pid, {}).get(username) != rid:
            raise ValueError("این کاربر در لیست شما نیست.")
        m = c["rep_notes"].setdefault(pid, {})
        if note:
            m[username] = note
        else:
            m.pop(username, None)
    update_config(mut)
    return note


def set_rep_paid(rid: str, pid: str, username: str, paid: bool) -> bool:
    """علامت «پرداخت شد» (مشتری پول را به نماینده داده) برای یکی از کاربرهای خود نماینده."""
    def mut(c):
        if c["rep_users"].get(pid, {}).get(username) != rid:
            raise ValueError("این کاربر در لیست شما نیست.")
        m = c["rep_paid"].setdefault(pid, {})
        if paid:
            m[username] = int(time.time())
        else:
            m.pop(username, None)
    update_config(mut)
    return bool(paid)


# ───────────────────────────── کیف پول نماینده ─────────────────────────────
def _wlog(c: dict, rid: str, kind: str, amount: int, note: str = "") -> None:
    r = c["reps"][rid]
    r["log"] = (r.get("log", []) + [{"ts": int(time.time()), "type": kind, "amount": int(amount), "balance": r["balance"], "note": str(note)[:120]}])[-WALLET_LOG_MAX:]


def wallet_adjust(rid: str, amount: int, kind: str = "charge", note: str = "") -> int:
    """موجودی نماینده را با `amount` (مثبت = شارژ، منفی = کسر) تغییر می‌دهد؛ موجودی جدید را برمی‌گرداند. موجودی منفی مجاز نیست."""
    amount = int(amount)
    out = {}

    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        nb = c["reps"][rid]["balance"] + amount
        if nb < 0:
            raise ValueError("موجودی کافی نیست.")
        if nb > 10 ** 11:
            raise ValueError("سقف موجودی رد می‌شود.")
        c["reps"][rid]["balance"] = nb
        _wlog(c, rid, kind, amount, note)
        if kind == "charge" and amount > 0:                      # شارژ دستی ادمین ← فروش امروز
            c["wallet_income"].append({"ts": time.time(), "rid": rid, "amount": amount})
        out["b"] = nb
    update_config(mut)
    return out["b"]


def set_rep_templates(rid: str, names: list[str]) -> list[str]:
    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        c["reps"][rid]["tpls"] = [n for n in dict.fromkeys(names) if n in c["templates"]]
    return update_config(mut)["reps"][rid]["tpls"]


def topup_add(rid: str, amount: int, note: str = "") -> dict:
    rec = {}

    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        if sum(1 for t in c["topups"] if t["rid"] == rid and t["status"] == "pending") >= 5:
            raise ValueError("۵ درخواست در انتظار دارید؛ تا بررسی ادمین صبر کنید.")
        rec.update(id="t" + secrets.token_hex(4), rid=rid, amount=int(amount), note=str(note).strip()[:200], ts=int(time.time()), status="pending", done=0)
        c["topups"].append(dict(rec))
    update_config(mut)
    return rec


def topup_resolve(tid: str, approve: bool, amount: int | None = None) -> dict:
    """تأیید (با شارژ کیف پول؛ مبلغ قابل اصلاح) یا رد درخواست."""
    out = {}

    def mut(c):
        t = next((x for x in c["topups"] if x["id"] == tid), None)
        if not t or t["status"] != "pending":
            raise ValueError("این درخواست قبلاً بررسی شده یا وجود ندارد.")
        if approve:
            amt = int(amount if amount is not None else t["amount"])
            if amt <= 0:
                raise ValueError("مبلغ نامعتبر است.")
            c["reps"][t["rid"]]["balance"] += amt
            _wlog(c, t["rid"], "topup", amt, "تأیید درخواست افزایش موجودی")
            c["wallet_income"].append({"ts": time.time(), "rid": t["rid"], "amount": amt})     # تأیید درخواست ← فروش امروز
            t["amount"] = amt
        t["status"] = "approved" if approve else "rejected"
        t["done"] = int(time.time())
        out.update(t)
    update_config(mut)
    return out


def rep_owner(cfg: dict, pid: str, username: str) -> str | None:
    return cfg["rep_users"].get(pid, {}).get(username)


# ───────────────────────────── کمکی‌ها ─────────────────────────────
def esc(s) -> str:
    return html.escape(str(s), quote=False)


def fmt_gb(size) -> str:
    return "نامحدود" if not size or size <= 0 else f"{size / GB:.2f} GB"


def fmt_bw(size) -> str:
    """فرمت فشرده‌ی حجم/پهنای باند: 23M ، 1.5G ، 512K (بدون فاصله و بدون اعشار اضافه)."""
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        n = 0.0
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            v = f"{n:.0f}" if n >= 10 or unit == "B" else f"{n:.1f}".rstrip("0").rstrip(".")
            return f"{v}{unit}"
        n /= 1024
    return "0B"


def pick_default_service(services: list[dict], group: bool) -> int | None:
    """سرویس پیش‌فرض: ساخت تکی ← «ms» ، ساخت گروهی ← «hamkari». اگر پیدا نشد None."""
    cfg = load_config().get("sales", {})
    ham_ids = {int(x) for x in cfg.get("hamkari_service_ids", []) if str(x).lstrip("-").isdigit()}
    ham_names = [str(n).lower() for n in cfg.get("hamkari_service_names", ["hamkari"]) if n] or ["hamkari"]

    def is_ham(s: dict) -> bool:
        return s["id"] in ham_ids or any(n in s["name"].lower() for n in ham_names)

    if group:
        return next((s["id"] for s in services if is_ham(s)), None)
    cands = [s for s in services if not is_ham(s)]
    exact = next((s["id"] for s in cands if s["name"].strip().lower() == "ms"), None)
    if exact is not None:
        return exact
    return next((s["id"] for s in cands if re.search(r"(^|[^a-z0-9])ms([^a-z0-9]|$)", s["name"].lower())), None)


def fmt_expire(exp, status, on_hold_dur=None) -> str:
    if status == "on_hold":
        return f"{int((on_hold_dur or 0) // 86400)} روز (On-Hold)"
    if not exp:
        return "نامحدود"
    rem = int(exp) - int(time.time())
    return "منقضی شده" if rem <= 0 else f"{rem // 86400} روز و {(rem % 86400) // 3600} ساعت"


KIND_LABEL = {"single": "👤 تکی (ms)", "group": "👥 همکاری (hamkari)"}


def tpl_kind(t: dict) -> str:
    return "group" if t.get("kind") == "group" else "single"


def templates_of(cfg: dict, kind: str) -> dict:
    """تمپلت‌های یک نوع؛ اگر هیچ‌کدام نبود همه را برمی‌گرداند (تا فرم خالی نماند)."""
    sel = {n: t for n, t in cfg["templates"].items() if tpl_kind(t) == kind}
    return sel or dict(cfg["templates"])


def default_template(cfg: dict, kind: str) -> str:
    """تمپلت فعال هر نوع: تکی ← active_template ، گروهی ← active_group_template."""
    pool = templates_of(cfg, kind)
    key = "active_group_template" if kind == "group" else "active_template"
    return cfg.get(key) if cfg.get(key) in pool else next(iter(pool))


def tpl_summary(t: dict) -> str:
    gb = "نامحدود" if not t["gb"] else f"{t['gb']}GB"
    d = "نامحدود" if not t["days"] else f"{t['days']} روز"
    return f"{gb} | {d} | {t['mode']}"


def make_names(base: str, start, count: int, max_batch: int) -> list[str]:
    base = (base or "").strip()
    if start is None:
        names = [base]
    else:
        if not 1 <= count <= max_batch:
            raise ValueError(f"تعداد باید بین 1 و {max_batch} باشد.")
        if start < 0:
            raise ValueError("عدد شروع نباید منفی باشد.")
        names = [f"{base}{start + i}" for i in range(count)]
    for n in (names[0], names[-1]):
        if not USERNAME_RE.match(n):
            raise ValueError(f"نام «{n}» معتبر نیست؛ فقط حروف انگلیسی، عدد و _ (۳ تا ۳۲ کاراکتر).")
    return names


def build_payload(username: str, tpl: dict, service_id: int) -> dict:
    gb, days, mode = tpl["gb"], int(tpl["days"]), tpl.get("mode", "on_hold")
    p = {"username": username, "data_limit": int(gb * GB) if gb > 0 else 0, "service_id": int(service_id), "status": mode}
    if mode == "on_hold":
        p["on_hold_expire_duration"] = days * 86400 if days > 0 else 30 * 86400
    else:
        p["expire"] = int(time.time()) + days * 86400 if days > 0 else 0
    return p


def build_renew_payload(tpl: dict) -> dict:
    """تمدید با تمپلت: حجم و مدت جدید، به حالت On-Hold (اولین اتصال) یا Active."""
    gb, days, mode = tpl["gb"], int(tpl["days"]), tpl.get("mode", "on_hold")
    p = {"data_limit": int(gb * GB) if gb > 0 else 0, "status": mode}
    if mode == "on_hold":
        p["on_hold_expire_duration"] = days * 86400 if days > 0 else 30 * 86400
        p["expire"] = 0
    else:
        p["expire"] = int(time.time()) + days * 86400 if days > 0 else 0
    return p


def make_qr_png(text: str) -> bytes:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(box_size=8, border=2, error_correction=ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    bio = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(bio, "PNG")
    return bio.getvalue()


# ───────────────────────────── کلاینت ربکا ─────────────────────────────
class RebeccaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json()
    except ValueError:
        return (resp.text or "")[:200] or f"HTTP {resp.status_code}"
    if isinstance(d, dict):
        d = d.get("detail", d)
    if isinstance(d, list):
        return "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
    if isinstance(d, dict):
        return "; ".join(f"{k}: {v}" for k, v in d.items())
    return str(d)


def _strip_dashboard(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/dashboard"):
        url = url[: -len("/dashboard")]
    return url.rstrip("/")


class RebeccaClient:
    LOGIN_PATHS = ("/api/admin/token", "/dashboard/api/admin/token", "/api/v1/admin/token")
    SERVICE_PATHS = ("/api/services", "/api/service", "/api/v1/services")

    def __init__(self, pid: str = ""):
        self.pid = pid
        self.name = ""
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._sig = None
        self._lock: asyncio.Lock | None = None
        self._login_path: str | None = None
        self._svc = (0.0, [])
        self.base = self.sub_base = self.username = self.password = ""
        self.verify = False

    # تنظیمات هر بار از کانفیگ همگام می‌شود؛ با تغییر آن، نشست قبلی دور ریخته می‌شود
    def _sync(self):
        cfg = load_config()
        pn = get_panel(cfg, self.pid) or {}
        base = _strip_dashboard(pn.get("panel_url", ""))
        sig = (base, pn.get("username", ""), pn.get("password", ""), bool(pn.get("verify_ssl")))
        if sig != self._sig:
            old, self._http = self._http, None
            self._token = self._login_path = None
            self._svc = (0.0, [])
            self._sig = sig
            if old is not None:
                try:
                    asyncio.get_running_loop().create_task(old.aclose())
                except RuntimeError:
                    pass
        self.base, self.username, self.password = base, pn.get("username", ""), pn.get("password", "")
        self.verify = bool(pn.get("verify_ssl"))
        self.name = pn.get("name", "")
        self.sub_base = _strip_dashboard(pn.get("sub_url", "")) or base
        return cfg

    def is_configured(self) -> bool:
        self._sync()
        return bool(self.base and self.username and self.password)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=self.verify,
                timeout=httpx.Timeout(25.0, connect=8.0),
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
                headers={"Accept": "application/json"},
            )
        return self._http

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def login(self, stale: str | None = None) -> str:
        self._sync()
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:  # فقط یک لاگین هم‌زمان؛ بقیه از توکن تازه استفاده می‌کنند
            if self._token and self._token != stale:
                return self._token
            if not (self.base and self.username and self.password):
                raise RebeccaError("اتصال به پنل تنظیم نشده است.")
            paths = list(self.LOGIN_PATHS)
            if self._login_path in paths:
                paths.remove(self._login_path)
                paths.insert(0, self._login_path)
            last = "ورود ناموفق"
            for p in paths:
                try:
                    r = await self._client().post(
                        self.base + p, data={"username": self.username, "password": self.password}
                    )
                except httpx.HTTPError as e:
                    last = f"اتصال به پنل ممکن نیست ({e.__class__.__name__})"
                    continue
                if r.status_code == 200:
                    try:
                        self._token = r.json()["access_token"]
                        self._login_path = p
                        return self._token
                    except (ValueError, KeyError):
                        last = "پاسخ نامعتبر از پنل"
                else:
                    last = _detail(r)
            raise RebeccaError(last, 401)

    async def request(self, method: str, path: str, *, retries: int = 2, **kw):
        self._sync()
        if not self._token:
            await self.login()
        refreshed, attempt = False, 0
        while True:
            tok = self._token
            try:
                r = await self._client().request(method, self.base + path, headers={"Authorization": f"Bearer {tok}"}, **kw)
            except httpx.HTTPError as e:
                # برای درخواست‌های نوشتنی فقط وقتی تکرار می‌کنیم که اصلاً به پنل نرسیده باشد
                safe = method in ("GET", "DELETE") or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if safe and attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.4 * attempt)
                    continue
                raise RebeccaError(f"اتصال به پنل ممکن نیست ({e.__class__.__name__})") from e
            if r.status_code == 401 and not refreshed:
                refreshed = True
                await self.login(stale=tok)
                continue
            if r.status_code in (502, 503, 504) and method == "GET" and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
                continue
            if r.status_code >= 400:
                raise RebeccaError(_detail(r), r.status_code)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}

    # ── توابع سطح بالا ──
    @staticmethod
    def upath(username: str) -> str:
        return f"/api/user/{quote(username, safe='')}"

    def full_sub(self, sub: str) -> str:
        """لینک ساب را روی دامنه‌ی سابسکریپشن تنظیم‌شده می‌سازد (query حفظ می‌شود)."""
        self._sync()
        if not sub:
            return ""
        if sub.startswith("http"):
            sp = urlsplit(sub)
            return f"{self.sub_base}{sp.path}" + (f"?{sp.query}" if sp.query else "")
        return f"{self.sub_base}/{sub.lstrip('/')}"

    async def list_services(self, force: bool = False) -> list[dict]:
        if not force and self._svc[1] and time.time() - self._svc[0] < 120:
            return self._svc[1]
        if self.is_configured():
            for ep in self.SERVICE_PATHS:
                try:
                    data = await self.request("GET", ep, retries=1)
                except RebeccaError:
                    continue
                items = data
                if isinstance(data, dict):
                    items = next((data[k] for k in ("services", "items", "data", "results") if isinstance(data.get(k), list)), [])
                out = []
                for s in items if isinstance(items, list) else []:
                    if isinstance(s, dict) and s.get("id") is not None:
                        out.append({"id": int(s["id"]), "name": str(s.get("name") or s.get("title") or s["id"])})
                if out:
                    self._svc = (time.time(), out)
                    def save(c):
                        pn = get_panel(c, self.pid)
                        if pn is not None and pn["services"] != out:
                            pn["services"] = out
                    update_config(save)
                    return out
        return (get_panel(load_config(), self.pid) or {}).get("services", [])

    async def iter_users(self, status: str | None = None, page: int = 200) -> list[dict]:
        out, off = [], 0
        while off < 50000:
            params = {"offset": off, "limit": page}
            if status:
                params["status"] = status
            res = await self.request("GET", "/api/users", params=params)
            users = res.get("users", []) if isinstance(res, dict) else (res or [])
            out.extend(users)
            total = res.get("total") if isinstance(res, dict) else None
            if len(users) < page or (total is not None and len(out) >= total):
                break
            off += page
        return out


_clients: dict[str, RebeccaClient] = {}


def get_api(pid: str | None = None) -> RebeccaClient:
    """کلاینت مخصوص هر پنل (توکن و نشست جدا). بدون pid یعنی پنل فعال."""
    pid = pid or load_config()["active_panel"]
    if pid not in _clients:
        _clients[pid] = RebeccaClient(pid)
    _clients[pid]._sync()  # نام و آدرس‌ها همیشه با config هماهنگ باشند
    return _clients[pid]


def drop_api(pid: str):
    c = _clients.pop(pid, None)
    return c


async def create_users(api, names, tpl, service_id, *, concurrency=5, on_progress=None):
    """ساخت هم‌زمان کنترل‌شده. خروجی: (موفق‌ها به ترتیب، [(نام، دلیل خطا)])."""
    sem = asyncio.Semaphore(concurrency)
    ok, failed, done = {}, {}, 0

    async def one(n):
        nonlocal done
        async with sem:
            try:
                r = await api.request("POST", "/api/user", json=build_payload(n, tpl, service_id))
                ok[n] = {"username": n, "sub": api.full_sub((r or {}).get("subscription_url", ""))}
            except RebeccaError as e:
                failed[n] = "از قبل وجود دارد" if e.status == 409 else str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("create %s", n)
                failed[n] = f"خطای غیرمنتظره: {e.__class__.__name__}"
            done += 1
            if on_progress:
                try:
                    await on_progress(done, len(names))
                except Exception:  # noqa: BLE001
                    pass

    await asyncio.gather(*(one(n) for n in names))
    return [ok[n] for n in names if n in ok], [(n, failed[n]) for n in names if n in failed]


async def apply_user_edit(api, username: str, gb, days, add_gb=None, add_days=None):
    """gb/days = مقدار جدید (None = بدون تغییر). add_gb/add_days = افزودن به مقدار فعلی (بدون صفر شدن مصرف)."""
    u = await api.request("GET", api.upath(username))
    p = {}
    if add_gb:
        cur = int(u.get("data_limit") or 0)
        if cur > 0:                      # نامحدود همان نامحدود می‌ماند
            p["data_limit"] = max(0, cur + int(add_gb * GB))
    elif gb is not None:
        p["data_limit"] = int(gb * GB) if gb > 0 else 0
    if add_days:
        if u.get("status") == "on_hold":
            cur = int(u.get("on_hold_expire_duration") or 0)
            p["on_hold_expire_duration"] = max(86400, cur + int(add_days) * 86400)
        else:
            exp = int(u.get("expire") or 0)
            if exp > 0:                  # منقضی‌شده از همین الان حساب می‌شود، فعال از تاریخ انقضای فعلی
                p["expire"] = max(exp, int(time.time())) + int(add_days) * 86400
    elif days is not None:
        if u.get("status") == "on_hold":
            p["on_hold_expire_duration"] = days * 86400 if days > 0 else 30 * 86400
        else:
            p["expire"] = int(time.time()) + days * 86400 if days > 0 else 0
    if not p:
        return u
    if u.get("status") in ("expired", "limited"):
        p["status"] = "active"
    res = await api.request("PUT", api.upath(username), json=p)
    if "expire" in p or "on_hold_expire_duration" in p:
        res = await rebase_plans(api, username, u) or res
    return res


async def apply_user_save(api, username: str, gb=None, days=None, service_id=None, note=None):
    """ذخیره‌ی یک‌جای تغییرات ویرایش. فقط مقدارهایی که None نیستند اعمال می‌شوند.
    gb = حجم کل جدید (۰=نامحدود) ، days = روز از «الان» (۰=نامحدود؛ برای On-Hold مدت پس از اولین اتصال)."""
    ph = api.upath(username)
    u = await api.request("GET", ph)
    p: dict = {}
    if gb is not None:
        p["data_limit"] = int(gb * GB) if gb > 0 else 0
    if days is not None:
        if u.get("status") == "on_hold":
            p["on_hold_expire_duration"] = days * 86400 if days > 0 else 30 * 86400
        else:
            p["expire"] = int(time.time()) + days * 86400 if days > 0 else 0
    if service_id is not None and service_id != u.get("service_id"):
        p["service_id"] = int(service_id)
    if note is not None and note != (u.get("note") or ""):
        p["note"] = note[:500]
    if not p:
        return u
    if ("data_limit" in p or "expire" in p) and u.get("status") in ("expired", "limited"):
        p["status"] = "active"
    res = await api.request("PUT", ph, json=p)
    if "expire" in p or "on_hold_expire_duration" in p:
        res = await rebase_plans(api, username, u) or res
    if "service_id" in p:
        got = (res if isinstance(res, dict) and "service_id" in res else await api.request("GET", ph)).get("service_id")
        if got is not None and int(got) != p["service_id"]:
            raise RebeccaError("پنل تغییر سرویس را اعمال نکرد (سرویس همچنان قبلی است).")
    return res


_PLAN_DROP = ("id", "user_id")                      # فیلدهایی که هنگام نوشتن نباید فرستاده شوند
_PLAN_DEFAULTS = {"add_remaining_traffic": False, "fire_on_either": True, "increase_data_limit": False, "start_on_first_connect": False}


def _raw_plans(u: dict) -> list[dict]:
    """فهرست خام پلن‌های بعدی کاربر؛ ربکا ≥0.2 «next_plans» (لیست) دارد، نسخه‌های قدیمی «next_plan» (تکی)."""
    pl = u.get("next_plans")
    if isinstance(pl, list):
        return [x for x in pl if isinstance(x, dict)]
    old = u.get("next_plan")
    return [old] if isinstance(old, dict) and (old.get("data_limit") or old.get("expire")) else []


def _is_dur(p: dict, legacy: bool = False) -> bool:
    """expire این پلن «مدت به ثانیه» است (نه تاریخ مطلق)؟ — نسخه‌ی قدیمی یا شروع از اولین اتصال."""
    return bool(legacy or (p.get("start_on_first_connect") and 0 < int(p.get("expire") or 0) < 10 ** 9))


def _is_broken(p: dict, legacy: bool = False) -> bool:
    """پلن تاریخ‌مطلق با expire کوچک (مثل 2592000 = «۳۰ روز» که اشتباهاً به‌جای تاریخ ذخیره شده)."""
    ex = int(p.get("expire") or 0)
    return (not _is_dur(p, legacy)) and 0 < ex < 10 ** 9


def _plan_base(u: dict) -> float:
    """نقطه‌ی شروع زنجیره‌ی پلن‌ها: انقضای فعلی (اگر در آینده است) وگرنه همین الان."""
    now = time.time()
    if u.get("status") == "on_hold":
        return now + int(u.get("on_hold_expire_duration") or 0)
    exp = int(u.get("expire") or 0)
    return max(float(exp), now) if exp else now


def next_plans_view(u: dict, mode: str | None = None) -> list[dict]:
    """پلن‌های بعدی برای نمایش: [{idx, gb, days, add_remaining}] به ترتیب اجرا.
    در ربکا، expire پلن یک «تاریخ مطلق» است و خودِ پنل «روز» را برابر (expire − همین الان) نشان می‌دهد.
    mode=duration (پیش‌فرض): روز = فاصله‌ی expire تا همین الان (دقیقاً مثل پنل) | chain: فاصله تا پلن قبلی / انقضای فعلی."""
    mode = mode if mode in ("duration", "chain") else load_config()["next_plan_mode"]
    legacy = "next_plans" not in u
    now, base, out = time.time(), _plan_base(u), []
    for i, p in enumerate(_raw_plans(u)):
        dl, ex = int(p.get("data_limit") or 0), int(p.get("expire") or 0)
        if _is_dur(p, legacy) or _is_broken(p, legacy):
            days = ex // 86400
        elif ex:
            days = max(0, round((ex - (now if mode == "duration" else base)) / 86400))
            base = float(ex)
        else:
            days = 0
        out.append({"idx": i, "gb": round(dl / GB, 2), "days": days, "add_remaining": bool(p.get("add_remaining_traffic")),
                    "broken": _is_dur(p, legacy) or _is_broken(p, legacy)})
    return out


async def set_next_plans(api, username: str, plans: list[dict], mode: str | None = None):
    """جایگزینی کامل فهرست «پلن بعدی» (شارژ خودکار). هر پلن: {idx|None, gb, days, add_remaining}.
    idx = شماره‌ی پلن موجود. پلنِ بدون تغییر دست‌نخورده می‌ماند؛ duration: همین الان + N روز (پنل همان N را نشان می‌دهد) | chain: پایان پلن قبلی/انقضای فعلی + N.
    بعد از نوشتن دوباره می‌خواند تا مطمئن شود پنل واقعاً پذیرفته است."""
    ph = api.upath(username)
    u = await api.request("GET", ph)
    mode = mode if mode in ("duration", "chain") else load_config()["next_plan_mode"]
    # ربکا هنگام اجرای پلن، expire را مستقیماً روی کاربر می‌نویسد (تاریخ مطلق) و start_on_first_connect فقط شرط اجراست، نه «مدت».
    # پس duration = «از همین الان + N روز» (پنل هم همان N را نشان می‌دهد) | chain = بعد از پایان اشتراک فعلی + N روز
    raw, view = _raw_plans(u), next_plans_view(u, mode)
    legacy = "next_plans" not in u and "next_plan" in u          # ربکای قدیمی
    now = time.time()
    base, built = (now if mode == "duration" else _plan_base(u)), []
    for i, pl in enumerate(plans):
        gb, days, rem = float(pl.get("gb") or 0), int(pl.get("days") or 0), bool(pl.get("add_remaining"))
        if gb <= 0 and days <= 0:
            raise RebeccaError("حجم یا مدت پلن بعدی را وارد کنید.")
        idx = pl.get("idx")
        known = isinstance(idx, int) and 0 <= idx < len(raw)
        same = (known and not view[idx]["broken"] and abs(view[idx]["gb"] - gb) < 0.01
                and view[idx]["days"] == days and view[idx]["add_remaining"] == rem)
        if same:
            q = dict(raw[idx])                                    # بدون تغییر: همان‌طور که هست
            if mode == "chain" and not _is_dur(q, legacy) and int(q.get("expire") or 0):
                base = float(q["expire"])
        else:
            q = dict(raw[idx]) if known else {**_PLAN_DEFAULTS, **{k: v for k, v in (raw[0] if raw else {}).items() if k not in ("data_limit", "expire")}}
            q["data_limit"] = int(gb * GB) if gb > 0 else 0
            q["add_remaining_traffic"] = rem
            if not legacy:
                q["start_on_first_connect"] = False               # مدت نیست؛ تاریخ مطلق است
                q["increase_data_limit"] = False                  # پنل خودش هم هیچ‌وقت این را روشن نمی‌فرستد (روشن = حجم به حجم فعلی اضافه می‌شود)
            q["expire"] = int(base) + days * 86400 if days > 0 else 0
            if days > 0 and mode == "chain":
                base = float(q["expire"])
        for k in _PLAN_DROP:
            q.pop(k, None)
        if "position" in q:
            q["position"] = i
        built.append(q)
    body = {"next_plan": built[0] if built else None} if legacy else {"next_plans": built}
    await api.request("PUT", ph, json=body)
    after = await api.request("GET", ph)
    if len(next_plans_view(after)) != len(built):
        raise RebeccaError("پنل پلن بعدی را ذخیره نکرد (نسخه‌ی ربکا یا فیلدها سازگار نیست). جزئیات فنی را بفرستید.")
    return after


async def rebase_plans(api, username: str, before: dict):
    """بعد از تغییر انقضای کاربر، تاریخ پایان پلن‌های بعدی (که تاریخ مطلق‌اند) را به‌اندازه‌ی جابه‌جایی انقضا هم جابه‌جا می‌کند
    تا بعد از تمدید/تغییر، پلن بعدی عقب‌تر از انقضای جدید نیفتد. اگر چیزی عوض نشد None برمی‌گرداند."""
    raw = _raw_plans(before)
    legacy = "next_plans" not in before
    if load_config()["next_plan_mode"] != "chain":      # duration: پلن «از همین الان + N روز» است و با تمدید جابه‌جا نمی‌شود
        return None
    if legacy or not any(not _is_dur(p) and int(p.get("expire") or 0) >= 10 ** 9 for p in raw):
        return None
    ph = api.upath(username)
    after = await api.request("GET", ph)
    delta = int(_plan_base(after) - _plan_base(before))
    if abs(delta) < 3600:
        return None
    new = []
    for i, p in enumerate(_raw_plans(after)):
        q = {k: v for k, v in p.items() if k not in _PLAN_DROP}
        if not _is_dur(q) and int(q.get("expire") or 0) >= 10 ** 9:
            q["expire"] = int(q["expire"]) + delta
        new.append(q)
    await api.request("PUT", ph, json={"next_plans": new})
    return await api.request("GET", ph)


async def set_service(api, username: str, service_id: int):
    """تغییر سرویس کاربر؛ بعد از تغییر وضعیت را می‌خواند تا مطمئن شود پنل واقعاً پذیرفته است."""
    p = api.upath(username)
    res = await api.request("PUT", p, json={"service_id": int(service_id)})
    after = res if isinstance(res, dict) and "service_id" in res else await api.request("GET", p)
    got = after.get("service_id")
    if got is not None and int(got) != int(service_id):
        raise RebeccaError("پنل تغییر سرویس را اعمال نکرد (سرویس همچنان قبلی است).")
    return after


# ───────────────────────────── برگشت تمدید / ریست اشتباهی ─────────────────────────────
_UNDO_LOCK = threading.Lock()
_UNDO_CACHE: dict = {"sig": None, "data": []}


def _undo_file() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE) or ".", "renew_undo.json")


def _undo_read() -> list[dict]:
    path = _undo_file()
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    if _UNDO_CACHE["sig"] == sig:
        return _UNDO_CACHE["data"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = [x for x in data if isinstance(x, dict) and "u" in x and "ts" in x] if isinstance(data, list) else []
    except (OSError, ValueError):
        data = []
    _UNDO_CACHE.update(sig=sig, data=data)
    return data


def _undo_write(items: list[dict]) -> None:
    path = _undo_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _UNDO_CACHE["sig"] = None


def undo_snapshot(pid: str, username: str, before: dict, action: str = "renew") -> None:
    """وضعیت کاربر را «قبل از» تمدید/ریست در فایل محلی نگه می‌دارد. هیچ درخواستی به پنل نمی‌زند و هیچ‌وقت خطا نمی‌دهد."""
    try:
        uc = load_config()["undo"]
        if not uc["enabled"] or not isinstance(before, dict):
            return
        now = time.time()
        snap = {
            "pid": str(pid or ""), "u": str(username), "ts": now, "act": action,
            "used": int(before.get("used_traffic") or 0), "limit": int(before.get("data_limit") or 0),
            "expire": int(before.get("expire") or 0), "hold": int(before.get("on_hold_expire_duration") or 0),
            "status": str(before.get("status") or ""), "legacy": "next_plans" not in before,
            "np": [{k: v for k, v in p.items() if k not in _PLAN_DROP} for p in _raw_plans(before)],
        }
        with _UNDO_LOCK:
            cut = now - uc["days"] * 86400
            items = [x for x in _undo_read() if float(x.get("ts", 0)) >= cut] + [snap]
            _undo_write(items[-uc["keep"]:])
    except Exception:  # noqa: BLE001
        log.exception("undo_snapshot")


def undo_latest(pid: str, username: str) -> dict | None:
    """آخرین نقطه‌ی برگشت این کاربر (یا None)."""
    try:
        cut = time.time() - load_config()["undo"]["days"] * 86400
        for x in reversed(_undo_read()):
            if x.get("u") == username and str(x.get("pid", "")) == str(pid or "") and float(x.get("ts", 0)) >= cut:
                return x
    except Exception:  # noqa: BLE001
        pass
    return None


def undo_drop(snap: dict) -> None:
    with _UNDO_LOCK:
        _undo_write([x for x in _undo_read() if not (x.get("u") == snap.get("u") and x.get("ts") == snap.get("ts") and x.get("pid") == snap.get("pid"))])


async def undo_restore(api, username: str) -> tuple[dict, dict, list[str]]:
    """آخرین تمدید/ریست کاربر را برمی‌گرداند: (کاربر بعد از برگشت، نقطه‌ی برگشت، هشدارها).
    مدت/انقضا، وضعیت و پلن‌های بعدی دقیقاً به حالت قبل برمی‌گردند. پنل اجازه‌ی نوشتن «مصرف» را نمی‌دهد؛ پس حجم کل طوری تنظیم می‌شود
    که «حجم باقی‌مانده» همان قبل از تمدید شود (حجم کل = باقی‌مانده‌ی قبلی + مصرف فعلی). فقط ۲–۴ درخواست سبک، آن هم فقط وقتی دکمه زده شود."""
    snap = undo_latest(getattr(api, "pid", ""), username)
    if not snap:
        raise RebeccaError("نقطه‌ی برگشتی برای این کاربر نیست (تمدید قبل از فعال شدن این قابلیت بوده یا قدیمی‌تر از مدت نگه‌داری است).")
    p = api.upath(username)
    cur = await api.request("GET", p)
    warns: list[str] = []
    lim, used = int(snap["limit"]), int(snap["used"])
    body: dict = {"data_limit": (max(0, lim - used) + int(cur.get("used_traffic") or 0)) if lim > 0 else 0}
    st = snap.get("status")
    if st == "on_hold":
        body.update(status="on_hold", on_hold_expire_duration=int(snap["hold"]) or 30 * 86400, expire=0)
    elif st in ("active", "disabled"):
        body.update(status=st, expire=int(snap["expire"]))
    else:                                                     # limited / expired: وضعیت را خود پنل از روی حجم و تاریخ تعیین می‌کند
        body["expire"] = int(snap["expire"])
    await api.request("PUT", p, json=body)
    want = snap.get("np") or []
    have = [{k: v for k, v in x.items() if k not in _PLAN_DROP} for x in _raw_plans(cur)]
    if want != have:
        try:
            put = {"next_plan": want[0] if want else None} if snap.get("legacy") else {"next_plans": want}
            await api.request("PUT", p, json=put)
        except Exception as e:  # noqa: BLE001
            warns.append(f"پلن‌های بعدی برنگشتند: {e}")
    after = await api.request("GET", p)
    await asyncio.to_thread(undo_drop, snap)
    return after, snap, warns


async def renew_user(api, username: str, tpl: dict, reset_usage: bool = True):
    """حجم/مدت جدید طبق تمپلت. با reset_usage مصرف صفر می‌شود (ابتدا ریست، بعد وضعیت تا On-Hold خراب نشود)."""
    p = api.upath(username)
    before = await api.request("GET", p)
    await asyncio.to_thread(undo_snapshot, getattr(api, "pid", ""), username, before, "renew" if reset_usage else "renew-keep")
    if reset_usage:
        await api.request("POST", p + "/reset")
    res = await api.request("PUT", p, json=build_renew_payload(tpl))
    return await rebase_plans(api, username, before) or res


async def set_note(api, username: str, note: str):
    return await api.request("PUT", api.upath(username), json={"note": note[:500]})


BULK_ACTIONS = {
    "service": "🔀 تغییر سرویس", "renew": "⚡ تمدید با تمپلت", "reset": "🔄 صفر کردن مصرف",
    "disable": "⛔ غیرفعال‌سازی", "enable": "✅ فعال‌سازی", "delete": "🗑 حذف",
}


async def bulk_apply(api, names, action: str, *, service_id=None, tpl=None, concurrency=5, on_progress=None):
    """اجرای یک عملیات روی چند کاربر. خروجی: (موفق‌ها، [(نام، دلیل)])"""
    if action not in BULK_ACTIONS:
        raise ValueError("عملیات ناشناخته")
    sem = asyncio.Semaphore(concurrency)
    ok, bad, done = [], {}, 0

    async def one(n):
        nonlocal done
        async with sem:
            p = api.upath(n)
            try:
                if action == "service":
                    await set_service(api, n, service_id)
                elif action == "renew":
                    await renew_user(api, n, tpl)
                elif action == "reset":
                    if len(names) <= load_config()["undo"]["bulk_max"]:      # ریست دسته‌ای بزرگ: عکس‌برداری نمی‌کنیم تا پنل سنگین نشود
                        await asyncio.to_thread(undo_snapshot, getattr(api, "pid", ""), n, await api.request("GET", p), "reset")
                    await api.request("POST", p + "/reset")
                elif action in ("disable", "enable"):
                    await api.request("PUT", p, json={"status": "disabled" if action == "disable" else "active"})
                else:
                    await api.request("DELETE", p)
                ok.append(n)
            except RebeccaError as e:
                bad[n] = "پیدا نشد" if e.status == 404 else str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("bulk %s %s", action, n)
                bad[n] = f"خطای غیرمنتظره: {e.__class__.__name__}"
            done += 1
            if on_progress:
                try:
                    await on_progress(done, len(names))
                except Exception:  # noqa: BLE001
                    pass

    await asyncio.gather(*(one(n) for n in names))
    return [n for n in names if n in ok], [(n, bad[n]) for n in names if n in bad]


async def delete_users(api, usernames, concurrency=10):
    sem = asyncio.Semaphore(concurrency)
    n_ok = 0

    async def d(u):
        nonlocal n_ok
        async with sem:
            try:
                await api.request("DELETE", api.upath(u))
                n_ok += 1
            except RebeccaError:
                pass

    await asyncio.gather(*(d(u) for u in usernames))
    return n_ok


# ───────────────────── ارسال نتایج به تلگرام (QR دسته‌ای) ─────────────────────
async def _retry(fn):
    from telegram.error import RetryAfter

    for _ in range(4):
        try:
            return await fn()
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
    return await fn()


def build_info(panel: str, tpl: dict, service: str) -> dict:
    """مشخصات ساخت برای پیام تلگرام."""
    return {"panel": panel, "gb": tpl["gb"], "days": tpl["days"], "mode": tpl.get("mode", "on_hold"), "service": str(service)}


def _info_lines(info) -> list[str]:
    if isinstance(info, str):            # سازگاری با فراخوانی قدیمی
        return [esc(info)]
    gb = "نامحدود" if not info["gb"] else f"{info['gb']} گیگابایت"
    days = "نامحدود" if not info["days"] else f"{info['days']} روز"
    start = "از اولین اتصال" if info["mode"] == "on_hold" else "از همین حالا"
    rows = [("🖥", "پنل", info["panel"]), ("📂", "سرویس", info["service"]), ("💾", "حجم", gb), ("📅", "اعتبار", days), ("⏳", "شروع", start)]
    rows = [r for r in rows if r[2]]
    return [f"{'└' if i == len(rows) - 1 else '├'} {ic} {lb}: <b>{esc(v)}</b>" for i, (ic, lb, v) in enumerate(rows)]


async def send_results(bot, chat_id, results: list[dict], header, max_qr: int = 100, send_qr: bool = True):
    """QR هر کاربر را با مشخصات مرتب و لینک قابل‌کپی جداگانه می‌فرستد + فایل متنی لینک‌ها."""
    from telegram.constants import ParseMode
    from telegram.error import TelegramError

    lines = _info_lines(header)

    def caption(r):
        return (f"✅ <b>{esc(r['username'])}</b>\n\n" + "\n".join(lines)
                + f"\n\n🔗 <b>لینک اشتراک</b> (لمس کنید تا کپی شود):\n<code>{esc(r['sub'])}</code>")

    if len(results) > 1:   # خلاصه‌ی ساخت گروهی قبل از QRها
        rng = f"{results[0]['username']} … {results[-1]['username']}"
        await _retry(lambda: bot.send_message(
            chat_id, f"📦 <b>{len(results)} اکانت ساخته شد</b>\n<code>{esc(rng)}</code>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML))

    with_qr = [r for r in results if r["sub"]][:max_qr]
    pngs = await asyncio.gather(*(asyncio.to_thread(make_qr_png, r["sub"]) for r in with_qr)) if send_qr else [None] * len(with_qr)
    # لینک همیشه ارسال می‌شود؛ تصویر QR فقط وقتی send_qr روشن است. هر اشتراک جداگانه و پشت‌سرهم (به ترتیب ساخت)
    for r, png in zip(with_qr, pngs):
        if png is not None:
            try:
                await _retry(lambda: bot.send_photo(chat_id, png, caption=caption(r), parse_mode=ParseMode.HTML))
            except TelegramError as e:
                log.warning("send_photo failed for %s: %s", r["username"], e)
                await _retry(lambda: bot.send_message(chat_id, caption(r), parse_mode=ParseMode.HTML))
        else:
            await _retry(lambda: bot.send_message(chat_id, caption(r), parse_mode=ParseMode.HTML, disable_web_page_preview=True))
        await asyncio.sleep(0.5)  # رعایت محدودیت نرخ تلگرام
    if len(results) > 1 or len(results) > len(with_qr):
        txt = "\n".join(f"{r['username']} | {r['sub']}" for r in results)
        bio = io.BytesIO(txt.encode("utf-8"))
        bio.name = f"{results[0]['username']}_to_{results[-1]['username']}.txt"
        await _retry(lambda: bot.send_document(chat_id, bio, caption=f"📂 فهرست کامل ({len(results)} کاربر)"))


# ───────────────────────────── CLI نصب ─────────────────────────────
def _cli(argv):
    import argparse

    p = argparse.ArgumentParser(prog="common.py")
    sp = p.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("init")
    i.add_argument("--token", default="")
    i.add_argument("--admin", default="")
    i.add_argument("--port", type=int, default=0)
    i.add_argument("--user", default="")
    w = sp.add_parser("passwd")
    w.add_argument("--password", default="")
    s_ = sp.add_parser("ssl")
    s_.add_argument("action", choices=["off", "status"])
    a = p.parse_args(argv)

    cfg = load_config()
    out = {}
    if a.cmd == "ssl":
        if a.action == "off":
            cfg["ssl"]["enabled"] = False
            save_config(cfg)
            print("SSL=off")
        else:
            ss = cfg["ssl"]
            print(f"SSL={'on' if ss['enabled'] else 'off'}\nCERT={ss['cert']}\nKEY={ss['key']}\nERROR={ss['error']}")
        return
    # رمز از متغیر محیطی خوانده می‌شود تا در لیست پروسه‌ها (ps) دیده نشود
    env_pw = os.environ.get("REBECCA_INIT_PASSWORD", "")
    if a.cmd == "init":
        if a.token:
            cfg["bot_token"] = a.token.strip()
        if a.admin:
            ids = [int(x) for x in re.split(r"[,\s]+", a.admin.strip()) if x.lstrip("-").isdigit()]
            if ids:
                cfg["admin_ids"] = ids
        if a.port:
            if not 1 <= a.port <= 65535:
                sys.exit("پورت باید بین 1 تا 65535 باشد.")
            cfg["web_port"] = a.port
        if a.user:
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,32}", a.user):
                sys.exit("نام کاربری فقط حروف انگلیسی/عدد/_.-@ و ۳ تا ۳۲ کاراکتر.")
            cfg["web"]["user"] = a.user
        if env_pw or not cfg["web"].get("hash"):
            if env_pw and len(env_pw) < 8:
                sys.exit("رمز باید حداقل ۸ کاراکتر باشد.")
            pw = env_pw or secrets.token_urlsafe(12)
            cfg["web"].update(hash_password(pw))
            cfg["web"]["secret"] = secrets.token_hex(32)
            if not cfg["web"].get("user"):
                cfg["web"]["user"] = "admin"
            if not env_pw:
                out["WEB_PASSWORD"] = pw
    else:
        pw = a.password or env_pw or secrets.token_urlsafe(12)
        cfg["web"].update(hash_password(pw))
        cfg["web"]["secret"] = secrets.token_hex(32)
        out["WEB_PASSWORD"] = pw
    save_config(cfg)
    out.update(WEB_USER=cfg["web"].get("user", "admin"), WEB_PORT=cfg["web_port"])
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    _cli(sys.argv[1:])
