#!/bin/sh
# /usr/local/bin/nbwatch: corre nbwatch a mano con el mismo entorno y usuario que los timers.
set -a
. /etc/nbwatch.env
set +a
cd /opt/nbwatch
if [ "$(id -u)" = 0 ]; then
  exec runuser -u nbwatch -- /opt/nbwatch/venv/bin/python -m nbwatch "$@"
fi
exec /opt/nbwatch/venv/bin/python -m nbwatch "$@"
