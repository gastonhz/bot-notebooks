import html
import json
import urllib.parse
import urllib.request

from .util import log

LIMIT = 4000  # Telegram corta en 4096


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def _chunks(text):
    """Parte el mensaje en bloques < LIMIT respetando líneas (no corta tags HTML a la mitad)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > LIMIT and chunk:
            yield chunk
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        yield chunk


def telegram_call(token, method, params=None, timeout=30):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body}")
    return body["result"]


def alert_channel(cfg) -> bool:
    """¿Las alertas inmediatas van a un canal aparte? (instant_alerts.priority: high + TELEGRAM_ALERT_*)"""
    high = (cfg.get("instant_alerts") or {}).get("priority") == "high"
    return high and bool(cfg.telegram_alert_token or cfg.telegram_alert_chat_id)


def send(cfg, text, dry_run=False, alert=False, reply_to=None) -> int | None:
    """Manda un mensaje HTML a Telegram y devuelve el message_id del primer bloque. En dry-run o sin
    credenciales, lo imprime y devuelve None.

    alert=True: alerta inmediata. Si hay canal de alertas (ver alert_channel) va ahí, con sonido: otro bot
    (TELEGRAM_ALERT_BOT_TOKEN) u otro chat (TELEGRAM_ALERT_CHAT_ID) se pueden configurar con su propio
    sonido en Telegram. Si no, va al chat de siempre. reply_to: message_id al que responde (mismo canal).
    """
    token, chat_id = cfg.telegram_token, cfg.telegram_chat_id
    if alert and alert_channel(cfg):
        token, chat_id = cfg.telegram_alert_token or token, cfg.telegram_alert_chat_id or chat_id
    if dry_run or not (token and chat_id):
        if not dry_run:
            log.warning("Telegram no configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): imprimo el mensaje")
        label = ("alerta" if alert else "mensaje") + (f", respuesta a {reply_to}" if reply_to else "")
        print(f"----- {label} -----\n" + text.strip() + "\n-------------------")
        return None
    first = None
    for chunk in _chunks(text):
        params = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if alert:
            params["disable_notification"] = "false"
        if reply_to and first is None:
            params["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        msg = telegram_call(token, "sendMessage", params)
        first = first or msg["message_id"]
    return first
