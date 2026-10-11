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


def send(cfg, text, dry_run=False) -> bool:
    """Manda un mensaje HTML a Telegram. En dry-run o sin credenciales, lo imprime."""
    if dry_run or not (cfg.telegram_token and cfg.telegram_chat_id):
        if not dry_run:
            log.warning("Telegram no configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): imprimo el mensaje")
        print("----- mensaje -----\n" + text.strip() + "\n-------------------")
        return False
    for chunk in _chunks(text):
        telegram_call(cfg.telegram_token, "sendMessage", {
            "chat_id": cfg.telegram_chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
    return True
