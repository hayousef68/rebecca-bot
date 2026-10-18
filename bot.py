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
 Q_SEARCH, E_GB, E_DAYS, C_PANEL, S_NAME) = range(19)

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
        [B("⚡ ساخت سریع", callback_data="c:quick"), B("🎯 ساخت با انتخاب تمپلت", callback_data="c:pick")],
        [B("🔍 جستجو و مدیریت کاربر", callback_data="search"), B("📋 لیست کاربران", callback_data="l:all:0")],
        [B("⚙️ تمپلت‌ها", callback_data="t:menu"), B("📊 وضعیت سرور", callback_data="stats")],
        [B("🧹 پاکسازی منقضی‌ها", callback_data=f"clean:ask:{pid}"), B("📥 خروجی لینک‌ها", callback_data="export")],
        [B("📈 گزارش فروش امروز", callback_data="rp:today"), B("📈 دیروز", callback_data="rp:yday")],
        [B(f"🖥 پنل‌ها (فعال: {pn['name']})", callback_data="pn:list")],
    ])


def home_text() -> str:
    cfg = load_config()
    t = cfg["templates"][cfg["active_template"]]
    pn = get_panel(cfg, cfg["active_panel"])
    st = "🟢 متصل" if get_api().is_configured() else "🔴 تنظیم نشده"
    return (f"🚀 <b>ربات مدیریت پنل ربکا</b>\n🖥 پنل فعال: <b>{esc(pn['name']) if pn else '—'}</b> ({st})\n\n"
            f"📌 تمپلت فعال: <b>{esc(cfg['active_template'])}</b>\n{esc(C.tpl_summary(t))}")


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
        for i, (n, t) in enumerate(cfg["templates"].items()):
            mark = "✅ " if n == cfg["active_template"] else ""
            rows.append([B(f"{mark}{n} ({t['gb']}GB/{t['days']}d)", callback_data=f"c:t:{i}")])
        await show(update, context, "🎯 تمپلت ساخت را انتخاب کنید:", cancel_kb(rows))
        return C_TPL
    context.user_data["cr"] = {"tpl": cfg["active_template"]}
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


