"""
Progress tracking for batches of replay-parsing jobs, on Redis.

A "batch" is one drag-and-drop or one folder scan: N replays fanned out as N
independent Celery tasks, which may run on any number of workers. Each task
reports its outcome here, and the browser follows along live:

  batch:<id>          HASH   counters (total / done / added / updated /
                             skipped / unmatched / failed) - the current state
  batch:<id>:events   STREAM one entry per finished replay - the history,
                             which the SSE endpoint tails with a blocking XREAD

The counters are updated with HINCRBY, which is atomic, so any number of
workers can report at once without a lock and without a coordinator.
"""

import time
import uuid

import redis

from config import REDIS_URL

BATCH_TTL = 24 * 3600
STREAM_MAXLEN = 5000
OUTCOMES = ("added", "updated", "skipped", "unmatched", "failed")

_client = None


def client():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def _key(batch_id):
    return f"batch:{batch_id}"


def create_batch(kind, total=None):
    """total=None means "a scan task is still listing the folder"; the batch
    isn't finished until set_total() says how many replays there are."""
    batch_id = uuid.uuid4().hex[:12]
    r = client()
    r.hset(_key(batch_id), mapping={
        "kind": kind, "total": total or 0, "listed": int(total is not None), "done": 0,
        "started": int(time.time()), **{o: 0 for o in OUTCOMES},
    })
    r.expire(_key(batch_id), BATCH_TTL)
    return batch_id


def set_total(batch_id, total):
    client().hset(_key(batch_id), mapping={"total": total, "listed": 1})


def record(batch_id, outcome, file="", detail=""):
    """Called by a worker when one replay is finished."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    r = client()
    pipe = r.pipeline()
    pipe.hincrby(_key(batch_id), "done", 1)
    pipe.hincrby(_key(batch_id), outcome, 1)
    pipe.xadd(f"{_key(batch_id)}:events",
              {"outcome": outcome, "file": file, "detail": detail},
              maxlen=STREAM_MAXLEN, approximate=True)
    pipe.expire(_key(batch_id), BATCH_TTL)
    pipe.expire(f"{_key(batch_id)}:events", BATCH_TTL)
    pipe.execute()


def snapshot(batch_id):
    """Current state of a batch as a dict of ints, or None if unknown."""
    raw = client().hgetall(_key(batch_id))
    if not raw:
        return None
    state = {k: int(v) for k, v in raw.items() if k != "kind"}
    state["kind"] = raw.get("kind", "")
    state["finished"] = bool(state.get("listed")) and state["done"] >= state["total"]
    return state


def read_events(batch_id, last_id="0", block_ms=15000, count=200):
    """Block until there are stream entries after `last_id` (or the timeout).
    Returns [(entry_id, fields), ...]."""
    result = client().xread({f"{_key(batch_id)}:events": last_id}, block=block_ms, count=count)
    if not result:
        return []
    return result[0][1]


def ping():
    try:
        return bool(client().ping())
    except redis.exceptions.RedisError:
        return False
