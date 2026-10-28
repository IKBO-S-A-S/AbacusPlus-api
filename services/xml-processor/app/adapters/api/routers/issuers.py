from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.application.dto.issuer import IssuerListResponse, IssuerResponse, IssuerUpdateRequest
from app.application.use_cases.query_issuers import (
    GetIssuerByNitUseCase,
    ListIssuersUseCase,
    UpdateIssuerUseCase,
)
from app.dependencies import (
    get_issuer_by_nit_use_case,
    get_list_issuers_use_case,
    get_update_issuer_use_case,
)
from app.domain.exceptions.base import ValidationException
from app.infrastructure.config.auth_dependency import require_write

router = APIRouter()


@router.get(
    "/issuers",
    response_model=IssuerListResponse,
    status_code=status.HTTP_200_OK,
    summary="Listar emisores (terceros)",
    description=(
        "Lista paginada de emisores con **todas las columnas** de la tabla `issuers`.\n\n"
        "- `q` filtra por coincidencia parcial (sin distinguir mayúsculas) en nombre, NIT o correo.\n"
        "- Orden fijo por nombre.\n"
        "- `total` es el conteo con el filtro aplicado, para paginar en el cliente."
    ),
    response_description="Página de emisores y total de coincidencias.",
)
def list_issuers(
    q: Optional[str] = Query(None, description="Texto a buscar en nombre, NIT o correo."),
    limit: int = Query(50, ge=1, le=500, description="Tamaño de página."),
    offset: int = Query(0, ge=0, description="Registros a omitir."),
    use_case: ListIssuersUseCase = Depends(get_list_issuers_use_case),
):
    return use_case.execute(q, limit, offset)


@router.patch(
    "/issuers/{nit}",
    dependencies=[Depends(require_write)],
    response_model=IssuerResponse,
    status_code=status.HTTP_200_OK,
    summary="Actualizar un emisor (tercero)",
    description=(
        "Modifica los datos de un emisor. Es parcial: solo cambia lo que viene en el body.\n\n"
        "- **No editables**: `nit` (identifica al emisor) y `dv` (se deriva del NIT).\n"
        "- `name` y `email` son únicos y no pueden quedar vacíos.\n"
        "- `tipo_contribuyente` son códigos DIAN separados por `;`; se normalizan a mayúsculas "
        "y sin repetidos.\n"
        "- `payment_id` debe ser un medio de pago existente y activo. Solo afecta a los "
        "documentos procesados después; los ya cargados conservan el suyo.\n"
        "- `notes` alimenta la sugerencia de retenciones del LLM: ver la descripción del campo "
        "para saber qué se lee y con qué límites.\n"
        "- `item_assignment_mode` define cómo se asignan las líneas de sus facturas: `account` "
        "(siempre por cuenta PUC), `product` (por producto del catálogo; la línea que no case "
        "va por cuenta) o `null` (sin definir: con la primera factura el sistema lo decide). "
        "Solo afecta a las asignaciones que se calculen después.\n\n"
        "Requiere rol con permiso de escritura (`tenant_admin` u `operator`)."
    ),
    response_description="Emisor con todas sus columnas ya actualizadas.",
    responses={
        404: {"description": "No hay emisor con ese NIT."},
        409: {"description": "Otro emisor ya usa ese `name` o `email`."},
        422: {
            "description": (
                "Datos inválidos, `payment_id` inexistente o inactivo, o "
                "`item_assignment_mode` distinto de `account`, `product` o `null`."
            )
        },
    },
)
def update_issuer(
    nit: str,
    request: IssuerUpdateRequest,
    use_case: UpdateIssuerUseCase = Depends(get_update_issuer_use_case),
):
    fields = request.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El body no trae ningún campo para actualizar.",
        )
    try:
        return use_case.execute(nit, fields)
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message)


@router.get(
    "/issuers/{nit}",
    response_model=IssuerResponse,
    summary="Obtener emisor por NIT",
    description=(
        "Retorna los datos de un emisor registrado en el sistema, incluyendo su cuenta contable "
        "de cuentas por pagar (`account_number`) y su régimen tributario (`tipo_contribuyente`).\n\n"
        "Estos campos son utilizados por el llm-service para enriquecer el contexto de causación "
        "contable: la cuenta CxP del proveedor y si aplica IVA descontable."
    ),
    response_description="Datos del emisor con cuenta CxP y régimen.",
    responses={
        404: {"description": "Emisor no encontrado."},
    },
)
def get_issuer_by_nit(
    nit: str,
    use_case: GetIssuerByNitUseCase = Depends(get_issuer_by_nit_use_case),
):
    issuer = use_case.execute(nit)
    if issuer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Emisor con NIT {nit} no encontrado.",
        )
    return issuer
