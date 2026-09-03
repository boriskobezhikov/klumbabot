#!/usr/bin/env bash
#
# Первичная установка на чистый VPS. Идемпотентен — можно запускать повторно.
#
#   git clone git@github.com:boriskobezhikov/klumbabot.git /opt/klumba-bot
#   bash /opt/klumba-bot/deploy/install.sh
#
set -euo pipefail

APP_DIR=/opt/klumba-bot
APP_USER=klumba
UNIT=klumba-bot.service

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запусти от root: sudo bash $0"
[[ -f "$APP_DIR/main.py" ]] || die "не вижу $APP_DIR/main.py — склонируй репозиторий в $APP_DIR"

say "Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sudo

python3 - <<'PY' || die "нужен Python 3.10 или новее"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

say "Пользователь $APP_USER"
if id "$APP_USER" &>/dev/null; then
    echo "уже есть"
else
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    echo "создан"
fi

say "Виртуальное окружение"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "зависимости установлены"

say "Файл .env"
if [[ -f "$APP_DIR/.env" ]]; then
    echo "уже есть, не трогаю"
else
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "создан из .env.example — его нужно заполнить"
fi

say "Права"
# Код принадлежит root (чтобы git pull от root работал без плясок),
# каталог доступен группе на запись — приложению нужно создавать
# config.json и файлы сессии. setgid, чтобы новые файлы наследовали группу.
chgrp -R "$APP_USER" "$APP_DIR"
chmod 2775 "$APP_DIR"
chmod 640 "$APP_DIR/.env"
# Сессия Telegram = полный доступ к аккаунту, читать может только владелец
find "$APP_DIR" -maxdepth 1 -name '*.session' -exec chmod 600 {} \;
find "$APP_DIR" -maxdepth 1 -name 'config.json' -exec chown "$APP_USER" {} \;
echo "выставлены"

say "systemd"
cp "$APP_DIR/$UNIT" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
echo "юнит установлен и включён в автозапуск"

# ---------------------------------------------------------------------------
say "Что дальше"

NEEDS_ENV=0
grep -q 'your_api_hash_here' "$APP_DIR/.env" && NEEDS_ENV=1

SESSION_NAME=$(grep -E '^TG_SESSION_NAME=' "$APP_DIR/.env" | cut -d= -f2- || true)
SESSION_NAME=${SESSION_NAME:-klumba_userbot}

if [[ $NEEDS_ENV -eq 1 ]]; then
    cat <<EOF

1) Заполни настройки:

     nano $APP_DIR/.env

2) Войди в Telegram (разово, спросит номер и код из приложения):

     bash $APP_DIR/deploy/login.sh

3) Запусти:

     systemctl start $UNIT
     journalctl -u $UNIT -f
EOF
elif [[ ! -f "$APP_DIR/$SESSION_NAME.session" ]]; then
    cat <<EOF

.env заполнен. Осталось войти в Telegram (разово, спросит номер и код):

     bash $APP_DIR/deploy/login.sh

потом:

     systemctl start $UNIT
EOF
else
    systemctl restart "$UNIT"
    sleep 2
    systemctl status "$UNIT" --no-pager --lines=10 || true
    cat <<EOF

Всё на месте, сервис перезапущен.
Логи:      journalctl -u $UNIT -f
Обновить:  bash $APP_DIR/deploy/update.sh
EOF
fi
