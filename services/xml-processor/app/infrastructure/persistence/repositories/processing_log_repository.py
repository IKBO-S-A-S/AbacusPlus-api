from typing import Optional

from sqlalchemy.orm import Session

from app.infrastructure.persistence.models.processing_log import ProcessingLog


class ProcessingLogRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, log: ProcessingLog) -> ProcessingLog:
        self.db.add(log)
        self.db.commit()
        self.db.refresh(log)
        return log

    def get_all(
        self, status: Optional[str] = None, storage_status: Optional[str] = None
    ) -> list[ProcessingLog]:
        query = self.db.query(ProcessingLog)
        if status:
            query = query.filter(ProcessingLog.status == status)
        if storage_status:
            query = query.filter(ProcessingLog.storage_status == storage_status)
        return query.order_by(ProcessingLog.processed_at.desc()).all()
