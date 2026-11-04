import collections
import time

from . import notify, quickjudge
from .notify import esc
from .prefilter import all_marks, check_buy_now, evaluate
from .sources import SourceBlocked, SourceContext, SourceError, get_sources
from .util import fmt_eur, log, now_utc, squash, today_rome


def listing_row(l) -> dict:
    """Listing -> dict con las mismas claves que una fila de la DB (para evaluate())."""
    return {"title": l.title, "price": l.price, "available": l.available, "store": l.store,
            "source": l.source, "extra": l.extra, "mpn": l.mpn, "url": l.url, "key": l.key,
            "base_price": l.base_price, "shipping": l.shipping}


def safe_send(cfg, text, alert=False):
    try:
        return notify.send(cfg, text, alert=alert)
    except Exception as e:
        log.error("no se pudo mandar a Telegram: %s", e)


def update_health(cfg, db, name, error, count):
    h = db.health(name)
    if error is None:
        if h["alerted"]:
            safe_send(cfg, f"✅ <b>nbwatch</b>: la fuente <b>{name}</b> volvió a funcionar ({count} resultados).")
        db.set_health(name, consecutive_failures=0, last_ok=now_utc(), last_count=count, alerted=0, last_error=None)
        return
    fails = h["consecutive_failures"] + 1
    alerted = h["alerted"]
    if fails >= cfg["health"]["fail_threshold"] and not alerted:
        safe_send(cfg, (
            f"⚠️ <b>nbwatch</b>: la fuente <b>{name}</b> falló {fails} veces seguidas.\n"
            f"<code>{esc(squash(error, 600))}</code>\n"
            f"Último OK: {h['last_ok'] or 'nunca'}. Las demás fuentes siguen corriendo."
        ))
        alerted = 1
    db.set_health(name, consecutive_failures=fails, last_error=error, last_count=count, alerted=alerted)


def _alert_once(cfg, db, kind, row, header, v, bn):
    prev = db.alert_sent(kind, row["key"])
    if prev and prev["price"] is not None and row["price"] is not None and row["price"] >= prev["price"] - 0.5:
        return
    avail = {1: "disponible", 0: "no disponible"}.get(row["available"], "disponibilidad desconocida")
    lines = [header, "", esc(row["title"]),
             f"💶 <b>{fmt_eur(row['price'])}</b> total · {esc(row['store'])} · {avail} · vía {row['source']}"]
    if v.marks:
        lines.append(esc(" · ".join(v.marks)))
    if bn and bn.missing and not bn.failed:
        lines.append(f"ℹ️ Podría ser COMPRAR YA, pero falta: {esc(', '.join(bn.missing))}.")
    lines.append(f"🔗 <a href=\"{esc(row['url'])}\">ver oferta</a>")
    quick = quickjudge.claim(cfg, db, row)
    lines.append("<i>Alerta automática, sin pasar por el juez: verificá MPN, vendedor y plazo de entrega."
                 + (" El veredicto rápido llega como respuesta en unos minutos." if quick else "") + "</i>")
    msg_id = safe_send(cfg, "\n".join(lines), alert=True)
    db.mark_alert(kind, row["key"], row["price"])
    if quick:
        quickjudge.launch(cfg, row, v, bn, msg_id)


def check_instant_alerts(cfg, db, since_ts):
    ref = cfg["reference"]
    ref_mpn = ref["mpn"].lower()
    high_vram = [g.lower() for g in cfg["gpus"]["high_vram"]]
    vram_cap = cfg.get("instant_alerts", {}).get("high_vram_max_price")
    for row in db.listings_since(since_ts):
        v = evaluate(row, cfg)
        bn = check_buy_now(row, v, cfg) if v.passed else None
        haystack = f"{row['title']} {row['mpn'] or ''} {row['url'] or ''}".lower()
        if ref_mpn in haystack:
            if row["available"] != 0 and row["price"] and row["price"] <= ref["instant_alert_max_price"]:
                _alert_once(cfg, db, "reference", row, f"🚨 <b>nbwatch: ¡apareció la referencia {ref['mpn']}!</b>",
                            v, bn)
            continue
        if bn and bn.ok:  # dispara aunque no cumpla las otras condiciones
            _alert_once(cfg, db, "buy_now", row,
                        f"🟢 <b>nbwatch: COMPRAR YA</b> — RTX {v.gpu} a {fmt_eur(row['price'])}", v, bn)
        elif vram_cap and v.passed and v.gpu and v.gpu.lower() in high_vram and row["price"] <= vram_cap:
            _alert_once(cfg, db, "high_vram", row, f"🚀 <b>nbwatch: RTX {v.gpu} a {fmt_eur(row['price'])}</b>",
                        v, bn)


def print_dry_run(cfg, name, listings):
    rejected = collections.Counter()
    passed = []
    for l in listings:
        v = evaluate(listing_row(l), cfg)
        if v.passed:
            passed.append((l, v))
        else:
            rejected[v.reasons[0].split(" (")[0]] += 1
    print(f"\n=== {name}: {len(listings)} listados, {len(passed)} pasan el prefiltro")
    for l, v in sorted(passed, key=lambda x: x[0].price or 0):
        cpu = f"{v.cpu} ({v.cpu_tier[-1]})" if v.cpu else "?"
        marks = all_marks(v, check_buy_now(listing_row(l), v, cfg))
        print(f"  ✔ {fmt_eur(l.price):>12} | {squash(l.store, 18):18} | {v.gpu:7} | {squash(cpu, 16):16} | {squash(l.title, 80)}"
              + (f"  {' · '.join(marks)}" if marks else "") + (f"  [{', '.join(v.flags)}]" if v.flags else ""))
    for reason, n in rejected.most_common(8):
        print(f"  ✘ {n:3} × {reason}")


def run_scrape(cfg, db, only=None, dry_run=False):
    if today_rome() > cfg.dates["end"]:
        log.info("pasó la fecha de fin (%s): no se scrapea más", cfg.dates["end"])
        return
    started = now_utc()
    sources = get_sources()
    names = only or cfg.enabled_sources()
    ctx = SourceContext(cfg)
    try:
        for name in names:
            t0 = time.time()
            error = None
            try:
                listings = sources[name](ctx)
            except SourceError as e:
                listings = e.partial
                error = f"{'BLOQUEO' if isinstance(e, SourceBlocked) else 'ERROR'}: {e}"
            except Exception as e:
                log.exception("fuente %s rompió", name)
                listings, error = [], f"EXCEPCIÓN: {type(e).__name__}: {e}"
            log.info("%s: %d listados en %.0fs%s", name, len(listings), time.time() - t0, f" — {error}" if error else "")
            if dry_run:
                print_dry_run(cfg, name, listings)
                if error:
                    print(f"  ⚠ {error}")
                continue
            for l in listings:
                db.upsert_listing(l)
            update_health(cfg, db, name, error, len(listings))
    finally:
        ctx.close()
    if not dry_run:
        check_instant_alerts(cfg, db, started)
        db.kv_set("last_scrape", now_utc())
