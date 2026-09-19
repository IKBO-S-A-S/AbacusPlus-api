import logging
from datetime import datetime
from typing import Optional

from fastapi import UploadFile

from app.domain.exceptions.base import DuplicateEntityException, FileProcessingException
from app.domain.ports.repositories import (
    ConceptRepositoryPort,
    DocumentRepositoryPort,
    IssuerRepositoryPort,
    ReceiverRepositoryPort,
    TaxRepositoryPort,
)
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.persistence.models.concept import ConceptDescription
from app.infrastructure.persistence.models.document import Document, DocumentDetail
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.models.receiver import Receiver
from app.infrastructure.persistence.models.tax import Tax
from app.utils.dian_dv import dv_calculate
from app.utils.xml_parser import parse_xml
from app.utils.zip_handler import extract_zip_file

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"xml", "zip"}


class ProcessXmlUseCase:
    """Orquesta el procesamiento de un archivo ZIP/XML DIAN.

    Flujo: extracción → parseo → deduplicación → enriquecimiento de líneas
    → persistencia → indexación RAG (best-effort) → asignación PUC (best-effort).
    Los clientes opcionales (rag_client, llm_client) se inyectan como None en tests.
    """

    def __init__(
        self,
        document_repo: DocumentRepositoryPort,
        issuer_repo: IssuerRepositoryPort,
        receiver_repo: ReceiverRepositoryPort,
        tax_repo: TaxRepositoryPort,
        concept_repo: ConceptRepositoryPort,
        rag_client=None,
        integration_config_client=None,
        llm_client=None,
    ):
        self.document_repo = document_repo
        self.issuer_repo = issuer_repo
        self.receiver_repo = receiver_repo
        self.tax_repo = tax_repo
        self.concept_repo = concept_repo
        self.rag_client = rag_client
        self.integration_config_client = integration_config_client
        self.llm_client = llm_client

    async def execute(self, file: UploadFile) -> dict:
        xml_content, filename = await self._extract_content(file)
        xml_data = parse_xml(xml_content)
        if not xml_data:
            raise FileProcessingException("Failed to parse XML. Please verify the file content.")

        logger.info("Processing document: %s", filename)

        duplicate = self.document_repo.get_by_document_number(xml_data.get("numero_documento", ""))
        if duplicate:
            raise DuplicateEntityException("Document", xml_data.get("numero_documento", ""))

        # Obtener impuestos de integration-config-service (best-effort, una vez por invocación)
        taxes: list[dict] = []
        if self.integration_config_client:
            taxes = await self.integration_config_client.get_taxes()

        issuer = self._ensure_issuer(xml_data)
        self._ensure_receiver(xml_data)
        self._ensure_tax(xml_data)
        document = self._build_document(xml_data, filename, payment_type_id=issuer.payment_id if issuer else None)
        self._build_details(document, xml_data, taxes=taxes)

        created = self.document_repo.create(document)
        logger.info("Document created with ID: %d", created.id)

        # Indexar en rag-service de forma best-effort
        if self.rag_client:
            content = self._build_chunk_content(xml_data, created)
            await self.rag_client.index_chunk(
                source_type="invoice",
                source_id=created.id,
                content=content,
            )

        # Disparar asignación de cuentas PUC en llm-service (best-effort)
        if self.llm_client:
            await self.llm_client.trigger_code_assignment(created.id)

        return {
            "status": "success",
            "data": {
                "id": created.id,
                "document_name": created.document_name,
                "document_number": created.document_number,
                "date": created.date,
                "document_type": created.document_type,
                "issuer_name": created.issuer_name,
                "issuer_nit": created.issuer_nit,
                "receiver_name": created.receiver_name,
                "receiver_nit": created.receiver_nit,
                "cufe": created.cufe,
                "total": created.total,
                "status": created.status,
                "details_count": len(created.details),
            },
            "document_id": created.id,
            "filename": filename,
        }

    async def _extract_content(self, file: UploadFile) -> tuple:
        file_extension = file.filename.split(".")[-1].lower()
        if file_extension not in ALLOWED_EXTENSIONS:
            raise FileProcessingException(
                "File format not allowed. Only ZIP and XML files are accepted."
            )
        if file_extension == "zip":
            return await extract_zip_file(file)
        xml_bytes = await file.read()
        return xml_bytes.decode("utf-8"), file.filename

    def _ensure_issuer(self, xml_data: dict) -> Issuer:
        emisor = xml_data.get("emisor", {})
        nit = emisor.get("nit", "")
        existing = self.issuer_repo.get_by_nit(nit)
        if existing:
            return existing
        contacto = emisor.get("contacto", {})
        return self.issuer_repo.create(
            Issuer(
                name=emisor.get("nombre", ""),
                nit=nit,
                dv=dv_calculate(nit),
                phone=contacto.get("telefono", ""),
                email=contacto.get("email", ""),
                tipo_contribuyente=emisor.get("regimen") or None,
            )
        )

    def _ensure_receiver(self, xml_data: dict) -> None:
        receptor = xml_data.get("receptor", {})
        nit = receptor.get("nit", "")
        if not self.receiver_repo.get_by_nit(nit):
            contacto = receptor.get("contacto", {})
            self.receiver_repo.create(
                Receiver(
                    name=receptor.get("nombre", ""),
                    nit=nit,
                    dv=dv_calculate(nit),
                    phone=contacto.get("telefono", ""),
                    email=contacto.get("email", ""),
                )
            )

    def _ensure_tax(self, xml_data: dict) -> None:
        impuestos = xml_data.get("impuestos", [])
        if not impuestos:
            return
        receiver_nit = xml_data.get("receptor", {}).get("nit", "")
        tax_name = impuestos[0].get("nombre", "")
        if not self.tax_repo.get_by_receiver_and_name(receiver_nit, tax_name):
            self.tax_repo.create(
                Tax(
                    receiver_nit=receiver_nit,
                    tax=tax_name,
                    percentage=float(impuestos[0].get("porcentaje") or 0),
                )
            )

    def _build_document(self, xml_data: dict, filename: str, payment_type_id: Optional[int] = None) -> Document:
        emisor = xml_data.get("emisor", {})
        receptor = xml_data.get("receptor", {})
        totales = xml_data.get("totales", {})

        tipo = xml_data.get("tipo_documento", {})
        doc_type = (
            (tipo.get("nombre") or tipo.get("codigo") or "")
            if isinstance(tipo, dict)
            else str(tipo or "")
        )

        retenciones = xml_data.get("retenciones", [])
        retefuente = sum(float(r.get("valor") or 0) for r in retenciones if r.get("codigo") == "06")
        reteica = sum(float(r.get("valor") or 0) for r in retenciones if r.get("codigo") == "07")

        return Document(
            document_name=filename,
            document_number=xml_data.get("numero_documento", ""),
            date=datetime.strptime(xml_data.get("fecha_emision", ""), "%Y-%m-%d"),
            hour=xml_data.get("hora_emision") or "",
            currency=xml_data.get("moneda") or "",
            document_type=doc_type,
            uuid=xml_data.get("cufe", "") or "",
            cufe=xml_data.get("cufe"),
            issuer_name=emisor.get("nombre", ""),
            issuer_nit=emisor.get("nit", ""),
            issuer_phone=emisor.get("contacto", {}).get("telefono", ""),
            issuer_email=emisor.get("contacto", {}).get("email", ""),
            receiver_name=receptor.get("nombre", ""),
            receiver_nit=receptor.get("nit", ""),
            receiver_phone=receptor.get("contacto", {}).get("telefono", ""),
            receiver_email=receptor.get("contacto", {}).get("email", ""),
            subtotal=float(totales.get("subtotal") or 0),
            total_taxes=float(totales.get("total_impuestos") or 0),
            total=float(totales.get("total") or 0),
            retefuente=retefuente,
            reteica=reteica,
            status=DocumentStatus.PROCESADO,
            payment_type_id=payment_type_id,
        )

    def _build_details(self, document: Document, xml_data: dict, taxes: Optional[list] = None) -> None:
        """Construye las líneas de detalle enriquecidas y las adjunta al documento.

        Enriquecimiento por línea:
        - concept_description_id: reutiliza descripciones previas del mismo receptor
          para mantener consistencia en el historial de asignaciones PUC.
        - tax_id: vincula al catálogo de integration-config-service (best-effort).
        - cost_center_id: hereda el centro de costo más frecuente para el mismo
          emisor+descripción, permitiendo que los documentos futuros no requieran
          asignación manual si el patrón ya existe.
        """
        receiver_nit = xml_data.get("receptor", {}).get("nit", "")
        issuer_nit = xml_data.get("emisor", {}).get("nit", "")
        for item in xml_data.get("items", []):
            description = item.get("descripcion", "")
            matched = self.concept_repo.find_matching_description(receiver_nit, description)
            if matched:
                concept_description_id = matched.id
            else:
                created = self.concept_repo.create_description(
                    ConceptDescription(receiver_nit=receiver_nit, description=description)
                )
                concept_description_id = created.id

            first_tax = (item.get("impuestos") or [{}])[0]
            tax_type_str = str(first_tax.get("porcentaje") or 0)
            tax_id = self._match_tax(tax_type_str, taxes or [])
            cost_center_id = self.document_repo.find_most_frequent_cost_center(
                issuer_nit, description
            )

            document.details.append(
                DocumentDetail(
                    description=description,
                    concept_description_id=concept_description_id,
                    quantity=float(item.get("cantidad") or 0),
                    unit=item.get("unidad_medida") or "",
                    price=float(item.get("precio_unitario") or 0),
                    subtotal=float(item.get("valor_total") or 0),
                    tax_type=tax_type_str,
                    tax_value=float(first_tax.get("valor") or 0),
                    total=float(item.get("valor_total") or 0) + float(first_tax.get("valor") or 0),
                    tax_id=tax_id,
                    cost_center_id=cost_center_id,
                )
            )

    @staticmethod
    def _match_tax(tax_type_str: str, taxes: list[dict]) -> Optional[int]:
        """Busca el id de integration_taxes que corresponde al porcentaje/nombre del ítem.

        Retorna None si tax_type_str es cero/vacío o no hay coincidencia (y loguea warning).

        Estrategias en orden:
        1. Comparación exacta por nombre (ej: "19.0" vs name "19.0") — funciona cuando
           el catálogo usa strings idénticos al XML DIAN.
        2. Comparación numérica con tolerancia 0.01 — cubre casos donde el catálogo
           guarda "19" y el XML emite "19.00" o viceversa.
        3. None + warning — impuesto no catalogado; el documento se guarda igual.
        """
        if not taxes or tax_type_str.strip() in ("0", "", "0.00", "0.0"):
            return None
        normalized = tax_type_str.strip().lower()
        for tax in taxes:
            if str(tax.get("name", "")).lower() == normalized:
                return tax["id"]
        try:
            pct = float(tax_type_str)
            for tax in taxes:
                if abs(float(tax.get("percentage", -999)) - pct) < 0.01:
                    return tax["id"]
        except (ValueError, TypeError):
            pass
        logger.warning(
            "Sin coincidencia de impuesto para tax_type=%r — se deja tax_id=None", tax_type_str
        )
        return None

    def _build_chunk_content(self, xml_data: dict, document) -> str:
        emisor = xml_data.get("emisor", {})
        receptor = xml_data.get("receptor", {})
        lines = [
            f"Factura {document.document_number} del {document.date}",
            f"Emisor: {emisor.get('nombre', '')} NIT {emisor.get('nit', '')}",
            f"Receptor: {receptor.get('nombre', '')} NIT {receptor.get('nit', '')}",
            f"Moneda: {document.currency} | Subtotal: {document.subtotal} | "
            f"Impuestos: {document.total_taxes} | Total: {document.total}",
            "Items:",
        ]
        for item in xml_data.get("items", []):
            lines.append(
                f"  - {item.get('descripcion', '')} | "
                f"cant: {item.get('cantidad', '')} | "
                f"precio: {item.get('precio_unitario', '')}"
            )
        return "\n".join(lines)
