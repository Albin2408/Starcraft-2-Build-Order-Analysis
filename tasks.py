"""
Background jobs. One Celery task parses one replay; a scan (or a drag-and-drop
of 50 files) is fanned out as that many independent tasks, so the work spreads
over every worker that's running and one bad file can't sink the rest.

Each finished replay reports to Redis (events.py), which is what the browser's
progress bar follows.
"""

import hashlib
import os
import sqlite3

import analyzer
import db
import events
from celery_app import celery_app
from config import PRO_UPLOAD_DIR, UPLOAD_DIR

TABLES = {"mine": "games", "pro": "pro_games"}


def list_replays(folder):
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".sc2replay"))


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the actual work ----------------------------------------------------------

def _discard_if_uploaded(path):
    """A duplicate that arrived through the upload endpoint is just a second
    copy on disk - drop it. Anything outside our own uploads folder (e.g. a
    replay being scanned in place from the game's folder) is never touched."""
    try:
        if os.path.commonpath([os.path.abspath(path), os.path.abspath(UPLOAD_DIR)]) == os.path.abspath(UPLOAD_DIR):
            os.remove(path)
    except (ValueError, OSError):
        pass


def process_replay(conn, kind, path, my_names, force=False):
    """Parse one replay into the database. Returns (outcome, detail)."""
    table = TABLES[kind]
    name = os.path.basename(path)
    state = db.known_file_state(conn, table, name)
    if state and state[0] == db.PARSER_VERSION and not force:
        return "skipped", "already analysed"

    digest = file_sha1(path)
    if kind == "mine" and not state:
        # same replay dropped in under a different file name
        dup = conn.execute(
            "SELECT file_name FROM games WHERE file_hash = ? AND file_name != ?", (digest, name)
        ).fetchone()
        if dup:
            _discard_if_uploaded(path)
            return "skipped", f"same replay as {dup['file_name']}"

    if kind == "mine":
        record = analyzer.extract_game_record(path, my_names)
        if record is None:
            if state:                      # qualified under an older rule set; doesn't now
                db.delete_game(conn, kind, name)
                conn.commit()
            return "unmatched", "not a human 1v1 with one of your names"
        db.upsert_game(conn, record, name, source_path=path, file_hash=digest)
    else:
        records = analyzer.extract_both_perspectives(path)
        if not records:
            if state:
                db.delete_game(conn, kind, name)
                conn.commit()
            return "unmatched", "not a readable human 1v1"
        for perspective, record in enumerate(records):
            db.upsert_pro_game(conn, record, name, perspective, source_path=path, file_hash=digest)

    conn.commit()
    return ("updated" if state else "added"), ""


@celery_app.task(
    name="sc2.parse_replay", bind=True,
    autoretry_for=(sqlite3.OperationalError,),   # DB briefly locked by another writer
    retry_backoff=2, retry_jitter=True, max_retries=5,
)
def parse_replay(self, batch_id, kind, path, my_names=None, force=False):
    name = os.path.basename(path)
    conn = db.get_conn()
    try:
        outcome, detail = process_replay(conn, kind, path, my_names or [], force)
    except sqlite3.OperationalError:
        raise                                    # let Celery retry with backoff
    except Exception as exc:                     # a corrupt replay must not stall the batch
        conn.rollback()
        events.record(batch_id, "failed", name, f"{type(exc).__name__}: {exc}"[:300])
        return
    finally:
        conn.close()
    events.record(batch_id, outcome, name, detail)


@celery_app.task(name="sc2.scan_folder")
def scan_folder(batch_id, kind, folder, my_names=None, force=False):
    """List a folder and fan its replays out as individual parse tasks."""
    files = list_replays(folder)
    events.set_total(batch_id, len(files))
    for fname in files:
        parse_replay.delay(batch_id, kind, os.path.join(folder, fname), my_names, force)


# --- helpers the web app calls ---------------------------------------------------

def start_scan(kind, folder, my_names=None, force=False):
    batch_id = events.create_batch(kind)            # total unknown until listed
    scan_folder.delay(batch_id, kind, folder, my_names, force)
    return batch_id


def start_files(kind, paths, my_names=None, force=False):
    batch_id = events.create_batch(kind, total=len(paths))
    for path in paths:
        parse_replay.delay(batch_id, kind, path, my_names, force)
    return batch_id


def _resolve_source(conn, kind, row):
    """Find the replay file behind an existing row so it can be re-parsed."""
    candidates = []
    if row["source_path"]:
        candidates.append(row["source_path"])
    name = row["file_name"]
    if kind == "pro":
        candidates.append(os.path.join(PRO_UPLOAD_DIR, name))
        candidates.append(os.path.join(db.get_setting(conn, "last_pro_folder"), name))
    else:
        candidates.append(os.path.join(UPLOAD_DIR, name))
        candidates.append(os.path.join(db.get_setting(conn, "last_replay_folder"), name))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def start_reprocess(conn, my_names):
    """Queue every row produced by an older parser for a rebuild from its
    source replay. Returns (batch_id or None, queued, missing_files)."""
    jobs, missing = [], 0
    for kind, table in TABLES.items():
        seen = set()
        rows = conn.execute(
            f"SELECT DISTINCT file_name, source_path FROM {table} "
            f"WHERE parser_version IS NULL OR parser_version < ?", (db.PARSER_VERSION,)
        ).fetchall()
        for row in rows:
            path = _resolve_source(conn, kind, row)
            if path is None:
                missing += 1
            elif path not in seen:
                seen.add(path)
                jobs.append((kind, path))
    if not jobs:
        return None, 0, missing

    batch_id = events.create_batch("reprocess", total=len(jobs))
    for kind, path in jobs:
        parse_replay.delay(batch_id, kind, path, my_names, True)
    return batch_id, len(jobs), missing
