"""
Replay -> records, and records -> matchup statistics.

`extract_*` turn a binary .SC2Replay into plain dicts (build-order milestones,
the early build order, plus the telemetry metrics from telemetry.py).
`compute_matchup_data` turns many such dicts into the numbers the dashboard
shows, including the "recommended build from the pro pool" and "what pros do
that you don't" comparisons.

All times are real in-game seconds on the Faster clock (frame / fps). sc2reader's
own `event.second` counts 16 frames/s no matter the game speed, so on Faster it
reads about 1.4x too large - we never use it.
"""

import os
import re
import statistics
from collections import defaultdict

import sc2reader

import coaching
import telemetry

SUPPLY_UNIT_BY_RACE = {
    "Terran": "SupplyDepot",
    "Protoss": "Pylon",
    "Zerg": "Overlord",
}

TOWNHALL_NAMES = telemetry.TOWNHALL_NAMES
GAS_NAMES = {"Assimilator", "Refinery", "Extractor"}

METRICS = [
    ("First supply", "first_supply_time"),
    ("First gas", "first_gas_time"),
    ("2nd base", "second_base_time"),
    ("3rd base", "third_base_time"),
]

# Telemetry-derived numbers shown as wins / losses / pro comparisons.
TELEMETRY_METRICS = [
    ("apm_combat", "APM in fights"),
    ("apm_macro", "APM outside fights"),
    ("apm_retention", "APM retention (fight / calm)"),
    ("macro_retention", "Macro retention (fight / calm)"),
    ("float_penalty", "Minerals floated above 400"),
    ("float_pct", "% of time floating 800+"),
    ("fight_count", "Fights"),
    ("workers_4m", "Workers at 4:00"),
    ("workers_6m", "Workers at 6:00"),
    ("workers_8m", "Workers at 8:00"),
    ("mech_efficiency", "Macro mechanic usage"),
]

DEVIATION_THRESHOLD_SECONDS = 15
FLOAT_DEVIATION = 300          # minerals
RETENTION_DEVIATION = 0.15     # ratio points
WORKER_DEVIATION = 6           # workers at 6:00
MECH_DEVIATION = 0.2           # usage ratio points

# Only these count as real ladder/melee races. Co-op commander missions and
# Arcade maps can report custom race strings.
VALID_RACES = {"Terran", "Protoss", "Zerg"}

# AI opponents in Co-op Missions and some Arcade maps are named like
# "Amon's Forces" rather than a real player name.
_AI_NAME_SUFFIX = "'s forces"

# Pre-game cosmetics (sprays, portrait "dance" previews, ...) surface as
# UpgradeCompleteEvent at frame 0 - not build-order information.
COSMETIC_UPGRADE_PREFIXES = ("Spray", "RewardDance", "GameHeart", "Decal", "Portrait", "VoiceLine", "Skin")

# How much of the game the step-by-step build order covers.
BUILD_ORDER_CAP_SECONDS = 600
BUILD_ORDER_MAX_ITEMS = 30
SUPPLY_BLOCK_WINDOW_SECONDS = 720

GUIDE_MIN_FREQUENCY = 0.4
DIFF_MIN_FREQUENCY = 0.3
DIFF_MIN_GAMES = 3
TIMING_DIFF_THRESHOLD_SECONDS = 15

# A few upgrade internal names aren't CamelCase like unit/structure names.
NAME_OVERRIDES = {
    "zerglingmovementspeed": "Zergling Movement Speed",
}


def prettify_name(name):
    """'CyberneticsCore' -> 'Cybernetics Core'"""
    if not name:
        return name
    if name in NAME_OVERRIDES:
        return NAME_OVERRIDES[name]
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


def _is_ladder_game(me, opp):
    """True if both sides are real humans on a standard race - i.e. a 1v1 whose
    timings are comparable. Co-op/Arcade missions and games against the built-in
    AI (often on custom maps with their own economy) are left out."""
    if me.play_race not in VALID_RACES or opp.play_race not in VALID_RACES:
        return False
    for player in (me, opp):
        if not getattr(player, "is_human", True):
            return False
        if (player.name or "").strip().lower().endswith(_AI_NAME_SUFFIX):
            return False
    return True


