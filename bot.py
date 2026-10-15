import io
import os
import json
import time
import asyncio
import logging
from datetime import datetime
import httpx
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.WARNING
)
logger = logging.getLogger(__name__)

# مراحل مکالمه (States)
(
    SET_PANEL_URL,
    SET_SUB_URL,
    SET_PANEL_USER,
    SET_PANEL_PASS,
    ASK_BASE_NAME,
    ASK_START_NUM,
    ASK_COUNT,
    ASK_DATA_LIMIT,
    ASK_EXPIRE_DAYS,
    ASK_STATUS_MODE,
    ASK_INBOUNDS,
    ASK_SEARCH_USER,
    ASK_EDIT_DATA,
    ASK_EDIT_DAYS,
) = range(14)


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"bot_token": "", "admin_ids": [], "panel_url": "", "sub_url": "", "username": "", "password": ""}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


class RebeccaClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            verify=False,
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
        self.token = None
        self.inbounds_cache = {}
        self.reload_credentials()

    def reload_credentials(self):
        cfg = load_config()
        self.base_url = cfg.get("panel_url", "").rstrip("/")
        self.sub_url = cfg.get("sub_url", "").rstrip("/") or self.base_url
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.token = None

    def is_configured(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    async def login(self) -> str:
        resp = await self.client.post(
            f"{self.base_url}/api/admin/token",
            data={"username": self.username, "password": self.password},
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

    async def get_inbounds(self) -> dict:
        data = await self.req("GET", "/api/inbounds")
        self.inbounds_cache = data
        return data


api = RebeccaClient()


# ================= توابع کمکی =================
def is_admin(user_id: int) -> bool:
    cfg = load_config()
    return user_id in cfg.get("admin_ids", [])


def fmt_gb(size: int) -> str:
    if not size or size <= 0:
        return "نامحدود"
    return f"{size / (1024 ** 3):.2f} GB"


def fmt_expire(expire_ts: int, status: str, on_hold_dur: int = None) -> str:
    if status == "on_hold":
        d = int(on_hold_dur // 86400) if on_hold_dur else 0
        return f"{d} روز (شروع از اولین اتصال)"
    if not expire_ts:
        return "نامحدود"
    rem = expire_ts - int(time.time())
    if rem <= 0:
        return "منقضی شده"
    return f"{rem // 86400} روز و {(rem % 86400) // 3600} ساعت"


def full_sub(sub_path: str) -> str:
    if not sub_path:
        return "نامشخص"
    if sub_path.startswith("http"):
        return sub_path
    return f"{api.sub_url}/{sub_path.lstrip('/')}"


def make_qr(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(version=1, box_size=9, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    bio.name = "qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


def main_keyboard() -> InlineKeyboardMarkup:
    if not api.is_configured():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔌 اتصال به پنل ربکا (ورود یوزرنیم و پسورد)", callback_data="setup_panel")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت کاربر (تکی / گروهی ترتیبی)", callback_data="menu_create")],
        [
            InlineKeyboardButton("🔍 جستجو و مدیریت کاربر", callback_data="menu_search"),
            InlineKeyboardButton("📋 لیست کاربران", callback_data="list_users:all:0"),
        ],
        [
            InlineKeyboardButton("📊 وضعیت سرور و هسته", callback_data="menu_stats"),
            InlineKeyboardButton("🧹 پاکسازی منقضی‌ها", callback_data="menu_cleanup"),
        ],
        [
            InlineKeyboardButton("📥 خروجی همه لینک‌های ساب", callback_data="export_all_subs"),
            InlineKeyboardButton("⚙️ تنظیمات اتصال پنل", callback_data="setup_panel"),
        ],
    ])


# ================= منوی اصلی =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        if update.message:
            await update.message.reply_text("⛔ شما به این ربات دسترسی ندارید.")
        return ConversationHandler.END

    context.user_data.clear()
    status_txt = "🟢 متصل به پنل" if api.is_configured() else "🔴 پنل تنظیم نشده است (ابتدا متصل شوید)"
    text = (
        f"🚀 **ربات مدیریت سریع پنل ربکا (Rebecca)**\n"
        f"وضعیت: {status_txt}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ================= تنظیمات اتصال به پنل از داخل ربات =================
async def start_setup_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await query.edit_message_text(
        "🌐 **مرحله ۱ از ۴: آدرس پنل**\n\n"
        "آدرس کامل پنل ربکا را همراه با پورت ارسال کنید:\n"
        "مثال: `https://panel.domain.com:8000`",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SET_PANEL_URL


async def get_panel_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip().rstrip("/")
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ آدرس باید با `http://` یا `https://` شروع شود. مجدداً ارسال کنید:")
        return SET_PANEL_URL
    context.user_data["setup_panel_url"] = url
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ مشابه آدرس پنل است (رد کردن)", callback_data="skip_sub_url")],
        [InlineKeyboardButton("❌ انصراف", callback_data="back_main")],
    ])
    await update.message.reply_text(
        "🔗 **مرحله ۲ از ۴: آدرس دامنه سابسکریپشن**\n\n"
        "اگر دامنه ساب شما متفاوت است آن را بفرستید (مثلاً `https://sub.domain.com`)، در غیر این صورت دکمه رد کردن را بزنید:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SET_SUB_URL


async def get_sub_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data["setup_sub_url"] = context.user_data["setup_panel_url"]
        msg_func = update.callback_query.edit_message_text
    else:
        context.user_data["setup_sub_url"] = update.message.text.strip().rstrip("/")
        msg_func = update.message.reply_text

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await msg_func(
        "👤 **مرحله ۳ از ۴: نام کاربری ادمین**\n\n"
        "یوزرنیم ادمین پنل ربکا را وارد کنید:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SET_PANEL_USER


async def get_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["setup_user"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text(
        "🔑 **مرحله ۴ از ۴: رمز عبور ادمین**\n\n"
        "پسورد ادمین پنل ربکا را وارد کنید:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SET_PANEL_PASS


async def get_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    wait_msg = await update.message.reply_text("⏳ در حال تست اتصال و دریافت توکن از پنل ربکا...")

    cfg = load_config()
    cfg["panel_url"] = context.user_data["setup_panel_url"]
    cfg["sub_url"] = context.user_data["setup_sub_url"]
    cfg["username"] = context.user_data["setup_user"]
    cfg["password"] = password
    save_config(cfg)
    api.reload_credentials()

    try:
        await api.login()
        stats = await api.req("GET", "/api/system")
        await wait_msg.edit_text(
            f"✅ **اتصال به پنل ربکا با موفقیت برقرار شد!**\n\n"
            f"👥 کل کاربران پنل: `{stats.get('total_user', 0)}`\n"
            f"🟢 کاربران فعال: `{stats.get('users_active', 0)}`",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await wait_msg.edit_text(
            f"❌ **خطا در اتصال به پنل!**\nاطلاعات وارد شده یا پورت پنل را بررسی کنید.\n\nجزئیات خطا:\n`{e}`",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


# ================= ساخت کاربر (ترتیبی و تکی) =================
async def start_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await query.edit_message_text(
        "👤 **گام ۱ از ۶: نام پایه کاربر**\n\n"
        "اسم پایه کاربرانی که می‌خواهید بسازید را وارد کنید (مثلاً `ali`):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_BASE_NAME


async def create_get_base_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["base_name"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("بدون عدد (ساخت تک کاربر با همین نام)", callback_data="startnum_none")],
        [InlineKeyboardButton("❌ انصراف", callback_data="back_main")],
    ])
    await update.message.reply_text(
        f"🔢 **گام ۲ از ۶: عدد شروع شماره‌گذاری**\n\n"
        f"شروع اعداد بعد از نام `{context.user_data['base_name']}` از چه عددی باشد؟\n"
        f"(مثلاً عدد `1050` را بفرستید تا از `{context.user_data['base_name']}1050` به ترتیب بسازد):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_START_NUM


async def create_get_start_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data["start_num"] = None
        context.user_data["count"] = 1
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
        await update.callback_query.edit_message_text(
            "💾 **گام ۴ از ۶: حجم کاربر (گیگابایت)**\n\n"
            "حجم را به گیگابایت وارد کنید (برای نامحدود عدد `0` بفرستید):",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_DATA_LIMIT

    try:
        start_num = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ لطفاً یک عدد صحیح وارد کنید (مثلاً `1050`):")
        return ASK_START_NUM

    context.user_data["start_num"] = start_num
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    base = context.user_data["base_name"]
    await update.message.reply_text(
        f"📦 **گام ۳ از ۶: تعداد کاربران**\n\n"
        f"چه تعداد کاربر از `{base}{start_num}` به بعد ساخته شود؟ (عددی بین `1` تا `200` بفرستید):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_COUNT


async def create_get_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
        if count < 1 or count > 200:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ لطفاً عددی بین 1 تا 200 وارد کنید:")
        return ASK_COUNT

    context.user_data["count"] = count
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text(
        "💾 **گام ۴ از ۶: حجم هر کاربر (گیگابایت)**\n\n"
        "حجم هر اکانت را به گیگابایت وارد کنید (برای نامحدود `0` بفرستید):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_DATA_LIMIT


async def create_get_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gb = float(update.message.text.strip())
        if gb < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ عدد معتبر وارد کنید:")
        return ASK_DATA_LIMIT

    context.user_data["data_limit"] = int(gb * (1024 ** 3))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text(
        "📅 **گام ۵ از ۶: مدت اعتبار (روز)**\n\n"
        "تعداد روز اعتبار را وارد کنید (مثلاً `30` یا برای نامحدود `0`):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_EXPIRE_DAYS


async def create_get_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
        if days < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح معتبر وارد کنید:")
        return ASK_EXPIRE_DAYS

    context.user_data["days"] = days
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ شروع از اولین اتصال (On-Hold)", callback_data="cmode:on_hold")],
        [InlineKeyboardButton("🟢 فعال از همین لحظه (Active)", callback_data="cmode:active")],
        [InlineKeyboardButton("❌ انصراف", callback_data="back_main")],
    ])
    await update.message.reply_text(
        "⏳ **گام ۶ از ۶: نوع زمان‌بندی و اینباند**\n\n"
        "نحوه محاسبه تاریخ انقضا را انتخاب کنید:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_STATUS_MODE


def build_inbound_selector_kb(inbounds_dict: dict, selected_tags: set) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🚀 ساخت فوری با همه اینباندها", callback_data="ib_all_go")]
    ]
    for proto, ib_list in inbounds_dict.items():
        for ib in ib_list:
            tag = ib.get("tag")
            if not tag:
                continue
            mark = "✅" if tag in selected_tags else "⬜"
            rows.append([InlineKeyboardButton(f"{mark} [{proto.upper()}] {tag}", callback_data=f"ib_tog:{tag}")])
    rows.append([
        InlineKeyboardButton("✔️ تایید و شروع ساخت", callback_data="ib_confirm_go"),
        InlineKeyboardButton("❌ انصراف", callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(rows)


async def create_get_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mode"] = query.data.split(":")[1]

    try:
        ib_data = await api.get_inbounds()
    except Exception as e:
        await query.edit_message_text(f"❌ خطا در دریافت اینباندها:\n`{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    all_tags = {ib["tag"] for ib_list in ib_data.values() for ib in ib_list if "tag" in ib}
    context.user_data["selected_inbounds"] = all_tags

    await query.edit_message_text(
        "📡 **انتخاب اینباندها:**\n"
        "می‌توانید دکمه **«ساخت فوری با همه اینباندها»** را بزنید یا اینباندهای دلخواه را تیک بزنید:",
        reply_markup=build_inbound_selector_kb(ib_data, all_tags),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_INBOUNDS


async def handle_inbound_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    ib_data = api.inbounds_cache
    selected: set = context.user_data.get("selected_inbounds", set())

    if data.startswith("ib_tog:"):
        tag = data.split(":", 1)[1]
        if tag in selected:
            selected.remove(tag)
        else:
            selected.add(tag)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_inbound_selector_kb(ib_data, selected))
        return ASK_INBOUNDS

    if data == "ib_all_go":
        selected = {ib["tag"] for ib_list in ib_data.values() for ib in ib_list if "tag" in ib}

    if not selected:
        await query.answer("⚠️ حداقل یک اینباند باید انتخاب شود!", show_alert=True)
        return ASK_INBOUNDS

    await query.answer("شروع ساخت کاربران...")
    return await execute_batch_creation(update, context, selected)


async def execute_batch_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, selected_tags: set):
    query = update.callback_query
    base_name = context.user_data["base_name"]
    start_num = context.user_data["start_num"]
    count = context.user_data["count"]
    data_limit = context.user_data["data_limit"]
    days = context.user_data["days"]
    mode = context.user_data["mode"]

    ib_data = api.inbounds_cache
    proxies = {}
    inbounds = {}
    for proto, ib_list in ib_data.items():
        tags = [ib["tag"] for ib in ib_list if ib.get("tag") in selected_tags]
        if tags:
            proxies[proto] = {}
            inbounds[proto] = tags

    if start_num is None:
        usernames = [base_name]
    else:
        usernames = [f"{base_name}{start_num + i}" for i in range(count)]

    await query.edit_message_text(
        f"⚡ در حال ساخت سریع `{len(usernames)}` کاربر (`{usernames[0]}` تا `{usernames[-1]}`)...",
        parse_mode=ParseMode.MARKDOWN,
    )

    sem = asyncio.Semaphore(10)

    async def create_single(uname: str):
        payload = {
            "username": uname,
            "proxies": proxies,
            "inbounds": inbounds,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
        }
        if mode == "on_hold" and days > 0:
            payload["status"] = "on_hold"
            payload["on_hold_expire_duration"] = days * 86400
            payload["on_hold_timeout"] = int(time.time()) + (365 * 86400)
        else:
            payload["status"] = "active"
            payload["expire"] = int(time.time()) + (days * 86400) if days > 0 else 0

        async with sem:
            try:
                res = await api.req("POST", "/api/user", json=payload)
                return uname, full_sub(res.get("subscription_url", "")), None
            except Exception as e:
                return uname, None, str(e)

    results = await asyncio.gather(*(create_single(u) for u in usernames))

    success_list = [(u, sub) for u, sub, err in results if sub]
    failed_list = [(u, err) for u, sub, err in results if not sub]

    if not success_list:
        err_sample = failed_list[0][1] if failed_list else "Unknown"
        await query.edit_message_text(
            f"❌ هیچ کاربری ساخته نشد!\nنمونه خطا: `{err_sample}`",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # ارسال QR Code برای کاربران (تا سقف 20 عدد مستقیم در چت)
    for uname, sub_link in success_list[:20]:
        qr_bio = make_qr(sub_link)
        caption = (
            f"✅ **کاربر:** `{uname}`\n"
            f"💾 **حجم:** `{fmt_gb(data_limit)}`\n"
            f"⏳ **اعتبار:** `{days if days > 0 else 'نامحدود'} روز` "
            f"({'شروع از اولین اتصال' if mode == 'on_hold' else 'فعال'})\n\n"
            f"🔗 **لینک ساب:**\n`{sub_link}`"
        )
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_bio,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )

    # ارسال فایل متنی مرتب‌شده برای ساخت‌های گروهی
    if len(success_list) > 1:
        lines = [f"{u} | {sub}" for u, sub in success_list]
        only_links = [sub for _, sub in success_list]
        content = "=== لیست با نام کاربری ===\n" + "\n".join(lines) + "\n\n=== فقط لینک‌های ساب ===\n" + "\n".join(only_links)
        bio = io.BytesIO(content.encode("utf-8"))
        bio.name = f"{base_name}_{usernames[0]}_to_{usernames[-1]}.txt"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            caption=(
                f"📂 **فایل لینک‌های سابسکریپشن**\n"
                f"✅ ساخته شده: `{len(success_list)}` از `{len(usernames)}` کاربر\n"
                f"🔢 ترتیب: `{usernames[0]}` تا `{usernames[-1]}`"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🎉 عملیات با موفقیت انجام شد (`{len(success_list)}` کاربر ساخته شد).",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ================= جستجو و مدیریت کاربر =================
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await query.edit_message_text(
        "🔍 **نام کاربری (Username)** را جهت جستجو ارسال کنید:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SEARCH_USER


def render_user_panel(u: dict):
    uname = u["username"]
    status = u.get("status", "unknown")
    used = u.get("used_traffic", 0)
    limit = u.get("data_limit", 0)
    expire = u.get("expire", 0)
    on_hold_dur = u.get("on_hold_expire_duration", 0)
    sub_url = full_sub(u.get("subscription_url", ""))
    online_at = u.get("online_at") or "تاکنون متصل نشده"

    st_icons = {
        "active": "🟢 فعال",
        "on_hold": "⏳ On-Hold (اولین اتصال)",
        "disabled": "🔴 غیرفعال",
        "expired": "⚫ منقضی شده",
        "limited": "🟠 اتمام حجم",
    }

    text = (
        f"👤 **کاربر:** `{uname}`\n"
        f"📡 **وضعیت:** {st_icons.get(status, status)}\n"
        f"📊 **مصرف:** `{fmt_gb(used)}` از `{fmt_gb(limit)}`\n"
        f"⏳ **اعتبار:** `{fmt_expire(expire, status, on_hold_dur)}`\n"
        f"🕒 **آخرین اتصال:** `{online_at}`\n\n"
        f"🔗 **لینک ساب:**\n`{sub_url}`"
    )

    toggle_lbl = "🔴 غیرفعال کردن" if status != "disabled" else "🟢 فعال کردن"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 دریافت QR Code", callback_data=f"uact:qr:{uname}")],
        [
            InlineKeyboardButton("🔄 صفر کردن مصرف", callback_data=f"uact:reset:{uname}"),
            InlineKeyboardButton("✏️ تغییر حجم / زمان", callback_data=f"uact:edit:{uname}"),
        ],
        [
            InlineKeyboardButton("🔗 تغییر لینک ساب (Revoke)", callback_data=f"uact:revoke:{uname}"),
            InlineKeyboardButton(toggle_lbl, callback_data=f"uact:toggle:{uname}:{status}"),
        ],
        [InlineKeyboardButton("🗑 حذف کاربر", callback_data=f"uact:del:{uname}")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="back_main")],
    ])
    return text, kb


async def process_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uname = update.message.text.strip()
    try:
        u = await api.req("GET", f"/api/user/{uname}")
        text, kb = render_user_panel(u)
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            # جستجو در لیست کاربران مشابه
            res = await api.req("GET", "/api/users", params={"search": uname, "limit": 10})
            users = res.get("users", [])
            if not users:
                await update.message.reply_text("❌ کاربری یافت نشد. مجدداً تلاش کنید:")
                return ASK_SEARCH_USER
            rows = [[InlineKeyboardButton(f"👤 {item['username']} ({item['status']})", callback_data=f"uact:view:{item['username']}")] for item in users]
            rows.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="back_main")])
            await update.message.reply_text("🔍 نتایج مشابه یافت شد:", reply_markup=InlineKeyboardMarkup(rows))
            return ConversationHandler.END
        await update.message.reply_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END


async def handle_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    uname = parts[2]

    try:
        if action == "view":
            await query.answer()
            u = await api.req("GET", f"/api/user/{uname}")
            text, kb = render_user_panel(u)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

        elif action == "qr":
            await query.answer("در حال تولید QR Code...")
            u = await api.req("GET", f"/api/user/{uname}")
            sub_url = full_sub(u.get("subscription_url", ""))
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=make_qr(sub_url),
                caption=f"👤 کاربر: `{uname}`\n🔗 لینک ساب:\n`{sub_url}`",
                parse_mode=ParseMode.MARKDOWN,
            )

        elif action == "reset":
            await api.req("POST", f"/api/user/{uname}/reset")
            await query.answer("✅ ترافیک مصرفی صفر شد.", show_alert=True)
            u = await api.req("GET", f"/api/user/{uname}")
            text, kb = render_user_panel(u)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

        elif action == "revoke":
            u = await api.req("POST", f"/api/user/{uname}/revoke_sub")
            await query.answer("✅ لینک ساب قبلی باطل و لینک جدید ساخته شد!", show_alert=True)
            text, kb = render_user_panel(u)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

        elif action == "toggle":
            cur = parts[3]
            new_st = "disabled" if cur != "disabled" else "active"
            u = await api.req("PUT", f"/api/user/{uname}", json={"status": new_st})
            await query.answer(f"وضعیت به {new_st} تغییر کرد.")
            text, kb = render_user_panel(u)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

        elif action == "del":
            await api.req("DELETE", f"/api/user/{uname}")
            await query.answer("🗑 کاربر حذف شد.", show_alert=True)
            await query.edit_message_text(f"✅ کاربر `{uname}` حذف شد.", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await query.answer(f"❌ خطا: {e}", show_alert=True)


# ================= ویرایش و تمدید کاربر =================
async def start_edit_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uname = query.data.split(":")[2]
    context.user_data["edit_uname"] = uname
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await query.edit_message_text(
        f"✏️ **ویرایش و تمدید `{uname}`**\n\nحجم کل جدید را به **گیگابایت** وارد کنید (`0` برای نامحدود):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_EDIT_DATA


async def edit_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gb = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ عدد معتبر وارد کنید:")
        return ASK_EDIT_DATA

    context.user_data["edit_limit"] = int(gb * (1024 ** 3))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="back_main")]])
    await update.message.reply_text("📅 **تعداد روز اعتبار جدید** را وارد کنید (`0` برای نامحدود):")
    return ASK_EDIT_DAYS


async def edit_get_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ عدد صحیح وارد کنید:")
        return ASK_EDIT_DAYS

    uname = context.user_data["edit_uname"]
    limit = context.user_data["edit_limit"]
    exp = int(time.time()) + (days * 86400) if days > 0 else 0

    try:
        await api.req("POST", f"/api/user/{uname}/reset")
        u = await api.req("PUT", f"/api/user/{uname}", json={"data_limit": limit, "expire": exp, "status": "active"})
        text, kb = render_user_panel(u)
        await update.message.reply_text(f"✅ کاربر تمدید شد!\n\n{text}", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ================= لیست کاربران، وضعیت، پاکسازی و خروجی =================
async def list_users_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, status_filter, offset_str = query.data.split(":")
    offset = int(offset_str)
    limit = 10

    params = {"offset": offset, "limit": limit}
    if status_filter != "all":
        params["status"] = status_filter

    try:
        res = await api.req("GET", "/api/users", params=params)
        users = res.get("users", [])
        total = res.get("total", 0)
    except Exception as e:
        await query.edit_message_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return

    filter_btns = [
        InlineKeyboardButton("همه", callback_data="list_users:all:0"),
        InlineKeyboardButton("🟢 فعال", callback_data="list_users:active:0"),
        InlineKeyboardButton("⏳ On-Hold", callback_data="list_users:on_hold:0"),
        InlineKeyboardButton("⚫ منقضی", callback_data="list_users:expired:0"),
    ]
    rows = [filter_btns]

    for u in users:
        rows.append([
            InlineKeyboardButton(
                f"👤 {u['username']} | {fmt_gb(u.get('used_traffic', 0))}/{fmt_gb(u.get('data_limit', 0))}",
                callback_data=f"uact:view:{u['username']}",
            )
        ])

    nav = []
    if offset >= limit:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"list_users:{status_filter}:{offset - limit}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"list_users:{status_filter}:{offset + limit}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="back_main")])
    await query.edit_message_text(
        f"📋 **لیست کاربران ({status_filter})** — کل: `{total}`",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )


async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("دریافت وضعیت...")
    try:
        st = await api.req("GET", "/api/system")
        text = (
            f"📊 **وضعیت سرور و پنل ربکا**\n\n"
            f"⚙️ نسخه هسته: `{st.get('version', 'نامشخص')}`\n"
            f"👥 کل کاربران: `{st.get('total_user', 0)}`\n"
            f"🟢 فعال: `{st.get('users_active', 0)}` | ⏳ On-Hold: `{st.get('users_on_hold', 0)}`\n"
            f"⚫ منقضی: `{st.get('users_expired', 0)}` | 🟠 اتمام حجم: `{st.get('users_limited', 0)}`\n\n"
            f"🖥 مصرف CPU: `{st.get('cpu_usage', 0)}%`\n"
            f"🧠 مصرف RAM: `{fmt_gb(st.get('mem_used', 0))} / {fmt_gb(st.get('mem_total', 0))}`\n"
            f"📥 کل دانلود: `{fmt_gb(st.get('incoming_bandwidth', 0))}`\n"
            f"📤 کل آپلود: `{fmt_gb(st.get('outgoing_bandwidth', 0))}`"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 ریستارت هسته Xray", callback_data="restart_core")],
            [InlineKeyboardButton("🔙 منوی اصلی", callback_data="back_main")],
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await query.edit_message_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)


async def restart_core_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await api.req("POST", "/api/core/restart")
        await query.answer("✅ هسته Xray با موفقیت ریستارت شد!", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ خطا: {e}", show_alert=True)


async def cleanup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("در حال بررسی کاربران منقضی...")
    try:
        exp_res = await api.req("GET", "/api/users", params={"status": "expired"})
        lim_res = await api.req("GET", "/api/users", params={"status": "limited"})
        targets = [u["username"] for u in exp_res.get("users", []) + lim_res.get("users", [])]

        if not targets:
            await query.edit_message_text("✅ هیچ کاربر منقضی یا اتمام حجمی برای حذف وجود ندارد.", reply_markup=main_keyboard())
            return

        sem = asyncio.Semaphore(10)

        async def del_u(uname):
            async with sem:
                await api.req("DELETE", f"/api/user/{uname}")

        await asyncio.gather(*(del_u(u) for u in targets), return_exceptions=True)
        await query.edit_message_text(
            f"🧹 پاکسازی انجام شد! تعداد `{len(targets)}` کاربر منقضی/اتمام حجم حذف شدند.",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await query.edit_message_text(f"❌ خطا: `{e}`", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)


async def export_all_subs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("در حال آماده‌سازی فایل خروجی...")
    try:
        res = await api.req("GET", "/api/users")
        users = res.get("users", [])
        lines = [f"{u['username']} | {u['status']} | {full_sub(u.get('subscription_url', ''))}" for u in users]
        bio = io.BytesIO("\n".join(lines).encode("utf-8"))
        bio.name = f"rebecca_all_users_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            caption=f"📥 خروجی کامل لینک‌های سابسکریپشن (`{len(users)}` کاربر)",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await query.answer(f"❌ خطا: {e}", show_alert=True)


def main():
    cfg = load_config()
    if not cfg.get("bot_token"):
        print("Error: bot_token is missing in config.json")
        return

    app = Application.builder().token(cfg["bot_token"]).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(start_setup_panel, pattern="^setup_panel$"),
            CallbackQueryHandler(start_create, pattern="^menu_create$"),
            CallbackQueryHandler(start_search, pattern="^menu_search$"),
            CallbackQueryHandler(start_edit_user, pattern="^uact:edit:"),
        ],
        states={
            SET_PANEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_url)],
            SET_SUB_URL: [
                CallbackQueryHandler(get_sub_url, pattern="^skip_sub_url$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_sub_url),
            ],
            SET_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_user)],
            SET_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_panel_pass)],
            ASK_BASE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_base_name)],
            ASK_START_NUM: [
                CallbackQueryHandler(create_get_start_num, pattern="^startnum_none$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_start_num),
            ],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_count)],
            ASK_DATA_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_limit)],
            ASK_EXPIRE_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_days)],
            ASK_STATUS_MODE: [CallbackQueryHandler(create_get_mode, pattern="^cmode:")],
            ASK_INBOUNDS: [CallbackQueryHandler(handle_inbound_selection, pattern="^ib_")],
            ASK_SEARCH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_search)],
            ASK_EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_data)],
            ASK_EDIT_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_days)],
        },
        fallbacks=[
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(start_cmd, pattern="^back_main$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(start_cmd, pattern="^back_main$"))
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
