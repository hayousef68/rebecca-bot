#!/usr/bin/env python3
"""عملیات سبک پس‌زمینه: مانیتور سلامت پنل‌ها و هشدار انقضا/اتمام حجم.

هر دو بین اجراها کاملاً خواب هستند (مانیتور هر ۵ دقیقه فقط یک درخواست کوچک به هر پنل؛ هشدار انقضا روزی یک بار).
"""
from __future__ import annotations

import asyncio
import logging
import math
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
        mc = C.load_config()["monitor"]
        if mc["enabled"]:
            for p in C.load_config()["panels"]:
                api = C.get_api(p["id"])
                if not api.is_configured():
                    continue
                st = state.setdefault(p["id"], {"fails": 0, "down": False})
                try:
                    await asyncio.wait_for(api.request("GET", "/api/system", retries=0), 35)
                    st["fails"] = 0
                    if st["down"]:
                        st["down"] = False
                        await notify(bot, f"🟢 پنل <b>{C.esc(p['name'])}</b> دوباره برقرار شد.")
                except Exception as e:  # noqa: BLE001
                    st["fails"] += 1
                    if st["fails"] >= 2 and not st["down"]:
                        st["down"] = True
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
