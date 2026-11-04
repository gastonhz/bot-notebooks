"""Interfaz web: editar criteria.yaml y judge_prompt.md (con validación y vista previa) y correr a mano."""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from base64 import b64encode
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import PROJECT_ROOT, Config, resolve_criteria_path, resolve_state_dir
from .db import DB
from .judge import _iso_hours_ago, build_prompt, collect_candidates, current_mode, notified_history
from .prefilter import evaluate
from .util import fmt_eur, log, now_utc, today_rome

PAGE = Path(__file__).with_name("web.html")
RUNS = {  # botón -> argumentos de nbwatch
    "scrape": ["scrape"],
    "judge": ["judge", "--dry-run"],
    "daily": ["daily"],
}
KEEP_BACKUPS = 30


def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _hash(text) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _describe(e) -> str:
    return f"falta la clave {e}" if isinstance(e, KeyError) else str(e)


def _write_atomic(path, text):
    """Una corrida que arranca justo ahora lee el archivo viejo o el nuevo, nunca uno a medio escribir."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _file(path) -> dict:
    text = _read(path)
    return {"path": str(path), "text": text, "hash": _hash(text)}


def _summary(c) -> dict:
    return {"id": c["id"], "price": fmt_eur(c["price_total"]), "store": c.get("store"), "gpu": c.get("gpu"),
            "cpu": c.get("cpu"), "title": c["title"], "flags": c.get("flags") or []}


class App:
    def __init__(self, criteria_arg=None):
        self.criteria_path = resolve_criteria_path(criteria_arg)
        self.state_dir = resolve_state_dir()
        self.run_log = self.state_dir / "web-run.log"
        self.lock = threading.Lock()
        self.proc = None
        self.run_info = {}

    def prompt_path(self, criteria_text=None):
        """La que indica criteria.yaml (o el borrador); si no se puede leer, la de siempre al lado."""
        try:
            return Config(self.criteria_path, text=criteria_text).judge_prompt_path
        except Exception:
            return self.criteria_path.parent / "judge_prompt.md"

    def state(self):
        return {"criteria": _file(self.criteria_path), "prompt": _file(self.prompt_path()), "run": self.run_status()}

    def preview(self, body):
        """Valida el borrador y compara sus candidatos con los de lo guardado (sin scrapear ni llamar a claude)."""
        try:
            draft = Config(self.criteria_path, text=body["criteria"])
        except Exception as e:
            return {"errors": [f"criteria.yaml: {_describe(e)}"]}
        errors = draft.problems()
        if not body["prompt"].strip():
            errors.append("judge_prompt.md está vacío")
        if errors:
            return {"errors": errors}
        today = today_rome()
        warnings = []
        if draft.dates["end"] < today:
            warnings.append(f"dates.end ({draft.dates['end']}) ya pasó: el scrape y el juez no corren.")
        if draft.dates["deliver_by"] < today:
            warnings.append(f"dates.deliver_by ({draft.dates['deliver_by']}) ya pasó.")

        db = DB(draft.db_path)
        try:
            lookback = draft["judge"].get("lookback_hours", 30)
            mode = current_mode(draft)
            new = collect_candidates(draft, db)
            try:
                old = collect_candidates(Config(self.criteria_path), db)
            except Exception:
                old = []  # lo guardado está roto: todo cuenta como nuevo
            new_ids, old_ids = {c["id"] for c in new}, {c["id"] for c in old}
            gone = db.listings_by_ids(old_ids - new_ids)
            removed = []
            for c in old:
                if c["id"] in gone:
                    v = evaluate(gone[c["id"]], draft)
                    removed.append(_summary(c) | {"why": v.reasons[0] if v.reasons else
                                                  "duplicado o fuera de max_candidates"})
            return {
                "errors": [], "warnings": warnings,
                "recent": len(db.listings_since(_iso_hours_ago(lookback))), "lookback_hours": lookback,
                "last_scrape": db.kv_get("last_scrape"), "mode": mode,
                "saved_count": len(old),
                "candidates": [_summary(c) | {"added": c["id"] not in old_ids} for c in new],
                "removed": removed,
                "prompt": build_prompt(draft, new, mode, notified_history(db), instructions=body["prompt"]),
            }
        except Exception as e:
            log.exception("web: la vista previa rompió")
            return {"errors": [f"con estos criterios la corrida rompería: {_describe(e)}"], "warnings": warnings}
        finally:
            db.close()

    def save(self, body):
        result = self.preview(body)
        if result["errors"]:
            return 422, result
        targets = [("criteria", self.criteria_path, body["criteria"]),
                   ("prompt", self.prompt_path(body["criteria"]), body["prompt"])]
        saved = []
        with self.lock:
            for key, path, _ in targets:
                if body.get(f"{key}_hash") and _hash(_read(path)) != body[f"{key}_hash"]:
                    return 409, {"errors": [f"{path.name} cambió en disco desde que abriste la página "
                                            "(¿lo editaste por SSH?). Recargá antes de guardar."]}
            for key, path, text in targets:
                old = _read(path)
                if text != old:
                    self._backup(path, old)
                    _write_atomic(path, text)
                    saved.append(path.name)
        if saved:
            log.info("web: guardado %s", ", ".join(saved))
        return 200, result | {"saved": saved, "hashes": {key: _hash(text) for key, _, text in targets}}

    def _backup(self, path, text):
        if not text:
            return
        folder = self.state_dir / "backups"
        folder.mkdir(exist_ok=True)
        (folder / f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}").write_text(text, encoding="utf-8")
        for old in sorted(folder.glob(f"{path.name}.*"))[:-KEEP_BACKUPS]:
            old.unlink()

    def run(self, kind):
        if kind not in RUNS:
            return 400, {"errors": [f"corrida desconocida: {kind}"]}
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return 409, {"errors": [f"ya hay una corrida en curso ({self.run_info['kind']})"]}
            # mismo usuario y entorno que este servicio; el file_lock de nbwatch evita chocar con los timers
            cmd = [sys.executable, "-m", "nbwatch", "--criteria", str(self.criteria_path), *RUNS[kind]]
            with open(self.run_log, "w", encoding="utf-8") as out:
                out.write(f"$ nbwatch {' '.join(RUNS[kind])}\n")
                out.flush()
                self.proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=out,
                                             stderr=subprocess.STDOUT, env=os.environ | {"PYTHONUNBUFFERED": "1"})
            self.run_info = {"kind": kind, "started": now_utc()}
        log.info("web: corriendo nbwatch %s", " ".join(RUNS[kind]))
        return 200, self.run_status()

    def run_status(self):
        running = bool(self.proc) and self.proc.poll() is None
        lines = _read(self.run_log).splitlines()
        return {**self.run_info, "running": running,
                "exit": self.proc.returncode if self.proc and not running else None,
                "log": "\n".join(lines[-300:])}


def make_handler(app, user, password):
    expected = f"Basic {b64encode(f'{user}:{password}'.encode()).decode()}".encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def send_json(self, status, body):
            self.send_body(status, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def send_body(self, status, data, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            got = self.headers.get("Authorization", "").encode()
            if hmac.compare_digest(got, expected):
                return True
            if got:
                log.warning("web: usuario o clave incorrectos desde %s", self.client_address[0])
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="nbwatch", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/":
                self.send_body(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.send_json(200, app.state())
            elif self.path == "/api/run":
                self.send_json(200, app.run_status())
            else:
                self.send_json(404, {"errors": ["no existe"]})

        def do_POST(self):
            if not self.authorized():
                return
            # anti-CSRF: un formulario de otro sitio no puede mandar JSON, y fetch() desde otro origen manda Origin
            origin = self.headers.get("Origin")
            if (self.headers.get_content_type() != "application/json"
                    or (origin and urlsplit(origin).netloc != self.headers.get("Host"))):
                return self.send_json(403, {"errors": ["pedido rechazado"]})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/preview":
                    status, result = 200, app.preview(body)
                elif self.path == "/api/save":
                    status, result = app.save(body)
                elif self.path == "/api/run":
                    status, result = app.run(body.get("kind"))
                else:
                    status, result = 404, {"errors": ["no existe"]}
            except Exception as e:
                log.exception("web: %s rompió", self.path)
                status, result = 500, {"errors": [f"{type(e).__name__}: {e}"]}
            self.send_json(status, result)

    return Handler


def serve(criteria_arg, host, port):
    user = os.environ.get("NBWATCH_WEB_USER", "").strip()
    password = os.environ.get("NBWATCH_WEB_PASSWORD", "").strip()
    if not (user and password):
        sys.exit("Falta NBWATCH_WEB_USER y/o NBWATCH_WEB_PASSWORD en el entorno: sin clave la interfaz no arranca.")
    app = App(criteria_arg)
    server = ThreadingHTTPServer((host, port), make_handler(app, user, password))
    log.info("interfaz web en http://%s:%d · criterios: %s", host, port, app.criteria_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
