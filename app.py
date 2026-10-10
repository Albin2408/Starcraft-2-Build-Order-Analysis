"""
Flask front end. It never parses a replay itself: uploads and folder scans are
handed to Celery (tasks.py), and the browser follows progress over Server-Sent
Events fed by a Redis Stream (events.py).
"""

import json
import os
import uuid

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, stream_with_context, url_for
from werkzeug.utils import secure_filename

import analyzer
import db
import events
import heatmaps
import tasks
from config import DEFAULT_PATCH_CUTOFF, PRO_UPLOAD_DIR, UPLOAD_DIR, detect_replay_dir

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024      # a big batch of replays
app.jinja_env.globals["fmt_time"] = analyzer.fmt_time

RACE_ICONS = {"Protoss": "protoss.png", "Terran": "terran.png", "Zerg": "zerg.png"}
KINDS = ("mine", "pro")

db.init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = db.get_conn()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def get_my_names():
    names = db.get_setting(get_db(), "sc2_names", "")
    return [n.strip() for n in names.split(",") if n.strip()]


def get_patch_cutoff():
    return db.get_setting(get_db(), "patch_cutoff_date", DEFAULT_PATCH_CUTOFF).strip()


@app.context_processor
def inject_globals():
    names = get_my_names()
    return {
        "display_name": names[0].split("#")[0] if names else None,
        "race_icons": RACE_ICONS,
        "race_icon_urls": {r: url_for("static", filename=f"img/{f}") for r, f in RACE_ICONS.items()},
    }


def _redis_or_error():
    """None if Redis is reachable, else a (json, status) error response."""
    if events.ping():
        return None
    return jsonify({"error": "Redis isn't reachable, so replays can't be queued. "
                             "Start it (see the README) and try again."}), 503


