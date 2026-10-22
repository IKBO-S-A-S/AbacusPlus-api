import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends
from sqlalchemy.orm import Session

from app.application.services.accounting_knowledge import AccountingKnowledgePublisher
from app.application.services.accounting_queue import AccountingQueueService
from app.application.use_cases.account_document import (
    AccountDocumentUseCase,
    build_siigo_service_client,
)
from app.application.use_cases.approve_document import (
    ApproveDocumentUseCase,
    BulkApproveDocumentsUseCase,
    BulkCausarDocumentsUseCase,
    BulkUnapproveDocumentsUseCase,
    CausarDocumentUseCase,
    UnapproveDocumentUseCase,
)
from app.application.use_cases.dashboard import DashboardUseCase
from app.application.use_cases.get_document_detail import GetDocumentDetailUseCase
from app.application.use_cases.import_retention_rates import ImportRetentionRatesUseCase
from app.application.use_cases.process_downloads import ProcessDownloadsUseCase
from app.application.use_cases.process_single_file import ProcessSingleFileUseCase
from app.application.use_cases.process_xml import ProcessXmlUseCase
from app.application.use_cases.query_documents import (
    GetDocumentByIdUseCase,
    GetDocumentsByDateRangeUseCase,
)
from app.application.use_cases.query_issuers import (
    GetIssuerByNitUseCase,
    ListIssuersUseCase,
    UpdateIssuerUseCase,
)
from app.application.use_cases.query_receivers import GetAllReceiversUseCase
from app.application.use_cases.reconcile_document import ReconcileDocumentUseCase
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.clients.llm_client import LlmClient
from app.infrastructure.clients.rag_client import RagClient
from app.infrastructure.clients.rag_knowledge_client import build_rag_knowledge_client
from app.infrastructure.config.auth_dependency import TokenData, get_tenant_db, get_token_data
from app.infrastructure.config.dashboard_settings import get_timezone, load_targets, today_local
from app.infrastructure.persistence.repositories.accounting_job_repository import (
    AccountingAuditRepository,
    AccountingJobRepository,
)
from app.infrastructure.persistence.repositories.concept_repository import ConceptRepository
from app.infrastructure.persistence.repositories.cost_center_repository import CostCenterRepository
from app.infrastructure.persistence.repositories.dashboard_repository import DashboardRepository
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.persistence.repositories.document_tax_repository import (
    DocumentTaxRepository,
)
from app.infrastructure.persistence.repositories.integration_retention_repository import (
    IntegrationRetentionRepository,
)
from app.infrastructure.persistence.repositories.integration_tax_repository import (
    IntegrationTaxRepository,
)
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository
from app.infrastructure.persistence.repositories.processing_log_repository import (
    ProcessingLogRepository,
)
from app.infrastructure.persistence.repositories.puc_repository import PucRepository
from app.infrastructure.persistence.repositories.receiver_repository import ReceiverRepository
from app.infrastructure.persistence.repositories.retention_repository import RetentionRepository
from app.infrastructure.persistence.repositories.tax_or_retention_repository import (
    TaxOrRetentionRepository,
)
from app.infrastructure.persistence.repositories.tax_repository import TaxRepository
from app.infrastructure.queue.download_queue import get_queue

load_dotenv()


def get_rag_client(token: Annotated[TokenData, Depends(get_token_data)]) -> RagClient:
    url = os.getenv("RAG_SERVICE_URL", "http://rag-service:8002")
    return RagClient(base_url=url, bearer_token=token.raw_token)


def get_integration_config_client(
    token: Annotated[TokenData, Depends(get_token_data)],
) -> IntegrationConfigClient:
    url = os.getenv("INTEGRATION_CONFIG_URL", "http://integration-config-service:8007")
    return IntegrationConfigClient(base_url=url, bearer_token=token.raw_token)


def get_llm_client(token: Annotated[TokenData, Depends(get_token_data)]) -> LlmClient:
    url = os.getenv("LLM_SERVICE_URL", "http://llm-service:8003")
    return LlmClient(base_url=url, bearer_token=token.raw_token)


def get_process_xml_use_case(
    db: Session = Depends(get_tenant_db),
    integration_config_client: IntegrationConfigClient = Depends(get_integration_config_client),
    llm_client: LlmClient = Depends(get_llm_client),
) -> ProcessXmlUseCase:
    # Sin rag_client: RF-08 desplaza la indexación al cierre de la contabilización.
    return ProcessXmlUseCase(
        document_repo=DocumentRepository(db),
        issuer_repo=IssuerRepository(db),
        receiver_repo=ReceiverRepository(db),
        tax_repo=TaxRepository(db),
        concept_repo=ConceptRepository(db),
        integration_config_client=integration_config_client,
        llm_client=llm_client,
    )


def get_documents_by_date_range_use_case(
    db: Session = Depends(get_tenant_db),
) -> GetDocumentsByDateRangeUseCase:
    return GetDocumentsByDateRangeUseCase(document_repo=DocumentRepository(db))