# =============================================================================
# Replay -> record
# =============================================================================

def _milestones_and_build_order(replay, me, fps):
    """First supply / gas / 2nd & 3rd base times, supply blocks, and the early
    build order (structures + real upgrades) from one player's perspective."""
    out = {
        "first_supply_time": None, "first_gas_time": None,
        "second_base_time": None, "third_base_time": None,
        "supply_block_count": 0,
    }
    supply_unit = SUPPLY_UNIT_BY_RACE.get(me.play_race)
    townhall_times = []
    build_order = []

    for event in replay.tracker_events:
        kind = type(event).__name__
        t = event.frame / fps

        if kind == "PlayerStatsEvent":
            # Being within 1 supply of your cap early on is a rough proxy for
            # getting supply blocked (made == 200 is just maxed out).
            if event.pid == me.pid and t <= SUPPLY_BLOCK_WINDOW_SECONDS:
                used, made = event.food_used, event.food_made
                if used is not None and made is not None and made < 200 and (made - used) <= 1:
                    out["supply_block_count"] += 1
            continue

        if kind == "UpgradeCompleteEvent":
            if event.player is not None and event.player.pid == me.pid and t <= BUILD_ORDER_CAP_SECONDS:
                name = getattr(event, "upgrade_type_name", None)
                if name and not name.startswith(COSMETIC_UPGRADE_PREFIXES):
                    build_order.append({"time": round(t), "kind": "upgrade", "name": name})
            continue

        unit = getattr(event, "unit", None)
        owner = getattr(unit, "owner", None) if unit else None
        if owner is None or owner.pid != me.pid:
            continue

        # The event's own unit_type_name is the type *at that instant*.
        # `unit.name` is the unit's *final* type, so a depot that was later
        # lowered reads "SupplyDepotLowered" and a Gateway that later warped
        # reads "WarpGate" - both silently break the checks below.
        # (UnitDoneEvent has no unit_type_name; there we only ever test
        # membership in sets that already include every final form.)
        name = getattr(event, "unit_type_name", None) or unit.name

        if kind in ("UnitInitEvent", "UnitBornEvent", "UnitDoneEvent"):
            # frame 0 births are the starting units (a Zerg's first Overlord),
            # not something the player built.
            if name == supply_unit and event.frame > 0 and out["first_supply_time"] is None:
                out["first_supply_time"] = round(t)

        if kind == "UnitInitEvent":
            if name in GAS_NAMES and out["first_gas_time"] is None:
                out["first_gas_time"] = round(t)
            if unit.is_building and t <= BUILD_ORDER_CAP_SECONDS and name:
                build_order.append({"time": round(t), "kind": "structure", "name": name})

        if kind == "UnitDoneEvent" and name in TOWNHALL_NAMES:
            townhall_times.append(round(t))

    townhall_times.sort()
    if len(townhall_times) >= 2:
        out["second_base_time"] = townhall_times[1]
    if len(townhall_times) >= 3:
        out["third_base_time"] = townhall_times[2]

    build_order.sort(key=lambda item: item["time"])
    out["build_order"] = build_order[:BUILD_ORDER_MAX_ITEMS]
    return out


def _played_at(replay):
    start = getattr(replay, "start_time", None) or getattr(replay, "date", None)
    return start.isoformat() if start else None


def _records_for(replay, pairs):
    """Build one record per (me, opp) pair. Telemetry is analysed once for the
    whole replay and sliced per perspective."""
    fps = telemetry.fps_for(replay)
    pids = sorted({p.pid for pair in pairs for p in pair})
    analysis = telemetry.analyze_replay(replay, pids)

    records = []
    for me, opp in pairs:
        record = {
            "map": replay.map_name,
            "my_race": me.play_race,
            "opp_race": opp.play_race,
            "win": me.result == "Win",
            "played_at": _played_at(replay),
            "game_version": getattr(replay, "release_string", None),
            "game_length": round(analysis["length"]),
        }
        record.update(_milestones_and_build_order(replay, me, fps))
        metrics, tel = telemetry.player_view(analysis, me.pid, opp.pid)
        record.update(metrics)
        record["telemetry"] = tel
        records.append(record)
    return records


