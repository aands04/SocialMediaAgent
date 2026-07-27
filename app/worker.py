import time

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import JobStatus, PublicationJob

log=structlog.get_logger(); settings=get_settings()
def run():
    log.info("worker_started",mode=settings.publisher_mode)
    while True:
        from pathlib import Path
        Path("/tmp/worker-heartbeat").touch()
        with SessionLocal() as db:
            due=db.scalars(select(PublicationJob).where(PublicationJob.status.in_([JobStatus.SCHEDULED,JobStatus.RETRY])).limit(20)).all()
            if due: log.info("publication_jobs_due",count=len(due))
        time.sleep(15)
if __name__=="__main__": run()
