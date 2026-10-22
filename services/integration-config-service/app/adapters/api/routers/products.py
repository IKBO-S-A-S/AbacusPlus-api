from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from openpyxl import Workbook

from app.adapters.api.routers._excel_template import style_header, xlsx_response
from app.application.dto.product import ImportProductsResponse, ProductResponse
from app.application.use_cases.import_products import ImportProductsUseCase
from app.application.use_cases.sync_siigo_products import SyncSiigoProductsUseCase
from app.dependencies import (
    get_import_products_use_case,
    get_product_repository,
    get_sync_siigo_products_use_case,
)
from app.infrastructure.config.auth_dependency import require_write
from app.infrastructure.persistence.repositories.product_repository import ProductRepository

router = APIRouter()

PRODUCTS_EXCEL_STRUCTURE = """
Estructura del Excel:

| Columna       | Obligatoria | Ejemplo                      | Descripcion                              |
| ------------- | ----------- | ----------------------------- | ---------------------------------------- |
| `código`      | Si          | `P-001`                       | Codigo unico del producto o servicio.    |
| `tipo`        | Si          | `producto`                    | Tipo: `producto` o `servicio`.           |
| `descripción` | Si          | `Licencia de software anual`  | Descripcion del producto o servicio.     |
| `activo`      | No          | `true`                        | Estado. Si se omite, queda `true`.       |

Valores booleanos aceptados: `true`, `false`, `1`, `0`, `yes`, `no`, `si`, `sí`, `x`.

`code`, `type`, `description` y `active` tambien se aceptan como alias (igual que
`product`/`service` para `tipo`): son el encabezado con que este endpoint funciono antes
de que la plantilla pasara a espanol.
"""


@router.get(
    "/integrations/products",
    response_model=list[ProductResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar productos y servicios",
    description=(
        "Retorna todos los productos y servicios registrados en la tabla local `integration_products`.\n\n"
        "Usa el filtro `active` para obtener solo los activos o solo los inactivos.\n\n"
        "Usa el filtro `type` para obtener solo productos (`product`) o solo servicios (`service`)."
    ),
    response_description="Lista de productos ordenada por codigo.",
)
def list_products(
    active: Optional[bool] = Query(
        None, description="Filtrar por estado activo. Si se omite, retorna todos."
    ),
    type: Optional[str] = Query(None, description="Filtrar por tipo: 'product' o 'service'."),
    repository: ProductRepository = Depends(get_product_repository),
) -> list[ProductResponse]:
    return repository.list(active=active, type=type)


@router.post(
    "/integrations/products/imports",
    dependencies=[Depends(require_write)],
    response_model=ImportProductsResponse,
    status_code=status.HTTP_200_OK,
    summary="Importar productos y servicios desde Excel",
    description=(
        "Recibe un archivo `.xlsx` con productos o servicios y alimenta "
        "la tabla local `integration_products`.\n\n"
        "La operacion es idempotente por `code`: si el producto ya existe, "
        "actualiza sus datos; si no existe, lo crea.\n\n"
        f"{PRODUCTS_EXCEL_STRUCTURE}"
    ),
    response_description="Resumen de productos importados y listado resultante.",
    responses={
        400: {
            "description": "Archivo invalido, columnas faltantes, tipo invalido o filas con datos incorrectos."
        },
    },
)
async def import_products_from_excel(
    sheet_name: Optional[str] = Form(
        None, description="Nombre de hoja a leer. Si se omite, usa la primera hoja."
    ),
    file: UploadFile = File(..., description="Archivo Excel .xlsx con los productos."),
    mode: str = Form(
        "upsert",
        description=(
            "`upsert`: actualiza productos existentes y agrega nuevos (no elimina). "
            "`replace`: elimina todo el catalogo de productos actual (incluidos los "
            "sincronizados desde SIIGO) antes de importar."
        ),
        examples=["upsert"],
    ),
    use_case: ImportProductsUseCase = Depends(get_import_products_use_case),
) -> ImportProductsResponse:
    content = await file.read()
    return use_case.execute(
        sheet_name=sheet_name,
        file_content=content,
        mode=mode,
    )


@router.get(
    "/integrations/products/template",
    summary="Descargar plantilla Excel de productos y servicios",
    description=(
        "Genera un `.xlsx` listo para llenar y volver a importar via "
        "`POST /integrations/products/imports`.\n\n"
        "La hoja llega solo con encabezados: los productos y servicios son propios de cada "
        "empresa y no existe una tabla estandar que precargar.\n\n"
        f"{PRODUCTS_EXCEL_STRUCTURE}"
    ),
    response_description="Archivo .xlsx de plantilla.",
)
def download_products_template():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Productos"
    sheet.append(["código", "tipo", "descripción", "activo"])
    style_header(
        sheet,
        notes={
            "código": "Codigo unico del producto o servicio. Identifica cada fila al importar de nuevo.",
            "tipo": "Tipo: 'producto' o 'servicio'.",
            "descripción": "Descripcion del producto o servicio.",
            "activo": "Opcional. true/false. Si se omite, queda true.",
        },
        widths={"A": 14, "B": 12, "C": 34, "D": 10},
    )
    return xlsx_response(workbook, "plantilla-productos.xlsx")


@router.post(
    "/integrations/products/siigo-syncs",
    dependencies=[Depends(require_write)],
    response_model=ImportProductsResponse,
    status_code=status.HTTP_200_OK,
    summary="Sincronizar productos y servicios desde SIIGO",
    description=(
        "Consulta el endpoint `GET /v1/products` de la API de SIIGO y sincroniza "
        "los resultados en la tabla local `integration_products`.\n\n"
        "La operacion es idempotente por `code`: actualiza si ya existe, crea si no. "
        "Los productos que ya no existen en SIIGO son eliminados de la tabla local.\n\n"
        "Si el token de acceso ha expirado o no existe, autentica automaticamente contra SIIGO "
        "y persiste el nuevo token antes de hacer la consulta."
    ),
    response_description="Resumen de productos sincronizados y listado resultante.",
    responses={
        404: {"description": "No existe credencial activa para siigo con el account_key indicado."},
        502: {"description": "SIIGO no responde o retorna error."},
    },
)
def sync_products_from_siigo(
    use_case: SyncSiigoProductsUseCase = Depends(get_sync_siigo_products_use_case),
) -> ImportProductsResponse:
    return use_case.execute()
