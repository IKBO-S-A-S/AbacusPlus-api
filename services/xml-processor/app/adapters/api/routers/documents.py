import logging
import re
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from app.adapters.api.document_guards import require_editable as _require_editable
from app.application.dto.document import (
    AccountingAttemptResponse,
    AccountingBatchProgress,
    AccountingEnqueueItem,
    AccountingEnqueueResponse,
    AccountingRejectedItem,
    DocumentAccountingAuditResponse,
    DocumentAccountingBatchRequest,
    DocumentAccountingResponse,
    DocumentBulkStatusUpdateRequest,
    DocumentBulkStatusUpdateResponse,
    DocumentCostCenterUpdateRequest,
    DocumentDetailCodeUpdateItem,
    DocumentDetailCodeUpdateResponse,
    DocumentFieldChangeResponse,
    DocumentFileLinksBatchResponse,
    DocumentFileLinksResponse,
    DocumentPaymentTypeUpdateRequest,
    DocumentReconciliationRequest,
    DocumentReconciliationResponse,
    DocumentReconciliationView,
    DocumentResponse,
    DocumentStatusUpdateRequest,
    DocumentSummaryResponse,
    SiigoInvoiceMatch,
)
from app.application.services.accounting_queue import AccountingQueueService
from app.application.use_cases.account_document import AccountDocumentUseCase
from app.application.use_cases.approve_document import (
    ApproveDocumentUseCase,
    BulkApproveDocumentsUseCase,
    BulkCausarDocumentsUseCase,
    BulkUnapproveDocumentsUseCase,
    CausarDocumentUseCase,
    UnapproveDocumentUseCase,
)
from app.application.use_cases.get_document_detail import GetDocumentDetailUseCase
from app.application.use_cases.publish_document_files import PublishDocumentFilesUseCase
from app.application.use_cases.query_documents import (
    GetDocumentByIdUseCase,
    GetDocumentsByDateRangeUseCase,
)
from app.application.use_cases.reconcile_document import ReconcileDocumentUseCase
from app.dependencies import (
    get_account_document_use_case,
    get_accounting_audit_repo,
    get_accounting_queue_service,
    get_approve_document_use_case,
    get_bulk_approve_documents_use_case,
    get_bulk_causar_documents_use_case,
    get_bulk_unapprove_documents_use_case,
    get_causar_document_use_case,
    get_concept_repo,
    get_cost_center_repo,
    get_document_by_id_use_case,
    get_document_detail_use_case,
    get_document_repo,
    get_documents_by_date_range_use_case,
    get_product_repo,
    get_puc_repo,
    get_reconcile_document_use_case,
    get_unapprove_document_use_case,
)
from app.domain.exceptions.base import EntityNotFoundException
from app.domain.services.account_assignment import validate_assignments
from app.domain.value_objects.accounting_error import can_edit, can_retry
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.config.auth_dependency import TokenData, get_token_data, require_write
from app.infrastructure.persistence.repositories.accounting_job_repository import (
    AccountingAuditRepository,
)
from app.infrastructure.persistence.repositories.concept_repository import ConceptRepository
from app.infrastructure.persistence.repositories.cost_center_repository import CostCenterRepository
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.persistence.repositories.product_repository import ProductRepository
from app.infrastructure.persistence.repositories.puc_repository import PucRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _enrich_details(doc_response: DocumentResponse, concept_repo: ConceptRepository) -> None:
    ids = [d.concept_description_id for d in doc_response.details]
    account_map = concept_repo.get_accounts_by_description_ids(ids)
    for detail in doc_response.details:
        detail.concept_account_number = account_map.get(detail.concept_description_id)


@router.get(
    "/documents",
    response_model=list[DocumentSummaryResponse],
    summary="Listar documentos por rango de fechas",
    description=(
        "Retorna un resumen de los documentos (facturas electrónicas DIAN) procesados dentro del rango "
        "de fechas indicado. Opcionalmente se puede filtrar por estado del procesamiento.\n\n"
        "Las fechas corresponden al campo `date` del documento (fecha de emisión de la factura), "
        "no a la fecha de registro en el sistema.\n\n"
        "Para obtener el detalle completo con líneas XML y asiento contable, "
        "usar `GET /api/v1/documents/{id}/full`."
    ),
    response_description="Lista de documentos con datos de resumen (sin líneas de detalle).",
    responses={
        400: {"description": "Parámetros de fecha inválidos."},
    },
)
async def get_documents(
    from_date: date = Query(
        ..., description="Fecha de inicio del rango (inclusive). Formato: YYYY-MM-DD"
    ),
    to_date: date = Query(
        ..., description="Fecha de fin del rango (inclusive). Formato: YYYY-MM-DD"
    ),
    status: Optional[int] = Query(
        None,
        description="Filtrar por código de estado. 0=Error, 100=Procesado, 200=Causado, 300=Aprobado, 400=Contabilizada.",
    ),
    use_case: GetDocumentsByDateRangeUseCase = Depends(get_documents_by_date_range_use_case),
):
    documents = use_case.execute(from_date, to_date, status)
    return [DocumentSummaryResponse.model_validate(doc, from_attributes=True) for doc in documents]


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    summary="Obtener documento por ID",
    description=(
        "Retorna el detalle completo de un documento específico: datos del emisor, receptor, "
        "totales, impuestos y todas las líneas de detalle (conceptos facturados).\n\n"
        "Para obtener también el asiento contable de causación generado por el LLM, "
        "usar `GET /api/v1/documents/{id}/full`."
    ),
    response_description="Documento con todos sus campos y líneas de detalle.",
    responses={
        404: {"description": "Documento no encontrado."},
    },
)
async def get_document(
    document_id: int,
    use_case: GetDocumentByIdUseCase = Depends(get_document_by_id_use_case),
    concept_repo: ConceptRepository = Depends(get_concept_repo),
):
    doc = use_case.execute(document_id)
    response = DocumentResponse.model_validate(doc, from_attributes=True)
    _enrich_details(response, concept_repo)
    return response


