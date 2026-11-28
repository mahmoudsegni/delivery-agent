"""Celery application configuration."""

from celery import Celery
from celery.schedules import crontab

from app.config import settings


def _crontab_from_str(expr: str) -> crontab:
    """Parse a 5-field cron string into a celery crontab."""
    minute, hour, dom, month, dow = expr.split()
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month_of_year=month,
        day_of_week=dow,
    )


# Scheduled tasks (run by `celery beat`).
_beat_schedule: dict = {
    # Daily standup kickoff.
    "daily-standup": {
        "task": "app.tasks.standup_tasks.start_scheduled_standup",
        "schedule": _crontab_from_str(settings.standup_schedule_cron),
    },
    # Mid-round nudge for members who haven't answered yet.
    "standup-nudge": {
        "task": "app.tasks.standup_tasks.nudge_pending_standup",
        "schedule": _crontab_from_str(settings.standup_nudge_cron),
    },
    # Daily release-confidence snapshot (builds the trend series).
    "daily-confidence-snapshot": {
        "task": "app.tasks.confidence_tasks.snapshot_active_sprints",
        "schedule": _crontab_from_str(settings.confidence_snapshot_cron),
    },
    # Safety-net poller for coding runs stuck in ci_pending.
    "poll-inflight-coding-ci": {
        "task": "app.tasks.coding_tasks.poll_inflight_coding_ci",
        "schedule": float(settings.coding_agent_ci_poll_interval),
    },
    # Autopilot control loop. ALWAYS registered — the runtime mode
    # (off/propose/auto) is read from the DB each tick, which is what makes
    # the dial live-togglable without a beat restart.
    "autopilot-tick": {
        "task": "app.tasks.autopilot_tasks.autopilot_tick",
        "schedule": float(settings.autopilot_tick_interval),
    },
}
# Daily morning brief (generated + pushed to Telegram by the main worker).
if settings.morning_brief_enabled:
    _beat_schedule["morning-brief"] = {
        "task": "app.tasks.brief_tasks.generate_morning_brief",
        "schedule": _crontab_from_str(settings.morning_brief_cron),
    }
# LLM budget watchdog: DM when today's tokens cross the daily cap (issue #62).
if settings.llm_daily_token_budget > 0:
    _beat_schedule["llm-budget-check"] = {
        "task": "app.tasks.llm_tasks.check_llm_budget_task",
        "schedule": 1800.0,  # 30-min checks: a crossing lands within ~30 min
    }
# Periodic pull from the external task provider — only when explicitly enabled
# (the mock provider would otherwise flood the DB with new tasks each run).
if settings.task_sync_enabled:
    _beat_schedule["sync-external-tasks"] = {
        "task": "app.tasks.tfs_tasks.sync_tfs_tasks",
        "schedule": float(settings.tfs_sync_interval),
    }
    # Watchdog: DM when that pull stalls (alert once per stall episode).
    _beat_schedule["sync-stall-check"] = {
        "task": "app.tasks.tfs_tasks.check_sync_staleness_task",
        "schedule": float(settings.sync_check_interval),
    }


# Create Celery application
celery_app = Celery(
    "delivery_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.agent_tasks",
        "app.tasks.standup_tasks",
        "app.tasks.tfs_tasks",
        "app.tasks.confidence_tasks",
        "app.tasks.coding_tasks",
        "app.tasks.brief_tasks",
        "app.tasks.autopilot_tasks",
        "app.tasks.llm_tasks",
    ],
)

# Task configuration
celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Time limits (in seconds)
    # Soft limit sends SIGTERM, hard limit sends SIGKILL
    task_soft_time_limit=300,  # 5 minutes for LLM operations
    task_time_limit=360,       # 6 minutes hard limit

    # Result backend settings
    result_expires=3600,  # Results expire after 1 hour

    # Task routing. Long Claude Code runs get a dedicated "coding" queue so
    # they never starve standups/sync; poll/merge stay fast on "agent".
    # ORDER MATTERS: exact task names must precede the glob so they win.
    task_routes={
        "app.tasks.coding_tasks.dispatch_coding_agent": {"queue": "coding"},
        "app.tasks.coding_tasks.revise_coding_run": {"queue": "coding"},
        "app.tasks.agent_tasks.*": {"queue": "agent"},
        "app.tasks.coding_tasks.*": {"queue": "agent"},
    },

    # Default queue for tasks without explicit routing
    task_default_queue="default",

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Worker settings
    worker_prefetch_multiplier=1,  # One task at a time per worker
    worker_concurrency=4,          # Number of concurrent workers

    # Scheduled tasks (run by `celery beat`)
    beat_schedule=_beat_schedule,
)