def _load_games(conn, table, cutoff):
    rows = [db.inflate(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    kept = [r for r in rows if not analyzer.is_pre_patch(r, cutoff)]
    return kept, len(rows) - len(kept)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    message = None
    conn = get_db()
    if request.method == "POST":
        db.set_setting(conn, "sc2_names", request.form.get("sc2_names", "").strip())
        db.set_setting(conn, "patch_cutoff_date", request.form.get("patch_cutoff_date", "").strip())
        message = "Saved."
    return render_template("settings.html", message=message,
                           sc2_names=db.get_setting(conn, "sc2_names"), patch_cutoff_date=get_patch_cutoff())


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/games")
def games():
    conn = get_db()
    source = request.args.get("source", "mine")
    if source not in KINDS:
        abort(400)
    table = tasks.TABLES[source]
    my_race = request.args.get("my_race", "")
    opp_race = request.args.get("opp_race", "")

    query, conditions, params = f"SELECT * FROM {table}", [], []
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
    my_games = [db.inflate(r) for r in conn.execute(query, params).fetchall()]
    for g_ in my_games:
        g_["pre_patch"] = analyzer.is_pre_patch(g_, cutoff)

    races = conn.execute(f"SELECT DISTINCT my_race, opp_race FROM {table}").fetchall()
    return render_template(
        "games.html", games=my_games, source=source,
        my_races=sorted({r["my_race"] for r in races if r["my_race"]}),
        opp_races=sorted({r["opp_race"] for r in races if r["opp_race"]}),
        selected_my_race=my_race, selected_opp_race=opp_race, patch_cutoff_date=cutoff,
    )


@app.route("/game/<source>/<int:game_id>")
def game_detail(source, game_id):
    if source not in KINDS:
        abort(404)
    return render_template("game.html", source=source, game_id=game_id)


@app.route("/upload")
def upload():
    conn = get_db()
    folder = db.get_setting(conn, "last_replay_folder") or detect_replay_dir()
    return render_template("upload.html", kind="mine", my_names=get_my_names(), default_folder=folder)


@app.route("/upload-pro")
def upload_pro():
    conn = get_db()
    folder = db.get_setting(conn, "last_pro_folder") or PRO_UPLOAD_DIR
    return render_template("upload.html", kind="pro", my_names=get_my_names(), default_folder=folder)


# ---------------------------------------------------------------------------
# Data APIs
# ---------------------------------------------------------------------------

@app.route("/api/matchups")
def api_matchups():
    conn = get_db()
    cutoff = get_patch_cutoff()
    my_records, excluded = _load_games(conn, "games", cutoff)
    pro_records, _ = _load_games(conn, "pro_games", cutoff)

    trend = [{"played_at": r["played_at"], "win": r["win"]}
             for r in sorted((r for r in my_records if r.get("played_at")), key=lambda r: r["played_at"])]

    return jsonify({
        "matchups": analyzer.compute_matchup_data(my_records, pro_records),
        "trend": trend,
        "patch_cutoff_date": cutoff,
        "excluded_pre_patch_count": excluded,
        "stale": db.stale_counts(conn),
    })


@app.route("/api/heatmap")
def api_heatmap():
    """Time-sliced heatmap grids for a matchup. Query: source=mine|pro,
    my_race, opp_race, outcome=win|loss|all, map, orient=1|0."""
    conn = get_db()
    source = request.args.get("source", "mine")
    if source not in KINDS:
        abort(400)
    table = tasks.TABLES[source]
    cutoff = get_patch_cutoff()

    clauses, params = [], []
    for column, arg in (("my_race", "my_race"), ("opp_race", "opp_race"), ("map", "map")):
        value = request.args.get(arg)
        if value:
            clauses.append(f"g.{column} = ?")
            params.append(value)
    outcome = request.args.get("outcome", "all")
    if outcome in ("win", "loss"):
        clauses.append("g.win = ?")
        params.append(1 if outcome == "win" else 0)

    join_perspective = "AND t.perspective = g.perspective" if source == "pro" else "AND t.perspective = 0"
    rows = conn.execute(
        f"SELECT t.data, g.played_at, g.game_version FROM {table} g "
        f"JOIN telemetry t ON t.source = ? AND t.file_name = g.file_name {join_perspective} "
        + ("WHERE " + " AND ".join(clauses) if clauses else ""),
        [source] + params,
    ).fetchall()

    telemetries = [json.loads(r["data"]) for r in rows if not analyzer.is_pre_patch(dict(r), cutoff)]
    result = heatmaps.aggregate(telemetries, orient=request.args.get("orient", "1") != "0")
    return jsonify(result)


@app.route("/api/game/<source>/<int:game_id>")
def api_game(source, game_id):
    if source not in KINDS:
        abort(404)
    conn = get_db()
    table = tasks.TABLES[source]
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (game_id,)).fetchone()
    if row is None:
        abort(404)
    record = db.inflate(row)
    perspective = record.get("perspective", 0)
    record.pop("build_order_json", None)
    return jsonify({
        "game": record,
        "telemetry": db.load_telemetry(conn, source, record["file_name"], perspective),
    })


# ---------------------------------------------------------------------------
# Queueing: uploads, folder scans, rebuilds
# ---------------------------------------------------------------------------

def _require_names_for(kind):
    if kind == "mine" and not get_my_names():
        return jsonify({"error": "Set your in-game name(s) in Settings first."}), 400
    return None


def _save_upload(file_storage, directory):
    """Store an uploaded replay under its own name, or - if a *different*
    replay already has that name - under name-<hash>. Returns the final path."""
    name = secure_filename(file_storage.filename)
    temp = os.path.join(directory, f".incoming-{uuid.uuid4().hex}")
    file_storage.save(temp)
    digest = tasks.file_sha1(temp)
    final = os.path.join(directory, name)
    if os.path.exists(final):
        if tasks.file_sha1(final) == digest:
            os.remove(temp)
            return final
        stem, ext = os.path.splitext(name)
        final = os.path.join(directory, f"{stem}-{digest[:8]}{ext}")
    os.replace(temp, final)
    return final


@app.route("/api/upload/<kind>", methods=["POST"])
def api_upload(kind):
    if kind not in KINDS:
        abort(404)
    for check in (_require_names_for(kind), _redis_or_error()):
        if check:
            return check

    directory = UPLOAD_DIR if kind == "mine" else PRO_UPLOAD_DIR
    files = [f for f in request.files.getlist("replays") if f.filename.lower().endswith(".sc2replay")]
    ignored = len(request.files.getlist("replays")) - len(files)
    if not files:
        return jsonify({"error": "No .SC2Replay files in that drop."}), 400

    paths = [_save_upload(f, directory) for f in files]
    batch_id = tasks.start_files(kind, paths, get_my_names() if kind == "mine" else None)
    return jsonify({"batch_id": batch_id, "total": len(paths), "ignored": ignored})


@app.route("/api/scan/<kind>", methods=["POST"])
def api_scan(kind):
    if kind not in KINDS:
        abort(404)
    for check in (_require_names_for(kind), _redis_or_error()):
        if check:
            return check

    folder = (request.form.get("folder_path") or "").strip().strip('"')
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": f"Couldn't find that folder: {folder or '(empty)'}"}), 400

    db.set_setting(get_db(), "last_replay_folder" if kind == "mine" else "last_pro_folder", folder)
    force = request.form.get("force") in ("1", "true", "on")      # re-analyse even up-to-date replays
    batch_id = tasks.start_scan(kind, folder, get_my_names() if kind == "mine" else None, force)
    return jsonify({"batch_id": batch_id})


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    if (err := _redis_or_error()):
        return err
    batch_id, queued, missing = tasks.start_reprocess(get_db(), get_my_names())
    return jsonify({"batch_id": batch_id, "queued": queued, "missing_files": missing})


@app.route("/api/status")
def api_status():
    return jsonify({"redis": events.ping(), "stale": db.stale_counts(get_db())})


# --- progress ------------------------------------------------------------------

@app.route("/api/batches/<batch_id>")
def api_batch(batch_id):
    state = events.snapshot(batch_id)
    if state is None:
        abort(404)
    return jsonify(state)


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/api/batches/<batch_id>/stream")
def api_batch_stream(batch_id):
    """Server-Sent Events: a `file` event per replay as workers finish it
    (tailing the Redis Stream with a blocking XREAD) and a `state` event with
    the running counters after each wake-up, until the batch is complete."""
    if events.snapshot(batch_id) is None:
        abort(404)

    @stream_with_context
    def generate():
        last_id = "0"
        while True:
            for entry_id, fields in events.read_events(batch_id, last_id, block_ms=5000):
                last_id = entry_id
                yield _sse("file", fields)
            state = events.snapshot(batch_id)
            yield _sse("state", state)
            if state is None or state["finished"]:
                yield _sse("done", state)
                return

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
