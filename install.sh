#!/bin/bash
set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

INSTALL_DIR="/opt/rebecca-bot"
SERVICE_FILE="/etc/systemd/system/rebecca-bot.service"
# آدرس Raw مخزن گیت‌هاب خود را اینجا قرار دهید:
REPO_RAW_URL="https://raw.githubusercontent.com/hayousef68/rebecca-bot/main"

clear
echo -e "${CYAN}=====================================================${NC}"
echo -e "${GREEN}      Rebecca Panel Telegram Bot - Auto Installer    ${NC}"
echo -e "${CYAN}=====================================================${NC}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Please run as root (sudo su)${NC}"
  exit 1
fi

echo -e "\n${YELLOW}[1/4] Installing lightweight Python dependencies...${NC}"
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip curl

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo -e "${YELLOW}[2/4] Downloading bot.py from GitHub...${NC}"
curl -Ls "$REPO_RAW_URL/bot.py" -o "$INSTALL_DIR/bot.py"

echo -e "${YELLOW}[3/4] Setting up Python Virtual Environment...${NC}"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install "python-telegram-bot>=20.8" httpx "qrcode[pil]" -q

echo -e "\n${CYAN}-----------------------------------------------------${NC}"
read -p "Enter your Telegram Bot Token: " BOT_TOKEN
read -p "Enter your Numeric Telegram Admin ID: " ADMIN_ID
echo -e "${CYAN}-----------------------------------------------------${NC}"

cat <<EOF > "$INSTALL_DIR/config.json"
{
  "bot_token": "${BOT_TOKEN}",
  "admin_ids": [${ADMIN_ID}],
  "panel_url": "",
  "sub_url": "",
  "username": "",
  "password": ""
}
EOF

echo -e "${YELLOW}[4/4] Creating systemd service...${NC}"
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Rebecca Panel Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rebecca-bot

echo -e "\n${GREEN}✅ Installation Complete!${NC}"
echo -e "${CYAN}Now open your Telegram bot, send /start, and click '🔌 اتصال به پنل ربکا' to enter your Rebecca panel username & password!${NC}"
