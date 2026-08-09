# SC2 Build Order Analyzer — Web App

A small local website that parses your StarCraft 2 replays, tracks your
build-order timings per matchup, flags losses that deviate from your own
normal patterns, and compares you against a "pro pool" of higher-level
replays. Single-user for now — no login, just you and your own local
data.

## 1. Setup

```
pip install -r requirements.txt --break-system-packages
```

(On Windows you likely don't need `--break-system-packages` — plain
`pip install -r requirements.txt` should work.)

## 2. Run it

```
python app.py
```

This starts the site at `http://localhost:5000` — open that in your
browser. The first run creates a `sc2.db` SQLite file in this folder
(that's your entire database of parsed stats — back it up if you want
to keep it safe).

## 3. How it works

- **Settings** — enter your SC2 in-game name(s)/battletag(s)
  (comma-separated if you've used more than one over time). That's how
  the app finds "you" inside each replay file. Do this first.
- **Upload my replays** — paste the path to your replay folder (it
  remembers it for next time) and it scans every `.SC2Replay` file there
  directly — no need to pick files by hand. A manual file-upload option
  is still there as a fallback.
- **Upload pro pool** — same folder-path scanning, but for a benchmark
  set of higher-level replays. Good source:
  [lotv.spawningtool.com](https://lotv.spawningtool.com/replays/?pro_only=on),
  filtered by matchup and "Pro only," ideally from recent dates so it
  reflects the current patch. There's no public API for that site (their
  own FAQ confirms this), so replays still have to be downloaded there
  manually — their "replay packs" page (whole-tournament zip files) is
  the fastest way to grab a lot at once. Both players' perspectives are
  extracted from each replay, so it works as a benchmark regardless of
  which side is the "pro."
- **Dashboard** —
  - Overall win rate and game count.
  - A "suggested focus" banner that automatically calls out your worst
    matchup (with at least 3 games played), so you're not guessing what
    to work on.
  - "My race" / "opponent race" filters to narrow the matchup cards down
    to what you're actively practicing.
  - Per-matchup charts comparing your win-median vs. loss-median vs. the
    pro pool's median for first supply, first gas, and 2nd/3rd base
    timing.
  - A rough **supply-block counter**: how often you were at or near your
    supply cap in the first 12 minutes, averaged separately for wins vs.
    losses — a classic mechanical leak that's easy to miss on your own.
  - **Win rate by map** within each matchup.
  - A list of losses where your build timing (or supply-block count)
    deviated meaningfully from your own typical winning games in that
    matchup.

## 4. Known limitations / things to extend later

- Only handles 1v1 replays (skips FFA/2v2 for now).
- The "first supply/gas/expansion" timing set is a good starting point but
  intentionally simple — extending it (tech buildings, upgrades, unit
  composition at key timings) is a matter of adding more names to track
  in `analyzer.py`.
- The supply-block counter is a rough proxy (samples every ~10s, flags
  when you're within 1 supply of your cap), not a frame-perfect count —
  useful directionally, not as an exact number.
- No accounts/login — this is single-user by design for now. If you
  later want to share it with a friend, that's a fairly small addition
  on top of what's here (ask and it can be added back in).
