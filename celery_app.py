"""
Celery application. The Flask app only enqueues work; one or more
`celery -A celery_app worker` processes do the parsing, so the web process
never blocks on a replay.

Redis is both the broker and (via events.py) the live progress store. Task
return values aren't kept - results land in SQLite and progress in Redis -
so the result backend is disabled.

Point it elsewhere with REDIS_URL.
"""

from celery import Celery

from config import REDIS_URL

celery_app = Celery("sc2_tasks", broker=REDIS_URL, include=["tasks"])

celery_app.conf.update(
    # A named queue (workers pick it up automatically, no -Q flag needed) so
    # this app doesn't share Celery's default "celery" queue with anything else
    # that happens to use the same Redis.
    task_default_queue="sc2_replays",
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    # Parsing is CPU-bound and tasks are short: take one at a time so work is
    # spread evenly across however many workers are running, and acknowledge
    # only after a task finishes so a crashed worker's replay is retried.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600},
)
