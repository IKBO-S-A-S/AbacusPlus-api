from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.infrastructure.persistence.models.product import Product


class ProductRepository:
    def __init__(self, db: Session):
        self.db = db

    def upsert_many(
        self,
        products: Iterable[dict],
        deactivate_missing: bool = False,
        replace: bool = False,
    ) -> int:
        """`replace=True` borra TODO el catálogo antes de insertar (modo `replace` del import
        por Excel). `deactivate_missing=True` solo borra los códigos ausentes del lote (lo usa
        el sync de SIIGO). Nada se confirma hasta el `commit()` final: si algo falla a mitad de
        camino, el `rollback()` deshace también el borrado, así que el catálogo nunca queda
        vacío por un archivo que fallaba a mitad de la importación.
        """
        items = list(products)
        incoming_codes = [str(item["code"]) for item in items if item.get("code")]

        try:
            if replace:
                self.db.query(Product).delete(synchronize_session=False)
                self.db.flush()
            elif deactivate_missing and incoming_codes:
                self.db.query(Product).filter(Product.code.notin_(incoming_codes)).delete(
                    synchronize_session=False
                )

            synced = 0
            for product in items:
                code = str(product["code"])
                model = self.db.query(Product).filter(Product.code == code).one_or_none()
                if model is None:
                    model = Product(code=code)
                    self.db.add(model)

                model.type = product["type"]
                model.description = product["description"]
                model.active = product.get("active", True)
                model.raw_payload = product.get("raw_payload", {})
                synced += 1

            self.db.commit()
            return synced
        except Exception:
            self.db.rollback()
            raise

    def list(self, active: Optional[bool] = None, type: Optional[str] = None) -> list[Product]:
        query = self.db.query(Product)
        if active is not None:
            query = query.filter(Product.active.is_(active))
        if type is not None:
            query = query.filter(Product.type == type.strip().lower())
        return query.order_by(Product.code.asc()).all()
