#!/usr/bin/env bash
# Установка gpu-deals на VPS (hoster.kz Cloud, Ubuntu/Debian).
#
# Запуск от root ПОСЛЕ того, как репозиторий склонирован в /opt/gpu-deals
# и рядом лежит заполненный .env (по образцу .env.example):
#   git clone https://github.com/john88cool-coder/gpu-deals.git /opt/gpu-deals
#   cp .env.example /opt/gpu-deals/.env && nano /opt/gpu-deals/.env
#   bash /opt/gpu-deals/deploy/install.sh
#
# Скрипт идемпотентен: повторный запуск доводит систему до того же состояния.
set -euo pipefail

APP_DIR=/opt/gpu-deals
DATA_DIR=/var/lib/gpu-deals
SVC_USER=gpudeals

[ "$(id -u)" -eq 0 ] || { echo "запустите от root"; exit 1; }
[ -d "$APP_DIR/.git" ] || { echo "нет $APP_DIR/.git — сначала склонируйте репозиторий"; exit 1; }
[ -f "$APP_DIR/.env" ] || { echo "нет $APP_DIR/.env — скопируйте .env.example и заполните токены"; exit 1; }

echo "=== 1/5 пользователь и каталоги ==="
id -u "$SVC_USER" &>/dev/null || useradd -r -m -d "/home/$SVC_USER" -s /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DATA_DIR"
chown "$SVC_USER:$SVC_USER" "$DATA_DIR"

echo "=== 2/5 uv (системно, /usr/local/bin) ==="
if ! command -v uv &>/dev/null; then
  curl -sSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
uv --version

echo "=== 3/5 зависимости и браузер ==="
cd "$APP_DIR"
uv sync --extra dns
uv run playwright install --with-deps chromium

echo "=== 4/5 база данных ==="
# История цен из репозитория переносится на VPS, чтобы сигналы не начинали
# с нуля. Дальше VPS живёт своей базой и в git ничего не коммитит.
if [ ! -f "$DATA_DIR/prices.sqlite3" ] && [ -f "$APP_DIR/data/prices.sqlite3" ]; then
  cp "$APP_DIR/data/prices.sqlite3" "$DATA_DIR/prices.sqlite3"
  echo "история цен скопирована из репозитория"
fi
chown "$SVC_USER:$SVC_USER" "$DATA_DIR/prices.sqlite3" 2>/dev/null || true

echo "=== 5/5 systemd ==="
cp "$APP_DIR"/deploy/*.service "$APP_DIR"/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now \
  gpu-deals-crawl.timer gpu-deals-watchlist.timer gpu-deals-heartbeat.timer

echo
echo "Готово. Таймеры: обход каждые 2 ч, watchlist каждые 20 мин, heartbeat ежедневно."
echo "Логи: journalctl -u gpu-deals-crawl -f"
echo "Проверка вручную: sudo -u $SVC_USER $APP_DIR/.venv/bin/gpu-deals heartbeat --console"
