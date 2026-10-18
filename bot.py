#!/usr/bin/env python3
"""عملیات سبک پس‌زمینه: مانیتور سلامت پنل‌ها (هر ۵ دقیقه فقط یک درخواست کوچک به هر پنل؛ بین اجراها خواب است).
"""
from __future__ import annotations

import asyncio
import logging

import common as C

log = logging.getLogger("rebecca.ops")


async def notify(bot, text: str):
    for uid in C.load_config()["admin_ids"]:
        try:
            await bot.send_message(uid, text[:4000], parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s: %s", uid, e)


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
