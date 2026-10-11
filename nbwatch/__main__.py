import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import notify
from .config import Config
from .db import DB
from .judge import _iso_hours_ago, collect_candidates, run_judge
from .notify import esc, telegram_call
from .scrape import run_scrape
from .util import file_lock, fmt_eur, log, now_utc, squash


def cmd_scrape(cfg, db, args):
    run_scrape(cfg, db, only=args.source, dry_run=args.dry_run)


def cmd_judge(cfg, db, args):
    run_judge(cfg, db, dry_run=args.dry_run, print_prompt=args.print_prompt,
              force_notify=args.force_notify, mode=args.mode)


def cmd_daily(cfg, db, args):
    last = db.kv_get("last_scrape")
    recent = last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=45)
    if recent and not args.dry_run:
        log.info("último scrape hace menos de 45 min (%s): no se repite", last)
    else:
        run_scrape(cfg, db, dry_run=args.dry_run)
    if args.dry_run:
        log.info("dry-run: el scrape no guardó nada; el juez usa lo que ya hay en la DB")
    run_judge(cfg, db, dry_run=args.dry_run, force_notify=args.force_notify)


def cmd_status(cfg, db, args):
    print(f"Criterios: {cfg.criteria_path}\nEstado:    {cfg.state_dir}")
    print(f"Último scrape: {db.kv_get('last_scrape', 'nunca')}")
    print("\nFuentes:")
    for h in db.all_health():
        state = "OK" if not h["consecutive_failures"] else f"FALLA x{h['consecutive_failures']}"
        print(f"  {h['source']:12} {state:10} último OK {h['last_ok'] or 'nunca'} · {h['last_count']} listados"
              + (f"\n               └ {squash(h['last_error'], 150)}" if h["last_error"] else ""))
    recent = db.listings_since(_iso_hours_ago(cfg["judge"].get("lookback_hours", 30)))
    cands = collect_candidates(cfg, db)
    print(f"\nListados en la DB: {db.count_listings()} · vistos en las últimas "
          f"{cfg['judge'].get('lookback_hours', 30)} h: {len(recent)} · candidatos para el juez: {len(cands)}")
    for c in cands[: args.top]:
        print(f"  #{c['id']:<5} {fmt_eur(c['price_total']):>12} | {squash(c['store'], 18):18} | "
              f"{c['gpu']:7} | {squash(c['title'], 75)}")
    j = db.last_judgment()
    if j:
        import json
        out = json.loads(j["output"])
        print(f"\nÚltimo juicio: {j['ts']} · modo {j['mode']} · {'avisó' if j['notified'] else 'silencio'} · "
              f"{out.get('headline')}")


def cmd_test_notify(cfg, db, args):
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        sys.exit("Falta TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en el entorno.")
    notify.send(cfg, f"🧪 <b>nbwatch</b>: notificación de prueba ({esc(now_utc())}). Si leés esto, Telegram anda.")
    print("Mensaje de prueba enviado.")


def cmd_telegram_chat_id(cfg, db, args):
    if not cfg.telegram_token:
        sys.exit("Falta TELEGRAM_BOT_TOKEN en el entorno.")
    updates = telegram_call(cfg.telegram_token, "getUpdates")
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No hay mensajes. Mandale /start a tu bot desde Telegram y volvé a correr esto.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   # {name}")


def _report_crash(cfg, db, command, exc):
    """Avisa por Telegram que una corrida programada rompió (sin repetir el mismo error más de 1 vez cada 12 h)."""
    sig = f"{command}:{type(exc).__name__}:{str(exc)[:200]}"
    prev = db.kv_get("last_crash")
    if prev:
        prev_sig, _, prev_ts = prev.rpartition("|")
        if prev_sig == sig and datetime.fromisoformat(prev_ts) > datetime.now(timezone.utc) - timedelta(hours=12):
            return
    try:
        notify.send(cfg, f"💥 <b>nbwatch {command}</b> falló:\n<code>{esc(type(exc).__name__)}: {esc(squash(str(exc), 700))}</code>\n"
                         "Logs: <code>journalctl -u nbwatch-*</code>")
        db.kv_set("last_crash", f"{sig}|{now_utc()}")
    except Exception as e:
        log.error("tampoco se pudo avisar por Telegram: %s", e)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nbwatch", description="Vigilante de ofertas de notebooks gamer (Italia)")
    ap.add_argument("--criteria", help="ruta a criteria.yaml (default: la del proyecto o $NBWATCH_CRITERIA)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scrape", help="recorre las fuentes, guarda ofertas, alertas inmediatas y de salud")
    p.add_argument("--source", action="append", choices=["trovaprezzi", "idealo", "unieuro", "hpstore"],
                   help="solo esta fuente (se puede repetir)")
    p.add_argument("--dry-run", action="store_true", help="no guarda ni avisa: imprime qué pasa el prefiltro")
    p.set_defaults(func=cmd_scrape, locked=True)

    p = sub.add_parser("judge", help="juicio diario con claude -p sobre los candidatos de la DB")
    p.add_argument("--dry-run", action="store_true", help="llama a claude pero imprime el mensaje en vez de mandarlo")
    p.add_argument("--print-prompt", action="store_true", help="solo imprime el prompt (no llama a claude)")
    p.add_argument("--force-notify", action="store_true", help="manda el mensaje aunque el juez diga que no")
    p.add_argument("--mode", choices=["pre", "post"], help="forzar el modo (pre/post fecha de corte)")
    p.set_defaults(func=cmd_judge, locked=True)

    p = sub.add_parser("daily", help="scrape + juicio (lo que corre el timer diario)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-notify", action="store_true", help="prueba end-to-end: manda el aviso sí o sí")
    p.set_defaults(func=cmd_daily, locked=True)

    p = sub.add_parser("status", help="salud de fuentes, candidatos actuales y último juicio")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=cmd_status, locked=False)

    sub.add_parser("test-notify", help="manda un mensaje de prueba a Telegram").set_defaults(func=cmd_test_notify, locked=False)
    sub.add_parser("telegram-chat-id", help="muestra el chat_id de quien le escribió al bot").set_defaults(
        func=cmd_telegram_chat_id, locked=False)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = Config(args.criteria)
    db = DB(cfg.db_path)
    scheduled = args.locked and not getattr(args, "dry_run", False) and not getattr(args, "print_prompt", False)
    try:
        if args.locked:
            with file_lock(cfg.lock_path):
                args.func(cfg, db, args)
        else:
            args.func(cfg, db, args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log.exception("%s falló", args.command)
        if scheduled:
            _report_crash(cfg, db, args.command, e)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
