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
import pickle
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

try:                                  # قفل فایل بین پروسه‌ها (ربات و وب‌پنل دو سرویس جدا هستند)
    import fcntl
except ImportError:                   # ویندوز
    fcntl = None
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
WALLET_TX_MAX = 5000
REP_SALES_MAX = 50000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")

DEFAULT_TEMPLATES = {
    "پیش‌فرض (30 گیگ - 30 روز)": {"gb": 30, "days": 30, "mode": "on_hold", "kind": "single"},
    "تست (5 گیگ - 3 روز)": {"gb": 5, "days": 3, "mode": "active", "kind": "single"},
}
AUDIT_MAX = 3000          # سقف رویدادهای لاگ فعالیت (قدیمی‌ترها حذف می‌شوند)
AUDIT_CATS = ("wallet", "rep", "user", "del", "sys")
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
    "backup": {"enabled": True, "interval_min": 1440, "password": "", "live": False, "live_chat": "", "bot_token": ""},   # interval_min: ۱۴۴۰ = روزانه ساعت ۱۲:۰۰؛ کمتر از آن = هر N دقیقه (فقط وقتی چیزی عوض شده)
    "monitor": {"enabled": True, "interval": 300},
    "smart_alerts": {"low_rep_wallet": True, "panel_down": True, "backup_failed": True, "topup_request": True, "low_wallet_threshold": 100000},
    "audit_log": [],
    "reminders": {"enabled": True, "hour": 10, "days": 3, "low_pct": 10},
    "unpaid": {"hours": 24, "pending": {}},   # تمدیدهای «واریز نشده»: pending = {panel_id: {username: {"due": ts, "set": ts}}}
    "web": {"user": "admin", "salt": "", "hash": "", "secret": "", "totp": "", "totp_pending": "", "totp_last": 0, "recovery": [], "known_ips": []},
    "security": {"wallet_code_threshold": 2000000},   # شارژ/کسر/تأییدِ بالاتر از این مبلغ (تومان) با ورود دومرحله‌ای، کد 2FA جداگانه می‌خواهد
    "login_log": [],     # تلاش‌های ورود (موفق/ناموفق) با IP: [{ts, ip, user, ok, why}]
    "reps": {},          # نمایندگان: {id: {name, label, salt, hash, blocked, created}}
    "rep_users": {},     # کاربرهای هر نماینده: {panel_id: {username: rep_id}}
    "rep_paid": {},      # کاربرهایی که نماینده «پرداخت شد» زده: {panel_id: {username: timestamp}}
    "wallet_income": [],  # شارژهای کیف پول نماینده‌ها (تأیید درخواست یا شارژ دستی ادمین) که جزو فروش همان روز حساب می‌شوند: [{ts, rid, amount}]
    "wallet_tx": [],     # دفتر تراکنش‌های کیف پول برای جلوگیری از ثبت/کسر تکراری: [{id, ts, rid, kind, amount, balance, state, user}]
    "rep_sales": [],     # خرید/تمدید نماینده‌ها با کیف پول (برای «فروش روزانه» در پنل نماینده؛ بازگشت وجه با مبلغ منفی): [{ts, rid, amount}]
    "topups": [],        # درخواست‌های افزایش موجودی نماینده‌ها: [{id, rid, amount, note, ts, status: pending|approved|rejected, done}]
    "rep_notes": {},     # اسم/یادداشت نماینده برای هر کاربرش: {panel_id: {username: note}}
    # فروشگاه (ربات فروش مشتری): توکن ربات جدا، کارت، پلن‌ها، مشتری‌ها با کیف پول، سفارش‌ها (خرید/تمدید/شارژ با رسید) و دفتر تراکنش
    "shop": {"enabled": True, "token": "", "panel": "", "card": {"number": "", "holder": "", "bank": ""}, "plans": [], "welcome": "", "support": "",
             "min_topup": 10000, "max_topup": 50000000, "receipt_hours": 24, "customers": {}, "orders": [], "tx": [],
             "test": {"enabled": False, "template": "", "prefix": "test", "hours": 24}, "cards": [],
             "force": {"enabled": False, "channels": []}},      # عضویت اجباری: channels=[{chat: "@name"|"-100…", link: "https://t.me/…"}]      # تست رایگان: هر مشتری فقط یک‌بار
    "ssl": {"enabled": False, "cert": "", "key": "", "domain": "", "error": ""},
}


# ───────────────────────────── کانفیگ ─────────────────────────────
_lock = threading.RLock()
_cache = {"sig": None, "data": None}
_flock_depth = 0


class _CfgLock:
    """قفل هم‌زمان برای نخ‌ها (RLock) و پروسه‌ها (flock). بدون آن، ربات و وب‌پنل می‌توانستند هم‌زمان config.json را بخوانند-تغییر دهند-بنویسند
    و یکی تغییر دیگری (مثلاً موجودی کیف پول) را ناخواسته برگردانَد. قفل تودرتو (re-entrant) است."""

    def __enter__(self):
        global _flock_depth
        _lock.acquire()
        try:
            if _flock_depth == 0 and fcntl is not None:
                self.fh = open(CONFIG_FILE + ".lock", "a+")
                try:
                    os.chmod(CONFIG_FILE + ".lock", 0o600)
                except OSError:
                    pass
                fcntl.flock(self.fh, fcntl.LOCK_EX)
                _cache["data"] = None          # شاید پروسه‌ی دیگر چیزی نوشته باشد؛ حتماً از دیسک بخوان
            _flock_depth += 1
        except BaseException:
            _lock.release()
            raise
        return self

    def __exit__(self, *exc):
        global _flock_depth
        try:
            _flock_depth -= 1
            if _flock_depth == 0 and fcntl is not None:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
                finally:
                    self.fh.close()
        finally:
            _lock.release()
        return False


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


SHOP_ORDER_MAX = 3000
SHOP_TX_MAX = 5000
SHOP_LOG_MAX = 50
SHOP_BAL_MAX = 10 ** 10
SHOP_CARDS_MAX = 5


