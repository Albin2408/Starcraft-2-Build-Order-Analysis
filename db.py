"""
SQLite schema + helpers, shared by the Flask app and the Celery workers.

One schema, generated from the column lists below, so adding a metric is a
one-line change (plus a PARSER_VERSION bump to re-derive old rows).

Workers and the web process write concurrently, so the connection uses WAL
mode and a generous busy timeout.
"""

import json
import os
import sqlite3

from config import DB_PATH

# Bump whenever extraction logic changes in a way that alters stored numbers.
# Rows with an older parser_version are flagged "stale" and can be rebuilt
# from their source replay without re-uploading anything.
#   3: timings now in real (Faster) seconds instead of sc2reader's 16fps
#      clock; supply-depot type fix; telemetry (APM / floating / heatmaps).
#   4: floating minerals ignores time spent maxed out; games vs the built-in
#      AI are excluded.
#   5: worker counts at 4/6/8 min and chrono / MULE / inject usage.
PARSER_VERSION = 5

# Columns shared by `games` (your replays) and `pro_games` (benchmark pool).
STAT_COLUMNS = [
    ("map", "TEXT"),
    ("my_race", "TEXT"),
    ("opp_race", "TEXT"),
    ("win", "INTEGER"),
    ("played_at", "TEXT"),
    ("game_version", "TEXT"),
    ("game_length", "INTEGER"),
    ("first_supply_time", "INTEGER"),
    ("first_gas_time", "INTEGER"),
    ("second_base_time", "INTEGER"),
    ("third_base_time", "INTEGER"),
    ("supply_block_count", "INTEGER"),
    ("build_order_json", "TEXT"),
    # --- telemetry-derived ---
    ("apm_total", "REAL"),
    ("apm_combat", "REAL"),
    ("apm_macro", "REAL"),          # APM outside fights ("calm" APM)
    ("apm_retention", "REAL"),      # combat APM / calm APM
    ("macro_rate_combat", "REAL"),  # production/economy commands per min, in fights
    ("macro_rate_calm", "REAL"),    # ... outside fights
    ("macro_retention", "REAL"),    # in-fight macro rate / calm macro rate
    ("mineral_floating_avg", "REAL"),
    ("float_penalty", "REAL"),      # mean minerals above the float threshold
    ("float_pct", "REAL"),          # % of time spent heavily floating
    ("fight_count", "INTEGER"),
    # --- economy & macro mechanics (first 10 minutes) ---
    ("workers_4m", "INTEGER"),
    ("workers_6m", "INTEGER"),
    ("workers_8m", "INTEGER"),
    ("mech_uses", "INTEGER"),       # chrono / MULE+scan / inject commands used
    ("mech_possible", "INTEGER"),   # how many the energy allowed
    ("mech_efficiency", "REAL"),    # uses / possible, capped at 1
    # --- bookkeeping ---
    ("file_hash", "TEXT"),
    ("source_path", "TEXT"),
    ("parser_version", "INTEGER"),
]

# Left over from an earlier prototype of the heatmap feature (a fixed
# 20x20 grid per phase). Superseded by the `telemetry` table; kept nullable
# so databases created by that version still open cleanly.
LEGACY_COLUMNS = [("heatmap_early", "TEXT"), ("heatmap_mid", "TEXT"), ("heatmap_late", "TEXT")]

