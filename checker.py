#!/usr/bin/env python3
"""مانیتور سلامت کانفیگ‌ها (Proxy Checker).

کانفیگ‌ها را از یکی از این راه‌ها می‌گیرد (می‌شود هر سه را با هم داد):
  ۱) یک کاربر تستیِ داخل پنل ربکا (همان کاربری که لینک‌هایش را به مشتری می‌دهید)
  ۲) یک لینک ساب (subscription) دلخواه
  ۳) لینک‌های خام (vless:// vmess:// trojan:// ss://) که دستی چسبانده شده

هر کانفیگ را سه‌مرحله‌ای می‌سنجد: TCP connect ← (اگر TLS/Reality بود) TLS handshake ← ثبت تأخیر.
بعد از N بررسی ناموفق پیاپی به ادمین‌ها در تلگرام هشدار می‌دهد و هنگام برگشت هم خبر می‌دهد.

نکته: بررسی از روی سروری انجام می‌شود که ربات روی آن است؛ پس «زنده بودن سرور/پورت» را می‌سنجد،
نه اینکه آن کانفیگ داخل ایران فیلتر شده یا نه. حلقه‌ی هشدار فقط داخل پروسه‌ی ربات اجرا می‌شود؛
«بررسی الان» وب‌پنل فقط نتیجه را در فایل وضعیت به‌روز می‌کند و شمارنده‌ی هشدار را دست نمی‌زند.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import ssl
import time
from urllib.parse import parse_qs, unquote, urlsplit

import common as C

log = logging.getLogger("rebecca.checker")

SCHEMES = ("vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic")
UDP_SCHEMES = ("hysteria2", "hy2", "tuic")          # با TCP نمی‌شود سنجید؛ بررسی نمی‌شوند (هشدار اشتباه نمی‌دهیم)
HISTORY_MAX = 48                                     # آخرین ۴۸ نتیجه برای درصد آپتایم
CONTROL_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))   # اگر خودِ سرور ربات اینترنت نداشته باشد، هشدار الکی نمی‌دهیم

_run_lock: asyncio.Lock | None = None


# ───────────────────────────── تنظیمات ─────────────────────────────
DEFAULTS = {"enabled": True, "interval": 300, "timeout": 6, "fails": 2, "alerts": True,
            "panel": "", "username": "", "sub_url": "", "links": "", "ignore": ""}


def get_cfg(cfg: dict | None = None) -> dict:
    """تنظیمات چکر را با پیش‌فرض‌ها و محدوده‌ی امن برمی‌گرداند."""
    raw = (cfg or C.load_config_ro()).get("checker")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULTS if k in raw})

    def _i(v, lo, hi, dv):
        try:
            return min(hi, max(lo, int(v)))
        except (TypeError, ValueError):
            return dv
    out["enabled"] = bool(out["enabled"])
    out["alerts"] = bool(out["alerts"])
    out["interval"] = _i(out["interval"], 60, 3600, 300)
    out["timeout"] = _i(out["timeout"], 2, 20, 6)
    out["fails"] = _i(out["fails"], 1, 10, 2)
    for k in ("panel", "username"):
        out[k] = str(out[k] or "").strip()[:64]
    out["sub_url"] = str(out["sub_url"] or "").strip()[:2000]
    out["links"] = str(out["links"] or "")[:60000]
    out["ignore"] = str(out["ignore"] or "")[:1000]
    return out


def has_source(c: dict) -> bool:
    return bool((c["panel"] and c["username"]) or c["sub_url"] or c["links"].strip())


# ───────────────────────────── پارس لینک‌ها ─────────────────────────────
def _b64(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _hostport(netloc: str) -> tuple[str, int] | None:
    """از قسمت host:port (با پشتیبانی از IPv6 و user@) آدرس و پورت را درمی‌آورد."""
    hp = netloc.rsplit("@", 1)[-1]
    m = re.fullmatch(r"\[([^\]]+)\]:(\d{1,5})", hp) or re.fullmatch(r"([^:\[\]]+):(\d{1,5})", hp)
    if not m:
        return None
    port = int(m.group(2))
    return (m.group(1), port) if 0 < port < 65536 else None


def parse_link(link: str) -> dict | None:
    """یک لینک کانفیگ را به {proto, host, port, name, security, network, sni} تبدیل می‌کند؛ نامعتبر = None."""
    link = link.strip()
    m = re.match(r"([a-z0-9]+)://(.+)", link, re.I | re.S)
    if not m or m.group(1).lower() not in SCHEMES:
        return None
    proto, rest = m.group(1).lower(), m.group(2)
    try:
        if proto == "vmess":
            d = json.loads(_b64(rest.split("#", 1)[0]).decode("utf-8", "replace"))
            host, port = str(d.get("add") or "").strip(), int(d.get("port") or 0)
            if not host or not 0 < port < 65536:
                return None
            tls = str(d.get("tls") or "").lower()
            return {"proto": proto, "host": host, "port": port, "name": str(d.get("ps") or f"{host}:{port}"),
                    "security": "tls" if tls == "tls" else "", "network": str(d.get("net") or "tcp"),
                    "sni": str(d.get("sni") or d.get("host") or "")}
        if proto == "ss" and "@" not in rest.split("#", 1)[0]:      # شکل قدیمی: ss://base64(method:pass@host:port)#name
            body, _, frag = rest.partition("#")
            dec = _b64(body.split("?", 1)[0]).decode("utf-8", "replace")
            hp = _hostport(dec)
            if not hp:
                return None
            return {"proto": proto, "host": hp[0], "port": hp[1], "name": unquote(frag) or f"{hp[0]}:{hp[1]}",
                    "security": "", "network": "tcp", "sni": ""}
        sp = urlsplit(link)
        hp = _hostport(sp.netloc)
        if not hp:
            return None
        q = {k: v[0] for k, v in parse_qs(sp.query).items() if v}
        return {"proto": proto, "host": hp[0], "port": hp[1], "name": unquote(sp.fragment) or f"{hp[0]}:{hp[1]}",
                "security": (q.get("security") or ("tls" if proto == "trojan" else "")).lower(),
                "network": (q.get("type") or "tcp").lower(), "sni": q.get("sni") or q.get("host") or ""}
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeError):
        return None


def parse_many(text: str) -> list[dict]:
    """متنی که چند لینک دارد (یا کل بدنه‌ی ساب، حتی base64) را به فهرست کانفیگ‌های یکتا تبدیل می‌کند."""
    text = text.strip()
    if text and "://" not in text.split("\n", 1)[0]:
        try:                                                        # بدنه‌ی ساب معمولاً base64 است
            dec = _b64("".join(text.split())).decode("utf-8", "replace")
            if "://" in dec:
                text = dec
        except (ValueError, binascii.Error):
            pass
    out, seen = [], set()
    for ln in re.split(r"[\r\n]+", text):
        it = parse_link(ln)
        if not it:
            continue
        key = (it["proto"], it["host"].lower(), it["port"], it["name"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def cfg_key(it: dict) -> str:
    return f"{it['proto']}|{it['host'].lower()}|{it['port']}|{it['name']}"


# ───────────────────────────── گرفتن کانفیگ‌ها ─────────────────────────────
async def _fetch_sub(url: str, timeout: float) -> str:
    import httpx                                                    # همان وابستگی پروژه؛ فقط وقتی لازم شد بارگذاری می‌شود
    async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "v2rayN/6.0"}) as cl:
        r = await cl.get(url)
        r.raise_for_status()
        return r.text


async def load_configs(c: dict) -> tuple[list[dict], list[str]]:
    """فهرست کانفیگ‌ها + خطاهای منبع‌ها. اگر یک منبع خراب بود، بقیه کار می‌کنند."""
    texts, errs = [], []
    if c["panel"] and c["username"]:
        try:
            api = C.get_api(c["panel"])
            u = await api.request("GET", api.upath(c["username"]))
            links = u.get("links")
            if isinstance(links, list) and links:
                texts.append("\n".join(str(x) for x in links))
            elif u.get("subscription_url"):
                texts.append(await _fetch_sub(api.full_sub(u["subscription_url"]), c["timeout"] + 6))
            else:
                errs.append("کاربر تست لینکی ندارد (غیرفعال/منقضی است؟)")
        except Exception as e:  # noqa: BLE001
            errs.append(f"کاربر تست: {e or e.__class__.__name__}")
    if c["sub_url"]:
        try:
            texts.append(await _fetch_sub(c["sub_url"], c["timeout"] + 6))
        except Exception as e:  # noqa: BLE001
            errs.append(f"لینک ساب: {e or e.__class__.__name__}")
    if c["links"].strip():
        texts.append(c["links"])
    items = parse_many("\n".join(texts))
    words = [w.strip().lower() for w in re.split(r"[,\n،]+", c["ignore"]) if w.strip()]
    if words:
        items = [i for i in items if not any(w in i["name"].lower() for w in words)]
    return items, errs


# ───────────────────────────── سنجش ─────────────────────────────
async def _tcp(host: str, port: int, timeout: float):
    t0 = time.perf_counter()
    r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    return r, w, (time.perf_counter() - t0) * 1000


async def _probe_once(it: dict, timeout: float) -> dict:
    host, port = it["host"], it["port"]
    try:
        r, w, ms = await _tcp(host, port, timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "stage": "tcp", "err": "تایم‌اوت اتصال"}
    except OSError as e:
        return {"ok": False, "stage": "tcp", "err": (e.strerror or e.__class__.__name__)[:60]}
    try:
        if it.get("security") in ("tls", "reality", "xtls"):
            # هندشیک TLS با SNI (بدون verify گواهی). روی Reality هم سرور واقعی جواب هندشیک می‌دهد.
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            w.close()
            try:
                await w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            sni = it.get("sni") or (host if not re.fullmatch(r"[0-9.:]+", host) else None)
            t1 = time.perf_counter()
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni), timeout)
            except asyncio.TimeoutError:
                return {"ok": False, "stage": "tls", "err": "تایم‌اوت هندشیک TLS"}
            except (ssl.SSLError, OSError) as e:
                return {"ok": False, "stage": "tls", "err": (getattr(e, "reason", None) or e.__class__.__name__)[:60]}
            ms = (time.perf_counter() - t1) * 1000
            return {"ok": True, "stage": "tls", "ms": round(ms)}
        return {"ok": True, "stage": "tcp", "ms": round(ms)}
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def probe(it: dict, timeout: float) -> dict:
    """یک بار تلاش مجدد می‌کند تا قطعیِ لحظه‌ای (packet loss) هشدار الکی نسازد."""
    res = await _probe_once(it, timeout)
    if not res["ok"]:
        await asyncio.sleep(1)
        res2 = await _probe_once(it, timeout)
        if res2["ok"]:
            return res2
    return res


async def internet_ok(timeout: float = 4) -> bool:
    """اگر خودِ سرور ربات اینترنت ندارد، همه‌ی کانفیگ‌ها «قطع» دیده می‌شوند؛ این حالت را تشخیص می‌دهیم."""
    async def one(h, p):
        try:
            _, w, _ = await _tcp(h, p, timeout)
            w.close()
            return True
        except Exception:  # noqa: BLE001
            return False
    return any(await asyncio.gather(*(one(h, p) for h, p in CONTROL_HOSTS)))


# ───────────────────────────── وضعیت ─────────────────────────────
def _state_file() -> str:
    return os.path.join(os.path.dirname(C.CONFIG_FILE) or ".", "checker_state.json")


def load_state() -> dict:
    try:
        with open(_state_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    path = _state_file()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("checker state save: %s", e)


async def run_check(*, alerts: bool = False) -> dict:
    """یک دور کامل بررسی. alerts=True فقط از حلقه‌ی ربات: شمارنده‌ی خطا و وضعیت هشدار را به‌روز می‌کند و رویدادها را برمی‌گرداند.

    خروجی: {"events": [...], "skipped": "..."} و وضعیت در checker_state.json ذخیره می‌شود."""
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    async with _run_lock:
        c = get_cfg()
        st = load_state()
        now = time.time()
        if not has_source(c):
            st.update(last_run=now, error="هیچ منبعی برای کانفیگ‌ها تنظیم نشده", items={})
            await asyncio.to_thread(save_state, st)
            return {"events": [], "skipped": "no_source"}
        items, errs = await load_configs(c)
        st["source_errors"] = errs
        if not items:
            st.update(last_run=now, error="; ".join(errs) or "هیچ کانفیگ معتبری پیدا نشد")
            await asyncio.to_thread(save_state, st)
            return {"events": [], "skipped": "no_items"}      # منبع خراب = نتیجه‌ی قبلی را نگه می‌داریم، چیزی «قطع» نمی‌شود
        if not await internet_ok():
            st.update(last_run=now, error="سرور ربات اینترنت ندارد؛ بررسی رد شد")
            await asyncio.to_thread(save_state, st)
            return {"events": [], "skipped": "no_internet"}
        sem = asyncio.Semaphore(20)

        async def one(it):
            if it["proto"] in UDP_SCHEMES:
                return it, {"ok": None, "stage": "udp", "err": "UDP؛ بررسی نمی‌شود"}
            async with sem:
                return it, await probe(it, c["timeout"])

        results = await asyncio.gather(*(one(i) for i in items))
        old, new, events = st.get("items") or {}, {}, []
        for it, res in results:
            k = cfg_key(it)
            o = old.get(k, {})
            rec = {"name": it["name"], "proto": it["proto"], "host": it["host"], "port": it["port"],
                   "security": it["security"], "network": it["network"],
                   "ok": res["ok"], "ms": res.get("ms"), "stage": res["stage"], "err": res.get("err", ""),
                   "last_check": now, "last_ok": now if res["ok"] else o.get("last_ok", 0),
                   "fails": o.get("fails", 0), "down_since": o.get("down_since", 0), "alerted": o.get("alerted", False),
                   "hist": (o.get("hist") or [])[-(HISTORY_MAX - 1):]}
            if res["ok"] is not None:
                rec["hist"].append(1 if res["ok"] else 0)
            if alerts and res["ok"] is not None:
                if res["ok"]:
                    if rec["alerted"]:
                        events.append({"type": "up", "rec": rec, "down_for": now - (rec["down_since"] or now)})
                    rec.update(fails=0, down_since=0, alerted=False)
                else:
                    rec["fails"] += 1
                    if rec["fails"] == 1:
                        rec["down_since"] = now
                    if rec["fails"] >= c["fails"] and not rec["alerted"]:
                        rec["alerted"] = True
                        events.append({"type": "down", "rec": rec})
            new[k] = rec
        st.update(items=new, last_run=now, error="; ".join(errs), interval=c["interval"])
        await asyncio.to_thread(save_state, st)
        return {"events": events if c["alerts"] else [], "skipped": ""}


# ───────────────────────────── نمایش ─────────────────────────────
def view(st: dict | None = None) -> dict:
    """فهرست مرتب‌شده برای وب‌پنل و ربات: اول خراب‌ها، بعد بر اساس نام."""
    st = st if st is not None else load_state()
    rows = []
    for rec in (st.get("items") or {}).values():
        h = rec.get("hist") or []
        rows.append({**{k: rec.get(k) for k in ("name", "proto", "host", "port", "security", "network", "ok", "ms", "stage", "err", "last_ok", "last_check", "fails", "down_since", "alerted")},
                     "uptime": round(100 * sum(h) / len(h)) if h else None})
    rows.sort(key=lambda r: (r["ok"] is not False, r["name"]))
    return {"items": rows, "last_run": st.get("last_run", 0), "error": st.get("error", ""),
            "down": sum(1 for r in rows if r["ok"] is False), "up": sum(1 for r in rows if r["ok"]), "total": len(rows)}


def _dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec >= 86400:
        return f"{sec // 86400} روز"
    if sec >= 3600:
        return f"{sec // 3600} ساعت"
    return f"{max(1, sec // 60)} دقیقه"


def format_status(v: dict) -> str:
    esc = C.esc
    if not v["total"]:
        return "🩺 هنوز نتیجه‌ای نیست؛ از وب‌پنل ← خانه ← «سلامت کانفیگ‌ها» منبع را تنظیم کنید."
    lines = [f"🩺 <b>سلامت کانفیگ‌ها</b> — 🟢 {v['up']} سالم · 🔴 {v['down']} خراب · کل {v['total']}"]
    for r in v["items"]:
        if r["ok"] is None:
            ic, tail = "⚪", "بررسی نمی‌شود (UDP)"
        elif r["ok"]:
            ic, tail = "🟢", f"{r['ms']}ms" + (f" · آپتایم {r['uptime']}٪" if r["uptime"] is not None else "")
        else:
            ic, tail = "🔴", esc(r["err"] or "قطع")
        lines.append(f"{ic} {esc(r['name'])} — {tail}")
    return "\n".join(lines)[:4000]


def format_events(events: list[dict]) -> str:
    esc, out = C.esc, []
    for e in events:
        r = e["rec"]
        where = f"<code>{esc(r['host'])}:{r['port']}</code>"
        if e["type"] == "down":
            out.append(f"🔴 کانفیگ <b>{esc(r['name'])}</b> از کار افتاد\n{where}\nدلیل: {esc(r['err'] or 'قطع')}")
        else:
            out.append(f"🟢 کانفیگ <b>{esc(r['name'])}</b> برگشت ({r['ms']}ms)\nمدت قطعی: {_dur(e['down_for'])}")
    return "\n\n".join(out)[:4000]


# ───────────────────────────── حلقه‌ی پس‌زمینه ─────────────────────────────
async def checker_loop(bot):
    """هر interval ثانیه همه‌ی کانفیگ‌ها را می‌سنجد و فقط روی تغییر وضعیت (خراب/سالم‌شدن) پیام می‌فرستد."""
    await asyncio.sleep(45)
    while True:
        c = get_cfg()
        if c["enabled"] and has_source(c):
            try:
                res = await run_check(alerts=True)
                ev = res["events"]
                if ev and C.load_config_ro()["admin_ids"]:
                    import ops
                    await ops.notify(bot, format_events(ev))
            except Exception:  # noqa: BLE001
                log.exception("checker_loop")
        await asyncio.sleep(get_cfg()["interval"])
