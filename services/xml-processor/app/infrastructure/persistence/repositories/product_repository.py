from sqlalchemy.orm import Session

from app.infrastructure.persistence.models.integration_product import IntegrationProduct

#: `integration_products.type` de lo que es inventariable. Los servicios no llevan código de
#: producto en la causación: van por cuenta.
PRODUCT_TYPE = "product"


class ProductRepository:
    """Lectura del catálogo de productos del tenant (lo escribe integration-config-service)."""

    def __init__(self, db: Session):
        self._db = db

    def get_active(self) -> list[IntegrationProduct]:
        return (
            self._db.query(IntegrationProduct)
            .filter(
                IntegrationProduct.active.is_(True),
                IntegrationProduct.type == PRODUCT_TYPE,
            )
            .order_by(IntegrationProduct.code)
            .all()
        )

    def index_by_code(self) -> dict:
        """Productos indexados por código, en la forma que espera
        `validate_assignments(product_index=...)`."""
        rows = (
            self._db.query(IntegrationProduct)
            .filter(IntegrationProduct.type == PRODUCT_TYPE)
            .all()
        )
        # Incluye los inactivos para que el rechazo diga «inactivo» y no «no existe».
        return {p.code: {"is_active": bool(p.active)} for p in rows}

    def has_active_products(self) -> bool:
        return (
            self._db.query(IntegrationProduct.id)
            .filter(
                IntegrationProduct.active.is_(True),
                IntegrationProduct.type == PRODUCT_TYPE,
            )
            .first()
            is not None
        )