STAT_NAMES = [name for name, _ in STAT_COLUMNS]


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_columns(conn, table, columns):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, coltype in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT UNIQUE NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pro_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            perspective INTEGER NOT NULL,
            UNIQUE(file_name, perspective)
        )
    """)
    # Heavy per-game telemetry (unit-position snapshots, time series, fights)
    # lives apart from the stat rows so list queries stay light.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            source TEXT NOT NULL,            -- 'mine' | 'pro'
            file_name TEXT NOT NULL,
            perspective INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL,
            PRIMARY KEY (source, file_name, perspective)
        )
    """)
    for table in ("games", "pro_games"):
        _ensure_columns(conn, table, STAT_COLUMNS)
        _ensure_columns(conn, table, LEGACY_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_games_hash ON games(file_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pro_hash ON pro_games(file_hash)")
    conn.commit()
    conn.close()


# --- settings ---------------------------------------------------------------

def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# --- writes -----------------------------------------------------------------

def _row_values(record, file_name, source_path, file_hash):
    values = {name: record.get(name) for name in STAT_NAMES}
    values["win"] = int(bool(record["win"]))
    values["build_order_json"] = json.dumps(record.get("build_order") or [])
    values["file_hash"] = file_hash
    values["source_path"] = source_path
    values["parser_version"] = PARSER_VERSION
    values["file_name"] = file_name
    return values


def upsert_game(conn, record, file_name, source_path=None, file_hash=None):
    """Insert or refresh one of *your* games, keyed by file name."""
    values = _row_values(record, file_name, source_path, file_hash)
    cols = ["file_name"] + STAT_NAMES
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in STAT_NAMES)
    conn.execute(
        f"INSERT INTO games ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(file_name) DO UPDATE SET {updates}",
        [values[c] for c in cols],
    )
    if record.get("telemetry"):
        save_telemetry(conn, "mine", file_name, 0, record["telemetry"])


def upsert_pro_game(conn, record, file_name, perspective, source_path=None, file_hash=None):
    values = _row_values(record, file_name, source_path, file_hash)
    cols = ["file_name", "perspective"] + STAT_NAMES
    values["perspective"] = perspective
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in STAT_NAMES)
    conn.execute(
        f"INSERT INTO pro_games ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(file_name, perspective) DO UPDATE SET {updates}",
        [values[c] for c in cols],
    )
    if record.get("telemetry"):
        save_telemetry(conn, "pro", file_name, perspective, record["telemetry"])


def delete_game(conn, source, file_name):
    """Drop a replay's rows - used when a rebuild finds it no longer qualifies."""
    table = "games" if source == "mine" else "pro_games"
    conn.execute(f"DELETE FROM {table} WHERE file_name = ?", (file_name,))
    conn.execute("DELETE FROM telemetry WHERE source = ? AND file_name = ?", (source, file_name))


def save_telemetry(conn, source, file_name, perspective, telemetry):
    conn.execute(
        "INSERT INTO telemetry (source, file_name, perspective, data) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(source, file_name, perspective) DO UPDATE SET data = excluded.data",
        (source, file_name, perspective, json.dumps(telemetry, separators=(",", ":"))),
    )


# --- reads ------------------------------------------------------------------

def load_telemetry(conn, source, file_name, perspective=0):
    row = conn.execute(
        "SELECT data FROM telemetry WHERE source = ? AND file_name = ? AND perspective = ?",
        (source, file_name, perspective),
    ).fetchone()
    return json.loads(row["data"]) if row else None


def inflate(row):
    """sqlite Row -> plain dict with JSON columns decoded and win as bool."""
    r = dict(row)
    r["win"] = bool(r["win"])
    r["file"] = r["file_name"]
    raw = r.get("build_order_json")
    try:
        r["build_order"] = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        r["build_order"] = []
    return r


def stale_counts(conn):
    """How many rows were produced by an older parser (and so should be rebuilt)."""
    out = {}
    for table in ("games", "pro_games"):
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE parser_version IS NULL OR parser_version < ?",
            (PARSER_VERSION,),
        ).fetchone()[0]
    return out


def known_file_state(conn, table, file_name):
    """(parser_version, file_hash) for an existing row, or None."""
    row = conn.execute(
        f"SELECT parser_version, file_hash FROM {table} WHERE file_name = ? LIMIT 1", (file_name,)
    ).fetchone()
    return (row["parser_version"], row["file_hash"]) if row else None
