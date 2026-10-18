#!/usr/bin/env python3
"""ربات تلگرام مدیریت پنل ربکا: ساخت گروهی + لینک + QR، تمپلت، مدیریت کاربر."""
from __future__ import annotations

import asyncio
import functools
import io
import logging
import re
import time

from telegram import BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters,
)

import common as C
import ops
import sales
from common import esc, get_api, get_panel, load_config, new_panel_id, update_config

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rebecca.bot")

HTML = ParseMode.HTML
END = ConversationHandler.END
(C_TPL, C_BASE, C_START, C_COUNT, C_SERVICE, C_CONFIRM,
 T_NAME, T_GB, T_DAYS, T_EDIT,
 S_URL, S_SUB, S_USER, S_PASS,
 Q_SEARCH, E_GB, E_DAYS, C_PANEL, S_NAME,
 B_PATTERN, B_ACTION, B_PICK, B_CONFIRM, N_TEXT) = range(24)

STATUS_ICON = {"active": "🟢", "disabled": "⚫", "expired": "🔴", "limited": "🟠", "on_hold": "⏳"}


# ───────────────────────────── ابزارهای رابط ─────────────────────────────
def admin_only(fn):
    """هر هندلر فقط برای ادمین‌ها اجرا می‌شود (قبلاً فقط /start چک می‌شد)."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not C.is_admin(u.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ دسترسی ندارید", show_alert=True)
            elif update.message:
                await update.message.reply_html(
                    f"⛔ شما دسترسی ندارید.\nشناسه عددی شما: <code>{u.id if u else '?'}</code>"
                )
            return END
        return await fn(update, context)
    return wrapper


def nav(fn):
    """هندلرهای مستقل (غیر گفتگو): هر گفتگوی نیمه‌کاره بسته می‌شود."""
    @functools.wraps(fn)
    async def wrapper(update, context):
        context.user_data.clear()
        await fn(update, context)
        return END
    return admin_only(wrapper)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb=None):
    """ویرایش پیام فعلی؛ اگر ممکن نبود (مثلاً پیام عکس است) پیام جدید می‌فرستد."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML, disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except TelegramError:
            pass
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb, parse_mode=HTML, disable_web_page_preview=True)


def cancel_kb(extra=None):
    rows = list(extra or []) + [[B("❌ انصراف", callback_data="back")]]
    return M(rows)


def main_kb() -> M:
    cfg = load_config()
    if not cfg["panels"]:
        return M([[B("➕ افزودن پنل ربکا", callback_data="pn:new")]])
    pid = cfg["active_panel"]
    pn = get_panel(cfg, pid)
    return M([
        [B("⚡ ساخت تکی (ms)", callback_data="c:quick"), B("👥 ساخت همکاری", callback_data="c:qg")],
        [B("🎯 ساخت با انتخاب تمپلت", callback_data="c:pick")],
        [B("🔍 جستجو و مدیریت کاربر", callback_data="search"), B("📋 لیست کاربران", callback_data="l:all:0")],
        [B("👥 عملیات گروهی", callback_data="bk:start"), B("⏰ نزدیک انقضا", callback_data="soon")],
        [B("⚙️ تمپلت‌ها", callback_data="t:menu"), B("📊 وضعیت سرور", callback_data="stats")],
        [B("🧹 پاکسازی منقضی‌ها", callback_data=f"clean:ask:{pid}"), B("📥 خروجی لینک‌ها", callback_data="export")],
        [B("📤 فروش امروز", callback_data="rp:today"), B("📈 دیروز", callback_data="rp:yday"),
         B("📅 هفته", callback_data="rp:week"), B("🗓 ماه", callback_data="rp:month"), B("🗓 ماه قبل", callback_data="rp:lmonth")],
        [B(f"🖥 پنل‌ها (فعال: {pn['name']})", callback_data="pn:list")],
    ])


def home_text() -> str:
    cfg = load_config()
    ns, ng = C.default_template(cfg, "single"), C.default_template(cfg, "group")
    pn = get_panel(cfg, cfg["active_panel"])
    st = "🟢 متصل" if get_api().is_configured() else "🔴 تنظیم نشده"
    return (f"🚀 <b>ربات مدیریت پنل ربکا</b>\n🖥 پنل فعال: <b>{esc(pn['name']) if pn else '—'}</b> ({st})\n\n"
            f"👤 تکی (ms): <b>{esc(ns)}</b>\n    {esc(C.tpl_summary(cfg['templates'][ns]))}\n"
            f"👥 همکاری (hamkari): <b>{esc(ng)}</b>\n    {esc(C.tpl_summary(cfg['templates'][ng]))}")


async def home(update, context, prefix: str = ""):
    await show(update, context, prefix + home_text(), main_kb())


def err_text(e: Exception) -> str:
    return f"❌ {esc(e)}"


# ───────────────────────────── شروع و ناوبری ─────────────────────────────
@nav
async def start_cmd(update, context):
    if update.callback_query:
        await update.callback_query.answer()
    await home(update, context)


@nav
async def back_cb(update, context):
    await update.callback_query.answer()
    await home(update, context)


@nav
async def cancel_cmd(update, context):
    await home(update, context, "↩️ لغو شد.\n\n")


# ───────────────────────────── ساخت گروهی ─────────────────────────────
def tpl_by_idx(i: int):
    names = list(load_config()["templates"])
    return names[i] if 0 <= i < len(names) else None


@admin_only
async def create_entry(update, context):
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    context.user_data.clear()
    if q.data == "c:pick":
        rows = []
        for kind in ("single", "group"):
            ids = [(i, n, t) for i, (n, t) in enumerate(cfg["templates"].items()) if C.tpl_kind(t) == kind]
            if not ids:
                continue
            rows.append([B(C.KIND_LABEL[kind], callback_data="noop")])
            for i, n, t in ids:
                mark = "✅ " if n == C.default_template(cfg, kind) else ""
                rows.append([B(f"{mark}{n} ({t['gb']}GB/{t['days']}d)", callback_data=f"c:t:{i}")])
        await show(update, context, "🎯 تمپلت ساخت را انتخاب کنید:\n<i>سرویس از روی نوع تمپلت تعیین می‌شود.</i>", cancel_kb(rows))
        return C_TPL
    context.user_data["cr"] = {"tpl": C.default_template(cfg, "group" if q.data == "c:qg" else "single")}
    return await ask_panel(update, context)


@admin_only
async def create_pick_tpl(update, context):
    q = update.callback_query
    await q.answer()
    name = tpl_by_idx(int(q.data.split(":")[2]))
    if not name:
        await q.answer("تمپلت پیدا نشد", show_alert=True)
        return C_TPL
    context.user_data["cr"] = {"tpl": name}
    return await ask_panel(update, context)


async def ask_panel(update, context):
    cfg = load_config()
    cr = context.user_data["cr"]
    if len(cfg["panels"]) <= 1:
        cr["pid"] = cfg["active_panel"]
        return await ask_base(update, context)
    rows = [[B(("✅ " if p["id"] == cfg["active_panel"] else "🖥 ") + p["name"], callback_data=f"c:p:{p['id']}")] for p in cfg["panels"]]
    await show(update, context, "🖥 اکانت‌ها روی کدام <b>پنل</b> ساخته شوند؟", cancel_kb(rows))
    return C_PANEL


@admin_only
async def create_pick_panel(update, context):
    q = update.callback_query
    await q.answer()
    pid = q.data.split(":")[2]
    if not get_panel(load_config(), pid):
        await q.answer("پنل پیدا نشد", show_alert=True)
        return C_PANEL
    context.user_data["cr"]["pid"] = pid
    return await ask_base(update, context)


async def _suggest(cr: dict, prefix: str | None = None):
    """آخرین نام‌کاربری با پیشوند و شماره‌ی بعدی؛ در صورت خطا None."""
    cfg = load_config()
    try:
        return await asyncio.wait_for(
            sales.next_number(get_api(cr["pid"]), prefix or cfg["default_prefix"], len(cfg["panels"]) <= 1), 20)
    except Exception as e:  # noqa: BLE001
        log.warning("suggest next name: %s", e)
        return None


async def ask_base(update, context):
    cfg = load_config()
    cr = context.user_data["cr"]
    name = cr["tpl"]
    t = cfg["templates"][name]
    pn = get_panel(cfg, cr["pid"])
    sg = await _suggest(cr)
    rows, hint = [], ""
    if sg and cfg["default_prefix"]:
        cr["auto"] = {"base": sg["prefix"], "start": sg["next"]}
        nxt = f"{sg['prefix']}{sg['next']}"
        hint = (f"\n💡 آخرین کاربر: <code>{esc(sg['last'])}</code> ← بعدی: <code>{esc(nxt)}</code>" + (f"\n⚠️ بالاترین عدد موجود: <code>{esc(sg['max'])}</code>" if sg.get("max") else "") if sg["last"]
                else f"\n💡 شروع پیشنهادی: <code>{esc(nxt)}</code>")
        rows.append([B(f"⚡ ساخت {nxt} (۱ کاربر)", callback_data="c:auto")])
    await show(update, context,
               f"➕ <b>ساخت اکانت</b>\n🖥 پنل: <b>{esc(pn['name'])}</b>\n📌 تمپلت: <b>{esc(name)}</b>\n{esc(C.tpl_summary(t))}{hint}\n\n"
               "👤 <b>نام پایه</b> را بفرستید:\n"
               "• <code>ali</code> ← یک کاربر\n"
               "• <code>MS_ 150 10</code> ← ۱۰ کاربر: MS_150 تا MS_159",
               cancel_kb(rows))
    return C_BASE


