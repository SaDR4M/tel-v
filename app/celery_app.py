from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "subscriptions_tasks",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
    include=["tasks.subs"]
)

celery_app.conf.enable_utc = True

celery_app.conf.update(
    beat_schedule={
        "scan-subscriptions-remoinders": {
            "task": "tasks.subs.scan_subscriptions",
            "schedule": crontab(minute="*/1"),
        }
    }
)

