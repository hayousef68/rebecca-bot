#!/usr/bin/env bash
# Rebecca Bot + Web Panel — نصب، به‌روزرسانی و حذف تک‌خطی
#
#   نصب:      bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh)
#   حذف:      bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh) uninstall -y
#   بعد از نصب دستور  rebecca-bot  در دسترس است:  rebecca-bot {update|restart|status|logs|passwd|info|uninstall}
#
# نصب بدون سؤال:  BOT_TOKEN=123:ABC ADMIN_ID=111,222 WEB_PORT=8686 bash <(curl -Ls ...)
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/hayousef68/rebecca-bot/main}"
DIR="/opt/rebecca-bot"
FILES="common.py sales.py bot.py webapp.py index.html requirements.txt"
BOT_UNIT="/etc/systemd/system/rebecca-bot.service"
WEB_UNIT="/etc/systemd/system/rebecca-webapp.service"
BIN="/usr/local/bin/rebecca-bot"

G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; C=$'\033[0;36m'; N=$'\033[0m'
ok()   { echo "${G}✔${N} $*"; }
info() { echo "${C}➜${N} $*"; }
warn() { echo "${Y}!${N} $*"; }
die()  { echo "${R}✘ $*${N}" >&2; exit 1; }

need_root() { [ "${EUID:-$(id -u)}" -eq 0 ] || die "با کاربر root اجرا کنید (sudo -i)."; }

ask() { # ask VAR "متن"  — اگر مقدار از env آمده باشد سؤال نمی‌پرسد
  local var="$1" prompt="$2" cur="${!1:-}"
  if [ -z "$cur" ] && [ -r /dev/tty ]; then read -r -p "$prompt" cur </dev/tty || true; fi
  printf -v "$var" '%s' "$cur"
}

install_deps() {
  info "نصب پیش‌نیازها…"
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null
  elif command -v dnf >/dev/null; then dnf install -y -q python3 python3-pip curl ca-certificates >/dev/null
  elif command -v yum >/dev/null; then yum install -y -q python3 python3-pip curl ca-certificates >/dev/null
  else die "مدیر بسته پشتیبانی‌نشده؛ python3 (+venv) و curl را دستی نصب کنید."; fi
  python3 -c 'import venv, sys; assert sys.version_info >= (3, 9)' 2>/dev/null || die "Python 3.9+ همراه با venv لازم است."
}

download() {
  info "دریافت فایل‌ها از گیت‌هاب…"
  mkdir -p "$DIR"
  local f
  for f in $FILES install.sh; do
    curl -fsSL --retry 3 --connect-timeout 10 "$REPO_RAW/$f?t=$(date +%s)" -o "$DIR/$f.tmp" || die "دانلود $f ناموفق بود."
    mv -f "$DIR/$f.tmp" "$DIR/$f"
  done
  chmod +x "$DIR/install.sh"
  ln -sf "$DIR/install.sh" "$BIN"
}

setup_venv() {
  info "ساخت محیط پایتون و نصب کتابخانه‌ها (ممکن است کمی طول بکشد)…"
  [ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install -q --disable-pip-version-check --upgrade pip >/dev/null
  "$DIR/venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"
}

init_config() {
  local token="${BOT_TOKEN:-}" admin="${ADMIN_ID:-}" args=() line
  local have_cfg=0; [ -f "$DIR/config.json" ] && have_cfg=1
  if [ "$have_cfg" -eq 0 ] || [ -n "$token$admin" ]; then
    ask token "توکن ربات تلگرام (از @BotFather، برای رد کردن Enter): "
    ask admin "شناسه عددی ادمین(ها) (با کاما): "
  fi
  if [ -n "$token" ] && ! [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then die "فرمت توکن صحیح نیست."; fi
  if [ -n "$admin" ] && ! [[ "$admin" =~ ^-?[0-9]+(,[[:space:]]*-?[0-9]+)*$ ]]; then die "شناسه ادمین باید عدد باشد (چندتا با کاما)."; fi
  [ -n "$token" ] && args+=(--token "$token")
  [ -n "$admin" ] && args+=(--admin "$admin")
  [ -n "${WEB_PORT:-}" ] && args+=(--port "$WEB_PORT")
  WEB_PASSWORD=""; WEB_USER="admin"; PORT="8686"
  while IFS='=' read -r k v; do
    case "$k" in WEB_PASSWORD) WEB_PASSWORD="$v";; WEB_USER) WEB_USER="$v";; WEB_PORT) PORT="$v";; esac
  done < <(cd "$DIR" && "$DIR/venv/bin/python" common.py init ${args[@]+"${args[@]}"})
  chmod 600 "$DIR/config.json"
}

