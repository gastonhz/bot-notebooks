# nbwatch: vigilante de ofertas de notebooks gamer (Italia)

Vigila tiendas italianas, filtra por reglas (GPU, precio, tienda) y una vez por día le pide a
Claude Code (`claude -p`, con tu suscripción) que juzgue si algo vale la pena. Si vale, avisa por
Telegram; si no, silencio. Desde la fecha de corte avisa todos los días con la mejor opción.

## Cómo funciona

```
cada ~4 h   nbwatch scrape ──► fuentes ──► SQLite (historial de precios)
                                   │
                                   ├─► alerta inmediata: vuelve la referencia D73SXEA, aparece una
                                   │   GPU con >8 GB de VRAM barata o algo cumple buy_now ("🟢 COMPRAR YA")
                                   │   └─► veredicto rápido en segundo plano (claude -p, solo ese producto)
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

**Prefiltro** (reglas fijas, en `criteria.yaml`): GPU aceptada (5060 o superior; 5050/4060/4070 entran
marcadas para penalizar), CPU por niveles (los de gama baja se descartan, los de nivel 3 entran marcados
y el no identificado pasa con aviso), precio total entre `min_sane` y el tope duro, disponible, tienda
no bloqueada, sin "ricondizionato" y similares.
Las tiendas chicas entran si aceptan Visa/Mastercard y tienen rating y opiniones suficientes,
cuando el comparador muestra esos datos. Los vendedores "Marketplace" de Amazon se tratan como
tienda chica.

**Marcas** (no descartan; se ven en los avisos y le llegan al juez):
- `⚠️ sospechoso`: precio por debajo de `min_sane` o de `price.suspicious_below` para esa GPU, con
  `suspicious_action: flag`. Con `discard` (el valor por defecto) se descarta.
- `🏷️ palabra`: una de `flag_keywords` en el título, las specs o las etiquetas de la tienda (el
  "OUTLET ONLINE" de Unieuro es una etiqueta, no parte del título). `exclude_keywords` tiene prioridad.
  La búsqueda es por subcadena, así que "reso" también aparece dentro de "compreso".
- `🟢 COMPRAR YA`: cumple todas las condiciones de `buy_now`: GPU, nivel de CPU, RAM, almacenamiento,
  precio, tienda confiable sin marketplace (lo que llega por Idealo cuenta como vendedor sin confirmar)
  y entrega estimada antes de `deliver_by`. Si falta un dato no se marca, y la alerta dice cuál faltó.
  Además, dispara la alerta inmediata por sí sola.

**Veredicto rápido** (`instant_alerts.quick_judge`): después de cada alerta inmediata, `claude -p`
revisa solo ese producto en segundo plano: vendedor y envío reales, reseñas recientes, coherencia de
la ficha, precio y marcas. La respuesta llega como reply a la alerta. Hay uno solo por producto,
tienda y precio. Si falla o pasa `quick_judge_timeout_minutes`, llega un aviso corto y no se reintenta.

**Juez**: recibe los candidatos (deduplicados, con historial de precio, señales del prefiltro, marcas
y si cumplen `buy_now`), la referencia, el plan B, las otras opciones conocidas y lo ya notificado.
Puede usar WebSearch y WebFetch para verificar specs por MPN (TGP, slots, panel, cargador) y devuelve
JSON validado por esquema. Los links y precios del mensaje salen de la base, no del modelo. Desde
`dates.decide_by` el aviso diario pasa a "⏰ Decidí hoy": siempre trae la mejor opción disponible y,
como respaldo, la opción conocida cuya nota empieza con "Plan C".

`criteria.yaml` se valida al arrancar cada corrida: un valor inválido corta ahí con un mensaje claro
(y el aviso 💥), no a mitad de camino.

## Archivos

- `criteria.yaml`: tope de precio, fechas, GPUs, tiendas, búsquedas, referencias. Se relee en cada corrida.
- `judge_prompt.md`: criterios blandos en texto libre para el juez (TGP, slim, Linux, trampas de nombres).
- `nbwatch/`: el código. `sources/` tiene un módulo por fuente; `web.py` y `web.html`, la interfaz web.
- `deploy/`: `install.sh`, units de systemd, el atajo `nbwatch` y el ejemplo de `/etc/nbwatch.env`.

En el LXC, `criteria.yaml` y `judge_prompt.md` viven en `/var/lib/nbwatch/` (los del repo son la
versión inicial), para que la interfaz web los pueda escribir.

## Interfaz web

`http://<ip-del-lxc>:8080`, con el usuario y la clave de `NBWATCH_WEB_USER` / `NBWATCH_WEB_PASSWORD`
en `/etc/nbwatch.env`. Sirve para:

- **Editar** los dos archivos. Al guardar valida: el YAML, las claves que el código necesita, las
  regex de CPU y los nombres de fuentes. Si algo está mal no guarda, así una edición rota no
  hace fallar la próxima corrida.
- **Vista previa**: con el borrador muestra qué candidatos entrarían y cuáles saldrían frente a lo
  guardado (y por qué salen), más el prompt exacto que recibiría el juez. Usa lo que ya está en la
  base: no scrapea ni llama a claude.
