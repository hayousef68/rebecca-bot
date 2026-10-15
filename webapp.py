import os, json, time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

app = FastAPI(title="Rebecca Advanced WebApp")

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# کلاینت ارتباطی با پنل ربکا
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

# صفحه گرافیکی وب‌اپ (HTML & Tailwind CSS)
@app.get("/", response_class=HTMLResponse)
async def webapp_home(request: Request):
    return """
    <!DOCTYPE html>
    <html lang="fa" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>وب‌اپ مدیریت ربکا</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <style>
            body { font-family: Tahoma, sans-serif; background: #0b0f19; color: #f3f4f6; }
        </style>
    </head>
    <body class="p-4 max-w-lg mx-auto">
        <header class="flex justify-between items-center mb-6 bg-slate-900 p-4 rounded-xl border border-slate-800">
            <h1 class="text-lg font-bold text-sky-400">⚡ پنل مدیریت ربکا</h1>
            <span id="sys-status" class="text-xs bg-emerald-500/20 text-emerald-400 px-2 py-1 rounded-full">🟢 متصل</span>
        </header>

        <!-- بخش آمار سرور -->
        <div class="grid grid-cols-2 gap-3 mb-6">
            <div class="bg-slate-900 p-3 rounded-xl border border-slate-800">
                <span class="text-xs text-slate-400">کل کاربران</span>
                <p id="stat-total" class="text-xl font-bold text-sky-300 mt-1">...</p>
            </div>
            <div class="bg-slate-900 p-3 rounded-xl border border-slate-800">
                <span class="text-xs text-slate-400">کاربران فعال</span>
                <p id="stat-active" class="text-xl font-bold text-emerald-400 mt-1">...</p>
            </div>
        </div>

        <!-- جستجو و لیست کاربران -->
        <div class="bg-slate-900 p-4 rounded-xl border border-slate-800 mb-6">
            <h2 class="text-sm font-semibold mb-3 text-slate-300">🔍 جستجوی کاربر</h2>
            <input type="text" id="search-box" placeholder="نام کاربری را وارد کنید..." class="w-full bg-slate-950 border border-slate-800 p-2.5 rounded-lg text-sm text-white focus:outline-none focus:border-sky-500 mb-3" oninput="searchUser()">
            
            <div id="user-result" class="space-y-2 text-sm">
                <p class="text-slate-500 text-center py-2">نام کاربری را جستجو کنید یا لیست زیر را ببینید</p>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();

            async function loadStats() {
                try {
                    let res = await fetch('/api/stats');
                    let data = await res.json();
                    document.getElementById('stat-total').innerText = data.total_user || 0;
                    document.getElementById('stat-active').innerText = data.users_active || 0;
                } catch(e) { console.log(e); }
            }

            async function searchUser() {
                let q = document.getElementById('search-box').value.trim();
                let box = document.getElementById('user-result');
                if(!q) { box.innerHTML = '<p class="text-slate-500 text-center py-2">نام کاربری را جستجو کنید</p>'; return; }
                
                box.innerHTML = '<p class="text-sky-400 text-center py-2">در حال جستجو...</p>';
                try {
                    let res = await fetch(`/api/user/${q}`);
                    let data = await res.json();
                    if(data.error) {
                        box.innerHTML = '<p class="text-rose-400 text-center py-2">کاربر یافت نشد</p>';
                    } else {
                        box.innerHTML = `
                            <div class="bg-slate-950 p-3 rounded-lg border border-slate-800">
                                <div class="flex justify-between items-center mb-2">
                                    <span class="font-bold text-white">${data.username}</span>
                                    <span class="text-xs bg-sky-500/20 text-sky-400 px-2 py-0.5 rounded">${data.status}</span>
                                </div>
                                <p class="text-xs text-slate-400 mb-2">حجم مصرفی: ${(data.used_traffic / (1024*1024*1024)).toFixed(2)} GB</p>
                                <button onclick="resetTraffic('${data.username}')" class="w-full bg-rose-600/20 text-rose-400 text-xs py-1.5 rounded hover:bg-rose-600/30">🔄 صفر کردن مصرف</button>
                            </div>
                        `;
                    }
                } catch(e) { box.innerHTML = '<p class="text-rose-400 text-center py-2">خطا در ارتباط</p>'; }
            }

            async function resetTraffic(uname) {
                if(!confirm(`آیا مصرف کاربر ${uname} صفر شود؟`)) return;
                let res = await fetch(`/api/user/${uname}/reset`, {method: 'POST'});
                let data = await res.json();
                if(data.success) alert('مصرف کاربر با موفقیت صفر شد.');
            }

            loadStats();
        </script>
    </body>
    </html>
    """

# APIهای پشتیبان وب‌اپ
@app.get("/api/stats")
async def api_stats():
    try:
        st = await api_client.req("GET", "/api/system")
        return st
    except Exception:
        return {"total_user": 0, "users_active": 0}

@app.get("/api/user/{username}")
async def api_get_user(username: str):
    try:
        u = await api_client.req("GET", f"/api/user/{username}")
        if not u or "username" not in u:
            return {"error": True}
        return u
    except Exception:
        return {"error": True}

@app.post("/api/user/{username}/reset")
async def api_reset_user(username: str):
    try:
        await api_client.req("POST", f"/api/user/{username}/reset")
        return {"success": True}
    except Exception:
        return {"success": False}
