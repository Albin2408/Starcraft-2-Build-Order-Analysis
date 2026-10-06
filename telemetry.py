"""
Frame-level telemetry reconstruction from a parsed replay.

sc2reader gives us three raw streams (tracker events, game events, and the
replay header). This module turns them into things worth looking at:

  * a time-indexed record of where each side's army was standing, rebuilt by
    replaying born / moved / died events into a live unit table and
    snapshotting it on a fixed timer (UnitPositionsEvent alone only reports
    units that *moved*, so it would under-count an army sitting still);
  * fights, found by clustering army deaths in time;
  * APM split into "during fights" vs "calm", plus the same split for
    *macro* commands only - i.e. do you keep producing while you fight;
  * resource curves and a mineral-floating penalty.

All times here are real in-game seconds on the Faster clock (what the game
shows), not sc2reader's `event.second`, which counts 16 frames/s regardless
of game speed and so reads ~1.4x too large on Faster.

The heavy lifting is done once per replay by `analyze_replay`; the cheap
`player_view` then slices it from either player's perspective (the pro pool
needs both).
"""

import re
import statistics
from collections import Counter, defaultdict

# --- clock --------------------------------------------------------------------

SPEED_FPS = {"Slower": 9.6, "Slow": 12.8, "Normal": 16.0, "Fast": 19.2, "Faster": 22.4}
DEFAULT_FPS = 22.4  # ladder games are always Faster


def fps_for(replay):
    return SPEED_FPS.get(str(getattr(replay, "speed", "Faster")), DEFAULT_FPS)


# --- spatial grid ---------------------------------------------------------------

GRID = 64            # raw telemetry grid is GRID x GRID cells...
COORD_MAX = 256      # ...over SC2's 0..256 coordinate space
CELL = COORD_MAX // GRID
SNAP_SECONDS = 10    # army snapshot interval


