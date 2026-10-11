import json
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from . import notify
from .notify import esc
from .prefilter import evaluate, extract_mpns, row_extra, spec_text
from .util import fmt_eur, log, squash, today_rome

SCHEMA = {
    "type": "object",
    "properties": {
        "notify": {"type": "boolean"},
        "headline": {"type": "string"},
        "picks": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["comprar", "considerar"]},
                    "reason": {"type": "string"},
                    "risks": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "verdict", "reason", "risks"],
            },
        },
        "best_current": {
            "type": ["object", "null"],
            "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["id", "reason"],
        },
        "notes": {"type": "string"},
    },
    "required": ["notify", "headline", "picks", "best_current", "notes"],
}

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4, "sabado": 5,
            "sábado": 5, "domingo": 6}


class JudgeError(Exception):
    pass


def _iso_hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def collect_candidates(cfg, db):
    """Listados recientes que pasan el prefiltro, sin duplicados (mismo producto + misma tienda)."""
    rows = db.listings_since(_iso_hours_ago(cfg["judge"].get("lookback_hours", 30)))
    last = db.last_judgment()
    last_ts = last["ts"] if last else None
    best = {}
    for row in rows:
        v = evaluate(row, cfg)
        if not v.passed:
            continue
        extra = row_extra(row)
        mpns = [row["mpn"]] if row["mpn"] else extract_mpns(f"{row['title']} {spec_text(row)}")
        cand = {
            "id": row["id"],
            "title": row["title"],
            "mpn": mpns[0] if mpns else None,
            "gpu": v.gpu,
            "store": row["store"],
            "store_type": "chica" if v.small_store else "grande",
            "store_rating": extra.get("store_rating"),
            "store_reviews": extra.get("store_reviews"),
            "review_service": extra.get("review_service"),
            "payments": extra.get("payments") or None,
            "price_total": row["price"],
            "base_price": row["base_price"] if row["base_price"] != row["price"] else None,
            "shipping": row["shipping"],
            "availability": extra.get("availability") or extra.get("stock"),
            "delivery": extra.get("delivery"),
            "coupon": extra.get("coupon"),
            "specs": extra.get("specs") or extra.get("desc") or None,
            "flags": v.flags or None,
            "source": row["source"],
            "first_seen": row["first_seen"][:10],
            "new_since_last_judgment": last_ts is None or row["first_seen"] > last_ts,
            "prev_price": row["prev_price"],
            "min_price_seen": row["min_price"] if row["min_price"] != row["price"] else None,
        }
        cand = {k: val for k, val in cand.items() if val not in (None, [], {}, "")}
        group = (cand.get("mpn") or re.sub(r"\W+", " ", row["title"].lower())[:70], (row["store"] or "").lower())
        if group not in best or row["price"] < best[group]["price_total"]:
            best[group] = cand
    cands = sorted(best.values(), key=lambda c: c["price_total"])
    return cands[: cfg["judge"].get("max_candidates", 120)]


def notified_history(db, days=10):
    out = []
    for j in db.recent_judgments(_iso_hours_ago(days * 24)):
        if j["notified"]:
            out += json.loads(j["picks"] or "[]")
    return out


def build_prompt(cfg, cands, mode, history):
    d, pc, ref, pb = cfg.dates, cfg["price"], cfg["reference"], cfg["plan_b"]
    today = today_rome()
    days_left = (d["deliver_by"] - today).days
    if mode == "pre":
        rules = (
            f"MODO PRE-CORTE (hasta el {d['cutoff']}): la pregunta es \"¿hay algo que supere a la referencia?\" "
            "(precio/specs comparables o mejores, contando el valor de la RAM). notify=true solo si hay al menos un "
            "pick 'comprar', o un 'considerar' muy interesante, que no se haya notificado ya (o que haya bajado de "
            "precio). Si no, notify=false: silencio. Igual completá best_current."
        )
    else:
        rules = (
            f"MODO POST-CORTE (desde el {d['cutoff']}): la pregunta es \"¿cuál es la mejor opción disponible que llega "
            f"a tiempo?\". El aviso sale siempre (notify=true) y best_current es obligatorio si hay algo aceptable. "
            f"Sé estricto con los plazos: quedan {days_left} días para la entrega límite; descartá lo que "
            "probablemente no llegue (preventa, 'su ordinazione', plazos largos, tiendas lentas)."
        )
    hist = "\n".join(
        f"- {h.get('date')}: [{h.get('verdict')}] {h.get('title')} — {h.get('store')} — {fmt_eur(h.get('price'))}"
        for h in history
    ) or "- (nada)"
    cand_json = "[\n" + ",\n".join(json.dumps(c, ensure_ascii=False) for c in cands) + "\n]"
    return f"""{cfg.judge_prompt_path.read_text(encoding='utf-8')}

# Datos de hoy

- Fecha (Italia): {today.isoformat()}
- Entrega límite en el domicilio en Italia: {d['deliver_by']} (quedan {days_left} días)
- Tope de precio objetivo: {pc['target']} € total (tope duro {pc['hard_cap']} €; {pc['hard_cap_high_vram']} € con más de 8 GB de VRAM)
- {rules}

# Referencia (agotada)

{ref['name']} — MPN {ref['mpn']} — {ref['specs']} — {fmt_eur(ref['price'])} en {ref['where']}.

# Plan B

{pb['name']} — MPN {pb['mpn']} — {pb['specs']} — {fmt_eur(pb['price'])} en {pb['where']}.

# Ya notificado en los últimos días (no repetir salvo baja de precio o cambio relevante)

{hist}

# Candidatos ({len(cands)}, ya prefiltrados por reglas; precios en EUR, total con envío)

Campos: id (usalo en picks/best_current), store_type (grande/chica), store_rating (sobre 5) y
store_reviews (del comparador), payments, availability/delivery (si la fuente los da), flags
(señales del prefiltro), new_since_last_judgment, prev_price/min_price_seen (historial).

{cand_json}
"""