def get_document_by_id_use_case(db: Session = Depends(get_tenant_db)) -> GetDocumentByIdUseCase:
    return GetDocumentByIdUseCase(document_repo=DocumentRepository(db))


def get_all_receivers_use_case(db: Session = Depends(get_tenant_db)) -> GetAllReceiversUseCase:
    return GetAllReceiversUseCase(receiver_repo=ReceiverRepository(db))


def get_list_issuers_use_case(db: Session = Depends(get_tenant_db)) -> ListIssuersUseCase:
    return ListIssuersUseCase(issuer_repo=IssuerRepository(db))


def get_update_issuer_use_case(db: Session = Depends(get_tenant_db)) -> UpdateIssuerUseCase:
    return UpdateIssuerUseCase(issuer_repo=IssuerRepository(db))


def get_issuer_by_nit_use_case(db: Session = Depends(get_tenant_db)) -> GetIssuerByNitUseCase:
    return GetIssuerByNitUseCase(issuer_repo=IssuerRepository(db))


def get_document_repo(db: Session = Depends(get_tenant_db)) -> DocumentRepository:
    return DocumentRepository(db)


def get_document_tax_repo(db: Session = Depends(get_tenant_db)) -> DocumentTaxRepository:
    return DocumentTaxRepository(db)


def get_concept_repo(db: Session = Depends(get_tenant_db)) -> ConceptRepository:
    return ConceptRepository(db)


def get_integration_tax_repo(db: Session = Depends(get_tenant_db)) -> IntegrationTaxRepository:
    return IntegrationTaxRepository(db)


def get_integration_retention_repo(
    db: Session = Depends(get_tenant_db),
) -> IntegrationRetentionRepository:
    return IntegrationRetentionRepository(db)


def get_tax_or_retention_repo(db: Session = Depends(get_tenant_db)) -> TaxOrRetentionRepository:
    """`document_taxes.tax_id` puede apuntar a un impuesto o a una retención: las rutas que
    validan ese campo (crear/editar un impuesto del documento, persistir sugerencias de la
    IA) usan este repositorio combinado en vez de `IntegrationTaxRepository` solo."""
    return TaxOrRetentionRepository(db)


def get_cost_center_repo(db: Session = Depends(get_tenant_db)) -> CostCenterRepository:
    return CostCenterRepository(db)


def get_puc_repo(db: Session = Depends(get_tenant_db)) -> PucRepository:
    return PucRepository(db)


def get_retention_repo(db: Session = Depends(get_tenant_db)) -> RetentionRepository:
    return RetentionRepository(db)


def get_import_retention_rates_use_case(
    db: Session = Depends(get_tenant_db),
) -> ImportRetentionRatesUseCase:
    return ImportRetentionRatesUseCase(RetentionRepository(db))


def get_process_downloads_use_case() -> ProcessDownloadsUseCase:
    return ProcessDownloadsUseCase(
        downloads_dir=os.getenv("DOWNLOADS_DIR", "/app/downloads"),
        queue=get_queue(),
    )


def get_process_single_file_use_case() -> ProcessSingleFileUseCase:
    return ProcessSingleFileUseCase(
        downloads_dir=os.getenv("DOWNLOADS_DIR", "/app/downloads"),
        queue=get_queue(),
    )


def get_processing_log_repo(db: Session = Depends(get_tenant_db)) -> ProcessingLogRepository:
    return ProcessingLogRepository(db)


def get_approve_document_use_case(
    db: Session = Depends(get_tenant_db),
) -> ApproveDocumentUseCase:
    return ApproveDocumentUseCase(document_repo=DocumentRepository(db))


def get_causar_document_use_case(
    db: Session = Depends(get_tenant_db),
) -> CausarDocumentUseCase:
    return CausarDocumentUseCase(document_repo=DocumentRepository(db))


def get_unapprove_document_use_case(
    db: Session = Depends(get_tenant_db),
) -> UnapproveDocumentUseCase:
    return UnapproveDocumentUseCase(document_repo=DocumentRepository(db))


def get_bulk_causar_documents_use_case(
    db: Session = Depends(get_tenant_db),
) -> BulkCausarDocumentsUseCase:
    return BulkCausarDocumentsUseCase(document_repo=DocumentRepository(db))


def get_bulk_approve_documents_use_case(
    db: Session = Depends(get_tenant_db),
) -> BulkApproveDocumentsUseCase:
    return BulkApproveDocumentsUseCase(document_repo=DocumentRepository(db))


def get_bulk_unapprove_documents_use_case(
    db: Session = Depends(get_tenant_db),
) -> BulkUnapproveDocumentsUseCase:
    return BulkUnapproveDocumentsUseCase(document_repo=DocumentRepository(db))


