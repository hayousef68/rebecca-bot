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

from common import load_config, esc

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


def fetch_events(day: str, partial: bool) -> list[dict]:
    return _fetch(*_bounds_db(day, partial))


def fetch_events_range(d1: str, d2: str, partial: bool) -> list[dict]:
    return _fetch(*_bounds_days(d1, d2, partial))


def _fetch(a, b) -> list[dict]:
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
    # ریستی که بلافاصله (≤ ۲ دقیقه) بعد از ساخت همان کاربر ثبت شده، فروش جدا حساب نمی‌شود
    created = {r["username"]: r["at"] for r in rows if r["kind"] == "new" and isinstance(r["at"], datetime)}
    rows = [r for r in rows if not (r["kind"] == "renew" and r["username"] in created and isinstance(r["at"], datetime)
                                    and abs((r["at"] - created[r["username"]]).total_seconds()) <= 120)]
    rows.sort(key=lambda r: r["at"] if isinstance(r["at"], datetime) else datetime.min)
    return rows


# ───────────────────────────── طبقه‌بندی و قیمت ─────────────────────────────
def _is_hamkari(r: dict, cfg: dict) -> bool:
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
    ham, single, extra, ignored = Counter(), Counter(), Counter(), Counter()
    plans = cfg["single_prices"]
    for r in rows:
        gb = round((r["data_limit"] or 0) / GB)
        if gb <= 0:
            continue
        key = ("new" if r["kind"] == "new" else "renew")
        if _is_hamkari(r, cfg):          # همکاری: همیشه گیگی (حتی خارج از لیست)
            ham[(gb, key)] += 1
            continue
        if _is_ignored(r, cfg):          # تست و سرویس‌های غیرفروشی
            ignored[(gb, key)] += 1
            continue
        m = _months(r)
        match = next((p for p in plans if int(p["gb"]) == gb and int(p["months"]) == m), None)
        (single if match else extra)[(gb, m, key)] += 1   # ms خارج از لیست: به‌نرخ گیگی
    return {"ham": ham, "single": single, "extra": extra, "ignored": ignored}


def compute(day: str, partial: bool) -> dict:
    return compute_rows(fetch_events(day, partial))


def compute_range(d1: str, d2: str, partial: bool) -> dict:
    return compute_rows(fetch_events_range(d1, d2, partial))


def compute_rows(rows: list[dict]) -> dict:
    cfg = load_config()["sales"]
    cl = classify(rows, cfg)
    price = {(int(p["gb"]), int(p["months"])): int(p["price"]) for p in cfg["single_prices"]}
    rate = fallback_rate(cfg)
    ham_gb = sum(gb * n for (gb, _k), n in cl["ham"].items())
    ham_income = ham_gb * int(cfg["hamkari_per_gb"])
    single_income = sum(price[(gb, m)] * n for (gb, m, _k), n in cl["single"].items())
    single_gb = sum(gb * n for (gb, _m, _k), n in cl["single"].items())
    extra_gb = sum(gb * n for (gb, _m, _k), n in cl["extra"].items())
    extra_income = extra_gb * rate
    mult = float(cfg.get("hamkari_usage_multiplier", 1.5))
    per_gb_cost = int(cfg.get("buy_per_tb", 750000)) / float(cfg.get("tb_gb", 1000))
    cost = (ham_gb * mult + single_gb + extra_gb) * per_gb_cost
    return {"cl": cl, "ham_gb": ham_gb, "ham_income": ham_income, "single_income": single_income,
            "single_gb": single_gb, "extra_gb": extra_gb, "extra_income": extra_income, "rate": rate, "cost": cost,
            "income": ham_income + single_income + extra_income}


def _t(n) -> str:
    return f"{int(round(n)):,}".replace(",", "٬")


def _render(title: str, d: dict, html: bool) -> str:
    cl, L = d["cl"], []
    L.append(title)
    L.append("")
    L.append("🤝 همکاری (Hamkari)")
    if not cl["ham"]:
        L.append("  — فروشی نبود")
    for (gb, k), n in sorted(cl["ham"].items()):
        L.append(f"  {n} تا {gb} گیگ — {'ساخت' if k == 'new' else 'تمدید/ریست'}")
    L.append(f"  جمع: {_t(d['ham_gb'])} گیگ ← {_t(d['ham_income'])} تومان")
    L.append("")
    L.append("👤 فروش تکی (MS)")
    if not cl["single"]:
        L.append("  — فروشی نبود")
    for (gb, m, k), n in sorted(cl["single"].items()):
        L.append(f"  {n} تا {gb} گیگ {'یک' if m == 1 else 'دو'}ماهه — {'ساخت' if k == 'new' else 'تمدید/ریست'}")
    L.append(f"  جمع لیست: {_t(d['single_income'])} تومان")
    if cl["extra"]:
        L.append("")
        L.append(f"📐 تکی خارج از لیست (هر گیگ {_t(d['rate'])} تومان):")
        for (gb, m, k), n in sorted(cl["extra"].items()):
            L.append(f"  {n} تا {gb} گیگ {m} ماهه ← {_t(gb * n * d['rate'])}")
        L.append(f"  جمع: {_t(d['extra_income'])} تومان")
    if cl["ignored"]:
        L.append("")
        L.append(f"🚫 نادیده‌گرفته (تست/غیرفروشی): {sum(cl['ignored'].values())} مورد")
    L.append("")
    L.append(f"💰 جمع کل فروش: {_t(d['income'])} تومان")
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


def build_range_report(kind: str, html: bool = True) -> str:
    """kind: week = ۷ روز اخیر | month = از اول ماه شمسی تا همین لحظه"""
    today = tehran_day()
    if kind == "week":
        d1, title = shift_day(today, -6), "📅 گزارش ۷ روز اخیر"
    else:
        d1, title = jmonth_start(today), "🗓 گزارش ماه جاری"
    try:
        d = compute_range(d1, today, True)
    except Exception as e:  # noqa: BLE001
        return _err(e, html)
    return _render(f"{title}\n{jdate(d1, False)} تا {jdate(today, False)} (تا این لحظه)", d, html)


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
            m = _months(r)
            inlist = any(int(p["gb"]) == gb and int(p["months"]) == m for p in cfg["single_prices"])
            typ = f"تکی {m}ماهه" + ("" if inlist else " (خارج لیست)")
        L.append(f"{at} | {'ساخت' if r['kind'] == 'new' else 'تمدید'} | {r['username']} | {gb}GB | {typ}")
    if len(rows) > limit:
        L.append(f"… و {len(rows) - limit} مورد دیگر")
    text = "\n".join(L)
    return _html.escape(text) if html else text
