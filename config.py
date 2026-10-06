"""
Shared configuration. Everything machine-specific is overridable through
environment variables so nothing personal (account IDs, home-directory
paths, ...) needs to live in source control.

    SC2_DB_PATH       SQLite file                      (default: ./sc2.db)
    SC2_UPLOAD_DIR    where dropped-in replays are kept (default: ./uploads)
    SC2_PRO_DIR       the pro/benchmark replay pool     (default: ./pro_uploads)
    SC2_REPLAY_DIR    your own StarCraft II replay folder
                      (default: auto-detected under Documents\\StarCraft II)
    REDIS_URL         broker + event stream             (default: redis://localhost:6379/0)
"""

import glob
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.environ.get("SC2_DB_PATH", os.path.join(BASE_DIR, "sc2.db"))
UPLOAD_DIR = os.environ.get("SC2_UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
PRO_UPLOAD_DIR = os.environ.get("SC2_PRO_DIR", os.path.join(BASE_DIR, "pro_uploads"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PRO_UPLOAD_DIR, exist_ok=True)

# Games before this are on an older economy patch (starting workers 12 -> 8,
# new mineral/gas amounts), so their timings aren't comparable. Either a date
# (YYYY-MM-DD) or a game version (e.g. 5.0.16); editable in Settings.
DEFAULT_PATCH_CUTOFF = "2026-06-22"


def detect_replay_dir():
    """Best-effort guess at the user's own multiplayer replay folder, so a
    fresh install doesn't need a path pasted in. Returns '' if not found."""
    explicit = os.environ.get("SC2_REPLAY_DIR")
    if explicit:
        return explicit
    docs = os.path.join(os.path.expanduser("~"), "Documents", "StarCraft II", "Accounts")
    pattern = os.path.join(docs, "*", "*", "Replays", "Multiplayer")
    matches = [m for m in glob.glob(pattern) if os.path.isdir(m)]
    # If several accounts exist, prefer the folder with the most recent replay.
    def newest(folder):
        try:
            return max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder))
        except ValueError:
            return 0
    return max(matches, key=newest) if matches else ""