def parse_force_channels(text: str) -> list[dict]:
    """هر خط یک کانال: «@name» یا «https://t.me/name» یا «-100123… لینک‌دعوت» (برای کانال خصوصی). خطای قالب: ValueError."""
    out = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        first, link = parts[0], (parts[1] if len(parts) > 1 else "")
        m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,64})/?", first)
        if m:
            first = "@" + m.group(1)
        elif re.fullmatch(r"[A-Za-z0-9_]{4,64}", first) and not first.isdigit():
            first = "@" + first
        if not re.fullmatch(r"@[A-Za-z0-9_]{4,64}|-100\d{5,15}", first):
            raise ValueError(f"کانال «{ln[:40]}» معتبر نیست؛ مثل @mychannel یا شناسه -100… بنویسید.")
        if first.startswith("-") and not link:
            raise ValueError("برای کانال خصوصی (شناسه -100…) لینک دعوت را بعد از شناسه بنویسید.")
        if link and not re.fullmatch(r"https://t\.me/\S+", link):
            raise ValueError("لینک دعوت باید با https://t.me/ شروع شود.")
        if all(x["chat"] != first for x in out):
            out.append({"chat": first, "link": link})
    if len(out) > 5:
        raise ValueError("حداکثر ۵ کانال.")
    return out


def _normalize_shop(raw) -> dict:
    """تنظیمات و داده‌ی فروشگاه را به ساختار معتبر برمی‌گرداند (کلیدهای ناشناخته حذف می‌شوند)."""
    s = copy.deepcopy(DEFAULT_CFG["shop"])
    raw = raw if isinstance(raw, dict) else {}
    s["enabled"] = bool(raw.get("enabled", True))
    s["token"] = str(raw.get("token") or "").strip()
    s["panel"] = str(raw.get("panel") or "").strip()[:40]
    card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
    s["card"] = {k: str(card.get(k) or "").strip()[:60] for k in ("number", "holder", "bank")}
    cards = []
    for x in (raw.get("cards") if isinstance(raw.get("cards"), list) else [])[:SHOP_CARDS_MAX]:
        if isinstance(x, dict):
            num = re.sub(r"\D", "", str(x.get("number") or ""))[:24]
            if num:
                cards.append({"number": num, "holder": str(x.get("holder") or "").strip()[:60], "bank": str(x.get("bank") or "").strip()[:60]})
    if not cards and "cards" not in raw and s["card"]["number"]:          # مهاجرت از نسخه‌ی تک‌کارت
        cards = [dict(s["card"])]
    s["cards"] = cards
    s["card"] = dict(cards[0]) if cards else {"number": "", "holder": "", "bank": ""}          # سازگاری: «کارت اول»
    for k in ("welcome", "support"):
        s[k] = str(raw.get(k) or "").strip()[:1000]
    for k, lo, hi in (("min_topup", 1000, 10 ** 9), ("max_topup", 1000, 10 ** 10), ("receipt_hours", 1, 720)):
        try:
            s[k] = min(hi, max(lo, int(raw.get(k, s[k]))))
        except (TypeError, ValueError):
            pass
    t = raw.get("test") if isinstance(raw.get("test"), dict) else {}
    pre = re.sub(r"[^A-Za-z0-9_]", "", str(t.get("prefix") or "test"))[:16] or "test"
    try:
        th = min(720, max(1, int(t.get("hours", 24))))
    except (TypeError, ValueError):
        th = 24
    s["test"] = {"enabled": bool(t.get("enabled")), "template": str(t.get("template") or "")[:80], "prefix": pre, "hours": th}
    f = raw.get("force") if isinstance(raw.get("force"), dict) else {}
    chans = []
    for x in (f.get("channels") if isinstance(f.get("channels"), list) else [])[:5]:
        if isinstance(x, dict) and re.fullmatch(r"@[A-Za-z0-9_]{4,64}|-100\d{5,15}", str(x.get("chat") or "")):
            chans.append({"chat": str(x["chat"]), "link": str(x.get("link") or "")[:200]})
    s["force"] = {"enabled": bool(f.get("enabled")) and bool(chans), "channels": chans}
    plans, seen = [], set()
    for p in raw.get("plans") if isinstance(raw.get("plans"), list) else []:
        try:
            pid = str(p["id"]).strip()[:12]
            if not pid or pid in seen or not str(p.get("template") or ""):
                continue
            seen.add(pid)
            plans.append({"id": pid, "name": str(p.get("name") or p["template"]).strip()[:60], "template": str(p["template"]),
                          "price": max(0, int(p.get("price") or 0)), "active": bool(p.get("active", True))})
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    s["plans"] = plans
    cust = {}
    for uid, r in (raw.get("customers") if isinstance(raw.get("customers"), dict) else {}).items():
        if not isinstance(r, dict) or not str(uid).lstrip("-").isdigit():
            continue
        try:
            svcs = [{"pid": str(x.get("pid") or ""), "u": str(x["u"])[:64], "ts": int(x.get("ts") or 0)}
                    for x in (r.get("services") if isinstance(r.get("services"), list) else []) if isinstance(x, dict) and x.get("u")]
            cust[str(uid)] = {"balance": min(SHOP_BAL_MAX, max(0, int(r.get("balance") or 0))), "name": str(r.get("name") or "")[:60],
                              "username": str(r.get("username") or "")[:40], "joined": int(r.get("joined") or 0), "banned": bool(r.get("banned")),
                              "test": int(r.get("test") or 0), "test_user": str(r.get("test_user") or "")[:64],
                              "test_exp": int(r.get("test_exp") or 0), "test_pid": str(r.get("test_pid") or "")[:40], "test_del": int(r.get("test_del") or 0),
                              "services": svcs, "log": [x for x in (r.get("log") if isinstance(r.get("log"), list) else []) if isinstance(x, dict)][-SHOP_LOG_MAX:]}
        except (TypeError, ValueError):
            continue
    s["customers"] = cust
    s["orders"] = [o for o in (raw.get("orders") if isinstance(raw.get("orders"), list) else []) if isinstance(o, dict) and o.get("id")][-SHOP_ORDER_MAX:]
    s["tx"] = [t for t in (raw.get("tx") if isinstance(raw.get("tx"), list) else []) if isinstance(t, dict) and t.get("id")][-SHOP_TX_MAX:]
    return s


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
    w_ = cfg["web"]
    w_["recovery"] = [str(x) for x in w_["recovery"]][:12] if isinstance(w_.get("recovery"), list) else []
    w_["known_ips"] = [str(x)[:64] for x in w_["known_ips"]][-30:] if isinstance(w_.get("known_ips"), list) else []
    w_["totp"] = str(w_.get("totp") or "")
    w_["totp_pending"] = str(w_.get("totp_pending") or "")
    try:
        w_["totp_last"] = int(w_.get("totp_last") or 0)
    except (TypeError, ValueError):
        w_["totp_last"] = 0
    sec = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
    try:
        thr = max(0, int(sec.get("wallet_code_threshold", 2000000)))
    except (TypeError, ValueError):
        thr = 2000000
    cfg["security"] = {"wallet_code_threshold": thr}
    cfg["login_log"] = [{"ts": float(x.get("ts") or 0), "ip": str(x.get("ip") or "")[:64], "user": str(x.get("user") or "")[:60], "ok": bool(x.get("ok")), "why": str(x.get("why") or "")[:60]}
                        for x in (cfg.get("login_log") if isinstance(cfg.get("login_log"), list) else []) if isinstance(x, dict)][-300:]
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
    alerts = copy.deepcopy(DEFAULT_CFG["smart_alerts"])
    if isinstance(cfg.get("smart_alerts"), dict):
        alerts.update({k: v for k, v in cfg["smart_alerts"].items() if k in alerts})
    for k in ("low_rep_wallet", "panel_down", "backup_failed", "topup_request"):
        alerts[k] = bool(alerts[k])
    try:
        alerts["low_wallet_threshold"] = max(0, int(alerts["low_wallet_threshold"]))
    except (TypeError, ValueError):
        alerts["low_wallet_threshold"] = DEFAULT_CFG["smart_alerts"]["low_wallet_threshold"]
    cfg["smart_alerts"] = alerts
    cfg["audit_log"] = [x for x in cfg.get("audit_log", []) if isinstance(x, dict)][-AUDIT_MAX:] if isinstance(cfg.get("audit_log"), list) else []
    for key in ("monitor", "reminders", "backup"):
        sec = copy.deepcopy(DEFAULT_CFG[key])
        if isinstance(cfg.get(key), dict):
            sec.update(cfg[key])
        for k, dv in DEFAULT_CFG[key].items():
            if isinstance(dv, bool):
                sec[k] = bool(sec[k])
            elif isinstance(dv, str):
                sec[k] = str(sec[k] or "")[:128]
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
                              "balance": int(r.get("balance") or 0), "panel": str(r.get("panel") or ""), "next_plan": bool(r.get("next_plan")),
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
            if isinstance(x, dict) and int(x.get("amount") or 0) != 0:      # منفی = کسر ادمین (اصلاح شارژ)
                wi.append({"ts": float(x["ts"]), "rid": str(x.get("rid") or ""), "amount": int(x["amount"])})
        except (TypeError, ValueError, KeyError):
            continue
    cfg["wallet_income"] = wi[-WALLET_INCOME_MAX:]
    wt = []
    for x in (cfg.get("wallet_tx") if isinstance(cfg.get("wallet_tx"), list) else []):
        try:
            if isinstance(x, dict) and str(x.get("id") or ""):
                wt.append({"id": str(x["id"])[:80], "ts": float(x.get("ts") or 0), "rid": str(x.get("rid") or ""), "kind": str(x.get("kind") or ""),
                           "amount": int(x.get("amount") or 0), "balance": int(x.get("balance") or 0),
                           "state": str(x.get("state") or "done")[:12], "user": str(x.get("user") or "")[:128], "alerted": bool(x.get("alerted"))})
        except (TypeError, ValueError):
            continue
    cfg["wallet_tx"] = wt[-WALLET_TX_MAX:]
    rs = []
    for x in (cfg.get("rep_sales") if isinstance(cfg.get("rep_sales"), list) else []):
        try:
            if isinstance(x, dict) and int(x.get("amount") or 0) != 0:
                rec = {"ts": float(x["ts"]), "rid": str(x.get("rid") or ""), "amount": int(x["amount"])}
                # Keep sale metadata so the representative dashboard can group
                # successful creations/renewals by GB, duration and template.
                meta = x.get("meta")
                if isinstance(meta, dict):
                    action = str(meta.get("action") or "new")
                    if action not in ("new", "renew", "next"):
                        action = "new"
                    try:
                        gb = max(0, int(meta.get("gb") or 0))
                        days = max(0, int(meta.get("days") or 0))
                    except (TypeError, ValueError):
                        gb, days = 0, 0
                    clean_meta = {"action": action, "gb": gb, "days": days}
                    if meta.get("template") is not None:
                        clean_meta["template"] = str(meta.get("template"))[:80]
                    if meta.get("username") is not None:
                        clean_meta["username"] = str(meta.get("username"))[:128]
                    rec["meta"] = clean_meta
                rs.append(rec)
        except (TypeError, ValueError, KeyError):
            continue
    cfg["rep_sales"] = rs[-REP_SALES_MAX:]
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
    cfg["shop"] = _normalize_shop(cfg.get("shop"))
    for k in ("max_batch", "max_qr", "web_port"):
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CFG[k]
    return cfg


