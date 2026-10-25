#!/usr/bin/env python3
"""راه‌انداز وب‌پنل: پورت و SSL را از config.json می‌خواند.

اگر SSL روشن باشد و گواهی سالم باشد با https بالا می‌آید؛ اگر گواهی خراب/ناموجود باشد
به‌جای از دسترس خارج شدن، روی http بالا می‌آید و علت در تنظیمات نمایش داده می‌شود.
"""
from __future__ import annotations

import logging
import os

import uvicorn

import common as C
import tlsutil

log = logging.getLogger("rebecca.run")


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    os.environ["REBECCA_LAUNCHER"] = "run_web"      # webapp می‌فهمد با این راه‌انداز (و پشتیبانی SSL) بالا آمده، نه مستقیم با uvicorn
    cfg = C.load_config()
    port = int(cfg.get("web_port") or 8686)
    ss = cfg.get("ssl") or {}
    kw: dict = {}
    err = ""
    if ss.get("enabled"):
        err = tlsutil.check_pair(ss.get("cert", ""), ss.get("key", ""))
        if err:
            log.error("SSL فعال است ولی گواهی مشکل دارد؛ روی http بالا می‌آیم: %s", err)
        else:
            kw = {"ssl_certfile": ss["cert"], "ssl_keyfile": ss["key"]}
            os.environ["REBECCA_SSL_ACTIVE"] = "1"
            log.info("وب‌پنل با SSL روی پورت %s", port)

    def mark(c):
        c["ssl"]["error"] = err
    if (ss.get("error") or "") != err:
        C.update_config(mark)

    uvicorn.run("webapp:app", host="0.0.0.0", port=port, access_log=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", **kw)


if __name__ == "__main__":
    main()
