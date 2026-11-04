#!/usr/bin/env bash
# Instala o actualiza nbwatch en un LXC Debian 12 / Ubuntu 24.04.
# Correr como root desde la carpeta del proyecto:   bash deploy/install.sh
# Es idempotente: sirve también para actualizar el código (no pisa criteria.yaml ni judge_prompt.md editados,
# que viven en /var/lib/nbwatch para que la interfaz web pueda escribirlos).
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

# Antes de mover los criterios: un timer que corra mientras se instala tiene que encontrarlos en $STATE.
echo "==> Archivo de entorno $ENV_FILE"
if [ -e "$ENV_FILE" ]; then
  echo "    ya existe: solo le agrego las variables nuevas que falten"
  [ -z "$(tail -c1 "$ENV_FILE")" ] || echo >> "$ENV_FILE"
  for kv in "NBWATCH_CRITERIA=$STATE/criteria.yaml" NBWATCH_WEB_USER= NBWATCH_WEB_PASSWORD=; do
    grep -q "^${kv%%=*}=" "$ENV_FILE" || { echo "$kv" >> "$ENV_FILE"; echo "    + ${kv%%=*}"; }
  done
else
  install -m 640 -o root -g nbwatch "$SRC/deploy/nbwatch.env.example" "$ENV_FILE"
  echo "    creado a partir del ejemplo: completá los tokens"
fi

echo "==> Criterios en $STATE (los edita el usuario nbwatch desde la interfaz web)"
for f in criteria.yaml judge_prompt.md; do
  if [ ! -e "$STATE/$f" ] && [ -e "$APP/$f" ]; then
    mv "$APP/$f" "$STATE/$f"   # instalaciones anteriores los tenían en $APP
    echo "    $APP/$f movido a $STATE/$f"
  fi
  rm -f "$APP/$f.new"
  if [ ! -e "$STATE/$f" ]; then
    cp "$SRC/$f" "$STATE/$f"
  elif ! cmp -s "$SRC/$f" "$STATE/$f"; then
    echo "    $STATE/$f fue editado: no lo piso (la versión del repo queda en $STATE/$f.new)"
    cp "$SRC/$f" "$STATE/$f.new"
  fi
done
chown nbwatch:nbwatch "$STATE"/criteria.yaml* "$STATE"/judge_prompt.md*

echo "==> Código en $APP"
mkdir -p "$APP"
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

echo "==> Atajo /usr/local/bin/nbwatch y units de systemd"
install -m 755 "$SRC/deploy/nbwatch-wrapper.sh" /usr/local/bin/nbwatch
install -m 644 "$SRC"/deploy/systemd/nbwatch-* /etc/systemd/system/
systemctl daemon-reload
systemctl try-restart nbwatch-web.service   # si ya estaba corriendo, que tome el código nuevo

cat <<EOF

Listo. Próximos pasos:
  1. Completá $ENV_FILE (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CLAUDE_CODE_OAUTH_TOKEN,
     NBWATCH_WEB_USER y NBWATCH_WEB_PASSWORD).
  2. nbwatch test-notify
  3. nbwatch daily --force-notify      # prueba end-to-end: scrape + juez + aviso a Telegram
  4. systemctl enable --now nbwatch-scrape.timer nbwatch-daily.timer nbwatch-web.service
     La interfaz web queda en http://<ip-del-lxc>:8080
EOF