write_units() {
  cat > "$BOT_UNIT" <<EOF
[Unit]
Description=Rebecca Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/venv/bin/python $DIR/bot.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  cat > "$WEB_UNIT" <<EOF
[Unit]
Description=Rebecca Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/venv/bin/python -m uvicorn webapp:app --host 0.0.0.0 --port $PORT --no-access-log --proxy-headers
Restart=always
RestartSec=3
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

has_token() { (cd "$DIR" && "$DIR/venv/bin/python" -c 'import common,sys; sys.exit(0 if common.load_config()["bot_token"] else 1)'); }

start_services() {
  systemctl enable rebecca-webapp >/dev/null 2>&1
  systemctl restart rebecca-webapp
  if has_token; then
    systemctl enable rebecca-bot >/dev/null 2>&1
    systemctl restart rebecca-bot
    ok "ربات و وب‌پنل اجرا شدند."
  else
    warn "توکن ربات خالی است؛ ربات اجرا نشد. از وب‌پنل > تنظیمات توکن را بدهید (ربات خودکار ریستارت می‌شود)."
    systemctl enable rebecca-bot >/dev/null 2>&1 || true
  fi
}

server_ip() { curl -s4 --max-time 4 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'; }

show_info() {
  local port ip
  port=$(cd "$DIR" && "$DIR/venv/bin/python" -c 'import common;print(common.load_config()["web_port"])')
  ip=$(server_ip)
  echo
  echo "${C}════════════════════════════════════════════${N}"
  echo " 🌐 وب‌پنل:  http://${ip:-IP}:${port}"
  echo " 👤 کاربر:   ${WEB_USER:-admin}"
  if [ -n "${WEB_PASSWORD:-}" ]; then echo " 🔑 رمز:     ${WEB_PASSWORD}   ${Y}(همین حالا ذخیره کنید)${N}"
  else echo " 🔑 رمز:     (قبلاً ساخته شده؛ برای رمز جدید: rebecca-bot passwd)"; fi
  echo " ⚠  برای امنیت، وب‌پنل را پشت HTTPS (Nginx/Caddy) بگذارید یا پورت را فقط برای IP خودتان باز کنید."
  echo " 🛠 دستورها: rebecca-bot {update|restart|status|logs|passwd|info|uninstall}"
  echo "${C}════════════════════════════════════════════${N}"
}

do_install() {
  need_root; install_deps; download; setup_venv; init_config; write_units; start_services
  ok "نصب کامل شد."; show_info
}

do_update() {
  need_root; [ -d "$DIR" ] || die "ابتدا نصب کنید."
  download; setup_venv
  PORT=$(cd "$DIR" && "$DIR/venv/bin/python" -c 'import common;print(common.load_config()["web_port"])')
  write_units; start_services; ok "به‌روزرسانی انجام شد (config.json حفظ شد)."
}

do_uninstall() {
  need_root
  local yes=0 keep=0 a
  for a in "$@"; do case "$a" in -y|--yes) yes=1;; --keep-config) keep=1;; esac; done
  [ "${YES:-0}" = "1" ] && yes=1
  if [ "$yes" -ne 1 ]; then
    local ans=""; read -r -p "ربات و وب‌پنل کاملاً حذف شوند؟ [y/N] " ans </dev/tty || true
    [[ "$ans" =~ ^[yY]$ ]] || { echo "لغو شد."; return 0; }
  fi
  if [ "$keep" -eq 1 ] && [ -f "$DIR/config.json" ]; then
    cp "$DIR/config.json" /root/rebecca-bot.config.bak.json && chmod 600 /root/rebecca-bot.config.bak.json
    ok "نسخه پشتیبان config: /root/rebecca-bot.config.bak.json"
  fi
  systemctl disable --now rebecca-bot rebecca-webapp >/dev/null 2>&1 || true
  rm -f "$BOT_UNIT" "$WEB_UNIT" "$BIN"
  systemctl daemon-reload
  rm -rf "$DIR"
  ok "حذف کامل انجام شد."
}

do_passwd() {
  need_root
  local out; out=$(cd "$DIR" && "$DIR/venv/bin/python" common.py passwd "$@")
  echo "$out" | sed -n 's/^WEB_PASSWORD=/🔑 رمز جدید وب‌پنل: /p'
}

main() {
  local cmd="${1:-install}"; shift || true
  case "$cmd" in
    install)   do_install ;;
    update|upgrade) do_update ;;
    uninstall|remove) do_uninstall "$@" ;;
    restart)   need_root; systemctl restart rebecca-webapp; systemctl restart rebecca-bot || warn "ربات اجرا نشد (توکن؟)"; ok "ریستارت شد." ;;
    status)    systemctl --no-pager -l status rebecca-bot rebecca-webapp || true ;;
    logs)      journalctl -u rebecca-bot -u rebecca-webapp -f -n 80 --no-hostname -o cat ;;
    passwd)    do_passwd "$@" ;;
    info)      show_info ;;
    *)         echo "استفاده: $0 {install|update|uninstall [-y] [--keep-config]|restart|status|logs|passwd|info}"; exit 1 ;;
  esac
}
main "$@"
exit $?
