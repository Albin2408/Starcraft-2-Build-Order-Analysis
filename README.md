# SC2 Replay Telemetry & Spatial Analysis

Drop in StarCraft II replays and get back more than a win/loss record: where
each army stood over time, when fights happened, whether your APM *and* your
macro survive contact, how many minerals you sat on, and how your build
compares to a pool of pro games.

It is an event-driven pipeline rather than a page that parses files in the
request: uploads fan out to background workers over **Redis + Celery**, the
workers reconstruct frame-level telemetry from the binary replay's event
streams, and the browser follows progress live over **Server-Sent Events**
fed by a **Redis Stream**.

```
 browser ──drop files / scan folder──▶ Flask ──one task per replay──▶ Redis (Celery broker, queue "sc2_replays")
    ▲                                    │                                  │
    │ SSE: per-file events + counters    │                                  ▼
    └──────── Redis Stream + hash ◀──────┼───────────────────────── Celery worker(s)
                                         │                            sc2reader → telemetry.py → SQLite (WAL)
                                         ▼
                              SQLite ──▶ /api/matchups, /api/heatmap, /api/game ──▶ Canvas + Chart.js
```

## What it measures

| | |
|---|---|
| **Build order** | Structures and real upgrades in the first 10 minutes, in the in-game clock. The pro pool is boiled down to a *recommended build* per matchup (steps that appear in ≥40% of pro games, at their median time), and your own games are diffed against it: steps you skip, steps you're late on. |
| **Fights** | Army deaths (with a killer) clustered in time. A fight is ≥3 deaths within 8 s of each other, padded by 5 s. |
| **APM retention** | Actions per minute inside fights ÷ outside them. Actions are commands, selections and control-group use; camera moves and UI chatter are excluded. |
| **Macro retention** | Same split, but only for production / economy commands (train, build, research, inject, chrono, MULE…) — *do you keep producing while you fight?* |
| **Mineral floating** | Mean minerals above 400 after 4:00, plus the share of time above 800. Time spent at ≥190 supply is ignored: banking when maxed is unavoidable, and counting it makes long wins look like worse macro than short losses. |
| **Army heatmaps** | Where each side's army stood, as smoothed density on a canvas, with a time scrubber. Aggregated per matchup (wins / losses / pro pool), per map, or per game. |

Games before a **patch cutoff** (a date or a version like `5.0.16`, set in
Settings) are left out of the stats and marked in the game log, since an
economy-changing patch makes older timings misleading.

## How the telemetry is built

`telemetry.py` makes one pass over a replay's tracker events and keeps a
track (birth → sparse position updates → exact death position) for every army
unit, then:

1. **Interpolates each track onto a 10-second clock.** The game only reports a
   unit's position when it's born, when it *moves* (≈ once a minute per unit),
   and exactly when it dies — so an idle army would be invisible to a naive
   "sample the position events" approach. Checked by leave-one-out on real
   replays (hold out a known sample, predict it from its neighbours), linear
   interpolation lands within ≈10 map units at the median versus ≈24 for
   holding the last known position.
2. Clusters deaths into fights, classifies each action as macro or not, and
   splits both by fight / calm time.
3. Bins everything into a 64×64 grid (4 map units per cell) per snapshot, which
   serialises to ≈40 KB per game.
4. At query time `heatmaps.py` rotates games 180° where needed so *your* base is
   always lower-left (2-player maps are point-symmetric), sums by game-minute,
   and averages over the games still running at that minute. The browser
   Gaussian-smooths and colour-maps it (`static/js/heatmap.js`, no libraries).

