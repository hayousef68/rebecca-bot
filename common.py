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
    "backup": {"enabled": True},
    "monitor": {"enabled": True, "interval": 300},
    "reminders": {"enabled": True, "hour": 10, "days": 3, "low_pct": 10},
    "web": {"user": "admin", "salt": "", "hash": "", "secret": ""},
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
            tpls[str(name)] = {"gb": max(0, _num(t.get("gb", 0))), "days": max(0, int(t.get("days", 0))), "mode": mode, "kind": kind}
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
                except (TypeError, ValueError):
                    sec[k] = dv
        cfg[key] = sec
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
    return await api.request("PUT", api.upath(username), json=p)


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
    if "service_id" in p:
        got = (res if isinstance(res, dict) and "service_id" in res else await api.request("GET", ph)).get("service_id")
        if got is not None and int(got) != p["service_id"]:
            raise RebeccaError("پنل تغییر سرویس را اعمال نکرد (سرویس همچنان قبلی است).")
    return res


_PLAN_DROP = ("id", "user_id")                      # فیلدهایی که هنگام نوشتن نباید فرستاده شوند
_PLAN_DEFAULTS = {"add_remaining_traffic": False, "fire_on_either": True, "increase_data_limit": True, "start_on_first_connect": False}


def _raw_plans(u: dict) -> list[dict]:
    """فهرست خام پلن‌های بعدی کاربر؛ ربکا ≥0.2 «next_plans» (لیست) دارد، نسخه‌های قدیمی «next_plan» (تکی)."""
    pl = u.get("next_plans")
    if isinstance(pl, list):
        return [x for x in pl if isinstance(x, dict)]
    old = u.get("next_plan")
    return [old] if isinstance(old, dict) and (old.get("data_limit") or old.get("expire")) else []


def next_plans_view(u: dict) -> list[dict]:
    """پلن‌های بعدی برای نمایش: [{idx, gb, days, add_remaining}] به ترتیب اجرا."""
    out = []
    for i, p in enumerate(_raw_plans(u)):
        dl, ex = int(p.get("data_limit") or 0), int(p.get("expire") or 0)
        out.append({"idx": i, "gb": round(dl / GB, 2), "days": ex // 86400, "add_remaining": bool(p.get("add_remaining_traffic"))})
    return out


async def set_next_plans(api, username: str, plans: list[dict]):
    """جایگزینی کامل فهرست «پلن بعدی» (شارژ خودکار). هر پلن: {idx|None, gb, days, add_remaining}.
    idx = شماره‌ی پلن موجود (برای حفظ سایر فیلدهای آن)؛ None = پلن جدید.
    بعد از نوشتن دوباره می‌خواند تا مطمئن شود پنل واقعاً پذیرفته است."""
    ph = api.upath(username)
    u = await api.request("GET", ph)
    raw = _raw_plans(u)
    legacy = "next_plans" not in u and "next_plan" in u          # ربکای قدیمی
    built = []
    for i, pl in enumerate(plans):
        gb, days = float(pl.get("gb") or 0), int(pl.get("days") or 0)
        if gb <= 0 and days <= 0:
            raise RebeccaError("حجم یا مدت پلن بعدی را وارد کنید.")
        idx = pl.get("idx")
        base = dict(raw[idx]) if isinstance(idx, int) and 0 <= idx < len(raw) else {**_PLAN_DEFAULTS, **{k: v for k, v in (raw[0] if raw else {}).items() if k not in ("data_limit", "expire")}}
        for k in _PLAN_DROP:
            base.pop(k, None)
        base.update(data_limit=int(gb * GB) if gb > 0 else 0, expire=days * 86400 if days > 0 else 0, add_remaining_traffic=bool(pl.get("add_remaining")))
        if "position" in base:
            base["position"] = i
        built.append(base)
    if legacy:
        body = {"next_plan": built[0] if built else None}
    else:
        body = {"next_plans": built}
    await api.request("PUT", ph, json=body)
    after = await api.request("GET", ph)
    if len(next_plans_view(after)) != len(built):
        raise RebeccaError("پنل پلن بعدی را ذخیره نکرد (نسخه‌ی ربکا یا فیلدها سازگار نیست). جزئیات فنی را بفرستید.")
    return after


async def set_service(api, username: str, service_id: int):
    """تغییر سرویس کاربر؛ بعد از تغییر وضعیت را می‌خواند تا مطمئن شود پنل واقعاً پذیرفته است."""
    p = api.upath(username)
    res = await api.request("PUT", p, json={"service_id": int(service_id)})
    after = res if isinstance(res, dict) and "service_id" in res else await api.request("GET", p)
    got = after.get("service_id")
    if got is not None and int(got) != int(service_id):
        raise RebeccaError("پنل تغییر سرویس را اعمال نکرد (سرویس همچنان قبلی است).")
    return after


async def renew_user(api, username: str, tpl: dict, reset_usage: bool = True):
    """حجم/مدت جدید طبق تمپلت. با reset_usage مصرف صفر می‌شود (ابتدا ریست، بعد وضعیت تا On-Hold خراب نشود)."""
    p = api.upath(username)
    if reset_usage:
        await api.request("POST", p + "/reset")
    return await api.request("PUT", p, json=build_renew_payload(tpl))


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


async def send_results(bot, chat_id, results: list[dict], header, max_qr: int = 100):
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
    pngs = await asyncio.gather(*(asyncio.to_thread(make_qr_png, r["sub"]) for r in with_qr))
    # هر اشتراک جداگانه و پشت‌سرهم (به ترتیب ساخت)
    for r, png in zip(with_qr, pngs):
        try:
            await _retry(lambda: bot.send_photo(chat_id, png, caption=caption(r), parse_mode=ParseMode.HTML))
        except TelegramError as e:
            log.warning("send_photo failed for %s: %s", r["username"], e)
            await _retry(lambda: bot.send_message(chat_id, caption(r), parse_mode=ParseMode.HTML))
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
