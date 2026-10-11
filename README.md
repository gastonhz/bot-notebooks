# nbwatch: vigilante de ofertas de notebooks gamer (Italia)

Vigila tiendas italianas, filtra por reglas (GPU, precio, tienda) y una vez por día le pide a
Claude Code (`claude -p`, con tu suscripción) que juzgue si algo vale la pena. Si vale, avisa por
Telegram; si no, silencio. Desde la fecha de corte avisa todos los días con la mejor opción.

## Cómo funciona

```
cada ~4 h   nbwatch scrape ──► fuentes ──► SQLite (historial de precios)
                                   │
                                   ├─► alerta inmediata: vuelve la referencia D73SXEA,
                                   │   o aparece una GPU con >8 GB de VRAM barata
                                   └─► alerta de salud: una fuente falló N veces seguidas
09:00 (AR)  nbwatch daily  ──► scrape + prefiltro + claude -p ──► Telegram (o silencio)
```

| Fuente | Cómo | Qué aporta |
|---|---|---|
| **Trovaprezzi** | Chromium visible bajo Xvfb (DataDome bloquea headless y HTTP) | Comparador principal: Amazon.it, MediaWorld, Unieuro, Euronics y tiendas chicas, con envío, rating y medios de pago de cada tienda |
| **Idealo** | Chromium (JSON embebido en la página) | Segundo comparador: oferta más barata por producto |
| **Unieuro** | HTTP al índice Algolia de su buscador | MPN exacto, stock y fechas de entrega |
| **HP Store IT** | HTTP | Ficha HP oficial (MPN, cupones, "cargador no incluido") |

Amazon, MediaWorld y Euronics bloquean el acceso directo (Amazon a propósito, los otros dos con
Cloudflare Turnstile), pero sus ofertas llegan a través de los comparadores. Los sitios cambian:
si una fuente deja de devolver resultados o muestra un captcha, llega un aviso por Telegram
(y otro cuando se recupera). Ninguna fuente falla en silencio.

**Prefiltro** (reglas fijas, en `criteria.yaml`): GPU aceptada (5060 o superior), precio total
entre `min_sane` y el tope duro, disponible, tienda no bloqueada, sin "ricondizionato" y similares.
Las tiendas chicas entran si aceptan Visa/Mastercard y tienen rating y opiniones suficientes,
cuando el comparador muestra esos datos. Los vendedores "Marketplace" de Amazon se tratan como
tienda chica.

**Juez**: recibe los candidatos (deduplicados, con historial de precio y señales del prefiltro),
la referencia, el plan B y lo ya notificado. Puede usar WebSearch y WebFetch para verificar specs
por MPN (TGP, slots, panel) y devuelve JSON validado por esquema. Los links y precios del mensaje
salen de la base, no del modelo.

## Archivos

- `criteria.yaml`: tope de precio, fechas, GPUs, tiendas, búsquedas, referencias. Se relee en cada corrida.
- `judge_prompt.md`: criterios blandos en texto libre para el juez (TGP, slim, Linux, trampas de nombres).
- `nbwatch/`: el código. `sources/` tiene un módulo por fuente.
- `deploy/`: `install.sh`, units de systemd, el atajo `nbwatch` y el ejemplo de `/etc/nbwatch.env`.

## Deploy en el LXC de Proxmox

1. **Crear el LXC**: Debian 12 o Ubuntu 24.04, unprivileged sirve. Recomendado: 2 GB de RAM
   (Chromium tiene picos de ~1 GB), 1 GB de swap y 8 GB de disco.

2. **Clonar e instalar** (como root dentro del LXC):
   ```bash
   apt-get update && apt-get install -y git
   git clone https://github.com/gastonhz/bot-notebooks.git /root/nbwatch-src
   cd /root/nbwatch-src && bash deploy/install.sh
   ```
   Instala python, venv, Xvfb, Chromium de Playwright con sus librerías, Claude Code para el
   usuario `nbwatch`, el atajo `/usr/local/bin/nbwatch` y los units de systemd.
   Para actualizar el código más adelante: `cd /root/nbwatch-src && git pull && bash deploy/install.sh`.
   No pisa `/opt/nbwatch/criteria.yaml` ni `judge_prompt.md` si los editaste ahí.

3. **Bot de Telegram**: en Telegram, hablale a @BotFather → `/newbot` → copiá el token a
   `TELEGRAM_BOT_TOKEN` en `/etc/nbwatch.env`. Mandale cualquier mensaje a tu bot y corré:
   ```bash
   nbwatch telegram-chat-id      # imprime TELEGRAM_CHAT_ID=... para pegar en /etc/nbwatch.env
   ```