def normalize_names(names):
    """'Player#1234' and 'player' both match a player called 'Player'."""
    out = set()
    for n in names:
        n = n.strip().lower()
        if n:
            out.add(n)
            out.add(n.split("#", 1)[0])
    return out


def find_player_by_name(replay, names):
    wanted = normalize_names(names)
    for player in replay.players:
        if (player.name or "").lower() in wanted:
            return player
        detail = getattr(player, "detail_data", None) or {}
        tag = getattr(player, "detail_tag", None)
        if tag and tag.lower() in wanted:
            return player
        if isinstance(detail, dict) and str(detail.get("name", "")).lower() in wanted:
            return player
    return None


FULL_LOAD = 4        # tracker + game events: the expensive part
PLAYERS_ONLY = 2     # header + player list: enough to decide if a replay qualifies


def load_replay(path, level=FULL_LOAD):
    try:
        return sc2reader.load_replay(path, load_level=level)
    except Exception:
        return None


def extract_game_record(path, my_names):
    """One of *your* replays: find you by name, return your record or None
    (not a 1v1, not a ladder game, or your name isn't in it).

    Most files in a replay folder are co-op / arcade / team games that get
    rejected, so qualify the replay from the cheap player list first and only
    pay for the full event stream once it's known to be a keeper."""
    probe = load_replay(path, PLAYERS_ONLY)
    if probe is None:
        return None
    me = find_player_by_name(probe, my_names)
    if me is None:
        return None
    opponents = [p for p in probe.players if p != me]
    if len(opponents) != 1 or not _is_ladder_game(me, opponents[0]):
        return None

    replay = load_replay(path)
    if replay is None:
        return None
    me = find_player_by_name(replay, my_names)
    opp = next(p for p in replay.players if p != me)
    record = _records_for(replay, [(me, opp)])[0]
    record["file"] = os.path.basename(path)
    return record


def extract_both_perspectives(path):
    """Pro pool: no name matching, both players' records so either race side
    works as a benchmark."""
    probe = load_replay(path, PLAYERS_ONLY)
    if probe is None or len(probe.players) != 2 or not _is_ladder_game(*probe.players):
        return []
    replay = load_replay(path)
    if replay is None:
        return []
    p1, p2 = replay.players
    records = _records_for(replay, [(p1, p2), (p2, p1)])
    for record in records:
        record["file"] = os.path.basename(path)
    return records


# =============================================================================
# Patch filtering
# =============================================================================

def _version_tuple(text):
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:3])


def is_pre_patch(game, cutoff):
    """True if `game` predates the patch cutoff. `cutoff` is a date
    (YYYY-MM-DD) or a game version (5.0.16); a game with no known date or
    version is never excluded - better shown than silently dropped."""
    cutoff = (cutoff or "").strip()
    if not cutoff:
        return False
    if re.fullmatch(r"\d+\.\d+(\.\d+)?", cutoff):
        version = _version_tuple(game.get("game_version"))
        return bool(version) and version < _version_tuple(cutoff)
    played = game.get("played_at")
    return bool(played) and played < cutoff


# =============================================================================
# Aggregation
# =============================================================================

def fmt_time(seconds):
    if seconds is None:
        return "n/a"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def median_or_none(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def mean_or_none(values, digits=1):
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), digits) if vals else None


def _indexed_build_steps(build_order):
    """(name, occurrence#, time): the 2nd Gateway of a game is a different
    step from the 1st, so games line up on 'the Nth X'."""
    counts = defaultdict(int)
    steps = []
    for item in build_order or []:
        counts[item["name"]] += 1
        steps.append((item["name"], counts[item["name"]], item["time"]))
    return steps