@router.get(
    "/documents/{document_id}/full",
    response_model=DocumentResponse,
    summary="Detalle completo del documento con cuentas PUC asignadas",
    description=(
        "Retorna el documento con todas sus líneas de detalle enriquecidas:\n\n"
        "- `code`: cuenta PUC asignada por el LLM (null si aún no se procesó).\n"
        "- `type`: tipo de ítem contable (Account, Product, FixedAsset).\n"
        "- `tax_id`: referencia al impuesto en el catálogo local.\n"
        "- `cost_center_id`: centro de costo asignado por historial.\n\n"
        "Para disparar la asignación de cuentas usar `POST /api/v1/accounting/code-assignments/{id}`."
    ),
    response_description="Documento completo con líneas de detalle y cuentas PUC asignadas.",
    responses={
        404: {"description": "Documento no encontrado."},
    },
)
def get_document_detail(
    document_id: int,
    use_case: GetDocumentDetailUseCase = Depends(get_document_detail_use_case),
    concept_repo: ConceptRepository = Depends(get_concept_repo),
):
    try:
        doc = use_case.execute(document_id)
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    response = DocumentResponse.model_validate(doc, from_attributes=True)
    _enrich_details(response, concept_repo)
    return response


def _is_pdf(data: bytes) -> bool:
    """Valida la firma de un PDF (%PDF-) para no servir contenido corrupto/erróneo."""
    return bool(data) and bytes(data[:5]) == b"%PDF-"


