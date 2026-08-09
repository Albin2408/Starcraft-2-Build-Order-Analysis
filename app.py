import json
import os
import sqlite3

from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import analyzer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "sc2.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")          # your own replay files
PRO_UPLOAD_DIR = os.path.join(BASE_DIR, "pro_uploads")  # benchmark pool

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PRO_UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.jinja_env.globals["fmt_time"] = analyzer.fmt_time

RACE_ICONS = {"Protoss": "protoss.png", "Terran": "terran.png", "Zerg": "zerg.png"}

# 5.0.16 changed the starting worker count (12 -> 8) plus mineral/gas
# amounts, which shifts every early-game timing - games from before this
# are a different economy and skew comparisons if left in. Configurable via
# Settings since it'll need bumping again for the next econ-changing patch.
DEFAULT_PATCH_CUTOFF = "2026-06-22"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_column(db, table, column, coltype):
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT UNIQUE NOT NULL,
            map TEXT,
            my_race TEXT,
            opp_race TEXT,
            win INTEGER,
            first_supply_time INTEGER,
            first_gas_time INTEGER,
            second_base_time INTEGER,
            third_base_time INTEGER,
            supply_block_count INTEGER
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS pro_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            perspective INTEGER NOT NULL,
            map TEXT,
            my_race TEXT,
            opp_race TEXT,
            win INTEGER,
            first_supply_time INTEGER,
            first_gas_time INTEGER,
            second_base_time INTEGER,
            third_base_time INTEGER,
            supply_block_count INTEGER,
            UNIQUE(file_name, perspective)
        )
    """)

    _ensure_column(db, "games", "build_order_json", "TEXT")
    _ensure_column(db, "games", "played_at", "TEXT")
    _ensure_column(db, "pro_games", "build_order_json", "TEXT")
    _ensure_column(db, "pro_games", "played_at", "TEXT")

    db.commit()
    db.close()


def _find_source_replay(db, file_name):
    """Locate the .SC2Replay file backing a `games` row so we can backfill
    it. Direct uploads live in UPLOAD_DIR; rows added via "Scan folder"
    were never copied locally, so fall back to the saved folder setting."""
    candidate = os.path.join(UPLOAD_DIR, file_name)
    if os.path.exists(candidate):
        return candidate
    folder = get_setting_raw(db, "last_replay_folder")
    if folder:
        candidate = os.path.join(folder, file_name)
        if os.path.exists(candidate):
            return candidate
    return None


def get_setting_raw(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def backfill_build_data():
    """Rows created before build_order_json/played_at existed were also
    computed with the old buggy supply-detection logic (see analyzer.py -
    it used to read a unit's *current* type instead of its type at the
    time of the event, so e.g. a Terran depot that got lowered/raised could
    be missed entirely and a much later one picked up instead). So this
    isn't just filling in two new columns: every row missing build_order_json
    gets fully recomputed from its source file, overwriting the old stats
    with correct ones. Safe to run on every startup - only touches rows
    where build_order_json is still NULL, and any file that can't be
    found/parsed is left as-is."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    pro_rows = db.execute(
        "SELECT id, file_name, perspective FROM pro_games WHERE build_order_json IS NULL"
    ).fetchall()
    for row in pro_rows:
        path = os.path.join(PRO_UPLOAD_DIR, row["file_name"])
        if not os.path.exists(path):
            continue
        try:
            records = analyzer.extract_both_perspectives(path)
        except Exception:
            continue
        if row["perspective"] < len(records):
            record = records[row["perspective"]]
            db.execute(
                """UPDATE pro_games SET
                       map = ?, my_race = ?, opp_race = ?, win = ?,
                       first_supply_time = ?, first_gas_time = ?,
                       second_base_time = ?, third_base_time = ?, supply_block_count = ?,
                       build_order_json = ?, played_at = ?
                   WHERE id = ?""",
                (record["map"], record["my_race"], record["opp_race"], int(record["win"]),
                 record["first_supply_time"], record["first_gas_time"],
                 record["second_base_time"], record["third_base_time"], record["supply_block_count"],
                 json.dumps(record["build_order"]), record["played_at"], row["id"]),
            )

    my_names = get_setting_raw(db, "sc2_names", "")
    names = [n.strip() for n in my_names.split(",") if n.strip()]
    game_rows = db.execute(
        "SELECT id, file_name FROM games WHERE build_order_json IS NULL"
    ).fetchall()
    for row in game_rows:
        path = _find_source_replay(db, row["file_name"])
        if not path or not names:
            continue
        try:
            record = analyzer.extract_game_record(path, names)
        except Exception:
            continue
        if record is None:
            continue
        db.execute(
            """UPDATE games SET
                   map = ?, my_race = ?, opp_race = ?, win = ?,
                   first_supply_time = ?, first_gas_time = ?,
                   second_base_time = ?, third_base_time = ?, supply_block_count = ?,
                   build_order_json = ?, played_at = ?
               WHERE id = ?""",
            (record["map"], record["my_race"], record["opp_race"], int(record["win"]),
             record["first_supply_time"], record["first_gas_time"],
             record["second_base_time"], record["third_base_time"], record["supply_block_count"],
             json.dumps(record["build_order"]), record["played_at"], row["id"]),
        )

    db.commit()
    db.close()


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def get_my_names():
    names = get_setting("sc2_names", "")
    return [n.strip() for n in names.split(",") if n.strip()]


