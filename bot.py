import io, os, json, time, asyncio, logging
from datetime import datetime
import httpx, qrcode
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# مراحل گفتگو (Conversation States) فقط برای ساخت دستی و سریع
(SET_PANEL_URL, SET_SUB_URL, SET_PANEL_USER, SET_PANEL_PASS, 
 ASK_SEARCH_USER, EDIT_DATA, EDIT_DAYS_USER,
 MAN_USERNAME, MAN_START, MAN_COUNT, MAN_SERVICE,
 QUICK_BASE, QUICK_START, QUICK_SERVICE) = range(14)

def load_config() -> dict:
    default_cfg = {
        "bot_token": "", "admin_ids": [], "panel_url": "", "sub_url": "", "username": "", "password": "",
        "templates": {
            "پیش‌فرض (30 گیگ - 30 روز)": {"gb": 30, "days": 30, "mode": "on_hold"},
            "تست (5 گیگ - 3 روز)": {"gb": 5, "days": 3, "mode": "active"}
        },
        "active_template": "پیش‌فرض (30 گیگ - 30 روز)"
    }
    if not os.path.exists(CONFIG_FILE):
        return default_cfg
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        if "templates" not in data:
            data["templates"] = default_cfg["templates"]
        if "active_template" not in data:
            data["active_template"] = list(data["templates"].keys())[0]
        return data

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

