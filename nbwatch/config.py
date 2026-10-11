import os
from datetime import date
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """criteria.yaml + variables de entorno (secretos y rutas)."""

    def __init__(self, criteria_path=None):
        self.criteria_path = Path(
            criteria_path or os.environ.get("NBWATCH_CRITERIA") or PROJECT_ROOT / "criteria.yaml"
        ).resolve()
        with open(self.criteria_path, encoding="utf-8") as f:
            self.c = yaml.safe_load(f)

        self.state_dir = Path(os.environ.get("NBWATCH_STATE_DIR") or PROJECT_ROOT / "state").resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
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
