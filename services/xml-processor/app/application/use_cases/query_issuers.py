from typing import Optional

from app.domain.exceptions.base import (
    DuplicateEntityException,
    EntityNotFoundException,
    ValidationException,
)
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository


class GetIssuerByNitUseCase:
    def __init__(self, issuer_repo: IssuerRepository):
        self._issuer_repo = issuer_repo

    def execute(self, nit: str):
        return self._issuer_repo.get_by_nit(nit)


class UpdateIssuerUseCase:
    def __init__(self, issuer_repo: IssuerRepository):
        self._issuer_repo = issuer_repo

    def execute(self, nit: str, fields: dict):
        """Aplica `fields` (solo lo enviado por el cliente) al emisor con ese NIT."""
        issuer = self._issuer_repo.get_by_nit(nit)
        if issuer is None:
            raise EntityNotFoundException("Issuer", nit)

        conflict = self._issuer_repo.find_conflict(
            issuer.id, fields.get("name"), fields.get("email")
        )
        if conflict:
            raise DuplicateEntityException("Issuer", f"{conflict}={fields[conflict]}")

        payment_id = fields.get("payment_id")
        if payment_id is not None:
            state = self._issuer_repo.payment_type_state(payment_id)
            if state is None:
                raise ValidationException(f"El medio de pago {payment_id} no existe.")
            if not state:
                raise ValidationException(f"El medio de pago {payment_id} está inactivo.")

        return self._issuer_repo.update(issuer, fields)


class ListIssuersUseCase:
    def __init__(self, issuer_repo: IssuerRepository):
        self._issuer_repo = issuer_repo

    def execute(self, q: Optional[str], limit: int, offset: int):
        items, total = self._issuer_repo.search(q, limit, offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}
