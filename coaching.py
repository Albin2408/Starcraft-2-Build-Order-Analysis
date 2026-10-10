"""
"Work on this": turn one matchup's games into a short ranked list of the habits
most worth fixing.

Every candidate metric is scored two ways, then combined:

  * wins vs. losses - how much worse the metric is in your losses (a gap that
    separates your wins from your losses is a gap that's costing games);
  * you vs. the pro pool - how far you sit from pro norms, in units of the
    pros' own spread.

Both are standardised effect sizes (difference / spread), so a 40-second
timing gap and a 0.2 drop in a ratio can be ranked against each other. The
score is then shrunk toward zero when there are few games, so a pattern in 4
games can't outrank one backed by 15, and anything resting on fewer than 3
wins or 3 losses is marked low-confidence rather than hidden.

This is a heuristic for deciding what to look at first, not a statistical
test; the evidence line shows the numbers it's based on.
"""

import statistics

from telemetry import MECH_LABEL

MIN_SCORE = 0.3          # below this a gap isn't worth mentioning
SHRINK_K = 3             # score *= n / (n + K)
CLAMP = 3.0              # one runaway metric can't dominate
MIN_PRO = 3              # pro games needed for the pro comparison
MIN_GROUP = 2            # wins / losses needed for the win-vs-loss comparison
LOW_CONFIDENCE_BELOW = 3   # wins and losses each
LOW_CONFIDENCE_PRO_ONLY = 5  # games, when only the pro comparison is available


def _time(v):
    v = int(round(v))
    return f"{v // 60}:{v % 60:02d}"


FORMATTERS = {
    "time": _time,
    "int": lambda v: f"{v:.0f}",
    "pct": lambda v: f"{100 * v:.0f}%",
    "minerals": lambda v: f"{v:,.0f}",
}

# (key, label, better, formatter, group, min_diff, tip)
#   `better`   which direction is good
#   `min_diff` the smallest gap (in the metric's own units) that's worth caring about;
#              smaller gaps count as zero however 'significant' they look statistically
SPECS = [
    ("second_base_time", "2nd base timing", "lower", "time", None, 10,
     "Expand earlier or safer: compare your opening to the pro build below."),
    ("third_base_time", "3rd base timing", "lower", "time", None, 10,
     "A late third caps your income. Check the pro build for when it goes down."),
    ("supply_block_count", "Supply blocks (first 12 min)", "lower", "int", None, 2,
     "Start the next depot / pylon / overlord before you need it, around 6 supply from the cap."),
    ("float_penalty", "Floating minerals", "lower", "minerals", None, 100,
     "When minerals pile up past ~400, add production or an expansion instead of waiting."),
    ("apm_retention", "APM holding up in fights", "higher", "pct", None, 0.05,
     "Your actions fall off when fighting. Use control groups and a camera hotkey so a fight doesn't cost you macro."),
    ("macro_retention", "Macro while fighting", "higher", "pct", None, 0.05,
     "You stop producing during fights. Queue production before you engage and tab back between engagements."),
    ("workers_4m", "Workers at 4:00", "higher", "int", "workers", 3,
     "Keep every townhall producing workers constantly until saturation."),
    ("workers_6m", "Workers at 6:00", "higher", "int", "workers", 3,
     "Keep every townhall producing workers constantly, and take bases on time so there's room to saturate."),
    ("workers_8m", "Workers at 8:00", "higher", "int", "workers", 3,
     "Keep every townhall producing workers constantly, and take bases on time so there's room to saturate."),
]

MECH_TIPS = {
    "Protoss": "Spend every chrono boost as soon as a Nexus reaches 50 energy: probes first, then production and upgrades.",
    "Terran": "Drop a MULE whenever an Orbital hits 50 energy; keep scans for when you actually need them.",
    "Zerg": "Inject every ~30 seconds: queue it the moment a queen has 25 energy.",
}


def _vals(games, key):
    return [g[key] for g in games if g.get(key) is not None]


def _spread(values):
    """Sample standard deviation, floored so a near-constant metric can't
    turn a tiny difference into an enormous effect size."""
    sd = statistics.stdev(values) if len(values) >= 2 else 0.0
    return max(sd, 0.08 * abs(statistics.mean(values)), 1e-6)


def _worse(better, delta):
    """Positive when `delta` (mine - reference) is the bad direction."""
    return delta if better == "lower" else -delta


def _clamp(x):
    return max(-CLAMP, min(CLAMP, x))


def _specs_for(race):
    specs = list(SPECS)
    if race in MECH_LABEL:
        specs.append(("mech_efficiency", MECH_LABEL[race], "higher", "pct", None, 0.05, MECH_TIPS[race]))
    return specs


def rank_focus(wins, losses, pro_games, race, limit=3):
    """Top `limit` habits to work on for one matchup. Each item:
    {key, label, score, confidence, evidence, sample, tip}."""
    mine = list(wins) + list(losses)
    if len(mine) < MIN_GROUP:
        return []

    items = []
    for key, label, better, fmt_kind, group, min_diff, tip in _specs_for(race):
        fmt = FORMATTERS[fmt_kind]
        w, l, p = _vals(wins, key), _vals(losses, key), _vals(pro_games, key)
        m = w + l

        parts, comps = [], []
        wl_effect = pro_effect = None
        if len(w) >= MIN_GROUP and len(l) >= MIN_GROUP:
            delta = statistics.mean(l) - statistics.mean(w)
            wl_effect = _worse(better, delta) / _spread(m) if abs(delta) >= min_diff else 0.0
            comps.append(wl_effect)
        if len(p) >= MIN_PRO and len(m) >= MIN_GROUP:
            delta = statistics.mean(m) - statistics.mean(p)
            pro_effect = _worse(better, delta) / _spread(p) if abs(delta) >= min_diff else 0.0
            comps.append(pro_effect)
        if not comps:
            continue

        score = statistics.mean(_clamp(c) for c in comps) * (len(m) / (len(m) + SHRINK_K))
        if score < MIN_SCORE:
            continue

        if wl_effect is not None and wl_effect >= MIN_SCORE:
            parts.append(f"losses {fmt(statistics.mean(l))} vs wins {fmt(statistics.mean(w))}")
        if pro_effect is not None and pro_effect >= MIN_SCORE:
            parts.append(f"you {fmt(statistics.mean(m))} vs pros {fmt(statistics.mean(p))}")

        low = (min(len(w), len(l)) < LOW_CONFIDENCE_BELOW) if wl_effect is not None else (len(m) < LOW_CONFIDENCE_PRO_ONLY)
        items.append({
            "key": key, "label": label, "group": group, "score": round(score, 2),
            "confidence": "low" if low else "ok",
            "evidence": " · ".join(parts) or "somewhat worse than your wins / the pro pool",
            "sample": f"{len(w)} win{'' if len(w) == 1 else 's'}, {len(l)} loss{'' if len(l) == 1 else 'es'}"
                      + (f", {len(p)} pro game{'' if len(p) == 1 else 's'}" if p else ""),
            "tip": tip,
        })

    items.sort(key=lambda i: i["score"], reverse=True)

    # the three worker checkpoints are one habit: keep the strongest
    seen, out = set(), []
    for item in items:
        if item["group"] and item["group"] in seen:
            continue
        seen.add(item["group"])
        out.append({k: v for k, v in item.items() if k != "group"})
        if len(out) == limit:
            break
    return out
