"""میدل‌ور ASGI خالص (بدون وابستگی) برای هدرهای امنیتی؛ روی همه‌ی پاسخ‌های HTTP اعمال می‌شود.

هدری که خود مسیر گذاشته باشد (مثل CSP مینی‌اپ تلگرام) هرگز بازنویسی نمی‌شود.
HSTS به‌صورت پیش‌فرض خاموش است: وب‌پنل وقتی گواهی خراب شود عمداً روی http بالا می‌آید و HSTS آن لحظه مرورگر را قفل می‌کرد.
با REBECCA_HSTS=1 (و فقط روی https) روشن می‌شود.
"""
from __future__ import annotations

import os

# 'unsafe-inline' لازم است چون index.html اسکریپت/استایل درون‌خطی دارد؛ بقیه‌ی مبداها بسته‌اند.
CSP_PAGE = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "font-src 'self' data: https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
MINI_PATH = "/app"          # مینی‌اپ تلگرام داخل وب‌ویوی تلگرام باز می‌شود؛ CSP/فریم آن جداگانه تنظیم شده


class SecurityHeaders:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        path = scope.get("path", "")
        https = scope.get("scheme") == "https"

        async def send2(msg):
            if msg["type"] == "http.response.start":
                hdrs = list(msg.get("headers") or [])
                have = {k.lower() for k, _ in hdrs}
                ct = next((v for k, v in hdrs if k.lower() == b"content-type"), b"").lower()

                def add(k: bytes, v: str) -> None:
                    if k not in have:
                        hdrs.append((k, v.encode("latin-1")))
                        have.add(k)
                add(b"x-content-type-options", "nosniff")
                add(b"referrer-policy", "no-referrer")
                add(b"permissions-policy", PERMISSIONS)
                add(b"cross-origin-opener-policy", "same-origin")
                if https and os.environ.get("REBECCA_HSTS") == "1":
                    add(b"strict-transport-security", "max-age=31536000")
                if ct.startswith(b"text/html") and path != MINI_PATH:
                    add(b"content-security-policy", CSP_PAGE)
                    add(b"x-frame-options", "DENY")
                elif ct.startswith(b"application/json"):
                    add(b"cache-control", "no-store")
                msg = {**msg, "headers": hdrs}
            await send(msg)
        await self.inner(scope, receive, send2)
