import json
import sqlite3

from .models import Listing
from .util import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL,
  store TEXT,
  title TEXT NOT NULL,
  url TEXT,
  mpn TEXT,
  price REAL,
  base_price REAL,
  shipping REAL,
  available INTEGER,
  extra TEXT NOT NULL DEFAULT '{}',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  min_price REAL,
  prev_price REAL,
  price_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
  listing_id INTEGER NOT NULL, ts TEXT NOT NULL, price REAL, available INTEGER
);
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_ok TEXT, last_error TEXT, last_count INTEGER,
  alerted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent_alerts (
  kind TEXT NOT NULL, ref TEXT NOT NULL, price REAL, ts TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);
CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, mode TEXT, notified INTEGER, output TEXT, picks TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # --- listings ---
    def upsert_listing(self, l: Listing, ts=None) -> int:
        ts = ts or now_utc()
        avail = None if l.available is None else int(l.available)
        row = self.conn.execute("SELECT id, price, min_price, available FROM listings WHERE key=?", (l.key,)).fetchone()
        extra = json.dumps(l.extra, ensure_ascii=False)
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO listings (key, source, store, title, url, mpn, price, base_price, shipping, available,"
                " extra, first_seen, last_seen, min_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (l.key, l.source, l.store, l.title, l.url, l.mpn, l.price, l.base_price, l.shipping, avail,
                 extra, ts, ts, l.price),
            )
            lid = cur.lastrowid
            self.conn.execute("INSERT INTO price_history VALUES (?,?,?,?)", (lid, ts, l.price, avail))
        else:
            lid = row["id"]
            changed = row["price"] != l.price or row["available"] != avail
            prices = [p for p in (row["min_price"], l.price) if p is not None]
            min_price = min(prices) if prices else None
            self.conn.execute(
                "UPDATE listings SET store=?, title=?, url=?, mpn=?, price=?, base_price=?, shipping=?, available=?,"
                " extra=?, last_seen=?, min_price=? WHERE id=?",
                (l.store, l.title, l.url, l.mpn, l.price, l.base_price, l.shipping, avail, extra, ts, min_price, lid),
            )
            if row["price"] != l.price:
                self.conn.execute("UPDATE listings SET prev_price=?, price_changed_at=? WHERE id=?", (row["price"], ts, lid))
            if changed:
                self.conn.execute("INSERT INTO price_history VALUES (?,?,?,?)", (lid, ts, l.price, avail))
        self.conn.commit()
        return lid

    def listings_since(self, ts_iso):
        return self.conn.execute("SELECT * FROM listings WHERE last_seen >= ? ORDER BY price", (ts_iso,)).fetchall()

    def listings_by_ids(self, ids):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["id"]: r for r in self.conn.execute(f"SELECT * FROM listings WHERE id IN ({q})", list(ids))}

    def count_listings(self):
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    # --- salud de fuentes ---
    def health(self, source):
        row = self.conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO source_health (source) VALUES (?)", (source,))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
        return row

    def all_health(self):
        return self.conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()

    def set_health(self, source, **fields):
        self.health(source)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE source_health SET {cols} WHERE source=?", (*fields.values(), source))
        self.conn.commit()

    # --- alertas inmediatas (dedupe) ---
    def alert_sent(self, kind, ref):
        return self.conn.execute("SELECT * FROM sent_alerts WHERE kind=? AND ref=?", (kind, ref)).fetchone()

    def mark_alert(self, kind, ref, price):
        self.conn.execute("INSERT OR REPLACE INTO sent_alerts VALUES (?,?,?,?)", (kind, ref, price, now_utc()))
        self.conn.commit()

    # --- juicios ---
    def save_judgment(self, mode, notified, output, picks):
        self.conn.execute(
            "INSERT INTO judgments (ts, mode, notified, output, picks) VALUES (?,?,?,?,?)",
            (now_utc(), mode, int(notified), json.dumps(output, ensure_ascii=False), json.dumps(picks, ensure_ascii=False)),
        )
        self.conn.commit()

    def recent_judgments(self, since_iso):
        return self.conn.execute("SELECT * FROM judgments WHERE ts >= ? ORDER BY ts", (since_iso,)).fetchall()

    def last_judgment(self):
        return self.conn.execute("SELECT * FROM judgments ORDER BY ts DESC LIMIT 1").fetchone()

    # --- kv ---
    def kv_get(self, k, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))
        self.conn.commit()