def call_claude(cfg, prompt) -> dict:
    j = cfg["judge"]
    tools = ",".join(j.get("tools") or [])
    cmd = [cfg.claude_bin, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", tools, "--no-session-persistence", "--strict-mcp-config"]
    if tools:
        cmd += ["--allowedTools", tools]
    if j.get("model"):
        cmd += ["--model", j["model"]]
    log.info("llamando a claude -p (%s, herramientas: %s)…", j.get("model") or "modelo por defecto", tools or "ninguna")
    try:
        # directorio vacío: el juez no tiene nada local para leer
        with tempfile.TemporaryDirectory(prefix="nbwatch-judge-") as tmp:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=j.get("timeout_minutes", 20) * 60, cwd=tmp)
    except FileNotFoundError:
        raise JudgeError(f"no encuentro el binario de Claude Code ({cfg.claude_bin}); configurá CLAUDE_BIN")
    except subprocess.TimeoutExpired:
        raise JudgeError(f"claude -p no terminó en {j.get('timeout_minutes', 20)} minutos")
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise JudgeError(f"salida de claude no es JSON (exit {proc.returncode}): "
                         f"{proc.stdout[-400:]!r} {proc.stderr[-400:]!r}")
    if res.get("is_error") or res.get("subtype") != "success":
        raise JudgeError(f"claude devolvió error: subtype={res.get('subtype')} result={str(res.get('result'))[:400]}")
    out = res.get("structured_output")
    if out is None:
        try:
            out = json.loads(res.get("result") or "")
        except json.JSONDecodeError:
            raise JudgeError(f"claude no devolvió JSON estructurado: {str(res.get('result'))[:400]}")
    log.info("juez listo en %.0fs, %s turnos", (res.get("duration_ms") or 0) / 1000, res.get("num_turns"))
    return out


def _offer_line(row):
    extra = row_extra(row)
    bits = [f"💶 <b>{fmt_eur(row['price'])}</b>", esc(row["store"]), f"vía {row['source']}"]
    if extra.get("delivery"):
        bits.append(f"entrega {esc(extra['delivery'])}")
    if extra.get("coupon"):
        bits.append(f"cupón {esc(extra['coupon'])}")
    return " · ".join(bits)


