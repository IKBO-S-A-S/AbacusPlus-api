from fastapi import APIRouter, Depends, HTTPException, status

from app.application.dto.storage import (
    DocumentNamingResponse,
    DocumentNamingUpsertRequest,
    S3StorageResponse,
    S3StorageUpsertRequest,
    SharePointStorageResponse,
    SharePointStorageUpsertRequest,
)
from app.application.use_cases.manage_storage_settings import ManageStorageSettingsUseCase
from app.dependencies import get_storage_settings_use_case

router = APIRouter()


@router.put(
    "/integrations/storage/s3",
    response_model=S3StorageResponse,
    status_code=status.HTTP_200_OK,
    summary="Configurar credenciales de almacenamiento en S3",
    description=(
        "Crea o actualiza la configuracion de S3 del tenant. Solo existe una fila por tenant: "
        "cada llamada reemplaza la configuracion anterior. `secret_access_key` se cifra antes "
        "de persistir y nunca se retorna en la respuesta."
    ),
    response_description="Configuracion de S3 registrada sin exponer el secret access key.",
    responses={400: {"description": "Payload invalido o bucket_name vacio."}},
)
def upsert_s3_storage(
    request: S3StorageUpsertRequest,
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> S3StorageResponse:
    return use_case.upsert_s3(request)


@router.get(
    "/integrations/storage/s3",
    response_model=S3StorageResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar configuracion de S3",
    description="Retorna la configuracion de S3 del tenant si existe, sin exponer el secret access key.",
    response_description="Configuracion de S3 vigente.",
    responses={404: {"description": "No hay configuracion de S3 registrada."}},
)
def get_s3_storage(
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> S3StorageResponse:
    config = use_case.get_s3()
    if config is None:
        raise HTTPException(status_code=404, detail="S3 storage not configured")
    return config


@router.put(
    "/integrations/storage/sharepoint",
    response_model=SharePointStorageResponse,
    status_code=status.HTTP_200_OK,
    summary="Configurar credenciales de almacenamiento en SharePoint",
    description=(
        "Crea o actualiza la configuracion de SharePoint del tenant (autenticacion app-only "
        "via Microsoft Graph). Solo existe una fila por tenant: cada llamada reemplaza la "
        "configuracion anterior. `client_secret` se cifra antes de persistir y nunca se "
        "retorna en la respuesta."
    ),
    response_description="Configuracion de SharePoint registrada sin exponer el client secret.",
    responses={400: {"description": "Payload invalido o site_id vacio."}},
)
def upsert_sharepoint_storage(
    request: SharePointStorageUpsertRequest,
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> SharePointStorageResponse:
    return use_case.upsert_sharepoint(request)


@router.get(
    "/integrations/storage/sharepoint",
    response_model=SharePointStorageResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar configuracion de SharePoint",
    description="Retorna la configuracion de SharePoint del tenant si existe, sin exponer el client secret.",
    response_description="Configuracion de SharePoint vigente.",
    responses={404: {"description": "No hay configuracion de SharePoint registrada."}},
)
def get_sharepoint_storage(
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> SharePointStorageResponse:
    config = use_case.get_sharepoint()
    if config is None:
        raise HTTPException(status_code=404, detail="SharePoint storage not configured")
    return config


@router.put(
    "/integrations/storage/naming-pattern",
    response_model=DocumentNamingResponse,
    status_code=status.HTTP_200_OK,
    summary="Configurar el patron de nombre de archivo de los documentos",
    description=(
        "Define el patron de nombre aplicado tanto al PDF como al XML generados al procesar "
        "un documento DIAN (misma base, distinta extension), sin importar el destino de "
        "almacenamiento (local, S3 o SharePoint)."
    ),
    response_description="Patron de nombre registrado.",
    responses={400: {"description": "filename_pattern vacio."}},
)
def upsert_naming_pattern(
    request: DocumentNamingUpsertRequest,
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> DocumentNamingResponse:
    return use_case.upsert_naming_pattern(request)


@router.get(
    "/integrations/storage/naming-pattern",
    response_model=DocumentNamingResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar el patron de nombre de archivo vigente",
    description=(
        "Retorna el patron configurado, o el patron por defecto si el tenant no ha "
        "configurado ninguno."
    ),
    response_description="Patron de nombre vigente.",
)
def get_naming_pattern(
    use_case: ManageStorageSettingsUseCase = Depends(get_storage_settings_use_case),
) -> DocumentNamingResponse:
    return DocumentNamingResponse(id=1, filename_pattern=use_case.get_naming_pattern())
