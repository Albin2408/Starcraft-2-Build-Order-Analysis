"""
Core replay-parsing and stats logic, shared by the Flask app.
This is the same extraction logic as the standalone script, refactored
so it can be reused for both "my games" (matched by name) and the
shared pro pool (both players' perspectives, no name matching needed).
"""

import os
import re
import statistics
from collections import defaultdict

import sc2reader

SUPPLY_UNIT_BY_RACE = {
    "Terran": "SupplyDepot",
    "Protoss": "Pylon",
    "Zerg": "Overlord",
}

TOWNHALL_NAMES = {"Nexus", "CommandCenter", "OrbitalCommand", "PlanetaryFortress", "Hatchery", "Lair", "Hive"}
GAS_NAMES = {"Assimilator", "Refinery", "Extractor"}

# Pre-game cosmetics (sprays, portrait "dance" previews, etc.) surface as
# UpgradeCompleteEvent too, all essentially at second 0 - not real build
# order information, so filter them out by name prefix.
COSMETIC_UPGRADE_PREFIXES = ("Spray", "RewardDance", "GameHeart", "Decal", "Portrait", "VoiceLine", "Skin")

METRICS = [
    ("First supply", "first_supply_time"),
    ("First gas", "first_gas_time"),
    ("2nd base", "second_base_time"),
    ("3rd base", "third_base_time"),
]

DEVIATION_THRESHOLD_SECONDS = 20

# How much of the game to capture for the step-by-step build order timeline,
# and how many steps to keep at most (early game only - this powers the
# "recommended build" / "what pros do differently" features, not a full
# game replay-by-replay log).
BUILD_ORDER_CAP_SECONDS = 600
BUILD_ORDER_MAX_ITEMS = 30

# Thresholds for the pro-pool build-order guide and the user-vs-pro diff.
GUIDE_MIN_FREQUENCY = 0.4
DIFF_MIN_FREQUENCY = 0.3
DIFF_MIN_GAMES = 3
TIMING_DIFF_THRESHOLD_SECONDS = 20


# A handful of upgrade internal names don't follow the CamelCase convention
# every unit/structure type name does (e.g. all-lowercase), so regex word
# splitting can't help - list the ones we've actually seen out of the pro
# pool here as they come up.
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


def _extract_stats_for_player(replay, me, opp):
    """Given a loaded replay and two Player objects, return the build-order
    stats dict from me's perspective."""
    record = {
        "map": replay.map_name,
        "my_race": me.play_race,
        "opp_race": opp.play_race,
        "win": (me.result == "Win"),
        "first_supply_time": None,
        "first_gas_time": None,
        "second_base_time": None,
        "third_base_time": None,
        "supply_block_count": 0,
        "build_order": [],
    }

    supply_unit_name = SUPPLY_UNIT_BY_RACE.get(me.play_race)
    townhall_times = []
    build_order = []

    for event in replay.tracker_events:
        # Supply-block detection: PlayerStatsEvent fires periodically (~every
        # 10s) per player with food_used/food_made. Being at or within 1 of
        # your supply cap in the first 12 minutes is a rough proxy for
        # getting "supply blocked."
        if type(event).__name__ == "PlayerStatsEvent":
            stats_player = getattr(event, "player", None)
            if stats_player == me:
                food_used = getattr(event, "food_used", None)
                food_made = getattr(event, "food_made", None)
                if food_used is not None and food_made is not None and event.second <= 720:
                    if (food_made - food_used) <= 1:
                        record["supply_block_count"] += 1
            continue

        if type(event).__name__ == "UpgradeCompleteEvent":
            if getattr(event, "player", None) == me and event.second <= BUILD_ORDER_CAP_SECONDS:
                upgrade_name = getattr(event, "upgrade_type_name", None)
                if upgrade_name and not upgrade_name.startswith(COSMETIC_UPGRADE_PREFIXES):
                    build_order.append({"time": event.second, "kind": "upgrade", "name": upgrade_name})
            continue

        unit = getattr(event, "unit", None)
        owner = getattr(unit, "owner", None) if unit else None
        if owner != me:
            continue

        game_seconds = event.second
        event_type = type(event).__name__

        # Use the event's own unit_type_name (the type AT THIS INSTANT) where
        # available, not unit.name, which reflects the unit's *final* type by
        # the time we're reading it here (e.g. a depot later lowered/raised
        # reads back as "SupplyDepotLowered", or a Gateway that warped later
        # reads back as "WarpGate" - both would silently break the checks
        # below). UnitDoneEvent doesn't carry unit_type_name, but that's only
        # used for townhall detection where every possible final name
        # (Hatchery/Lair/Hive, CC/OC/PF) is already in TOWNHALL_NAMES.
        unit_type_name = getattr(event, "unit_type_name", None) or getattr(unit, "name", None)

        if event_type in ("UnitInitEvent", "UnitBornEvent", "UnitDoneEvent"):
            if unit_type_name == supply_unit_name and record["first_supply_time"] is None:
                record["first_supply_time"] = game_seconds

        if event_type == "UnitInitEvent":
            if unit_type_name in GAS_NAMES and record["first_gas_time"] is None:
                record["first_gas_time"] = game_seconds

            if unit.is_building and game_seconds <= BUILD_ORDER_CAP_SECONDS and unit_type_name:
                build_order.append({"time": game_seconds, "kind": "structure", "name": unit_type_name})

        if unit_type_name in TOWNHALL_NAMES and event_type == "UnitDoneEvent":
            townhall_times.append(game_seconds)

    townhall_times.sort()
    if len(townhall_times) >= 2:
        record["second_base_time"] = townhall_times[1]
    if len(townhall_times) >= 3:
        record["third_base_time"] = townhall_times[2]

    build_order.sort(key=lambda item: item["time"])
    record["build_order"] = build_order[:BUILD_ORDER_MAX_ITEMS]

    return record