# لیست‌های سنگینِ فقط‌افزودنی: عنصرهایشان هیچ‌جا درجا تغییر نمی‌کند (فقط append یا جایگزینی کل لیست)، پس در کپی فقط خودِ لیست
# کپی می‌شود. کپی عمیقِ ده‌ها هزار رکورد در هر درخواست باعث می‌شد وب‌پنل (به‌خصوص صفحه‌ی تنظیمات و لیست کاربران) کند شود.
_HEAVY_LISTS = ("rep_sales", "wallet_income", "audit_log", "login_log")


def _copy_cfg(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k in _HEAVY_LISTS and isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = pickle.loads(pickle.dumps(v, pickle.HIGHEST_PROTOCOL))      # چندین برابر سریع‌تر از copy.deepcopy روی داده‌ی JSON
    return out


def _cached_cfg() -> dict:
    """کش را (در صورت تغییر فایل) تازه می‌کند و خودِ شیء کش را برمی‌گرداند (بدون کپی)."""
    # مسیر سریع بدون قفل: اگر فایل عوض نشده، خواندن منتظر نویسنده‌ای که وسط fsync است نمی‌ماند (علت کندی صفحه‌ی فروشگاه/وب‌پنل
    # وقتی ربات هم‌زمان می‌نویسد). نویسنده روی یک کپی کار می‌کند و کش را فقط بعد از جایگزینی فایل باطل می‌کند، پس دیدن نسخه‌ی قبلی تا آن لحظه درست است.
    data = _cache["data"]
    if data is not None:
        try:
            st = os.stat(CONFIG_FILE)
            if _cache["sig"] == (st.st_mtime_ns, st.st_size):
                return data
        except OSError:
            if _cache["sig"] is None:
                return data
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
        return _cache["data"]


def load_config_ro() -> dict:
    """نسخه‌ی فقط‌خواندنی (بدون کپی). نتیجه (و زیرمجموعه‌هایش) را هرگز تغییر ندهید؛ فقط برای خواندن یا ساختن پاسخ."""
    return _cached_cfg()


def load_config() -> dict:
    """کانفیگ را می‌خواند؛ تا وقتی فایل تغییر نکرده از کش استفاده می‌کند (بدون I/O اضافه). خروجی کپیِ قابل‌تغییر است."""
    with _lock:
        return _copy_cfg(_cached_cfg())


def save_config(cfg: dict) -> None:
    """ذخیره اتمیک (نوشتن در فایل موقت و جایگزینی) با دسترسی 600."""
    with _CfgLock():
        d = os.path.dirname(CONFIG_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")))     # فشرده و یک‌جا: چندین برابر سریع‌تر از json.dump با indent
                f.flush()
                os.fsync(f.fileno())          # قبل از جایگزینی روی دیسک بنشیند (قطع برق وسط تراکنش مالی)
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


# ───────────────────────────── رمزنگاری بکاپ ─────────────────────────────
ENC_BACKUP_FORMAT = "rebecca-encrypted-backup"
_ENC_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


def _enc_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def encrypt_backup(data: bytes, password: str) -> bytes:
    """بکاپ را با رمز رمزنگاری می‌کند (AES-256-GCM + کلید مشتق‌شده با scrypt). خروجی یک JSON کوچک است."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _enc_key(password, salt, **_ENC_SCRYPT)
    ct = AESGCM(key).encrypt(nonce, data, ENC_BACKUP_FORMAT.encode())
    env = {"format": ENC_BACKUP_FORMAT, "version": 1, "kdf": "scrypt", **_ENC_SCRYPT,
           "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
           "data": base64.b64encode(ct).decode()}
    return json.dumps(env).encode("utf-8")


def is_encrypted_backup(text: str) -> bool:
    try:
        d = json.loads(text)
    except ValueError:
        return False
    return isinstance(d, dict) and d.get("format") == ENC_BACKUP_FORMAT


def decrypt_backup(text: str, password: str) -> bytes:
    """بکاپ رمزنگاری‌شده ← محتوای اصلی. رمز اشتباه یا فایل خراب: ValueError."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        env = json.loads(text)
        if env.get("format") != ENC_BACKUP_FORMAT or env.get("kdf") != "scrypt":
            raise ValueError
        n, r, p = int(env["n"]), int(env["r"]), int(env["p"])
        if not (2 ** 10 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
            raise ValueError
        salt, nonce, ct = (base64.b64decode(env[k]) for k in ("salt", "nonce", "data"))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("فایل بکاپ رمزنگاری‌شده خراب یا ناقص است.")
    try:
        return AESGCM(_enc_key(password, salt, n, r, p)).decrypt(nonce, ct, ENC_BACKUP_FORMAT.encode())
    except InvalidTag:
        raise ValueError("رمز بکاپ اشتباه است (یا فایل دستکاری شده).")


def update_config(mutator) -> dict:
    with _CfgLock():
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg          # همان نسخه‌ی تغییرداده‌شده؛ خواندن دوباره‌ی کل فایل (و نرمال‌سازی آن) لازم نیست


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
    return uid in load_config_ro()["admin_ids"]


# ─────────────────────── رمز وب‌پنل (PBKDF2 + کش) ───────────────────────
def hash_password(pw: str, salt: str | None = None) -> dict:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return {"salt": salt, "hash": h}


_auth_ok: dict[str, float] = {}


def web_configured() -> bool:
    return bool(load_config_ro()["web"].get("hash"))


def check_web_login(user: str, pw: str) -> bool:
    w = load_config_ro()["web"]
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


# ─────────────────────── ورود دومرحله‌ای (TOTP، RFC 6238) ───────────────────────
import base64
import hmac as _hmac
import struct


def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = _hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret: str, code: str) -> int | None:
    """شماره‌ی گامِ منطبق (با ±۱ گام تحمل ساعت) یا None."""
    code = re.sub(r"\D", "", str(code or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
    if len(code) != 6 or not secret:
        return None
    now = int(time.time()) // 30
    for st in (now, now - 1, now + 1):
        if secrets.compare_digest(_totp_at(secret, st), code):
            return st
    return None


def totp_uri(secret: str, user: str) -> str:
    return f"otpauth://totp/Rebecca:{quote(user)}?secret={secret}&issuer=Rebecca&digits=6&period=30"


def _rec_hash(code: str) -> str:
    return hashlib.sha256(re.sub(r"[^a-z0-9]", "", code.lower()).encode()).hexdigest()


def totp_enabled() -> bool:
    return bool(load_config_ro()["web"].get("totp"))


def totp_verify_use(code: str) -> bool:
    """کد ۶ رقمی یا یکی از کدهای بازیابی را می‌پذیرد. هر کد فقط یک‌بار قابل استفاده است (ضد تکرار)."""
    ok = {"v": False}

    def mut(c):
        w = c["web"]
        if not w.get("totp"):
            return
        st = totp_match(w["totp"], code)
        if st is not None and st > int(w.get("totp_last") or 0):
            w["totp_last"] = st
            ok["v"] = True
            return
        if st is None:
            h = _rec_hash(str(code or ""))
            if len(re.sub(r"[^a-z0-9]", "", str(code or "").lower())) >= 8 and h in w["recovery"]:
                w["recovery"] = [x for x in w["recovery"] if x != h]
                ok["v"] = True
    update_config(mut)
    return ok["v"]


def totp_begin() -> dict:
    """ساخت راز جدید (هنوز فعال نیست تا کاربر با یک کد درست تأیید کند)."""
    sec = totp_new_secret()
    update_config(lambda c: c["web"].__setitem__("totp_pending", sec))
    return {"secret": sec, "uri": totp_uri(sec, load_config()["web"].get("user", "admin"))}


def totp_confirm(code: str) -> list[str]:
    """با کد درست، 2FA فعال می‌شود و ۸ کد بازیابی (فقط همین یک‌بار نمایش) برمی‌گردد."""
    codes: list[str] = []

    def mut(c):
        w = c["web"]
        sec = w.get("totp_pending")
        if not sec:
            raise ValueError("ابتدا «فعال‌سازی» را بزنید.")
        st = totp_match(sec, code)
        if st is None:
            raise ValueError("کد اشتباه است؛ کد فعلیِ اپلیکیشن را وارد کنید.")
        w["totp"], w["totp_pending"], w["totp_last"] = sec, "", st
        for _ in range(8):
            codes.append(secrets.token_hex(3) + "-" + secrets.token_hex(3))
        w["recovery"] = [_rec_hash(x) for x in codes]
        w["secret"] = secrets.token_hex(32)           # نشست‌های قبلی (بدون 2FA) باطل شوند
    update_config(mut)
    return codes


def totp_disable() -> None:
    def mut(c):
        c["web"].update(totp="", totp_pending="", totp_last=0, recovery=[])
    update_config(mut)


# ─────────────────────── مرحله‌ی دوم ورود، کد برای عملیات مالی بزرگ، بازیابی امن ───────────────────────
PRE_TTL = 300


def make_pre_token(user: str, remember: bool) -> str:
    """توکن کوتاه‌مدت بین «رمز درست» و «کد 2FA». کوکی نشست نیست و فقط برای /api/login/2fa قابل استفاده است."""
    import hmac
    key = _session_key()
    if key is None:
        raise ValueError("رمز وب‌پنل تنظیم نشده است.")
    body = f"2fa|{user}|{int(bool(remember))}|{int(time.time()) + PRE_TTL}"
    return body + "|" + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def check_pre_token(tok: str) -> tuple[str, bool] | None:
    import hmac
    try:
        body, sig = str(tok).rsplit("|", 1)
        tag, user, rem, exp = body.split("|")
        key = _session_key()
        if tag != "2fa" or key is None or int(exp) < time.time():
            return None
        if hmac.compare_digest(sig, hmac.new(key, body.encode(), hashlib.sha256).hexdigest()):
            return user, rem == "1"
    except (ValueError, AttributeError):
        pass
    return None


def wallet_code_threshold() -> int:
    return int(load_config_ro()["security"].get("wallet_code_threshold", 2000000))


def code_needed_for(amount: int) -> bool:
    """عملیات مالی با مبلغِ ≥ آستانه، وقتی 2FA روشن است، کد تازه می‌خواهد. آستانه‌ی ۰ = خاموش."""
    thr = wallet_code_threshold()
    return totp_enabled() and thr > 0 and abs(int(amount)) >= thr


_WALLET_KEYS = ("wallet_tx", "wallet_income", "rep_sales", "topups", "audit_log", "login_log")


def restore_diff(cur: dict, new: dict) -> dict:
    """مقایسه‌ی وضعیت فعلی با بکاپ، قبل از بازیابی: کدام موجودی‌ها عوض می‌شود و چه تراکنش‌هایی از بین می‌رود."""
    rows = []
    for rid in set(cur["reps"]) | set(new["reps"]):
        a, b = cur["reps"].get(rid), new["reps"].get(rid)
        ref = a or b
        rows.append({"id": rid, "name": ref.get("label") or ref["name"], "now": a["balance"] if a else None, "backup": b["balance"] if b else None})
    changed = [r for r in rows if r["now"] != r["backup"]]
    changed.sort(key=lambda r: -abs((r["backup"] or 0) - (r["now"] or 0)))
    latest = 0.0
    for r in new["reps"].values():
        latest = max([latest] + [float(e.get("ts") or 0) for e in r.get("log", [])])
    for t in new.get("wallet_tx", []):
        latest = max(latest, float(t.get("ts") or 0))
    lost = sum(1 for r in cur["reps"].values() for e in r.get("log", []) if float(e.get("ts") or 0) > latest)
    return {"changed": changed[:40], "changed_n": len(changed),
            "total_now": sum(r["balance"] for r in cur["reps"].values()), "total_backup": sum(r["balance"] for r in new["reps"].values()),
            "only_now": [r["name"] for r in rows if r["backup"] is None], "only_backup": [r["name"] for r in rows if r["now"] is None],
            "lost_tx": lost, "backup_latest_ts": latest, "pending_topups_now": sum(1 for t in cur["topups"] if t.get("status") == "pending"),
            "shop_now": shop_totals(cur), "shop_backup": shop_totals(new)}


def restore_keep_wallet(new: dict, cur: dict) -> None:
    """بازیابی «فقط تنظیمات»: موجودی، تراکنش‌ها و لاگ‌های فعلی دست‌نخورده می‌مانند. نماینده‌ی فقط-در-فعلی حذف نمی‌شود،
    نماینده‌ی فقط-در-بکاپ با موجودی صفر اضافه می‌شود."""
    only_cur = set(cur["reps"]) - set(new["reps"])
    for rid, r in cur["reps"].items():
        if rid in new["reps"]:
            new["reps"][rid]["balance"], new["reps"][rid]["log"] = r["balance"], r["log"]
        else:
            new["reps"][rid] = r
    for rid, r in new["reps"].items():
        if rid not in cur["reps"]:
            r["balance"], r["log"] = 0, []
    for pid, m in cur["rep_users"].items():
        for u, rid in m.items():
            if rid in only_cur:
                new["rep_users"].setdefault(pid, {})[u] = rid
    for k in _WALLET_KEYS:
        new[k] = cur[k]
    # فروشگاه: تنظیمات از بکاپ می‌آید ولی موجودی مشتری‌ها، سفارش‌ها و دفتر تراکنش فعلی دست‌نخورده می‌ماند
    new["shop"] = {**new["shop"], "customers": cur["shop"]["customers"], "orders": cur["shop"]["orders"], "tx": cur["shop"]["tx"]}


def preserve_security(new: dict, cur: dict) -> None:
    """بازیابی هرگز ورود دومرحله‌ای را خاموش یا عوض نمی‌کند (بکاپ قدیمی ممکن است قبل از فعال‌شدن 2FA باشد)."""
    if cur["web"].get("totp"):
        for k in ("totp", "totp_pending", "totp_last", "recovery"):
            new["web"][k] = cur["web"][k]
    new["web"]["known_ips"] = cur["web"]["known_ips"]
    new["security"] = cur["security"]


# ─────────────────────── لاگ تلاش‌های ورود ───────────────────────
_login_last: dict[str, float] = {}


def login_log_add(ip: str, user: str, ok: bool, why: str = "") -> bool:
    """ثبت تلاش ورود. ناموفق‌ها برای هر IP حداکثر هر ۱۰ ثانیه یک‌بار ثبت می‌شوند (ضد پر کردن لاگ). خروجی: True اگر ورود موفق از IP جدید بود."""
    now = time.time()
    if not ok:
        if now - _login_last.get(ip, 0) < 10:
            return False
        _login_last[ip] = now
        if len(_login_last) > 500:
            _login_last.clear()
    new_ip = {"v": False}

    def mut(c):
        c["login_log"].append({"ts": now, "ip": ip, "user": str(user)[:60], "ok": bool(ok), "why": why})
        c["login_log"] = c["login_log"][-300:]
        if ok and why == "admin":
            kn = c["web"]["known_ips"]
            if ip not in kn:
                new_ip["v"] = bool(kn)               # اولین ورود بعد از نصب «جدید» حساب نمی‌شود
                kn.append(ip)
                c["web"]["known_ips"] = kn[-30:]
    update_config(mut)
    return new_ip["v"]


# ─────────────────────── نشست ورود (کوکی امضاشده) ───────────────────────
SESSION_SHORT = 12 * 3600
SESSION_LONG = 30 * 86400


def _session_key() -> bytes | None:
    """کلید امضا = راز تصادفی + هش رمز؛ با عوض شدن رمز همه‌ی نشست‌های قبلی باطل می‌شوند."""
    cfg = load_config_ro()
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
        if hmac.compare_digest(sig, good) and secrets.compare_digest(user.encode(), str(load_config_ro()["web"].get("user", "admin")).encode()):
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
                          "balance": 0, "panel": "", "next_plan": False, "tpls": [], "log": []}
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
    sh = shop_totals(cfg)
    return {"panels": len(cfg["panels"]), "reps": len(cfg["reps"]),
            "rep_users": sum(len(m) for m in cfg["rep_users"].values()),
            "shop_customers": sh["customers"], "shop_balance": sh["balance"], "shop_orders": sh["orders"],
            "shop_services": sh["services"]}


def shop_totals(cfg: dict) -> dict:
    """شمارش کاربران، جمع موجودی، سفارش‌ها و سرویس‌های فروشگاه (برای کپشن بکاپ و پیش‌نمایش بازیابی)."""
    sh = cfg.get("shop") or {}
    cu = sh.get("customers") or {}
    return {"customers": len(cu), "balance": sum(int(r.get("balance") or 0) for r in cu.values()),
            "orders": len(sh.get("orders") or []), "services": sum(len(r.get("services") or []) for r in cu.values())}


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


def set_rep_next_plan(rid: str, enabled: bool) -> bool:
    """اجازه‌ی «افزودن پلن بعدی» برای کاربرهای خود نماینده (با کسر از کیف پول)."""
    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        c["reps"][rid]["next_plan"] = bool(enabled)
    update_config(mut)
    return bool(enabled)


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


# ───────────────────────────── لاگ ممیزی ─────────────────────────────
def audit_event(actor: str, action: str, target: str = "", details: str = "", amount: int | None = None, balance: int | None = None,
                cat: str | None = None) -> None:
    """ثبت تغییرات مدیریتی و فعالیت‌های مهم؛ آخرین AUDIT_MAX رخداد نگه‌داری می‌شود.

    cat (اختیاری): wallet | rep | user | del | sys. اگر ندهید از روی نام عملیات حدس زده می‌شود.
    هیچ‌وقت خطا نمی‌دهد؛ شکست در ثبت لاگ نباید ساخت/تمدید/کیف پول را خراب کند.
    """
    try:
        actor = str(actor or "نامشخص")
        if actor == "ادمین وب‌پنل":
            actor = f"ادمین وب‌پنل ({load_config_ro().get('web', {}).get('user', 'admin')})"
        elif actor in ("نماینده", "عملیات نماینده"):
            rep = load_config_ro().get("reps", {}).get(str(target), {})
            if rep:
                actor = f"نماینده: {rep.get('name', target)}"
        rec = {"ts": int(time.time()), "actor": actor[:80], "action": str(action or "تغییر")[:120],
               "target": str(target or "")[:120], "details": str(details or "")[:1500]}
        if cat in AUDIT_CATS:
            rec["cat"] = cat
        if amount is not None:                  # عملیات مالی: مبلغ (±) و موجودی بعد از آن جدا ذخیره می‌شود
            rec["amt"] = int(amount)
        if balance is not None:
            rec["bal"] = int(balance)

        def mut(c):
            c.setdefault("audit_log", []).append(rec)
            c["audit_log"] = c["audit_log"][-AUDIT_MAX:]
        update_config(mut)
    except Exception:  # noqa: BLE001
        log.exception("audit_event")


def audit_category(x: dict) -> str:
    """دسته‌ی رویداد برای فیلتر: wallet | rep | user | del | sys (اول مقدار ذخیره‌شده، برای رویدادهای قدیمی حدس از روی نام)."""
    if x.get("cat") in AUDIT_CATS:
        return x["cat"]
    a = str(x.get("action") or "")
    if "amt" in x or "کیف پول" in a or "موجودی" in a:
        return "wallet"
    if "حذف کاربر" in a and "از نماینده" not in a:
        return "del"
    if "نماینده" in a or "تمپلت" in a or "پنل نماینده" in a:
        return "rep"
    if "کاربر" in a or "اشتراک" in a or "یادداشت" in a or "پرداخت" in a:
        return "user"
    return "sys"


def user_brief(u: dict) -> str:
    """خلاصه‌ی کوتاه وضعیت کاربر برای لاگ (قبل از حذف و ...)."""
    try:
        st = str(u.get("status") or "؟")
        used = float(u.get("used_traffic") or 0) / GB
        lim = float(u.get("data_limit") or 0) / GB
        exp = u.get("expire")
        if isinstance(exp, (int, float)) and exp > 0:
            exp = datetime.fromtimestamp(int(exp), timezone.utc).strftime("%Y-%m-%d")
        elif not exp:
            exp = "نامحدود"
        return f"وضعیت: {st}؛ مصرف: {used:.1f}/{(str(round(lim, 1)) if lim else 'نامحدود')} گیگ؛ انقضا: {str(exp)[:10]}"
    except Exception:  # noqa: BLE001
        return ""


# ───────────────────────────── کیف پول نماینده ─────────────────────────────
def _wlog(c: dict, rid: str, kind: str, amount: int, note: str = "") -> None:
    r = c["reps"][rid]
    r["log"] = (r.get("log", []) + [{"ts": int(time.time()), "type": kind, "amount": int(amount), "balance": r["balance"], "note": str(note)[:120]}])[-WALLET_LOG_MAX:]


WALLET_MAX = 10 ** 11
WALLET_KIND_FA = {"charge": "شارژ توسط ادمین", "deduct": "کسر توسط ادمین", "topup": "افزایش موجودی", "purchase": "خرید/تمدید", "refund": "بازگشت وجه"}


def _tx_find(c: dict, txid: str) -> dict | None:
    return next((t for t in reversed(c.get("wallet_tx", [])) if t["id"] == txid), None)


def wallet_adjust_ex(rid: str, amount: int = 0, kind: str = "charge", note: str = "", sale_meta: dict | None = None,
                     txid: str | None = None, refund_of: str | None = None) -> dict:
    """تغییر اتمیک موجودی نماینده. خروجی: {"balance", "dup", "txid"}.

    - amount مثبت = شارژ، منفی = کسر. موجودی منفی مجاز نیست.
    - txid: شناسه‌ی یکتای تراکنش. اگر قبلاً ثبت شده باشد هیچ تغییری نمی‌دهد و dup=True برمی‌گرداند (جلوگیری از کسر/شارژ تکراری با کلیک دوباره یا تلاش مجدد).
    - refund_of: شناسه‌ی خریدی که باید برگردد؛ مبلغ از خودِ تراکنش اصلی خوانده می‌شود و هر خرید فقط یک‌بار قابل بازگشت است.
    """
    out: dict = {}

    def mut(c):
        nonlocal amount, kind, txid
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        rep = c["reps"][rid]
        if refund_of:
            orig = _tx_find(c, refund_of)
            if not orig or orig["rid"] != rid or orig["kind"] != "purchase" or orig["amount"] >= 0:
                raise ValueError("تراکنش اصلی برای بازگشت وجه پیدا نشد.")
            txid = "refund:" + refund_of
            kind, amount = "refund", -int(orig["amount"])
        amount = int(amount)
        if txid:
            old = _tx_find(c, txid)
            if old:                                           # قبلاً انجام شده ← تکراری
                out.update(balance=rep["balance"], dup=True, txid=txid, tx=dict(old))
                return
        nb = rep["balance"] + amount
        if nb < 0:
            raise ValueError("موجودی کافی نیست.")
        if nb > WALLET_MAX:
            raise ValueError("سقف موجودی رد می‌شود.")
        rep["balance"] = nb
        _wlog(c, rid, kind, amount, note)
        if kind in ("purchase", "refund") and amount != 0:       # خرید/تمدید (منفی در کیف پول) ← فروش نماینده؛ بازگشت وجه از آن کم می‌شود
            c["rep_sales"].append({"ts": time.time(), "rid": rid, "amount": -amount, **({"meta": dict(sale_meta)} if sale_meta else {})})
        if kind == "charge" and amount > 0:                      # شارژ دستی ادمین ← فروش امروز
            c["wallet_income"].append({"ts": time.time(), "rid": rid, "amount": amount})
        if kind == "deduct" and amount < 0:                      # کسر ادمین (اصلاح شارژ اشتباه) ← از فروش همان روز کم می‌شود
            c["wallet_income"].append({"ts": time.time(), "rid": rid, "amount": amount})
        if txid:
            c["wallet_tx"].append({"id": txid, "ts": time.time(), "rid": rid, "kind": kind, "amount": amount, "balance": nb,
                                   "state": "pending" if kind == "purchase" else "done", "user": str((sale_meta or {}).get("username") or ""), "alerted": False})
            c["wallet_tx"] = c["wallet_tx"][-WALLET_TX_MAX:]
            if kind == "refund" and refund_of:
                o = _tx_find(c, refund_of)
                if o:
                    o["state"] = "refunded"
        out.update(balance=nb, dup=False, txid=txid, amount=amount)

    update_config(mut)
    if not out.get("dup"):
        try:                                                     # شکست لاگ ممیزی نباید عملیات مالیِ انجام‌شده را خراب کند
            audit_event("ادمین وب‌پنل" if kind in ("charge", "deduct") else "عملیات نماینده", "تغییر کیف پول", rid,
                        f"{WALLET_KIND_FA.get(kind, kind)}" + (f" — {note}" if note else ""), amount=out.get("amount", 0), balance=out["balance"])
        except Exception:  # noqa: BLE001
            log.exception("audit wallet")
    return out


def wallet_adjust(rid: str, amount: int, kind: str = "charge", note: str = "", sale_meta: dict | None = None, txid: str | None = None) -> int:
    """نسخه‌ی ساده: فقط موجودی جدید را برمی‌گرداند (سازگار با کد قبلی)."""
    return wallet_adjust_ex(rid, amount, kind, note, sale_meta, txid)["balance"]


def wallet_tx_get(txid: str) -> dict | None:
    t = _tx_find(load_config_ro(), txid)
    return dict(t) if t else None


def wallet_tx_mark(txid: str, state: str, user: str | None = None) -> None:
    """وضعیت تراکنش خرید را نهایی می‌کند: done (نتیجه ثبت شد) | refunded | failed."""
    def mut(c):
        t = _tx_find(c, txid)
        if t:
            t["state"] = state
            if user:
                t["user"] = str(user)[:128]
    update_config(mut)


def wallet_tx_stale(max_age: int = 600) -> list[dict]:
    """خریدهایی که بیش از max_age ثانیه «در حال انجام» مانده‌اند (کسر شده ولی نتیجه ثبت نشده، مثلاً قطع برق/کرش) و هنوز به ادمین گزارش نشده‌اند."""
    now = time.time()
    found: list[dict] = []
    if not any(t["state"] == "pending" and t["kind"] == "purchase" and now - t["ts"] > max_age and not t.get("alerted") for t in load_config()["wallet_tx"]):
        return found                                           # چیزی برای گزارش نیست؛ بی‌دلیل روی config.json ننویس

    def mut(c):
        for t in c["wallet_tx"]:
            if t["state"] == "pending" and t["kind"] == "purchase" and now - t["ts"] > max_age and not t.get("alerted"):
                t["alerted"] = True
                found.append(dict(t))
    update_config(mut)
    return found


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
    audit_event("نماینده", "درخواست افزایش موجودی", rid, f"مبلغ: {int(amount):,} تومان؛ {str(note).strip()[:200]}")
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
            if t["rid"] not in c["reps"]:
                raise ValueError("نماینده پیدا نشد.")
            if c["reps"][t["rid"]]["balance"] + amt > WALLET_MAX:
                raise ValueError("سقف موجودی رد می‌شود.")
            c["reps"][t["rid"]]["balance"] += amt
            _wlog(c, t["rid"], "topup", amt, "تأیید درخواست افزایش موجودی")
            c["wallet_income"].append({"ts": time.time(), "rid": t["rid"], "amount": amt})     # تأیید درخواست ← فروش امروز
            c["wallet_tx"].append({"id": "topup:" + tid, "ts": time.time(), "rid": t["rid"], "kind": "topup", "amount": amt,
                                   "balance": c["reps"][t["rid"]]["balance"], "state": "done", "user": "", "alerted": False})
            t["amount"] = amt
        t["status"] = "approved" if approve else "rejected"
        t["done"] = int(time.time())
        out.update(t)
    update_config(mut)
    audit_event("ادمین وب‌پنل", "بررسی درخواست افزایش موجودی", out.get("rid", ""), "تأیید شد" if out.get("status") == "approved" else "رد شد",
                amount=out.get("amount", 0) if out.get("status") == "approved" else None)
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
    cfg = load_config_ro().get("sales", {})
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
    if tpl.get("hours"):                                   # تست رایگان: مدت بر حسب ساعت، شروع از لحظه‌ی ساخت
        p["status"] = "active"
        p["expire"] = int(time.time()) + int(tpl["hours"]) * 3600
    elif mode == "on_hold":
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
    pid = pid or load_config_ro()["active_panel"]
    if pid not in _clients:
        _clients[pid] = RebeccaClient(pid)
    _clients[pid]._sync()  # نام و آدرس‌ها همیشه با config هماهنگ باشند
    return _clients[pid]


def drop_api(pid: str):
    c = _clients.pop(pid, None)
    return c


async def create_users(api, names, tpl, service_id, *, concurrency=1, on_progress=None):
    """ساخت کاربرها. پیش‌فرض یکی‌یکی و به ترتیب نام‌ها (concurrency=1) تا شماره‌ها در پنل پشت‌سرهم ساخته شوند؛
    با concurrency بالاتر درخواست‌ها هم‌زمان می‌روند و ترتیب ساخت در پنل به‌هم می‌ریزد. خروجی: (موفق‌ها به ترتیب، [(نام، دلیل خطا)])."""
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
    if ok:
        await asyncio.to_thread(hist_add_many, [{"pid": getattr(api, "pid", ""), "u": n, "k": "new", "gb": tpl.get("gb") or 0, "days": tpl.get("days") or 0} for n in ok])
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
    mode = mode if mode in ("duration", "chain") else load_config_ro()["next_plan_mode"]
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
    mode = mode if mode in ("duration", "chain") else load_config_ro()["next_plan_mode"]
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
    if load_config_ro()["next_plan_mode"] != "chain":      # duration: پلن «از همین الان + N روز» است و با تمدید جابه‌جا نمی‌شود
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
        uc = load_config_ro()["undo"]
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
        cut = time.time() - load_config_ro()["undo"]["days"] * 86400
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


# ───────────────────────────── تاریخچه‌ی کاربر (ساخت / تمدید) ─────────────────────────────
# فقط‌نمایشی: در فایل محلی user_history.json ثبت می‌شود و هیچ‌وقت روی عملیات اصلی (ساخت/تمدید/کیف پول) اثر ندارد.
# هر رکورد: {"ts", "pid", "u": username, "k": new|renew|renew-keep|attach|next, "gb", "days"}
_HIST_LOCK = threading.Lock()
_HIST_MAX = 60000          # سقف کل رکوردها؛ قدیمی‌ترها حذف می‌شوند
_HIST_KINDS = ("new", "renew", "renew-keep", "attach", "next")


def _hist_file() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE) or ".", "user_history.json")


def _hist_read() -> list[dict]:
    try:
        with open(_hist_file(), encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict) and "u" in x and "ts" in x] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def hist_add_many(items: list[dict]) -> None:
    """ثبت چند رویداد با یک بار نوشتن فایل. هیچ‌وقت خطا نمی‌دهد."""
    try:
        now = time.time()
        rows = []
        for x in items or []:
            k = x.get("k")
            if k not in _HIST_KINDS or not x.get("u"):
                continue
            rows.append({"ts": float(x.get("ts") or now), "pid": str(x.get("pid") or ""), "u": str(x["u"]), "k": k,
                         "gb": float(x.get("gb") or 0), "days": int(x.get("days") or 0)})
        if not rows:
            return
        with _HIST_LOCK:
            data = (_hist_read() + rows)[-_HIST_MAX:]
            path = _hist_file()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("hist_add_many")


