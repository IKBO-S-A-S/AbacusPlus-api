from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.infrastructure.config.database import Base


class ProcessingLog(Base):
    __tablename__ = "processing_logs"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    xml_filename = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False)  # added | duplicate | error
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    document_number = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    accounting_status = Column(String(20), nullable=True)  # triggered | error | null
    accounting_error = Column(Text, nullable=True)
    storage_status = Column(String(20), nullable=True)  # ok | error | null
    storage_error = Column(Text, nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow)