@admin_only
async def create_auto(update, context):
    q = update.callback_query
    await q.answer()
    cr = context.user_data.get("cr") or {}
    a = cr.get("auto")
    if not a:
        await q.answer("پیشنهادی موجود نیست؛ نام را بفرستید", show_alert=True)
        return C_BASE
    cr.update(base=a["base"], start=a["start"], count=1)
    return await validate_and_pick_service(update, context)


@admin_only
async def create_one(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["cr"]["count"] = 1
    return await validate_and_pick_service(update, context)


@admin_only
async def create_next(update, context):
    q = update.callback_query
    await q.answer()
    cr = context.user_data["cr"]
    cr["start"] = (cr.get("auto2") or {}).get("start", 1)
    await show(update, context, f"📦 <b>تعداد</b> کاربران را بفرستید (حداکثر {load_config()['max_batch']}):",
               cancel_kb([[B("۱ کاربر", callback_data="c:one")]]))
    return C_COUNT


@admin_only
async def create_base(update, context):
    parts = update.message.text.split()
    cr = context.user_data["cr"]
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        cr.update(base=parts[0], start=int(parts[1]), count=int(parts[2]))
        return await validate_and_pick_service(update, context)
    if len(parts) != 1:
        await update.message.reply_text("⚠️ فقط نام پایه یا الگوی «نام شروع تعداد» (مثل ali 1 10) را بفرستید:")
        return C_BASE
    cr["base"] = parts[0]
    rows = [[B("تک کاربر (بدون عدد)", callback_data="c:single")]]
    sg = await _suggest(cr, parts[0])
    if sg:
        cr["auto2"] = {"start": sg["next"]}
        rows.insert(0, [B(f"شروع از {sg['next']} (پیشنهاد)", callback_data="c:next")])
        note = f"\n💡 آخرین: <code>{esc(sg['last'])}</code>" if sg["last"] else ""
    else:
        note = ""
    await update.message.reply_html(f"🔢 عدد شروع شماره‌گذاری را بفرستید، یا «تک کاربر» را بزنید:{note}", reply_markup=cancel_kb(rows))
    return C_START


@admin_only
async def create_start(update, context):
    cr = context.user_data["cr"]
    if update.callback_query:
        await update.callback_query.answer()
        cr.update(start=None, count=1)
        return await validate_and_pick_service(update, context)
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("⚠️ یک عدد صحیح بفرستید:")
        return C_START
    cr["start"] = int(txt)
    await update.message.reply_html(
        f"📦 <b>تعداد</b> کاربران را بفرستید (حداکثر {load_config()['max_batch']}):",
        reply_markup=cancel_kb([[B("۱ کاربر", callback_data="c:one")]]),
    )
    return C_COUNT


@admin_only
async def create_count(update, context):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("⚠️ یک عدد صحیح بفرستید:")
        return C_COUNT
    context.user_data["cr"]["count"] = int(txt)
    return await validate_and_pick_service(update, context)


async def validate_and_pick_service(update, context):
    cr = context.user_data["cr"]
    try:
        cr["names"] = C.make_names(cr["base"], cr.get("start"), cr.get("count", 1), load_config()["max_batch"])
    except ValueError as e:
        await context.bot.send_message(update.effective_chat.id, f"⚠️ {e}\nدوباره نام پایه را بفرستید:", reply_markup=cancel_kb())
        return C_BASE
    try:
        services = await get_api(cr["pid"]).list_services()
        if not services:
            raise C.RebeccaError("لیست سرویس‌های این پنل خوانده نشد؛ اتصال پنل را بررسی کنید.")
    except Exception as e:  # noqa: BLE001
        await context.bot.send_message(update.effective_chat.id, err_text(e), reply_markup=main_kb(), parse_mode=HTML)
        return END
    cr["services"] = {s["id"]: s["name"] for s in services}
    if len(services) == 1:
        cr["service_id"] = services[0]["id"]
        return await confirm_step(update, context)
    # تکی ← ms ، گروهی ← hamkari (در مرحله‌ی تأیید قابل تغییر است)
    auto = C.pick_default_service(services, group=C.tpl_kind(load_config()["templates"][cr["tpl"]]) == "group")
    if auto is not None:
        cr["service_id"] = auto
        return await confirm_step(update, context)
    return await ask_service(update, context)


async def ask_service(update, context):
    cr = context.user_data["cr"]
    rows, row = [], []
    for sid, name in cr["services"].items():
        row.append(B(("✅ " if sid == cr.get("service_id") else "📂 ") + name, callback_data=f"srv:{sid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await context.bot.send_message(update.effective_chat.id, "📂 <b>سرویس</b> را انتخاب کنید:", reply_markup=cancel_kb(rows), parse_mode=HTML)
    return C_SERVICE


async def noop_cb(update, context):
    """دکمه‌های عنوان (جداکننده) — فقط loading را می‌بندد."""
    await update.callback_query.answer()


@admin_only
async def create_change_service(update, context):
    await update.callback_query.answer()
    if "cr" not in context.user_data or "services" not in context.user_data["cr"]:
        await show(update, context, "⚠️ نشست منقضی شد. دوباره شروع کنید.", main_kb())
        return END
    return await ask_service(update, context)


@admin_only
async def create_service(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["cr"]["service_id"] = int(q.data.split(":")[1])
    return await confirm_step(update, context)


async def confirm_step(update, context):
    cr = context.user_data["cr"]
    t = load_config()["templates"][cr["tpl"]]
    names = cr["names"]
    rng = names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"
    pn = get_panel(load_config(), cr["pid"])
    text = (f"📝 <b>تأیید نهایی</b>\n\n🖥 پنل: <b>{esc(pn['name'])}</b>\n👥 تعداد: <b>{len(names)}</b>\n🔤 نام‌ها: <code>{esc(rng)}</code>\n"
            f"📌 تمپلت: {esc(cr['tpl'])}\n{esc(C.tpl_summary(t))}\n"
            f"📂 سرویس: {esc(cr['services'].get(cr['service_id'], cr['service_id']))}")
    kb = cancel_kb([[B("✅ شروع ساخت", callback_data="c:go")], [B("🔀 تغییر سرویس", callback_data="c:svc")]])
    await show(update, context, text, kb)
    return C_CONFIRM


@admin_only
async def create_go(update, context):
    q = update.callback_query
    await q.answer()
    cr = context.user_data.get("cr")
    if not cr or "names" not in cr:
        await show(update, context, "⚠️ نشست منقضی شد. دوباره شروع کنید.", main_kb())
        return END
    cfg = load_config()
    tpl = cfg["templates"][cr["tpl"]]
    names, chat_id = cr["names"], update.effective_chat.id
    msg = q.message
    last = [0.0]

    async def progress(done, total):
        if done == total or time.monotonic() - last[0] > 1.5:
            last[0] = time.monotonic()
            try:
                await msg.edit_text(f"⚡ در حال ساخت… {done}/{total}")
            except TelegramError:
                pass

    await progress(0, len(names))
    results, failed = await C.create_users(get_api(cr["pid"]), names, tpl, cr["service_id"], on_progress=progress)
    context.user_data.clear()

    if not results:
        reason = failed[0][1] if failed else "نامشخص"
        await context.bot.send_message(chat_id, f"❌ هیچ کاربری ساخته نشد.\nدلیل: {esc(reason)}", reply_markup=main_kb(), parse_mode=HTML)
        return END

    pname = (get_panel(cfg, cr["pid"]) or {}).get("name", "")
    header = C.build_info(pname, tpl, cr["services"].get(cr["service_id"], cr["service_id"]))
    try:
        await C.send_results(context.bot, chat_id, results, header, cfg["max_qr"])
    except Exception as e:  # noqa: BLE001
        log.exception("send_results")
        await context.bot.send_message(chat_id, f"⚠️ ساخت انجام شد ولی ارسال QR مشکل داشت: {esc(e)}", parse_mode=HTML)
    summary = f"🎉 <b>{len(results)}</b> کاربر ساخته شد."
    if failed:
        lines = "\n".join(f"• <code>{esc(n)}</code>: {esc(r)}" for n, r in failed[:15])
        more = f"\n… و {len(failed) - 15} مورد دیگر" if len(failed) > 15 else ""
        summary += f"\n\n⚠️ <b>{len(failed)}</b> مورد ناموفق:\n{lines}{more}"
    await context.bot.send_message(chat_id, summary, reply_markup=main_kb(), parse_mode=HTML)
    return END


# ───────────────────────────── تمپلت‌ها ─────────────────────────────
@nav
async def tpl_menu(update, context):
    q = update.callback_query
    await q.answer()
    await render_tpl_menu(update, context)


async def render_tpl_menu(update, context, prefix=""):
    cfg = load_config()
    rows = []
    for kind in ("single", "group"):
        rows.append([B(C.KIND_LABEL[kind], callback_data="noop")])
        for i, (n, t) in enumerate(cfg["templates"].items()):
            if C.tpl_kind(t) != kind:
                continue
            mark = "✅" if n == C.default_template(cfg, kind) else "📁"
            rows.append([B(f"{mark} {n} ({t['gb']}GB/{t['days']}d)", callback_data=f"t:sel:{i}")])
            rows.append([B("✏️ ویرایش", callback_data=f"t:edit:{i}"), B("🗑 حذف", callback_data=f"t:del:{i}")])
    rows += [[B("➕ تمپلت جدید", callback_data="t:new")], [B("🔙 منوی اصلی", callback_data="back")]]
    await show(update, context, prefix + "⚙️ <b>مدیریت تمپلت‌ها</b>\nروی نام بزنید تا پیش‌فرض همان نوع شود:", M(rows))


@nav
async def tpl_action(update, context):
    q = update.callback_query
    _, act, idx = q.data.split(":")
    name = tpl_by_idx(int(idx))
    if not name:
        await q.answer("تمپلت پیدا نشد", show_alert=True)
        return await render_tpl_menu(update, context)
    if act == "sel":
        def sel(c):
            c["active_group_template" if C.tpl_kind(c["templates"][name]) == "group" else "active_template"] = name
        update_config(sel)
        await q.answer("✅ فعال شد")
        await render_tpl_menu(update, context)
    elif act == "del":
        cfg = load_config()
        if len(cfg["templates"]) <= 1:
            await q.answer("⚠️ حداقل یک تمپلت باید بماند!", show_alert=True)
            return await render_tpl_menu(update, context)

        def rm(c):
            c["templates"].pop(name, None)
        update_config(rm)
        await q.answer("🗑 حذف شد")
        await render_tpl_menu(update, context)
    elif act == "kind":
        def tk(c):
            t = c["templates"][name]
            t["kind"] = "single" if C.tpl_kind(t) == "group" else "group"
        update_config(tk)
        await q.answer("نوع تغییر کرد")
        await tpl_edit_view(update, context, int(idx))
    elif act == "mode":
        def tg(c):
            t = c["templates"][name]
            t["mode"] = "active" if t["mode"] == "on_hold" else "on_hold"
        update_config(tg)
        await q.answer("حالت تغییر کرد")
        await tpl_edit_view(update, context, int(idx))
    elif act == "edit":
        await q.answer()
        await tpl_edit_view(update, context, int(idx))


async def tpl_edit_view(update, context, idx):
    name = tpl_by_idx(idx)
    t = load_config()["templates"][name]
    kb = M([
        [B("✏️ نام", callback_data=f"te:name:{idx}"), B("📊 حجم", callback_data=f"te:gb:{idx}"), B("📅 روز", callback_data=f"te:days:{idx}")],
        [B(f"⏳ حالت: {t['mode']} ← تغییر", callback_data=f"t:mode:{idx}")],
        [B(f"{C.KIND_LABEL[C.tpl_kind(t)]} ← تغییر نوع", callback_data=f"t:kind:{idx}")],
        [B("🔙 بازگشت", callback_data="t:menu")],
    ])
    await show(update, context, f"✏️ <b>{esc(name)}</b>\n{esc(C.tpl_summary(t))}\n\nکدام مورد ویرایش شود؟", kb)


@admin_only
async def tpl_new(update, context):
    await update.callback_query.answer()
    context.user_data.clear()
    context.user_data["nt"] = {}
    await show(update, context, "📝 نام تمپلت جدید را بفرستید (مثلاً: اقتصادی):", cancel_kb())
    return T_NAME


@admin_only
async def tpl_new_name(update, context):
    name = update.message.text.strip()
    if not 1 <= len(name) <= 40 or name in load_config()["templates"]:
        await update.message.reply_text("⚠️ نام خالی/تکراری/بیش از ۴۰ کاراکتر است. نام دیگری بفرستید:")
        return T_NAME
    context.user_data["nt"]["name"] = name
    await update.message.reply_html("💾 حجم به <b>گیگابایت</b> (<code>0</code> = نامحدود):", reply_markup=cancel_kb())
    return T_GB


def parse_nonneg(text: str, as_int=False):
    v = float(text.strip().replace(",", "."))
    if v < 0 or v != v or v == float("inf"):
        raise ValueError
    return int(v) if as_int else (int(v) if v.is_integer() else v)


@admin_only
async def tpl_new_gb(update, context):
    try:
        context.user_data["nt"]["gb"] = parse_nonneg(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ عدد معتبر (۰ یا بیشتر) بفرستید:")
        return T_GB
    await update.message.reply_html("📅 اعتبار به <b>روز</b> (<code>0</code> = نامحدود):", reply_markup=cancel_kb())
    return T_DAYS


@admin_only
async def tpl_new_days(update, context):
    try:
        days = parse_nonneg(update.message.text, as_int=True)
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح (۰ یا بیشتر) بفرستید:")
        return T_DAYS
    nt = context.user_data.pop("nt")

    def add(c):
        c["templates"][nt["name"]] = {"gb": nt["gb"], "days": days, "mode": "on_hold", "kind": "single"}
    update_config(add)
    await render_tpl_menu(update, context, f"✅ تمپلت «{esc(nt['name'])}» ساخته شد (نوع: تکی؛ از «✏️ ویرایش» می‌توانید همکاری‌اش کنید).\n\n")
    return END


@admin_only
async def tpl_edit_field(update, context):
    q = update.callback_query
    await q.answer()
    _, field, idx = q.data.split(":")
    name = tpl_by_idx(int(idx))
    if not name:
        return END
    context.user_data.clear()
    context.user_data["te"] = {"name": name, "field": field}
    prompt = {"name": "📝 نام جدید:", "gb": "💾 حجم جدید (GB، 0 = نامحدود):", "days": "📅 اعتبار جدید (روز، 0 = نامحدود):"}[field]
    await show(update, context, prompt, cancel_kb())
    return T_EDIT


@admin_only
async def tpl_edit_value(update, context):
    te, text = context.user_data["te"], update.message.text.strip()
    cfg = load_config()
    if te["name"] not in cfg["templates"]:
        return END
    try:
        if te["field"] == "name":
            if not 1 <= len(text) <= 40 or (text != te["name"] and text in cfg["templates"]):
                raise ValueError
            def ren(c):
                items = [(text if k == te["name"] else k, v) for k, v in c["templates"].items()]
                c["templates"] = dict(items)
                if c["active_template"] == te["name"]:
                    c["active_template"] = text
            update_config(ren)
        else:
            val = parse_nonneg(text, as_int=te["field"] == "days")
            update_config(lambda c: c["templates"][te["name"]].__setitem__(te["field"], val))
    except ValueError:
        await update.message.reply_text("⚠️ مقدار نامعتبر است، دوباره بفرستید:")
        return T_EDIT
    context.user_data.clear()
    await render_tpl_menu(update, context, "✅ ویرایش شد.\n\n")
    return END


# ───────────────────────────── مدیریت پنل‌ها ─────────────────────────────
async def render_panel_list(update, context, prefix=""):
    cfg = load_config()
    rows = []
    for p in cfg["panels"]:
        on = p["id"] == cfg["active_panel"]
        rows.append([B(("✅ " if on else "🖥 ") + p["name"], callback_data=f"pn:set:{p['id']}")])
        rows.append([B("🔁 ویرایش/اتصال مجدد", callback_data=f"pn:edit:{p['id']}"), B("🗑 حذف", callback_data=f"pn:del:{p['id']}")])
    rows += [[B("➕ افزودن پنل", callback_data="pn:new")], [B("🔙 منوی اصلی", callback_data="back")]]
    await show(update, context, prefix + "🖥 <b>پنل‌ها</b>\nروی نام بزنید تا «پنل فعال» (برای جستجو/لیست/آمار) شود:", M(rows))


@nav
async def panel_list(update, context):
    await update.callback_query.answer()
    await render_panel_list(update, context)


@nav
async def panel_action(update, context):
    q = update.callback_query
    _, act, pid = q.data.split(":")
    cfg = load_config()
    pn = get_panel(cfg, pid)
    if not pn:
        await q.answer("پنل پیدا نشد", show_alert=True)
        return await render_panel_list(update, context)
    if act == "set":
        update_config(lambda c: c.__setitem__("active_panel", pid))
        await q.answer("✅ پنل فعال شد")
        await render_panel_list(update, context)
    elif act == "del":
        await q.answer()
        await show(update, context, f"⚠️ پنل <b>{esc(pn['name'])}</b> از ربات حذف شود؟\n(کاربران داخل خود پنل دست نمی‌خورند.)",
                   M([[B("✅ حذف", callback_data=f"pn:delok:{pid}"), B("↩️ انصراف", callback_data="pn:list")]]))
    elif act == "delok":
        def rm(c):
            c["panels"] = [p for p in c["panels"] if p["id"] != pid]
        update_config(rm)
        C.drop_api(pid)
        await q.answer("🗑 حذف شد")
        await render_panel_list(update, context)


@admin_only
async def setup_start(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    parts = q.data.split(":")
    pid = parts[2] if len(parts) > 2 else None
    context.user_data["su"] = {"pid": pid}
    if pid:
        return await ask_url(update, context)
    await show(update, context, "🏷 یک <b>نام</b> برای این پنل بفرستید (مثلاً: پنل اصلی):", cancel_kb())
    return S_NAME


@admin_only
async def setup_name(update, context):
    name = update.message.text.strip()
    if not 1 <= len(name) <= 40:
        await update.message.reply_text("⚠️ نام باید ۱ تا ۴۰ کاراکتر باشد:")
        return S_NAME
    context.user_data["su"]["name"] = name
    return await ask_url(update, context)


async def ask_url(update, context):
    await show(update, context,
               "🌐 آدرس پنل را با پورت بفرستید:\n<code>https://panel.example.com:2096</code>\n(پسوند /dashboard اشکالی ندارد)",
               cancel_kb())
    return S_URL


@admin_only
async def setup_url(update, context):
    url = update.message.text.strip().rstrip("/")
    if not re.match(r"^https?://\S+$", url):
        await update.message.reply_text("⚠️ آدرس باید با http:// یا https:// شروع شود:")
        return S_URL
    context.user_data["su"]["panel_url"] = url
    await update.message.reply_html(
        "🔗 دامنه <b>سابسکریپشن</b> را بفرستید (اگر با پنل یکی است رد کنید):",
        reply_markup=cancel_kb([[B("⏭ مشابه پنل", callback_data="s:skip")]]),
    )
    return S_SUB


@admin_only
async def setup_sub(update, context):
    su = context.user_data["su"]
    if update.callback_query:
        await update.callback_query.answer()
        su["sub_url"] = ""
        await show(update, context, "👤 نام کاربری ادمین پنل:", cancel_kb())
    else:
        su["sub_url"] = update.message.text.strip().rstrip("/")
        await update.message.reply_html("👤 نام کاربری ادمین پنل:", reply_markup=cancel_kb())
    return S_USER


@admin_only
async def setup_user(update, context):
    context.user_data["su"]["username"] = update.message.text.strip()
    await update.message.reply_html("🔑 رمز عبور ادمین (پیام شما بعد از دریافت حذف می‌شود):", reply_markup=cancel_kb())
    return S_PASS


@admin_only
async def setup_pass(update, context):
    su = context.user_data.pop("su")
    pw = update.message.text.strip()
    try:
        await update.message.delete()
    except TelegramError:
        pass
    old_cfg = load_config()
    pid = su["pid"]
    old_panel = dict(get_panel(old_cfg, pid)) if pid and get_panel(old_cfg, pid) else None

    def save(c):
        nonlocal pid
        fields = dict(panel_url=su["panel_url"], sub_url=su["sub_url"], username=su["username"], password=pw)
        pn = get_panel(c, pid) if pid else None
        if pn:
            pn.update(fields)
        else:
            pid = new_panel_id(c)
            c["panels"].append({"id": pid, "name": su.get("name") or f"پنل {len(c['panels']) + 1}", "verify_ssl": False, "services": [], **fields})
            if not c["active_panel"] or len(c["panels"]) == 1:
                c["active_panel"] = pid
    update_config(save)
    wait = await context.bot.send_message(update.effective_chat.id, "⏳ در حال تست اتصال…")
    api = get_api(pid)
    try:
        await api.login()
        st = await api.request("GET", "/api/system")
        svc = await api.list_services(force=True)
        await wait.edit_text(f"✅ اتصال برقرار شد.\n👥 کاربران: {st.get('total_user', 0)} | 📂 سرویس‌ها: {len(svc)}", reply_markup=main_kb())
    except Exception as e:  # noqa: BLE001
        def rollback(c):
            if old_panel:
                pn = get_panel(c, pid)
                if pn:
                    pn.update({k: old_panel[k] for k in ("panel_url", "sub_url", "username", "password")})
            else:
                c["panels"] = [p for p in c["panels"] if p["id"] != pid]
        update_config(rollback)
        await wait.edit_text(f"❌ اتصال ناموفق؛ تغییرات اعمال نشد.\n{esc(e)}", reply_markup=main_kb(), parse_mode=HTML)
    return END


# ───────────────────────────── جستجو و مدیریت کاربر ─────────────────────────────
async def user_card(u: dict, api):
    pid = api.pid
    name, st = u["username"], u.get("status", "unknown")
    used, lim = u.get("used_traffic", 0) or 0, u.get("data_limit") or 0
    pct = f" ({used * 100 / lim:.0f}%)" if lim else ""
    sub = api.full_sub(u.get("subscription_url", "")) or "نامشخص"
    try:
        names = {s["id"]: s["name"] for s in await api.list_services()}
    except Exception:  # noqa: BLE001
        names = {}
    sid = u.get("service_id")
    sname = names.get(sid) or (f"#{sid}" if sid is not None else "—")
    note = str(u.get("note") or "").strip()
    text = (f"👤 <b>{esc(name)}</b> ({esc(api.name)})\n{STATUS_ICON.get(st, '❔')} وضعیت: {esc(st)}\n"
            f"📂 سرویس: <b>{esc(sname)}</b>\n"
            f"📊 مصرف: {C.fmt_gb(used)} / {C.fmt_gb(lim)}{pct}\n"
            f"⏳ اعتبار: {C.fmt_expire(u.get('expire'), st, u.get('on_hold_expire_duration'))}\n"
            + (f"📝 {esc(note[:200])}\n" if note else "") + f"\n🔗 <code>{esc(sub)}</code>")
    tg = "✅ فعال‌سازی" if st == "disabled" else "⛔ غیرفعال‌سازی"
    kb = M([
        [B("🖼 QR", callback_data=f"u:qr:{pid}:{name}"), B("✏️ ویرایش", callback_data=f"u:edit:{pid}:{name}")],
        [B("🔀 تغییر سرویس", callback_data=f"u:svc:{pid}:{name}"), B("⚡ تمدید با تمپلت", callback_data=f"u:renew:{pid}:{name}")],
        [B("🔄 صفر کردن مصرف", callback_data=f"u:rst:{pid}:{name}"), B("🔗 لینک جدید", callback_data=f"u:rev:{pid}:{name}")],
        [B("📝 یادداشت", callback_data=f"u:note:{pid}:{name}"), B(tg, callback_data=f"u:tg:{pid}:{name}")],
        [B("🗑 حذف", callback_data=f"u:del:{pid}:{name}"), B("🔙 منوی اصلی", callback_data="back")],
    ])
    return text, kb


@admin_only
async def search_start(update, context):
    await update.callback_query.answer()
    context.user_data.clear()
    await show(update, context, "🔍 نام کاربری (یا بخشی از آن) را بفرستید:", cancel_kb())
    return Q_SEARCH


@admin_only
async def search_do(update, context):
    q = update.message.text.strip()
    api = get_api()
    try:
        try:
            u = await api.request("GET", api.upath(q), retries=0)
            text, kb = await user_card(u, api)
            await update.message.reply_html(text, reply_markup=kb)
            return END
        except C.RebeccaError as e:
            if e.status not in (404, 422, 400):
                raise
        res = await api.request("GET", "/api/users", params={"search": q, "limit": 10})
    except Exception as e:  # noqa: BLE001
        await update.message.reply_html(err_text(e), reply_markup=main_kb())
        return END
    users = res.get("users", [])
    if not users:
        await update.message.reply_text("❌ کاربری پیدا نشد. دوباره بفرستید:")
        return Q_SEARCH
    rows = [[B(f"{STATUS_ICON.get(x['status'], '❔')} {x['username']}", callback_data=f"u:view:{api.pid}:{x['username']}")] for x in users]
    rows.append([B("🔙 منوی اصلی", callback_data="back")])
    await update.message.reply_text("🔍 نتایج:", reply_markup=M(rows))
    return END


@nav
async def user_action(update, context):
    q = update.callback_query
    _, act, pid, uname = q.data.split(":", 3)
    api = get_api(pid)
    p = api.upath(uname)
    try:
        if act == "qr":
            await q.answer("ساخت QR…")
            u = await api.request("GET", p)
            sub = api.full_sub(u.get("subscription_url", ""))
            png = await asyncio.to_thread(C.make_qr_png, sub)
            await context.bot.send_photo(update.effective_chat.id, png, caption=f"👤 <b>{esc(uname)}</b>\n🔗 <code>{esc(sub)}</code>", parse_mode=HTML)
            return
        if act == "del":
            await q.answer()
            kb = M([[B("✅ بله، حذف شود", callback_data=f"u:delok:{pid}:{uname}"), B("↩️ خیر", callback_data=f"u:view:{pid}:{uname}")]])
            return await show(update, context, f"⚠️ کاربر <b>{esc(uname)}</b> برای همیشه حذف شود؟", kb)
        if act == "delok":
            await api.request("DELETE", p)
            await q.answer("🗑 حذف شد", show_alert=True)
            return await home(update, context, f"✅ کاربر {esc(uname)} حذف شد.\n\n")
        if act == "svc":
            await q.answer()
            cur = (await api.request("GET", p)).get("service_id")
            rows = [[B(("✅ " if s["id"] == cur else "📂 ") + s["name"], callback_data=f"u:svs:{pid}:{s['id']}:{uname}")]
                    for s in await api.list_services()]
            rows.append([B("↩️ بازگشت", callback_data=f"u:view:{pid}:{uname}")])
            return await show(update, context, f"🔀 سرویس جدید <b>{esc(uname)}</b> را انتخاب کنید:", M(rows))
        if act == "renew":
            await q.answer()
            rows = [[B(f"{n} ({t['gb']}GB/{t['days']}d)", callback_data=f"u:rnw:{pid}:{i}:{uname}")]
                    for i, (n, t) in enumerate(load_config()["templates"].items())]
            rows.append([B("↩️ بازگشت", callback_data=f"u:view:{pid}:{uname}")])
            return await show(update, context, f"⚡ تمدید <b>{esc(uname)}</b> با کدام تمپلت؟\n(مصرف صفر و حجم/مدت جدید اعمال می‌شود)", M(rows))
        if act == "rst":
            await api.request("POST", p + "/reset")
            await q.answer("✅ مصرف صفر شد", show_alert=True)
        elif act == "rev":
            await api.request("POST", p + "/revoke_sub")
            await q.answer("✅ لینک جدید ساخته شد", show_alert=True)
        elif act == "tg":
            cur = await api.request("GET", p)
            new = "active" if cur.get("status") == "disabled" else "disabled"
            await api.request("PUT", p, json={"status": new})
            await q.answer(f"وضعیت: {new}")
        else:
            await q.answer()
        text, kb = await user_card(await api.request("GET", p), api)
        await show(update, context, text, kb)
    except Exception as e:  # noqa: BLE001
        await q.answer(f"❌ {str(e)[:180]}", show_alert=True)


@nav
async def user_apply(update, context):
    """اعمال تغییر سرویس / تمدید با تمپلت (با تأیید)."""
    q = update.callback_query
    _, act, pid, idx, uname = q.data.split(":", 4)
    api = get_api(pid)
    try:
        if act == "svs":
            await C.set_service(api, uname, int(idx))
            await q.answer("✅ سرویس تغییر کرد")
        else:
            name = tpl_by_idx(int(idx))
            if not name:
                return await q.answer("تمپلت پیدا نشد", show_alert=True)
            tpl = load_config()["templates"][name]
            if act == "rnw":
                await q.answer()
                kb = M([[B("✅ تمدید شود", callback_data=f"u:rnok:{pid}:{idx}:{uname}"), B("↩️ انصراف", callback_data=f"u:view:{pid}:{uname}")]])
                return await show(update, context, f"⚡ تمدید <b>{esc(uname)}</b>\n📌 {esc(name)}\n{esc(C.tpl_summary(tpl))}\n\nمصرف صفر می‌شود. ادامه می‌دهید؟", kb)
            await C.renew_user(api, uname, tpl)
            await q.answer("✅ تمدید شد", show_alert=True)
        text, kb = await user_card(await api.request("GET", api.upath(uname)), api)
        await show(update, context, text, kb)
    except Exception as e:  # noqa: BLE001
        await q.answer(f"❌ {str(e)[:180]}", show_alert=True)


@admin_only
async def note_start(update, context):
    q = update.callback_query
    await q.answer()
    _, _, pid, uname = q.data.split(":", 3)
    context.user_data.clear()
    context.user_data["nt2"] = {"u": uname, "pid": pid}
    await show(update, context, f"📝 یادداشت <b>{esc(uname)}</b> را بفرستید (نام مشتری، شماره، توضیح…)\n<code>-</code> = حذف یادداشت", cancel_kb())
    return N_TEXT


@admin_only
async def note_text(update, context):
    d = context.user_data.pop("nt2", None)
    if not d:
        return END
    api = get_api(d["pid"])
    t = update.message.text.strip()
    try:
        await C.set_note(api, d["u"], "" if t == "-" else t)
        text, kb = await user_card(await api.request("GET", api.upath(d["u"])), api)
        await update.message.reply_html("✅ ذخیره شد.\n\n" + text, reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_html(err_text(e), reply_markup=main_kb())
    return END


# ───────────────────────────── عملیات گروهی ─────────────────────────────
def _bk(context) -> dict:
    return context.user_data.setdefault("bk", {})


@admin_only
async def bulk_start(update, context):
    await update.callback_query.answer()
    context.user_data.clear()
    api = get_api()
    _bk(context)["pid"] = api.pid
    await show(update, context,
               f"👥 <b>عملیات گروهی</b> روی پنل «{esc(api.name)}»\n\nنام کاربران را بفرستید:\n"
               "• <code>ali 1 10</code> ← ali1 تا ali10\n• <code>ali</code> ← فقط یک کاربر", cancel_kb())
    return B_PATTERN


@admin_only
async def bulk_pattern(update, context):
    parts = update.message.text.split()
    bk = _bk(context)
    try:
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            names = C.make_names(parts[0], int(parts[1]), int(parts[2]), load_config()["max_batch"])
        elif len(parts) == 1:
            names = C.make_names(parts[0], None, 1, 1)
        else:
            raise ValueError("فرمت درست: ali 1 10  یا  ali")
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\nدوباره بفرستید:")
        return B_PATTERN
    bk["names"] = names
    acts = list(C.BULK_ACTIONS.items())
    rows = [[B(lbl, callback_data=f"bk:a:{k}") for k, lbl in acts[i:i + 2]] for i in range(0, len(acts), 2)]
    await update.message.reply_html(f"✅ {len(names)} کاربر ({esc(names[0])}{' … ' + esc(names[-1]) if len(names) > 1 else ''})\n\nچه کاری انجام شود؟",
                                    reply_markup=cancel_kb(rows))
    return B_ACTION


@admin_only
async def bulk_action(update, context):
    q = update.callback_query
    await q.answer()
    bk = _bk(context)
    if "names" not in bk:
        await show(update, context, "⚠️ نشست منقضی شد.", main_kb())
        return END
    bk["action"] = q.data.split(":")[2]
    if bk["action"] == "service":
        try:
            services = await get_api(bk["pid"]).list_services()
        except Exception as e:  # noqa: BLE001
            await show(update, context, err_text(e), main_kb())
            return END
        bk["services"] = {s["id"]: s["name"] for s in services}
        rows = [[B(f"📂 {s['name']}", callback_data=f"bk:s:{s['id']}")] for s in services]
        await show(update, context, "🔀 به کدام سرویس منتقل شوند؟", cancel_kb(rows))
        return B_PICK
    if bk["action"] == "renew":
        rows = [[B(f"{n} ({t['gb']}GB/{t['days']}d)", callback_data=f"bk:t:{i}")] for i, (n, t) in enumerate(load_config()["templates"].items())]
        await show(update, context, "⚡ تمدید با کدام تمپلت؟ (مصرف صفر می‌شود)", cancel_kb(rows))
        return B_PICK
    return await bulk_confirm(update, context)


@admin_only
async def bulk_pick(update, context):
    q = update.callback_query
    await q.answer()
    bk = _bk(context)
    kind, val = q.data.split(":")[1], int(q.data.split(":")[2])
    if kind == "s":
        bk["service_id"] = val
    else:
        name = tpl_by_idx(val)
        if not name:
            await q.answer("تمپلت پیدا نشد", show_alert=True)
            return B_PICK
        bk["tpl"] = name
    return await bulk_confirm(update, context)


async def bulk_confirm(update, context):
    bk = _bk(context)
    names = bk["names"]
    api = get_api(bk["pid"])
    line = ""
    if bk["action"] == "service":
        line = f"\n📂 سرویس جدید: <b>{esc(bk['services'].get(bk['service_id'], bk['service_id']))}</b>"
    elif bk["action"] == "renew":
        t = load_config()["templates"][bk["tpl"]]
        line = f"\n📌 {esc(bk['tpl'])} — {esc(C.tpl_summary(t))}"
    warn = "\n\n⚠️ <b>حذف برگشت‌پذیر نیست!</b>" if bk["action"] == "delete" else ""
    text = (f"📝 <b>تأیید عملیات گروهی</b>\n🖥 پنل: <b>{esc(api.name)}</b>\n⚙️ عملیات: {C.BULK_ACTIONS[bk['action']]}"
            f"\n👥 تعداد: <b>{len(names)}</b> ({esc(names[0])}{' … ' + esc(names[-1]) if len(names) > 1 else ''}){line}{warn}")
    await show(update, context, text, cancel_kb([[B("✅ اجرا شود", callback_data="bk:go")]]))
    return B_CONFIRM


@admin_only
async def bulk_go(update, context):
    q = update.callback_query
    await q.answer()
    bk = context.user_data.get("bk")
    if not bk or "action" not in bk:
        await show(update, context, "⚠️ نشست منقضی شد.", main_kb())
        return END
    api = get_api(bk["pid"])
    tpl = load_config()["templates"].get(bk.get("tpl", ""))
    last, msg = [0.0], q.message

    async def progress(done, total):
        if done == total or time.monotonic() - last[0] > 1.5:
            last[0] = time.monotonic()
            try:
                await msg.edit_text(f"⏳ در حال انجام… {done}/{total}")
            except TelegramError:
                pass
    ok, bad = await C.bulk_apply(api, bk["names"], bk["action"], service_id=bk.get("service_id"), tpl=tpl, on_progress=progress)
    context.user_data.clear()
    text = f"✅ {C.BULK_ACTIONS[bk['action']]}: <b>{len(ok)}</b> از {len(bk['names'])} کاربر انجام شد."
    if bad:
        lines = "\n".join(f"• <code>{esc(n)}</code>: {esc(r)}" for n, r in bad[:15])
        text += f"\n\n⚠️ <b>{len(bad)}</b> مورد ناموفق:\n{lines}" + (f"\n… و {len(bad) - 15} مورد دیگر" if len(bad) > 15 else "")
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=main_kb(), parse_mode=HTML)
    return END


# ───────────────────────────── نزدیک انقضا ─────────────────────────────
@nav
async def soon_cb(update, context):
    if update.callback_query:
        await update.callback_query.answer("در حال بررسی…")
    api, rc = get_api(), load_config()["reminders"]
    try:
        items = await ops.find_soon(api, rc["days"], rc["low_pct"])
    except Exception as e:  # noqa: BLE001
        return await show(update, context, err_text(e), main_kb())
    if not items:
        return await show(update, context, f"✅ در پنل «{esc(api.name)}» کاربری نزدیک انقضا یا اتمام حجم نیست.", main_kb())
    rows = [[B(f"{i['username']} — {i['reason']}"[:60], callback_data=f"u:view:{api.pid}:{i['username']}")] for i in items[:20]]
    rows.append([B("🔙 منوی اصلی", callback_data="back")])
    await show(update, context, f"⏰ پنل «{esc(api.name)}»: <b>{len(items)}</b> کاربر تا {rc['days']} روز دیگر منقضی می‌شوند یا کمتر از {rc['low_pct']}٪ حجم دارند.\nروی هر کدام بزنید تا تمدیدش کنید:", M(rows))


@admin_only
async def edit_start(update, context):
    q = update.callback_query
    await q.answer()
    _, _, pid, uname = q.data.split(":", 3)
    context.user_data.clear()
    context.user_data["eu"] = {"u": uname, "pid": pid}
    await show(update, context, f"✏️ <b>{esc(uname)}</b>\n\n💾 حجم جدید (GB)\n<code>0</code> = نامحدود | <code>-</code> = بدون تغییر",
               cancel_kb([[B("بدون تغییر", callback_data="e:keep")]]))
    return E_GB


@admin_only
async def edit_gb(update, context):
    eu = context.user_data["eu"]
    if update.callback_query:
        await update.callback_query.answer()
        eu["gb"] = None
    else:
        t = update.message.text.strip()
        try:
            eu["gb"] = None if t == "-" else parse_nonneg(t)
        except ValueError:
            await update.message.reply_text("⚠️ عدد معتبر یا - بفرستید:")
            return E_GB
    await context.bot.send_message(update.effective_chat.id,
                                   "📅 اعتبار جدید (روز)\n<code>0</code> = نامحدود | <code>-</code> = بدون تغییر",
                                   reply_markup=cancel_kb([[B("بدون تغییر", callback_data="e:keep")]]), parse_mode=HTML)
    return E_DAYS


@admin_only
async def edit_days(update, context):
    eu = context.user_data["eu"]
    if update.callback_query:
        await update.callback_query.answer()
        days = None
    else:
        t = update.message.text.strip()
        try:
            days = None if t == "-" else parse_nonneg(t, as_int=True)
        except ValueError:
            await update.message.reply_text("⚠️ عدد صحیح یا - بفرستید:")
            return E_DAYS
    try:
        api = get_api(eu["pid"])
        u = await C.apply_user_edit(api, eu["u"], eu.get("gb"), days)
        text, kb = await user_card(u, api)
        await context.bot.send_message(update.effective_chat.id, "✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode=HTML)
    except Exception as e:  # noqa: BLE001
        await context.bot.send_message(update.effective_chat.id, err_text(e), reply_markup=main_kb(), parse_mode=HTML)
    context.user_data.clear()
    return END


# ───────────────────────────── لیست، آمار، پاکسازی، خروجی ─────────────────────────────
@nav
async def list_users(update, context):
    q = update.callback_query
    await q.answer()
    _, st, off_s = q.data.split(":")
    off = int(off_s)
    api = get_api()
    try:
        params = {"offset": off, "limit": 10}
        if st != "all":
            params["status"] = st
        res = await api.request("GET", "/api/users", params=params)
    except Exception as e:  # noqa: BLE001
        return await show(update, context, err_text(e), main_kb())
    users, total = res.get("users", []), res.get("total", 0)
    flt = [("همه", "all"), ("🟢", "active"), ("⏳", "on_hold"), ("🔴", "expired"), ("⚫", "disabled")]
    rows = [[B(("• " if k == st else "") + n, callback_data=f"l:{k}:0") for n, k in flt]]
    for u in users:
        rows.append([B(f"{STATUS_ICON.get(u['status'], '❔')} {u['username']} | {C.fmt_gb(u.get('used_traffic', 0))}/{C.fmt_gb(u.get('data_limit', 0))}",
                       callback_data=f"u:view:{api.pid}:{u['username']}")])
    n = []
    if off >= 10:
        n.append(B("⬅️ قبلی", callback_data=f"l:{st}:{off - 10}"))
    if off + 10 < total:
        n.append(B("بعدی ➡️", callback_data=f"l:{st}:{off + 10}"))
    if n:
        rows.append(n)
    rows.append([B("🔙 منوی اصلی", callback_data="back")])
    await show(update, context, f"📋 {esc(api.name)} — کاربران ({st}) — کل: <b>{total}</b>", M(rows))


@nav
async def stats(update, context):
    await update.callback_query.answer()
    api = get_api()
    try:
        s = await api.request("GET", "/api/system")
    except Exception as e:  # noqa: BLE001
        return await show(update, context, err_text(e), main_kb())
    mem_t = s.get("mem_total", 0) or 0
    ram_p = f" ({s.get('mem_used', 0) * 100 / mem_t:.0f}%)" if mem_t else ""
    text = (f"📊 <b>وضعیت {esc(api.name)}</b>\n"
            f"👥 کل: <b>{s.get('total_user', 0)}</b> | 🟢 فعال: <b>{s.get('users_active', 0)}</b> | 🌐 آنلاین: <b>{s.get('online_users', 0)}</b>\n"
            f"⏳ On-Hold: {s.get('users_on_hold', 0)} | 🔴 منقضی: {s.get('users_expired', 0)} | 🟠 محدود: {s.get('users_limited', 0)} | ⚫ غیرفعال: {s.get('users_disabled', 0)}\n"
            f"🖥 CPU: {float(s.get('cpu_usage') or 0):.0f}% | 🧠 RAM: {C.fmt_bw(s.get('mem_used', 0))}/{C.fmt_bw(mem_t)}{ram_p}\n"
            f"🌐 ↓ <code>{C.fmt_bw(s.get('incoming_bandwidth', 0))}</code>  ↑ <code>{C.fmt_bw(s.get('outgoing_bandwidth', 0))}</code>")
    await show(update, context, text, M([[B("🔄 ریستارت هسته Xray", callback_data=f"core:ask:{api.pid}")], [B("🔙 منوی اصلی", callback_data="back")]]))


@nav
async def core_cb(update, context):
    q = update.callback_query
    _, kind, pid = q.data.split(":")
    api = get_api(pid)
    if kind == "ask":
        await q.answer()
        return await show(update, context, f"⚠️ هسته Xray پنل «{esc(api.name)}» ریستارت شود؟ اتصال کاربران چند ثانیه قطع می‌شود.",
                          M([[B("✅ بله", callback_data=f"core:go:{pid}"), B("↩️ خیر", callback_data="stats")]]))
    try:
        await api.request("POST", "/api/core/restart")
        await q.answer("✅ ریستارت شد", show_alert=True)
    except Exception as e:  # noqa: BLE001
        await q.answer(f"❌ {str(e)[:180]}", show_alert=True)


@nav
async def clean_cb(update, context):
    q = update.callback_query
    await q.answer()
    _, kind, pid = q.data.split(":")
    api = get_api(pid)
    try:
        users = await api.iter_users("expired") + await api.iter_users("limited")
    except Exception as e:  # noqa: BLE001
        return await show(update, context, err_text(e), main_kb())
    if kind == "ask":
        if not users:
            return await show(update, context, "✅ کاربر منقضی/محدودشده‌ای وجود ندارد.", main_kb())
        return await show(update, context, f"⚠️ در پنل «{esc(api.name)}» تعداد <b>{len(users)}</b> کاربر منقضی/محدودشده برای همیشه حذف شوند؟",
                          M([[B("✅ حذف شوند", callback_data=f"clean:go:{pid}"), B("↩️ انصراف", callback_data="back")]]))
    await show(update, context, f"⏳ در حال حذف {len(users)} کاربر…")
    n = await C.delete_users(api, [u["username"] for u in users])
    await show(update, context, f"🧹 {n} از {len(users)} کاربر حذف شد.", main_kb())


@nav
async def export_cb(update, context):
    await update.callback_query.answer("آماده‌سازی فایل…")
    api = get_api()
    try:
        users = await api.iter_users()
    except Exception as e:  # noqa: BLE001
        return await show(update, context, err_text(e), main_kb())
    txt = "\n".join(f"{u['username']} | {u['status']} | {api.full_sub(u.get('subscription_url', ''))}" for u in users)
    bio = io.BytesIO(txt.encode("utf-8"))
    bio.name = f"all_subs_{time.strftime('%Y%m%d')}.txt"
    await context.bot.send_document(update.effective_chat.id, bio, caption=f"📥 {api.name} — کل کاربران ({len(users)})")


# ───────────────────────────── گزارش فروش ─────────────────────────────
def _report_text(which: str) -> str:
    today = sales.tehran_day()
    if which == "today":
        return sales.build_report(today, True, True)
    if which == "yday":
        return sales.build_report(sales.shift_day(today, -1), True, False)
    return sales.build_range_report(which, True)


@nav
async def report_cb(update, context):
    """از منو: گزارش را به‌صورت پیام جدید می‌فرستد؛ دکمه‌ی به‌روزرسانی همان پیام را ویرایش می‌کند."""
    q = update.callback_query
    parts = q.data.split(":")
    which, refresh = parts[1], len(parts) > 2
    await q.answer("در حال خواندن گزارش از دیتابیس…")
    text = await asyncio.to_thread(_report_text, which)
    kb = M([[B("🔄 به‌روزرسانی", callback_data=f"rp:{which}:r"), B("🔙 منوی اصلی", callback_data="back")]])
    if refresh:
        await show(update, context, text, kb)
    else:
        await context.bot.send_message(update.effective_chat.id, text[:4000], reply_markup=kb, parse_mode=HTML)


@nav
async def report_cmd(update, context):
    """/report | /report yesterday | /report week | /report month"""
    arg = (context.args[0].lower() if context.args else "today")
    which = {"yesterday": "yday", "yday": "yday", "week": "week", "month": "month", "lmonth": "lmonth", "last": "lmonth"}.get(arg, "today")
    text = await asyncio.to_thread(_report_text, which)
    await update.message.reply_html(text[:4000], reply_markup=main_kb())


# ───────────────────── تنظیمات فروش (قیمت/حجم/دیتابیس) ─────────────────────
def _prices_text() -> str:
    sc = load_config()["sales"]
    lines = ["⚙️ <b>تنظیمات فروش</b>", "",
             f"🤝 همکاری: هر گیگ <b>{sales._t(sc['hamkari_per_gb'])}</b> تومان",
             f"🧾 خرید: هر ترابایت <b>{sales._t(sc.get('buy_per_tb', 750000))}</b> تومان | ضریب مصرف همکاری: <b>{sc.get('hamkari_usage_multiplier', 1.5)}</b> ({'🟢 روشن' if sc.get('hamkari_multiplier_enabled', True) else '⚪ خاموش'})",
             "", "👤 لیست قیمت تکی:"]
    for x in sorted(sc["single_prices"], key=lambda x: (x["months"], x["gb"])):
        lines.append(f"  {x['gb']} گیگ {x['months']} ماهه = {sales._t(x['price'])}")
    lines += ["", "<b>دستورات:</b>",
              "<code>/setham 3000</code> — قیمت هر گیگ همکاری",
              "<code>/setplan 60 1 250000</code> — افزودن/ویرایش پلن (گیگ، ماه، قیمت)",
              "<code>/delplan 60 1</code> — حذف پلن",
              "<code>/setcost 750000</code> — قیمت خرید هر ترابایت",
              "<code>/setmult 1.5</code> — ضریب مصرف همکاری | <code>/setmult off</code> خاموش | <code>/setmult on</code> روشن",
              "<code>/setdb host port user pass dbname</code> — اتصال MySQL (فقط خواندن)",
              "<code>/dbcheck</code> — بررسی اتصال و ساختار جدول‌ها",
              "<code>/events</code> یا <code>/events yesterday</code> — فهرست تک‌تک ساخت/تمدیدها"]
    return "\n".join(lines)


@admin_only
async def prices_cmd(update, context):
    await update.message.reply_html(_prices_text())


def _arg_err(update, usage):
    return update.message.reply_html(f"❌ فرمت درست: <code>{usage}</code>")


@admin_only
async def events_cmd(update, context):
    which = (context.args[0] if context.args else "today").lower()
    today = sales.tehran_day()
    day = sales.shift_day(today, -1) if which in ("yesterday", "yday", "dirooz") else today
    text = await asyncio.to_thread(sales.build_events, day, True, day == today)
    await update.message.reply_html(f"<pre>{text}</pre>"[:4000])


@admin_only
async def setham_cmd(update, context):
    try:
        v = int(context.args[0])
    except (IndexError, ValueError):
        return await _arg_err(update, "/setham 3000")
    update_config(lambda c: c["sales"].__setitem__("hamkari_per_gb", v))
    await update.message.reply_html("✅ ذخیره شد.\n\n" + _prices_text())


@admin_only
async def setplan_cmd(update, context):
    try:
        gb, mo, pr = int(context.args[0]), int(context.args[1]), int(context.args[2])
    except (IndexError, ValueError):
        return await _arg_err(update, "/setplan 60 1 250000")

    def mut(c):
        lst = [x for x in c["sales"]["single_prices"] if not (x["gb"] == gb and x["months"] == mo)]
        lst.append({"gb": gb, "months": mo, "price": pr})
        c["sales"]["single_prices"] = lst
    update_config(mut)
    await update.message.reply_html("✅ ذخیره شد.\n\n" + _prices_text())


@admin_only
async def delplan_cmd(update, context):
    try:
        gb, mo = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        return await _arg_err(update, "/delplan 60 1")
    update_config(lambda c: c["sales"].__setitem__("single_prices", [x for x in c["sales"]["single_prices"] if not (x["gb"] == gb and x["months"] == mo)]))
    await update.message.reply_html("✅ حذف شد.\n\n" + _prices_text())


@admin_only
async def setcost_cmd(update, context):
    try:
        v = int(context.args[0])
    except (IndexError, ValueError):
        return await _arg_err(update, "/setcost 750000")
    update_config(lambda c: c["sales"].__setitem__("buy_per_tb", v))
    await update.message.reply_html("✅ ذخیره شد.\n\n" + _prices_text())


@admin_only
async def setmult_cmd(update, context):
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        update_config(lambda c: c["sales"].__setitem__("hamkari_multiplier_enabled", arg == "on"))
    else:
        try:
            v = float(arg)
            if v <= 0:
                raise ValueError
        except ValueError:
            return await _arg_err(update, "/setmult 1.5  یا  /setmult off  یا  /setmult on")
        update_config(lambda c: c["sales"].update(hamkari_usage_multiplier=v, hamkari_multiplier_enabled=True))
    await update.message.reply_html("✅ ذخیره شد.\n\n" + _prices_text())


@admin_only
async def setdb_cmd(update, context):
    a = context.args
    if len(a) != 5 or not a[1].isdigit():
        return await _arg_err(update, "/setdb 127.0.0.1 3306 user pass dbname")
    update_config(lambda c: c["sales"].__setitem__("db", {"host": a[0], "port": int(a[1]), "user": a[2], "password": a[3], "name": a[4]}))
    try:
        await update.message.delete()  # پیام حاوی رمز را پاک کن
    except TelegramError:
        pass
    await context.bot.send_message(update.effective_chat.id, "✅ تنظیمات دیتابیس ذخیره شد. برای تست: /dbcheck")


@admin_only
async def dbcheck_cmd(update, context):
    try:
        txt = await asyncio.to_thread(sales.db_check)
    except Exception as e:  # noqa: BLE001
        txt = f"❌ {e}"
    await update.message.reply_text(txt[:4000])


# ───────────────────────────── هشدار، مانیتور، پشتیبان، راهنما ─────────────────────────────
@admin_only
async def remind_cmd(update, context):
    a = context.args

    def status() -> str:
        rc = load_config()["reminders"]
        return (f"⏰ هشدار انقضا: <b>{'روشن' if rc['enabled'] else 'خاموش'}</b> — هر روز ساعت {rc['hour']} (تهران) | "
                f"تا {rc['days']} روز مانده یا کمتر از {rc['low_pct']}٪ حجم")
    if a and a[0] in ("on", "off"):
        update_config(lambda c: c["reminders"].__setitem__("enabled", a[0] == "on"))
    elif len(a) == 3 and all(x.isdigit() for x in a) and 1 <= int(a[0]) <= 23:
        update_config(lambda c: c["reminders"].update(hour=int(a[0]), days=max(1, int(a[1])), low_pct=max(1, int(a[2]))))
    elif a:
        return await _arg_err(update, "/remind on | off | 10 3 10  (ساعت، روز، درصد حجم)")
    await update.message.reply_html(status() + "\n\nفهرست لحظه‌ای: /soon")


@admin_only
async def monitor_cmd(update, context):
    a = context.args
    if a and a[0] in ("on", "off"):
        update_config(lambda c: c["monitor"].__setitem__("enabled", a[0] == "on"))
    elif a:
        return await _arg_err(update, "/monitor on | off")
    mc = load_config()["monitor"]
    await update.message.reply_html(f"🩺 مانیتور پنل‌ها: <b>{'روشن' if mc['enabled'] else 'خاموش'}</b> — بررسی هر {mc['interval'] // 60} دقیقه؛ فقط هنگام قطع/وصل پیام می‌دهد.")


@admin_only
async def backup_cmd(update, context):
    with open(C.CONFIG_FILE, "rb") as f:
        bio = io.BytesIO(f.read())
    bio.name = f"rebecca-config-{time.strftime('%Y%m%d')}.json"
    await update.message.reply_document(bio, caption="💾 پشتیبان تنظیمات — شامل توکن ربات و رمز پنل‌هاست؛ جای امن نگه دارید و جایی منتشر نکنید.")


@admin_only
async def autobackup_cmd(update, context):
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        update_config(lambda c: c["backup"].__setitem__("enabled", arg == "on"))
    on = load_config()["backup"]["enabled"]
    await update.message.reply_html(f"💾 بکاپ خودکار شبانه (همراه گزارش ۰۰:۰۰): <b>{'روشن ✅' if on else 'خاموش ⛔'}</b>\n"
                                    "<code>/autobackup on</code> یا <code>/autobackup off</code>")


@admin_only
async def help_cmd(update, context):
    await update.message.reply_html(
        "📖 <b>راهنمای دستورها</b>\n\n"
        "/start — منوی اصلی\n/report [yesterday|week|month|lmonth] — گزارش فروش\n/events — جزئیات ساخت/تمدیدها\n"
        "/soon — کاربران نزدیک انقضا/اتمام حجم\n/remind — هشدار روزانه انقضا\n/monitor — مانیتور سلامت پنل‌ها\n"
        "/prices — قیمت‌ها و تنظیمات فروش\n/dbcheck — بررسی اتصال دیتابیس\n/backup — پشتیبان تنظیمات\n/autobackup — بکاپ خودکار شبانه\n/cancel — لغو")


# ───────────────────────────── راه‌اندازی ─────────────────────────────
async def on_error(update, context):
    log.error("خطا در پردازش آپدیت", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("❌ خطای داخلی؛ دوباره تلاش کنید.", show_alert=True)
    except TelegramError:
        pass


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "منوی اصلی"), BotCommand("report", "گزارش فروش (امروز/week/month)"), BotCommand("soon", "نزدیک انقضا"),
        BotCommand("prices", "قیمت‌ها و تنظیمات فروش"), BotCommand("help", "راهنما"), BotCommand("cancel", "لغو عملیات جاری")])
    # ردیاب فروش و گزارش روزانه ۰۰:۰۰ (به وقت ایران)
    app.bot_data["bg"] = [asyncio.create_task(sales.watch_loop()), asyncio.create_task(sales.report_loop(app.bot)),
                          asyncio.create_task(ops.monitor_loop(app.bot)), asyncio.create_task(ops.reminder_loop(app.bot))]


def build_app() -> Application:
    cfg = load_config()
    if not cfg["bot_token"]:
        raise SystemExit("bot_token در config.json خالی است. نصب را دوباره اجرا کنید یا وب‌پنل > تنظیمات را پر کنید.")
    app = Application.builder().token(cfg["bot_token"]).concurrent_updates(True).post_init(post_init).build()

    txt = filters.TEXT & ~filters.COMMAND
    cb = CallbackQueryHandler
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd), CommandHandler("cancel", cancel_cmd), CommandHandler("report", report_cmd),
            cb(back_cb, pattern=r"^back$"), cb(report_cb, pattern=r"^rp:(today|yday|week|month|lmonth)(:r)?$"),
            CommandHandler("soon", soon_cb), cb(soon_cb, pattern=r"^soon$"),
            cb(bulk_start, pattern=r"^bk:start$"), cb(note_start, pattern=r"^u:note:"),
            cb(create_entry, pattern=r"^c:(quick|qg|pick)$"),
            cb(tpl_new, pattern=r"^t:new$"), cb(tpl_edit_field, pattern=r"^te:(name|gb|days):\d+$"),
            cb(setup_start, pattern=r"^pn:(new|edit:\w+)$"), cb(search_start, pattern=r"^search$"),
            cb(edit_start, pattern=r"^u:edit:"),
            cb(panel_list, pattern=r"^pn:list$"), cb(panel_action, pattern=r"^pn:(set|del|delok):\w+$"),
            # مستقل‌ها: هرکدام گفتگوی باز را می‌بندند
            cb(tpl_menu, pattern=r"^t:menu$"), cb(tpl_action, pattern=r"^t:(sel|del|mode|kind|edit):\d+$"),
            cb(list_users, pattern=r"^l:"), cb(stats, pattern=r"^stats$"), cb(core_cb, pattern=r"^core:(ask|go):\w+$"),
            cb(clean_cb, pattern=r"^clean:(ask|go):\w+$"), cb(export_cb, pattern=r"^export$"),
            cb(user_action, pattern=r"^u:(view|qr|rst|rev|tg|del|delok|svc|renew):"),
            cb(user_apply, pattern=r"^u:(svs|rnw|rnok):"),
        ],
        states={
            C_TPL: [cb(create_pick_tpl, pattern=r"^c:t:\d+$")],
            C_PANEL: [cb(create_pick_panel, pattern=r"^c:p:\w+$")],
            S_NAME: [MessageHandler(txt, setup_name)],
            C_BASE: [cb(create_auto, pattern=r"^c:auto$"), MessageHandler(txt, create_base)],
            C_START: [cb(create_next, pattern=r"^c:next$"), cb(create_start, pattern=r"^c:single$"), MessageHandler(txt, create_start)],
            C_COUNT: [cb(create_one, pattern=r"^c:one$"), MessageHandler(txt, create_count)],
            C_SERVICE: [cb(create_service, pattern=r"^srv:\d+$")],
            C_CONFIRM: [cb(create_go, pattern=r"^c:go$"), cb(create_change_service, pattern=r"^c:svc$")],
            T_NAME: [MessageHandler(txt, tpl_new_name)], T_GB: [MessageHandler(txt, tpl_new_gb)],
            T_DAYS: [MessageHandler(txt, tpl_new_days)], T_EDIT: [MessageHandler(txt, tpl_edit_value)],
            S_URL: [MessageHandler(txt, setup_url)],
            S_SUB: [cb(setup_sub, pattern=r"^s:skip$"), MessageHandler(txt, setup_sub)],
            S_USER: [MessageHandler(txt, setup_user)], S_PASS: [MessageHandler(txt, setup_pass)],
            Q_SEARCH: [MessageHandler(txt, search_do)],
            E_GB: [cb(edit_gb, pattern=r"^e:keep$"), MessageHandler(txt, edit_gb)],
            E_DAYS: [cb(edit_days, pattern=r"^e:keep$"), MessageHandler(txt, edit_days)],
            N_TEXT: [MessageHandler(txt, note_text)],
            B_PATTERN: [MessageHandler(txt, bulk_pattern)],
            B_ACTION: [cb(bulk_action, pattern=r"^bk:a:\w+$")],
            B_PICK: [cb(bulk_pick, pattern=r"^bk:(s|t):\d+$")],
            B_CONFIRM: [cb(bulk_go, pattern=r"^bk:go$")],
        },
        fallbacks=[CommandHandler("start", start_cmd), CommandHandler("cancel", cancel_cmd), cb(back_cb, pattern=r"^back$"), cb(noop_cb, pattern=r"^noop$")],
        allow_reentry=True,
    )
    for name, fn in (("prices", prices_cmd), ("events", events_cmd), ("setham", setham_cmd), ("setplan", setplan_cmd), ("delplan", delplan_cmd),
                     ("setcost", setcost_cmd), ("setmult", setmult_cmd), ("setdb", setdb_cmd), ("dbcheck", dbcheck_cmd),
                     ("remind", remind_cmd), ("monitor", monitor_cmd), ("backup", backup_cmd), ("autobackup", autobackup_cmd), ("help", help_cmd)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_error_handler(on_error)
    return app


def main():
    app = build_app()
    log.info("✅ Rebecca bot started")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