def hist_get(pid: str, username: str) -> list[dict]:
    """رویدادهای ثبت‌شده‌ی این کاربر (قدیمی‌ترین اول)."""
    pid = str(pid or "")
    return [x for x in _hist_read() if x.get("u") == username and str(x.get("pid", "")) == pid]


async def renew_user(api, username: str, tpl: dict, reset_usage: bool = True):
    """حجم/مدت جدید طبق تمپلت. با reset_usage مصرف صفر می‌شود (ابتدا ریست، بعد وضعیت تا On-Hold خراب نشود)."""
    p = api.upath(username)
    before = await api.request("GET", p)
    await asyncio.to_thread(undo_snapshot, getattr(api, "pid", ""), username, before, "renew" if reset_usage else "renew-keep")
    if reset_usage:
        await api.request("POST", p + "/reset")
    res = await api.request("PUT", p, json=build_renew_payload(tpl))
    await asyncio.to_thread(hist_add_many, [{"pid": getattr(api, "pid", ""), "u": username, "k": "renew" if reset_usage else "renew-keep",
                                             "gb": tpl.get("gb") or 0, "days": tpl.get("days") or 0}])
    return await rebase_plans(api, username, before) or res


async def set_note(api, username: str, note: str):
    return await api.request("PUT", api.upath(username), json={"note": note[:500]})


