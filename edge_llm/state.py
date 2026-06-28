"""
edge_llm/state.py — State machine + SQLite state management.

v4.0: Added GPUMode (idle/exclusive/shared), validate_transition(),
      StateDB.get/set_active_services().
"""

import json
import sqlite3
import threading
import time
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("edge_llm")


# ─── GPU Mode State Machine ──────────────────────────────────────

class GPUMode:
    """Valid GPU mode states."""
    IDLE = "idle"
    EXCLUSIVE = "exclusive"
    SHARED = "shared"

    @classmethod
    def is_valid(cls, mode: str) -> bool:
        return mode in (cls.IDLE, cls.EXCLUSIVE, cls.SHARED)


# Valid transitions: (from_mode, to_mode) → True
# Invalid transitions → False (must go through idle first)
_VALID_TRANSITIONS = {
    # From idle
    ("idle", "idle"): True,          # no-op
    ("idle", "exclusive"): True,     # deploy exclusive model
    ("idle", "shared"): True,        # deploy shared model/service
    # From exclusive
    ("exclusive", "idle"): True,     # stop exclusive model
    ("exclusive", "exclusive"): False, # must idle first
    ("exclusive", "shared"): False,  # must idle first
    # From shared
    ("shared", "idle"): True,        # stop all shared services
    ("shared", "shared"): True,      # add/remove shared service (hot-plug)
    ("shared", "exclusive"): False,  # must idle first
}


def validate_transition(from_mode: str, to_mode: str) -> bool:
    """Check if a GPU mode transition is valid.

    Rules:
      - idle → exclusive: ✅ deploy exclusive model, GPU fully locked
      - idle → shared:    ✅ deploy shared service
      - exclusive → idle: ✅ stop exclusive model
      - shared → idle:    ✅ stop all shared services
      - shared → shared:  ✅ add/remove shared service
      - exclusive → shared: ❌ must idle first
      - shared → exclusive: ❌ must idle first
    """
    result = _VALID_TRANSITIONS.get((from_mode, to_mode))
    if result is None:
        log.warning("Unknown GPU mode transition: %s → %s", from_mode, to_mode)
        return False
    return result


class ProfileState:
    """Service health state (kept for backward compat, will rename to ServiceState)."""
    SWITCHING = "switching"
    HEALTHY = "healthy"
    IDLE = "idle"
    ERROR = "error"

    @classmethod
    def is_active(cls, state: str) -> bool:
        return state in (cls.SWITCHING, cls.HEALTHY, cls.ERROR)


# ─── State Manager ─────────────────────────────────────────────────

class StateDB:
    """Thread-safe SQLite — fresh connection per call (WAL mode)."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self._db_path), timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._lock:
            c = self._conn()
            c.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT DEFAULT CURRENT_TIMESTAMP, "
                "from_profile TEXT, to_profile TEXT, duration REAL, status TEXT)"
            )
            # Migration: add status column if missing (from v2 schema)
            try:
                c.execute("SELECT status FROM history LIMIT 1")
            except sqlite3.OperationalError:
                log.info("Migrating history table: adding status column")
                c.execute("ALTER TABLE history ADD COLUMN status TEXT DEFAULT 'ok'")
            # Ensure default state keys exist
            c.execute("INSERT OR IGNORE INTO state VALUES ('current_profile', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('profile_state', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('gpu_mode', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('active_services', '[]')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('vllm_pid', '')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('comfyui_pid', '')")
            c.commit()
            c.close()

    def get(self, key: str) -> Optional[str]:
        c = self._conn()
        try:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def set(self, key: str, value: str):
        with self._lock:
            c = self._conn()
            try:
                c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
                c.commit()
            finally:
                c.close()

    def set_multi(self, kv: dict[str, str]):
        """Atomically set multiple state keys."""
        with self._lock:
            c = self._conn()
            try:
                for k, v in kv.items():
                    c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
                c.commit()
            finally:
                c.close()

    # ─── Active Services ────────────────────────────────────────

    def get_active_services(self) -> list[str]:
        """Get list of currently active service names."""
        raw = self.get("active_services")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_active_services(self, services: list[str]):
        """Set active services list."""
        self.set("active_services", json.dumps(services))

    def add_active_service(self, name: str):
        """Add a service to the active list."""
        services = self.get_active_services()
        if name not in services:
            services.append(name)
            self.set_active_services(services)

    def remove_active_service(self, name: str):
        """Remove a service from the active list."""
        services = self.get_active_services()
        if name in services:
            services.remove(name)
            self.set_active_services(services)

    # ─── Manual Stop Protection ────────────────────────────────

    MANUAL_STOP_TTL = 600  # 10 min

    def record_manual_stop(self, name: str):
        """Record that user manually stopped a model (blocks auto-switch)."""
        stops = json.loads(self.get("manual_stops") or "{}")
        stops[name] = time.time()
        self.set("manual_stops", json.dumps(stops))

    def is_manually_stopped(self, name: str) -> bool:
        """Check if model was manually stopped within TTL."""
        stops = json.loads(self.get("manual_stops") or "{}")
        ts = stops.get(name)
        if ts is None:
            return False
        if time.time() - ts > self.MANUAL_STOP_TTL:
            del stops[name]
            self.set("manual_stops", json.dumps(stops))
            return False
        return True

    def clear_manual_stop(self, name: str):
        """Clear manual stop record (e.g. when user explicitly switches TO this model)."""
        stops = json.loads(self.get("manual_stops") or "{}")
        stops.pop(name, None)
        self.set("manual_stops", json.dumps(stops))

    # ─── GPU Mode ───────────────────────────────────────────────

    @property
    def gpu_mode(self) -> str:
        return self.get("gpu_mode") or GPUMode.IDLE

    @gpu_mode.setter
    def gpu_mode(self, mode: str):
        assert GPUMode.is_valid(mode), f"Invalid GPU mode: {mode}"
        self.set("gpu_mode", mode)

    # ─── History ────────────────────────────────────────────────

    def add_history(self, from_profile: str, to_profile: str, duration: float, status: str = "ok"):
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "INSERT INTO history (from_profile, to_profile, duration, status) VALUES (?, ?, ?, ?)",
                    (from_profile, to_profile, duration, status),
                )
                c.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (100,),
                )
                c.commit()
            finally:
                c.close()

    def get_history(self, limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT timestamp, from_profile, to_profile, duration, status "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2], "duration": r[3], "status": r[4]}
                for r in rows
            ]
        finally:
            c.close()
