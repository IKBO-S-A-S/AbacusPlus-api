from collections import Counter
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.ports.repositories import DocumentRepositoryPort
from app.infrastructure.persistence.models.document import Document, DocumentDetail


class DocumentRepository(DocumentRepositoryPort):
    def __init__(self, db: Session):
        self.db = db

    def get_by_document_number(self, document_number: str) -> Optional[Document]:
        return self.db.query(Document).filter(Document.document_number == document_number).first()

    def get_by_id(self, document_id: int) -> Optional[Document]:
        return self.db.query(Document).filter(Document.id == document_id).first()

    def get_by_date_range(
        self, date_start: date, date_end: date, status: Optional[int] = None
    ) -> list[Document]:
        q = self.db.query(Document).filter(
            Document.date >= date_start,
            Document.date <= date_end,
        )
        if status is not None:
            q = q.filter(Document.status == status)
        return q.all()

    def create(self, document: Document) -> Document:
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def update_status(self, document_id: int, new_status: str) -> Optional[Document]:
        doc = self.get_by_id(document_id)
        if doc is None:
            return None
        doc.status = new_status
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def update_detail_codes(self, assignments: list[dict]) -> int:
        """Actualiza campos de document_details. Solo modifica los campos presentes (no None).

        Campos soportados: code, type, cost_center_id, tax_id.
        """
        updated = 0
        for item in assignments:
            row = self.db.query(DocumentDetail).filter(DocumentDetail.id == item["detail_id"]).first()
            if row is None:
                continue
            if item.get("code") is not None:
                row.code = item["code"]
            if item.get("type") is not None:
                row.type = item["type"]
            if item.get("cost_center_id") is not None:
                row.cost_center_id = item["cost_center_id"]
            if item.get("tax_id") is not None:
                row.tax_id = item["tax_id"]
            updated += 1
        self.db.commit()
        return updated

    def update_payment_type(self, document_id: int, payment_type_id: int) -> Optional[Document]:
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        row.payment_type_id = payment_type_id
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_storage_locations(
        self, document_id: int, pdf_locations: dict, xml_locations: dict
    ) -> Optional[Document]:
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        row.pdf_storage_locations = pdf_locations
        row.xml_storage_locations = xml_locations
        self.db.commit()
        self.db.refresh(row)
        return row

    def find_most_frequent_cost_center(
        self, issuer_nit: str, description: str
    ) -> Optional[int]:
        """Busca el cost_center_id más usado históricamente para descripciones
        similares del mismo emisor. Retorna None si no hay historial."""
        words = [w for w in description.strip().split() if len(w) > 3]
        if not words:
            return None
        pattern = f"%{words[0]}%"
        rows = (
            self.db.query(DocumentDetail.cost_center_id)
            .join(Document, DocumentDetail.document_id == Document.id)
            .filter(
                Document.issuer_nit == issuer_nit,
                DocumentDetail.cost_center_id.isnot(None),
                DocumentDetail.description.ilike(pattern),
            )
            .all()
        )
        if not rows:
            return None
        counts = Counter(row.cost_center_id for row in rows)
        return counts.most_common(1)[0][0]
