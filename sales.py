# -*- coding: utf-8 -*-
"""گزارش فروش بر پایه‌ی خواندن مستقیم دیتابیس MySQL پنل ربکا.

- هیچ پولینگ/اسنپ‌شاتی ندارد: فقط وقتی گزارش لازم است (۰۰:۰۰ تهران یا دکمه) یک کوئری read-only اجرا می‌شود.
- ساخت کاربر  = users.created_at داخل بازه‌ی روز
- ریست/تمدید = user_usage_logs.reset_at داخل بازه‌ی روز
نام جدول/ستون‌ها در config.json > sales.schema قابل تغییر است (دستور /dbcheck ساختار واقعی را نشان می‌دهد).
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # noqa: BLE001
    TEHRAN = timezone(timedelta(hours=3, minutes=30))

from common import load_config, esc, config_backup_bytes, backup_summary

log = logging.getLogger("rebecca.sales")

DEFAULT_SCHEMA = {
    "users": "users", "u_id": "id", "u_name": "username", "u_created": "created_at",
    "u_limit": "data_limit", "u_expire": "expire", "u_hold": "on_hold_expire_duration", "u_service": "service_id",
    "resets": "user_usage_logs", "r_user": "user_id", "r_at": "reset_at",
    "services": "services", "s_id": "id", "s_name": "name",
}
GB = 1024 ** 3


# ───────────────────────────── زمان ─────────────────────────────
def tehran_day() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d")


def shift_day(day: str, delta: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=delta)).strftime("%Y-%m-%d")


def g2j(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


_WD = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # weekday(): Mon=0
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def jdate(day: str, weekday: bool = True) -> str:
    d = date.fromisoformat(day)
    jy, jm, jd = g2j(d.year, d.month, d.day)
    out = f"{jy}/{jm:02d}/{jd:02d}".translate(_FA)
    return f"{_WD[d.weekday()]} {out}" if weekday else out


def jmonth_start(day: str) -> str:
    """اولین روز ماه شمسی که day در آن است (به میلادی)."""
    d = date.fromisoformat(day)
    _, _, jd = g2j(d.year, d.month, d.day)
    return (d - timedelta(days=jd - 1)).strftime("%Y-%m-%d")


def _bounds_days(d1: str, d2: str, partial: bool):
    """از شروع d1 تا پایان d2 (به وقت تهران) → datetime ساده در منطقه‌ی دیتابیس."""
    off = int(load_config()["sales"].get("db_tz_offset_minutes", 0))
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    s = datetime(a.year, a.month, a.day, tzinfo=TEHRAN)
    e = datetime(b.year, b.month, b.day, tzinfo=TEHRAN) + timedelta(days=1)
    if partial:
        e = min(e, datetime.now(TEHRAN))
    conv = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=off)  # noqa: E731
    return conv(s), conv(e)


def _bounds_db(day: str, partial: bool):
    """بازه‌ی روز تهران → datetime ساده در منطقه‌ی زمانی دیتابیس (پیش‌فرض UTC)."""
    cfg = load_config()["sales"]
    off = int(cfg.get("db_tz_offset_minutes", 0))
    d = date.fromisoformat(day)
    s = datetime(d.year, d.month, d.day, tzinfo=TEHRAN)
    e = s + timedelta(days=1)
    if partial:
        e = min(e, datetime.now(TEHRAN))
    conv = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=off)  # noqa: E731
    return conv(s), conv(e)


# ───────────────────────────── ثبت پیوست‌ها ─────────────────────────────
# پیوست (افزودن حجم/زمان به اکانت) در دیتابیس ربکا اثری ندارد (ریست نیست)؛ پس خود ربات ثبتش می‌کند.
import json
import os
import threading
import time as _time

_ATTACH_LOCK = threading.Lock()
_ATTACH_KEEP_DAYS = 400


def _attach_file() -> str:
    from common import CONFIG_FILE
    return os.path.join(os.path.dirname(CONFIG_FILE), "attach_log.json")


def _load_attach() -> list[dict]:
    try:
        with open(_attach_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_attach(username: str, service_id, gb: float, days: int = 0, ts: float | None = None, kind: str = "attach") -> None:
    """یک پیوست/پلن بعدی (حجم gb + مدت days) را برای محاسبه‌ی فروش ثبت می‌کند. kind: attach | next. حجم صفر ثبت نمی‌شود."""
    if not gb or gb <= 0:
        return
    now = ts or _time.time()
    with _ATTACH_LOCK:
        items = [x for x in _load_attach() if now - float(x.get("ts", 0)) < _ATTACH_KEEP_DAYS * 86400]
        items.append({"ts": now, "u": str(username), "sid": service_id, "gb": float(gb), "days": int(days or 0), "k": "next" if kind == "next" else "attach"})
        path = _attach_file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, path)


def _attach_rows(a, b, off: int, svc: dict) -> list[dict]:
    """پیوست‌های بازه‌ی [a, b) (datetime ساده در منطقه‌ی دیتابیس) به‌شکل ردیف رویداد."""
    out = []
    for x in _load_attach():
        try:
            at = datetime.fromtimestamp(float(x["ts"]), tz=timezone.utc).replace(tzinfo=None) + timedelta(minutes=off)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if not (a <= at < b):
            continue
        sid = x.get("sid")
        kd = "next" if x.get("k") == "next" else "attach"
        out.append({"kind": kd, "eid": f"{kd}|{x.get('u', '')}|{int(float(x['ts']))}", "at": at, "username": x.get("u", ""), "data_limit": float(x.get("gb", 0)) * GB, "expire": None,
                    "hold": None, "service_id": sid, "service_name": svc.get(sid, ""), "created_at": None, "_off": off,
                    "months": 2 if int(x.get("days") or 0) > 45 else 1, "days": int(x.get("days") or 0)})
    return out


# ───────────────────────────── اصلاح دستی فروش (حذف / ویرایش) ─────────────────────────────
# ردیف‌های دیتابیس ربکا فقط‌خواندنی‌اند؛ پس حذف/ویرایش یک فروش در فایل کوچک sales_overrides.json ذخیره می‌شود و
# موقع ساخت گزارش اعمال می‌شود (هیچ درخواستی به پنل/دیتابیس نمی‌زند و هر لحظه قابل برگشت است).
#   hide : {eid: {"v": True|False, "ts": زمان ثبت}}   — True = حذف از گزارش، False = نمایش اجباری
#   edit : {eid: {"gb": int|None, "months": 1|2|None, "type": "ham"|"single"|None, "ts": ...}}
#   spans: [{"u": username, "from": ts, "to": ts, "ts": ...}] — تمدیدهای این کاربر در این بازه حذف‌اند (برگشت تمدید)
_OVR_LOCK = threading.Lock()
_OVR_KEEP_DAYS = 400
_OVR_CACHE: dict = {"sig": None, "data": None}


def _ovr_file() -> str:
    from common import CONFIG_FILE
    return os.path.join(os.path.dirname(CONFIG_FILE), "sales_overrides.json")


def _ovr_empty() -> dict:
    return {"hide": {}, "edit": {}, "spans": []}


def _load_ovr() -> dict:
    path = _ovr_file()
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _ovr_empty()
    if _OVR_CACHE["sig"] == sig and _OVR_CACHE["data"] is not None:
        return _OVR_CACHE["data"]
    data = _ovr_empty()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in ("hide", "edit"):
                if isinstance(raw.get(k), dict):
                    data[k] = raw[k]
            if isinstance(raw.get("spans"), list):
                data["spans"] = [x for x in raw["spans"] if isinstance(x, dict) and "u" in x and "from" in x and "to" in x]
    except (OSError, ValueError):
        pass
    _OVR_CACHE.update(sig=sig, data=data)
    return data


def _save_ovr(data: dict) -> None:
    cut = _time.time() - _OVR_KEEP_DAYS * 86400
    data["hide"] = {k: v for k, v in data["hide"].items() if float((v or {}).get("ts", cut + 1)) >= cut}
    data["edit"] = {k: v for k, v in data["edit"].items() if float((v or {}).get("ts", cut + 1)) >= cut}
    keep = [x for x in data["spans"] if float(x.get("ts", cut + 1)) >= cut]
    data["spans"] = [x for x in keep if not x.get("any")][-500:] + [x for x in keep if x.get("any")][-20000:]     # بازه‌های کیف پول نماینده کم نمی‌شوند تا فروش قدیمی دوباره ظاهر نشود
    path = _ovr_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    _OVR_CACHE["sig"] = None


def _ovr_mutate(fn) -> None:
    with _OVR_LOCK:
        data = json.loads(json.dumps(_load_ovr()))   # کپی عمیق؛ کش دست‌نخورده می‌ماند
        fn(data)
        _save_ovr(data)


def ovr_hide(eid: str, hidden: bool = True) -> None:
    """حذف (hidden=True) یا بازگرداندن (False) یک فروش در گزارش."""
    _ovr_mutate(lambda d: d["hide"].__setitem__(str(eid), {"v": bool(hidden), "ts": _time.time()}))


def ovr_edit(eid: str, gb: int | None = None, months: int | None = None, typ: str | None = None) -> None:
    """ویرایش حجم/مدت/نوع (همکاری یا تکی) یک فروش. فقط مقادیرِ غیر None عوض می‌شوند و بقیه‌ی ویرایش‌های قبلی همان فروش حفظ می‌ماند."""
    def fn(d):
        cur = d["edit"].get(str(eid)) or {}
        rec = {"gb": gb if gb is not None else cur.get("gb"), "months": months if months is not None else cur.get("months"),
               "type": typ if typ is not None else cur.get("type"), "ts": _time.time()}
        if rec["gb"] is None and rec["months"] is None and rec["type"] is None:
            d["edit"].pop(str(eid), None)
        else:
            d["edit"][str(eid)] = rec
    _ovr_mutate(fn)


def ovr_edit_clear(eid: str) -> None:
    """لغو همه‌ی ویرایش‌های یک فروش (برمی‌گردد به مقدار اصلی دیتابیس)."""
    _ovr_mutate(lambda d: d["edit"].pop(str(eid), None))


def ovr_add_span(username: str, t_from: float, t_to: float, any_kind: bool = False) -> None:
    """بعد از «برگشت تمدید»: تمدید/ریست/پیوست همان کاربر در این بازه از فروش حذف شود.
    any_kind=True: «ساخت» را هم حذف می‌کند (ساخت/تمدید با کیف پول نماینده که جزو فروش نیست)."""
    rec = {"u": str(username), "from": float(t_from), "to": float(t_to), "ts": _time.time()}
    if any_kind:
        rec["any"] = True
    _ovr_mutate(lambda d: d["spans"].append(rec))


def _eid(r: dict) -> str:
    at = r.get("at")
    stamp = at.strftime("%Y%m%d%H%M%S") if isinstance(at, datetime) else str(at)
    return f"{r.get('kind')}|{r.get('username')}|{stamp}"


def _ts_of(r: dict, off: int) -> float:
    at = r.get("at")
    if not isinstance(at, datetime):
        return 0.0
    return (at - timedelta(minutes=off)).replace(tzinfo=timezone.utc).timestamp()


def _apply_overrides(rows: list[dict], off: int, include_hidden: bool) -> list[dict]:
    ov = _load_ovr()
    hide, edit, spans = ov["hide"], ov["edit"], ov["spans"]
    if not (hide or edit or spans) and not include_hidden:
        for r in rows:
            r["eid"] = r.get("eid") or _eid(r)
        return rows
    cfg = load_config()["sales"]
    out = []
    for r in rows:
        r["eid"] = eid = r.get("eid") or _eid(r)
        ts = _ts_of(r, off)
        r["ts"] = ts
        h = hide.get(eid)
        if isinstance(h, dict):
            hidden = bool(h.get("v"))
        else:
            hidden = any(s["u"] == r["username"] and float(s["from"]) <= ts <= float(s["to"]) and (s.get("any") or r["kind"] != "new") for s in spans)
        e = edit.get(eid)
        if isinstance(e, dict):
            r["orig"] = {"gb": round((r.get("data_limit") or 0) / GB), "months": int(r["months"]) if r.get("months") else _months(r), "ham": _is_hamkari(r, cfg)}
            if e.get("gb"):
                r["data_limit"] = float(e["gb"]) * GB
            if e.get("months") in (1, 2):
                r["months"] = int(e["months"])
            if e.get("type") in ("ham", "single"):
                r["force_type"] = e["type"]
            r["edited"] = True
        if hidden:
            if not include_hidden:
                continue
            r["hidden"] = True
        out.append(r)
    return out


# ───────────────────────────── دیتابیس ─────────────────────────────
def _connect():
    import pymysql
    db = load_config()["sales"].get("db") or {}
    if not db.get("name"):
        raise RuntimeError("اتصال دیتابیس تنظیم نشده؛ از /setdb استفاده کنید.")
    return pymysql.connect(
        host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)), user=db.get("user", ""),
        password=db.get("password", ""), database=db["name"], charset="utf8mb4",
        connect_timeout=5, read_timeout=15, cursorclass=pymysql.cursors.DictCursor,
    )


def db_ping() -> int:
    """بررسی اتصال دیتابیس فروش؛ زمان پاسخ به میلی‌ثانیه (در خطا استثنا)."""
    t0 = _time.time()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()
    return int((_time.time() - t0) * 1000)


def _schema() -> dict:
    s = dict(DEFAULT_SCHEMA)
    s.update(load_config()["sales"].get("schema") or {})
    return s


def _ident(x: str) -> str:
    x = str(x)
    if not x.replace("_", "").isalnum():
        raise ValueError(f"نام نامعتبر: {x}")
    return f"`{x}`"


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def usernames_like(prefix: str) -> list[tuple]:
    """(نام‌کاربری، زمان ساخت) کاربرانی که با prefix شروع می‌شوند؛ کوئری سبک و read-only."""
    sc = _schema()
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    n, c_ = _ident(sc["u_name"]), _ident(sc["u_created"])
    conn = _connect()
    try:
        with conn.cursor() as c:
            c.execute(f"SELECT {n} AS n, {c_} AS c FROM {_ident(sc['users'])} WHERE LOWER({n}) LIKE LOWER(%s)", (like,))
            return [(r["n"], r["c"]) for r in c.fetchall()]
    finally:
        conn.close()


def _pick_next(rows: list, prefix: str, recent: int = 5) -> dict:
    """آخرین کاربر «ساخته‌شده» را پیدا می‌کند (نه بزرگ‌ترین عدد) و شماره‌ی بعدی را می‌دهد.
    از بین ۵ کاربر آخر، بیشترین عدد + ۱ (برای ساخت گروهی هم‌زمان). اگر زمان ساخت نبود، بیشترین عدد."""
    rx = re.compile(r"^" + re.escape(prefix) + r"(\d+)$", re.I | re.A)
    items = []
    for r in rows:
        name, created = (r if isinstance(r, (tuple, list)) else (r, None))
        m = rx.match(str(name or "").translate(_DIGITS))
        if m:
            items.append((str(created or ""), int(m.group(1)), str(name)))
    if not items:
        return {"prefix": prefix, "last": "", "next": 1, "max": ""}
    top = max(items, key=lambda x: x[1])                       # بیشترین عدد کل
    if any(i[0] for i in items):
        pool = sorted(items, key=lambda x: (x[0], x[1]))[-recent:]
        pick = max(pool, key=lambda x: x[1])
    else:
        pick = top
    return {"prefix": pick[2][:len(prefix)], "last": pick[2], "next": pick[1] + 1,
            "max": top[2] if top[1] > pick[1] else ""}


async def next_number(api, prefix: str, use_db: bool = True) -> dict:
    """آخرین نام‌کاربری ساخته‌شده با این پیشوند و شماره‌ی بعدی.
    اول از دیتابیس (سبک‌تر)، اگر نبود از جستجوی API پنل."""
    prefix = (prefix or "").strip()
    if use_db and (load_config()["sales"].get("db") or {}).get("name"):
        try:
            return _pick_next(await asyncio.to_thread(usernames_like, prefix), prefix)
        except Exception as e:  # noqa: BLE001
            log.warning("next_number via db failed: %s", e)
    rows, off = [], 0
    while off < 50000:
        res = await api.request("GET", "/api/users", params={"offset": off, "limit": 200, "search": prefix})
        users = res.get("users", []) if isinstance(res, dict) else (res or [])
        rows += [(u.get("username", ""), u.get("created_at")) for u in users]
        if len(users) < 200:
            break
        off += 200
    return _pick_next(rows, prefix)


def fetch_events(day: str, partial: bool, include_hidden: bool = False) -> list[dict]:
    return _fetch(*_bounds_db(day, partial), include_hidden=include_hidden)


def fetch_events_range(d1: str, d2: str, partial: bool, include_hidden: bool = False) -> list[dict]:
    return _fetch(*_bounds_days(d1, d2, partial), include_hidden=include_hidden)


def _fetch(a, b, include_hidden: bool = False) -> list[dict]:
    s = _schema()
    off = int(load_config()["sales"].get("db_tz_offset_minutes", 0))
    U, R = _ident(s["users"]), _ident(s["resets"])
    cols = (f"u.{_ident(s['u_name'])} AS username, u.{_ident(s['u_limit'])} AS data_limit, u.{_ident(s['u_expire'])} AS expire, "
            f"u.{_ident(s['u_hold'])} AS hold, u.{_ident(s['u_service'])} AS service_id, u.{_ident(s['u_created'])} AS created_at")
    sql_new = (f"SELECT 'new' AS kind, u.{_ident(s['u_created'])} AS at, {cols} FROM {U} u "
               f"WHERE u.{_ident(s['u_created'])} >= %s AND u.{_ident(s['u_created'])} < %s")
    sql_rst = (f"SELECT 'renew' AS kind, r.{_ident(s['r_at'])} AS at, {cols} FROM {R} r "
               f"JOIN {U} u ON u.{_ident(s['u_id'])} = r.{_ident(s['r_user'])} "
               f"WHERE r.{_ident(s['r_at'])} >= %s AND r.{_ident(s['r_at'])} < %s")
    sql_svc = f"SELECT {_ident(s['s_id'])} AS id, {_ident(s['s_name'])} AS name FROM {_ident(s['services'])}"
    conn = _connect()
    try:
        with conn.cursor() as c:
            c.execute(sql_new, (a, b)); rows = list(c.fetchall())
            c.execute(sql_rst, (a, b)); rows += list(c.fetchall())
            try:
                c.execute(sql_svc); svc = {r["id"]: str(r["name"] or "") for r in c.fetchall()}
            except Exception:  # noqa: BLE001
                svc = {}
    finally:
        conn.close()
    for r in rows:
        r["service_name"] = svc.get(r["service_id"], "")
        r["_off"] = off
    try:
        rows += _attach_rows(a, b, off, svc)
    except Exception:  # noqa: BLE001
        log.exception("attach rows")
    # ریستی که بلافاصله (≤ ۲ دقیقه) بعد از ساخت همان کاربر ثبت شده، فروش جدا حساب نمی‌شود
    created = {r["username"]: r["at"] for r in rows if r["kind"] == "new" and isinstance(r["at"], datetime)}
    rows = [r for r in rows if not (r["kind"] == "renew" and r["username"] in created and isinstance(r["at"], datetime)
                                    and abs((r["at"] - created[r["username"]]).total_seconds()) <= 120)]
    rows.sort(key=lambda r: r["at"] if isinstance(r["at"], datetime) else datetime.min)
    return _apply_overrides(rows, off, include_hidden)


# ───────────────────────────── طبقه‌بندی و قیمت ─────────────────────────────
def _is_hamkari(r: dict, cfg: dict) -> bool:
    if r.get("force_type") in ("ham", "single"):      # ویرایش دستی نوع فروش
        return r["force_type"] == "ham"
    if r["service_id"] in [int(x) for x in cfg.get("hamkari_service_ids", []) if str(x).lstrip("-").isdigit()]:
        return True
    names = [str(n).lower() for n in cfg.get("hamkari_service_names", [])]
    return any(n and n in r["service_name"].lower() for n in names)


def _months(r: dict) -> int:
    """مدت پلن (ماه) از on_hold یا فاصله‌ی انقضا تا لحظه‌ی رویداد؛ حداقل ۱."""
    days = 0
    if r.get("hold"):
        days = int(r["hold"]) / 86400
    elif r.get("expire") and r.get("at"):
        at = ((r["at"] - timedelta(minutes=int(r.get("_off", 0)))).replace(tzinfo=timezone.utc).timestamp()
              if isinstance(r["at"], datetime) else 0)
        days = (int(r["expire"]) - at) / 86400
    return 2 if days > 45 else 1


def _norm(x) -> str:
    return str(x or "").lower()


def _is_ignored(r: dict, cfg: dict) -> bool:
    """سرویس‌هایی مثل Tes (تست رایگان) فروش حساب نمی‌شوند."""
    if r.get("force_type") in ("ham", "single"):      # اگر دستی نوع فروش تعیین شده، دیگر «تست» حساب نمی‌شود
        return False
    names = [_norm(n) for n in cfg.get("ignore_service_names", [])]
    sn = _norm(r.get("service_name"))
    return bool(sn) and any(n and n in sn for n in names)


def fallback_rate(cfg: dict) -> float:
    """نرخ هر گیگ فروش تکی برای حجم‌های خارج از لیست؛ از پلن ۳۰ گیگ یک‌ماهه (قابل تغییر)."""
    plans = cfg.get("single_prices", [])
    want = int(cfg.get("fallback_plan_gb", 30))
    p = next((p for p in plans if int(p["gb"]) == want and int(p["months"]) == 1), None)
    if not p and plans:
        p = min(plans, key=lambda p: (int(p["months"]), int(p["gb"])))
    return (int(p["price"]) / max(1, int(p["gb"]))) if p else 0.0


def ham_mult(cfg: dict) -> float:
    """ضریب مصرف همکاری. اگر خاموش باشد یا نامعتبر، ۱ (یعنی بدون ضریب)."""
    if not cfg.get("hamkari_multiplier_enabled", True):
        return 1.0
    try:
        m = float(cfg.get("hamkari_usage_multiplier", 1.5))
    except (TypeError, ValueError):
        return 1.0
    return m if m > 0 else 1.0


def price_for(service_name: str, gb: float, months: int, cfg: dict | None = None):
    """قیمت فروش یک اکانت → (قیمت، نوع). نوع: ham | list | extra | ignored"""
    cfg = cfg or load_config()["sales"]
    r = {"service_name": service_name, "service_id": None}
    if _is_ignored(r, cfg):
        return 0, "ignored"
    if _is_hamkari(r, cfg):
        return int(round(gb * int(cfg["hamkari_per_gb"]))), "ham"
    for p in cfg["single_prices"]:
        if int(p["gb"]) == round(gb) and int(p["months"]) == months:
            return int(p["price"]), "list"
    return int(round(gb * fallback_rate(cfg))), "extra"


def classify(rows: list[dict], cfg: dict) -> dict:
    ham, single, extra, ignored, skipped = Counter(), Counter(), Counter(), Counter(), Counter()
    plans = cfg["single_prices"]
    skip_extra = cfg.get("attach_extra_mode", "skip") == "skip"
    for r in rows:
        gb = round((r["data_limit"] or 0) / GB)
        if gb <= 0:
            continue
        key = r["kind"] if r["kind"] in ("new", "attach", "next") else "renew"
        if _is_hamkari(r, cfg):          # همکاری: همیشه گیگی (حتی خارج از لیست)
            ham[(gb, key)] += 1
            continue
        if _is_ignored(r, cfg):          # تست و سرویس‌های غیرفروشی
            ignored[(gb, key)] += 1
            continue
        m = int(r["months"]) if r.get("months") else _months(r)
        match = next((p for p in plans if int(p["gb"]) == gb and int(p["months"]) == m), None)
        if match:
            single[(gb, m, key)] += 1
        elif key in ("attach", "next") and skip_extra:
            skipped[(gb, m)] += 1        # پیوست خارج از لیست: نه فروش حساب می‌شود نه هزینه
        else:
            extra[(gb, m, key)] += 1     # ms خارج از لیست: به‌نرخ گیگی
    return {"ham": ham, "single": single, "extra": extra, "ignored": ignored, "skipped": skipped}


def wallet_income(d1: str, d2: str, partial: bool) -> tuple[int, int]:
    """مجموع و تعداد شارژهای کیف پول نماینده‌ها در بازه‌ی روزهای تهران (جزو فروش حساب می‌شود)."""
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    s = datetime(a.year, a.month, a.day, tzinfo=TEHRAN).timestamp()
    e = (datetime(b.year, b.month, b.day, tzinfo=TEHRAN) + timedelta(days=1)).timestamp()
    if partial:
        e = min(e, _time.time())
    items = [x for x in load_config().get("wallet_income", []) if s <= float(x["ts"]) < e]
    # شارژها مثبت و کسرهای ادمین (اصلاح شارژ اشتباه) منفی‌اند؛ جمع خالص حساب می‌شود، «تعداد» فقط شارژهاست
    return sum(int(x["amount"]) for x in items), sum(1 for x in items if int(x["amount"]) > 0)


def rep_daily_sales(rid: str, days: int = 7) -> list[dict]:
    """فروش روزانه‌ی یک نماینده (خرید و تمدید با کیف پول منهای بازگشت وجه) برای `days` روز اخیر به وقت تهران؛ جدیدترین روز اول."""
    today = tehran_day()
    out = []
    items = [x for x in load_config().get("rep_sales", []) if x.get("rid") == rid]
    for i in range(days):
        d = shift_day(today, -i)
        a = date.fromisoformat(d)
        s = datetime(a.year, a.month, a.day, tzinfo=TEHRAN).timestamp()
        e = (datetime(a.year, a.month, a.day, tzinfo=TEHRAN) + timedelta(days=1)).timestamp()
        day_items = [x for x in items if s <= float(x["ts"]) < e]
        breakdown: dict[tuple, dict] = {}
        for item in day_items:
            meta = item.get("meta") or {}
            if not meta or int(meta.get("gb") or 0) <= 0:
                continue  # تراکنش‌های قدیمی یا بدون جزئیات در جمع مبلغ می‌مانند، اما دسته‌بندی ندارند
            key = (str(meta.get("action") or "new"), int(meta.get("gb") or 0), int(meta.get("days") or 0), str(meta.get("template") or ""))
            rec = breakdown.setdefault(key, {"action": key[0], "gb": key[1], "days": key[2], "template": key[3], "count": 0})
            rec["count"] += 1 if int(item.get("amount") or 0) > 0 else -1
        details = [v for v in breakdown.values() if v["count"] > 0]
        details.sort(key=lambda x: (x["action"], x["gb"], x["days"], x["template"]))
        out.append({"day": d, "date": jdate(d), "total": sum(int(x["amount"]) for x in day_items),
                    "count": max(0, sum(1 if int(x["amount"]) > 0 else -1 for x in day_items)),
                    "new_count": sum(x["count"] for x in details if x["action"] == "new"),
                    "renew_count": sum(x["count"] for x in details if x["action"] == "renew"),
                    "breakdown": details})
    return out


def compute(day: str, partial: bool) -> dict:
    w, n = wallet_income(day, day, partial)
    return compute_rows(fetch_events(day, partial), w, n)


def compute_range(d1: str, d2: str, partial: bool) -> dict:
    w, n = wallet_income(d1, d2, partial)
    return compute_rows(fetch_events_range(d1, d2, partial), w, n)


def compute_rows(rows: list[dict], wallet: int = 0, wallet_n: int = 0) -> dict:
    """wallet = جمع خالص شارژ کیف پول نماینده‌ها (تومان). به گیگ همکاری تبدیل می‌شود (مبلغ ÷ قیمت هر گیگ همکاری، مثلاً ۳٬۰۰۰)
    و مثل فروش همکاری هم در درآمد و هم در هزینه‌ی خرید حجم حساب می‌شود."""
    cfg = load_config()["sales"]
    cl = classify(rows, cfg)
    price = {(int(p["gb"]), int(p["months"])): int(p["price"]) for p in cfg["single_prices"]}
    rate = fallback_rate(cfg)
    ham_per_gb = int(cfg["hamkari_per_gb"])
    wallet_gb = (wallet / ham_per_gb) if ham_per_gb > 0 else 0
    ham_gb = sum(gb * n for (gb, _k), n in cl["ham"].items()) + wallet_gb
    ham_income = ham_gb * ham_per_gb
    single_income = sum(price[(gb, m)] * n for (gb, m, _k), n in cl["single"].items())
    single_gb = sum(gb * n for (gb, _m, _k), n in cl["single"].items())
    extra_gb = sum(gb * n for (gb, _m, _k), n in cl["extra"].items())
    extra_income = extra_gb * rate
    # مصرف ۱ گیگ واقعی همکاری، ضریب‌برابر از سهمیه‌ی مشتری کم می‌کند ولی ۱ گیگ از حجم خرید ما.
    # پس ترافیک واقعی = گیگ فروخته‌شده ÷ ضریب
    mult = ham_mult(cfg)
    per_gb_cost = int(cfg.get("buy_per_tb", 750000)) / float(cfg.get("tb_gb", 1000))
    ham_real_gb = ham_gb / mult
    cost = (ham_real_gb + single_gb + extra_gb) * per_gb_cost
    return {"cl": cl, "wallet": wallet, "wallet_n": wallet_n, "wallet_gb": wallet_gb, "ham_gb": ham_gb, "ham_income": ham_income, "single_income": single_income,
            "single_gb": single_gb, "extra_gb": extra_gb, "ham_real_gb": ham_real_gb, "mult": mult, "extra_income": extra_income, "rate": rate, "cost": cost,
            "income": ham_income + single_income + extra_income}


def _t(n) -> str:
    return f"{int(round(n)):,}".replace(",", "٬")


_KIND_FA = {"new": "ساخت", "renew": "تمدید/ریست", "attach": "پیوست", "next": "پلن بعدی"}


def _render(title: str, d: dict, html: bool) -> str:
    cl, L = d["cl"], []
    L.append(title)
    L.append("")
    L.append("🤝 همکاری (Hamkari)")
    if not cl["ham"] and not (d.get("wallet_n") or d.get("wallet")):
        L.append("  — فروشی نبود")
    for (gb, k), n in sorted(cl["ham"].items()):
        L.append(f"  {n} تا {gb} گیگ — {_KIND_FA.get(k, k)}")
    if d.get("wallet_n") or d.get("wallet"):
        L.append(f"  💳 شارژ کیف پول نماینده‌ها: {d['wallet_n']} مورد، {_t(d['wallet'])} تومان ← {d['wallet_gb']:,.1f} گیگ".replace(".0 گیگ", " گیگ"))
    L.append(f"  جمع: {_t(d['ham_gb'])} گیگ ← {_t(d['ham_income'])} تومان")
    L.append("")
    L.append("👤 فروش تکی (MS)")
    if not cl["single"]:
        L.append("  — فروشی نبود")
    for (gb, m, k), n in sorted(cl["single"].items()):
        L.append(f"  {n} تا {gb} گیگ {'یک' if m == 1 else 'دو'}ماهه — {_KIND_FA.get(k, k)}")
    L.append(f"  جمع لیست: {_t(d['single_income'])} تومان")
    if cl["extra"]:
        L.append("")
        L.append(f"📐 تکی خارج از لیست (هر گیگ {_t(d['rate'])} تومان):")
        for (gb, m, k), n in sorted(cl["extra"].items()):
            L.append(f"  {n} تا {gb} گیگ {m} ماهه ← {_t(gb * n * d['rate'])}")
        L.append(f"  جمع: {_t(d['extra_income'])} تومان")
    if cl.get("skipped"):
        L.append("")
        L.append(f"📎 پیوست خارج از لیست (محاسبه نشد): {sum(cl['skipped'].values())} مورد")
        for (gb, m), n in sorted(cl["skipped"].items()):
            L.append(f"  {n} تا {gb} گیگ {m} ماهه")
    if cl["ignored"]:
        L.append("")
        L.append(f"🚫 نادیده‌گرفته (تست/غیرفروشی): {sum(cl['ignored'].values())} مورد")
    L.append("")
    L.append(f"💰 جمع کل فروش: {_t(d['income'])} تومان")
    if d.get("mult", 1) != 1:
        L.append(f"🔢 ترافیک واقعی همکاری: {_t(d['ham_real_gb'])} گیگ (÷ ضریب {d['mult']:g})")
    L.append(f"🧾 هزینه‌ی خرید حجم: {_t(d['cost'])} تومان")
    L.append(f"✅ سود تخمینی: {_t(d['income'] - d['cost'])} تومان")
    text = "\n".join(L)
    return _html.escape(text) if html else text


def _err(e: Exception, html: bool) -> str:
    log.exception("sales report")
    msg = f"❌ خطا در خواندن دیتابیس: {e}"
    return _html.escape(msg) if html else msg


def build_report(day: str, html: bool = True, partial: bool = False) -> str:
    try:
        d = compute(day, partial)
    except Exception as e:  # noqa: BLE001
        return _err(e, html)
    return _render(f"📈 گزارش فروش {jdate(day)}" + (" (تا این لحظه)" if partial else ""), d, html)


_JM = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def jmonths(n: int = 12) -> list[dict]:
    """n ماه شمسی اخیر (از ماه جاری به عقب) با اولین و آخرین روز هر ماه (میلادی)."""
    today = tehran_day()
    start, out = jmonth_start(today), []
    end = today
    for _ in range(n):
        y, m, _d = g2j(*map(int, start.split("-")))
        out.append({"label": f"{_JM[m - 1]} {y}".translate(_FA), "from": start, "to": end})
        end = shift_day(start, -1)
        start = jmonth_start(end)
    return out


def build_period_report(d1: str, d2: str, title: str = "", html: bool = True) -> str:
    """گزارش بازه‌ی دلخواه (روزهای میلادی، شامل هر دو سر بازه)."""
    today = tehran_day()
    if d1 > d2:
        d1, d2 = d2, d1
    d2 = min(d2, today)
    try:
        d = compute_range(d1, d2, d2 >= today)
    except Exception as e:  # noqa: BLE001
        return _err(e, html)
    tail = " (تا این لحظه)" if d2 >= today else ""
    return _render(f"{title or '📆 گزارش بازه'}\n{jdate(d1, False)} تا {jdate(d2, False)}{tail}", d, html)


def build_range_report(kind: str, html: bool = True) -> str:
    """kind: week = ۷ روز اخیر | month = ماه شمسی جاری | lmonth = ماه شمسی گذشته (کامل)"""
    today = tehran_day()
    if kind == "week":
        return build_period_report(shift_day(today, -6), today, "📅 گزارش ۷ روز اخیر", html)
    if kind == "lmonth":
        m = jmonths(2)[1]
        return build_period_report(m["from"], m["to"], f"🗓 گزارش {m['label']}", html)
    return build_period_report(jmonth_start(today), today, "🗓 گزارش ماه جاری", html)


def summary(kind: str = "today") -> dict:
    """خلاصه‌ی عددی فروش برای کارت‌های صفحه‌ی خانه (today | yday | week | month | lmonth)."""
    today = tehran_day()
    if kind == "yday":
        d1 = d2 = shift_day(today, -1)
    elif kind == "week":
        d1, d2 = shift_day(today, -6), today
    elif kind == "month":
        d1, d2 = jmonth_start(today), today
    elif kind == "lmonth":
        m = jmonths(2)[1]
        d1, d2 = m["from"], m["to"]
    else:
        d1 = d2 = today
    d = compute_range(d1, d2, d2 >= today)
    cl = d["cl"]
    cnt = lambda c, i: sum(n for k, n in c.items() if k[i] == "new")  # noqa: E731
    news = cnt(cl["ham"], 1) + cnt(cl["single"], 2) + cnt(cl["extra"], 2)
    total = sum(cl["ham"].values()) + sum(cl["single"].values()) + sum(cl["extra"].values())
    agg: dict = {}
    for (gb, m, _k), n in cl["extra"].items():
        agg[(gb, m)] = agg.get((gb, m), 0) + n
    extra = [{"gb": gb, "months": m, "count": n, "suggest": int(round(gb * d["rate"]))} for (gb, m), n in sorted(agg.items())]
    att = sum(n for c, i in ((cl["ham"], 1), (cl["single"], 2), (cl["extra"], 2)) for k, n in c.items() if k[i] in ("attach", "next"))
    return {"wallet": int(d.get("wallet", 0)), "income": int(round(d["income"])), "cost": int(round(d["cost"])), "profit": int(round(d["income"] - d["cost"])),
            "new": news, "renew": total - news, "attach": att, "skipped": sum(cl["skipped"].values()), "ham_gb": d["ham_gb"], "wallet_gb": d.get("wallet_gb", 0), "ignored": sum(cl["ignored"].values()), "extra": extra}


def set_plan(gb: int, months: int, price: int) -> None:
    """افزودن/ویرایش یک پلن در لیست قیمت تکی."""
    def mut(c):
        lst = [p for p in c["sales"]["single_prices"] if not (int(p["gb"]) == gb and int(p["months"]) == months)]
        lst.append({"gb": gb, "months": months, "price": price})
        c["sales"]["single_prices"] = lst
    from common import update_config
    update_config(mut)


def resolve_range(day: str = "today", d_from: str = "", d_to: str = "") -> tuple[str, str]:
    """انتخاب گزارش (today|yesterday|week|month|lmonth|YYYY-MM-DD یا from/to) ← (روز شروع، روز پایان) میلادی. خطا: ValueError."""
    today = tehran_day()
    if d_from and d_to:
        shift_day(d_from, 0); shift_day(d_to, 0)
        a, b = sorted((d_from, d_to))
        return a, min(b, today)
    if day == "week":
        return shift_day(today, -6), today
    if day == "month":
        return jmonth_start(today), today
    if day == "lmonth":
        m = jmonths(2)[1]
        return m["from"], m["to"]
    d = {"today": today, "yesterday": shift_day(today, -1)}.get(day, day)
    shift_day(d, 0)
    return d, d


def events_list(d1: str, d2: str, limit: int = 300) -> list[dict]:
    """تک‌تک فروش‌های بازه (جدیدترین اول) برای صفحه‌ی ویرایش گزارش؛ شامل حذف‌شده‌ها تا قابل برگشت باشند."""
    today = tehran_day()
    cfg = load_config()["sales"]
    rows = _fetch(*_bounds_days(d1, d2, d2 >= today), include_hidden=True)
    per_gb, rate, skip_extra = int(cfg["hamkari_per_gb"]), fallback_rate(cfg), cfg.get("attach_extra_mode", "skip") == "skip"
    out = []
    for r in reversed(rows[-limit:]):
        gb = round((r.get("data_limit") or 0) / GB)
        m = int(r["months"]) if r.get("months") else _months(r)
        if _is_hamkari(r, cfg):
            typ, price = "ham", gb * per_gb
        elif _is_ignored(r, cfg):
            typ, price = "ignored", 0
        else:
            typ = "single"
            hit = next((p for p in cfg["single_prices"] if int(p["gb"]) == gb and int(p["months"]) == m), None)
            if hit:
                price = int(hit["price"])
            elif r["kind"] in ("attach", "next") and skip_extra:
                price = 0
            else:
                price = int(round(gb * rate))
        ts = r.get("ts") or 0
        local = datetime.fromtimestamp(ts, TEHRAN) if ts else None
        out.append({
            "eid": r["eid"], "kind": r["kind"], "kind_fa": _KIND_FA.get(r["kind"], r["kind"]), "username": r["username"],
            "gb": gb, "months": m, "type": typ, "service": r.get("service_name") or "", "price": price if gb > 0 else 0,
            "time": local.strftime("%H:%M") if local else "", "day": jdate(local.strftime("%Y-%m-%d"), False) if local else "",
            "hidden": bool(r.get("hidden")), "edited": bool(r.get("edited")), "orig": r.get("orig"),
        })
    return out


# ───────────────────────────── سازگاری با bot.py (بدون پولینگ) ─────────────────────────────
async def poll_all():
    return None  # دیگر لازم نیست؛ گزارش مستقیم از دیتابیس خوانده می‌شود


async def watch_loop():
    return None  # ردیاب دوره‌ای حذف شد (مصرف CPU/RAM صفر)


async def report_loop(bot):
    """هر شب ۰۰:۰۰ تهران گزارش روز قبل را برای ادمین‌ها می‌فرستد؛ بین دو گزارش کاملاً خواب است."""
    while True:
        now = datetime.now(TEHRAN)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        await asyncio.sleep(max(1, (nxt - now).total_seconds()))
        cfg = load_config()
        if not cfg["sales"].get("enabled", True):
            continue
        day = shift_day(tehran_day(), -1)
        text = await asyncio.to_thread(build_report, day, True, False)
        for uid in cfg["admin_ids"]:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("send report to %s: %s", uid, e)


BACKUP_FORMAT = "rebecca-full-backup"
BACKUP_HOUR = 12          # ساعت ارسال بکاپ اصلی (به وقت تهران)


def attach_import(items: list) -> int:
    """لاگ پیوست/پلن بعدی (برای محاسبه‌ی فروش) را هنگام بازیابی جایگزین می‌کند."""
    clean = [x for x in items if isinstance(x, dict) and "ts" in x and "gb" in x]
    with _ATTACH_LOCK:
        path = _attach_file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False)
        os.replace(tmp, path)
    return len(clean)


def full_backup_bytes() -> bytes:
    """بکاپ اصلی: همه‌ی تنظیمات (پنل‌ها، ربات، قیمت‌ها، دیتابیس، SSL، رمز وب‌پنل)، نماینده‌ها و کاربرهایشان + لاگ فروش."""
    cfg = json.loads(config_backup_bytes().decode("utf-8"))
    payload = {
        "format": BACKUP_FORMAT, "version": 2,
        "created": datetime.now(TEHRAN).isoformat(timespec="seconds"),
        "config": cfg,
        "attach_log": _load_attach(),
        "sales_overrides": _load_ovr(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def parse_backup(text: str) -> tuple[dict, list | None]:
    """فایل بکاپ (کامل یا config.json قدیمی) ← (کانفیگ، لاگ فروش یا None). در خطا ValueError."""
    data = json.loads(text)
    if isinstance(data, dict) and data.get("format") == BACKUP_FORMAT:
        cfg = data.get("config")
        if not isinstance(cfg, dict):
            raise ValueError("بکاپ ناقص است (بخش تنظیمات ندارد).")
        att = data.get("attach_log")
        return cfg, att if isinstance(att, list) else None
    if not isinstance(data, dict):
        raise ValueError("این فایل شبیه بکاپ ربات نیست.")
    return data, None


def _backup_fingerprint() -> str:
    """اثر انگشت محتوای بکاپ (بدون زمان ساخت)؛ برای ارسال نکردنِ بکاپ تکراری وقتی چیزی عوض نشده."""
    import hashlib
    d = json.loads(full_backup_bytes().decode("utf-8"))
    d.pop("created", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _backup_state_file() -> str:
    from common import CONFIG_FILE
    return os.path.join(os.path.dirname(CONFIG_FILE), "backup_state.json")


def _load_backup_state() -> dict:
    try:
        with open(_backup_state_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_backup_state(iv: int, slot: int, **extra) -> None:
    try:
        path = _backup_state_file()
        tmp = path + ".tmp"
        st = _load_backup_state()
        st.update({"iv": iv, "slot": slot}, **extra)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("backup state save: %s", e)


def _backup_slot(iv: int) -> int:
    """شماره‌ی «بازه‌ی زمانی» فعلی به وقت تهران. هر بازه فقط یک‌بار بکاپ می‌گیرد.
    روزانه: هر روز از ساعت ۱۲:۰۰ ؛ کمتر از روزانه: بازه‌های ثابت N دقیقه‌ای از ساعت ۰۰:۰۰ (مثلاً هر ۱ ساعت = سر هر ساعت)."""
    now = datetime.now(TEHRAN)
    local = now.timestamp() + now.utcoffset().total_seconds()
    if iv >= 1440:
        return int((local - BACKUP_HOUR * 3600) // 86400)
    return int(local // (iv * 60))


async def backup_loop(bot):
    """بکاپ اصلی را طبق زمان‌بندی ثابت به ادمین‌ها می‌فرستد (چه تغییری شده باشد چه نه).
    interval_min = ۱۴۴۰ ← روزانه ساعت ۱۲:۰۰ تهران؛ کمتر ← سر ساعتِ مضرب N دقیقه (مثلاً ۶۰ دقیقه: ۱۰:۰۰، ۱۱:۰۰، ...).
    آخرین بازه‌ی ارسال‌شده روی دیسک ذخیره می‌شود؛ پس ریستارت ربات زمان‌بندی را به‌هم نمی‌زند و اگر ربات
    موقع بکاپ خاموش بود، بعد از روشن شدن همان بکاپ جا‌مانده فرستاده می‌شود."""
    retry_after = 0.0
    while True:
        await asyncio.sleep(20)
        try:
            cfg = load_config()
            bc = cfg["backup"]
            if not (bc["enabled"] and cfg["admin_ids"]):
                continue
            iv = int(bc.get("interval_min", 1440))
            iv = 1440 if iv >= 1440 else max(5, iv)
            cur = _backup_slot(iv)
            st = await asyncio.to_thread(_load_backup_state)
            if st.get("iv") != iv or not isinstance(st.get("slot"), int):
                # اولین اجرا یا تغییر زمان‌بندی: از بازه‌ی بعدی شروع می‌شود (بدون ارسال فوری)
                await asyncio.to_thread(_save_backup_state, iv, cur)
                continue
            if cur <= st["slot"] or _time.time() < retry_after:
                continue
            cap = ("💾 بکاپ اصلی روزانه" if iv >= 1440 else f"💾 بکاپ خودکار (هر {iv} دقیقه)") + \
                " — شامل همه‌ی تنظیمات، نماینده‌ها، موجودی‌ها و تراکنش‌ها؛ شامل توکن و رمزهاست، جای امن نگه دارید."
            if await send_backup(bot, cfg["admin_ids"], cap):
                await asyncio.to_thread(_save_backup_state, iv, cur)
                retry_after = 0.0
            else:
                if cfg.get("smart_alerts", {}).get("backup_failed", True):
                    for uid in cfg["admin_ids"]:
                        try:
                            await bot.send_message(uid, "⚠️ ارسال بکاپ ربکا ناموفق بود؛ ربات تا ۲ دقیقه دیگر دوباره تلاش می‌کند.")
                        except Exception:
                            pass
                retry_after = _time.time() + 120           # ارسال ناموفق: ۲ دقیقه بعد دوباره
        except Exception as e:  # noqa: BLE001
            log.warning("backup loop: %s", e)
            try:
                cfg = load_config()
                if cfg.get("smart_alerts", {}).get("backup_failed", True) and cfg.get("admin_ids") and _time.time() >= retry_after:
                    for uid in cfg["admin_ids"]:
                        try:
                            await bot.send_message(uid, f"⚠️ خطا در فرایند بکاپ ربکا: {esc(str(e)[:500])}")
                        except Exception:
                            pass
                    retry_after = _time.time() + 120
            except Exception:
                pass


async def send_backup(bot, admin_ids, caption: str) -> int:
    """بکاپ اصلی را به ادمین‌ها می‌فرستد؛ تعداد ارسال موفق را برمی‌گرداند."""
    import io
    try:
        data = await asyncio.to_thread(full_backup_bytes)
    except (OSError, ValueError) as e:
        log.warning("backup read: %s", e)
        return 0
    name = f"rebecca-full-backup-{tehran_day()}.json"
    try:
        b = backup_summary()
        caption += f"\n👥 {b['reps']} نماینده · {b['rep_users']} کاربر تخصیص‌یافته در این بکاپ است."
        caption += f"\n💰 مجموع موجودی نماینده‌ها: {sum(r['balance'] for r in load_config()['reps'].values()):,} تومان"
    except Exception:  # noqa: BLE001
        pass
    sent = 0
    for uid in admin_ids:
        try:
            bio = io.BytesIO(data)
            bio.name = name
            await bot.send_document(uid, bio, caption=caption)
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("send backup to %s: %s", uid, e)
    if sent:                                                   # آخرین بکاپِ واقعاً ارسال‌شده؛ صفحه‌ی «سلامت سیستم» از آن می‌خواند
        st = _load_backup_state()
        _save_backup_state(int(st.get("iv") or 0), int(st.get("slot") or 0), last_ok=_time.time())
    return sent


def backup_last_ok() -> float:
    return float(_load_backup_state().get("last_ok") or 0)


def db_check() -> str:
    """ساختار واقعی جدول‌ها را برای تطبیق schema نشان می‌دهد."""
    s = _schema()
    out = []
    conn = _connect()
    try:
        with conn.cursor() as c:
            for t in (s["users"], s["resets"], s["services"]):
                try:
                    c.execute(f"SHOW COLUMNS FROM {_ident(t)}")
                    out.append(f"✅ {t}: " + ", ".join(r["Field"] for r in c.fetchall()))
                except Exception as e:  # noqa: BLE001
                    out.append(f"❌ {t}: {e}")
            c.execute("SHOW TABLES")
            out.append("جدول‌ها: " + ", ".join(list(r.values())[0] for r in c.fetchall()))
    finally:
        conn.close()
    return "\n".join(out)


def build_events(day: str, html: bool = True, partial: bool = False, limit: int = 80) -> str:
    """فهرست تک‌تک رویدادها (ساخت/تمدید) برای تطبیق دقیق گزارش با واقعیت."""
    try:
        cfg = load_config()["sales"]
        rows = fetch_events(day, partial)
    except Exception as e:  # noqa: BLE001
        msg = f"❌ خطا در خواندن دیتابیس: {e}"
        return _html.escape(msg) if html else msg
    off = int(cfg.get("db_tz_offset_minutes", 0))
    L = [f"🔎 جزئیات رویدادهای {day} ({len(rows)} مورد)", ""]
    for r in rows[:limit]:
        at = r["at"]
        if isinstance(at, datetime):
            at = (at - timedelta(minutes=off)).replace(tzinfo=timezone.utc).astimezone(TEHRAN).strftime("%H:%M")
        gb = round((r["data_limit"] or 0) / GB)
        if _is_hamkari(r, cfg):
            typ = "همکاری"
        elif _is_ignored(r, cfg):
            typ = f"نادیده ({r.get('service_name')})"
        else:
            m = int(r["months"]) if r.get("months") else _months(r)
            inlist = any(int(p["gb"]) == gb and int(p["months"]) == m for p in cfg["single_prices"])
            typ = f"تکی {m}ماهه" + ("" if inlist else (" (خارج لیست — محاسبه نمی‌شود)" if r["kind"] in ("attach", "next") and cfg.get("attach_extra_mode", "skip") == "skip" else " (خارج لیست)"))
        L.append(f"{at} | {_KIND_FA.get(r['kind'], r['kind'])} | {r['username']} | {gb}GB | {typ}")
    if len(rows) > limit:
        L.append(f"… و {len(rows) - limit} مورد دیگر")
    text = "\n".join(L)
    return _html.escape(text) if html else text
