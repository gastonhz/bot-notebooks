"""Veredicto rápido: tras una alerta inmediata, el juez revisa solo ese producto y responde a la alerta.

Corre en segundo plano (no frena el scrape ni las alertas) y nunca descarta nada: solo informa.
main() espera a que terminen antes de salir, para que systemd no los corte.
"""
import json
from concurrent.futures import ThreadPoolExecutor

from . import notify
from .judge import call_claude
from .notify import esc
from .prefilter import all_marks, product_key, row_extra, spec_text
from .util import fmt_eur, log, squash, today_rome

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["parece bien", "dudoso", "sospechoso"]},
        "summary": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "aspect": {"type": "string"},
                    "status": {"type": "string", "enum": ["ok", "riesgo", "sin datos"]},
                    "detail": {"type": "string"},
                },
                "required": ["aspect", "status", "detail"],
            },
        },
    },
    "required": ["verdict", "summary", "checks"],
}
VERDICT_ICONS = {"parece bien": "✅", "dudoso": "⚠️", "sospechoso": "🚩"}
CHECK_ICONS = {"ok": "✅", "riesgo": "⚠️", "sin datos": "❔"}
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


def build_prompt(cfg, row, v, bn):
    extra = row_extra(row)
    d = cfg.dates
    product = {
        "title": row["title"], "mpn": row["mpn"], "store": row["store"], "source": row["source"], "url": row["url"],
        "price_total": row["price"], "base_price": row["base_price"], "shipping": row["shipping"],
        "availability": extra.get("availability") or extra.get("stock"), "delivery": extra.get("delivery"),
        "specs": squash(spec_text(row), 800) or None, "store_rating": extra.get("store_rating"),
        "store_reviews": extra.get("store_reviews"), "gpu": v.gpu,
        "cpu": f"{v.cpu} (nivel {v.cpu_tier[-1]})" if v.cpu else None,
        "marks": all_marks(v, bn) or None, "flags": v.flags or None,
        "buy_now_failed": bn.failed + [f"falta {m}" for m in bn.missing] if bn and not bn.ok else None,
    }
    product = {k: val for k, val in product.items() if val not in (None, [], {}, "")}
    return f"""# Rol

Sos un verificador rápido de ofertas de notebooks en tiendas italianas. Acaba de salir una alerta automática
(sin juez) por este producto y el comprador puede decidir en minutos. Revisá SOLO este producto, usando
WebSearch y WebFetch, y respondé en español rioplatense, conciso y concreto. No descartes: tu trabajo es informar.

El comprador compra en Italia y necesita la notebook entregada en un domicilio en Italia como tarde el
{d['deliver_by']} (hoy es {today_rome()}). Uso: gaming + IA local + Linux.

# Qué revisar (un elemento de checks por cada punto)

1. Vendedor y envío reales: quién vende y quién envía (en Amazon, "venduto e spedito da Amazon" o un
   vendedor tercero), y el plazo de entrega real frente a la fecha límite.
2. Reseñas recientes del vendedor y del producto, sobre todo quejas de "llegó otro producto" o un modelo distinto.
3. Coherencia de la ficha: cargador incluido, specs (GPU y su TGP, CPU, RAM, SSD, pantalla) que coincidan con el
   MPN, contenido de la caja.
4. Si el precio es razonable para esa GPU y CPU en el mercado italiano de hoy.
5. Qué significa cada marca del producto (⚠️ sospechoso = precio bajo para esa GPU; 🏷️ palabra = encontrada en
   título, specs o etiquetas de la tienda, p. ej. outlet, esposto, reso). Si no hay marcas, decilo.

verdict: "parece bien", "dudoso" o "sospechoso". summary: una o dos oraciones con lo más importante.

# Producto

{json.dumps(product, ensure_ascii=False, indent=1)}
"""


def render(row, out):
    lines = [f"🔎 <b>Veredicto rápido: {VERDICT_ICONS.get(out.get('verdict'), '❔')} {esc(out.get('verdict'))}</b>",
             f"<i>{esc(squash(row['title'], 90))} · {fmt_eur(row['price'])} · {esc(row['store'])}</i>",
             esc(out.get("summary"))]
    lines += [f"{CHECK_ICONS.get(c['status'], '❔')} <b>{esc(c['aspect'])}</b>: {esc(c['detail'])}"
              for c in out.get("checks") or []]
    return "\n".join(lines)


def _run(cfg, row, v, bn, reply_to):
    timeout = (cfg.get("instant_alerts") or {}).get("quick_judge_timeout_minutes", 8)
    try:
        text = render(row, call_claude(cfg, build_prompt(cfg, row, v, bn), schema=SCHEMA, timeout_minutes=timeout))
    except Exception as e:
        log.warning("veredicto rápido de %r falló: %s", row["title"][:60], e)
        text = (f"🔎 <b>Veredicto rápido</b>: no salió ({esc(squash(str(e), 200))}).\n"
                f"<i>{esc(squash(row['title'], 90))} · {fmt_eur(row['price'])}</i>\nNo se reintenta: verificá a mano.")
    try:
        notify.send(cfg, text, alert=True, reply_to=reply_to)
    except Exception as e:
        log.error("no se pudo mandar el veredicto rápido: %s", e)