def aggregate_build_steps(games, min_frequency=GUIDE_MIN_FREQUENCY):
    """A representative build for a pool of games: steps that show up in at
    least `min_frequency` of them, at their median time, in time order."""
    with_data = [g for g in games if g.get("build_order")]
    total = len(with_data)
    if not total:
        return []
    step_times = defaultdict(list)
    for g in with_data:
        for name, occurrence, t in _indexed_build_steps(g["build_order"]):
            step_times[(name, occurrence)].append(t)

    guide = []
    for (name, occurrence), times in step_times.items():
        frequency = len(times) / total
        if frequency >= min_frequency:
            guide.append({
                "name": name, "label": prettify_name(name), "occurrence": occurrence,
                "time": median_or_none(times), "frequency": round(frequency, 2),
            })
    guide.sort(key=lambda s: s["time"])
    return guide[:15]


def compare_build_steps(user_games, pro_guide, min_frequency=DIFF_MIN_FREQUENCY):
    """For each step in the pro guide: do you do it at all, and if so, is your
    timing meaningfully off? Returns difference records in guide order."""
    with_data = [g for g in user_games if g.get("build_order")]
    total = len(with_data)
    if total < DIFF_MIN_GAMES or not pro_guide:
        return []

    user_times = defaultdict(list)
    for g in with_data:
        for name, occurrence, t in _indexed_build_steps(g["build_order"]):
            user_times[(name, occurrence)].append(t)

    differences = []
    for step in pro_guide:
        times = user_times.get((step["name"], step["occurrence"]), [])
        frequency = len(times) / total
        if frequency < min_frequency:
            differences.append({
                "type": "missing", "label": step["label"], "occurrence": step["occurrence"],
                "pro_time": step["time"], "pro_frequency": step["frequency"],
                "user_frequency": round(frequency, 2),
            })
            continue
        user_median = median_or_none(times)
        diff = user_median - step["time"]
        if abs(diff) >= TIMING_DIFF_THRESHOLD_SECONDS:
            differences.append({
                "type": "timing", "label": step["label"], "occurrence": step["occurrence"],
                "pro_time": step["time"], "user_time": user_median, "diff": round(diff),
            })
    return differences[:10]


def _flag_notes(game, stats, tele_win_means, avg_block_win):
    notes = []
    for label, key in METRICS:
        win_med, val = stats[key]["win"], game.get(key)
        if win_med is not None and val is not None:
            diff = val - win_med
            if abs(diff) >= DEVIATION_THRESHOLD_SECONDS:
                notes.append({
                    "label": label, "direction": "later" if diff > 0 else "earlier",
                    "diff": abs(int(diff)), "val": fmt_time(val), "win_med": fmt_time(win_med),
                    "unit": "s",
                })
    blocks = game.get("supply_block_count")
    if avg_block_win is not None and blocks is not None and blocks - avg_block_win >= 2:
        notes.append({
            "label": "Supply blocks", "direction": "more often",
            "diff": round(blocks - avg_block_win, 1), "val": str(blocks),
            "win_med": str(avg_block_win), "unit": "",
        })

    fl, fl_win = game.get("float_penalty"), tele_win_means.get("float_penalty")
    if fl is not None and fl_win is not None and fl - fl_win >= FLOAT_DEVIATION:
        notes.append({
            "label": "Floating minerals", "direction": "more",
            "diff": round(fl - fl_win), "val": str(round(fl)), "win_med": str(round(fl_win)),
            "unit": " minerals",
        })
    wk, wk_win = game.get("workers_6m"), tele_win_means.get("workers_6m")
    if wk is not None and wk_win is not None and wk_win - wk >= WORKER_DEVIATION:
        notes.append({
            "label": "Workers at 6:00", "direction": "fewer",
            "diff": round(wk_win - wk), "val": str(wk), "win_med": str(round(wk_win)), "unit": " workers",
        })
    mech, mech_win = game.get("mech_efficiency"), tele_win_means.get("mech_efficiency")
    if mech is not None and mech_win is not None and mech_win - mech >= MECH_DEVIATION:
        notes.append({
            "label": "Macro mechanic usage", "direction": "lower",
            "diff": round(100 * (mech_win - mech)), "val": f"{round(100 * mech)}%",
            "win_med": f"{round(100 * mech_win)}%", "unit": " points",
        })
    for key, label in (("apm_retention", "APM retention in fights"), ("macro_retention", "Macro retention in fights")):
        v, base = game.get(key), tele_win_means.get(key)
        if v is not None and base is not None and base - v >= RETENTION_DEVIATION:
            notes.append({
                "label": label, "direction": "lower",
                "diff": round(100 * (base - v)), "val": f"{round(100 * v)}%",
                "win_med": f"{round(100 * base)}%", "unit": " points",
            })
    return notes


