from celery import Celery
from celery.schedules import crontab

from config import settings

celery_app = Celery(
    "subscriptions_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["tasks.subs"]
)

celery_app.conf.enable_utc = True

celery_app.conf.update(
    beat_schedule={
        # Keep order statuses in sync with the panel. Runs on its own so the
        # rest of the bot sees fresh statuses between reminder passes.
        "sync-order-status": {
            "task": "tasks.subs.sync_order_statuses",
            "schedule": crontab(minute="*/15"),
        },
        # Expiry / 90% usage reminders. The separate sync above keeps order
        # statuses fresh; these runs only send messages.
        "scan-subscriptions-reminders": {
            "task": "tasks.subs.scan_subscriptions",
            "schedule": crontab(minute="*/60"),
        },
        # One-time nudge for test accounts that were never used. The task
        # itself skips orders younger than 15 minutes and never sends twice.
        "unused-test-nudge": {
            "task": "tasks.subs.send_unused_test_nudge",
            "schedule": crontab(minute="*/15"),
        },
    }
)

