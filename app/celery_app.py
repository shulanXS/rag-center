from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "rag_center",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    imports=[
        "app.tasks.indexing",
        "app.tasks.research",
        "app.tasks.cleanup",
    ],
)

# Daily retention sweep at 03:00 UTC. Disabled when retention is 0
# (the task itself no-ops), so it is safe to leave enabled here.
celery_app.conf.beat_schedule = {
    "purge-expired-documents-daily": {
        "task": "app.tasks.cleanup.purge_expired_documents",
        # 03:00 UTC every day
        "schedule": crontab(hour=3, minute=0),
    },
}