BULK_ACTIONS = {
    "service": "🔀 تغییر سرویس", "renew": "⚡ تمدید با تمپلت", "reset": "🔄 صفر کردن مصرف",
    "disable": "⛔ غیرفعال‌سازی", "enable": "✅ فعال‌سازی",   # حذف عمداً نیست: حذف کاربر فقط دستی و تکی
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
                    if len(names) <= load_config_ro()["undo"]["bulk_max"]:      # ریست دسته‌ای بزرگ: عکس‌برداری نمی‌کنیم تا پنل سنگین نشود
                        await asyncio.to_thread(undo_snapshot, getattr(api, "pid", ""), n, await api.request("GET", p), "reset")
                    await api.request("POST", p + "/reset")
                elif action in ("disable", "enable"):
                    await api.request("PUT", p, json={"status": "disabled" if action == "disable" else "active"})
                else:
                    raise ValueError("عملیات ناشناخته")
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
    t_ = sp.add_parser("twofa")
    t_.add_argument("action", choices=["off", "status"])
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
    if a.cmd == "twofa":                      # بازیابی اضطراری (گم‌شدن گوشی و کدهای بازیابی)؛ فقط با دسترسی root به سرور
        if a.action == "off":
            totp_disable()
            print("TWOFA=off")
        else:
            print("TWOFA=" + ("on" if totp_enabled() else "off"))
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
