from fastapi import Depends
from sqlalchemy.orm import Session

from app.application.use_cases.import_chart_accounts import ImportChartAccountsUseCase
from app.application.use_cases.import_cost_centers import ImportCostCentersUseCase
from app.application.use_cases.import_payment_types import ImportPaymentTypesUseCase
from app.application.use_cases.sync_siigo_cost_centers import SyncSiigoCostCentersUseCase
from app.application.use_cases.sync_siigo_products import SyncSiigoProductsUseCase
from app.application.use_cases.import_taxes import ImportTaxesUseCase
from app.application.use_cases.sync_siigo_payment_types import SyncSiigoPaymentTypesUseCase
from app.application.use_cases.sync_siigo_taxes import SyncSiigoTaxesUseCase
from app.application.use_cases.import_products import ImportProductsUseCase
from app.application.use_cases.manage_credentials import ManageCredentialsUseCase
from app.application.use_cases.manage_purchase_invoice_parameters import (
    ManagePurchaseInvoiceParametersUseCase,
)
from app.application.use_cases.manage_storage_settings import ManageStorageSettingsUseCase
from app.infrastructure.config.auth_dependency import get_tenant_db
from app.infrastructure.persistence.repositories.chart_account_repository import (
    ChartAccountRepository,
)
from app.infrastructure.persistence.repositories.cost_center_repository import CostCenterRepository
from app.infrastructure.persistence.repositories.integration_repository import (
    IntegrationCredentialRepository,
)
from app.infrastructure.persistence.repositories.payment_type_repository import PaymentTypeRepository
from app.infrastructure.persistence.repositories.tax_repository import TaxRepository
from app.infrastructure.persistence.repositories.product_repository import ProductRepository
from app.infrastructure.persistence.repositories.purchase_invoice_parameter_repository import (
    PurchaseInvoiceParameterRepository,
)
from app.infrastructure.persistence.repositories.storage_repository import (
    DocumentNamingSettingRepository,
    S3StorageConfigRepository,
    SharePointStorageConfigRepository,
)


def get_storage_settings_use_case(
    db: Session = Depends(get_tenant_db),
) -> ManageStorageSettingsUseCase:
    return ManageStorageSettingsUseCase(
        s3_repository=S3StorageConfigRepository(db),
        sharepoint_repository=SharePointStorageConfigRepository(db),
        naming_repository=DocumentNamingSettingRepository(db),
    )


def get_credentials_use_case(db: Session = Depends(get_tenant_db)) -> ManageCredentialsUseCase:
    return ManageCredentialsUseCase(IntegrationCredentialRepository(db))


def get_purchase_invoice_parameters_use_case(
    db: Session = Depends(get_tenant_db),
) -> ManagePurchaseInvoiceParametersUseCase:
    return ManagePurchaseInvoiceParametersUseCase(PurchaseInvoiceParameterRepository(db))


def get_import_chart_accounts_use_case(
    db: Session = Depends(get_tenant_db),
) -> ImportChartAccountsUseCase:
    return ImportChartAccountsUseCase(ChartAccountRepository(db))


def get_cost_center_repository(db: Session = Depends(get_tenant_db)) -> CostCenterRepository:
    return CostCenterRepository(db)


def get_import_cost_centers_use_case(
    db: Session = Depends(get_tenant_db),
) -> ImportCostCentersUseCase:
    return ImportCostCentersUseCase(CostCenterRepository(db))


def get_sync_siigo_cost_centers_use_case(
    db: Session = Depends(get_tenant_db),
) -> SyncSiigoCostCentersUseCase:
    return SyncSiigoCostCentersUseCase(
        credential_repository=IntegrationCredentialRepository(db),
        cost_center_repository=CostCenterRepository(db),
    )


def get_payment_type_repository(db: Session = Depends(get_tenant_db)) -> PaymentTypeRepository:
    return PaymentTypeRepository(db)


def get_import_payment_types_use_case(
    db: Session = Depends(get_tenant_db),
) -> ImportPaymentTypesUseCase:
    return ImportPaymentTypesUseCase(PaymentTypeRepository(db))


def get_sync_siigo_payment_types_use_case(
    db: Session = Depends(get_tenant_db),
) -> SyncSiigoPaymentTypesUseCase:
    return SyncSiigoPaymentTypesUseCase(
        credential_repository=IntegrationCredentialRepository(db),
        payment_type_repository=PaymentTypeRepository(db),
    )


def get_tax_repository(db: Session = Depends(get_tenant_db)) -> TaxRepository:
    return TaxRepository(db)


def get_import_taxes_use_case(db: Session = Depends(get_tenant_db)) -> ImportTaxesUseCase:
    return ImportTaxesUseCase(TaxRepository(db))


def get_sync_siigo_taxes_use_case(db: Session = Depends(get_tenant_db)) -> SyncSiigoTaxesUseCase:
    return SyncSiigoTaxesUseCase(
        credential_repository=IntegrationCredentialRepository(db),
        tax_repository=TaxRepository(db),
    )


def get_product_repository(db: Session = Depends(get_tenant_db)) -> ProductRepository:
    return ProductRepository(db)


def get_import_products_use_case(db: Session = Depends(get_tenant_db)) -> ImportProductsUseCase:
    return ImportProductsUseCase(ProductRepository(db))


def get_sync_siigo_products_use_case(
    db: Session = Depends(get_tenant_db),
) -> SyncSiigoProductsUseCase:
    return SyncSiigoProductsUseCase(
        credential_repository=IntegrationCredentialRepository(db),
        product_repository=ProductRepository(db),
    )
