#!/usr/bin/env python3
"""عملیات سبک پس‌زمینه: مانیتور سلامت پنل‌ها و هشدار انقضا/اتمام حجم.

هر دو بین اجراها کاملاً خواب هستند (مانیتور هر ۵ دقیقه فقط یک درخواست کوچک به هر پنل؛ هشدار انقضا روزی یک بار).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta

import common as C
from sales import TEHRAN

log = logging.getLogger("rebecca.ops")


async def notify(bot, text: str):
    for uid in C.load_config()["admin_ids"]:
        try:
            await bot.send_message(uid, text[:4000], parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s: %s", uid, e)


# ───────────────────────────── انقضا / اتمام حجم ─────────────────────────────
def _left(sec: float) -> str:
    return f"{math.ceil(sec / 86400)} روز" if sec >= 86400 else f"{max(1, math.ceil(sec / 3600))} ساعت"


async def find_soon(api, days: int, low_pct: int) -> list[dict]:
    """کاربران فعالی که تا `days` روز دیگر منقضی می‌شوند یا حجم باقی‌مانده‌شان کمتر از low_pct٪ است."""
    now, out = time.time(), []
    for u in await api.iter_users("active"):
        reasons, rank = [], 10 ** 12
        exp = int(u.get("expire") or 0)
        if exp > 0 and 0 < exp - now <= days * 86400:
            reasons.append(f"⏳ {_left(exp - now)} تا انقضا")
            rank = exp - now
        lim, used = int(u.get("data_limit") or 0), int(u.get("used_traffic") or 0)
        if lim > 0 and lim - used <= lim * low_pct / 100:
            reasons.append(f"📉 {max(0, lim - used) / C.GB:.1f}GB باقی")
            rank = min(rank, 10 ** 9)
        if reasons:
            out.append({"username": u["username"], "reason": " | ".join(reasons), "rank": rank, "user": u})
    out.sort(key=lambda x: x["rank"])
    return out


def format_soon(panel_name: str, items: list[dict], limit: int = 25) -> str:
    lines = [f"⏰ <b>{C.esc(panel_name)}</b> — {len(items)} کاربر در آستانه‌ی انقضا/اتمام حجم:"]
    lines += [f"• <code>{C.esc(i['username'])}</code> — {i['reason']}" for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"… و {len(items) - limit} مورد دیگر")
    return "\n".join(lines)


async def reminder_loop(bot):
    """هر روز در ساعت تنظیم‌شده (به وقت تهران) فهرست کاربران نزدیک به انقضا را برای ادمین‌ها می‌فرستد."""
    while True:
        rc = C.load_config()["reminders"]
        now = datetime.now(TEHRAN)
        nxt = now.replace(hour=min(23, rc["hour"]), minute=0, second=15, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        cfg = C.load_config()
        rc = cfg["reminders"]
        if not (rc["enabled"] and cfg["admin_ids"]):
            continue
        for p in cfg["panels"]:
            api = C.get_api(p["id"])
            if not api.is_configured():
                continue
            try:
                items = await find_soon(api, rc["days"], rc["low_pct"])
                if items:
                    await notify(bot, format_soon(p["name"], items))
            except Exception as e:  # noqa: BLE001
                log.warning("reminder %s: %s", p["id"], e)


# ───────────────────────────── مانیتور سلامت پنل ─────────────────────────────
async def monitor_loop(bot):
    """اگر پنلی دو بررسی پیاپی جواب ندهد هشدار می‌دهد و وقتی برگشت خبر می‌دهد (فقط روی تغییر وضعیت پیام می‌فرستد)."""
    state: dict[str, dict] = {}
    await asyncio.sleep(30)
    while True:
        cfg = C.load_config()
        mc = cfg["monitor"]
        if mc["enabled"]:
            for p in cfg["panels"]:
                api = C.get_api(p["id"])
                if not api.is_configured():
                    continue
                st = state.setdefault(p["id"], {"fails": 0, "down": False})
                try:
                    await asyncio.wait_for(api.request("GET", "/api/system", retries=0), 35)
                    st["fails"] = 0
                    if st["down"]:
                        st["down"] = False
                        if cfg.get("smart_alerts", {}).get("panel_down", True):
                            await notify(bot, f"🟢 پنل <b>{C.esc(p['name'])}</b> دوباره برقرار شد.")
                except Exception as e:  # noqa: BLE001
                    st["fails"] += 1
                    if st["fails"] >= 2 and not st["down"]:
                        st["down"] = True
                        if cfg.get("smart_alerts", {}).get("panel_down", True):
                            await notify(bot, f"🔴 پنل <b>{C.esc(p['name'])}</b> پاسخ نمی‌دهد.\n{C.esc(str(e) or e.__class__.__name__)}")
        await asyncio.sleep(max(60, mc["interval"]))


# ───────────────────────────── غیرفعال‌سازی خودکارِ «واریز نشده» ─────────────────────────────
async def unpaid_sweep(bot) -> int:
    """کاربرانی که مهلتِ «واریز نشده»شان تمام شده را غیرفعال می‌کند؛ تعداد غیرفعال‌شده‌ها را برمی‌گرداند."""
    cfg = C.load_config()
    now, done = time.time(), 0
    for pid, m in cfg["unpaid"]["pending"].items():
        due = [(u, r) for u, r in m.items() if r["due"] <= now]
        if not due:
            continue
        pn = C.get_panel(cfg, pid)
        if not pn:                                            # پنل حذف شده؛ صف بی‌صاحب پاک شود
            C.unpaid_clear(pid, [u for u, _ in due])
            continue
        api = C.get_api(pid)
        if not api.is_configured():
            continue
        for uname, _rec in due:
            try:
                u = await api.request("GET", api.upath(uname))
                if u.get("status") != "disabled":
                    await api.request("PUT", api.upath(uname), json={"status": "disabled"})
                    done += 1
                    await notify(bot, f"⛔ <code>{C.esc(uname)}</code> ({C.esc(pn['name'])}) — مهلتِ «واریز نشده» تمام شد؛ غیرفعال شد.")
                C.unpaid_clear(pid, uname)
            except C.RebeccaError as e:
                if e.status == 404:                           # کاربر دیگر وجود ندارد
                    C.unpaid_clear(pid, uname)
                else:
                    log.warning("unpaid %s/%s: %s", pid, uname, e)   # بار بعد دوباره تلاش می‌شود
            except Exception as e:  # noqa: BLE001
                log.warning("unpaid %s/%s: %s", pid, uname, e)
    return done


async def unpaid_loop(bot):
    """هر ۶۰ ثانیه صف «واریز نشده» را بررسی می‌کند (وقتی صف خالی است هیچ درخواستی به پنل نمی‌زند)."""
    await asyncio.sleep(20)
    while True:
        try:
            await unpaid_sweep(bot)
        except Exception:  # noqa: BLE001
            log.exception("unpaid_loop")
        await asyncio.sleep(60)


# ───────────────────────────── ردیاب اختلاف مالی (تطابق کیف پول‌ها) ─────────────────────────────
# قاعده‌ها (بدون نیاز به سابقه‌ی کامل؛ لاگ هر نماینده فقط ۱۰۰ رکورد آخر را نگه می‌دارد):
#  ۱) هر رکورد لاگ: موجودیِ بعد از آن = موجودیِ رکورد قبلی + مبلغ همان رکورد.
#  ۲) موجودیِ بعد از آخرین رکورد لاگ باید برابر موجودی فعلی نماینده باشد.
#  ۳) موجودی منفی یا بیش از سقف مجاز، و موجودیِ بدون هیچ سابقه (مثل بعد از بازیابی ناقص).
#  ۴) نسبت به بررسی قبلی: برگشت زمانی داده (جدیدترین تراکنش قدیمی‌تر شده) یا حذف نماینده.
_LVL = {"warn": 1, "bad": 2}


def _recon_state_file() -> str:
    return os.path.join(os.path.dirname(C.CONFIG_FILE) or ".", "recon_state.json")


def recon_load_state() -> dict:
    try:
        with open(_recon_state_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def recon_save_state(st: dict) -> None:
    path = _recon_state_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("recon state save: %s", e)


def recon_snapshot(cfg: dict) -> dict:
    reps = cfg.get("reps") or {}
    latest = 0
    for r in reps.values():
        for e in r.get("log", []):
            latest = max(latest, int(e.get("ts") or 0))
    return {"ts": time.time(), "latest_log_ts": latest,
            "total": sum(int(r.get("balance") or 0) for r in reps.values()),
            "reps": {rid: {"name": r.get("label") or r.get("name") or rid, "balance": int(r.get("balance") or 0)} for rid, r in reps.items()}}


def reconcile(cfg: dict, prev: dict | None = None) -> list[dict]:
    """بررسی تطابق مالی. خروجی: فهرست مشکل‌ها [{level, rid, name, code, text}] (خالی = همه چیز می‌خواند)."""
    out: list[dict] = []

    def add(level, rid, name, code, text):
        out.append({"level": level, "rid": rid, "name": name, "code": code, "text": text})

    for rid, r in (cfg.get("reps") or {}).items():
        name = r.get("label") or r.get("name") or rid
        bal = int(r.get("balance") or 0)
        lg = [e for e in (r.get("log") or []) if isinstance(e, dict)]
        if bal < 0:
            add("bad", rid, name, "negative", f"موجودی منفی: {bal:,}")
        if bal > C.WALLET_MAX:
            add("bad", rid, name, "over_cap", "موجودی از سقف مجاز بیشتر است")
        if not lg:
            if bal > 0:
                add("warn", rid, name, "no_history", f"موجودی {bal:,} بدون هیچ سابقه‌ی تراکنش (بعد از بازیابی/انتقال؟)")
            continue
        for a, b in zip(lg, lg[1:]):
            try:
                if int(b.get("balance")) != int(a.get("balance")) + int(b.get("amount")):
                    add("bad", rid, name, "chain", f"زنجیره‌ی تراکنش‌ها پاره است (رکورد {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(b.get('ts') or 0)))})")
                    break
            except (TypeError, ValueError):
                add("bad", rid, name, "chain", "رکورد تراکنش ناخوانا است")
                break
        try:
            last_bal = int(lg[-1].get("balance"))
            if last_bal != bal:
                d = bal - last_bal
                add("bad", rid, name, "mismatch", f"موجودی فعلی {bal:,} با آخرین تراکنش ({last_bal:,}) نمی‌خواند؛ اختلاف {d:+,}")
        except (TypeError, ValueError):
            pass

    if prev:
        snap = recon_snapshot(cfg)
        if snap["latest_log_ts"] < int(prev.get("latest_log_ts") or 0):
            add("bad", "", "کل سیستم", "rollback", "جدیدترین تراکنش از بررسی قبلی قدیمی‌تر شده؛ داده به گذشته برگشته (بکاپ قدیمی روی سرور؟)")
        for rid, old in (prev.get("reps") or {}).items():
            if rid not in snap["reps"]:
                add("warn", rid, old.get("name") or rid, "removed", f"نماینده حذف شده (موجودی قبلی {int(old.get('balance') or 0):,})")
    return out


def recon_fingerprint(issues: list[dict]) -> str:
    key = sorted((i["rid"], i["code"], i["text"]) for i in issues)
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest() if key else ""


def recon_summary(issues: list[dict], limit: int = 15) -> dict:
    lvl = max((_LVL[i["level"]] for i in issues), default=0)
    return {"ok": not issues, "level": {0: "ok", 1: "warn", 2: "bad"}[lvl], "count": len(issues), "issues": issues[:limit]}


def reconcile_now(save: bool = False) -> dict:
    """اجرای دستی/وب‌پنل. save=True وضعیت مقایسه‌ی بعدی را هم به‌روز می‌کند."""
    cfg = C.load_config()
    prev = recon_load_state()
    res = recon_summary(reconcile(cfg, prev.get("snap")))
    res["total"] = sum(int(r.get("balance") or 0) for r in cfg["reps"].values())
    res["reps"] = len(cfg["reps"])
    if save:
        st = dict(prev)
        st["snap"] = recon_snapshot(cfg)
        recon_save_state(st)
    return res


def format_recon(issues: list[dict], limit: int = 15) -> str:
    lines = [f"🧮 <b>اختلاف مالی کیف پول</b> — {len(issues)} مورد:"]
    for i in issues[:limit]:
        lines.append(f"{'🔴' if i['level'] == 'bad' else '🟡'} <b>{C.esc(i['name'])}</b>: {C.esc(i['text'])}")
    if len(issues) > limit:
        lines.append(f"… و {len(issues) - limit} مورد دیگر")
    lines.append("\nبرای جزئیات: وب‌پنل ← تنظیمات ← بکاپ ← «بررسی تطابق مالی»")
    return "\n".join(lines)


async def recon_loop(bot):
    """هر ۶ ساعت تطابق مالی را بررسی می‌کند؛ فقط وقتی مشکلِ جدید/متفاوت پیدا شود (یا برطرف شود) به ادمین‌ها پیام می‌دهد."""
    await asyncio.sleep(90)
    while True:
        try:
            cfg = C.load_config()
            st = recon_load_state()
            issues = await asyncio.to_thread(reconcile, cfg, st.get("snap"))
            fp = recon_fingerprint(issues)
            alerts_on = cfg.get("smart_alerts", {}).get("finance_mismatch", True)
            if fp != st.get("fp", ""):
                if alerts_on and cfg.get("admin_ids"):
                    if issues:
                        await notify(bot, format_recon(issues))
                    elif st.get("fp"):
                        await notify(bot, "✅ اختلاف‌های مالیِ کیف پول برطرف شد؛ همه‌ی موجودی‌ها با تراکنش‌ها می‌خوانند.")
            st["fp"] = fp
            st["snap"] = recon_snapshot(cfg)
            st["last_run"] = time.time()
            await asyncio.to_thread(recon_save_state, st)
        except Exception:  # noqa: BLE001
            log.exception("recon_loop")
        await asyncio.sleep(6 * 3600)