async def ask_base(update, context):
    cfg = load_config()
    cr = context.user_data["cr"]
    name = cr["tpl"]
    t = cfg["templates"][name]
    pn = get_panel(cfg, cr["pid"])
    await show(update, context,
               f"➕ <b>ساخت اکانت</b>\n🖥 پنل: <b>{esc(pn['name'])}</b>\n📌 تمپلت: <b>{esc(name)}</b>\n{esc(C.tpl_summary(t))}\n\n"
               "👤 <b>نام پایه</b> را بفرستید:\n"
               "• <code>ali</code> ← یک کاربر\n"
               "• <code>ali 1 10</code> ← ۱۰ کاربر: ali1 تا ali10",
               cancel_kb())
    return C_BASE


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
    await update.message.reply_html(
        "🔢 عدد شروع شماره‌گذاری را بفرستید، یا «تک کاربر» را بزنید:",
        reply_markup=cancel_kb([[B("تک کاربر (بدون عدد)", callback_data="c:single")]]),
    )
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
        f"📦 <b>تعداد</b> کاربران را بفرستید (حداکثر {load_config()['max_batch']}):", reply_markup=cancel_kb()
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
    rows, row = [], []
    for s in services:
        row.append(B(f"📂 {s['name']}", callback_data=f"srv:{s['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await context.bot.send_message(update.effective_chat.id, "📂 <b>سرویس</b> را انتخاب کنید:", reply_markup=cancel_kb(rows), parse_mode=HTML)
    return C_SERVICE


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
    kb = cancel_kb([[B("✅ شروع ساخت", callback_data="c:go")]])
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
    header = f"🖥 {pname} | 💾 {tpl['gb'] or 'نامحدود'} GB | 📅 {tpl['days'] or 'نامحدود'} روز ({tpl['mode']}) | 📂 {cr['services'].get(cr['service_id'], cr['service_id'])}"
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
    for i, (n, t) in enumerate(cfg["templates"].items()):
        mark = "✅" if n == cfg["active_template"] else "📁"
        rows.append([B(f"{mark} {n} ({t['gb']}GB/{t['days']}d)", callback_data=f"t:sel:{i}")])
        rows.append([B("✏️ ویرایش", callback_data=f"t:edit:{i}"), B("🗑 حذف", callback_data=f"t:del:{i}")])
    rows += [[B("➕ تمپلت جدید", callback_data="t:new")], [B("🔙 منوی اصلی", callback_data="back")]]
    await show(update, context, prefix + "⚙️ <b>مدیریت تمپلت‌ها</b>\nروی نام بزنید تا فعال شود:", M(rows))


@nav
async def tpl_action(update, context):
    q = update.callback_query
    _, act, idx = q.data.split(":")
    name = tpl_by_idx(int(idx))
    if not name:
        await q.answer("تمپلت پیدا نشد", show_alert=True)
        return await render_tpl_menu(update, context)
    if act == "sel":
        update_config(lambda c: c.__setitem__("active_template", name))
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
        c["templates"][nt["name"]] = {"gb": nt["gb"], "days": days, "mode": "on_hold"}
        c["active_template"] = nt["name"]
    update_config(add)
    await render_tpl_menu(update, context, f"✅ تمپلت «{esc(nt['name'])}» ساخته و فعال شد.\n\n")
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
def render_user(u: dict, api):
    pid = api.pid
    name, st = u["username"], u.get("status", "unknown")
    used, lim = u.get("used_traffic", 0) or 0, u.get("data_limit") or 0
    pct = f" ({used * 100 / lim:.0f}%)" if lim else ""
    sub = api.full_sub(u.get("subscription_url", "")) or "نامشخص"
    pn = esc(api.name)
    text = (f"👤 <b>{esc(name)}</b> ({pn})\n{STATUS_ICON.get(st, '❔')} وضعیت: {esc(st)}\n"
            f"📊 مصرف: {C.fmt_gb(used)} / {C.fmt_gb(lim)}{pct}\n"
            f"⏳ اعتبار: {C.fmt_expire(u.get('expire'), st, u.get('on_hold_expire_duration'))}\n\n🔗 <code>{esc(sub)}</code>")
    tg = "✅ فعال‌سازی" if st == "disabled" else "⛔ غیرفعال‌سازی"
    kb = M([
        [B("🖼 QR", callback_data=f"u:qr:{pid}:{name}"), B("✏️ ویرایش", callback_data=f"u:edit:{pid}:{name}")],
        [B("🔄 صفر کردن مصرف", callback_data=f"u:rst:{pid}:{name}"), B("🔗 لینک جدید", callback_data=f"u:rev:{pid}:{name}")],
        [B(tg, callback_data=f"u:tg:{pid}:{name}"), B("🗑 حذف", callback_data=f"u:del:{pid}:{name}")],
        [B("🔙 منوی اصلی", callback_data="back")],
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
            text, kb = render_user(u, api)
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
        text, kb = render_user(await api.request("GET", p), api)
        await show(update, context, text, kb)
    except Exception as e:  # noqa: BLE001
        await q.answer(f"❌ {str(e)[:180]}", show_alert=True)


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
        text, kb = render_user(u, api)
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
    text = (f"📊 <b>وضعیت {esc(api.name)}</b>\n👥 کل: {s.get('total_user', 0)} | 🟢 فعال: {s.get('users_active', 0)} | ⏳ On-Hold: {s.get('users_on_hold', 0)}\n"
            f"🖥 CPU: {s.get('cpu_usage', 0)}% | 🧠 RAM: {C.fmt_gb(s.get('mem_used', 0))}/{C.fmt_gb(s.get('mem_total', 0))}\n"
            f"📥 {C.fmt_gb(s.get('incoming_bandwidth', 0))} | 📤 {C.fmt_gb(s.get('outgoing_bandwidth', 0))}")
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
@nav
async def report_cb(update, context):
    q = update.callback_query
    await q.answer("در حال بررسی تغییرات پنل‌ها…")
    today = sales.tehran_day()
    which = q.data.split(":")[1]
    day, partial = (today, True) if which == "today" else (sales.shift_day(today, -1), False)
    if partial:
        try:
            await asyncio.wait_for(sales.poll_all(), 60)  # آخرین تغییرات پنل قبل از گزارش
        except Exception as e:  # noqa: BLE001
            log.warning("poll before report: %s", e)
    text = await asyncio.to_thread(sales.build_report, day, partial=partial)
    await show(update, context, text, M([[B("🔄 به‌روزرسانی", callback_data=f"rp:{which}"), B("🔙 منوی اصلی", callback_data="back")]]))


@nav
async def report_cmd(update, context):
    await sales_reply(update, context)


async def sales_reply(update, context):
    try:
        await asyncio.wait_for(sales.poll_all(), 60)
    except Exception as e:  # noqa: BLE001
        log.warning("poll before report: %s", e)
    text = await asyncio.to_thread(sales.build_report, sales.tehran_day(), partial=True)
    await update.message.reply_html(text, reply_markup=main_kb())


# ───────────────────────────── راه‌اندازی ─────────────────────────────
async def on_error(update, context):
    log.error("خطا در پردازش آپدیت", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("❌ خطای داخلی؛ دوباره تلاش کنید.", show_alert=True)
    except TelegramError:
        pass


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "منوی اصلی"), BotCommand("report", "گزارش فروش امروز"), BotCommand("cancel", "لغو عملیات جاری")])
    # ردیاب فروش و گزارش روزانه ۰۰:۰۰ (به وقت ایران)
    app.bot_data["bg"] = [
        asyncio.create_task(sales.watch_loop()), asyncio.create_task(sales.report_loop(app.bot)),
        asyncio.create_task(ops.reminder_loop(app.bot)), asyncio.create_task(ops.monitor_loop(app.bot)),
    ]


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
            cb(back_cb, pattern=r"^back$"), cb(report_cb, pattern=r"^rp:(today|yday)$"),
            cb(create_entry, pattern=r"^c:(quick|pick)$"),
            cb(tpl_new, pattern=r"^t:new$"), cb(tpl_edit_field, pattern=r"^te:(name|gb|days):\d+$"),
            cb(setup_start, pattern=r"^pn:(new|edit:\w+)$"), cb(search_start, pattern=r"^search$"),
            cb(edit_start, pattern=r"^u:edit:"),
            cb(panel_list, pattern=r"^pn:list$"), cb(panel_action, pattern=r"^pn:(set|del|delok):\w+$"),
            # مستقل‌ها: هرکدام گفتگوی باز را می‌بندند
            cb(tpl_menu, pattern=r"^t:menu$"), cb(tpl_action, pattern=r"^t:(sel|del|mode|edit):\d+$"),
            cb(list_users, pattern=r"^l:"), cb(stats, pattern=r"^stats$"), cb(core_cb, pattern=r"^core:(ask|go):\w+$"),
            cb(clean_cb, pattern=r"^clean:(ask|go):\w+$"), cb(export_cb, pattern=r"^export$"),
            cb(user_action, pattern=r"^u:(view|qr|rst|rev|tg|del|delok):"),
        ],
        states={
            C_TPL: [cb(create_pick_tpl, pattern=r"^c:t:\d+$")],
            C_PANEL: [cb(create_pick_panel, pattern=r"^c:p:\w+$")],
            S_NAME: [MessageHandler(txt, setup_name)],
            C_BASE: [MessageHandler(txt, create_base)],
            C_START: [cb(create_start, pattern=r"^c:single$"), MessageHandler(txt, create_start)],
            C_COUNT: [MessageHandler(txt, create_count)],
            C_SERVICE: [cb(create_service, pattern=r"^srv:\d+$")],
            C_CONFIRM: [cb(create_go, pattern=r"^c:go$")],
            T_NAME: [MessageHandler(txt, tpl_new_name)], T_GB: [MessageHandler(txt, tpl_new_gb)],
            T_DAYS: [MessageHandler(txt, tpl_new_days)], T_EDIT: [MessageHandler(txt, tpl_edit_value)],
            S_URL: [MessageHandler(txt, setup_url)],
            S_SUB: [cb(setup_sub, pattern=r"^s:skip$"), MessageHandler(txt, setup_sub)],
            S_USER: [MessageHandler(txt, setup_user)], S_PASS: [MessageHandler(txt, setup_pass)],
            Q_SEARCH: [MessageHandler(txt, search_do)],
            E_GB: [cb(edit_gb, pattern=r"^e:keep$"), MessageHandler(txt, edit_gb)],
            E_DAYS: [cb(edit_days, pattern=r"^e:keep$"), MessageHandler(txt, edit_days)],
        },
        fallbacks=[CommandHandler("start", start_cmd), CommandHandler("cancel", cancel_cmd), cb(back_cb, pattern=r"^back$")],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_error_handler(on_error)
    return app


def main():
    app = build_app()
    log.info("✅ Rebecca bot started")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