def find_player_by_name(replay, names):
    names_lower = {n.lower() for n in names}
    for player in replay.players:
        if player.name.lower() in names_lower:
            return player
        detail = getattr(player, "detail_tag", None)
        if detail and detail.lower() in names_lower:
            return player
    return None


def _replay_played_at(replay):
    start_time = getattr(replay, "start_time", None) or getattr(replay, "date", None)
    return start_time.isoformat() if start_time else None


def extract_game_record(path, my_names):
    """For a user's own replay folder: find the player matching one of
    my_names and return their build-order record, or None if not a 1v1
    or the name isn't found."""
    try:
        replay = sc2reader.load_replay(path, load_level=4)
    except Exception:
        return None

    me = find_player_by_name(replay, my_names)
    if me is None:
        return None

    opponents = [p for p in replay.players if p != me]
    if len(opponents) != 1:
        return None
    opp = opponents[0]

    record = _extract_stats_for_player(replay, me, opp)
    record["file"] = os.path.basename(path)
    record["played_at"] = _replay_played_at(replay)
    return record


def extract_both_perspectives(path):
    """For the shared pro pool: no name matching needed, just extract
    both players' build orders from a 1v1 replay so either race side
    is useful as a benchmark."""
    try:
        replay = sc2reader.load_replay(path, load_level=4)
    except Exception:
        return []

    if len(replay.players) != 2:
        return []

    p1, p2 = replay.players
    played_at = _replay_played_at(replay)
    records = []
    for me, opp in [(p1, p2), (p2, p1)]:
        record = _extract_stats_for_player(replay, me, opp)
        record["file"] = os.path.basename(path)
        record["played_at"] = played_at
        records.append(record)
    return records


def fmt_time(seconds):
    if seconds is None:
        return "n/a"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def median_or_none(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def mean_or_none(values):
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 1) if vals else None


def _indexed_build_steps(build_order):
    """Turn a game's flat build_order list into (name, occurrence#, time)
    triples, e.g. the 2nd Gateway in a game is a distinct step from the 1st,
    so aggregating across games lines up 'the Nth X' rather than blending
    every Gateway in a game into one bucket."""
    counts = defaultdict(int)
    steps = []
    for item in build_order or []:
        counts[item["name"]] += 1
        steps.append((item["name"], counts[item["name"]], item["time"]))
    return steps


def aggregate_build_steps(games, min_frequency=GUIDE_MIN_FREQUENCY):
    """Build a single representative build order across a pool of games:
    keep (name, occurrence) steps common enough across the pool, use the
    median time for each, and return them ordered by time."""
    games_with_data = [g for g in games if g.get("build_order")]
    total = len(games_with_data)
    if total == 0:
        return []

    step_times = defaultdict(list)
    for g in games_with_data:
        for name, occurrence, t in _indexed_build_steps(g["build_order"]):
            step_times[(name, occurrence)].append(t)

    guide = []
    for (name, occurrence), times in step_times.items():
        frequency = len(times) / total
        if frequency >= min_frequency:
            guide.append({
                "name": name,
                "label": prettify_name(name),
                "occurrence": occurrence,
                "time": median_or_none(times),
                "frequency": round(frequency, 2),
            })
    guide.sort(key=lambda s: s["time"])
    return guide[:15]


