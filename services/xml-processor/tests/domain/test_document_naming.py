from datetime import date

from app.domain.services.document_naming import build_document_filename as _build_filename


class TestBuildFilename:
    def test_applies_pattern_with_placeholders(self):
        pattern = "{nit_emisor} - {numero_documento} - {fecha_emision}"
        document_data = {
            "issuer_nit": "900123456",
            "document_number": "FE-001",
            "date": date(2024, 3, 15),
        }
        assert _build_filename(pattern, document_data) == "900123456 - FE-001 - 20240315"

    def test_missing_placeholder_data_becomes_empty(self):
        pattern = "{nit_emisor}-{numero_documento}"
        document_data = {"document_number": "FE-002", "date": date(2024, 1, 1)}
        assert _build_filename(pattern, document_data) == "-FE-002"

    def test_default_ikb_pattern(self):
        pattern = (
            "IKB - DOCU - {fecha_emision} - V01 - {tipo_documento} "
            "{numero_documento} {nombre_emisor}"
        )
        document_data = {
            "date": date(2024, 6, 1),
            "document_type": "Factura",
            "document_number": "FE-100",
            "issuer_name": "Acme S.A.S.",
        }
        result = _build_filename(pattern, document_data)
        assert result == "IKB - DOCU - 20240601 - V01 - Factura FE-100 Acme S A S"

    def test_invalid_pattern_falls_back_without_raising(self):
        pattern = "{nombre_emisor"  # llave sin cerrar
        document_data = {
            "date": date(2024, 6, 1),
            "document_type": "Factura",
            "document_number": "FE-100",
            "issuer_name": "Acme",
        }
        result = _build_filename(pattern, document_data)
        assert "FE-100" in result

    def test_sanitizes_special_characters(self):
        pattern = "{nombre_emisor}"
        document_data = {"issuer_name": "Ñoño & Cía. S.A.", "date": date(2024, 1, 1)}
        result = _build_filename(pattern, document_data)
        assert "&" not in result
        assert "." not in result
