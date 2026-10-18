#!/usr/bin/env python3
"""کمک‌های SSL وب‌پنل: پیدا کردن گواهی پنل ربکا روی همین سرور، اعتبارسنجی جفت cert/key و تاریخ انقضا."""
from __future__ import annotations

import glob
import os
import re
import shutil
import ssl
import subprocess
from urllib.parse import urlsplit

# فایل‌های env پنل‌های مرزبانی/ربکا که مسیر SSL در آن‌ها (UVICORN_SSL_CERTFILE / KEYFILE) نوشته می‌شود
ENV_FILES = (
    "/opt/rebecca/.env", "/opt/rebecca-panel/.env", "/opt/Rebecca/.env", "/etc/opt/rebecca/.env",
    "/opt/marzban/.env", "/etc/opt/marzban/.env",
)
# پوشه‌هایی که معمولاً گواهی در آن‌ها نگه‌داری می‌شود (هر پوشه = یک دامنه)
CERT_DIR_GLOBS = (
    "/var/lib/rebecca/certs/*", "/var/lib/rebecca/ssl/*", "/var/lib/marzban/certs/*",
    "/etc/letsencrypt/live/*", "/root/.acme.sh/*", "/etc/ssl/*",
)
CERT_NAMES = ("fullchain.pem", "fullchain.cer", "cert.pem", "certificate.crt", "server.crt", "ssl.crt")
KEY_NAMES = ("privkey.pem", "key.pem", "private.key", "server.key", "ssl.key")


def check_pair(cert: str, key: str) -> str:
    """رشته‌ی خالی = سالم؛ در غیر این صورت علت خطا."""
    if not cert or not key:
        return "مسیر cert و key هر دو لازم است."
    for label, p in (("cert", cert), ("key", key)):
        if not os.path.isabs(p):
            return f"مسیر {label} باید کامل (مطلق) باشد."
        if not os.path.isfile(p):
            return f"فایل {label} پیدا نشد: {p}"
        if not os.access(p, os.R_OK):
            return f"دسترسی خواندن فایل {label} وجود ندارد: {p}"
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    except (ssl.SSLError, OSError, ValueError) as e:
        return f"cert و key معتبر یا جفت هم نیستند: {e}"
    return ""


def cert_info(cert: str) -> dict:
    """تاریخ انقضا و موضوع گواهی (اگر openssl روی سرور باشد)؛ بدون آن خالی برمی‌گرداند."""
    out: dict = {}
    if not (cert and os.path.isfile(cert) and shutil.which("openssl")):
        return out
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-subject", "-in", cert],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in r.stdout.splitlines():
        if line.startswith("notAfter="):
            out["expires"] = line.split("=", 1)[1].strip()
        elif line.startswith("subject="):
            m = re.search(r"CN\s*=\s*([^,/\n]+)", line)
            if m:
                out["cn"] = m.group(1).strip()
    return out


def _read_env(path: str) -> dict:
    d: dict = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return d


def _domain_of(url: str) -> str:
    host = (urlsplit(url if "://" in url else "//" + url).hostname or "").lower()
    return "" if re.fullmatch(r"[\d.]+|[0-9a-f:]+", host) else host


def detect(panel_url: str = "") -> list[dict]:
    """گواهی‌های قابل‌استفاده روی همین سرور؛ اول موارد خود پنل ربکا (.env) و موارد هم‌دامنه با پنل."""
    want = _domain_of(panel_url)
    seen: set = set()
    found: list[dict] = []

    def add(cert: str, key: str, source: str):
        if (cert, key) in seen:
            return
        seen.add((cert, key))
        err = check_pair(cert, key)
        info = cert_info(cert) if not err else {}
        found.append({"cert": cert, "key": key, "source": source, "ok": not err, "error": err,
                      "cn": info.get("cn", ""), "expires": info.get("expires", ""),
                      "match": bool(want and (want in cert or want == info.get("cn", "").lower()))})

    for ef in ENV_FILES:
        env = _read_env(ef)
        c, k = env.get("UVICORN_SSL_CERTFILE", ""), env.get("UVICORN_SSL_KEYFILE", "")
        if c and k:
            add(c, k, f"تنظیمات پنل ({ef})")

    for pattern in CERT_DIR_GLOBS:
        for d in sorted(glob.glob(pattern)):
            if not os.path.isdir(d):
                continue
            base = re.sub(r"_ecc$", "", os.path.basename(d))
            cert = next((os.path.join(d, n) for n in CERT_NAMES if os.path.isfile(os.path.join(d, n))), "")
            key = next((os.path.join(d, n) for n in (*KEY_NAMES, base + ".key") if os.path.isfile(os.path.join(d, n))), "")
            if cert and key:
                add(cert, key, d)

    found.sort(key=lambda x: (not x["match"], not x["ok"], not x["source"].startswith("تنظیمات پنل")))
    return found[:12]