def compare_build_steps(user_games, pro_guide, min_frequency=DIFF_MIN_FREQUENCY):
    """For each step in the pro guide, check whether the user does it often
    enough in their own games at this matchup, and if so whether the timing
    differs meaningfully. Returns a list of plain-data difference records,
    still in pro-guide (time) order."""
    games_with_data = [g for g in user_games if g.get("build_order")]
    total = len(games_with_data)
    if total < DIFF_MIN_GAMES or not pro_guide:
        return []

    user_step_times = defaultdict(list)
    for g in games_with_data:
        for name, occurrence, t in _indexed_build_steps(g["build_order"]):
            user_step_times[(name, occurrence)].append(t)

    differences = []
    for step in pro_guide:
        key = (step["name"], step["occurrence"])
        user_times = user_step_times.get(key, [])
        user_frequency = len(user_times) / total

        if user_frequency < min_frequency:
            differences.append({
                "type": "missing",
                "label": step["label"],
                "occurrence": step["occurrence"],
                "pro_time": step["time"],
                "pro_frequency": step["frequency"],
                "user_frequency": round(user_frequency, 2),
            })
        else:
            user_median = median_or_none(user_times)
            diff = user_median - step["time"]
            if abs(diff) >= TIMING_DIFF_THRESHOLD_SECONDS:
                differences.append({
                    "type": "timing",
                    "label": step["label"],
                    "occurrence": step["occurrence"],
                    "pro_time": step["time"],
                    "user_time": user_median,
                    "diff": round(diff),
                })

    return differences[:10]


def compute_matchup_data(records, pro_records=None):
    by_matchup = defaultdict(list)
    for r in records:
        by_matchup[(r["my_race"], r["opp_race"])].append(r)

    by_matchup_pro = defaultdict(list)
    if pro_records:
        for r in pro_records:
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
                "overall": median_or_none([g[key] for g in games]),
                "win": median_or_none([g[key] for g in wins]),
                "loss": median_or_none([g[key] for g in losses]),
                "pro": median_or_none([g[key] for g in pro_games]) if pro_games else None,
            }

        # Per-map win rate, most-played first.
        map_counts = defaultdict(lambda: {"wins": 0, "losses": 0})
        for g in games:
            bucket = map_counts[g.get("map") or "Unknown"]
            if g["win"]:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
        map_stats = [
            {"map": m, "wins": v["wins"], "losses": v["losses"], "total": v["wins"] + v["losses"]}
            for m, v in map_counts.items()
        ]
        map_stats.sort(key=lambda x: x["total"], reverse=True)

        flagged = []
        if wins:
            avg_block_win = mean_or_none([g.get("supply_block_count") for g in wins])
            for g in losses:
                notes = []
                for label, key in METRICS:
                    win_med = stats[key]["win"]
                    val = g[key]
                    if win_med is not None and val is not None:
                        diff = val - win_med
                        if abs(diff) >= DEVIATION_THRESHOLD_SECONDS:
                            notes.append({
                                "label": label,
                                "direction": "later" if diff > 0 else "earlier",
                                "diff": abs(int(diff)),
                                "val": fmt_time(val),
                                "win_med": fmt_time(win_med),
                            })
                block_count = g.get("supply_block_count")
                if avg_block_win is not None and block_count is not None and block_count - avg_block_win >= 2:
                    notes.append({
                        "label": "Supply blocks",
                        "direction": "more often",
                        "diff": round(block_count - avg_block_win, 1),
                        "val": str(block_count),
                        "win_med": str(avg_block_win),
                    })
                if notes:
                    flagged.append({"file": g["file"], "map": g["map"], "notes": notes})

        pro_build_guide = aggregate_build_steps(pro_games) if len(pro_games) >= 3 else []
        build_differences = compare_build_steps(games, pro_build_guide) if pro_build_guide else []

        matchups.append({
            "my_race": my_race,
            "opp_race": opp_race,
            "game_count": len(games),
            "win_count": len(wins),
            "loss_count": len(losses),
            "has_pro": bool(pro_games),
            "pro_count": len(pro_games),
            "stats": stats,
            "flagged": flagged,
            "map_stats": map_stats,
            "avg_supply_blocks_win": mean_or_none([g.get("supply_block_count") for g in wins]),
            "avg_supply_blocks_loss": mean_or_none([g.get("supply_block_count") for g in losses]),
            "pro_build_guide": pro_build_guide,
            "build_differences": build_differences,
        })

    return matchups