class RebeccaClient:
    def __init__(self):
        self.client = httpx.AsyncClient(verify=False, timeout=20.0, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))
        self.token = None
        self.reload_credentials()

    def reload_credentials(self):
        cfg = load_config()
        raw_url = cfg.get("panel_url", "").rstrip("/")
        if raw_url.endswith("/dashboard"):
            raw_url = raw_url[:-10]
        self.base_url = raw_url
        self.sub_url = cfg.get("sub_url", "").rstrip("/") or self.base_url
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.token = None

    def is_configured(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    async def login(self) -> str:
        endpoints = ["/api/admin/token", "/dashboard/api/admin/token", "/api/v1/admin/token"]
        for ep in endpoints:
            try:
                resp = await self.client.post(
                    f"{self.base_url}{ep}",
                    data={"username": self.username, "password": self.password},
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                if resp.status_code == 200:
                    self.token = resp.json()["access_token"]
                    return self.token
            except Exception:
                continue
        resp = await self.client.post(
            f"{self.base_url}/api/admin/token",
            data={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        return self.token

    async def req(self, method: str, endpoint: str, **kwargs):
        if not self.token:
            await self.login()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        resp = await self.client.request(method, f"{self.base_url}{endpoint}", headers=headers, **kwargs)
        if resp.status_code == 401:
            await self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            resp = await self.client.request(method, f"{self.base_url}{endpoint}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

api = RebeccaClient()

def is_admin(uid: int) -> bool:
    return uid in load_config().get("admin_ids", [])

def fmt_gb(size: int) -> str:
    return "نامحدود" if not size or size <= 0 else f"{size / (1024 ** 3):.2f} GB"

def fmt_expire(exp: int, status: str, on_hold_dur: int = None) -> str:
    if status == "on_hold":
        return f"{int(on_hold_dur // 86400) if on_hold_dur else 0} روز (On-Hold)"
    if not exp:
        return "نامحدود"
    rem = exp - int(time.time())
    return "منقضی شده" if rem <= 0 else f"{rem // 86400} روز و {(rem % 86400) // 3600} ساعت"

def full_sub(sub_path: str) -> str:
    if not sub_path: return "نامشخص"
    if sub_path.startswith("http"):
        parts = sub_path.split("://")
        if len(parts) > 1:
            path_part = "/" + "/".join(parts[1].split("/")[1:])
            return f"{api.sub_url}{path_part}"
        return sub_path
    return f"{api.sub_url}/{sub_path.lstrip('/')}"

def make_qr(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(version=1, box_size=9, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    bio = io.BytesIO()
    bio.name = "qr.png"
    qr.make_image(fill_color="black", back_color="white").save(bio, "PNG")
    bio.seek(0)
    return bio

def cancel_kb():
    return Markup([[Btn("❌ انصراف", callback_data="back_main")]])

def main_keyboard() -> Markup:
    cfg = load_config()
    if not api.is_configured():
        return Markup([[Btn("🔌 اتصال به پنل ربکا", callback_data="setup_panel")]])
    active_t = cfg.get("active_template", "پیش‌فرض")
    return Markup([
        [Btn(f"⚡ ساخت سریع (تمپلت: {active_t})", callback_data="quick_create"), Btn("➕ ساخت دستی", callback_data="manual_create")],
        [Btn("⚙️ مدیریت و انتخاب تمپلت‌ها", callback_data="menu_template"), Btn("📋 لیست کاربران", callback_data="list_users:all:0")],
        [Btn("🔍 جستجو و مدیریت کاربر", callback_data="menu_search"), Btn("📊 وضعیت سرور", callback_data="menu_stats")],
        [Btn("🧹 پاکسازی منقضی‌ها", callback_data="menu_cleanup"), Btn("📥 خروجی ساب‌ها", callback_data="export_all_subs")],
        [Btn("⚙️ تنظیمات اتصال پنل", callback_data="setup_panel")]
    ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        if update.message: await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return ConversationHandler.END
    context.user_data.clear()
    cfg = load_config()
    st = "🟢 متصل" if api.is_configured() else "🔴 تنظیم نشده"
    active_name = cfg.get("active_template", "نامشخص")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    text = (
        f"🚀 ربات مدیریت پنل ربکا\nوضعیت: {st}\n\n"
        f"📌 تمپلت فعال فعلی: {active_name}\n"
        f"💾 حجم: {t_data.get('gb')} GB | 📅 اعتبار: {t_data.get('days')} روز | ⏳ حالت: {t_data.get('mode')}\n\n"
        f"یک گزینه را انتخاب کنید:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard())
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_keyboard())
    return ConversationHandler.END

# --- واکشی سرویس‌ها ---
async def fetch_services_kb():
    return [
        [Btn("⭐ Hamkari (ID: 6)", callback_data="srv_id:6:Hamkari")],
        [Btn("📂 ms (ID: 1)", callback_data="srv_id:1:ms")],
        [Btn("📂 Tes (ID: 5)", callback_data="srv_id:5:Tes")]
    ]

# --- تنظیمات اتصال ---
async def start_setup_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🌐 آدرس کامل پنل را با پورت بفرستید:\nمثال: `https://pan.vip-sam-shop98.top:2096/dashboard`", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return SET_PANEL_URL

async def get_panel_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip().rstrip("/")
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ با http یا https شروع شود:")
        return SET_PANEL_URL
    context.user_data["setup_panel_url"] = url
    kb = Markup([[Btn("⏭ مشابه پنل", callback_data="skip_sub_url")], [Btn("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text("🔗 آدرس دامنه سابسکریپشن را بفرستید یا رد کنید:", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return SET_SUB_URL

async def get_sub_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data["setup_sub_url"] = context.user_data["setup_panel_url"]
        await update.callback_query.edit_message_text("👤 نام کاربری ادمین:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    else:
        context.user_data["setup_sub_url"] = update.message.text.strip().rstrip("/")
        await update.message.reply_text("👤 نام کاربری ادمین:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return SET_PANEL_USER

async def get_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["setup_user"] = update.message.text.strip()
    await update.message.reply_text("🔑 رمز عبور ادمین:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return SET_PANEL_PASS

async def get_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait_msg = await update.message.reply_text("⏳ در حال تست اتصال...")
    cfg = load_config()
    cfg.update({"panel_url": context.user_data["setup_panel_url"], "sub_url": context.user_data["setup_sub_url"], "username": context.user_data["setup_user"], "password": update.message.text.strip()})
    save_config(cfg)
    api.reload_credentials()
    try:
        await api.login()
        st = await api.req("GET", "/api/system")
        await wait_msg.edit_text(f"✅ اتصال برقرار شد!\n👥 کل کاربران: {st.get('total_user', 0)}", reply_markup=main_keyboard())
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در اتصال:\n{e}", reply_markup=main_keyboard())
    return ConversationHandler.END

# --- مدیریت تمپلت‌ها (طراحی مجدد برای جلوگیری از فریز شدن) ---
async def menu_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("tmpl_state", None)
    cfg = load_config()
    templates = cfg.get("templates", {})
    active = cfg.get("active_template")

    rows = []
    for t_name, data in templates.items():
        prefix = "✅ " if t_name == active else "📁 "
        rows.append([
            Btn(f"{prefix}{t_name} ({data['gb']}GB - {data['days']}d)", callback_data=f"tmpl_sel:{t_name}"),
            Btn("✏️ ویرایش", callback_data=f"tmpl_edit:{t_name}"),
            Btn("🗑 حذف", callback_data=f"tmpl_del:{t_name}")
        ])
    rows.append([Btn("➕ ساخت تمپلت جدید", callback_data="tmpl_new")])
    rows.append([Btn("🔙 منوی اصلی", callback_data="back_main")])

    await q.edit_message_text("⚙️ **مدیریت تمپلت‌ها**\nروی هر تمپلت کلیک کنید تا فعال شود، آن را ویرایش کنید یا تمپلت جدید بسازید:", reply_markup=Markup(rows), parse_mode=ParseMode.MARKDOWN)

async def template_action_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    cfg = load_config()

    if data == "tmpl_new":
        context.user_data["tmpl_state"] = "waiting_new_name"
        await q.edit_message_text("📝 نام تمپلت جدید را بفرستید (مثلاً: `اقتصادی`):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    
    act, t_name = data.split(":", 1)
    if act == "tmpl_sel":
        cfg["active_template"] = t_name
        save_config(cfg)
        await q.answer(f"تمپلت فعال روی '{t_name}' تنظیم شد.", show_alert=True)
        await menu_template(update, context)
    elif act == "tmpl_del":
        if len(cfg["templates"]) <= 1:
            await q.answer("⚠️ حداقل یک تمپلت باید باقی بماند!", show_alert=True)
            await menu_template(update, context)
            return
        del cfg["templates"][t_name]
        if cfg["active_template"] == t_name:
            cfg["active_template"] = list(cfg["templates"].keys())[0]
        save_config(cfg)
        await menu_template(update, context)
    elif act == "tmpl_edit":
        context.user_data["editing_tmpl"] = t_name
        kb = Markup([
            [Btn("💾 تغییر نام", callback_data=f"tfield:name:{t_name}"), Btn("📊 تغییر حجم", callback_data=f"tfield:gb:{t_name}")],
            [Btn("📅 تغییر روز", callback_data=f"tfield:days:{t_name}"), Btn("⏳ تغییر حالت", callback_data=f"tfield:mode:{t_name}")],
            [Btn("🔙 بازگشت", callback_data="menu_template")]
        ])
        t_data = cfg["templates"][t_name]
        await q.edit_message_text(f"✏️ ویرایش تمپلت: **{t_name}**\nحجم: {t_data['gb']}GB | روز: {t_data['days']} | حالت: {t_data['mode']}\n\nکدام ویژگی ویرایش شود؟", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def template_field_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, field, t_name = q.data.split(":")
    context.user_data["edit_field"] = field
    context.user_data["edit_t_name"] = t_name

    if field == "mode":
        cfg = load_config()
        curr = cfg["templates"][t_name]["mode"]
        cfg["templates"][t_name]["mode"] = "active" if curr == "on_hold" else "on_hold"
        save_config(cfg)
        await q.edit_message_text(f"✅ حالت تمپلت `{t_name}` تغییر کرد به: `{cfg['templates'][t_name]['mode']}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return
    
    context.user_data["tmpl_state"] = f"waiting_edit_{field}"
    prompts = {
        "name": "📝 نام جدید تمپلت را بفرستید:",
        "gb": "💾 حجم جدید به گیگابایت (`0` نامحدود):",
        "days": "📅 تعداد روز اعتبار جدید (`0` نامحدود):"
    }
    await q.edit_message_text(prompts.get(field, "مقدار جدید را بفرستید:"), reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)

async def template_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("tmpl_state")
    if not state:
        return

    text = update.message.text.strip()
    cfg = load_config()

    if state == "waiting_new_name":
        context.user_data["new_t_name"] = text
        context.user_data["tmpl_state"] = "waiting_new_gb"
        await update.message.reply_text("💾 حجم تمپلت را به **گیگابایت** بفرستید (`0` برای نامحدود):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    elif state == "waiting_new_gb":
        try:
            context.user_data["new_t_gb"] = float(text)
            context.user_data["tmpl_state"] = "waiting_new_days"
            await update.message.reply_text("📅 تعداد روزهای اعتبار تمپلت را بفرستید (`0` برای نامحدود):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("⚠️ لطفاً یک عدد معتبر وارد کنید:")
        return
    elif state == "waiting_new_days":
        try:
            days = int(text)
            t_name = context.user_data.get("new_t_name")
            gb = context.user_data.get("new_t_gb")
            cfg["templates"][t_name] = {"gb": gb, "days": days, "mode": "on_hold"}
            cfg["active_template"] = t_name
            save_config(cfg)
            context.user_data.clear()
            await update.message.reply_text(f"✅ تمپلت جدید **{t_name}** با موفقیت ساخته شد و فعال گردید!", reply_markup=main_keyboard())
        except ValueError:
            await update.message.reply_text("⚠️ لطفاً عدد صحیح وارد کنید:")
        return

    # ویرایش فیلدها
    field = context.user_data.get("edit_field")
    t_name = context.user_data.get("edit_t_name")

    if field == "name":
        if text in cfg["templates"]:
            await update.message.reply_text("⚠️ این نام قبلاً وجود دارد. نام دیگری بفرستید:")
            return
        cfg["templates"][text] = cfg["templates"].pop(t_name)
        if cfg["active_template"] == t_name:
            cfg["active_template"] = text
    elif field == "gb":
        try:
            cfg["templates"][t_name]["gb"] = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ عدد معتبر بفرستید:")
            return
    elif field == "days":
        try:
            cfg["templates"][t_name]["days"] = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ عدد صحیح بفرستید:")
            return

    save_config(cfg)
    context.user_data.clear()
    await update.message.reply_text("✅ تمپلت با موفقیت ویرایش شد!", reply_markup=main_keyboard())

# --- ساخت دستی و گروهی کاربر ---
async def manual_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    active_name = cfg.get("active_template", "پیش‌فرض")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    
    await q.edit_message_text(
        f"➕ **ساخت دستی / گروهی کاربر**\n\n"
        f"📌 تمپلت فعال: {active_name} (حجم: {t_data['gb']}GB | اعتبار: {t_data['days']}روز)\n\n"
        f"👤 لطفاً **نام پایه** کاربران را بفرستید (مثلاً `ali`):",
        reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN
    )
    return MAN_USERNAME

async def manual_get_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["man_base"] = update.message.text.strip()
    kb = Markup([[Btn("تک کاربر (بدون شماره‌گذاری)", callback_data="mannum_none")], [Btn("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text("🔢 عدد شروع شماره‌گذاری را بفرستید (یا گزینه تک کاربر را بزنید):", reply_markup=kb)
    return MAN_START

async def manual_get_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data.update({"man_start": None, "man_count": 1})
        srv_kb = await fetch_services_kb()
        srv_kb.append([Btn("❌ انصراف", callback_data="back_main")])
        await update.callback_query.edit_message_text("📂 لطفاً **سرویس مورد نظر** را انتخاب کنید:", reply_markup=Markup(srv_kb))
        return MAN_SERVICE
    else:
        try:
            context.user_data["man_start"] = int(update.message.text.strip())
            await update.message.reply_text("📦 لطفاً **تعداد** کاربرانی که می‌خواهید ساخته شوند را بفرستید (مثلاً `5`):", reply_markup=cancel_kb())
            return MAN_COUNT
        except ValueError:
            await update.message.reply_text("⚠️ عدد معتبر بفرستید:")
            return MAN_START

async def manual_get_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["man_count"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح بفرستید:")
        return MAN_COUNT

    srv_kb = await fetch_services_kb()
    srv_kb.append([Btn("❌ انصراف", callback_data="back_main")])
    await update.message.reply_text("📂 لطفاً **سرویس مورد نظر** را انتخاب کنید:", reply_markup=Markup(srv_kb))
    return MAN_SERVICE

async def manual_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    service_id = int(parts[1])
    service_name = parts[2]
    
    base = context.user_data.get("man_base")
    snum = context.user_data.get("man_start")
    count = context.user_data.get("man_count", 1)
    
    cfg = load_config()
    active_name = cfg.get("active_template")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    
    limit = int(t_data["gb"] * (1024 ** 3)) if t_data["gb"] > 0 else 0
    days = t_data["days"]
    mode = t_data["mode"]

    usernames = [base] if snum is None else [f"{base}{snum + i}" for i in range(count)]

    await q.edit_message_text("⚡ در حال ساخت ترتیبی و پشت سر هم اکانت‌ها...")

    ok = []
    chat_id = update.effective_chat.id

    for u in usernames:
        p = {
            "username": u,
            "data_limit": limit,
            "service_id": service_id,
            "status": mode
        }
        if mode == "on_hold":
            p["on_hold_expire_duration"] = int(days * 86400) if days > 0 else 2592000
        else:
            p["expire"] = int(time.time()) + (days * 86400) if days > 0 else 0

        try:
            r = await api.req("POST", "/api/user", json=p)
            sub_url = full_sub(r.get("subscription_url", ""))
            ok.append((u, sub_url))
        except Exception as e:
            logging.error(f"Failed to create user {u}: {e}")
            pass
        await asyncio.sleep(0.15)

    if not ok:
        await context.bot.send_message(chat_id=chat_id, text="❌ خطا در ساخت کاربران.", reply_markup=main_keyboard())
        return ConversationHandler.END

    for u, s in ok[:20]:
        cap = f"✅ کاربر: {u}\n💾 حجم: {t_data['gb']} GB | اعتبار: {days} روز ({mode})\n📂 سرویس: {service_name}\n\n🔗 لینک ساب (کلیک جهت کپی):\n`{s}`"
        await context.bot.send_photo(chat_id=chat_id, photo=make_qr(s), caption=cap, parse_mode=ParseMode.MARKDOWN)

    if len(ok) > 1:
        txt = "=== لیست کاربران و سابسکریپشن‌ها ===\n" + "\n".join(f"{u} | {s}" for u, s in ok)
        bio = io.BytesIO(txt.encode("utf-8"))
        bio.name = f"{usernames[0]}_to_{usernames[-1]}.txt"
        await context.bot.send_document(chat_id=chat_id, document=bio, caption=f"📂 لیست کامل ({len(ok)} کاربر)")

    await context.bot.send_message(chat_id=chat_id, text=f"🎉 تعداد {len(ok)} کاربر به صورت کاملاً ترتیبی ساخته شد.", reply_markup=main_keyboard())
    return ConversationHandler.END

# --- ساخت سریع با تمپلت ---
async def quick_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    active_name = cfg.get("active_template")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    
    await q.edit_message_text(
        f"⚡ **ساخت سریع (تمپلت: {active_name})**\nحجم: {t_data['gb']} GB | اعتبار: {t_data['days']} روز\n\n"
        f"👤 لطفاً **اسم پایه** کاربران را بفرستید (مثلاً `ali`):",
        reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN
    )
    return QUICK_BASE

async def quick_get_base_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["quick_base"] = update.message.text.strip()
    kb = Markup([[Btn("تک کاربر (بدون عدد)", callback_data="quicknum_none")], [Btn("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text("🔢 عدد شروع شماره‌گذاری را بفرستید (یا تک کاربر را انتخاب کنید):", reply_markup=kb)
    return QUICK_START

async def quick_get_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data.update({"quick_start": None, "quick_count": 1})
        srv_kb = await fetch_services_kb()
        srv_kb.append([Btn("❌ انصراف", callback_data="back_main")])
        await update.callback_query.edit_message_text("📂 لطفاً **سرویس مورد نظر** برای ساخت را انتخاب کنید:", reply_markup=Markup(srv_kb))
        return QUICK_SERVICE
    else:
        try:
            context.user_data["quick_start"] = int(update.message.text.strip())
            await update.message.reply_text("📦 لطفاً **تعداد** کاربرانی که می‌خواهید ساخته شوند را بفرستید (مثلاً `5`):", reply_markup=cancel_kb())
            return QUICK_START
        except ValueError:
            pass
    
    try:
        cnt = int(update.message.text.strip())
        context.user_data["quick_count"] = cnt
        srv_kb = await fetch_services_kb()
        srv_kb.append([Btn("❌ انصراف", callback_data="back_main")])
        await update.message.reply_text("📂 لطفاً **سرویس مورد نظر** برای ساخت را انتخاب کنید:", reply_markup=Markup(srv_kb))
        return QUICK_SERVICE
    except Exception:
        await update.message.reply_text("⚠️ لطفاً عدد معتبر وارد کنید:")
        return QUICK_START

async def execute_quick_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    service_id = int(parts[1])
    service_name = parts[2]

    await q.edit_message_text("⚡ در حال ساخت ترتیبی و پشت سر هم اکانت‌ها...")

    cfg = load_config()
    base = context.user_data.get("quick_base")
    snum = context.user_data.get("quick_start")
    count = context.user_data.get("quick_count", 10)
    
    active_name = cfg.get("active_template")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    
    limit = int(t_data["gb"] * (1024 ** 3)) if t_data["gb"] > 0 else 0
    days = t_data["days"]
    mode = t_data["mode"]

    usernames = [base] if snum is None else [f"{base}{snum + i}" for i in range(count)]

    ok = []
    chat_id = update.effective_chat.id

    for u in usernames:
        p = {
            "username": u,
            "data_limit": limit,
            "service_id": service_id,
            "status": mode
        }
        if mode == "on_hold":
            p["on_hold_expire_duration"] = int(days * 86400) if days > 0 else 2592000
        else:
            p["expire"] = int(time.time()) + (days * 86400) if days > 0 else 0

        try:
            r = await api.req("POST", "/api/user", json=p)
            sub_url = full_sub(r.get("subscription_url", ""))
            ok.append((u, sub_url))
        except Exception as e:
            logging.error(f"Failed to create user {u}: {e}")
            pass
        await asyncio.sleep(0.15)

    if not ok:
        await context.bot.send_message(chat_id=chat_id, text="❌ خطا در ساخت کاربران.", reply_markup=main_keyboard())
        return ConversationHandler.END

    for u, s in ok[:20]:
        cap = f"✅ کاربر: {u}\n💾 حجم: {t_data['gb']} GB | اعتبار: {days} روز ({mode})\n📂 سرویس: {service_name}\n\n🔗 لینک ساب (کلیک جهت کپی):\n`{s}`"
        await context.bot.send_photo(chat_id=chat_id, photo=make_qr(s), caption=cap, parse_mode=ParseMode.MARKDOWN)

    if len(ok) > 1:
        txt = "=== لیست کاربران و سابسکریپشن‌ها ===\n" + "\n".join(f"{u} | {s}" for u, s in ok)
        bio = io.BytesIO(txt.encode("utf-8"))
        bio.name = f"{usernames[0]}_to_{usernames[-1]}.txt"
        await context.bot.send_document(chat_id=chat_id, document=bio, caption=f"📂 لیست کامل ({len(ok)} کاربر)")

    await context.bot.send_message(chat_id=chat_id, text=f"🎉 تعداد {len(ok)} کاربر به صورت کاملاً ترتیبی ساخته شد.", reply_markup=main_keyboard())
    return ConversationHandler.END

# --- جستجو و مدیریت کاربر ---
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🔍 **نام کاربری** را جهت جستجو بفرستید:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return ASK_SEARCH_USER

def render_user(u: dict):
    uname, st = u["username"], u.get("status", "unknown")
    sub = full_sub(u.get("subscription_url", ""))
    txt = (f"👤 کاربر: {uname}\n📡 وضعیت: {st}\n📊 مصرف: {fmt_gb(u.get('used_traffic', 0))} / {fmt_gb(u.get('data_limit', 0))}\n"
           f"⏳ اعتبار: {fmt_expire(u.get('expire', 0), st, u.get('on_hold_expire_duration', 0))}\n\n🔗 لینک ساب (کلیک جهت کپی):\n`{sub}`")
    t_lbl = "🔴 غیرفعال" if st != "disabled" else "🟢 فعال"
    kb = Markup([
        [Btn("🖼 QR Code", callback_data=f"uact:qr:{uname}")],
        [Btn("🔄 صفر کردن مصرف", callback_data=f"uact:reset:{uname}"), Btn("✏️ ویرایش", callback_data=f"uact:edit:{uname}")],
        [Btn("🔗 تغییر ساب", callback_data=f"uact:revoke:{uname}"), Btn(t_lbl, callback_data=f"uact:toggle:{uname}:{st}")],
        [Btn("🗑 حذف کاربر", callback_data=f"uact:del:{uname}")], [Btn("🔙 منوی اصلی", callback_data="back_main")]
    ])
    return txt, kb

async def process_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uname = update.message.text.strip()
    try:
        u = await api.req("GET", f"/api/user/{uname}")
        txt, kb = render_user(u)
        await update.message.reply_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        res = await api.req("GET", "/api/users", params={"search": uname, "limit": 10})
        users = res.get("users", [])
        if not users:
            await update.message.reply_text("❌ کاربری یافت نشد. مجدداً بفرستید:")
            return ASK_SEARCH_USER
        rows = [[Btn(f"👤 {x['username']} ({x['status']})", callback_data=f"uact:view:{x['username']}")] for x in users] + [[Btn("🔙 منوی اصلی", callback_data="back_main")]]
        await update.message.reply_text("🔍 نتایج مشابه:", reply_markup=Markup(rows))
    return ConversationHandler.END

async def handle_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    act, uname = parts[1], parts[2]
    try:
        if act == "view":
            await q.answer()
            txt, kb = render_user(await api.req("GET", f"/api/user/{uname}"))
            await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        elif act == "qr":
            await q.answer("ساخت QR...")
            u = await api.req("GET", f"/api/user/{uname}")
            s = full_sub(u.get("subscription_url", ""))
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=make_qr(s), caption=f"👤 {uname}\n🔗 `{s}`", parse_mode=ParseMode.MARKDOWN)
        elif act == "reset":
            await api.req("POST", f"/api/user/{uname}/reset")
            await q.answer("✅ مصرف صفر شد.", show_alert=True)
            txt, kb = render_user(await api.req("GET", f"/api/user/{uname}"))
            await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        elif act == "revoke":
            u = await api.req("POST", f"/api/user/{uname}/revoke_sub")
            await q.answer("✅ لینک ساب جدید ساخته شد!", show_alert=True)
            txt, kb = render_user(u)
            await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        elif act == "toggle":
            nst = "disabled" if parts[3] != "disabled" else "active"
            u = await api.req("PUT", f"/api/user/{uname}", json={"status": nst})
            await q.answer(f"وضعیت: {nst}")
            txt, kb = render_user(u)
            await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        elif act == "del":
            await api.req("DELETE", f"/api/user/{uname}")
            await q.answer("🗑 حذف شد.", show_alert=True)
            await q.edit_message_text(f"✅ کاربر {uname} حذف شد.", reply_markup=main_keyboard())
    except Exception as e:
        await q.answer(f"❌ خطا: {e}", show_alert=True)

async def start_edit_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data["edit_uname"] = update.callback_query.data.split(":")[2]
    await update.callback_query.edit_message_text(f"✏️ حجم جدید `{context.user_data['edit_uname']}` را به گیگابایت بفرستید (`0` نامحدود):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return EDIT_DATA

async def edit_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["edit_limit"] = int(float(update.message.text.strip()) * (1024 ** 3))
    except ValueError:
        await update.message.reply_text("⚠️ عدد معتبر بفرستید:")
        return EDIT_DATA
    await update.message.reply_text("📅 تعداد روز اعتبار جدید را بفرستید (`0` نامحدود):")
    return EDIT_DAYS_USER

async def edit_get_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: days = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح بفرستید:")
        return EDIT_DAYS_USER
    uname, limit = context.user_data["edit_uname"], context.user_data["edit_limit"]
    exp = int(time.time()) + (days * 86400) if days > 0 else 0
    try:
        await api.req("POST", f"/api/user/{uname}/reset")
        u = await api.req("PUT", f"/api/user/{uname}", json={"data_limit": limit, "expire": exp, "status": "active"})
        txt, kb = render_user(u)
        await update.message.reply_text(f"✅ تمدید شد!\n\n{txt}", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}", reply_markup=main_keyboard())
    return ConversationHandler.END

async def list_users_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, st_f, off_s = q.data.split(":")
    off = int(off_s)
    params = {"offset": off, "limit": 10}
    if st_f != "all": params["status"] = st_f
    res = await api.req("GET", "/api/users", params=params)
    users, total = res.get("users", []), res.get("total", 0)
    rows = [[Btn("همه", callback_data="list_users:all:0"), Btn("🟢 فعال", callback_data="list_users:active:0"), Btn("⏳ On-Hold", callback_data="list_users:on_hold:0"), Btn("⚫ منقضی", callback_data="list_users:expired:0")]]
    for u in users:
        rows.append([Btn(f"👤 {u['username']} | {fmt_gb(u.get('used_traffic', 0))}/{fmt_gb(u.get('data_limit', 0))}", callback_data=f"uact:view:{u['username']}")])
    nav = []
    if off >= 10: nav.append(Btn("⬅️ قبلی", callback_data=f"list_users:{st_f}:{off - 10}"))
    if off + 10 < total: nav.append(Btn("بعدی ➡️", callback_data=f"list_users:{st_f}:{off + 10}"))
    if nav: rows.append(nav)
    rows.append([Btn("🔙 منوی اصلی", callback_data="back_main")])
    await q.edit_message_text(f"📋 کاربران ({st_f}) — کل: {total}", reply_markup=Markup(rows))

async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = await api.req("GET", "/api/system")
    txt = (f"📊 وضعیت سرور و پنل\n👥 کل: {st.get('total_user', 0)} | 🟢 فعال: {st.get('users_active', 0)} | ⏳ On-Hold: {st.get('users_on_hold', 0)}\n"
           f"🖥 CPU: {st.get('cpu_usage', 0)}% | 🧠 RAM: {fmt_gb(st.get('mem_used', 0))}/{fmt_gb(st.get('mem_total', 0))}\n"
           f"📥 دانلود: {fmt_gb(st.get('incoming_bandwidth', 0))} | 📤 آپلود: {fmt_gb(st.get('outgoing_bandwidth', 0))}")
    kb = Markup([[Btn("🔄 ریستارت هسته Xray", callback_data="restart_core")], [Btn("🔙 منوی اصلی", callback_data="back_main")]])
    await q.edit_message_text(txt, reply_markup=kb)

async def restart_core_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await api.req("POST", "/api/core/restart")
    await update.callback_query.answer("✅ هسته Xray ریستارت شد!", show_alert=True)

async def cleanup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("در حال حذف منقضی‌ها...")
    r1, r2 = await api.req("GET", "/api/users", params={"status": "expired"}), await api.req("GET", "/api/users", params={"status": "limited"})
    targets = [u["username"] for u in r1.get("users", []) + r2.get("users", [])]
    if not targets:
        await q.edit_message_text("✅ کاربر منقضی وجود ندارد.", reply_markup=main_keyboard())
        return
    sem = asyncio.Semaphore(10)
    async def d(u):
        async with sem: await api.req("DELETE", f"/api/user/{u}")
    await asyncio.gather(*(d(u) for u in targets), return_exceptions=True)
    await q.edit_message_text(f"🧹 تعداد {len(targets)} کاربر منقضی حذف شدند.", reply_markup=main_keyboard())

async def export_all_subs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("آماده‌سازی فایل...")
    res = await api.req("GET", "/api/users")
    users = res.get("users", [])
    bio = io.BytesIO("\n".join(f"{u['username']} | {u['status']} | {full_sub(u.get('subscription_url', ''))}" for u in users).encode("utf-8"))
    bio.name = f"all_subs_{datetime.now().strftime('%Y%m%d')}.txt"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=bio, caption=f"📥 کل کاربران ({len(users)})")

def main():
    cfg = load_config()
    app = Application.builder().token(cfg["bot_token"]).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd), 
            CallbackQueryHandler(start_setup_panel, pattern="^setup_panel$"),
            CallbackQueryHandler(quick_create_start, pattern="^quick_create$"),
            CallbackQueryHandler(manual_create_start, pattern="^manual_create$"),
            CallbackQueryHandler(menu_template, pattern="^menu_template$"),
            CallbackQueryHandler(start_search, pattern="^menu_search$"),
            CallbackQueryHandler(start_edit_user, pattern="^uact:edit:")
        ],
        states={
            SET_PANEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_url)],
            SET_SUB_URL: [CallbackQueryHandler(get_sub_url, pattern="^skip_sub_url$"), MessageHandler(filters.TEXT & ~filters.COMMAND, get_sub_url)],
            SET_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_user)],
            SET_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_pass)],
            
            # حالات ساخت دستی و گروهی
            MAN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_get_username)],
            MAN_START: [
                CallbackQueryHandler(manual_get_start, pattern="^mannum_none$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_get_start)
            ],
            MAN_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_get_count)],
            MAN_SERVICE: [CallbackQueryHandler(manual_finalize, pattern="^srv_id:")],

            # حالات ساخت سریع تمپلت
            QUICK_BASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_get_base_name)],
            QUICK_START: [
                CallbackQueryHandler(quick_get_start, pattern="^quicknum_none$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_get_start)
            ],
            QUICK_SERVICE: [CallbackQueryHandler(execute_quick_creation, pattern="^srv_id:")],

            ASK_SEARCH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_search)],
            EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_data)],
            EDIT_DAYS_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_days)],
        },
        fallbacks=[CommandHandler("start", start_cmd), CallbackQueryHandler(start_cmd, pattern="^back_main$")],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(start_cmd, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(menu_template, pattern="^menu_template$"))
    app.add_handler(CallbackQueryHandler(template_action_router, pattern="^(tmpl_sel|tmpl_del|tmpl_edit|tmpl_new):"))
    app.add_handler(CallbackQueryHandler(template_field_router, pattern="^tfield:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, template_text_handler))
    app.add_handler(CallbackQueryHandler(list_users_cb, pattern="^list_users:"))
    app.add_handler(CallbackQueryHandler(stats_cb, pattern="^menu_stats$"))
    app.add_handler(CallbackQueryHandler(restart_core_cb, pattern="^restart_core$"))
    app.add_handler(CallbackQueryHandler(cleanup_cb, pattern="^menu_cleanup$"))
    app.add_handler(CallbackQueryHandler(export_all_subs_cb, pattern="^export_all_subs$"))
    app.add_handler(CallbackQueryHandler(handle_user_action, pattern="^uact:(view|qr|reset|revoke|toggle|del):"))
    print("✅ Rebecca Telegram Bot Started Successfully!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