def cell_of(x, y):
    cx = min(GRID - 1, max(0, int(x) // CELL))
    cy = min(GRID - 1, max(0, int(y) // CELL))
    return cy * GRID + cx


# --- what counts as "army" ---------------------------------------------------------

# is_army is true for these too, but they aren't units a player positions.
NON_ARMY_NAMES = {
    "Larva", "Egg", "BroodlingEscort", "Broodling", "Changeling", "ChangelingZealot",
    "ChangelingMarine", "ChangelingMarineShield", "ChangelingZergling",
    "ChangelingZerglingWings", "Interceptor", "AutoTurret", "LocustMP", "LocustMPFlying",
    "LocustMPPrecursor", "MULE", "AdeptPhaseShift", "DisruptorPhased", "ForceField",
    "KD8Charge", "RavenRepairDrone", "CreepTumor", "CreepTumorBurrowed", "CreepTumorQueen",
    "Overlord", "OverlordTransport", "Overseer", "OverseerSiegeMode", "WarpPrism",
    "WarpPrismPhasing", "Observer", "ObserverSiegeMode", "MedivacCarrier",
}

TOWNHALL_NAMES = {"Nexus", "CommandCenter", "OrbitalCommand", "PlanetaryFortress", "Hatchery", "Lair", "Hive"}


def is_army_unit(unit):
    return bool(unit) and unit.is_army and unit.name not in NON_ARMY_NAMES


# --- fights ---------------------------------------------------------------------------

FIGHT_CLUSTER_GAP = 8     # deaths closer than this (s) belong to the same fight
FIGHT_MIN_DEATHS = 3      # fewer army deaths than this is a skirmish, not a fight
FIGHT_PAD = 5             # seconds of context added either side of a fight

# --- floating -----------------------------------------------------------------------------

FLOAT_START = 240         # ignore the first 4:00 - banking for a hatch/nexus is fine
FLOAT_THRESHOLD = 400     # minerals above this are "floating"
FLOAT_HEAVY = 800
FLOAT_MAXED_SUPPLY = 190  # at/above this supply, floating is expected, not a penalty


# --- action classification ---------------------------------------------------------------

# Game events that are real player actions. Camera moves, UI state and the
# "update target" echoes of a drag aren't, so APM ignores them.
_COMMAND_EVENTS = {"BasicCommandEvent", "TargetPointCommandEvent", "TargetUnitCommandEvent", "DataCommandEvent"}
_SELECTION_EVENTS = {"SelectionEvent", "SetControlGroupEvent", "GetControlGroupEvent",
                     "AddToControlGroupEvent", "DeleteControlGroupEvent"}

# Commands that produce/improve the economy or army rather than steering it.
# `ability.is_build` already covers Train*/Build*/morphs; the rest are the
# racial "macro mechanics" and research.
_MACRO_NAME = re.compile(
    r"^(Research|UpgradeTo|Morph|Warp|Train|Build|Hatchery|Effect_Inject|Inject|SpawnLarva|"
    r"QueenSpawnLarva|Effect_Chrono|Chrono|CalldownMULE|SupplyDrop|Effect_Spawn|Effect_Calldown)",
    re.IGNORECASE,
)


def is_macro_command(event):
    ability = getattr(event, "ability", None)
    if ability is not None and getattr(ability, "is_build", False):
        return True
    name = getattr(event, "ability_name", None) or ""
    return bool(_MACRO_NAME.match(name))


# =============================================================================
# Pure helpers (unit-tested without any replay)
# =============================================================================

def cluster_fights(deaths, gap=FIGHT_CLUSTER_GAP, min_deaths=FIGHT_MIN_DEATHS, pad=FIGHT_PAD, length=None):
    """Group army deaths into fights.

    deaths: iterable of (time_s, pid, cell). Returns a list of dicts
    {start, end, count, by_pid: {pid: n}, cell} sorted by time, where start/end
    already include `pad` seconds of context. `cell` is the modal grid cell,
    i.e. roughly where the fight happened.
    """
    ordered = sorted(deaths, key=lambda d: d[0])
    clusters, current = [], []
    for d in ordered:
        if current and d[0] - current[-1][0] > gap:
            clusters.append(current)
            current = []
        current.append(d)
    if current:
        clusters.append(current)

    fights = []
    for c in clusters:
        if len(c) < min_deaths:
            continue
        start = max(0.0, c[0][0] - pad)
        end = c[-1][0] + pad
        if length is not None:
            end = min(float(length), end)
        by_pid = Counter(d[1] for d in c)
        cell = Counter(d[2] for d in c).most_common(1)[0][0]
        fights.append({"start": start, "end": end, "count": len(c), "by_pid": dict(by_pid), "cell": cell})
    return fights


def merged_windows(fights):
    """Fight windows as non-overlapping [start, end] pairs (padding can make
    neighbouring fights touch)."""
    windows = []
    for f in sorted(fights, key=lambda f: f["start"]):
        if windows and f["start"] <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], f["end"])
        else:
            windows.append([f["start"], f["end"]])
    return windows


def in_windows(t, windows):
    # windows are few (tens), so a linear scan is fine
    for s, e in windows:
        if s <= t <= e:
            return True
    return False


def split_rates(actions, windows, length):
    """Actions-per-minute inside vs outside the fight windows.

    actions: list of (time_s, is_macro). Returns a dict with total / combat /
    calm APM and the same for macro-only commands, plus the retention ratios.
    A rate is None when there isn't enough time in that phase to mean anything.
    """
    fight_seconds = sum(min(e, length) - max(s, 0) for s, e in windows if min(e, length) > max(s, 0))
    calm_seconds = max(0.0, length - fight_seconds)

    n = {"fight": 0, "calm": 0, "macro_fight": 0, "macro_calm": 0}
    for t, macro in actions:
        phase = "fight" if in_windows(t, windows) else "calm"
        n[phase] += 1
        if macro:
            n["macro_" + phase] += 1

    def rate(count, seconds, min_seconds=20):
        return round(count / (seconds / 60.0), 1) if seconds >= min_seconds else None

    out = {
        "apm_total": rate(len(actions), length, 60),
        "apm_combat": rate(n["fight"], fight_seconds),
        "apm_macro": rate(n["calm"], calm_seconds),
        "macro_rate_combat": rate(n["macro_fight"], fight_seconds),
        "macro_rate_calm": rate(n["macro_calm"], calm_seconds),
        "fight_seconds": round(fight_seconds, 1),
    }

    def ratio(a, b):
        return round(a / b, 3) if a is not None and b else None

    out["apm_retention"] = ratio(out["apm_combat"], out["apm_macro"])
    out["macro_retention"] = ratio(out["macro_rate_combat"], out["macro_rate_calm"])
    return out


def floating_stats(samples, start=FLOAT_START, threshold=FLOAT_THRESHOLD, heavy=FLOAT_HEAVY,
                   maxed=FLOAT_MAXED_SUPPLY):
    """samples: list of (time_s, minerals, supply_used). Returns avg unspent
    minerals, the mean amount above `threshold` (the "penalty"), and the share
    of time spent above `heavy`. Only counts time after `start` and while not
    maxed out: banking at 200/200 isn't a mistake (there's nothing to spend it
    on), and counting it would make long wins look like worse macro than short
    losses."""
    vals = [m for t, m, used in samples if t >= start and (used is None or used < maxed)]
    if not vals:
        return {"mineral_floating_avg": None, "float_penalty": None, "float_pct": None}
    return {
        "mineral_floating_avg": round(statistics.mean(vals)),
        "float_penalty": round(statistics.mean(max(0, m - threshold) for m in vals)),
        "float_pct": round(100.0 * sum(1 for m in vals if m > heavy) / len(vals), 1),
    }


def trimmed_bounds(xs, ys, trim=0.005, pad=4):
    """Playable-area estimate from every position seen in a replay."""
    if not xs or not ys:
        return [0, 0, COORD_MAX, COORD_MAX]
    xs, ys = sorted(xs), sorted(ys)
    lo, hi = int(len(xs) * trim), max(int(len(xs) * (1 - trim)) - 1, 0)
    x0, x1 = xs[lo], xs[hi]
    y0, y1 = ys[lo], ys[hi]
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(COORD_MAX, x1 + pad), min(COORD_MAX, y1 + pad)
    if x1 - x0 < 16 or y1 - y0 < 16:      # degenerate -> fall back to the whole space
        return [0, 0, COORD_MAX, COORD_MAX]
    return [x0, y0, x1, y1]


def normalize_cell(cell, bounds, flip, out_grid):
    """Map a raw grid cell into an `out_grid` x `out_grid` frame spanning the
    game's playable bounds. `flip` rotates 180 degrees, which puts the player's
    own base in the same corner every game (2-player maps are point symmetric)."""
    cx = (cell % GRID) * CELL + CELL / 2
    cy = (cell // GRID) * CELL + CELL / 2
    x0, y0, x1, y1 = bounds
    u = min(1.0, max(0.0, (cx - x0) / (x1 - x0)))
    v = min(1.0, max(0.0, (cy - y0) / (y1 - y0)))
    if flip:
        u, v = 1.0 - u, 1.0 - v
    gx = min(out_grid - 1, int(u * out_grid))
    gy = min(out_grid - 1, int(v * out_grid))
    return gy * out_grid + gx


def needs_flip(start, bounds):
    """True if `start` (x, y) sits in the upper-right half of the map."""
    if not start:
        return False
    x0, y0, x1, y1 = bounds
    u = (start[0] - x0) / (x1 - x0)
    v = (start[1] - y0) / (y1 - y0)
    return (u + v) > 1.0


def interpolate_track(points, times):
    """Positions of one unit at each time in `times` (ascending), linearly
    interpolated between its known (t, x, y) samples and held at the ends.
    Checked against real replays (leave-one-out on held-out samples) this
    lands within ~10 map units at the median vs ~24 for hold-last-known."""
    out = []
    i = 0
    n = len(points)
    for t in times:
        while i + 1 < n and points[i + 1][0] <= t:
            i += 1
        t0, x0, y0 = points[i]
        if i + 1 < n:
            t1, x1, y1 = points[i + 1]
            a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            out.append((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a))
        else:
            out.append((x0, y0))
    return out


def build_snapshots(tracks, pids, length, interval=SNAP_SECONDS):
    """tracks: iterable of [pid, [(t, x, y), ...], death_time_or_None].
    Returns {pid: [Counter(cell -> units) per tick]} for ticks 0..length."""
    ticks = int(length // interval) + 1
    snapshots = {pid: [Counter() for _ in range(ticks)] for pid in pids}
    for pid, points, died in tracks:
        if pid not in snapshots:
            continue
        end = died if died is not None else length
        first = int(-(-points[0][0] // interval))        # ceil
        last = min(ticks - 1, int(end // interval))
        if last < first:
            continue
        times = [k * interval for k in range(first, last + 1)]
        for k, (x, y) in zip(range(first, last + 1), interpolate_track(points, times)):
            snapshots[pid][k][cell_of(x, y)] += 1
    return snapshots


# =============================================================================
# The one pass over a replay
# =============================================================================

def analyze_replay(replay, pids):
    """Walk the tracker + game event streams once and collect everything the
    per-player views need. `pids` are the two player ids to track."""
    fps = fps_for(replay)
    pidset = set(pids)

    # Unit -> [pid, [(t, x, y), ...]]. The game only reports a unit's position
    # when it's born, when it (occasionally) shows up in a UnitPositionsEvent
    # (~1 per unit per minute), and exactly when it dies - so we keep each
    # unit's sparse track and interpolate it onto the snapshot clock later.
    tracks = {}
    deaths = []                                  # (t, victim_pid, cell, killer_pid)
    stats = {pid: [] for pid in pids}            # pid -> [(t, minerals, gas, workers, used, made, income)]
    starts = {}
    xs, ys = [], []
    last_frame = 0

    for event in replay.tracker_events:
        frame = event.frame
        last_frame = frame
        kind = type(event).__name__

        if kind == "UnitBornEvent" or kind == "UnitInitEvent":
            unit = event.unit
            owner = getattr(unit, "owner", None) if unit else None
            if owner is None or owner.pid not in pidset:
                continue
            x, y = event.x, event.y
            xs.append(x)
            ys.append(y)
            if frame == 0 and unit.name in TOWNHALL_NAMES and owner.pid not in starts:
                starts[owner.pid] = (x, y)
            if kind == "UnitBornEvent" and is_army_unit(unit):
                tracks[unit] = [owner.pid, [(frame / fps, x, y)], None]

        elif kind == "UnitPositionsEvent":
            t = frame / fps
            for unit, pos in event.units.items():
                track = tracks.get(unit)
                if track is not None and track[2] is None:
                    track[1].append((t, pos[0], pos[1]))

        elif kind == "UnitDiedEvent":
            track = tracks.get(event.unit)
            if track is None or track[2] is not None:
                continue
            t = frame / fps
            track[1].append((t, event.x, event.y))
            track[2] = t
            killer = getattr(event, "killing_player_id", None)
            # A death with no killer is a merge (Archon), a morph, or a timed
            # expiry - not a fight. (It still ends the unit's track.)
            if killer is not None:
                deaths.append((t, track[0], cell_of(event.x, event.y), killer))

        elif kind == "PlayerStatsEvent":
            if event.pid in pidset:
                stats[event.pid].append((
                    frame / fps, event.minerals_current, event.vespene_current,
                    event.workers_active_count, event.food_used, event.food_made,
                    event.minerals_collection_rate,
                ))

    length = last_frame / fps
    snapshots = build_snapshots(tracks.values(), pids, length)

    # Action stream: (time, is_macro) per player.
    actions = {pid: [] for pid in pids}
    for event in replay.game_events:
        player = getattr(event, "player", None)
        if player is None or player.pid not in pidset:
            continue
        kind = type(event).__name__
        if kind in _COMMAND_EVENTS:
            actions[player.pid].append((event.frame / fps, is_macro_command(event)))
        elif kind in _SELECTION_EVENTS:
            actions[player.pid].append((event.frame / fps, False))

    fights = cluster_fights([(t, pid, cell) for t, pid, cell, _ in deaths], length=length)

    return {
        "fps": fps, "length": length, "snapshots": snapshots, "deaths": deaths,
        "stats": stats, "actions": actions, "fights": fights, "starts": starts,
        "bounds": trimmed_bounds(xs, ys),
    }


def _flat(counter):
    out = []
    for cell, n in counter.items():
        out.extend((cell, n))
    return out


def player_view(analysis, me_pid, opp_pid):
    """Metrics + the compact telemetry blob from `me_pid`'s point of view."""
    length = analysis["length"]
    windows = merged_windows(analysis["fights"])
    rates = split_rates(analysis["actions"][me_pid], windows, length)
    floats = floating_stats([(s[0], s[1], s[4]) for s in analysis["stats"][me_pid]])

    metrics = {**{k: v for k, v in rates.items() if k != "fight_seconds"}, **floats,
               "fight_count": len(analysis["fights"])}

    # APM time series, one point per snapshot interval.
    bins = int(length // SNAP_SECONDS) + 1
    apm_series = [0] * bins
    for t, _ in analysis["actions"][me_pid]:
        apm_series[min(bins - 1, int(t // SNAP_SECONDS))] += 1
    apm_series = [n * (60 // SNAP_SECONDS) for n in apm_series]

    series = list(zip(*analysis["stats"][me_pid])) or [[]] * 7
    stat_t, minerals, gas, workers, used, made, income = (list(s) for s in series)

    telemetry = {
        "v": 1,
        "grid": GRID, "cell": CELL, "snap": SNAP_SECONDS,
        "length": round(length),
        "bounds": analysis["bounds"],
        "start_me": analysis["starts"].get(me_pid),
        "start_opp": analysis["starts"].get(opp_pid),
        "me": [_flat(c) for c in analysis["snapshots"][me_pid]],
        "opp": [_flat(c) for c in analysis["snapshots"][opp_pid]],
        # army that died, by whose it was: [time, cell]
        "deaths_me": [[round(t, 1), cell] for t, pid, cell, _ in analysis["deaths"] if pid == me_pid],
        "deaths_opp": [[round(t, 1), cell] for t, pid, cell, _ in analysis["deaths"] if pid == opp_pid],
        "fights": [
            [round(f["start"], 1), round(f["end"], 1),
             f["by_pid"].get(me_pid, 0), f["by_pid"].get(opp_pid, 0), f["cell"]]
            for f in analysis["fights"]
        ],
        "series": {
            "t": [round(t, 1) for t in stat_t],
            "minerals": minerals, "gas": gas, "workers": workers,
            "supply": used, "cap": made, "income": income,
            "apm": apm_series, "apm_t": [i * SNAP_SECONDS for i in range(bins)],
        },
        "float_threshold": FLOAT_THRESHOLD,
    }
    return metrics, telemetry
