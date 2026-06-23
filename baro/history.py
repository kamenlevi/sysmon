"""SQLite history storage."""
import os
import sqlite3
import time
from typing import List, Tuple

DB_PATH = os.path.expanduser("~/.local/share/baro/history.db")


class HistoryDB:
    def __init__(self, max_age_hours: int = 24):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.max_age_hours = max_age_hours
        self._last_prune = 0.0
        self._create_tables()

    def _create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                ts REAL,
                cpu_pct REAL, cpu_temp REAL,
                ram_pct REAL, ram_used_gb REAL,
                gpu_pct REAL, gpu_temp REAL, gpu_mem_pct REAL,
                warnings INTEGER
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON stats(ts)")
        self.conn.commit()

    def record(self, s):
        self.conn.execute(
            "INSERT INTO stats VALUES (?,?,?,?,?,?,?,?,?)",
            (
                s.timestamp,
                s.cpu_percent, s.cpu_temp,
                s.ram_percent, s.ram_used_gb,
                s.gpu_percent, s.gpu_temp, s.gpu_mem_percent,
                len(s.warnings),
            ),
        )
        self.conn.commit()
        # Pruning is range-scan + index churn. Doing it on every record
        # (every ~5s) is wasteful — once a minute is plenty.
        now = time.time()
        if now - self._last_prune >= 60.0:
            self._last_prune = now
            self._prune()

    def _prune(self):
        cutoff = time.time() - self.max_age_hours * 3600
        self.conn.execute("DELETE FROM stats WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def fetch(self, seconds: int = 300) -> List[Tuple]:
        cutoff = time.time() - seconds
        cur = self.conn.execute(
            "SELECT ts,cpu_pct,cpu_temp,ram_pct,gpu_pct,gpu_temp FROM stats WHERE ts > ? ORDER BY ts",
            (cutoff,),
        )
        return cur.fetchall()

    def close(self):
        self.conn.close()
