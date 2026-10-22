"""DTOs de la contabilización de una factura de compra en SIIGO (RF-05).

Los nombres y las restricciones de cada campo salen del contrato oficial de
`POST /v1/purchases` (Siigo API, grupo «Factura de compra o gasto»). Cuando un campo es
obligatorio aquí es porque la documentación lo marca como obligatorio, no por criterio
propio; lo mismo con los máximos de decimales y longitudes.
"""

# Se importa con alias porque hay un campo llamado `date` (así lo nombra SIIGO): sin el
# alias, el nombre del campo sombrea al tipo y Pydantic falla al construir el modelo.
from datetime import date as date_type
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


class PurchaseInvoiceItem(BaseModel):
    """Una línea de la factura de compra.

    Una línea es de **cuenta** (`type` «Account», `code` = código PUC) o de **producto**
    (`type` «Product», `code` = código del producto en SIIGO). Un mismo documento puede mezclar
    ambas: lo decide el tercero y la línea (ver `item_assignment_mode` en el xml-processor).
    «FixedAsset» lo admite SIIGO pero ningún flujo de este sistema lo produce hoy.
    """

    type: str = Field(
        "Account",
        description=(
            "Tipo de ítem. SIIGO solo admite 'Product', 'FixedAsset' o 'Account'. "
            "'Account' lleva una cuenta contable; 'Product', un producto del catálogo."
        ),
        examples=["Account"],
    )
    code: str = Field(
        ...,
        description=(
            "Con `type=Account`: código de la cuenta contable (PUC) asignada a la línea. Debe "
            "existir y estar activa en SIIGO Nube. SIIGO rechaza cuentas ligadas a grupos de "
            "inventario, activos fijos o impuestos. Con `type=Product`: código del producto en "
            "SIIGO."
        ),
        examples=["51951001"],
    )
    description: Optional[str] = Field(
        None,
        description="Nombre o descripción de la línea. Opcional según SIIGO.",
        examples=["Servicio de mantenimiento"],
    )
    quantity: Decimal = Field(
        ...,
        gt=0,
        description="Cantidad. SIIGO la registra con máximo 2 decimales.",
        examples=["1"],
    )
    price: Decimal = Field(
        ...,
        ge=0,
        description="Valor unitario. SIIGO admite máximo 6 decimales.",
        examples=["150000"],
    )
    tax_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Identificadores de los impuestos de la línea (IVA). Se envían a SIIGO como "
            "items[].taxes[].id. Deben existir en el catálogo de impuestos de SIIGO."
        ),
        examples=[[13156]],
    )


class SendPurchaseInvoiceRequest(BaseModel):
    """Datos necesarios para crear una factura de compra en SIIGO.

    Se separa en dos bloques por origen del dato:

    - Los **identificadores de SIIGO** (`document_id`, `payment_id`, `cost_center`,
      `retention_ids`, `tax_ids`) provienen de la plantilla local
      `purchase_invoice_parameters` y de los catálogos sincronizados. Nunca se inventan.
    - Los **datos del documento** (fecha, proveedor, líneas, valores) provienen del documento
      electrónico real descargado de la DIAN.
    """

    document_id: int = Field(
        ...,
        description=(
            "Identificador del tipo de comprobante de compra en SIIGO. Obligatorio. Debe "
            "existir previamente en SIIGO; se consulta por /v1/document-types?type=FC."
        ),
        examples=[7100],
    )
    date: date_type = Field(
        ...,
        description="Fecha del comprobante en formato YYYY-MM-DD. Obligatorio para SIIGO.",
        examples=["2026-08-10"],
    )
    supplier_identification: str = Field(
        ...,
        min_length=1,
        description=(
            "Identificación del proveedor (NIT del emisor del documento DIAN, sin dígito de "
            "verificación). El tercero debe existir y estar activo en SIIGO Nube."
        ),
        examples=["900123456"],
    )
    supplier_branch_office: int = Field(
        0,
        ge=0,
        description="Sucursal del proveedor. Opcional en SIIGO; por defecto 0.",
        examples=[0],
    )
    items: list[PurchaseInvoiceItem] = Field(
        ...,
        min_length=1,
        description="Líneas de la factura. SIIGO exige al menos una.",
    )
    payment_id: int = Field(
        ...,
        description=(
            "Identificador del medio de pago en SIIGO. Obligatorio: SIIGO lo usa para generar "
            "la cuenta por pagar. Se consulta por /v1/payment-types?document_type=FC."
        ),
        examples=[5636],
    )
    payment_value: Decimal = Field(
        ...,
        gt=0,
        description="Valor asociado al medio de pago. Obligatorio, máximo 2 decimales.",
        examples=["150000"],
    )
    payment_due_date: Optional[date_type] = Field(
        None,
        description=(
            "Fecha de vencimiento. SIIGO la exige solo si el medio de pago maneja vencimiento."
        ),
        examples=["2026-09-10"],
    )
    cost_center: Optional[int] = Field(
        None,
        description=(
            "Centro de costo del documento (RF-07, modalidad general). Opcional en SIIGO. "
            "Debe existir y estar activo. En /v1/purchases solo existe a nivel de documento, "
            "no por línea."
        ),
        examples=[1235],
    )
    retention_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Identificadores de las retenciones (ReteICA, ReteIVA) que aplican al documento "
            "(RF-02). SIIGO los recibe como un array de ids en 'retentions'."
        ),
        examples=[[1136]],
    )
    observations: Optional[str] = Field(
        None,
        max_length=4000,
        description="Observaciones del comprobante. Límite de 4.000 caracteres en SIIGO.",
    )
    provider_invoice_prefix: Optional[str] = Field(
        None,
        description="Prefijo de la factura del proveedor, si el tipo de comprobante lo exige.",
        examples=["SETP"],
    )
    provider_invoice_number: Optional[str] = Field(
        None,
        description="Número de la factura del proveedor (consecutivo del documento DIAN).",
        examples=["990000001"],
    )
    discount_type: Optional[str] = Field(
        None,
        description="Tipo de descuento del comprobante. SIIGO solo admite 'Percentage' o 'Value'.",
        examples=["Value"],
    )
    tax_included: Optional[bool] = Field(
        None,
        description="Indica si los precios ya incluyen impuesto. Opcional en SIIGO.",
    )
    currency_code: Optional[str] = Field(
        None,
        min_length=3,
        max_length=3,
        description=(
            "Código de moneda. Opcional: si no se envía, SIIGO toma la moneda local. Solo se "
            "debe enviar si la empresa maneja moneda extranjera."
        ),
        examples=["USD"],
    )
    currency_exchange_rate: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Tasa de cambio. Solo aplica junto con currency_code.",
    )
    account_key: str = Field(
        "default",
        description="Clave de la credencial de SIIGO a usar (multi-empresa).",
        examples=["default"],
    )


