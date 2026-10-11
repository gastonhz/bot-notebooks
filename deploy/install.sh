#!/usr/bin/env bash
# Instala o actualiza nbwatch en un LXC Debian 12 / Ubuntu 24.04.
# Correr como root desde la carpeta del proyecto:   bash deploy/install.sh
# Es idempotente: sirve también para actualizar el código (no pisa criteria.yaml ni judge_prompt.md editados).
set -euo pipefail

APP=/opt/nbwatch
STATE=/var/lib/nbwatch
ENV_FILE=/etc/nbwatch.env
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = 0 ] || { echo "Correr como root"; exit 1; }

echo "==> Paquetes del sistema"
apt-get update -qq
apt-get install -y -qq python3 python3-venv xvfb curl ca-certificates sqlite3 >/dev/null

echo "==> Usuario nbwatch (home = $STATE)"
id nbwatch >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --create-home --shell /bin/bash nbwatch

echo "==> Código en $APP"
mkdir -p "$APP"
for f in criteria.yaml judge_prompt.md; do
  if [ -e "$APP/$f" ] && ! cmp -s "$SRC/$f" "$APP/$f"; then
    echo "    $APP/$f ya existe y fue editado: no lo piso (la versión del repo queda en $APP/$f.new)"
    cp "$SRC/$f" "$APP/$f.new"
  else
    cp "$SRC/$f" "$APP/$f"
  fi
done
rm -rf "$APP/nbwatch"
cp -r "$SRC/nbwatch" "$SRC/requirements.txt" "$APP/"
find "$APP/nbwatch" -name __pycache__ -prune -exec rm -rf {} +

echo "==> venv + dependencias de Python"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> Chromium de Playwright y sus librerías del sistema"
export PLAYWRIGHT_BROWSERS_PATH="$APP/ms-playwright"
"$APP/venv/bin/playwright" install-deps chromium >/dev/null
"$APP/venv/bin/playwright" install chromium
chown -R root:nbwatch "$APP"
chmod -R g+rX,o+rX "$APP"

echo "==> Claude Code para el usuario nbwatch"
if [ -x "$STATE/.local/bin/claude" ]; then
  echo "    ya instalado: $("$STATE/.local/bin/claude" --version 2>/dev/null || echo '?')"
else
  runuser -u nbwatch -- bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash'
fi

echo "==> Archivo de entorno $ENV_FILE"
if [ -e "$ENV_FILE" ]; then
  echo "    ya existe, no lo toco"
else
  install -m 640 -o root -g nbwatch "$SRC/deploy/nbwatch.env.example" "$ENV_FILE"
  echo "    creado a partir del ejemplo: completá los tokens"
fi

echo "==> Atajo /usr/local/bin/nbwatch y units de systemd"
install -m 755 "$SRC/deploy/nbwatch-wrapper.sh" /usr/local/bin/nbwatch
install -m 644 "$SRC"/deploy/systemd/nbwatch-* /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Listo. Próximos pasos:
  1. Completá $ENV_FILE (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CLAUDE_CODE_OAUTH_TOKEN).
  2. nbwatch test-notify
  3. nbwatch daily --force-notify      # prueba end-to-end: scrape + juez + aviso a Telegram
  4. systemctl enable --now nbwatch-scrape.timer nbwatch-daily.timer
EOF
