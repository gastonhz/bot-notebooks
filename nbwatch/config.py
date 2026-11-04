import os
import re
from datetime import date
from pathlib import Path

import yaml

from .prefilter import CPU_TIERS

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Claves que el código lee sin valor por defecto (si faltan, la corrida rompe) y su tipo.
_NUM = (int, float)
_REQUIRED = {
    "price": {"target": _NUM, "hard_cap": _NUM, "hard_cap_high_vram": _NUM, "min_sane": _NUM},
    "gpus": {"preferred": list, "high_vram": list},
    "stores": {"trusted": list, "blocked": list},
    "reference": {"mpn": str, "name": str, "specs": str, "where": str, "price": _NUM,
                  "instant_alert_max_price": _NUM},
    "plan_b": {"mpn": str, "name": str, "specs": str, "where": str, "price": _NUM},
    "health": {"fail_threshold": int},
    "judge": {},
}
# Secciones opcionales: si están, estas claves son obligatorias dentro.
_REQUIRED_IF_PRESENT = {
    "buy_now": {"gpus": list, "cpu_tiers": list, "min_ram_gb": _NUM, "min_storage_gb": _NUM, "max_price": _NUM},
}
# Claves opcionales: si están, tienen que ser de este tipo (o uno de estos valores).
_OPTIONAL = {
    "price": {"suspicious_action": ("discard", "flag"), "suspicious_below": dict},
    "instant_alerts": {"high_vram_max_price": _NUM, "priority": ("normal", "high"), "quick_judge": bool,
                       "quick_judge_timeout_minutes": _NUM},
    "buy_now": {"trusted_store_only": bool, "must_deliver_by": bool},
}
_TYPE_NAMES = {str: "un texto", list: "una lista", int: "un entero", _NUM: "un número", dict: "un mapa",
               bool: "true o false"}


def _type_problem(path, value, typ) -> str | None:
    if isinstance(typ, tuple) and all(isinstance(t, str) for t in typ):
        return None if value in typ else f"{path}: tiene que ser {' o '.join(typ)}"
    if isinstance(value, typ):
        return None
    return f"{path}: falta" if value is None else f"{path}: no es {_TYPE_NAMES[typ]}"


def resolve_criteria_path(path=None) -> Path:
    return Path(path or os.environ.get("NBWATCH_CRITERIA") or PROJECT_ROOT / "criteria.yaml").resolve()


def resolve_state_dir() -> Path:
    path = Path(os.environ.get("NBWATCH_STATE_DIR") or PROJECT_ROOT / "state").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    """criteria.yaml + variables de entorno (secretos y rutas)."""

    def __init__(self, criteria_path=None, text=None):
        """text: contenido a usar en vez del archivo (los borradores de la interfaz web)."""
        self.criteria_path = resolve_criteria_path(criteria_path)
        if text is None:
            text = self.criteria_path.read_text(encoding="utf-8")
        self.c = yaml.safe_load(text)
        if not isinstance(self.c, dict):
            raise ValueError("criteria.yaml no es un mapa de claves YAML")

        self.state_dir = resolve_state_dir()
        self.db_path = self.state_dir / "nbwatch.sqlite3"
        self.lock_path = self.state_dir / "nbwatch.lock"
        self.browser_profile = self.state_dir / "browser-profile"

        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        # canal opcional para alertas inmediatas (instant_alerts.priority: high); vacíos = el de siempre
        self.telegram_alert_token = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "").strip()
        self.telegram_alert_chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
        self.claude_bin = os.environ.get("CLAUDE_BIN", "claude")

        dates = self.c["dates"]
        for key in ("cutoff", "deliver_by", "end", "decide_by"):
            if key == "decide_by" and dates.get(key) is None:
                continue  # opcional
            if not isinstance(dates[key], date):
                try:
                    dates[key] = date.fromisoformat(str(dates[key]))
                except ValueError:
                    raise ValueError(f"dates.{key}: fecha inválida ({dates[key]!r}), usá AAAA-MM-DD") from None

    def problems(self) -> list[str]:
        """Lo que haría romper una corrida: claves faltantes o de otro tipo, regex inválidas, fuentes desconocidas."""
        out = []
        for section, keys in (_REQUIRED | {s: k for s, k in _REQUIRED_IF_PRESENT.items() if s in self.c}).items():
            values = self.c.get(section)
            if not isinstance(values, dict):
                out.append(f"falta la sección {section}")
                continue
            out += [p for key, typ in keys.items() if (p := _type_problem(f"{section}.{key}", values.get(key), typ))]
        for section, keys in _OPTIONAL.items():
            values = self.c.get(section) or {}
            if isinstance(values, dict):
                out += [p for key, typ in keys.items()
                        if key in values and (p := _type_problem(f"{section}.{key}", values[key], typ))]
        floors = (self.c.get("price") or {}).get("suspicious_below") or {}
        if isinstance(floors, dict) and not all(isinstance(v, _NUM) for v in floors.values()):
            out.append("price.suspicious_below: cada GPU tiene que tener un precio")
        timeout = (self.c.get("instant_alerts") or {}).get("quick_judge_timeout_minutes", 1)
        if isinstance(timeout, _NUM) and timeout <= 0:
            out.append("instant_alerts.quick_judge_timeout_minutes: tiene que ser mayor que 0")
        tiers = (self.c.get("buy_now") or {}).get("cpu_tiers")
        if isinstance(tiers, list) and set(tiers) - {"tier1", "tier2", "tier3"}:
            out.append("buy_now.cpu_tiers: solo tier1, tier2 o tier3")
        if not isinstance(self.c.get("search_terms"), list):
            out.append("search_terms: falta o no es una lista")
        for key in ("exclude_keywords", "flag_keywords", "watch_mpns"):
            if not isinstance(self.c.get(key, []), list):
                out.append(f"{key}: tiene que ser una lista")
        for i, k in enumerate(self.c.get("known_options") or []):
            missing = [f for f in ("name", "specs", "price", "where") if f not in (k or {})]
            if missing:
                out.append(f"known_options[{i}]: falta {', '.join(missing)}")
        cpus = self.c.get("cpus") or {}
        if cpus.get("unknown", "judge") not in ("judge", "discard"):
            out.append("cpus.unknown: tiene que ser judge o discard")
        for tier in CPU_TIERS:
            for c in cpus.get(tier) or []:
                try:
                    re.compile(c["re"])
                except (KeyError, TypeError):
                    out.append(f"cpus.{tier}: entrada sin name/re: {c}")
                except re.error as e:
                    out.append(f"cpus.{tier} «{c.get('name')}»: regex inválida ({e})")
        from .sources import get_sources

        unknown = set(self.c.get("sources") or {}) - set(get_sources())
        if unknown:
            out.append(f"sources: fuentes desconocidas: {', '.join(sorted(unknown))}")
        return out

    def __getitem__(self, key):
        return self.c[key]

    def get(self, key, default=None):
        return self.c.get(key, default)

    @property
    def dates(self):
        return self.c["dates"]

    def source_cfg(self, name) -> dict:
        return self.c.get("sources", {}).get(name) or {}

    def enabled_sources(self) -> list:
        return [n for n, s in self.c.get("sources", {}).items() if (s or {}).get("enabled", True)]

    @property
    def judge_prompt_path(self) -> Path:
        return self.criteria_path.parent / self.c["judge"].get("prompt_file", "judge_prompt.md")
