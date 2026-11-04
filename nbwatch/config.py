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
_TYPE_NAMES = {str: "un texto", list: "una lista", int: "un entero", _NUM: "un número"}


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
        self.claude_bin = os.environ.get("CLAUDE_BIN", "claude")

        for key in ("cutoff", "deliver_by", "end"):
            value = self.c["dates"][key]
            if not isinstance(value, date):
                self.c["dates"][key] = date.fromisoformat(str(value))

    def problems(self) -> list[str]:
        """Lo que haría romper una corrida: claves faltantes o de otro tipo, regex inválidas, fuentes desconocidas."""
        out = []
        for section, keys in _REQUIRED.items():
            values = self.c.get(section)
            if not isinstance(values, dict):
                out.append(f"falta la sección {section}")
                continue
            out += [f"{section}.{key}: falta o no es {_TYPE_NAMES[typ]}"
                    for key, typ in keys.items() if not isinstance(values.get(key), typ)]
        if not isinstance(self.c.get("search_terms"), list):
            out.append("search_terms: falta o no es una lista")
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
