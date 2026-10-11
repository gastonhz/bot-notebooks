import fcntl
import logging
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("nbwatch")

ROME = ZoneInfo("Europe/Rome")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_rome():
    return datetime.now(ROME).date()


def parse_eur(value):
    """'1.199,99 €' -> 1199.99, 'Sped. gratuita' -> None, 1632 -> 1632.0."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d[\d.\s]*(?:,\d{1,2})?", value)
    if not m:
        return None
    s = m.group(0).replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+\.\d{1,2}", s):
        pass  # formato inglés "1199.99"
    else:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def fmt_eur(value) -> str:
    if value is None:
        return "?"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def polite_sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def squash(text, limit=None) -> str:
    text = " ".join((text or "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@contextmanager
def file_lock(path, timeout_s=3600):
    """Evita que scrape y daily corran a la vez (comparten el perfil del navegador y la DB)."""
    f = open(path, "w")
    deadline = time.time() + timeout_s
    waited = False
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                f.close()
                raise TimeoutError(f"no se pudo tomar el lock {path} en {timeout_s}s")
            if not waited:
                log.info("otra corrida en curso, esperando el lock…")
                waited = True
            time.sleep(5)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
