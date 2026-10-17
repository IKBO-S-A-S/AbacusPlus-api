from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.domain.ports.repositories import IssuerRepositoryPort
from app.infrastructure.persistence.models.integration_payment_type import IntegrationPaymentType
from app.infrastructure.persistence.models.issuer import Issuer


class IssuerRepository(IssuerRepositoryPort):
    def __init__(self, db: Session):
        self.db = db

    def get_by_nit(self, nit: str) -> Optional[Issuer]:
        return self.db.query(Issuer).filter(Issuer.nit == nit).first()

    def search(self, q: Optional[str], limit: int, offset: int) -> tuple[list[Issuer], int]:
        query = self.db.query(Issuer)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(
                or_(Issuer.name.ilike(like), Issuer.nit.ilike(like), Issuer.email.ilike(like))
            )
        total = query.count()
        items = query.order_by(Issuer.name, Issuer.id).offset(offset).limit(limit).all()
        return items, total

    def find_conflict(
        self, issuer_id: int, name: Optional[str], email: Optional[str]
    ) -> Optional[str]:
        """Campo único (`name` o `email`) que ya usa OTRO emisor, o None."""
        if name is not None:
            taken = self.db.query(Issuer.id).filter(Issuer.name == name, Issuer.id != issuer_id)
            if taken.first():
                return "name"
        if email is not None:
            taken = self.db.query(Issuer.id).filter(Issuer.email == email, Issuer.id != issuer_id)
            if taken.first():
                return "email"
        return None

    def payment_type_state(self, payment_id: int) -> Optional[bool]:
        """`active` del medio de pago, o None si no existe."""
        row = (
            self.db.query(IntegrationPaymentType.active)
            .filter(IntegrationPaymentType.id == payment_id)
            .first()
        )
        return None if row is None else bool(row[0])

    def update(self, issuer: Issuer, fields: dict) -> Issuer:
        for key, value in fields.items():
            setattr(issuer, key, value)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(issuer)
        return issuer

    def create(self, issuer: Issuer) -> Issuer:
        self.db.add(issuer)
        self.db.commit()
        self.db.refresh(issuer)
        return issuer
