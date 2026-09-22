"""Nombre de archivo para los artefactos (PDF/XML) publicados en los backends de
almacenamiento del tenant. Un solo lugar para esta lógica: antes vivía duplicada entre el
worker de descargas (nombraba por el patrón configurable) y la publicación en S3 (nombraba
solo por `document_number`), lo que producía dos subidas con dos nombres distintos para el
mismo documento.
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def sanitize_name(text: str) -> str:
    """Elimina caracteres especiales y normaliza el texto para usar en nombres de archivo."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_document_filename(pattern: str, document_data: dict) -> str:
    """Construye el nombre base (sin extensión) a partir del patrón configurable del
    tenant. Mismo nombre base para PDF y XML — solo cambia la extensión."""
    try:
        date_str = document_data.get("date").strftime("%Y%m%d")
    except Exception:
        date_str = str(document_data.get("date") or "").replace("-", "")[:8]

    placeholders = _SafeDict(
        nit_emisor=sanitize_name(document_data.get("issuer_nit") or ""),
        nombre_emisor=sanitize_name(document_data.get("issuer_name") or ""),
        nit_receptor=sanitize_name(document_data.get("receiver_nit") or ""),
        nombre_receptor=sanitize_name(document_data.get("receiver_name") or ""),
        tipo_documento=sanitize_name(document_data.get("document_type") or ""),
        numero_documento=sanitize_name(document_data.get("document_number") or ""),
        fecha_emision=date_str,
        cufe=sanitize_name(document_data.get("cufe") or ""),
    )
    try:
        name = pattern.format_map(placeholders)
    except Exception as e:
        logger.warning("Patron de nombre invalido (%s), usando fallback: %s", pattern, e)
        name = f"IKB - DOCU - {date_str} - V01 - {placeholders['tipo_documento']} {placeholders['numero_documento']} {placeholders['nombre_emisor']}"
    return sanitize_name(name) or f"documento-{placeholders['numero_documento'] or 'sin-numero'}"