def get_patch_cutoff():
    return get_setting("patch_cutoff_date", DEFAULT_PATCH_CUTOFF).strip()


def is_pre_patch(played_at, cutoff):
    """played_at is an ISO timestamp string; cutoff is a YYYY-MM-DD date.
    Games with no known date are never excluded - we can't tell their patch,
    so we'd rather show them than silently drop data."""
    return bool(cutoff) and bool(played_at) and played_at < cutoff


@app.context_processor
def inject_globals():
    names = get_my_names()
    race_icon_urls = {r: url_for("static", filename=f"img/{f}") for r, f in RACE_ICONS.items()}
    return {
        "display_name": names[0] if names else None,
        "race_icons": RACE_ICONS,
        "race_icon_urls": race_icon_urls,
    }


def _insert_game(db, fname, record):
    db.execute(
        """INSERT INTO games
           (file_name, map, my_race, opp_race, win,
            first_supply_time, first_gas_time, second_base_time, third_base_time,
            supply_block_count, build_order_json, played_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fname, record["map"], record["my_race"], record["opp_race"], int(record["win"]),
         record["first_supply_time"], record["first_gas_time"],
         record["second_base_time"], record["third_base_time"], record["supply_block_count"],
         json.dumps(record["build_order"]), record["played_at"]),
    )


def _insert_pro_game(db, fname, perspective, record):
    db.execute(
        """INSERT INTO pro_games
           (file_name, perspective, map, my_race, opp_race, win,
            first_supply_time, first_gas_time, second_base_time, third_base_time,
            supply_block_count, build_order_json, played_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fname, perspective, record["map"], record["my_race"], record["opp_race"],
         int(record["win"]), record["first_supply_time"], record["first_gas_time"],
         record["second_base_time"], record["third_base_time"], record["supply_block_count"],
         json.dumps(record["build_order"]), record["played_at"]),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    message = None
    if request.method == "POST":
        names = request.form.get("sc2_names", "").strip()
        set_setting("sc2_names", names)
        cutoff = request.form.get("patch_cutoff_date", "").strip()
        set_setting("patch_cutoff_date", cutoff)
        message = "Saved."
    return render_template(
        "settings.html", message=message, sc2_names=get_setting("sc2_names"),
        patch_cutoff_date=get_patch_cutoff(),
    )


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/games")
def games():
    db = get_db()
    my_race = request.args.get("my_race", "")
    opp_race = request.args.get("opp_race", "")

    query = "SELECT * FROM games"
    conditions = []
    params = []
    if my_race:
        conditions.append("my_race = ?")
        params.append(my_race)
    if opp_race:
        conditions.append("opp_race = ?")
        params.append(opp_race)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY (played_at IS NULL) ASC, played_at DESC"

    cutoff = get_patch_cutoff()
    rows = db.execute(query, params).fetchall()
    my_games = [dict(row) for row in rows]
    for g in my_games:
        g["win"] = bool(g["win"])
        g["pre_patch"] = is_pre_patch(g["played_at"], cutoff)

    all_rows = db.execute("SELECT DISTINCT my_race, opp_race FROM games").fetchall()
    my_races = sorted({r["my_race"] for r in all_rows if r["my_race"]})
    opp_races = sorted({r["opp_race"] for r in all_rows if r["opp_race"]})

    return render_template(
        "games.html", games=my_games, my_races=my_races, opp_races=opp_races,
        selected_my_race=my_race, selected_opp_race=opp_race, patch_cutoff_date=cutoff,
    )


@app.route("/api/matchups")
def api_matchups():
    db = get_db()
    cutoff = get_patch_cutoff()

    rows = db.execute("SELECT * FROM games").fetchall()
    my_records = [dict(row) for row in rows]
    for r in my_records:
        r["win"] = bool(r["win"])
        r["file"] = r["file_name"]
        r["build_order"] = json.loads(r["build_order_json"]) if r["build_order_json"] else []
    excluded_count = sum(1 for r in my_records if is_pre_patch(r["played_at"], cutoff))
    my_records = [r for r in my_records if not is_pre_patch(r["played_at"], cutoff)]

    pro_rows = db.execute("SELECT * FROM pro_games").fetchall()
    pro_records = [dict(row) for row in pro_rows]
    for r in pro_records:
        r["win"] = bool(r["win"])
        r["file"] = r["file_name"]
        r["build_order"] = json.loads(r["build_order_json"]) if r["build_order_json"] else []
    pro_records = [r for r in pro_records if not is_pre_patch(r["played_at"], cutoff)]

    matchups = analyzer.compute_matchup_data(my_records, pro_records)

    trend_games = sorted(
        (r for r in my_records if r.get("played_at")),
        key=lambda r: r["played_at"],
    )
    trend = [{"played_at": r["played_at"], "win": r["win"]} for r in trend_games]

    return jsonify({
        "matchups": matchups,
        "trend": trend,
        "patch_cutoff_date": cutoff,
        "excluded_pre_patch_count": excluded_count,
    })


@app.route("/upload", methods=["GET", "POST"])
def upload():
    message = None
    my_names = get_my_names()

    if request.method == "POST":
        if not my_names:
            return redirect(url_for("settings"))

        files = request.files.getlist("replays")
        db = get_db()
        added, skipped, unmatched = 0, 0, 0

        for file in files:
            if not file.filename.lower().endswith(".sc2replay"):
                continue
            fname = secure_filename(file.filename)
            existing = db.execute("SELECT id FROM games WHERE file_name = ?", (fname,)).fetchone()
            if existing:
                skipped += 1
                continue

            save_path = os.path.join(UPLOAD_DIR, fname)
            file.save(save_path)

            record = analyzer.extract_game_record(save_path, my_names)
            if record is None:
                unmatched += 1
                continue

            _insert_game(db, fname, record)
            added += 1

        db.commit()
        message = f"Added {added} new games. Skipped {skipped} already-uploaded files."
        if unmatched:
            message += f" Couldn't match your name in {unmatched} files (check Settings)."

    default_folder = get_setting("last_replay_folder")
    return render_template("upload.html", message=message, my_names=my_names, default_folder=default_folder)


@app.route("/scan-folder", methods=["POST"])
def scan_folder():
    my_names = get_my_names()
    if not my_names:
        return redirect(url_for("settings"))

    folder = request.form.get("folder_path", "").strip()
    db = get_db()

    if not folder or not os.path.isdir(folder):
        message = f"Couldn't find that folder: {folder}"
    else:
        set_setting("last_replay_folder", folder)
        added, skipped, unmatched = 0, 0, 0
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith(".sc2replay"):
                continue
            existing = db.execute("SELECT id FROM games WHERE file_name = ?", (fname,)).fetchone()
            if existing:
                skipped += 1
                continue

            full_path = os.path.join(folder, fname)
            record = analyzer.extract_game_record(full_path, my_names)
            if record is None:
                unmatched += 1
                continue

            _insert_game(db, fname, record)
            added += 1
        db.commit()
        message = f"Scanned folder. Added {added} new games, skipped {skipped} already-uploaded."
        if unmatched:
            message += f" Couldn't match your name in {unmatched} files (check Settings)."

    default_folder = get_setting("last_replay_folder")
    return render_template("upload.html", message=message, my_names=my_names, default_folder=default_folder)


@app.route("/upload-pro", methods=["GET", "POST"])
def upload_pro():
    message = None
    if request.method == "POST":
        files = request.files.getlist("replays")
        db = get_db()
        added, skipped = 0, 0

        for file in files:
            if not file.filename.lower().endswith(".sc2replay"):
                continue
            fname = secure_filename(file.filename)
            save_path = os.path.join(PRO_UPLOAD_DIR, fname)
            file.save(save_path)

            records = analyzer.extract_both_perspectives(save_path)
            for i, record in enumerate(records):
                existing = db.execute(
                    "SELECT id FROM pro_games WHERE file_name = ? AND perspective = ?",
                    (fname, i),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                _insert_pro_game(db, fname, i, record)
                added += 1

        db.commit()
        message = f"Added {added} pro build-order records. Skipped {skipped} already in the pool."

    default_folder = get_setting("last_pro_folder")
    return render_template("upload_pro.html", message=message, default_folder=default_folder)


@app.route("/scan-folder-pro", methods=["POST"])
def scan_folder_pro():
    folder = request.form.get("folder_path", "").strip()
    db = get_db()

    if not folder or not os.path.isdir(folder):
        message = f"Couldn't find that folder: {folder}"
    else:
        set_setting("last_pro_folder", folder)
        added, skipped = 0, 0
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith(".sc2replay"):
                continue
            full_path = os.path.join(folder, fname)
            records = analyzer.extract_both_perspectives(full_path)
            for i, record in enumerate(records):
                existing = db.execute(
                    "SELECT id FROM pro_games WHERE file_name = ? AND perspective = ?",
                    (fname, i),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                _insert_pro_game(db, fname, i, record)
                added += 1
        db.commit()
        message = f"Scanned folder. Added {added} pro build-order records, skipped {skipped} already in the pool."

    default_folder = get_setting("last_pro_folder")
    return render_template("upload_pro.html", message=message, default_folder=default_folder)


if __name__ == "__main__":
    init_db()
    backfill_build_data()
    app.run(host="127.0.0.1", port=5000, debug=True)
