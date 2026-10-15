import io, os, json, time, asyncio, logging
from datetime import datetime
import httpx, qrcode
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.WARNING)

(SET_PANEL_URL, SET_SUB_URL, SET_PANEL_USER, SET_PANEL_PASS, 
 ASK_BASE_NAME, ASK_START_NUM, ASK_COUNT,
 EDIT_GB, EDIT_DAYS, EDIT_GROUP, ASK_SEARCH_USER, EDIT_DATA, EDIT_DAYS_USER) = range(13)

def load_config() -> dict:
    default_cfg = {
        "bot_token": "", "admin_ids": [], "panel_url": "", "sub_url": "", "username": "", "password": "",
        "template_gb": 30, "template_days": 30, "template_mode": "on_hold", "template_count": 1
    }
    if not os.path.exists(CONFIG_FILE):
        return default_cfg
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        for k, v in default_cfg.items():
            if k not in data: data[k] = v
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
        return Markup([[Btn("🔌 اتصال به پنل ربکا (ورود یوزرنیم و پسورد)", callback_data="setup_panel")]])
    return Markup([
        [Btn("⚡ ساخت سریع با تمپلت پیش‌فرض", callback_data="quick_create")],
        [Btn("⚙️ تنظیمات تمپلت ساخت", callback_data="menu_template"), Btn("📋 لیست کاربران", callback_data="list_users:all:0")],
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
    text = (
        f"🚀 **ربات مدیریت پنل ربکا (نسخه Dev)**\nوضعیت: {st}\n\n"
        f"📌 **تمپلت فعلی ساخت:**\n"
        f"💾 حجم: `{cfg.get('template_gb')} GB` | 📅 اعتبار: `{cfg.get('template_days')} روز`\n"
        f"⏳ حالت: `{cfg.get('template_mode')}` | 📦 تعداد پیش‌فرض: `{cfg.get('template_count')}`\n\nیک گزینه را انتخاب کنید:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

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
        await wait_msg.edit_text(f"✅ **اتصال برقرار شد!**\n👥 کل کاربران: `{st.get('total_user', 0)}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در اتصال:\n`{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def menu_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    txt = (
        f"⚙️ **تنظیمات تمپلت ساخت کاربر**\n\n"
        f"1️⃣ حجم فعلی: `{cfg.get('template_gb')} GB`\n"
        f"2️⃣ اعتبار فعلی: `{cfg.get('template_days')} روز`\n"
        f"3️⃣ حالت اکانت: `{cfg.get('template_mode')}`\n"
        f"4️⃣ تعداد پیش‌فرض: `{cfg.get('template_count')}`\n\n"
        f"کدام مورد را می‌خواهید تغییر دهید؟"
    )
    kb = Markup([
        [Btn("💾 تغییر حجم", callback_data="tmpl_edit:gb"), Btn("📅 تغییر زمان", callback_data="tmpl_edit:days")],
        [Btn("⚡ تغییر حالت", callback_data="tmpl_edit:mode"), Btn("📦 تغییر تعداد", callback_data="tmpl_edit:count")],
        [Btn("🔙 بازگشت", callback_data="back_main")]
    ])
    await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def tmpl_edit_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
    context.user_data["tmpl_action"] = action
    
    if action == "gb":
        await q.edit_message_text("💾 حجم جدید را به گیگابایت بفرستید (`0` نامحدود):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        return EDIT_GB
    elif action == "days":
        await q.edit_message_text("📅 تعداد روز اعتبار جدید را بفرستید (`0` نامحدود):", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        return EDIT_DAYS
    elif action == "count":
        await q.edit_message_text("📦 تعداد پیش‌فرض ساخت در هر بار را بفرستید:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
        return EDIT_GB
    elif action == "mode":
        cfg = load_config()
        cfg["template_mode"] = "active" if cfg.get("template_mode") == "on_hold" else "on_hold"
        save_config(cfg)
        await menu_template(update, context)
        return ConversationHandler.END

async def tmpl_save_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    action = context.user_data.get("tmpl_action")
    cfg = load_config()
    try:
        if action == "gb": cfg["template_gb"] = float(val)
        elif action == "days": cfg["template_days"] = int(val)
        elif action == "count": cfg["template_count"] = int(val)
        save_config(cfg)
        await update.message.reply_text("✅ تمپلت به‌روزرسانی شد!", reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text("⚠️ لطفاً یک عدد معتبر وارد کنید:")
        return EDIT_GB
    return ConversationHandler.END

async def quick_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    await q.edit_message_text(
        f"⚡ **ساخت سریع با تمپلت**\nحجم: `{cfg.get('template_gb')} GB` | اعتبار: `{cfg.get('template_days')} روز`\n\n"
        f"👤 لطفاً **اسم پایه** کاربران را بفرستید (مثلاً `ali`):",
        reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN
    )
    return ASK_BASE_NAME

async def quick_get_base_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["base_name"] = update.message.text.strip()
    kb = Markup([[Btn("بدون عدد (تک کاربر)", callback_data="startnum_none")], [Btn("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text(f"🔢 عدد شروع شماره‌گذاری بعد از `{context.user_data['base_name']}` (مثلاً `1050`):", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return ASK_START_NUM

async def quick_get_start_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data.update({"start_num": None, "count": 1})
        return await execute_quick_creation(update, context)
    try:
        context.user_data["start_num"] = int(update.message.text.strip())
        context.user_data["count"] = cfg.get("template_count", 1)
        return await execute_quick_creation(update, context)
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح بفرستید:")
        return ASK_START_NUM

async def execute_quick_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query or update.message
    if update.callback_query:
        await update.callback_query.edit_text("⚡ در حال ساخت اکانت‌ها...")
    else:
        query = await update.message.reply_text("⚡ در حال ساخت اکانت‌ها...")

    cfg = load_config()
    base = context.user_data["base_name"]
    snum = context.user_data.get("start_num")
    count = context.user_data.get("count", 1)
    
    limit = int(cfg.get("template_gb", 30) * (1024 ** 3))
    days = cfg.get("template_days", 30)
    mode = cfg.get("template_mode", "on_hold")

    usernames = [base] if snum is None else [f"{base}{snum + i}" for i in range(count)]

    sem = asyncio.Semaphore(10)
    async def create_one(u):
        # ساختار فیلدهای کاملاً استاندارد برای نسخه Dev پنل ربکا
        p = {
            "username": u,
            "data_limit": limit,
            "data_limit_reset_strategy": "no_reset"
        }
        if mode == "on_hold" and days > 0:
            p.update({"status": "on_hold", "on_hold_expire_duration": days * 86400, "on_hold_timeout": int(time.time()) + 31536000})
        else:
            p.update({"status": "active", "expire": int(time.time()) + (days * 86400) if days > 0 else 0})

        async with sem:
            try:
                r = await api.req("POST", "/api/user", json=p)
                return u, full_sub(r.get("subscription_url", "")), None
            except httpx.HTTPStatusError as err:
                return u, None, err.response.text if err.response else str(err)
            except Exception as e:
                return u, None, str(e)

    res = await asyncio.gather(*(create_one(u) for u in usernames))
    ok = [(u, s) for u, s, _ in res if s]
    
    if not ok:
        err_msg = res[0][2] if res else "Unknown"
        await query.edit_text(f"❌ خطا در ساخت کاربر:\n`{err_msg[:400]}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    for u, s in ok[:20]:
        cap = f"✅ **کاربر:** `{u}`\n💾 حجم: `{cfg.get('template_gb')} GB`\n⏳ اعتبار: `{days} روز` ({mode})\n\n🔗 **لینک ساب:**\n`{s}`"
        await context.bot.send_photo(chat_id=chat_id, photo=make_qr(s), caption=cap, parse_mode=ParseMode.MARKDOWN)

    if len(ok) > 1:
        txt = "=== با نام کاربری ===\n" + "\n".join(f"{u} | {s}" for u, s in ok) + "\n\n=== فقط لینک ساب ===\n" + "\n".join(s for _, s in ok)
        bio = io.BytesIO(txt.encode("utf-8"))
        bio.name = f"{usernames[0]}_to_{usernames[-1]}.txt"
        await context.bot.send_document(chat_id=chat_id, document=bio, caption=f"📂 لیست کامل (`{len(ok)}` کاربر)")

    await context.bot.send_message(chat_id=chat_id, text=f"🎉 تعداد `{len(ok)}` کاربر ساخته شد.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🔍 **نام کاربری** را جهت جستجو بفرستید:", reply_markup=cancel_kb(), parse_mode=ParseMode.MARKDOWN)
    return ASK_SEARCH_USER

def render_user(u: dict):
    uname, st = u["username"], u.get("status", "unknown")
    sub = full_sub(u.get("subscription_url", ""))
    txt = (f"👤 **کاربر:** `{uname}`\n📡 وضعیت: `{st}`\n📊 مصرف: `{fmt_gb(u.get('used_traffic', 0))}` / `{fmt_gb(u.get('data_limit', 0))}`\n"
           f"⏳ اعتبار: `{fmt_expire(u.get('expire', 0), st, u.get('on_hold_expire_duration', 0))}`\n\n🔗 **لینک ساب:**\n`{sub}`")
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
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=make_qr(s), caption=f"👤 `{uname}`\n🔗 `{s}`", parse_mode=ParseMode.MARKDOWN)
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
            await q.edit_message_text(f"✅ کاربر `{uname}` حذف شد.", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
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
    await q.edit_message_text(f"📋 **کاربران ({st_f})** — کل: `{total}`", reply_markup=Markup(rows), parse_mode=ParseMode.MARKDOWN)

async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = await api.req("GET", "/api/system")
    txt = (f"📊 **وضعیت سرور و پنل**\n👥 کل: `{st.get('total_user', 0)}` | 🟢 فعال: `{st.get('users_active', 0)}` | ⏳ On-Hold: `{st.get('users_on_hold', 0)}`\n"
           f"🖥 CPU: `{st.get('cpu_usage', 0)}%` | 🧠 RAM: `{fmt_gb(st.get('mem_used', 0))}/{fmt_gb(st.get('mem_total', 0))}`\n"
           f"📥 دانلود: `{fmt_gb(st.get('incoming_bandwidth', 0))}` | 📤 آپلود: `{fmt_gb(st.get('outgoing_bandwidth', 0))}`")
    kb = Markup([[Btn("🔄 ریستارت هسته Xray", callback_data="restart_core")], [Btn("🔙 منوی اصلی", callback_data="back_main")]])
    await q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

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
    await q.edit_message_text(f"🧹 تعداد `{len(targets)}` کاربر منقضی حذف شدند.", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)

async def export_all_subs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("آماده‌سازی فایل...")
    res = await api.req("GET", "/api/users")
    users = res.get("users", [])
    bio = io.BytesIO("\n".join(f"{u['username']} | {u['status']} | {full_sub(u.get('subscription_url', ''))}" for u in users).encode("utf-8"))
    bio.name = f"all_subs_{datetime.now().strftime('%Y%m%d')}.txt"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=bio, caption=f"📥 کل کاربران (`{len(users)}`)")

def main():
    cfg = load_config()
    app = Application.builder().token(cfg["bot_token"]).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd), 
            CallbackQueryHandler(start_setup_panel, pattern="^setup_panel$"),
            CallbackQueryHandler(quick_create_start, pattern="^quick_create$"),
            CallbackQueryHandler(menu_template, pattern="^menu_template$"),
            CallbackQueryHandler(start_search, pattern="^menu_search$"),
            CallbackQueryHandler(start_edit_user, pattern="^uact:edit:")
        ],
        states={
            SET_PANEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_url)],
            SET_SUB_URL: [CallbackQueryHandler(get_sub_url, pattern="^skip_sub_url$"), MessageHandler(filters.TEXT & ~filters.COMMAND, get_sub_url)],
            SET_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_user)],
            SET_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_pass)],
            ASK_BASE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_get_base_name)],
            ASK_START_NUM: [CallbackQueryHandler(quick_get_start_num, pattern="^startnum_none$"), MessageHandler(filters.TEXT & ~filters.COMMAND, quick_get_start_num)],
            
            EDIT_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_save_value)],
            EDIT_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_save_value)],

            ASK_SEARCH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_search)],
            EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_data)],
            EDIT_DAYS_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_days)],
        },
        fallbacks=[CommandHandler("start", start_cmd), CallbackQueryHandler(start_cmd, pattern="^back_main$")],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(start_cmd, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(tmpl_edit_router, pattern="^tmpl_edit:"))
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
