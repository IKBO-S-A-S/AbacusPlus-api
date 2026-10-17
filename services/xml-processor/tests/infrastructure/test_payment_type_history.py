from datetime import date

from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.persistence.models.document import Document, DocumentDetail
from app.infrastructure.persistence.models.integration_payment_type import IntegrationPaymentType
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository


def _doc(db, nit, number, status, payment, description):
    doc = Document(
        document_name=number, document_number=number, date=date(2026, 1, 1), hour="00:00",
        currency="COP", document_type="Factura", uuid=number, issuer_name=nit, issuer_nit=nit,
        receiver_name="R", receiver_nit="9", subtotal=1, total_taxes=0, total=1,
        status=status, payment_type_id=payment,
    )
    db.add(doc)
    db.flush()
    db.add(DocumentDetail(
        document_id=doc.id, description=description, concept_description_id=1, quantity=1,
        unit="u", price=1, subtotal=1, tax_type="0", tax_value=0, total=1,
    ))
    db.commit()


def _seed_types(db):
    db.add_all([
        IntegrationPaymentType(id=1, name="Contado", type="x", active=True),
        IntegrationPaymentType(id=2, name="Crédito", type="x", active=True),
        IntegrationPaymentType(id=3, name="Viejo", type="x", active=False),
    ])
    db.commit()


def test_historial_del_emisor_solo_cuenta_contabilizados_y_activos(db_session):
    _seed_types(db_session)
    C, P = DocumentStatus.CONTABILIZADA, DocumentStatus.PROCESADO
    _doc(db_session, "1", "a", C, 2, "servicio aseo")
    _doc(db_session, "1", "b", C, 2, "servicio aseo")
    _doc(db_session, "1", "c", C, 1, "servicio aseo")
    _doc(db_session, "1", "d", P, 1, "servicio aseo")  # no contabilizado: no cuenta
    _doc(db_session, "1", "e", C, 3, "servicio aseo")  # tipo inactivo: no cuenta
    _doc(db_session, "2", "f", C, 1, "servicio aseo")  # otro emisor
    repo = DocumentRepository(db_session)
    assert repo.find_most_frequent_payment_type("1") == 2
    assert repo.find_most_frequent_payment_type("9") is None


def test_similares_excluye_al_propio_emisor_y_cuenta_cada_documento_una_vez(db_session):
    _seed_types(db_session)
    C = DocumentStatus.CONTABILIZADA
    _doc(db_session, "1", "a", C, 2, "papeleria oficina")  # propio emisor: excluido
    _doc(db_session, "2", "b", C, 1, "papeleria oficina")
    _doc(db_session, "3", "c", C, 2, "otra cosa")
    repo = DocumentRepository(db_session)
    assert repo.find_most_frequent_payment_type_for_similar("1", ["papeleria varia"]) == 1
    assert repo.find_most_frequent_payment_type_for_similar("1", ["zzz"]) is None
    assert repo.find_most_frequent_payment_type_for_similar("1", []) is None