4. **Token de Claude** (suscripción, sin API key): en tu máquina, con el navegador a mano:
   ```bash
   claude setup-token
   ```
   Pegá el token en `CLAUDE_CODE_OAUTH_TOKEN` en `/etc/nbwatch.env`.

5. **Probar** (ver la sección siguiente) y **activar los timers**:
   ```bash
   systemctl enable --now nbwatch-scrape.timer nbwatch-daily.timer
   systemctl list-timers 'nbwatch*'
   ```

Horarios: scrape cada ~4 h, con hasta 40 minutos de demora aleatoria (unas 6 visitas por día a cada
sitio). El juicio diario corre a las 09:00 hora de Argentina, configurable en
`deploy/systemd/nbwatch-daily.timer` (`systemctl daemon-reload` después de cambiarlo).

## Correrlo a mano / probar de punta a punta

Con el atajo, que usa el mismo usuario y entorno que los timers:

```bash
nbwatch test-notify                  # 1. mensaje de prueba a Telegram
nbwatch scrape --dry-run             # 2. recorre las fuentes y muestra qué pasa el prefiltro (no guarda ni avisa)
nbwatch scrape --source unieuro      #    una sola fuente, guardando
nbwatch scrape                       # 3. scrape real (guarda, alertas inmediatas y de salud)
nbwatch status                       # 4. salud de fuentes, candidatos actuales, último juicio
nbwatch judge --print-prompt | less  # 5. el prompt exacto que va a recibir el juez
nbwatch judge --dry-run              # 6. llama a claude e imprime el mensaje sin mandarlo
nbwatch daily --force-notify         # 7. end-to-end: scrape + juez + aviso a Telegram aunque el juez diga que no
nbwatch judge --mode post --dry-run  #    simular el modo "después del 10/11"
```

Sin el LXC (en cualquier Linux con escritorio), desde la carpeta del proyecto:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/playwright install chromium
venv/bin/python -m nbwatch scrape --dry-run    # usa ./state y abre ventanas de Chromium visibles
```

## Operación

- Logs: `journalctl -u nbwatch-scrape -u nbwatch-daily -e`
- Estado: `nbwatch status`
- La base es `/var/lib/nbwatch/nbwatch.sqlite3`. Se puede consultar con `sqlite3`, por ejemplo
  `select title, store, price, last_seen from listings order by last_seen desc limit 20;`
- Para cambiar el tope, las fechas o las tiendas, editá `/opt/nbwatch/criteria.yaml`; para los
  criterios del juez, `/opt/nbwatch/judge_prompt.md`. Los cambios rigen desde la próxima corrida.

Mensajes que pueden llegar:

| Mensaje | Cuándo |
|---|---|
| 🟢 / 🟡 nbwatch — … | Juicio diario con algo para mirar (🟢 = "comprar", 🟡 = "considerar") |
| ℹ️ nbwatch — … | Juicio diario sin picks (desde el 10/11 llega igual, con la mejor opción) |
| 🚨 ¡apareció la referencia! | D73SXEA disponible en cualquier tienda a ≤ `instant_alert_max_price` |
| 🚀 RTX 5070 Ti/5080 a … | GPU con >8 GB de VRAM a ≤ `instant_alerts.high_vram_max_price` |
| ⚠️ la fuente X falló | N corridas seguidas con bloqueo, 0 resultados o error |
| 💥 nbwatch … falló | La corrida entera rompió (máximo 1 aviso cada 12 h por el mismo error) |
| 💤 sigue vivo | Los lunes, si ese día no hubo aviso |

## Cuando una fuente se rompe

- **Bloqueo / captcha** (Trovaprezzi o Idealo): suele ser temporal y el perfil persistente del
  navegador ayuda. Si persiste, desactivá la fuente en `criteria.yaml` (`enabled: false`).
  Las otras siguen funcionando.
- **0 resultados / "¿cambió el HTML?"**: el sitio cambió su estructura. Cada parser está aislado
  en `nbwatch/sources/<fuente>.py`, con una función `parse_page`.
- **Unieuro 403**: rotaron la clave pública de Algolia. El docstring de `sources/unieuro.py` explica
  cómo sacar la nueva.
- **Juez**: si `claude -p` falla (token vencido, límite de uso), llega el aviso 💥. Para revisarlo,
  corré `nbwatch judge --dry-run`.

## Al terminar

El 21/11 (`dates.end`) el sistema deja de scrapear y juzgar, y avisa una vez. Para apagarlo del todo:

```bash
systemctl disable --now nbwatch-scrape.timer nbwatch-daily.timer
```
