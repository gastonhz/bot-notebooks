"""Veredicto rápido: tras una alerta inmediata, el juez revisa solo ese producto y responde a la alerta.

Usa judge_prompt.md (su sección "Modo veredicto rápido") y el mismo formato que el juez diario, con
"evitar" además de "comprar" y "considerar". Corre en segundo plano (no frena el scrape ni las alertas)
y nunca descarta nada: solo informa. main() espera a que terminen antes de salir, para que systemd no
los corte.
"""
import copy
from concurrent.futures import ThreadPoolExecutor

from . import notify
from .judge import SCHEMA as JUDGE_SCHEMA
from .judge import build_prompt, call_claude, candidate
from .notify import esc
from .prefilter import product_key
from .util import fmt_eur, log, squash

SCHEMA = copy.deepcopy(JUDGE_SCHEMA)
SCHEMA["properties"]["picks"]["items"]["properties"]["verdict"]["enum"].append("evitar")
VERDICTS = {"comprar": "🟢 COMPRAR", "considerar": "🟡 CONSIDERAR", "evitar": "🔴 EVITAR"}
MAX_PARALLEL = 2

_pool = None
_jobs = []


def claim(cfg, db, row) -> bool:
    """¿Hay que lanzarlo? Uno solo por producto, tienda y precio, aunque la alerta se repita."""
    if not (cfg.get("instant_alerts") or {}).get("quick_judge"):
        return False
    ref = f"{product_key(row)}|{row['price']:.2f}"
    if db.alert_sent("quick_judge", ref):
        return False
    db.mark_alert("quick_judge", ref, row["price"])
    return True


def launch(cfg, row, v, bn, reply_to):
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="veredicto")
    _jobs.append(_pool.submit(_run, cfg, dict(row), v, bn, reply_to))


def wait():
    pending = sum(not j.done() for j in _jobs)
    if pending:
        log.info("esperando %d veredicto(s) rápido(s) en curso…", pending)
    for j in _jobs:
        j.result()  # _run no lanza excepciones
    _jobs.clear()


def render(row, out):
    product = f"<i>{esc(squash(row['title'], 90))} · {fmt_eur(row['price'])} · {esc(row['store'])}</i>"
    pick = next((p for p in out.get("picks") or [] if p["id"] == row["id"]), None)
    if pick is None:
        lines = ["🔎 <b>Veredicto rápido: sin conclusión</b>", product, esc(out.get("headline"))]
    else:
        lines = [f"🔎 <b>Veredicto rápido: {VERDICTS.get(pick['verdict'], esc(pick['verdict']))}</b>", product,
                 f"✅ {esc(pick['reason'])}" if pick["verdict"] != "evitar" else f"⛔ {esc(pick['reason'])}"]
        lines += [f"⚠️ {esc(r)}" for r in pick.get("risks") or []]
    if out.get("notes"):
        lines.append(f"📝 {esc(out['notes'])}")
    return "\n".join(lines)


def _run(cfg, row, v, bn, reply_to):
    timeout = (cfg.get("instant_alerts") or {}).get("quick_judge_timeout_minutes", 8)
    try:
        prompt = build_prompt(cfg, [candidate(row, v, bn)], "quick", [])
        text = render(row, call_claude(cfg, prompt, schema=SCHEMA, timeout_minutes=timeout))
    except Exception as e:
        log.warning("veredicto rápido de %r falló: %s", row["title"][:60], e)
        text = (f"🔎 <b>Veredicto rápido</b>: no salió ({esc(squash(str(e), 200))}).\n"
                f"<i>{esc(squash(row['title'], 90))} · {fmt_eur(row['price'])}</i>\nNo se reintenta: verificá a mano.")
    try:
        notify.send(cfg, text, alert=True, reply_to=reply_to)
    except Exception as e:
        log.error("no se pudo mandar el veredicto rápido: %s", e)
