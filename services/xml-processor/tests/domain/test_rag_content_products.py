"""RF-08: el conocimiento indexado distingue cuentas de productos."""

from types import SimpleNamespace

from app.domain.services.rag_content import (
    build_accounted_knowledge_content,
    build_accounted_knowledge_metadata,
    build_accounted_knowledge_signature,
)

PAYLOAD = {
    "items": [
        {"type": "Product", "code": "P-001", "description": "Tornillo 1/4", "quantity": 10, "price": 100},
        {"type": "Account", "code": "613505", "description": "Flete", "quantity": 1, "price": 50},
    ]
}


def _doc():
    return SimpleNamespace(
        document_number="F1", date="2026-01-01", issuer_name="Ferretería", issuer_nit="900123456-7",
        receiver_name="Mi empresa", receiver_nit="800", currency="COP", subtotal=1, total_taxes=0,
        total=1, document_type="Factura", cost_center_id=None, payment_type_id=None, details=[],
    )


def test_el_texto_rotula_cada_linea_como_producto_o_cuenta():
    texto = build_accounted_knowledge_content(_doc(), payload=PAYLOAD)

    assert "Tornillo 1/4 | producto: P-001" in texto
    assert "Flete | cuenta: 613505" in texto
    assert "productos P-001" in texto


def test_la_firma_que_se_embebe_distingue_producto_de_cuenta():
    firma = build_accounted_knowledge_signature(_doc(), payload=PAYLOAD)

    assert "Tornillo 1/4 → producto P-001" in firma
    assert "Flete → cuenta 613505" in firma


def test_los_codigos_de_producto_no_contaminan_account_codes():
    meta = build_accounted_knowledge_metadata(_doc(), payload=PAYLOAD)

    assert meta["account_codes"] == ["613505"]
    assert meta["product_codes"] == ["P-001"]


def test_sin_productos_el_texto_no_cambia_de_forma():
    payload = {"items": [{"type": "Account", "code": "613505", "description": "Flete"}]}

    texto = build_accounted_knowledge_content(_doc(), payload=payload)

    assert "producto" not in texto.replace("(concepto → cuenta o producto)", "")
    assert build_accounted_knowledge_metadata(_doc(), payload=payload)["product_codes"] == []
