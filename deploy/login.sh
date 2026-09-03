#!/usr/bin/env bash
#
# Разовый интерактивный вход в Telegram под аккаунтом klumba.
#
# Telethon спросит номер телефона и код (код приходит в само приложение
# Telegram, не по SMS). После успешного входа появятся файлы сессии — тогда
# нажмите Ctrl+C и запускайте сервис.
#
set -euo pipefail

APP_DIR=/opt/klumba-bot
APP_USER=klumba
UNIT=klumba-bot.service

[[ $EUID -eq 0 ]] || { echo "Запусти от root: sudo bash $0" >&2; exit 1; }

# Сервис держал бы ту же сессию — вход при работающем сервисе кончится
# конфликтом за файл базы.
if systemctl is-active --quiet "$UNIT"; then
    echo "Останавливаю $UNIT на время входа..."
    systemctl stop "$UNIT"
fi

echo
echo "Введи номер телефона в формате +995XXXXXXXXX и код из Telegram."
echo "Когда увидишь строку 'Запущен. Слушаю: ...' — вход прошёл, жми Ctrl+C."
echo

cd "$APP_DIR"
sudo -u "$APP_USER" env HOME="$APP_DIR" "$APP_DIR/venv/bin/python" main.py || true

chmod 600 "$APP_DIR"/*.session 2>/dev/null || true

echo
if compgen -G "$APP_DIR/*.session" >/dev/null; then
    echo "Сессия сохранена. Запускай сервис:"
    echo "  systemctl start $UNIT"
    echo "  journalctl -u $UNIT -f"
else
    echo "Файл сессии не появился — вход не завершился. Попробуй ещё раз."
    exit 1
fi