- **Correr ahora**: un scrape, el juez de prueba (`judge --dry-run`: llama a claude y muestra el mensaje
  sin mandarlo) o el diario completo. Las corridas usan lo guardado, toman el mismo lock que los
  timers y su salida queda en `/var/lib/nbwatch/web-run.log`.

No hace falta reiniciar nada al guardar: cada corrida relee los archivos. Cada guardado deja la
versión anterior en `/var/lib/nbwatch/backups/` (se conservan las últimas 30 de cada archivo).
Va por HTTP plano, así que conviene usarla solo dentro de la LAN. Si reiniciás `nbwatch-web`, se
corta la corrida manual que esté en curso.

Para probarla fuera del LXC: `NBWATCH_WEB_USER=yo NBWATCH_WEB_PASSWORD=algo venv/bin/python -m nbwatch web`
(escucha en `127.0.0.1:8080`; `--host 0.0.0.0` para abrirla a la red).

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
   No pisa `/var/lib/nbwatch/criteria.yaml` ni `judge_prompt.md` si los editaste (la versión del repo
   queda al lado como `.new`). Si venís de una instalación donde estaban en `/opt/nbwatch`, los mueve
   y agrega las variables nuevas a `/etc/nbwatch.env`.

3. **Bot de Telegram**: en Telegram, hablale a @BotFather → `/newbot` → copiá el token a
   `TELEGRAM_BOT_TOKEN` en `/etc/nbwatch.env`. Mandale cualquier mensaje a tu bot y corré:
   ```bash
   nbwatch telegram-chat-id      # imprime TELEGRAM_CHAT_ID=... para pegar en /etc/nbwatch.env
   ```
   Opcional, para que las alertas inmediatas suenen distinto del resumen diario (con
   `instant_alerts.priority: high`): creá un segundo bot y poné su token en `TELEGRAM_ALERT_BOT_TOKEN`
   (el chat_id es el mismo), o mandalas a otro chat con `TELEGRAM_ALERT_CHAT_ID`. En Telegram le ponés
   a ese chat su propio sonido. `nbwatch test-notify` prueba los dos canales.

4. **Token de Claude** (suscripción, sin API key): en tu máquina, con el navegador a mano:
   ```bash
   claude setup-token
   ```
   Pegá el token en `CLAUDE_CODE_OAUTH_TOKEN` en `/etc/nbwatch.env`.

5. **Interfaz web**: poné un usuario y una clave en `NBWATCH_WEB_USER` y `NBWATCH_WEB_PASSWORD`
   en `/etc/nbwatch.env`.

6. **Probar** (ver la sección siguiente) y **activar los timers y la web**:
   ```bash
   systemctl enable --now nbwatch-scrape.timer nbwatch-daily.timer nbwatch-web.service
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
nbwatch judge --mode post --dry-run  #    simular el modo "después de la fecha de corte"
```

Sin el LXC (en cualquier Linux con escritorio), desde la carpeta del proyecto:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/playwright install chromium
venv/bin/python -m nbwatch scrape --dry-run    # usa ./state y abre ventanas de Chromium visibles
```

## Operación

- Logs: `journalctl -u nbwatch-scrape -u nbwatch-daily -u nbwatch-web -e`
- Estado: `nbwatch status`
- La base es `/var/lib/nbwatch/nbwatch.sqlite3`. Se puede consultar con `sqlite3`, por ejemplo
  `select title, store, price, last_seen from listings order by last_seen desc limit 20;`
- Para cambiar el tope, las fechas, las tiendas o los criterios del juez, usá la interfaz web (o editá
  `/var/lib/nbwatch/criteria.yaml` y `judge_prompt.md`). Los cambios rigen desde la próxima corrida.

Mensajes que pueden llegar:

| Mensaje | Cuándo |
|---|---|
| 🟢 / 🟡 nbwatch — … | Juicio diario con algo para mirar (🟢 = "comprar", 🟡 = "considerar") |
| ℹ️ nbwatch — … | Juicio diario sin picks (desde la fecha de corte llega igual, con la mejor opción) |
| ⏰ Decidí hoy | Juicio diario desde `dates.decide_by`: mejor opción disponible + Plan C |
| 🚨 ¡apareció la referencia! | D73SXEA disponible en cualquier tienda a ≤ `instant_alert_max_price` |
| 🟢 COMPRAR YA — RTX … | Algo cumple todas las condiciones de `buy_now` |
| 🚀 RTX 5070 Ti/5080 a … | GPU con >8 GB de VRAM a ≤ `instant_alerts.high_vram_max_price` |
| 🔎 Veredicto rápido | Respuesta a una alerta inmediata, con la revisión de ese producto |
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

En la fecha de fin (`dates.end`) el sistema deja de scrapear y juzgar, y avisa una vez. Para apagarlo del todo:

```bash
systemctl disable --now nbwatch-scrape.timer nbwatch-daily.timer nbwatch-web.service
```
