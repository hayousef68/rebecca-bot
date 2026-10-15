import os, json, time, asyncio, io
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import httpx, qrcode
from telegram import Bot

app = FastAPI(title="Rebecca Ultimate WebApp Secure")
security = HTTPBasic()

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
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

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = "admin"
    correct_pass = "You$ef@12156645"
    if credentials.username != correct_user or credentials.password != correct_pass:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

class WebAppRebeccaAPI:
    def __init__(self):
        self.client = httpx.AsyncClient(verify=False, timeout=15.0)
        self.token = None

    async def get_token(self):
        cfg = load_config()
        base_url = cfg.get("panel_url", "").rstrip("/")
        if base_url.endswith("/dashboard"):
            base_url = base_url[:-10]
        try:
            resp = await self.client.post(
                f"{base_url}/api/admin/token",
                data={"username": cfg.get("username"), "password": cfg.get("password")},
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            if resp.status_code == 200:
                self.token = resp.json().get("access_token")
                return self.token
        except Exception:
            pass
        return None

    async def req(self, method: str, endpoint: str, **kwargs):
        if not self.token:
            await self.get_token()
        cfg = load_config()
        base_url = cfg.get("panel_url", "").rstrip("/")
        if base_url.endswith("/dashboard"):
            base_url = base_url[:-10]
        
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        resp = await self.client.request(method, f"{base_url}{endpoint}", headers=headers, **kwargs)
        if resp.status_code == 401:
            await self.get_token()
            headers["Authorization"] = f"Bearer {self.token}"
            resp = await self.client.request(method, f"{base_url}{endpoint}", headers=headers, **kwargs)
        return resp.json() if resp.content else {}

api_client = WebAppRebeccaAPI()

def full_sub(sub_path: str) -> str:
    cfg = load_config()
    raw_url = cfg.get("panel_url", "").rstrip("/")
    if raw_url.endswith("/dashboard"):
        raw_url = raw_url[:-10]
    sub_base = cfg.get("sub_url", "").rstrip("/") or raw_url
    if not sub_path: return "نامشخص"
    if sub_path.startswith("http"):
        parts = sub_path.split("://")
        if len(parts) > 1:
            path_part = "/" + "/".join(parts[1].split("/")[1:])
            return f"{sub_base}{path_part}"
        return sub_path
    return f"{sub_base}/{sub_path.lstrip('/')}"

def make_qr(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(version=1, box_size=9, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    bio = io.BytesIO()
    bio.name = "qr.png"
    qr.make_image(fill_color="black", back_color="white").save(bio, "PNG")
    bio.seek(0)
    return bio

@app.get("/", response_class=HTMLResponse)
async def webapp_home(request: Request, username: str = Depends(verify_credentials)):
    return """
    <!DOCTYPE html>
    <html lang="fa" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>پنل مدیریت جامع ربکا</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <style>
            body { font-family: Tahoma, sans-serif; background: #0b0f19; color: #f3f4f6; }
            input, select, button { touch-action: manipulation; }
        </style>
    </head>
    <body class="p-3 max-w-lg mx-auto pb-16">
        <header class="flex justify-between items-center mb-4 bg-slate-900 p-3.5 rounded-xl border border-slate-800">
            <div>
                <h1 class="text-sm font-bold text-sky-400">⚡ پنل مدیریت کامل ربکا</h1>
                <span class="text-[11px] text-slate-400">سریع، امن و کاملاً روان</span>
            </div>
            <span class="text-[11px] bg-emerald-500/20 text-emerald-400 px-2.5 py-1 rounded-full font-medium">🟢 متصل</span>
        </header>

        <!-- تب‌های مدیریت روان و بزرگ -->
        <div class="grid grid-cols-5 gap-1.5 mb-5 bg-slate-950 p-1.5 rounded-xl border border-slate-800/80">
            <button onclick="switchTab('dashboard')" id="btn-dashboard" class="py-2.5 text-[10px] font-bold rounded-lg bg-sky-600 text-white transition shadow">📊 داشبورد</button>
            <button onclick="switchTab('create')" id="btn-create" class="py-2.5 text-[10px] font-bold rounded-lg text-slate-400 transition">➕ ساخت</button>
            <button onclick="switchTab('templates')" id="btn-templates" class="py-2.5 text-[10px] font-bold rounded-lg text-slate-400 transition">⚙️ تمپلت</button>
            <button onclick="switchTab('settings')" id="btn-settings" class="py-2.5 text-[10px] font-bold rounded-lg text-slate-400 transition">🔧 تنظیمات</button>
            <button onclick="switchTab('users')" id="btn-users" class="py-2.5 text-[10px] font-bold rounded-lg text-slate-400 transition">👥 کاربران</button>
        </div>

        <!-- تب ۱: داشبورد -->
        <div id="tab-dashboard" class="space-y-3">
            <div class="grid grid-cols-2 gap-2.5">
                <div class="bg-slate-900 p-4 rounded-xl border border-slate-800">
                    <span class="text-xs text-slate-400">کل کاربران</span>
                    <p id="stat-total" class="text-2xl font-black text-sky-300 mt-1">...</p>
                </div>
                <div class="bg-slate-900 p-4 rounded-xl border border-slate-800">
                    <span class="text-xs text-slate-400">کاربران فعال</span>
                    <p id="stat-active" class="text-2xl font-black text-emerald-400 mt-1">...</p>
                </div>
            </div>

            <div class="bg-slate-900 p-4 rounded-xl border border-slate-800">
                <h2 class="text-xs font-semibold mb-3 text-slate-300">🖥 وضعیت منابع سرور</h2>
                <div class="space-y-2 text-xs text-slate-300">
                    <div class="flex justify-between bg-slate-950 p-3 rounded-lg border border-slate-800/60">
                        <span>مصرف پردازنده (CPU):</span>
                        <span id="cpu-usage" class="font-bold text-sky-400">...</span>
                    </div>
                    <div class="flex justify-between bg-slate-950 p-3 rounded-lg border border-slate-800/60">
                        <span>حافظه رم (RAM):</span>
                        <span id="ram-usage" class="font-bold text-sky-400">...</span>
                    </div>
                </div>
                <button onclick="restartCore()" class="w-full mt-4 bg-amber-600/20 text-amber-400 text-xs py-3 rounded-xl font-bold hover:bg-amber-600/30 transition">🔄 ریستارت هسته Xray</button>
            </div>
        </div>

        <!-- تب ۲: ساخت کاربر -->
        <div id="tab-create" class="space-y-3 hidden">
            <div class="bg-slate-900 p-4 rounded-xl border border-slate-800 space-y-3.5">
                <h2 class="text-xs font-semibold text-slate-300">➕ ایجاد اکانت و ارسال به تلگرام</h2>
                
                <div>
                    <label class="block text-xs text-slate-400 mb-1.5">نام کاربری پایه:</label>
                    <input type="text" id="new-base" placeholder="مثال: ali" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:outline-none focus:border-sky-500">
                </div>

                <div class="grid grid-cols-2 gap-2.5">
                    <div>
                        <label class="block text-xs text-slate-400 mb-1.5">عدد شروع (اختیاری):</label>
                        <input type="number" id="new-start" placeholder="تک کاربر = خالی" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:outline-none focus:border-sky-500">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1.5">تعداد ساخت:</label>
                        <input type="number" id="new-count" value="1" min="1" max="50" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:outline-none focus:border-sky-500">
                    </div>
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1.5">انتخاب سرویس:</label>
                    <select id="new-service" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:outline-none focus:border-sky-500">
                        <option value="6">Hamkari (ID: 6)</option>
                        <option value="1">ms (ID: 1)</option>
                        <option value="5">Tes (ID: 5)</option>
                    </select>
                </div>

                <button onclick="createUser()" class="w-full bg-sky-600 hover:bg-sky-500 text-white text-sm py-3.5 rounded-xl font-bold transition shadow-lg">⚡ ساخت و ارسال فوری به تلگرام</button>
                <div id="create-result" class="text-xs mt-2"></div>
            </div>
        </div>

        <!-- تب ۳: مدیریت تمپلت‌ها -->
        <div id="tab-templates" class="space-y-3 hidden">
            <div class="bg-slate-900 p-4 rounded-xl border border-slate-800 space-y-3.5">
                <h2 class="text-xs font-semibold text-slate-300">⚙️ مدیریت و انتخاب تمپلت‌ها</h2>
                <div id="templates-list" class="space-y-2.5 text-xs">
                    <p class="text-slate-500 text-center py-2">در حال بارگذاری...</p>
                </div>

                <hr class="border-slate-800 my-4">

                <h3 class="text-xs font-semibold text-sky-400">➕ ساخت تمپلت جدید</h3>
                <div>
                    <label class="block text-xs text-slate-400 mb-1.5">نام تمپلت:</label>
                    <input type="text" id="tmpl-name" placeholder="مثال: اقتصادی" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white">
                </div>
                <div class="grid grid-cols-2 gap-2.5">
                    <div>
                        <label class="block text-xs text-slate-400 mb-1.5">حجم (GB):</label>
                        <input type="number" id="tmpl-gb" value="30" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1.5">اعتبار (روز):</label>
                        <input type="number" id="tmpl-days" value="30" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white">
                    </div>
                </div>
                <button onclick="createTemplate()" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs py-3 rounded-xl font-bold transition">ذخیره تمپلت جدید</button>
            </div>
        </div>

        <!-- تب ۴: تنظیمات روان و بزرگ -->
        <div id="tab-settings" class="space-y-3 hidden">
            <div class="bg-slate-900 p-4 rounded-xl border border-slate-800 space-y-3.5">
                <h2 class="text-xs font-semibold text-slate-300">🔧 تنظیمات توکن و شناسه ادمین</h2>
                <div>
                    <label class="block text-xs text-slate-400 mb-1.5">توکن ربات (Bot Token):</label>
                    <input type="text" id="set-token" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:border-sky-500 focus:outline-none" placeholder="123456:ABC-DEF...">
                </div>
                <div>
                    <label class="block text-xs text-slate-400 mb-1.5">شناسه تلگرام ادمین (Admin ID):</label>
                    <input type="number" id="set-admin" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:border-sky-500 focus:outline-none" placeholder="مثلاً 987654321">
                </div>
                <button onclick="saveSettings()" class="w-full bg-sky-600 hover:bg-sky-500 text-white text-sm py-3.5 rounded-xl font-bold transition shadow">💾 ذخیره تنظیمات</button>
                <div id="settings-result" class="text-xs mt-2 text-center font-bold"></div>
            </div>
        </div>

        <!-- تب ۵: جستجو و کاربران -->
        <div id="tab-users" class="space-y-3 hidden">
            <div class="bg-slate-900 p-4 rounded-xl border border-slate-800">
                <h2 class="text-xs font-semibold mb-3 text-slate-300">🔍 جستجوی کاربر</h2>
                <input type="text" id="search-box" placeholder="نام کاربری را وارد کنید..." class="w-full bg-slate-950 border border-slate-800 p-3 rounded-xl text-sm text-white focus:outline-none focus:border-sky-500 mb-3" oninput="searchUser()">
                
                <div id="user-result" class="space-y-2 text-xs">
                    <p class="text-slate-500 text-center py-4">نام کاربری را جستجو کنید.</p>
                </div>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();

            function switchTab(tab) {
                ['dashboard', 'create', 'templates', 'settings', 'users'].forEach(t => {
                    document.getElementById(`tab-${t}`).classList.add('hidden');
                    document.getElementById(`btn-${t}`).className = 'py-2.5 text-[10px] font-bold rounded-lg text-slate-400 transition';
                });
                document.getElementById(`tab-${tab}`).classList.remove('hidden');
                document.getElementById(`btn-${tab}`).className = 'py-2.5 text-[10px] font-bold rounded-lg bg-sky-600 text-white transition shadow';
                if(tab === 'dashboard') loadStats();
                if(tab === 'templates') loadTemplates();
                if(tab === 'settings') loadSettings();
            }

            async function loadStats() {
                try {
                    let res = await fetch('/api/stats');
                    let data = await res.json();
                    document.getElementById('stat-total').innerText = data.total_user || 0;
                    document.getElementById('stat-active').innerText = data.users_active || 0;
                    document.getElementById('cpu-usage').innerText = (data.cpu_usage || 0) + '%';
                    document.getElementById('ram-usage').innerText = ((data.mem_used || 0) / (1024*1024*1024)).toFixed(2) + ' GB';
                } catch(e) {}
            }

            async function loadTemplates() {
                let box = document.getElementById('templates-list');
                try {
                    let res = await fetch('/api/templates');
                    let data = await res.json();
                    let html = '';
                    for(let name in data.templates) {
                        let t = data.templates[name];
                        let isActive = name === data.active;
                        html += `
                            <div class="bg-slate-950 p-3 rounded-xl border ${isActive ? 'border-sky-500' : 'border-slate-800'} flex justify-between items-center">
                                <div>
                                    <span class="font-bold text-white text-sm">${isActive ? '✅ ' : ''}${name}</span>
                                    <p class="text-[11px] text-slate-400 mt-0.5">💾 ${t.gb} GB | 📅 ${t.days} روز</p>
                                </div>
                                ${!isActive ? `<button onclick="setActiveTemplate('${name.replace(/'/g, "\\'")}')" class="bg-sky-600/20 text-sky-400 text-xs px-3 py-2 rounded-lg font-bold hover:bg-sky-600/30">انتخاب فعال</button>` : '<span class="text-xs text-emerald-400 font-bold px-2">فعال</span>'}
                            </div>
                        `;
                    }
                    box.innerHTML = html;
                } catch(e) {}
            }

            async function setActiveTemplate(name) {
                await fetch('/api/templates/active', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
                loadTemplates();
            }

            async function createTemplate() {
                let name = document.getElementById('tmpl-name').value.trim();
                let gb = parseFloat(document.getElementById('tmpl-gb').value);
                let days = parseInt(document.getElementById('tmpl-days').value);
                if(!name) { alert('نام تمپلت الزامی است.'); return; }
                
                let res = await fetch('/api/templates/create', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, gb, days})});
                let data = await res.json();
                if(data.success) { alert('تمپلت ساخته شد.'); document.getElementById('tmpl-name').value = ''; loadTemplates(); }
            }

            async function loadSettings() {
                try {
                    let res = await fetch('/api/settings');
                    let data = await res.json();
                    document.getElementById('set-token').value = data.bot_token || '';
                    document.getElementById('set-admin').value = (data.admin_ids && data.admin_ids.length > 0) ? data.admin_ids[0] : '';
                } catch(e) {}
            }

            async function saveSettings() {
                let bot_token = document.getElementById('set-token').value.trim();
                let admin_val = document.getElementById('set-admin').value.trim();
                let admin_ids = admin_val ? [parseInt(admin_val)] : [];
                
                let res = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bot_token, admin_ids})});
                let data = await res.json();
                let resBox = document.getElementById('settings-result');
                if(data.success) {
                    resBox.innerHTML = '<span class="text-emerald-400">✅ تنظیمات با موفقیت ذخیره شد!</span>';
                } else {
                    resBox.innerHTML = '<span class="text-rose-400">❌ خطا در ذخیره تنظیمات</span>';
                }
            }

            async function createUser() {
                let base = document.getElementById('new-base').value.trim();
                let start = document.getElementById('new-start').value.trim();
                let count = document.getElementById('new-count').value;
                let serviceId = document.getElementById('new-service').value;
                let resBox = document.getElementById('create-result');
                let userId = tg.initDataUnsafe?.user?.id;

                if(!base) { alert('نام پایه الزامی است.'); return; }

                resBox.innerHTML = '<span class="text-sky-400">در حال ساخت و ارسال به ربات تلگرام...</span>';

                try {
                    let res = await fetch('/api/user/create', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({base, start: start ? parseInt(start) : null, count: parseInt(count), service_id: parseInt(serviceId), chat_id: userId})
                    });
                    let data = await res.json();
                    if(data.success) {
                        resBox.innerHTML = '<span class="text-emerald-400 font-bold text-sm">✅ اکانت‌ها ساخته شدند و مشخصات کامل همراه QR Code به ربات تلگرام شما ارسال گشت!</span>';
                    } else {
                        resBox.innerHTML = `<span class="text-rose-400 font-bold">❌ ${data.message || 'خطا در ساخت کاربر'}</span>`;
                    }
                } catch(e) { resBox.innerHTML = '<span class="text-rose-400">❌ خطا در ارتباط با سرور</span>'; }
            }

            async function searchUser() {
                let q = document.getElementById('search-box').value.trim();
                let box = document.getElementById('user-result');
                if(!q) { box.innerHTML = '<p class="text-slate-500 text-center py-4">نام کاربری را جستجو کنید</p>'; return; }
                
                box.innerHTML = '<p class="text-sky-400 text-center py-4">در حال جستجو...</p>';
                try {
                    let res = await fetch(`/api/user/${q}`);
                    let data = await res.json();
                    if(data.error) {
                        box.innerHTML = '<p class="text-rose-400 text-center py-4">کاربر یافت نشد</p>';
                    } else {
                        let usedGb = (data.used_traffic / (1024*1024*1024)).toFixed(2);
                        let limitGb = data.data_limit > 0 ? (data.data_limit / (1024*1024*1024)).toFixed(2) + ' GB' : 'نامحدود';
                        box.innerHTML = `
                            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-2.5">
                                <div class="flex justify-between items-center">
                                    <span class="font-bold text-white text-sm">${data.username}</span>
                                    <span class="text-xs bg-sky-500/20 text-sky-400 px-2.5 py-0.5 rounded">${data.status}</span>
                                </div>
                                <p class="text-xs text-slate-400">📊 مصرف: ${usedGb} / ${limitGb}</p>
                                <div class="bg-slate-900 p-2.5 rounded-lg border border-slate-800 text-[11px] text-sky-300 break-all select-all">
                                    ${data.subscription_url || 'لینک نامشخص'}
                                </div>
                                <div class="flex gap-2 pt-1">
                                    <button onclick="resetTraffic('${data.username}')" class="flex-1 bg-rose-600/20 text-rose-400 text-xs py-2 rounded-lg hover:bg-rose-600/30 font-medium">🔄 صفر کردن</button>
                                    <button onclick="deleteUser('${data.username}')" class="flex-1 bg-slate-800 text-slate-300 text-xs py-2 rounded-lg hover:bg-slate-700 font-medium">🗑 حذف</button>
                                </div>
                            </div>
                        `;
                    }
                } catch(e) {}
            }

            async function resetTraffic(uname) {
                if(!confirm(`ترافیک ${uname} صفر شود؟`)) return;
                let res = await fetch(`/api/user/${uname}/reset`, {method: 'POST'});
                let data = await res.json();
                if(data.success) { alert('صفر شد.'); searchUser(); }
            }

            async function deleteUser(uname) {
                if(!confirm(`کاربر ${uname} حذف شود؟`)) return;
                let res = await fetch(`/api/user/${uname}/delete`, {method: 'POST'});
                let data = await res.json();
                if(data.success) { alert('حذف شد.'); searchUser(); }
            }

            async function restartCore() {
                if(!confirm('هسته ریستارت شود؟')) return;
                let res = await fetch('/api/core/restart', {method: 'POST'});
                let data = await res.json();
                if(data.success) alert('انجام شد.');
            }

            loadStats();
        </script>
    </body>
    </html>
    """

@app.get("/api/templates")
async def api_get_templates(username: str = Depends(verify_credentials)):
    cfg = load_config()
    return {"templates": cfg.get("templates", {}), "active": cfg.get("active_template")}

@app.post("/api/templates/active")
async def api_set_active_template(req: Request, username: str = Depends(verify_credentials)):
    data = await req.json()
    name = data.get("name")
    cfg = load_config()
    if name in cfg.get("templates", {}):
        cfg["active_template"] = name
        save_config(cfg)
        return {"success": True}
    return {"success": False}

@app.post("/api/templates/create")
async def api_create_template(req: Request, username: str = Depends(verify_credentials)):
    data = await req.json()
    name = data.get("name")
    gb = data.get("gb", 30)
    days = data.get("days", 30)
    cfg = load_config()
    cfg["templates"][name] = {"gb": gb, "days": days, "mode": "on_hold"}
    cfg["active_template"] = name
    save_config(cfg)
    return {"success": True}

@app.get("/api/settings")
async def api_get_settings(username: str = Depends(verify_credentials)):
    cfg = load_config()
    return {"bot_token": cfg.get("bot_token", ""), "admin_ids": cfg.get("admin_ids", [])}

@app.post("/api/settings")
async def api_save_settings(req: Request, username: str = Depends(verify_credentials)):
    data = await req.json()
    cfg = load_config()
    cfg["bot_token"] = data.get("bot_token", "")
    cfg["admin_ids"] = data.get("admin_ids", [])
    save_config(cfg)
    return {"success": True}

@app.get("/api/stats")
async def api_stats(username: str = Depends(verify_credentials)):
    try: return await api_client.req("GET", "/api/system")
    except Exception: return {}

@app.get("/api/user/{username_param}")
async def api_get_user(username_param: str, username: str = Depends(verify_credentials)):
    try:
        u = await api_client.req("GET", f"/api/user/{username_param}")
        if not u or "username" not in u: return {"error": True}
        return u
    except Exception: return {"error": True}

@app.post("/api/user/create")
async def api_create_user(req: Request, username: str = Depends(verify_credentials)):
    data = await req.json()
    base = data.get("base")
    start = data.get("start")
    count = data.get("count", 1)
    service_id = data.get("service_id")
    chat_id = data.get("chat_id")

    cfg = load_config()
    token = cfg.get("bot_token")
    admin_ids = cfg.get("admin_ids", [])
    
    # تعیین هوشمند چت آیدی برای ارسال به ادمین
    target_chat = None
    if chat_id and chat_id in admin_ids:
        target_chat = chat_id
    elif admin_ids:
        target_chat = admin_ids[0]
    elif chat_id:
        target_chat = chat_id

    if not token or not target_chat:
        return {"success": False, "message": "لطفاً ابتدا از تب «تنظیمات»، توکن ربات و شناسه ادمین (Admin ID) خود را ذخیره کنید!"}
    
    bot = Bot(token=token)

    active_name = cfg.get("active_template")
    t_data = cfg.get("templates", {}).get(active_name, {"gb": 30, "days": 30, "mode": "on_hold"})
    
    limit = int(t_data["gb"] * (1024 ** 3)) if t_data["gb"] > 0 else 0
    days = t_data["days"]
    mode = t_data["mode"]

    usernames = [base] if start is None else [f"{base}{start + i}" for i in range(count)]
    created = []

    for u in usernames:
        p = {"username": u, "data_limit": limit, "service_id": service_id, "status": mode}
        if mode == "on_hold":
            p["on_hold_expire_duration"] = int(days * 86400) if days > 0 else 2592000
        else:
            p["expire"] = int(time.time()) + (days * 86400) if days > 0 else 0

        try:
            r = await api_client.req("POST", "/api/user", json=p)
            sub_url = full_sub(r.get("subscription_url", ""))
            created.append({"username": u, "sub": sub_url})

            cap = f"✅ کاربر: {u}\n💾 حجم: {t_data['gb']} GB | اعتبار: {days} روز ({mode})\n\n🔗 لینک ساب (کلیک جهت کپی):\n`{sub_url}`"
            await bot.send_photo(chat_id=target_chat, photo=make_qr(sub_url), caption=cap, parse_mode="Markdown")
        except Exception as e:
            pass
        await asyncio.sleep(0.15)

    return {"success": True, "created": created}

@app.post("/api/user/{username_param}/reset")
async def api_reset_user(username_param: str, username: str = Depends(verify_credentials)):
    try:
        await api_client.req("POST", f"/api/user/{username_param}/reset")
        return {"success": True}
    except Exception: return {"success": False}

@app.post("/api/user/{username_param}/delete")
async def api_delete_user(username_param: str, username: str = Depends(verify_credentials)):
    try:
        await api_client.req("DELETE", f"/api/user/{username_param}")
        return {"success": True}
    except Exception: return {"success": False}

@app.post("/api/core/restart")
async def api_restart_core(username: str = Depends(verify_credentials)):
    try:
        await api_client.req("POST", "/api/core/restart")
        return {"success": True}
    except Exception: return {"success": False}
