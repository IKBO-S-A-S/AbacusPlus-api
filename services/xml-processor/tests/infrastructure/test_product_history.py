"""Historial de productos por tercero y verificación del catálogo de productos."""

from datetime import date

from app.application.dto.document import DocumentDetailCodeUpdateItem
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.persistence.models.document import Document, DocumentDetail
from app.infrastructure.persistence.models.integration_product import IntegrationProduct
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.persistence.repositories.product_repository import ProductRepository


def _doc(db, numero, nit, status, lineas):
    doc = Document(
        document_name=numero, document_number=numero, date=date(2026, 1, 1), hour="00:00",
        currency="COP", document_type="Factura", uuid=numero, issuer_name=nit, issuer_nit=nit,
        receiver_name="R", receiver_nit="9", subtotal=1, total_taxes=0, total=1, status=status,
    )
    db.add(doc)
    db.flush()
    for descripcion, code, tipo in lineas:
        db.add(
            DocumentDetail(
                document_id=doc.id, description=descripcion, concept_description_id=1,
                quantity=1, unit="u", price=1, subtotal=1, tax_type="0", tax_value=0, total=1,
                code=code, type=tipo,
            )
        )
    db.commit()
    return doc


def test_historial_solo_cuenta_productos_de_documentos_contabilizados(db_session):
    _doc(
        db_session, "F1", "900", DocumentStatus.CONTABILIZADA,
        [("Tornillo 1/4", "P-001", "Product"), ("Asesoría", "511500", "Account")],
    )
    _doc(db_session, "F2", "900", DocumentStatus.CONTABILIZADA, [("Tornillo hexagonal", "P-001", "Product")])
    _doc(db_session, "F3", "900", DocumentStatus.CAUSADO, [("Arandela", "P-002", "Product")])
    _doc(db_session, "F4", "111", DocumentStatus.CONTABILIZADA, [("Otro", "P-003", "Product")])

    historial = DocumentRepository(db_session).get_issuer_product_history("900")

    assert [h["code"] for h in historial] == ["P-001"]
    assert historial[0]["uses"] == 2
    assert historial[0]["descriptions"] == ["Tornillo 1/4", "Tornillo hexagonal"]


def test_historial_vacio_sin_productos_contabilizados(db_session):
    assert DocumentRepository(db_session).get_issuer_product_history("900") == []


def test_indice_de_productos_incluye_inactivos_y_excluye_servicios(db_session):
    db_session.add_all(
        [
            IntegrationProduct(code="P-001", type="product", description="Tornillo", active=True),
            IntegrationProduct(code="P-OFF", type="product", description="Viejo", active=False),
            IntegrationProduct(code="S-001", type="service", description="Asesoría", active=True),
        ]
    )
    db_session.commit()

    repo = ProductRepository(db_session)

    assert repo.index_by_code() == {"P-001": {"is_active": True}, "P-OFF": {"is_active": False}}
    assert [p.code for p in repo.get_active()] == ["P-001"]
    assert repo.has_active_products() is True


def test_catalogo_sin_productos_activos(db_session):
    assert ProductRepository(db_session).has_active_products() is False


def test_codigo_sin_tipo_deja_la_linea_como_cuenta():
    """Una línea que era Product y recibe una cuenta sin decir el tipo no puede seguir siéndolo."""
    item = DocumentDetailCodeUpdateItem(detail_id=1, code="511500")
    assert item.model_dump(exclude_unset=True)["type"] == "Account"


def test_cambiar_solo_el_tipo_se_rechaza_para_no_cruzar_tipo_y_codigo():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DocumentDetailCodeUpdateItem(detail_id=1, type="Product")


def test_tipo_explicito_se_respeta_y_sin_codigo_no_se_inventa_tipo():
    assert (
        DocumentDetailCodeUpdateItem(detail_id=1, code="P-001", type="Product").type == "Product"
    )
    assert "type" not in DocumentDetailCodeUpdateItem(detail_id=1, cost_center_id=2).model_dump(
        exclude_unset=True
    )