def compute_matchup_data(records, pro_records=None):
    by_matchup = defaultdict(list)
    for r in records:
        by_matchup[(r["my_race"], r["opp_race"])].append(r)

    by_matchup_pro = defaultdict(list)
    for r in pro_records or []:
        by_matchup_pro[(r["my_race"], r["opp_race"])].append(r)

    matchups = []
    for (my_race, opp_race), games in sorted(by_matchup.items()):
        wins = [g for g in games if g["win"]]
        losses = [g for g in games if not g["win"]]
        pro_games = by_matchup_pro.get((my_race, opp_race), [])

        stats = {}
        for label, key in METRICS:
            stats[key] = {
                "label": label,
                "overall": median_or_none([g.get(key) for g in games]),
                "win": median_or_none([g.get(key) for g in wins]),
                "loss": median_or_none([g.get(key) for g in losses]),
                "pro": median_or_none([g.get(key) for g in pro_games]) if pro_games else None,
            }

        tele = {}
        for key, label in TELEMETRY_METRICS:
            tele[key] = {
                "label": label,
                "win": mean_or_none([g.get(key) for g in wins], 2),
                "loss": mean_or_none([g.get(key) for g in losses], 2),
                "pro": mean_or_none([g.get(key) for g in pro_games], 2) if pro_games else None,
            }

        map_counts = defaultdict(lambda: {"wins": 0, "losses": 0})
        for g in games:
            map_counts[g.get("map") or "Unknown"]["wins" if g["win"] else "losses"] += 1
        map_stats = sorted(
            ({"map": m, "wins": v["wins"], "losses": v["losses"], "total": v["wins"] + v["losses"]}
             for m, v in map_counts.items()),
            key=lambda x: x["total"], reverse=True,
        )

        flagged = []
        if wins:
            avg_block_win = mean_or_none([g.get("supply_block_count") for g in wins])
            win_means = {k: tele[k]["win"] for k, _ in TELEMETRY_METRICS}
            for g in losses:
                notes = _flag_notes(g, stats, win_means, avg_block_win)
                if notes:
                    flagged.append({"file": g["file"], "map": g["map"], "notes": notes})

        guide = aggregate_build_steps(pro_games) if len(pro_games) >= 3 else []

        matchups.append({
            "my_race": my_race, "opp_race": opp_race,
            "game_count": len(games), "win_count": len(wins), "loss_count": len(losses),
            "has_pro": bool(pro_games), "pro_count": len(pro_games),
            "stats": stats, "telemetry": tele,
            "mech_label": telemetry.MECH_LABEL.get(my_race, "Macro mechanic usage"),
            "focus": coaching.rank_focus(wins, losses, pro_games, my_race),
            "flagged": flagged, "map_stats": map_stats,
            "maps": sorted(map_counts),
            "avg_supply_blocks_win": mean_or_none([g.get("supply_block_count") for g in wins]),
            "avg_supply_blocks_loss": mean_or_none([g.get("supply_block_count") for g in losses]),
            "pro_build_guide": guide,
            "build_differences": compare_build_steps(games, guide) if guide else [],
        })
    return matchups