**Times are real in-game seconds.** sc2reader's `event.second` is
`frame / 16`, but ladder games run at 22.4 frames/s, so it reads ~1.4× too
large on Faster. Everything here uses `frame / 22.4` (checked against the
replay's own length).

## Setup

Python 3.10+ and Redis.

```bash
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

**Redis** (the Celery broker and the live-progress store). It has no native
Windows build; the easiest route is Docker, bound to localhost only:

```bash
docker run -d --name sc2-redis --restart unless-stopped -p 127.0.0.1:6379:6379 redis:7-alpine
```

WSL2 or Memurai also work. Elsewhere? Set `REDIS_URL`.

## Run (three processes)

```bash
# 1. Redis — already running from above

# 2. a worker  (--pool=solo is the reliable choice on Windows; start more
#    workers in more terminals, with distinct -n names, to parse in parallel)
celery -A celery_app worker --pool=solo -l info -n w1@%h

# 3. the web app
python app.py                    # http://127.0.0.1:5000
```

Then: **Settings** (your in-game name — a battletag like `Name#1234` works) →
**My replays** → drag replay files *or a whole folder* onto the drop zone, or
paste a folder path. The same page under **Pro pool** builds the benchmark
set; [spawningtool.com](https://lotv.spawningtool.com/replays/?pro_only=on) is
a good source of recent pro replays.

If the dashboard says some replays came from an older version of the parser,
**Rebuild now** re-derives them from the original files; nothing is re-uploaded.

### Configuration

All optional environment variables:

| | default |
|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` |
| `SC2_REPLAY_DIR` | auto-detected under `Documents\StarCraft II\Accounts\…\Replays\Multiplayer` |
| `SC2_DB_PATH` | `./sc2.db` |
| `SC2_UPLOAD_DIR` / `SC2_PRO_DIR` | `./uploads` / `./pro_uploads` |

Your database, uploaded replays and the pro pool are git-ignored.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Unit tests cover the pure logic (fight clustering, interpolation, floating,
grid normalisation, patch filter, build-guide aggregation, the schema
migration). The Redis tests and the real-replay consistency test skip
themselves when Redis or replay files aren't available — point
`SC2_TEST_REPLAYS` at a folder of replays to enable the latter. They run
against a temporary database and Redis DB 15, never your data.

## Design notes & honest limits

- **Why a queue, when a parse takes ~50 ms?** A single replay is quick (a
  231-file folder is ~5 s in one process, mostly rejecting co-op and team
  games from the cheap player list before any heavy parsing). The queue earns
  its keep from fan-out across workers, live per-file progress, isolation (one
  corrupt file fails alone and is reported, instead of aborting a scan), retries
  when SQLite is briefly locked, and keeping the web process free — not from any
  single parse being slow.
- **Progress without a coordinator.** Each task `HINCRBY`s a counter hash and
  `XADD`s to a stream; both are atomic, so any number of workers can report at
  once. The SSE endpoint tails the stream with a blocking `XREAD`; the browser
  falls back to polling if `EventSource` isn't available.
- **Idempotent and re-derivable.** Rows are upserted by file name (uploads are
  also de-duplicated by content hash) and stamped with a `parser_version`. Change
  the extraction and bump it, and old rows are flagged stale and rebuilt from
  their source files. A rebuild that now rejects a replay removes its old row.
- **Positions are reconstructed, not recorded.** Read heatmaps as "where armies
  tended to be", not an exact trace (~10 map units median error).
- **Fights are a proxy**: clusters of army deaths with a killer. Merges (Archons)
  and morphs are excluded; small harassment under 3 deaths is not a fight.
- **APM is raw**, not "effective" APM — spam inflates it equally in both phases,
  so the *ratio* is the more trustworthy number.
- **Orientation assumes point-symmetric 2-player maps.** "All maps" heatmaps
  line up on your base; pick a single map for the most exact picture.
- **Excluded on purpose:** co-op, arcade and team games, and games against the
  built-in AI (their custom maps and economies aren't comparable).
- SC2 only. Brood War `.rep` files aren't supported (different format).
- Single user, local only, no authentication.

---

*Unofficial fan project, not affiliated with or endorsed by Blizzard
Entertainment. StarCraft® is a trademark of Blizzard Entertainment; the race
logos under `static/img/` are Blizzard's and are included only for
illustration. Parsing is done by [sc2reader](https://github.com/ggtracker/sc2reader).*