def get_account_document_use_case(
    token: Annotated[TokenData, Depends(get_token_data)],
    db: Session = Depends(get_tenant_db),
) -> AccountDocumentUseCase:
    """RF-05: caso de uso de contabilización en SIIGO.

    El token del usuario se propaga al siigo-service: la credencial de SIIGO se resuelve por
    tenant, así que la llamada tiene que viajar autenticada como el usuario que la originó.
    """
    return build_account_document_use_case(db, token.raw_token, token.tenant_slug)


def build_account_document_use_case(
    db: Session, raw_token: str = "", tenant_slug: str = ""
) -> AccountDocumentUseCase:
    """Arma el caso de uso fuera del sistema de dependencias de FastAPI.

    Existe separado porque los workers de la cola también lo necesitan, y allí no hay
    petición HTTP de la que colgar un `Depends`. Con una sola función de construcción, el
    documento se contabiliza exactamente igual venga del endpoint o del worker — que es
    justo lo que debe ocurrir cuando la seguridad contable depende de esa ruta.
    """
    # Sin token de usuario la llamada va por la ruta interna de siigo-service, autenticada
    # con `X-Internal-Secret`. Es el caso del worker, que se despierta sin sesión de nadie.
    client = build_siigo_service_client(raw_token, tenant_slug=None if raw_token else tenant_slug)
    return AccountDocumentUseCase(
        document_repo=DocumentRepository(db),
        # Se pasa como función y no como valor para no consultar la plantilla en cada
        # petición de la API, sino solo cuando de verdad se va a contabilizar.
        parameters_provider=client.get_purchase_invoice_parameters,
        siigo_client=client,
        knowledge_publisher=build_knowledge_publisher(db, tenant_slug),
        audit_repo=AccountingAuditRepository(db),
    )


def get_accounting_job_repo(
    db: Session = Depends(get_tenant_db),
) -> AccountingJobRepository:
    return AccountingJobRepository(db)


def get_accounting_audit_repo(
    db: Session = Depends(get_tenant_db),
) -> AccountingAuditRepository:
    return AccountingAuditRepository(db)


def get_accounting_queue_service(
    db: Session = Depends(get_tenant_db),
) -> AccountingQueueService:
    """RF-05: alta de trabajos en la cola de contabilización.

    No recibe el token porque encolar no habla con SIIGO: solo escribe filas. El token hace
    falta en el worker, que es quien acaba llamando, y allí se toma del trabajo encolado.
    """
    return AccountingQueueService(
        document_repo=DocumentRepository(db),
        job_repo=AccountingJobRepository(db),
    )


def build_knowledge_publisher(db: Session, tenant_slug: str) -> AccountingKnowledgePublisher:
    """RF-08: publicador de conocimiento contable validado.

    Se construye igual para RF-05 y RF-06 porque el conocimiento no depende de por qué camino
    llegó el documento a «Contabilizada», sino de que lo esté.
    """
    return AccountingKnowledgePublisher(
        rag_client=build_rag_knowledge_client(),
        tenant_slug=tenant_slug,
        document_repo=DocumentRepository(db),
        tax_repo=DocumentTaxRepository(db),
        # Combinado (impuestos + retenciones): `document_taxes.tax_id` puede resolver en
        # cualquiera de las dos tablas, y el nombre tiene que salir de la que corresponda —
        # de lo contrario la causación indexada nombraría "Impuesto #10608" en vez de
        # "ReteIVA 15%" para toda retención contabilizada después de la migración del
        # 2026-08-31.
        integration_tax_repo=TaxOrRetentionRepository(db),
        cost_center_repo=CostCenterRepository(db),
        # Aporta el municipio del caso: el documento de la DIAN no lo trae y la única fuente
        # en el sistema son las tarifas de ReteICA configuradas.
        retention_repo=RetentionRepository(db),
    )


def get_reconcile_document_use_case(
    token: Annotated[TokenData, Depends(get_token_data)],
    db: Session = Depends(get_tenant_db),
) -> ReconcileDocumentUseCase:
    """RF-06: consulta y resolución de un documento con el cerrojo de contabilización puesto.

    Comparte cliente con la contabilización porque la credencial de SIIGO se resuelve por
    cliente y la consulta tiene que viajar autenticada como el usuario que la originó.
    """
    return ReconcileDocumentUseCase(
        document_repo=DocumentRepository(db),
        siigo_client=build_siigo_service_client(token.raw_token),
        knowledge_publisher=build_knowledge_publisher(db, token.tenant_slug),
    )


def get_document_detail_use_case(
    db: Session = Depends(get_tenant_db),
) -> GetDocumentDetailUseCase:
    return GetDocumentDetailUseCase(document_repo=DocumentRepository(db))


def get_dashboard_use_case(db: Session = Depends(get_tenant_db)) -> DashboardUseCase:
    return DashboardUseCase(
        repo=DashboardRepository(db, timezone=get_timezone()),
        targets=load_targets(),
        today=today_local,
    )
