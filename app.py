import os
import secrets
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    secret_key = secrets.token_hex(32)
app.config["SECRET_KEY"] = secret_key
app.config["WTF_CSRF_TIME_LIMIT"] = None

csrf = CSRFProtect(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="memory://",
)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(_BASE_DIR, "data", "laundry.db"))
LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))

MACHINES = {
    "lave-linge": {"name": "Lave-linge", "icon": "🧺", "cycle_minutes": "45"},
    "seche-linge": {"name": "Sèche-linge", "icon": "💨", "cycle_minutes": "45"},
}

_db_lock = threading.Lock()


def _cycle_minutes(machine_id: str) -> int:
    return int(MACHINES[machine_id]["cycle_minutes"])


def _max_remaining_minutes(machine_id: str) -> float:
    return round(_cycle_minutes(machine_id) * 1.10, 1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machines (
                id TEXT PRIMARY KEY,
                in_use INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                ends_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                planned_ends_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_started ON usage_events(started_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_machine ON usage_events(machine_id)"
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _expire_finished(conn: sqlite3.Connection) -> None:
    now = _utcnow()
    now_iso = now.isoformat()
    expired_rows = conn.execute(
        """
        SELECT id FROM machines
        WHERE in_use = 1 AND ends_at IS NOT NULL AND ends_at <= ?
        """,
        (now_iso,),
    ).fetchall()
    for row in expired_rows:
        _close_usage_event(conn, row["id"], now)
    conn.execute(
        """
        UPDATE machines
        SET in_use = 0, started_at = NULL, ends_at = NULL
        WHERE in_use = 1 AND ends_at IS NOT NULL AND ends_at <= ?
        """,
        (now_iso,),
    )


def _log_usage_start(
    conn: sqlite3.Connection, machine_id: str, started_at: datetime, planned_ends_at: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO usage_events (machine_id, started_at, planned_ends_at)
        VALUES (?, ?, ?)
        """,
        (machine_id, started_at.isoformat(), planned_ends_at.isoformat()),
    )


def _close_usage_event(
    conn: sqlite3.Connection, machine_id: str, ended_at: datetime
) -> None:
    conn.execute(
        """
        UPDATE usage_events
        SET ended_at = ?
        WHERE id = (
            SELECT id FROM usage_events
            WHERE machine_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
        )
        """,
        (ended_at.isoformat(), machine_id),
    )


STATS_DAYS = 30
STATS_MIN_DURATION_RATIO = 0.5

WEEKDAY_NAMES = [
    "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim",
]

USAGE_LABELS = {
    "high": "Généralement utilisé",
    "medium": "Usage modéré",
    "low": "Peu utilisé",
}


def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def _classify_usage(counts: list[int]) -> tuple[list[str], dict]:
    """Compare chaque créneau à la moyenne du mois (±25 %)."""
    n = len(counts)
    total = sum(counts)
    if total == 0:
        return ["low"] * n, {
            "mean": 0,
            "threshold_high": 0,
            "threshold_low": 0,
            "reliable": False,
        }

    mean = total / n
    threshold_high = mean * 1.25
    threshold_low = mean * 0.75

    levels = []
    for count in counts:
        if count == 0 or count < threshold_low:
            levels.append("low")
        elif count >= threshold_high:
            levels.append("high")
        else:
            levels.append("medium")

    return levels, {
        "mean": round(mean, 2),
        "threshold_high": round(threshold_high, 2),
        "threshold_low": round(threshold_low, 2),
        "reliable": total >= 10,
    }


def _event_counts_for_stats(row: sqlite3.Row) -> bool:
    machine_id = row["machine_id"]
    if machine_id not in MACHINES:
        return False

    started = _parse_dt(row["started_at"])
    planned_end = _parse_dt(row["planned_ends_at"])
    ended = _parse_dt(row["ended_at"])
    if not started or not planned_end or not ended:
        return False

    planned_seconds = (planned_end - started).total_seconds()
    if planned_seconds <= 0:
        return False

    actual_seconds = (ended - started).total_seconds()
    return actual_seconds >= planned_seconds * STATS_MIN_DURATION_RATIO


def get_monthly_heatmap_stats() -> dict:
    end_local = _to_local(_utcnow())
    start_local = (end_local - timedelta(days=STATS_DAYS - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since_utc = start_local.astimezone(timezone.utc)

    grid: dict[tuple[int, int], int] = defaultdict(int)

    with _db_lock:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT machine_id, started_at, ended_at, planned_ends_at
                FROM usage_events
                WHERE started_at >= ?
                """,
                (since_utc.isoformat(),),
            ).fetchall()

    for row in rows:
        if not _event_counts_for_stats(row):
            continue
        started = _to_local(_parse_dt(row["started_at"]))
        grid[(started.weekday(), started.hour)] += 1

    counts = [grid[(weekday, hour)] for weekday in range(7) for hour in range(24)]
    levels, algorithm = _classify_usage(counts)

    cells = []
    index = 0
    for weekday in range(7):
        for hour in range(24):
            count = grid[(weekday, hour)]
            level = levels[index]
            cells.append(
                {
                    "weekday": weekday,
                    "hour": hour,
                    "count": count,
                    "usage_level": level,
                    "usage_label": USAGE_LABELS[level],
                }
            )
            index += 1

    return {
        "period_days": STATS_DAYS,
        "period_start": start_local.date().isoformat(),
        "period_end": end_local.date().isoformat(),
        "timezone": str(LOCAL_TZ),
        "weekday_labels": WEEKDAY_NAMES,
        "cells": cells,
        "total_cycles": sum(counts),
        "algorithm": algorithm,
    }


def _row_to_status(machine_id: str, row: sqlite3.Row | None) -> dict:
    meta = MACHINES[machine_id]
    if row is None:
        return {
            "id": machine_id,
            "name": meta["name"],
            "icon": meta["icon"],
            "available": True,
            "in_use": False,
            "started_at": None,
            "ends_at": None,
            "remaining_seconds": 0,
            "cycle_minutes": _cycle_minutes(machine_id),
            "max_remaining_minutes": _max_remaining_minutes(machine_id),
        }

    in_use = bool(row["in_use"])
    ends_at = _parse_dt(row["ends_at"])
    started_at = _parse_dt(row["started_at"])
    now = _utcnow()

    remaining_seconds = 0
    if in_use and ends_at:
        remaining_seconds = max(0, int((ends_at - now).total_seconds()))

    return {
        "id": machine_id,
        "name": meta["name"],
        "icon": meta["icon"],
        "available": not in_use or remaining_seconds == 0,
        "in_use": in_use and remaining_seconds > 0,
        "started_at": started_at.isoformat() if started_at and in_use else None,
        "ends_at": ends_at.isoformat() if ends_at and in_use and remaining_seconds > 0 else None,
        "remaining_seconds": remaining_seconds,
        "cycle_minutes": _cycle_minutes(machine_id),
        "max_remaining_minutes": _max_remaining_minutes(machine_id),
    }


def get_all_status() -> list[dict]:
    with _db_lock:
        with get_db() as conn:
            _expire_finished(conn)
            rows = {
                row["id"]: row
                for row in conn.execute("SELECT * FROM machines").fetchall()
            }
            return [_row_to_status(machine_id, rows.get(machine_id)) for machine_id in MACHINES]


@app.route("/")
def index():
    return render_template("index.html", machines=MACHINES, csrf_token=generate_csrf())


@app.errorhandler(429)
def rate_limit_exceeded(_error):
    return jsonify({"error": "Trop de requêtes, réessaie dans un instant."}), 429


@app.errorhandler(CSRFError)
def csrf_error(_error):
    return jsonify({"error": "Session expirée, recharge la page."}), 403


@app.route("/api/status")
@limiter.limit("180 per minute")
def api_status():
    return jsonify({"machines": get_all_status(), "server_time": _utcnow().isoformat()})


@app.route("/api/start", methods=["POST"])
@limiter.limit("15 per minute")
def api_start():
    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id")

    if machine_id not in MACHINES:
        return jsonify({"error": "Machine inconnue."}), 400

    remaining_minutes = data.get("remaining_minutes")
    max_remaining = _max_remaining_minutes(machine_id)
    if remaining_minutes is not None:
        try:
            remaining_minutes = float(remaining_minutes)
        except (TypeError, ValueError):
            return jsonify({"error": "Temps restant invalide."}), 400
        if remaining_minutes <= 0 or remaining_minutes > max_remaining:
            return (
                jsonify(
                    {
                        "error": (
                            f"Temps restant invalide "
                            f"(entre 1 et {max_remaining:g} min)."
                        ),
                    }
                ),
                400,
            )
        duration_minutes = remaining_minutes
    else:
        duration_minutes = _cycle_minutes(machine_id)

    now = _utcnow()
    ends_at = now + timedelta(minutes=duration_minutes)

    with _db_lock:
        with get_db() as conn:
            _expire_finished(conn)
            row = conn.execute(
                "SELECT * FROM machines WHERE id = ?", (machine_id,)
            ).fetchone()

            if row:
                status = _row_to_status(machine_id, row)
                if status["in_use"]:
                    return (
                        jsonify(
                            {
                                "error": f"{MACHINES[machine_id]['name']} est déjà utilisé(e).",
                            }
                        ),
                        409,
                    )
                conn.execute(
                    """
                    UPDATE machines
                    SET in_use = 1, started_at = ?, ends_at = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), ends_at.isoformat(), machine_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO machines (id, in_use, started_at, ends_at)
                    VALUES (?, 1, ?, ?)
                    """,
                    (machine_id, now.isoformat(), ends_at.isoformat()),
                )

            _log_usage_start(conn, machine_id, now, ends_at)

    return jsonify({"ok": True, "machines": get_all_status()})


@app.route("/api/release", methods=["POST"])
@limiter.limit("15 per minute")
def api_release():
    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id")

    if machine_id not in MACHINES:
        return jsonify({"error": "Machine inconnue."}), 400

    with _db_lock:
        with get_db() as conn:
            _expire_finished(conn)
            row = conn.execute(
                "SELECT * FROM machines WHERE id = ?", (machine_id,)
            ).fetchone()

            if not row or not row["in_use"]:
                return jsonify({"error": "La machine est déjà libre."}), 409

            _close_usage_event(conn, machine_id, _utcnow())
            conn.execute(
                """
                UPDATE machines
                SET in_use = 0, started_at = NULL, ends_at = NULL
                WHERE id = ?
                """,
                (machine_id,),
            )

    return jsonify({"ok": True, "machines": get_all_status()})


@app.route("/api/stats")
@limiter.limit("60 per minute")
def api_stats():
    return jsonify(get_monthly_heatmap_stats())


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
