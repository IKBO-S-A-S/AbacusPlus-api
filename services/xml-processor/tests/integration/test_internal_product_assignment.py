"""PATCH /internal/documents/{id}/details con líneas de producto.

Cubre lo que decide el servidor y no puede delegar al llm-service:

- Una línea `Product` se valida contra el catálogo de productos (no contra el PUC).
- Un tercero en `account` nunca recibe productos, aunque el llm-service los mande.
- El modo de un tercero sin definir solo se fija cuando la detección corrió, y queda auditado.
"""

from datetime import date, datetime, timezone

import pytest

from app.adapters.api.routers.internal import update_detail_codes_internal
from app.application.dto.document import DocumentDetailCodeUpdateItem
from app.infrastructure.persistence.models.accounting import DocumentFieldChange
from app.infrastructure.persistence.models.document import Document, DocumentDetail
from app.infrastructure.persistence.models.integration_product import IntegrationProduct
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.models.puc import PucAccount

NIT = "900111222"


def _crear(db, modo=None):
    db.add(
        Issuer(name="Ferretería SAS", nit=NIT, dv=1, email="f@x.co", item_assignment_mode=modo)
    )
    db.add(PucAccount(code="613505", name="Comercio", is_active=True, accepts_movements=True))
    db.add(IntegrationProduct(code="P-001", type="product", description="Tornillo", active=True))
    doc = Document(
        document_name="t.xml", document_number="F1", date=date(2026, 1, 1), hour="10:00",
        currency="COP", document_type="Factura", uuid="u-1", issuer_name="Ferretería SAS",
        issuer_nit=NIT, receiver_name="R", receiver_nit="9", subtotal=1, total_taxes=0,
        retefuente=0, reteica=0, total=1, register_at=datetime.now(timezone.utc), status=200,
    )
    db.add(doc)
    db.flush()
    lineas = []
    for descripcion in ("Tornillo", "Servicio"):
        d = DocumentDetail(
            document_id=doc.id, description=descripcion, concept_description_id=1, quantity=1,
            unit="u", price=1, subtotal=1, tax_type="0", tax_value=0, total=1,
        )
        db.add(d)
        lineas.append(d)
    db.commit()
    return doc, lineas


def _producto(detail_id, code="P-001"):
    return DocumentDetailCodeUpdateItem(detail_id=detail_id, code=code, type="Product")


def _cuenta(detail_id, code="613505"):
    return DocumentDetailCodeUpdateItem(detail_id=detail_id, code=code, type="Account")


def _modo(db):
    db.expire_all()
    return db.query(Issuer).filter_by(nit=NIT).one().item_assignment_mode


def _cambios(db):
    return db.query(DocumentFieldChange).filter_by(entity="issuer").all()


def test_producto_valido_se_guarda_y_mezcla_con_cuenta(db_session):
    doc, (l1, l2) = _crear(db_session, modo="product")

    r = update_detail_codes_internal(
        doc.id, [_producto(l1.id), _cuenta(l2.id)], product_detection_ran=True, db=db_session
    )

    db_session.expire_all()
    assert r.updated == 2 and r.rejected == []
    lineas = {d.id: d for d in db_session.query(DocumentDetail).all()}
    assert (lineas[l1.id].type, lineas[l1.id].code) == ("Product", "P-001")
    assert (lineas[l2.id].type, lineas[l2.id].code) == ("Account", "613505")
    assert lineas[l1.id].code_source == "llm"


def test_producto_inexistente_en_el_catalogo_se_rechaza(db_session):
    doc, (l1, _) = _crear(db_session, modo="product")

    r = update_detail_codes_internal(
        doc.id, [_producto(l1.id, "P-999")], product_detection_ran=True, db=db_session
    )

    assert r.updated == 0
    assert "catálogo de productos" in r.rejected[0]


def test_tercero_en_cuenta_rechaza_productos_aunque_lleguen(db_session):
    doc, (l1, l2) = _crear(db_session, modo="account")

    r = update_detail_codes_internal(
        doc.id, [_producto(l1.id), _cuenta(l2.id)], product_detection_ran=True, db=db_session
    )

    db_session.expire_all()
    assert r.updated == 1, "la cuenta sí se guarda"
    assert "asignarse siempre por cuenta" in r.rejected[0]
    assert db_session.get(DocumentDetail, l1.id).code is None
    assert _modo(db_session) == "account", "un modo ya definido no se toca"


def test_sin_definir_con_un_producto_el_tercero_pasa_a_product_y_queda_auditado(db_session):
    doc, (l1, l2) = _crear(db_session)

    update_detail_codes_internal(
        doc.id, [_producto(l1.id), _cuenta(l2.id)], product_detection_ran=True, db=db_session
    )

    assert _modo(db_session) == "product"
    (cambio,) = _cambios(db_session)
    assert (cambio.field, cambio.old_value, cambio.new_value) == (
        "item_assignment_mode", None, "product",
    )
    assert cambio.changed_by == "llm"
    assert cambio.document_id == doc.id


def test_sin_definir_y_solo_cuentas_el_tercero_pasa_a_account(db_session):
    doc, (l1, l2) = _crear(db_session)

    update_detail_codes_internal(
        doc.id, [_cuenta(l1.id), _cuenta(l2.id)], product_detection_ran=True, db=db_session
    )

    assert _modo(db_session) == "account"


@pytest.mark.parametrize("asignaciones", ["productos", "cuentas"])
def test_sin_aviso_de_deteccion_el_modo_no_se_fija(db_session, asignaciones):
    """Una caída del catálogo no debe quedar como «este tercero no vende productos»."""
    doc, (l1, l2) = _crear(db_session)
    items = [_producto(l1.id)] if asignaciones == "productos" else [_cuenta(l1.id)]

    update_detail_codes_internal(doc.id, items, product_detection_ran=False, db=db_session)

    assert _modo(db_session) is None
    assert _cambios(db_session) == []


def test_un_modo_ya_definido_no_cambia_con_otra_factura(db_session):
    doc, (l1, l2) = _crear(db_session, modo="product")

    update_detail_codes_internal(
        doc.id, [_cuenta(l1.id), _cuenta(l2.id)], product_detection_ran=True, db=db_session
    )

    assert _modo(db_session) == "product"
    assert _cambios(db_session) == []
