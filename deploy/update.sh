#!/usr/bin/env bash
#
# Обновление до свежего кода из git: pull, зависимости, перезапуск.
#
#   bash /opt/klumba-bot/deploy/update.sh
#
set -euo pipefail

APP_DIR=/opt/klumba-bot
APP_USER=klumba
UNIT=klumba-bot.service

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Запусти от root: sudo bash $0" >&2; exit 1; }

cd "$APP_DIR"

say "Забираю изменения"
BEFORE=$(git rev-parse --short HEAD)
git pull --ff-only
AFTER=$(git rev-parse --short HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "уже последняя версия ($AFTER)"
else
    echo "$BEFORE -> $AFTER"
fi

say "Зависимости"
"$APP_DIR/venv/bin/pip" install --quiet -r requirements.txt
echo "актуальны"

say "Права и юнит"
chgrp -R "$APP_USER" "$APP_DIR"
chmod 2775 "$APP_DIR"
chmod 640 "$APP_DIR/.env"
cp "$APP_DIR/klumba-bot.service" /etc/systemd/system/
systemctl daemon-reload
echo "обновлены"

say "Перезапуск"
systemctl restart "$UNIT"
sleep 3

if systemctl is-active --quiet "$UNIT"; then
    systemctl status "$UNIT" --no-pager --lines=10
    echo
    echo "Готово. Логи: journalctl -u $UNIT -f"
else
    echo "Сервис не поднялся. Последние логи:" >&2
    journalctl -u "$UNIT" --no-pager --lines=30 >&2
    exit 1
fi