class SendPurchaseInvoiceResponse(BaseModel):
    """Resultado de la creación en SIIGO, ya validado."""

    siigo_id: str = Field(
        ...,
        description="Identificador que devuelve SIIGO. Es la prueba de que la factura existe.",
    )
    siigo_name: Optional[str] = Field(
        None,
        description="Consecutivo/nombre del comprobante en SIIGO, tal como lo ve el contador.",
    )
    siigo_response: dict[str, Any] = Field(
        ...,
        description="Respuesta cruda de SIIGO, para trazabilidad.",
    )
    supports_consumption_tax: Optional[bool] = Field(
        None,
        description=(
            "Si el comprobante de compra configurado en SIIGO maneja impuesto al consumo "
            "(`consumption_tax` de `GET /v1/document-types?type=FC`).\n\n"
            "Permite decidir entre enviar el impuesto de forma nativa en `items[].taxes` o "
            "recurrir a una línea de ajuste. `null` significa que no se pudo consultar el "
            "catálogo, no que el comprobante no lo admita."
        ),
        examples=[True],
    )


class PurchaseInvoiceMatch(BaseModel):
    """Una factura de compra que ya existe en SIIGO y podría ser la del documento buscado."""

    siigo_id: str = Field(
        ...,
        description="Identificador de la factura en SIIGO.",
        examples=["63f918c2-ca65-4edc-a7db-66bcdd5159fb"],
    )
    siigo_name: Optional[str] = Field(
        None,
        description="Consecutivo del comprobante tal como lo ve el contador.",
        examples=["FC-1-125"],
    )
    date: Optional[str] = Field(
        None, description="Fecha del comprobante en SIIGO.", examples=["2026-08-11"]
    )
    total: Optional[Decimal] = Field(
        None, description="Total del comprobante, para contrastarlo con el documento DIAN."
    )
    provider_invoice_number: Optional[str] = Field(
        None,
        description="Número de la factura del proveedor registrado en SIIGO.",
        examples=["FE1234"],
    )
    provider_invoice_prefix: Optional[str] = Field(
        None, description="Prefijo de la factura del proveedor.", examples=["FE"]
    )


class FindPurchaseInvoiceResponse(BaseModel):
    """Resultado de buscar en SIIGO una factura de compra ya creada (RF-06).

    Existe para resolver el único desenlace que la contabilización no puede cerrar sola: el
    documento quedó en «Contabilizando» porque no se supo si SIIGO llegó a crear la factura.
    Reenviarlo a ciegas duplicaría un asiento real —`/v1/purchases` no admite
    `Idempotency-Key`—, así que la salida es preguntarle a SIIGO qué tiene.
    """

    matches: list[PurchaseInvoiceMatch] = Field(
        default_factory=list,
        description=(
            "Facturas de SIIGO cuyo número de factura del proveedor coincide con el buscado. "
            "Vacío significa que SIIGO no creó nada y el documento puede reenviarse."
        ),
    )
    searched_provider_invoice_number: Optional[str] = Field(
        None, description="Número de factura del proveedor que se buscó."
    )
    searched_from: Optional[str] = Field(
        None, description="Inicio del rango de fechas consultado en SIIGO."
    )
    searched_to: Optional[str] = Field(
        None, description="Fin del rango de fechas consultado en SIIGO."
    )
