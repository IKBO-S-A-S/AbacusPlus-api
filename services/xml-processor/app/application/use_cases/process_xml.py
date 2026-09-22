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
from app.domain.services.line_taxes import (
    extraer_impuestos_de_linea,
    impuesto_principal,
    total_de_impuestos,
)
from app.domain.services.tax_resolution import resolver_impuesto
from app.domain.services.xml_withholdings import extraer_retenciones_del_xml, total_retenido
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
    → persistencia → asignación PUC (best-effort).

    RF-08: el procesamiento NO alimenta el RAG. El documento recién creado todavía no tiene
    ninguna decisión contable validada, así que no puede servir de precedente; el
    conocimiento se genera al contabilizarlo en SIIGO.
    Los clientes opcionales (llm_client) se inyectan como None en tests.
    """

    def __init__(
        self,
        document_repo: DocumentRepositoryPort,
        issuer_repo: IssuerRepositoryPort,
        receiver_repo: ReceiverRepositoryPort,
        tax_repo: TaxRepositoryPort,
        concept_repo: ConceptRepositoryPort,
        integration_config_client=None,
        llm_client=None,
        tenant_slug: str = "",
    ):
        self.document_repo = document_repo
        self.issuer_repo = issuer_repo
        self.receiver_repo = receiver_repo
        self.tax_repo = tax_repo
        self.concept_repo = concept_repo
        self.integration_config_client = integration_config_client
        self.llm_client = llm_client
        #: Tenant de la ejecución. Vacío en la vía interactiva (el cliente lleva el JWT) y
        #: explícito en la del worker de descargas DIAN, que corre sin sesión de usuario.
        self.tenant_slug = tenant_slug

    async def execute(self, file: UploadFile) -> dict:
        xml_content, filename, xml_bytes, pdf_bytes = await self._extract_content(file)
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
        document = self._build_document(
            xml_data, filename, payment_type_id=issuer.payment_id if issuer else None
        )
        self._build_details(document, xml_data, taxes=taxes)

        # RF-03: conserva el respaldo del documento igual que la descarga automática desde la
        # DIAN (`infrastructure/queue/download_queue.py`). Sin esto, un documento cargado a
        # mano no tiene bytes propios ni en `pdf_data`/`xml_data` ni, por lo tanto, en el
        # enlace S3 que se deriva de ellos: `GET /documents/{id}/pdf|xml` respondía 404.
        document.xml_data = xml_bytes
        if pdf_bytes:
            document.pdf_data = pdf_bytes
            document.pdf_source = "dian_official"

        created = self.document_repo.create(document)
        logger.info("Document created with ID: %d", created.id)

        # RF-08: aquí NO se indexa nada en el RAG.
        #
        # Un documento recién procesado no tiene todavía ninguna decisión contable: sus
        # cuentas son las que propuso el sistema y sus retenciones ni siquiera se han
        # revisado. Indexarlo lo convertiría en precedente de sí mismo, y el modelo acabaría
        # aprendiendo de sus propias sugerencias sin que ninguna persona ni SIIGO las hubiera
        # validado. El conocimiento se genera en un solo momento del ciclo de vida —cuando el
        # documento queda CONTABILIZADO en SIIGO— y lo publica `AccountingKnowledgePublisher`.

        # Disparar asignación de cuentas PUC en llm-service (best-effort).
        #
        # Solo cuando ya existe historial confirmado por el contador. En la primera descarga
        # desde la DIAN el tenant no tiene ninguna cuenta validada por una persona, así que
        # el modelo no tendría precedente sobre el que apoyarse: el documento se deja tal
        # como llegó y el contador decide, ya sea asignando a mano o pidiendo la sugerencia
        # de forma explícita desde la interfaz.
        if self.llm_client:
            if self.document_repo.has_confirmed_accounting_history():
                await self.llm_client.trigger_code_assignment(created.id)
                # RF-08: la IA determina las retenciones del tercero durante el procesamiento
                # del documento, no solo cuando el contador pulsa el botón. Va después de la
                # asignación de cuentas porque ambas comparten el prerrequisito del PUC: si
                # falta, la primera ya habrá dejado el aviso en el log.
                await self.llm_client.trigger_retention_suggestion(created.id)
            else:
                logger.info(
                    "Documento %d guardado sin asignación automática: el tenant aún no "
                    "tiene contabilizaciones confirmadas por el usuario.",
                    created.id,
                )

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
        """Devuelve (xml_content, filename, xml_bytes, pdf_bytes).

        `pdf_bytes` solo puede venir del ZIP (una carga `.xml` directa nunca trae PDF); es
        None en ese caso y también cuando el ZIP no incluye uno válido.
        """
        file_extension = file.filename.split(".")[-1].lower()
        if file_extension not in ALLOWED_EXTENSIONS:
            raise FileProcessingException(
                "File format not allowed. Only ZIP and XML files are accepted."
            )
        if file_extension == "zip":
            return await extract_zip_file(file)
        xml_bytes = await file.read()
        return xml_bytes.decode("utf-8"), file.filename, xml_bytes, None

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

    def _build_document(
        self, xml_data: dict, filename: str, payment_type_id: Optional[int] = None
    ) -> Document:
        emisor = xml_data.get("emisor", {})
        receptor = xml_data.get("receptor", {})
        totales = xml_data.get("totales", {})

        tipo = xml_data.get("tipo_documento", {})
        doc_type = (
            (tipo.get("nombre") or tipo.get("codigo") or "")
            if isinstance(tipo, dict)
            else str(tipo or "")
        )

        # Las retenciones que el PROVEEDOR declara en el XML.
        #
        # No son la fuente de verdad —quien decide qué se retiene es el perfil fiscal del
        # comprador y la ficha del tercero en SIIGO— sino la única señal independiente para
        # contrastar lo que Abacus determina. Importa porque SIIGO no informa qué retenciones
        # practicó: `PurchasesOut` no trae ningún campo `retentions`.
        #
        # Antes se sumaban solo los esquemas 06 y 07 en dos columnas que nadie leía, y el 08
        # (ReteIVA) se descartaba entero.
        retenciones_xml = extraer_retenciones_del_xml(xml_data.get("retenciones", []))
        retefuente = total_retenido([r for r in retenciones_xml if r["tipo"] == "retefuente"])
        reteica = total_retenido([r for r in retenciones_xml if r["tipo"] == "reteica"])

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
            # Responsabilidades del comprador (RUT), para decidir si es agente de retención.
            receiver_responsibilities=receptor.get("regimen"),
            subtotal=float(totales.get("subtotal") or 0),
            total_taxes=float(totales.get("total_impuestos") or 0),
            total=float(totales.get("total") or 0),
            retefuente=retefuente,
            reteica=reteica,
            # La lista completa, incluido el ReteIVA que antes se perdía, para poder
            # contrastarla después contra lo que el sistema determine.
            xml_withholdings=retenciones_xml or None,
            status=DocumentStatus.PROCESADO,
            payment_type_id=payment_type_id,
        )

    def _build_details(
        self, document: Document, xml_data: dict, taxes: Optional[list] = None
    ) -> None:
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

            # TODOS los impuestos de la línea, no solo el primero.
            #
            # Antes se tomaba `(item["impuestos"] or [{}])[0]` y el resto se descartaba sin
            # rastro. Sobre los 45 XML reales del cliente eso perdía $7.363,44 repartidos en
            # 19 documentos: ocho facturas de telecomunicaciones declaran IVA 19 % e impuesto
            # al consumo 4 % en el MISMO renglón. La línea de ajuste que cuadraba el total
            # después hacía la pérdida invisible.
            impuestos = extraer_impuestos_de_linea(item.get("impuestos"))

            # Cada impuesto se enlaza con el catálogo por separado: en una línea con IVA e
            # INC, cada uno tiene su propio id en SIIGO.
            for impuesto in impuestos:
                impuesto["tax_id"] = self._match_tax(
                    str(impuesto.get("porcentaje") or 0), taxes or []
                )

            # `tax_type`/`tax_value` conservan el impuesto PRINCIPAL —el de mayor importe—
            # porque los leen la interfaz y el RAG, y porque es el que describe la línea.
            principal = impuesto_principal(impuestos)
            tax_type_str = str(principal["porcentaje"]) if principal else "0"
            tax_value = principal["valor"] if principal else 0.0
            tax_id = principal.get("tax_id") if principal else None

            cost_center_id = self.document_repo.find_most_frequent_cost_center(
                issuer_nit, description
            )

            subtotal = float(item.get("valor_total") or 0)
            document.details.append(
                DocumentDetail(
                    description=description,
                    concept_description_id=concept_description_id,
                    quantity=float(item.get("cantidad") or 0),
                    unit=item.get("unidad_medida") or "",
                    price=float(item.get("precio_unitario") or 0),
                    subtotal=subtotal,
                    tax_type=tax_type_str,
                    tax_value=tax_value,
                    taxes=impuestos or None,
                    # El total de la línea suma TODOS sus impuestos, no solo el principal:
                    # es la cifra que debe cuadrar contra el total de la factura.
                    total=subtotal + total_de_impuestos(impuestos),
                    tax_id=tax_id,
                    cost_center_id=cost_center_id,
                )
            )

    @staticmethod
    def _match_tax(tax_type_str: str, taxes: list[dict]) -> Optional[int]:
        """Id del catálogo que corresponde al impuesto de la línea, o None.

        Delega en `domain/services/tax_resolution.py`, que es la ÚNICA forma de responder
        esta pregunta en todo el servicio. Antes había aquí una segunda implementación que
        comparaba primero por nombre y no desempataba entre impuestos del mismo porcentaje,
        de modo que esta capa y la del envío podían resolver la misma línea de forma distinta
        —y lo hacían—.
        """
        return resolver_impuesto(tax_type_str, taxes, nombre=tax_type_str)

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
