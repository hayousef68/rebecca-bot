bash <(curl -s https://raw.githubusercontent.com/hayousef68/rebecca-bot/main/install.sh)





# rebecca-bot



نصب 

apt update && apt install -y python3-venv python3-full git && rm -rf /opt/rebecca-bot && git clone https://github.com/hayousef68/rebecca-bot.git /opt/rebecca-bot && cd /opt/rebecca-bot && python3 -m venv /opt/rebecca-bot/venv && /opt/rebecca-bot/venv/bin/pip install --upgrade pip && /opt/rebecca-bot/venv/bin/pip install httpx qrcode python-telegram-bot fastapi uvicorn && cp /opt/rebecca-bot/*.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable rebecca-bot rebecca-webapp && systemctl restart rebecca-bot rebecca-webapp && echo "✅ نصب و راه‌اندازی از گیت‌هاب با موفقیت انجام شد!"



. راه‌اندازی و فعال‌سازی سرویس Systemd


cat <<EOF > /etc/systemd/system/rebecca-bot.service
[Unit]
Description=Rebecca Panel Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/rebecca-bot
ExecStart=/opt/rebecca-bot/venv/bin/python3 /opt/rebecca-bot/bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rebecca-bot
systemctl start rebecca-bot




ریستارت کردن ربات

systemctl restart rebecca-bot



مشاهده لاگ

journalctl -u rebecca-bot -f



حذف کامل ربات و سرویس

systemctl stop rebecca-bot
systemctl disable rebecca-bot
rm -f /etc/systemd/system/rebecca-bot.service
systemctl daemon-reload
rm -rf /opt/rebecca-bot
