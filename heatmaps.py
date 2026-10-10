"""
Aggregate per-game telemetry into time-sliced heatmap grids.

The browser draws and smooths the grids on a <canvas>; this module only does
the part that needs every game's data: put them in a common frame, sum them
by game-minute, and average over the games that were still going at that
minute (so a minute only 3 long games reached isn't drawn as "hotter" than
one all 30 reached).
"""

import statistics
from collections import defaultdict

import telemetry

OUT_GRID = 48
LAYERS = ("me", "opp", "deaths_me", "deaths_opp")


def aggregate(telemetries, orient=True, out_grid=OUT_GRID):
    """telemetries: list of telemetry dicts, each already from the viewpoint
    of the player being studied. With `orient`, games are rotated 180 degrees
    when needed so the player's own base is always in the lower-left."""
    telemetries = [t for t in telemetries if t and t.get("me") is not None]
    if not telemetries:
        return {"grid": out_grid, "games": 0, "minutes": 0, "layers": {l: [] for l in LAYERS},
                "start_me": None, "start_opp": None}

    minutes = max(int(t["length"] // 60) + 1 for t in telemetries)
    games_at = [0] * minutes
    sums = {layer: [defaultdict(float) for _ in range(minutes)] for layer in LAYERS}
    marks = {"me": [], "opp": []}

    for t in telemetries:
        bounds = t["bounds"]
        flip = orient and telemetry.needs_flip(t.get("start_me"), bounds)
        cache = {}

        def to_cell(raw):
            c = cache.get(raw)
            if c is None:
                c = cache[raw] = telemetry.normalize_cell(raw, bounds, flip, out_grid)
            return c

        for key, store in (("start_me", marks["me"]), ("start_opp", marks["opp"])):
            pos = t.get(key)
            if pos:
                raw = telemetry.cell_of(*pos)
                store.append(to_cell(raw))

        length_minutes = int(t["length"] // 60) + 1
        for m in range(min(length_minutes, minutes)):
            games_at[m] += 1

        per_min = 60 // t["snap"]
        for layer in ("me", "opp"):
            for k, flat in enumerate(t[layer]):
                m = (k * t["snap"]) // 60
                if m >= minutes:
                    break
                bucket = sums[layer][m]
                for i in range(0, len(flat), 2):
                    bucket[to_cell(flat[i])] += flat[i + 1] / per_min
        for layer in ("deaths_me", "deaths_opp"):
            for when, raw in t[layer]:
                m = int(when // 60)
                if m < minutes:
                    sums[layer][m][to_cell(raw)] += 1

    layers = {}
    for layer in LAYERS:
        per_minute = []
        for m in range(minutes):
            n = games_at[m] or 1
            flat = []
            for cell, v in sums[layer][m].items():
                flat.extend((cell, round(v / n, 4)))
            per_minute.append(flat)
        layers[layer] = per_minute

    def mean_cell_xy(cells):
        if not cells:
            return None
        xs = [c % out_grid for c in cells]
        ys = [c // out_grid for c in cells]
        return [round((statistics.mean(xs) + 0.5) / out_grid, 3), round((statistics.mean(ys) + 0.5) / out_grid, 3)]

    return {
        "grid": out_grid, "games": len(telemetries), "minutes": minutes,
        "games_at_minute": games_at, "layers": layers,
        "start_me": mean_cell_xy(marks["me"]), "start_opp": mean_cell_xy(marks["opp"]),
        "oriented": bool(orient),
    }
