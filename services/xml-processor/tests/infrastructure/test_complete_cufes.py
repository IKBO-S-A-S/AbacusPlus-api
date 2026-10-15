from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository


def _doc(number, cufe, pdf=b"%PDF-1", xml=b"<x/>"):
    from datetime import datetime

    return Document(
        document_name=f"{number}.xml",
        document_number=number,
        date=datetime(2026, 10, 1),
        hour="",
        currency="COP",
        document_type="Factura",
        uuid=cufe or "",
        cufe=cufe,
        issuer_name="E",
        issuer_nit="1",
        receiver_name="R",
        receiver_nit="2",
        subtotal=1.0,
        total_taxes=0.0,
        total=1.0,
        status=100,
        pdf_data=pdf,
        xml_data=xml,
    )


def test_solo_cuenta_lo_que_tiene_pdf_y_xml(db_session):
    db_session.add_all(
        [
            _doc("A1", "aaa111"),
            _doc("A2", "bbb222", pdf=None),  # incompleto: se re-descarga para completarlo
            _doc("A3", "ccc333", xml=None),
            _doc("A4", None),
        ]
    )
    db_session.commit()

    result = DocumentRepository(db_session).get_complete_cufes(
        ["aaa111", "bbb222", "ccc333", "zzz999"]
    )

    assert result == {"aaa111"}


def test_encuentra_el_cufe_sin_importar_las_mayusculas(db_session):
    db_session.add(_doc("A1", "abc123"))
    db_session.commit()

    assert DocumentRepository(db_session).get_complete_cufes(["ABC123"]) == {"abc123"}


def test_lista_vacia_no_consulta(db_session):
    assert DocumentRepository(db_session).get_complete_cufes([]) == set()