def render_message(cfg, out, mode, rows):
    today = today_rome()
    d = cfg.dates
    picks = [p for p in out.get("picks", []) if p["id"] in rows]
    icon = "🟢" if any(p["verdict"] == "comprar" for p in picks) else ("🟡" if picks else "ℹ️")
    days_left = (d["deliver_by"] - today).days
    mode_txt = "¿supera a la referencia?" if mode == "pre" else "mejor opción que llega a tiempo"
    lines = [
        f"{icon} <b>nbwatch — {esc(out.get('headline'))}</b>",
        f"<i>{today:%d/%m} · {mode_txt} · entrega límite {d['deliver_by']:%d/%m} (quedan {days_left} días)</i>",
    ]
    for i, p in enumerate(picks, 1):
        row = rows[p["id"]]
        lines += ["", f"<b>{i}. {p['verdict'].upper()}</b> · {esc(squash(row['title'], 150))}"]
        lines.append(_offer_line(row))
        lines.append(f"✅ {esc(p['reason'])}")
        lines += [f"⚠️ {esc(r)}" for r in p.get("risks") or []]
        lines.append(f"🔗 <a href=\"{esc(row['url'])}\">ver oferta</a>")
    bc = out.get("best_current")
    if bc and bc.get("id") in rows:
        pick_ids = [p["id"] for p in picks]
        if bc["id"] in pick_ids:
            lines += ["", f"🏆 Mejor opción actual: la n.º {pick_ids.index(bc['id']) + 1}. {esc(bc['reason'])}"]
        else:
            row = rows[bc["id"]]
            lines += ["", f"🏆 <b>Mejor opción actual</b>: {esc(squash(row['title'], 150))}"]
            lines.append(_offer_line(row))
            lines += [f"✅ {esc(bc['reason'])}", f"🔗 <a href=\"{esc(row['url'])}\">ver oferta</a>"]
    elif mode == "post":
        lines += ["", "🏆 Mejor opción actual: ninguna aceptable hoy."]
    if out.get("notes"):
        lines += ["", f"📝 {esc(out['notes'])}"]
    return "\n".join(lines)


def _heartbeat_due(cfg):
    day = cfg["judge"].get("heartbeat_weekday")
    return day is not None and WEEKDAYS.get(str(day).lower()) == today_rome().weekday()


def heartbeat(cfg, db, n_cands, out=None):
    health = db.all_health()
    bad = [h["source"] for h in health if h["consecutive_failures"]]
    best = ""
    if out and out.get("best_current"):
        row = db.listings_by_ids([out["best_current"]["id"]]).get(out["best_current"]["id"])
        if row:
            best = f"\nMejor opción hoy: {esc(squash(row['title'], 90))} — {fmt_eur(row['price'])} ({esc(row['store'])})"
    notify.send(cfg, (
        f"💤 <b>nbwatch sigue vivo</b>: nada que avisar. {n_cands} candidatos pasan los filtros.{best}\n"
        f"Fuentes: {'todas OK' if not bad else 'fallando: ' + ', '.join(bad)}."
    ))


def run_judge(cfg, db, dry_run=False, print_prompt=False, force_notify=False, mode=None):
    today = today_rome()
    if today > cfg.dates["end"]:
        if not db.kv_get("end_notice") and not dry_run:
            notify.send(cfg, "🏁 <b>nbwatch</b>: pasó la fecha de fin. Desactivá los timers: "
                             "<code>systemctl disable --now nbwatch-scrape.timer nbwatch-daily.timer</code>")
            db.kv_set("end_notice", "1")
        log.info("pasó la fecha de fin (%s): no se juzga más", cfg.dates["end"])
        return
    mode = mode or ("post" if today >= cfg.dates["cutoff"] else "pre")
    cands = collect_candidates(cfg, db)
    prompt = build_prompt(cfg, cands, mode, notified_history(db))
    if print_prompt:
        print(prompt)
        return
    log.info("modo %s, %d candidatos", mode, len(cands))

    if not cands:
        if mode == "post" or force_notify:
            notify.send(cfg, "⚠️ <b>nbwatch</b>: hoy ningún listado pasa los filtros. Revisá el estado de las "
                             "fuentes (<code>nbwatch status</code>) o los topes en criteria.yaml.", dry_run=dry_run)
        elif _heartbeat_due(cfg) and not dry_run:
            heartbeat(cfg, db, 0)
        return

    out = call_claude(cfg, prompt)
    rows = db.listings_by_ids({p["id"] for p in out.get("picks", [])} |
                              ({out["best_current"]["id"]} if out.get("best_current") else set()))
    valid_ids = {c["id"] for c in cands}
    rows = {k: v for k, v in rows.items() if k in valid_ids}
    should_send = bool(out.get("notify")) or mode == "post"
    text = render_message(cfg, out, mode, rows)
    if force_notify and not should_send:
        text = "🧪 <b>[PRUEBA]</b> el juez no habría avisado hoy; este es su análisis:\n\n" + text
    picks_hist = [{"date": today.isoformat(), "id": p["id"], "verdict": p["verdict"], "title": rows[p["id"]]["title"],
                   "store": rows[p["id"]]["store"], "price": rows[p["id"]]["price"]}
                  for p in out.get("picks", []) if p["id"] in rows]

    if dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        notify.send(cfg, text, dry_run=True)
        return
    if should_send or force_notify:
        notify.send(cfg, text)
    else:
        log.info("juez: sin aviso — %s", out.get("headline"))
        if _heartbeat_due(cfg):
            heartbeat(cfg, db, len(cands), out)
    db.save_judgment(mode, should_send, out, picks_hist)