def _safe_filename(document_number: str) -> str:
    """Sanea el número de documento para usarlo en Content-Disposition.

    Evita inyección de cabeceras / path traversal en el nombre de archivo
    permitiendo únicamente caracteres alfanuméricos, guion y guion bajo.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", document_number or "documento")
    return cleaned[:64] or "documento"


@router.get(
    "/documents/{document_id}/pdf",
    summary="Representación gráfica oficial de la DIAN (PDF)",
    description=(
        "Retorna la **representación gráfica oficial de la DIAN** (PDF) del documento.\n\n"
        "El PDF proviene del ZIP que descarga «Procesar Documentos» durante la sesión "
        "autenticada en la DIAN y queda almacenado en `documents.pdf_data` "
        "(`pdf_source = 'dian_official'`). Se sirve `inline` con "
        "`X-Content-Type-Options: nosniff`, que impide al navegador reinterpretar el "
        "contenido como HTML (defensa anti-XSS, igual que el endpoint de XML).\n\n"
        "Si el documento aún no tiene la representación gráfica oficial (por ejemplo, si el XML "
        "se cargó manualmente y nunca se descargó de la DIAN), se retorna `404`.\n\n"
        "El aislamiento por tenant es heredado de la sesión de base de datos derivada del JWT "
        "firmado (`get_tenant_db`), por lo que un `document_id` solo resuelve dentro de la base "
        "del propio tenant."
    ),
    response_description="Archivo PDF (`application/pdf`) con la representación gráfica oficial.",
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "PDF oficial de la DIAN servido correctamente.",
        },
        404: {
            "description": (
                "El documento no existe en el tenant actual, o no tiene almacenada la "
                "representación gráfica oficial de la DIAN."
            )
        },
    },
)
def get_document_pdf(
    document_id: int,
    use_case: GetDocumentDetailUseCase = Depends(get_document_detail_use_case),
):
    try:
        doc = use_case.execute(document_id)
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )

    official = getattr(doc, "pdf_data", None)
    if not official or not _is_pdf(official):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El documento no tiene representación gráfica oficial de la DIAN.",
        )

    filename = f"documento_{_safe_filename(doc.document_number)}.pdf"
    return Response(
        content=bytes(official),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            # `_is_pdf` solo mira los 5 primeros bytes, y existen archivos políglota que
            # empiezan por `%PDF-` y siguen con HTML/JS. Si el navegador husmeara el
            # contenido y lo tratara como HTML, ese script correría en NUESTRO origen: los
            # bytes vienen de los ZIP que sube el usuario, así que el contenido no es de
            # confianza. `nosniff` obliga a respetar el `application/pdf` declarado.
            # El endpoint de XML de más abajo ya lo hacía; aquí faltaba.
            "X-Content-Type-Options": "nosniff",
            "X-Pdf-Source": "dian_official",
        },
    )


@router.get(
    "/documents/{document_id}/xml",
    summary="XML oficial de la DIAN del documento",
    description=(
        "Retorna el **XML oficial de la DIAN** del documento (el que venía dentro del ZIP "
        "descargado en «Procesar Documentos»), almacenado en `documents.xml_data`.\n\n"
        "Se sirve como `application/xml` con `X-Content-Type-Options: nosniff` para impedir que "
        "el navegador reinterprete el contenido (defensa anti-XSS). Si el documento no tiene el "
        "XML almacenado, retorna `404`. El aislamiento por tenant es heredado del JWT firmado."
    ),
    response_description="Archivo XML (`application/xml`) del documento.",
    responses={
        200: {"content": {"application/xml": {}}, "description": "XML servido correctamente."},
        404: {"description": "Documento no encontrado o sin XML oficial almacenado."},
    },
)
def get_document_xml(
    document_id: int,
    use_case: GetDocumentDetailUseCase = Depends(get_document_detail_use_case),
):
    try:
        doc = use_case.execute(document_id)
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )

    xml = getattr(doc, "xml_data", None)
    if not xml:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El documento no tiene XML oficial de la DIAN almacenado.",
        )

    filename = f"documento_{_safe_filename(doc.document_number)}.xml"
    return Response(
        content=bytes(xml),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch(
    "/documents/{document_id}/approve",
    dependencies=[Depends(require_write)],
    response_model=DocumentSummaryResponse,
    summary="Aprobar documento causado",
    description=(
        "Cambia el estado de un documento de `Causado` a `Aprobado`.\n\n"
        "**Requisitos:**\n"
        "- El documento debe tener un asiento contable de causación generado (estado `Causado`).\n"
        "- Si el documento no tiene asiento contable o ya está en estado `Aprobado`, "
        "la operación es rechazada.\n\n"
        "Para revertir la aprobación usar `PATCH /api/v1/documents/{id}/unapprove`."
    ),
    response_description="Documento actualizado con estado 'Aprobado'.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "El documento no está en estado 'Causado' o ya está aprobado."},
    },
)
def approve_document(
    document_id: int,
    use_case: ApproveDocumentUseCase = Depends(get_approve_document_use_case),
):
    try:
        doc = use_case.execute(document_id)
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    # RF-08: aprobar NO alimenta el RAG.
    #
    # Antes sí lo hacía, y la razón por la que ya no es el punto entero del requisito: la
    # aprobación dice que el contador está conforme con la imputación, no que la imputación
    # sea contabilizable. Entre aprobar y contabilizar todavía puede fallar el tercero, el
    # comprobante, la cuenta o la forma de pago, y cada uno de esos fallos dejaba en el RAG
    # una causación que nunca llegó a ningún libro. El conocimiento se genera al confirmarse
    # el estado «Contabilizada», en `AccountingKnowledgePublisher`.
    return DocumentSummaryResponse.model_validate(doc, from_attributes=True)


@router.patch(
    "/documents/{document_id}/details",
    dependencies=[Depends(require_write)],
    response_model=DocumentDetailCodeUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Asignar cuentas PUC o productos a líneas de detalle",
    description=(
        "Actualiza los campos `code` y `type` en las líneas de detalle de un documento.\n\n"
        "- `type=Account` (por defecto si se envía `code` sin `type`): `code` es una cuenta "
        "PUC activa del catálogo.\n"
        "- `type=Product`: `code` es un producto activo del catálogo de productos "
        "(`/integrations/products`). Un documento puede mezclar líneas de ambos tipos.\n\n"
        "Es la vía de corrección manual de asignaciones; el llm-service usa la ruta interna.\n\n"
        "Solo se actualizan las líneas cuyos `detail_id` existan en `document_details`. "
        "Los IDs inexistentes se ignoran sin error."
    ),
    response_description="Cantidad de líneas actualizadas correctamente.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "El documento no admite edición en su estado actual."},
        422: {
            "description": (
                "Cuenta o producto inexistente, inactivo o no imputable; centro de costo "
                "inexistente; o `type` enviado sin `code`."
            )
        },
    },
)
async def update_detail_codes(
    document_id: int,
    assignments: list[DocumentDetailCodeUpdateItem],
    token: Annotated[TokenData, Depends(get_token_data)],
    doc_repo: DocumentRepository = Depends(get_document_repo),
    puc_repo: PucRepository = Depends(get_puc_repo),
    cost_center_repo: CostCenterRepository = Depends(get_cost_center_repo),
    product_repo: ProductRepository = Depends(get_product_repo),
    audit: AccountingAuditRepository = Depends(get_accounting_audit_repo),
):
    doc = doc_repo.get_by_id(document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    _require_editable(doc)

    # Las reglas viven en el dominio y son las mismas que aplica la ruta interna del
    # llm-service: una sola definición de qué es una imputación admisible. Aquí se acotan
    # las líneas al documento (defensa contra IDOR intra-tenant) y se valida la cuenta
    # contra el catálogo del tenant.
    #
    # `enforce_item_class` queda en False a propósito: el contador puede tener un caso
    # legítimo que las reglas generales de clase PUC no contemplan, y su criterio manda.
    # A la sugerencia del modelo sí se le exige, porque no tiene criterio que oponer.
    outcome = validate_assignments(
        assignments=assignments,
        own_detail_ids={d.id for d in doc.details},
        puc_index={
            p.code: {"is_active": p.is_active, "accepts_movements": p.accepts_movements}
            for p in puc_repo.get_active()
        },
        valid_cost_center_ids={c.id for c in cost_center_repo.get_active()},
        product_index=product_repo.index_by_code(),
    )

    # Un descarte por línea ajena se ignora en silencio —no debe revelar si ese id existe—,
    # pero un error de catálogo es del contador y tiene que verlo.
    for rejection in outcome.rejected:
        if "no pertenece a este documento" not in rejection.reason:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=rejection.reason
            )

    scoped = outcome.accepted

    # RF-04: esta ruta la consume la interfaz, así que toda cuenta que llegue por aquí es
    # una edición del contador y queda marcada como manual. El LLM usa la ruta interna.
    # `exclude_unset` conserva la diferencia entre «no envié el campo» (no tocar) y
    # «lo envié en null» (limpiar la asignación), que es lo que espera el repositorio.
    # RF-05: la foto de los valores ANTES de escribir. Se toma aquí y no después por un
    # motivo obvio en cuanto se piensa: después de escribir, el valor anterior ya no existe
    # en ninguna parte, y con él se pierde la mitad de la auditoría. Sin el «de 510505 a
    # 510506» el historial solo dice que alguien tocó algo.
    previos = {
        d.id: {
            "code": d.code,
            "type": d.type,
            "cost_center_id": d.cost_center_id,
            "tax_id": d.tax_id,
        }
        for d in doc.details
    }

    updated = doc_repo.update_detail_codes(
        [a.model_dump(exclude_unset=True) for a in scoped], code_source="manual"
    )

    _auditar_correcciones(
        audit=audit,
        document_id=document_id,
        assignments=scoped,
        previos=previos,
        changed_by=(token.email or token.user_id),
        # El motivo distingue una corrección de causación normal de la que se hace para
        # desatascar un documento que SIIGO rechazó, que es la que un auditor busca.
        reason=("error_correction" if doc.status == DocumentStatus.ERROR else "causacion_edit"),
    )
    return DocumentDetailCodeUpdateResponse(updated=updated)


def _auditar_correcciones(
    *, audit, document_id: int, assignments, previos: dict, changed_by, reason: str
) -> None:
    """Registra campo a campo lo que el contador cambió en las líneas del documento.

    Solo escribe los campos que **de verdad cambiaron**. Registrar también los que se
    enviaron con el mismo valor llenaría el historial de ruido y haría más difícil encontrar
    el cambio que importa, que es justo lo contrario de para qué existe el historial.
    """
    for asignacion in assignments:
        enviados = asignacion.model_dump(exclude_unset=True)
        detail_id = enviados.pop("detail_id", None) or getattr(asignacion, "detail_id", None)
        anterior = previos.get(detail_id, {})
        for campo, nuevo in enviados.items():
            if campo not in anterior:
                continue
            if anterior[campo] == nuevo:
                continue
            audit.record_field_change(
                document_id=document_id,
                entity="document_detail",
                entity_id=detail_id,
                field=campo,
                old_value=anterior[campo],
                new_value=nuevo,
                changed_by=changed_by,
                reason=reason,
            )


@router.patch(
    "/documents/{document_id}/payment-type",
    dependencies=[Depends(require_write)],
    response_model=DocumentSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Actualizar medio de pago de un documento",
    description="Asigna o cambia el `payment_type_id` de un documento.",
    response_description="Documento actualizado.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "El documento está aprobado o contabilizado y no admite cambios."},
    },
)
def update_payment_type(
    document_id: int,
    request: DocumentPaymentTypeUpdateRequest,
    doc_repo: DocumentRepository = Depends(get_document_repo),
):
    current = doc_repo.get_by_id(document_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    _require_editable(current)

    doc = doc_repo.update_payment_type(document_id, request.payment_type_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    return DocumentSummaryResponse.model_validate(doc, from_attributes=True)


@router.patch(
    "/documents/{document_id}/cost-center",
    dependencies=[Depends(require_write)],
    response_model=DocumentSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Asignar el centro de costo del documento",
    description=(
        "RF-07: fija el centro de costo **a nivel de documento**, que es el que se envía a "
        "SIIGO al contabilizar.\n\n"
        "La API de factura de compra de SIIGO (`POST /v1/purchases`) solo admite un "
        "`cost_center` general en la raíz del documento; sus líneas (`items[]`) no tienen ese "
        "campo. Por eso el centro de costo que gobierna la contabilización vive en el "
        "documento y no en cada detalle.\n\n"
        "Enviar `cost_center_id: null` deja el documento sin centro de costo (es opcional)."
    ),
    response_description="Documento actualizado con su centro de costo.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "El documento está aprobado o contabilizado y no admite cambios."},
        422: {"description": "El centro de costo no existe en el catálogo del tenant."},
    },
)
def update_document_cost_center(
    document_id: int,
    request: DocumentCostCenterUpdateRequest,
    doc_repo: DocumentRepository = Depends(get_document_repo),
    cost_center_repo: CostCenterRepository = Depends(get_cost_center_repo),
):
    current = doc_repo.get_by_id(document_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    _require_editable(current)

    # El backend es la autoridad: nunca confía en que el cliente haya elegido un centro
    # válido. Se consulta el centro por id (no todo el catálogo) y se distingue «no existe»
    # de «existe pero está inactivo», que son errores distintos para quien contabiliza.
    # `null` siempre es válido: deja el documento sin centro de costo (es opcional).
    if request.cost_center_id is not None:
        centro = cost_center_repo.get_by_id(request.cost_center_id)
        if centro is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"El centro de costo '{request.cost_center_id}' no existe en el catálogo.",
            )
        if not centro.is_active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"El centro de costo '{centro.code} — {centro.name}' está inactivo y no "
                    "puede asignarse. Sincronice el catálogo o elija otro."
                ),
            )

    doc = doc_repo.update_cost_center(document_id, request.cost_center_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    return DocumentSummaryResponse.model_validate(doc, from_attributes=True)


@router.patch(
    "/documents",
    dependencies=[Depends(require_write)],
    response_model=DocumentBulkStatusUpdateResponse,
    summary="Actualizar el estado de varios documentos en una sola operación",
    description=(
        "Mueve una selección completa de documentos al estado indicado, en **una sola "
        "sentencia atómica** por lote.\n\n"
        "**Cuándo usarlo.** Es la vía para las acciones masivas de la pantalla de documentos "
        "(«Calcular contabilización», «Aprobar seleccionados», «Cancelar aprobación»). "
        "Recorrer la selección llamando a `PATCH /documents/{id}` una vez por documento "
        "implica una petición HTTP y una transacción por cada uno, y es lo que hacía que "
        "pasar un mes de facturación de `Procesado` a `Causado` tardara minutos.\n\n"
        "**Estados destino soportados:**\n"
        "- `200` (Causado) — avanza los documentos que estén en `Procesado`.\n"
        "- `300` (Aprobado) — avanza los documentos que estén en `Causado`.\n"
        "- `201` — retrocede a `Causado` los que estén en `Aprobado` (cancelar aprobación). "
        "Es un código de la API, no un estado almacenado: el documento queda en `200`.\n\n"
        "**Ningún documento inválido interrumpe el lote.** La guarda de estado se aplica "
        "dentro del UPDATE, no en una lectura previa, así que un documento que otro usuario "
        "movió entre medias no se degrada en silencio: simplemente no se toca y aparece en "
        "`rejected`. Los que ya estaban en el estado destino se reportan en `unchanged`: la "
        "operación es idempotente y puede repetirse sin efectos."
    ),
    response_description=(
        "Clasificación de cada id pedido en actualizado, sin cambios, rechazado o inexistente."
    ),
    responses={
        422: {"description": "Código de estado no soportado o lista de documentos inválida."},
    },
)
async def update_documents_status(
    request: DocumentBulkStatusUpdateRequest,
    causar_use_case: BulkCausarDocumentsUseCase = Depends(get_bulk_causar_documents_use_case),
    approve_use_case: BulkApproveDocumentsUseCase = Depends(get_bulk_approve_documents_use_case),
    unapprove_use_case: BulkUnapproveDocumentsUseCase = Depends(
        get_bulk_unapprove_documents_use_case
    ),
):
    # El destino no basta para elegir la operación: a Causado (200) se llega avanzando desde
    # Procesado y retrocediendo desde Aprobado, y en un lote heterogéneo no se puede deducir
    # cuál quiso el usuario mirando cada documento —eso convertiría «causar el lote» en una
    # cancelación masiva de aprobaciones—. Por eso cancelar tiene su propio código de entrada.
    casos = {
        200: causar_use_case,
        300: approve_use_case,
        201: unapprove_use_case,
    }
    use_case = casos.get(request.status)
    if use_case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Status {request.status} not supported. Supported values: "
                "200 (Causado), 300 (Aprobado), 201 (cancelar aprobación → Causado)."
            ),
        )

    resultado = use_case.execute(request.document_ids)
    return DocumentBulkStatusUpdateResponse(
        requested=sum(len(v) for v in resultado.values()),
        **resultado,
    )


@router.patch(
    "/documents/{document_id}",
    dependencies=[Depends(require_write)],
    response_model=DocumentSummaryResponse,
    summary="Actualizar estado de un documento",
    description=(
        "Cambia el estado de un documento al valor indicado en `status`.\n\n"
        "**Estados destino soportados:**\n"
        "- `200` (Causado) — se llega por dos caminos, según el estado actual:\n"
        "  - desde `Procesado` (100): **avanza**, una vez calculada la contabilización;\n"
        "  - desde `Aprobado` (300): **retrocede**, cancelando la aprobación.\n\n"
        "  Si el documento ya está en `Causado` la operación es idempotente y responde `200`.\n\n"
        "Para aprobar un documento causado usar `PATCH /api/v1/documents/{id}/approve`."
    ),
    response_description="Documento actualizado con el nuevo estado.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "Transición de estado inválida para el estado actual del documento."},
        422: {"description": "Código de estado no soportado."},
    },
)
async def update_document_status(
    document_id: int,
    request: DocumentStatusUpdateRequest,
    unapprove_use_case: UnapproveDocumentUseCase = Depends(get_unapprove_document_use_case),
    causar_use_case: CausarDocumentUseCase = Depends(get_causar_document_use_case),
    doc_repo: DocumentRepository = Depends(get_document_repo),
    concept_repo: ConceptRepository = Depends(get_concept_repo),
):
    if request.status != 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status {request.status} not supported. Supported values: 200 (Causado).",
        )

    # A Causado se llega por dos caminos opuestos y el destino no basta para distinguirlos:
    # desde Procesado es AVANZAR (se calculó la contabilización) y desde Aprobado es
    # RETROCEDER (se cancela la aprobación). El estado actual del documento decide cuál de
    # los dos casos de uso aplica; cada uno mantiene su propia validación.
    # Basta el estado: cargar la entidad entera aquí hacía que el documento se leyera una vez
    # aquí y otra dentro del caso de uso, y cada lectura arrastraba el PDF y el XML de la fila.
    estado_actual = doc_repo.get_status(document_id)
    if estado_actual is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )

    use_case = unapprove_use_case if estado_actual == DocumentStatus.APROBADO else causar_use_case
    try:
        updated = use_case.execute(document_id)
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return DocumentSummaryResponse.model_validate(updated, from_attributes=True)


@router.post(
    "/documents/{document_id}/file-links",
    dependencies=[Depends(require_write)],
    response_model=DocumentFileLinksResponse,
    status_code=status.HTTP_200_OK,
    summary="Publicar el PDF y el XML del documento en Amazon S3",
    description=(
        "RF-03: sube a Amazon S3 los archivos ya almacenados del documento, consumiendo la "
        "API de subida existente, y guarda los enlaces que retorna.\n\n"
        "**Cuándo usarlo:** los documentos que llegan por la descarga de la DIAN se publican "
        "automáticamente al procesarse. Esta ruta sirve para **reintentar** un documento cuya "
        "subida falló (por ejemplo, por una caída momentánea de la API) y para publicar los "
        "documentos anteriores a la integración.\n\n"
        "**Idempotente por defecto:** un archivo que ya tiene enlace no se vuelve a subir. "
        "Enviar `overwrite=true` para rehacer un enlace roto o vencido; ten en cuenta que la "
        "API de subida añade una marca de tiempo al nombre, así que se crea un objeto nuevo "
        "en lugar de reemplazar el anterior.\n\n"
        "**Best-effort por archivo:** que falle el XML no impide guardar el enlace del PDF."
    ),
    response_description="Enlaces del documento y detalle de lo publicado, omitido o fallido.",
    responses={
        404: {"description": "Documento no encontrado."},
    },
)
async def publish_document_files(
    document_id: int,
    overwrite: bool = Query(
        False,
        description=(
            "Republica los archivos que ya tienen enlace. Úsese para reparar enlaces rotos "
            "o vencidos; por defecto se conservan los existentes."
        ),
    ),
    token: TokenData = Depends(get_token_data),
    repo: DocumentRepository = Depends(get_document_repo),
):
    use_case = PublishDocumentFilesUseCase(repo)
    try:
        result = await use_case.execute(
            document_id, tenant_slug=token.tenant_slug, overwrite=overwrite
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DocumentFileLinksResponse(**result)


@router.post(
    "/documents/file-links",
    dependencies=[Depends(require_write)],
    response_model=DocumentFileLinksBatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Publicar en lote los documentos que aún no tienen enlace en S3",
    description=(
        "RF-03: recorre los documentos del rango indicado y publica en Amazon S3 los archivos "
        "que aún no tienen enlace.\n\n"
        "Existe para poner al día los documentos procesados **antes** de que la integración "
        "con S3 estuviera operativa: sus archivos están almacenados en la base, pero nunca se "
        "subieron. Se procesan uno a uno para que una falla puntual no aborte el lote.\n\n"
        "**Idempotente:** los documentos que ya tienen enlace se omiten, así que puede "
        "ejecutarse varias veces sin duplicar objetos en el bucket. `limit` acota el tamaño "
        "del lote para poder validar con pocos documentos antes de procesarlos todos."
    ),
    response_description="Conteo de documentos publicados y fallidos, con el detalle de cada uno.",
)
async def publish_documents_file_links(
    from_date: date = Query(..., description="Fecha inicial del rango (inclusive)."),
    to_date: date = Query(..., description="Fecha final del rango (inclusive)."),
    limit: int = Query(
        50, ge=1, le=500, description="Máximo de documentos a procesar en esta ejecución."
    ),
    overwrite: bool = Query(False, description="Republica también los que ya tienen enlace."),
    token: TokenData = Depends(get_token_data),
    repo: DocumentRepository = Depends(get_document_repo),
):
    use_case = PublishDocumentFilesUseCase(repo)
    # El recorte va en la consulta, no en Python: `[:limit]` traía a memoria todos los
    # documentos del rango para quedarse con los primeros 50.
    documentos = repo.get_by_date_range(from_date, to_date, limit=limit)

    resultados: list[DocumentFileLinksResponse] = []
    publicados = 0
    fallidos = 0
    for doc in documentos:
        resultado = await use_case.execute(
            doc.id, tenant_slug=token.tenant_slug, overwrite=overwrite
        )
        if resultado["uploaded"]:
            publicados += 1
        elif resultado["warnings"]:
            fallidos += 1
        resultados.append(DocumentFileLinksResponse(**resultado))

    return DocumentFileLinksBatchResponse(
        processed=len(documentos),
        published=publicados,
        failed=fallidos,
        results=resultados,
    )


@router.post(
    "/documents/{document_id}/accounting-entries",
    dependencies=[Depends(require_write)],
    response_model=DocumentAccountingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enviar a contabilizar en SIIGO (un documento)",
    description=(
        "RF-05. Contabiliza **un** documento de forma síncrona, esperando la respuesta de "
        "SIIGO. Para varios documentos use el envío por lotes, que encola y responde de "
        "inmediato.\n\n"
        "**Flujo:**\n"
        "1. Toma el documento en exclusiva poniéndole el cerrojo de contabilización.\n"
        "2. Valida la información obligatoria y construye el JSON con datos del documento.\n"
        "3. Llama a SIIGO y exige que la respuesta traiga el identificador.\n"
        "4. Clasifica el desenlace, lo registra en el historial y actualiza el documento.\n\n"
        "**El estado solo cambia a `Contabilizada` tras validar la respuesta de SIIGO**, "
        "nunca por el hecho de haber enviado la petición.\n\n"
        "**Los cinco estados se conservan.** Cualquier fallo deja el documento en `Error` "
        "(0), con el motivo en `error`. Lo que cambia entre un fallo y otro no es el estado "
        "sino lo que se puede hacer, y eso llega en dos booleanos:\n"
        "- `can_edit` — corregir la causación desatasca el documento (SIIGO rechazó un dato "
        "contable).\n"
        "- `can_retry` — puede volver a enviarse a la cola.\n\n"
        "Cuando los dos son `false` el desenlace no consta y hay que verificar en SIIGO si "
        "la factura existe antes de tocar el documento.\n\n"
        "**Prevención de duplicados:** SIIGO no admite `Idempotency-Key` en facturas de "
        "compra. Si el desenlace no se puede confirmar (timeout, 5xx o respuesta sin id), el "
        "documento queda con el cerrojo puesto y `needs_reconciliation: true`, y ninguna vía "
        "—ni la cola, ni este endpoint— vuelve a enviarlo hasta que se reconcilie. Un "
        "segundo envío simultáneo del mismo documento recibe 409.\n\n"
        "**`force=true`** salta el cerrojo. **Úselo solo tras verificar en SIIGO que la "
        "factura no se creó**; el camino previsto para eso es la reconciliación, que hace "
        "esa comprobación por usted."
    ),
    response_description="Resultado de la contabilización, con el id de SIIGO si tuvo éxito.",
    responses={
        404: {"description": "Documento no encontrado."},
        409: {"description": "El documento ya está contabilizado, o tiene el cerrojo puesto."},
        422: {"description": "SIIGO rechazó el documento o falta información obligatoria."},
    },
)
def create_document_accounting_entry(
    document_id: int,
    token: Annotated[TokenData, Depends(get_token_data)],
    force: bool = Query(
        False,
        description=(
            "Salta el cerrojo de contabilización. Solo debe usarse tras verificar en SIIGO "
            "que la factura no se creó, porque con el cerrojo puesto un reenvío puede "
            "duplicar un asiento contable real."
        ),
    ),
    use_case: AccountDocumentUseCase = Depends(get_account_document_use_case),
) -> DocumentAccountingResponse:
    try:
        outcome = use_case.execute(
            document_id, force=force, triggered_by=(token.email or token.user_id), attempt=1
        )
    except EntityNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )

    if not outcome.ok:
        # Se responde con el cuerpo completo y no solo con un detalle de texto: el frontend
        # necesita `can_edit` y `can_retry` para decidir qué acciones habilita.
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if (outcome.status == DocumentStatus.CONTABILIZADA or outcome.needs_reconciliation)
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=_accounting_response(outcome).model_dump(),
        )

    return _accounting_response(outcome)


def _accounting_response(outcome) -> DocumentAccountingResponse:
    """Traduce el resultado del caso de uso al contrato de la API.

    Se extrae a una función porque el mismo resultado se serializa en dos sitios —el cuerpo
    de éxito y el detalle del error—, y tenerlo escrito dos veces era la vía segura de que un
    campo nuevo apareciera solo en uno de los dos.
    """
    return DocumentAccountingResponse(
        document_id=outcome.document_id,
        ok=outcome.ok,
        status=outcome.status,
        siigo_id=outcome.siigo_id,
        siigo_name=outcome.siigo_name,
        error=outcome.error,
        error_class=outcome.error_class,
        can_edit=can_edit(outcome.recommended_action or ""),
        can_retry=can_retry(outcome.recommended_action or ""),
        error_code=outcome.error_code,
        needs_reconciliation=outcome.needs_reconciliation,
        auto_retryable=outcome.auto_retryable,
    )


@router.post(
    "/documents/accounting-entries",
    dependencies=[Depends(require_write)],
    response_model=AccountingEnqueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enviar a contabilizar en SIIGO por lotes (encola)",
    description=(
        "RF-05. **Encola** los documentos y responde de inmediato con un `batch_id`. El "
        "envío a SIIGO lo hacen los workers en segundo plano; el progreso se consulta con "
        "`GET /documents/accounting-batches/{batch_id}`.\n\n"
        "**Por qué encola en lugar de contabilizar aquí mismo:** la versión anterior "
        "recorría el lote dentro de la propia petición, que quedaba abierta minutos. Si el "
        "proceso moría a mitad, los documentos ya enviados no dejaban registro de haberse "
        "enviado, y averiguar cuáles llegaron a SIIGO obligaba a revisarlos uno a uno. Con "
        "la cola persistida el trabajo sobrevive al reinicio con su cerrojo y su historial "
        "intactos.\n\n"
        "**Concurrencia:** la fija `ACCOUNTING_MAX_CONCURRENCY` y arranca en 1 (secuencial). "
        "SIIGO documenta un límite de peticiones por minuto —100 en producción, 10 en "
        "empresas de prueba, configurable en `ACCOUNTING_RATE_LIMIT_PER_MINUTE`— pero **no** "
        "documenta ningún límite de concurrencia, así que subirla debe apoyarse en pruebas "
        "contra el ambiente real.\n\n"
        "**Un documento rechazado no invalida el lote.** Los que no pueden encolarse "
        "—ya contabilizados, con el cerrojo puesto, o ya en cola— se devuelven en `rejected` "
        "con su motivo, y el resto sigue su curso.\n\n"
        "Responde `202 Accepted`: la petición se aceptó, el trabajo aún no ha ocurrido."
    ),
    response_description="Acuse del lote, con lo aceptado y lo rechazado.",
    responses={
        422: {"description": "El lote supera el máximo permitido o la selección es inválida."},
    },
)
def create_documents_accounting_entries(
    request: DocumentAccountingBatchRequest,
    token: Annotated[TokenData, Depends(get_token_data)],
    queue: AccountingQueueService = Depends(get_accounting_queue_service),
) -> AccountingEnqueueResponse:
    try:
        resultado = queue.enqueue(request.document_ids, enqueued_by=(token.email or token.user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return AccountingEnqueueResponse(
        batch_id=resultado.batch_id,
        total=resultado.total,
        enqueued=[AccountingEnqueueItem(**item) for item in resultado.enqueued],
        rejected=[AccountingRejectedItem(**item) for item in resultado.rejected],
    )


@router.get(
    "/documents/accounting-batches/{batch_id}",
    dependencies=[Depends(require_write)],
    response_model=AccountingBatchProgress,
    summary="Progreso de un lote de contabilización",
    description=(
        "RF-05. Devuelve el recuento de un lote encolado, para la barra de progreso.\n\n"
        "Los contadores se calculan sobre las filas del lote y no sobre un acumulador: un "
        "acumulador se desincroniza si un proceso muere a mitad, y entonces la barra de "
        "progreso miente sin que nada lo delate.\n\n"
        "`needs_reconciliation` se cuenta **aparte** de `failed` a propósito: un fallido es "
        "un documento que alguien puede corregir y reenviar; uno pendiente de reconciliar es "
        "un documento del que no se sabe si ya está en SIIGO, y ésos hay que vigilarlos como "
        "grupo separado."
    ),
    response_description="Recuento por estado de los trabajos del lote.",
)
def get_accounting_batch_progress(
    batch_id: str,
    queue: AccountingQueueService = Depends(get_accounting_queue_service),
) -> AccountingBatchProgress:
    return AccountingBatchProgress(**queue.progress(batch_id))


@router.get(
    "/documents/{document_id}/accounting-attempts",
    dependencies=[Depends(require_write)],
    response_model=DocumentAccountingAuditResponse,
    summary="Historial de contabilización de un documento",
    description=(
        "RF-05. Devuelve la auditoría completa: cada intento contra SIIGO —con el cuerpo "
        "enviado, la respuesta recibida, el código HTTP, el error y su clasificación— y "
        "cada corrección manual sobre la causación, con quién la hizo y desde qué valor.\n\n"
        "Ninguno de esos registros se modifica ni se borra jamás. Es lo que permite "
        "responder, meses después, qué se le envió exactamente a SIIGO y por qué el "
        "documento acabó como acabó."
    ),
    response_description="Intentos y correcciones, del más reciente al más antiguo.",
    responses={404: {"description": "Documento no encontrado."}},
)
def get_document_accounting_attempts(
    document_id: int,
    repo: DocumentRepository = Depends(get_document_repo),
    audit: AccountingAuditRepository = Depends(get_accounting_audit_repo),
) -> DocumentAccountingAuditResponse:
    if repo.get_by_id(document_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found"
        )
    return DocumentAccountingAuditResponse(
        document_id=document_id,
        attempts=[
            AccountingAttemptResponse.model_validate(a) for a in audit.attempts_for(document_id)
        ],
        changes=[
            DocumentFieldChangeResponse.model_validate(c) for c in audit.changes_for(document_id)
        ],
    )


# ── RF-06: reconciliación de un documento bloqueado ────────────────────────────


@router.get(
    "/documents/{document_id}/siigo-invoices",
    dependencies=[Depends(require_write)],
    response_model=DocumentReconciliationView,
    summary="Consultar en SIIGO si la factura del documento ya existe",
    description=(
        "**RF-06.** Pregunta a SIIGO si la factura de compra de este documento llegó a "
        "crearse. Es el primer paso para desbloquear un documento atascado en "
        "`Contabilizando`.\n\n"
        "**Por qué existe:** ese estado no es un indicador de progreso sino un cerrojo. Se "
        "llega a él cuando la contabilización termina sin respuesta legible —timeout, corte "
        "de red, error interno de SIIGO— y por tanto no consta si la factura se creó. Como "
        "`/v1/purchases` **no admite `Idempotency-Key`**, reenviar el documento podría "
        "generar un segundo asiento real. La única salida segura es preguntar.\n\n"
        "**No modifica nada.** Devuelve lo que SIIGO tiene y una acción sugerida; el cambio "
        "de estado solo ocurre si el contador lo confirma con "
        "`POST /documents/{id}/accounting-entries/reconciliations`.\n\n"
        "**Cómo leer la respuesta:**\n"
        "- `consulted: false` — no se pudo averiguar. **No reenvíe el documento.**\n"
        "- `consulted: true` con `matches` — la factura existe; ciérrelo con ese `siigo_id`.\n"
        "- `consulted: true` sin `matches` — SIIGO no creó nada; puede liberarse y reenviarse."
    ),
    response_description="Lo que SIIGO tiene sobre el documento, con la acción sugerida.",
    responses={
        403: {"description": "El usuario no tiene permiso de escritura."},
        404: {"description": "El documento no existe."},
    },
)
def get_document_siigo_invoices(
    document_id: int,
    use_case: ReconcileDocumentUseCase = Depends(get_reconcile_document_use_case),
) -> DocumentReconciliationView:
    try:
        vista = use_case.lookup(document_id)
    except EntityNotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DocumentReconciliationView(
        document_id=vista.document_id,
        status=vista.status,
        consulted=vista.consulted,
        matches=[SiigoInvoiceMatch(**m) for m in vista.matches],
        suggested_action=vista.suggested_action,
        message=vista.message,
        error=vista.error,
    )


@router.post(
    "/documents/{document_id}/accounting-entries/reconciliations",
    dependencies=[Depends(require_write)],
    response_model=DocumentReconciliationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Resolver un documento bloqueado en «Contabilizando»",
    description=(
        "**RF-06.** Aplica la resolución que el contador confirmó tras consultar "
        "`GET /documents/{id}/siigo-invoices`.\n\n"
        "**Con `siigo_id`:** el documento pasa a `Contabilizada` con ese identificador, **sin "
        "volver a llamar a SIIGO**. Es el caso en que la factura sí se creó y lo único que "
        "faltaba era registrarlo de este lado.\n\n"
        "**Sin `siigo_id`:** el documento vuelve a `Error`, desde donde puede contabilizarse "
        "de nuevo. Solo debe usarse cuando la consulta confirmó que SIIGO no tiene la "
        "factura: es el punto en que el criterio humano asume el riesgo que el sistema no "
        "puede asumir por su cuenta.\n\n"
        "**Requisito de estado:** solo opera sobre documentos en `Contabilizando`. Sobre "
        "cualquier otro responde 409, porque reconciliar un documento ya resuelto solo puede "
        "deshacer trabajo correcto."
    ),
    response_description="Estado en que quedó el documento tras la reconciliación.",
    responses={
        403: {"description": "El usuario no tiene permiso de escritura."},
        404: {"description": "El documento no existe."},
        409: {"description": "El documento no está bloqueado en «Contabilizando»."},
    },
)
def create_document_reconciliation(
    document_id: int,
    request: DocumentReconciliationRequest,
    use_case: ReconcileDocumentUseCase = Depends(get_reconcile_document_use_case),
) -> DocumentReconciliationResponse:
    try:
        resultado = use_case.resolve(
            document_id, request.siigo_id, request.siigo_name, request.siigo_total
        )
    except EntityNotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return DocumentReconciliationResponse(
        document_id=resultado.document_id,
        status=resultado.status,
        siigo_id=resultado.siigo_id,
        siigo_name=resultado.siigo_name,
        message=resultado.message,
    )
