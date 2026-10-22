from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.application.dto.document_tax import DocumentTaxResponse
from app.infrastructure.config.accounting_settings import get_accounting_settings

#: Tope de validación del DTO. Se lee al importar el módulo porque el esquema OpenAPI se
#: construye una sola vez al arrancar; quien lo hace cumplir en cada envío es el servicio de
#: cola, que relee la configuración. Duplicarlo aquí solo adelanta el rechazo con un mensaje
#: mejor que un 500 por un lote absurdo.
_MAX_LOTE = get_accounting_settings().batch_max_size


class DocumentDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Identificador único de la línea.")
    document_id: int = Field(..., description="ID del documento al que pertenece.")
    description: str = Field(..., description="Descripción del concepto facturado.")
    concept_description_id: int = Field(..., description="ID del concepto en el catálogo local.")
    quantity: float = Field(..., description="Cantidad.")
    unit: str = Field(..., description="Unidad de medida.")
    price: float = Field(..., description="Precio unitario.")
    subtotal: float = Field(..., description="Subtotal sin impuesto.")
    tax_type: str = Field(..., description="Porcentaje de impuesto como texto (ej. '19.0').")
    tax_value: float = Field(..., description="Valor del impuesto.")
    total: float = Field(..., description="Total de la línea (subtotal + impuesto).")
    # ── Desglose de IVA e INC, cada uno con SU tarifa y SU importe ────────────
    #
    # `tax_type`/`tax_value` guardan el impuesto PRINCIPAL de la línea, no el IVA: en una
    # línea que solo lleva INC, son el INC. Se mantienen sin tocar —los usan la
    # contabilización y el RAG— y estos cuatro campos se añaden al lado para que cada
    # impuesto se pueda leer con su nombre correcto.
    iva_percentage: Optional[float] = Field(
        None,
        description="Tarifa del IVA de la línea. Null si la línea no lleva IVA.",
        examples=[19.0],
    )
    iva_value: float = Field(0.0, description="Importe del IVA de la línea.", examples=[2370.25])
    inc_percentage: Optional[float] = Field(
        None,
        description=(
            "Tarifa del impuesto al consumo. Null si la línea no lleva INC, si el documento "
            "es anterior a que se conservara el desglose, o si la línea trae varios INC con "
            "tarifas distintas —ahí el importe es la suma pero no hay una tarifa única que lo "
            "explique, y devolver una de ellas sería inventarla—."
        ),
        examples=[4.0],
    )
    inc_value: float = Field(0.0, description="Importe del impuesto al consumo.", examples=[499.0])
    concept_account_number: Optional[str] = Field(
        None,
        description="Cuenta PUC del catálogo de conceptos. Null si no tiene concepto asignado.",
    )
    code: Optional[str] = Field(
        None,
        description="Código PUC asignado por el LLM. Null si aún no se ha procesado.",
        examples=["511500"],
    )
    type: str = Field(
        "Account",
        description="Tipo de ítem contable: Account, Product o FixedAsset.",
        examples=["Account"],
    )
    tax_id: Optional[int] = Field(
        None, description="ID del impuesto en integration_taxes. Null si no hubo coincidencia."
    )
    cost_center_id: Optional[int] = Field(
        None, description="ID del centro de costo asignado por historial. Null si no hay historial."
    )
    code_source: Optional[str] = Field(
        None,
        description=(
            "RF-04: origen de la cuenta asignada. `llm` si la sugirió el modelo, `manual` si "
            "la editó el contador, `null` si la línea aún no tiene cuenta. La interfaz lo usa "
            "para marcar el ítem y para exigir confirmación antes de sobrescribir una edición "
            "manual con una nueva sugerencia."
        ),
        examples=["manual"],
    )
    code_suggested: Optional[str] = Field(
        None,
        description=(
            "RF-04: última cuenta propuesta por el LLM. Se conserva aunque el contador la "
            "sobrescriba, de modo que junto con `code` y `code_source` permite distinguir los "
            "tres estados de la línea: sin asignar (`code_source` null), sugerida por el modelo "
            "(`code == code_suggested`) y editada a mano (`code != code_suggested`)."
        ),
        examples=["613505"],
    )


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_name: str
    document_number: str
    date: date
    hour: str
    currency: str
    document_type: str
    uuid: str
    issuer_name: str
    issuer_nit: str
    issuer_phone: Optional[str] = None
    issuer_email: Optional[str] = None
    receiver_name: str
    receiver_nit: str
    receiver_phone: Optional[str] = None
    receiver_email: Optional[str] = None
    receiver_responsibilities: Optional[str] = Field(
        None,
        description=(
            "Responsabilidades fiscales del RECEPTOR (comprador) del RUT, códigos separados "
            "por ';' (p. ej. 'O-13;O-23'). Definen si el comprador es agente de retención."
        ),
    )
    subtotal: float
    total_taxes: float
    total_consumption_tax: float = Field(
        0.0,
        description=(
            "Impuesto al consumo (INC) del documento, sumando el de todas sus líneas. Va "
            "incluido dentro de `total_taxes` junto al IVA; se expone aparte para poder "
            "verlo desglosado."
        ),
        examples=[499.0],
    )
    retefuente: float = 0.0
    reteica: float = 0.0
    total: float
    register_at: datetime
    status: int
    pdf_url: Optional[str] = Field(
        None,
        description="RF-03: enlace del PDF en Amazon S3 (si ya se subió). La interfaz lo renderiza.",
    )
    xml_url: Optional[str] = Field(
        None,
        description="RF-03: enlace del XML oficial en Amazon S3 (si ya se subió). La interfaz lo renderiza.",
    )
    payment_type_id: Optional[int] = Field(
        None,
        description="ID del medio de pago en integration_payment_types. Null si el emisor no tiene uno configurado.",
    )
    cost_center_id: Optional[int] = Field(
        None,
        description=(
            "RF-07: centro de costo del documento (integration_cost_centers). Es el que se "
            "envía a SIIGO al contabilizar, que solo admite un centro general por factura de "
            "compra. Null si aún no se ha asignado."
        ),
        examples=[3],
    )
    details: list[DocumentDetailResponse] = []
    # RF-02: retenciones a nivel de documento (ReteFuente, ReteICA, ReteIVA, …).
    # El IVA NO va aquí: es a nivel de ítem y llega en `details`/`total_taxes`.
    taxes: list[DocumentTaxResponse] = Field(
        default=[],
        description="RF-02: retenciones del documento con base gravable, porcentaje y valor.",
    )
    xml_withholdings: Optional[list[dict]] = Field(
        None,
        description=(
            "Retenciones que el PROVEEDOR declara en el XML (`cac:WithholdingTaxTotal`). "
            "No son las que se practican —eso lo deciden el perfil fiscal del comprador y la "
            "configuración del tercero— sino la única señal independiente para contrastar lo "
            "que Abacus determinó: SIIGO no informa qué retenciones aplicó. Cada elemento "
            "lleva `esquema`, `tipo`, `nombre`, `porcentaje`, `base` y `valor`. Ojo con la "
            "unidad: el XML declara la ReteICA como porcentaje verdadero (0.966) y el "
            "catálogo de SIIGO la guarda por mil (9.66)."
        ),
        examples=[[{"esquema": "07", "tipo": "reteica", "porcentaje": 0.966, "valor": 24043.45}]],
    )


class DocumentSummaryResponse(BaseModel):
    """Resumen de un documento para listados (sin líneas de detalle)."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "document_number": "FE7674",
                "document_name": "factura_fe7674.xml",
                "document_type": "Factura Electrónica",
                "date": "2024-01-15",
                "issuer_name": "PROVEEDOR S.A.S",
                "issuer_nit": "900123456",
                "receiver_name": "MI EMPRESA S.A.S",
                "receiver_nit": "800987654",
                "subtotal": 100000.0,
                "total_taxes": 19000.0,
                "retefuente": 0.0,
                "reteica": 0.0,
                "total": 119000.0,
                "status": 100,
                "register_at": "2024-01-15T10:30:00",
            }
        },
    )

    id: int = Field(..., description="Identificador único del documento.")
    document_number: str = Field(
        ..., description="Número de la factura electrónica.", examples=["FE7674"]
    )
    document_name: str = Field(..., description="Nombre del archivo XML o ZIP procesado.")
    document_type: str = Field(
        ..., description="Tipo de documento DIAN.", examples=["Factura Electrónica"]
    )
    date: date
    issuer_name: str = Field(..., description="Razón social del emisor.")
    issuer_nit: str = Field(..., description="NIT del emisor.", examples=["900123456"])
    receiver_name: str = Field(..., description="Razón social del receptor.")
    receiver_nit: str = Field(..., description="NIT del receptor.", examples=["800987654"])
    subtotal: float = Field(..., description="Subtotal antes de impuestos.")
    total_taxes: float = Field(..., description="Total de impuestos (IVA e INC).")
    total_consumption_tax: float = Field(
        0.0, description="Impuesto al consumo (INC), ya incluido dentro de `total_taxes`."
    )
    retefuente: float = Field(..., description="Valor de retención en la fuente.")
    reteica: float = Field(..., description="Valor de reteICA.")
    total: float = Field(..., description="Valor total del documento.")
    status: int = Field(
        ...,
        description=(
            "Código de estado del documento. 0=Error, 100=Procesado, 200=Causado, "
            "300=Aprobado, 350=Contabilizando, 400=Contabilizada."
        ),
        examples=[100],
    )
    register_at: datetime = Field(..., description="Fecha y hora de registro en el sistema.")
    payment_type_id: Optional[int] = Field(
        None,
        description="ID del medio de pago configurado para el emisor. Null si no tiene uno asignado.",
    )
    cost_center_id: Optional[int] = Field(
        None,
        description="RF-07: centro de costo del documento (el que se contabiliza en SIIGO).",
    )
    # RF-05 / RF-06: resultado de la contabilización. Van en el listado y no solo en el
    # detalle porque el contador necesita ver, de un vistazo sobre la tabla, cuáles quedaron
    # contabilizados y por qué fallaron los demás, sin abrir uno por uno.
    siigo_id: Optional[str] = Field(
        None,
        description=(
            "RF-05: identificador devuelto por SIIGO. Presente solo si el documento está "
            "contabilizado; es la prueba de que la factura existe en SIIGO."
        ),
        examples=["63f918c2-ca65-4edc-a7db-66bcdd5159fb"],
    )
    siigo_name: Optional[str] = Field(
        None,
        description="RF-05: consecutivo del comprobante en SIIGO.",
        examples=["FC-1-125"],
    )
    siigo_total: Optional[float] = Field(
        None,
        description=(
            "RF-05: total por el que SIIGO contabilizó la factura. Se guarda junto al "
            "documento y no solo dentro de la auditoría, para que esté disponible también en "
            "los documentos cerrados por reconciliación, que no registran ningún intento."
        ),
        examples=[83800.00],
    )
    siigo_total_matches_dian: Optional[bool] = Field(
        None,
        description=(
            "RF-05: si el total contabilizado coincide con el de la factura de la DIAN, con "
            "una tolerancia de un peso por el redondeo de la DIAN.\n\n"
            "`false` señala un documento **contabilizado por un importe distinto al "
            "facturado**. No impide nada —la factura ya existe en SIIGO— pero debe revisarse "
            "y corregirse en SIIGO; reenviarlo duplicaría un asiento real. `null` significa "
            "que no hubo con qué comparar, no que coincida."
        ),
        examples=[True],
    )
    accounted_at: Optional[datetime] = Field(
        None, description="RF-05: fecha y hora en que SIIGO confirmó la contabilización."
    )
    accounting_error: Optional[str] = Field(
        None,
        description=(
            "RF-06: último error de contabilización devuelto por SIIGO o por la validación "
            "previa. Se conserva para que el contador lo revise y corrija antes de "
            "reintentar."
        ),
        examples=["La cuenta 51951001 no existe en Siigo Nube"],
    )
    # RF-05: capacidades del documento. Es lo ÚNICO que la interfaz necesita para decidir
    # qué botones habilita en la columna «Acciones».
    #
    # Se exponen como booleanos y no como la clasificación interna a propósito. El contador
    # no tiene por qué aprender un vocabulario de clases de error: ve el estado, ve el
    # mensaje de lo que pasó, y ve dos botones. Que por dentro haga falta distinguir un
    # timeout de una cuenta PUC inválida —y hace falta, de ello depende que no se duplique un
    # asiento— es un problema del backend, no de la pantalla.
    #
    # La ventaja práctica es que la clasificación puede crecer sin tocar el frontend: un
    # error nuevo se mapea a un par de booleanos que ya existen.
    #: Clasificación interna. `exclude=True`: se lee del ORM para derivar las capacidades,
    #: pero NO se serializa. El usuario no debe recibir un vocabulario de clases de error que
    #: no le sirve para nada —lo accionable son los dos booleanos de abajo—, y no exponerlo
    #: además impide que el frontend empiece a depender de nombres internos.
    accounting_recommended_action: Optional[str] = Field(None, exclude=True)
    accounting_error_code: Optional[str] = Field(
        None,
        description=(
            "RF-05: código de error de SIIGO. Se expone para diagnóstico y soporte; la "
            "interfaz no lo muestra."
        ),
        examples=["invalid_reference"],
    )
    accounting_locked: bool = Field(
        False,
        description=(
            "RF-05: cerrojo de contabilización. True significa que hay un envío en curso o "
            "uno cuyo desenlace se desconoce; en ambos casos el documento no puede volver a "
            "enviarse a SIIGO hasta que se verifique. Sustituye al antiguo estado 350."
        ),
        examples=[False],
    )
    accounting_attempts: int = Field(
        0, description="RF-05: intentos de contabilización acumulados.", examples=[0]
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accounting_can_edit(self) -> bool:
        """Si el documento admite corrección de su causación.

        True cuando SIIGO rechazó un dato contable —cuenta PUC, centro de costo, impuesto,
        retención o tercero— y corregirlo es lo que desatasca el documento.

        Es deliberadamente False en un desenlace incierto: ese documento puede estar ya
        contabilizado en SIIGO, y editarle la causación crearía una discrepancia silenciosa
        entre lo que Abacus cree que envió y lo que SIIGO tiene registrado.
        """
        from app.domain.value_objects.accounting_error import can_edit

        return can_edit(self.accounting_recommended_action or "")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accounting_can_retry(self) -> bool:
        """Si el documento puede volver a enviarse a la cola de contabilización.

        False cuando el desenlace del último envío no consta —timeout, 5xx, respuesta sin
        identificador—: en esos casos la factura pudo haberse creado en SIIGO y reenviarla
        duplicaría un asiento contable real, que es el daño que no se deshace desde aquí.

        El cerrojo se comprueba además del veredicto: mientras esté puesto no hay reintento
        posible por ninguna vía, y el botón debe reflejarlo aunque la clasificación se
        hubiera quedado desfasada por cualquier motivo.
        """
        from app.domain.value_objects.accounting_error import can_retry

        if self.accounting_locked:
            return False
        return can_retry(self.accounting_recommended_action or "")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accounting_message(self) -> Optional[str]:
        """Mensaje del último fallo, tal como debe leerlo el contador.

        Es un alias de `accounting_error` con nombre propio porque es lo que la interfaz
        muestra literalmente junto al estado. Tenerlo nombrado por su función —y no por la
        columna donde vive— evita que un futuro cambio de almacenamiento arrastre al
        frontend.
        """
        return self.accounting_error

    @field_validator("accounting_locked", mode="before")
    @classmethod
    def _cerrojo_por_defecto(cls, valor):
        """Trata la ausencia de valor como el valor por defecto.

        Las dos columnas son NOT NULL con DEFAULT en la base, pero un objeto ORM recién
        construido y todavía sin volcar tiene None: el valor por defecto lo pone PostgreSQL
        al insertar, no Python al instanciar. Sin esta tolerancia, serializar un documento
        antes del flush falla con un error de validación que no describe ningún problema
        real.

        Se resuelve aquí y no relajando el tipo a opcional porque el contrato hacia el
        frontend sí debe ser un booleano y un entero: quien lee `accounting_locked` está
        decidiendo si puede reenviar un documento a SIIGO, y un `null` en esa pregunta no
        significa nada útil.
        """
        return False if valor is None else valor

    @field_validator("accounting_attempts", mode="before")
    @classmethod
    def _intentos_por_defecto(cls, valor):
        """Mismo motivo que `_cerrojo_por_defecto`: sin filas insertadas no hay intentos."""
        return 0 if valor is None else valor


class DocumentDetailCodeUpdateItem(BaseModel):
    detail_id: int = Field(
        ..., description="ID de la línea de detalle a actualizar.", examples=[42]
    )
    code: Optional[str] = Field(
        None,
        max_length=80,
        description=(
            "Código PUC (`type=Account`) o código del catálogo de productos (`type=Product`). "
            "Omitir para no modificar."
        ),
        examples=["511500"],
    )
    type: Optional[str] = Field(
        None,
        description=(
            "Tipo de ítem contable. Valores: Account, Product, FixedAsset. Omitir para no "
            "modificar; si se envía `code` sin `type`, la línea queda como Account."
        ),
        examples=["Account"],
    )
    cost_center_id: Optional[int] = Field(
        None, description="ID del centro de costo. Omitir para no modificar.", examples=[1]
    )
    tax_id: Optional[int] = Field(
        None, description="ID del impuesto. Omitir para no modificar.", examples=[2]
    )
    model_config = {
        "json_schema_extra": {
            "example": {
                "detail_id": 42,
                "code": "511500",
                "type": "Account",
                "cost_center_id": 1,
                "tax_id": 2,
            }
        }
    }

    @field_validator("type")
    @classmethod
    def _tipo_conocido(cls, v):
        if v is not None and v not in ("Account", "Product", "FixedAsset"):
            raise ValueError("`type` debe ser Account, Product o FixedAsset.")
        return v

    @model_validator(mode="after")
    def _tipo_exige_codigo(self):
        # Cambiar solo el tipo dejaría la línea con el código del tipo anterior (un código PUC
        # marcado Product, o al revés) y SIIGO la rechazaría. El tipo se valida contra el
        # catálogo que le corresponde, y eso requiere el código en la misma petición.
        if self.type is not None and "code" not in self.model_fields_set:
            raise ValueError("Para cambiar `type` envíe también `code`.")
        return self

    @model_validator(mode="after")
    def _codigo_sin_tipo_es_cuenta(self):
        # Una línea que antes era Product y recibe una cuenta sin decir el tipo se quedaría
        # con `type=Product` y un código PUC: SIIGO lo rechazaría. Asignar `type` aquí lo
        # incluye en `model_fields_set`, así que el repositorio lo escribe.
        if self.code is not None and "type" not in self.model_fields_set:
            self.type = "Account"
        return self


class DocumentDetailCodeUpdateResponse(BaseModel):
    updated: int = Field(..., description="Cantidad de líneas actualizadas.", examples=[3])
    rejected: list[str] = Field(
        default_factory=list,
        description=(
            "Motivos de las asignaciones descartadas por no cumplir las reglas contables "
            "(cuenta inexistente, inactiva, agrupadora o de clase inválida). La ruta pública "
            "responde 422 ante el primer motivo, así que este campo solo se llena en la "
            "ruta interna que consume el llm-service."
        ),
        examples=[["La cuenta '415005' no existe en el catálogo PUC sincronizado."]],
    )


class DocumentCostCenterUpdateRequest(BaseModel):
    cost_center_id: Optional[int] = Field(
        None,
        description=(
            "RF-07: ID del centro de costo del documento. Debe existir en el catálogo del "
            "tenant. `null` lo deja sin centro de costo (opcional)."
        ),
        examples=[3],
    )
    model_config = {"json_schema_extra": {"example": {"cost_center_id": 3}}}


class DocumentPaymentTypeUpdateRequest(BaseModel):
    payment_type_id: int = Field(
        ..., description="ID del medio de pago a asignar al documento.", examples=[1]
    )
    model_config = {"json_schema_extra": {"example": {"payment_type_id": 1}}}


class DocumentStatusUpdateRequest(BaseModel):
    status: int = Field(
        ...,
        description=(
            "Código de estado destino. Valores soportados: "
            "200 (Causado — revierte aprobación, el documento debe estar en estado Aprobado)."
        ),
        examples=[200],
    )
    model_config = {"json_schema_extra": {"example": {"status": 200}}}


#: Tope de documentos por transición masiva. No es una limitación técnica del UPDATE —una
#: sola sentencia mueve miles sin esfuerzo— sino un límite de superficie: acota el tamaño del
#: cuerpo que un cliente puede enviar y el de la respuesta que se construye, de modo que una
#: petición mal formada no pueda convertirse en una carga arbitraria para el servicio.
MAX_DOCUMENTOS_POR_TRANSICION = 500


class DocumentBulkStatusUpdateRequest(BaseModel):
    """Transición de estado de varios documentos en una sola petición."""

    document_ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=MAX_DOCUMENTOS_POR_TRANSICION,
        description=(
            "Documentos a mover. Los ids repetidos se procesan una sola vez. "
            f"Máximo {MAX_DOCUMENTOS_POR_TRANSICION} por petición."
        ),
        examples=[[24, 25, 26]],
    )
    status: int = Field(
        ...,
        description=(
            "Código de estado destino:\n"
            "- `200` (Causado) — avanza los documentos en `Procesado`.\n"
            "- `300` (Aprobado) — avanza los documentos en `Causado`.\n"
            "- `201` — **retrocede** a Causado los documentos en `Aprobado` "
            "(cancelar aprobación). Es un código sintético de la API, no un estado: "
            "el documento queda en 200. Existe porque a Causado se llega por dos caminos "
            "opuestos y el destino no basta para distinguirlos en un lote heterogéneo."
        ),
        examples=[200],
    )
    model_config = {"json_schema_extra": {"example": {"document_ids": [24, 25, 26], "status": 200}}}


class DocumentBulkStatusUpdateResponse(BaseModel):
    """Resultado por documento de una transición masiva.

    Las cuatro listas son disjuntas y su unión son todos los ids pedidos (sin repetidos), de
    modo que la interfaz pueda explicar cada documento sin volver a consultar nada.
    """

    requested: int = Field(..., description="Documentos distintos recibidos.", examples=[26])
    updated: list[int] = Field(
        ..., description="Cambiaron de estado en esta operación.", examples=[[24, 25]]
    )
    unchanged: list[int] = Field(
        ...,
        description="Ya estaban en el estado destino. La operación es idempotente.",
        examples=[[26]],
    )
    rejected: list[int] = Field(
        ...,
        description=(
            "Existen, pero su estado actual no admite la transición (por ejemplo, causar "
            "un documento ya Contabilizado). No se modificaron."
        ),
        examples=[[]],
    )
    not_found: list[int] = Field(
        ..., description="No existen en la base del cliente.", examples=[[]]
    )


class ProcessXmlResponse(BaseModel):
    status: str
    data: dict
    document_id: int
    filename: str


class DocumentFileLinksResponse(BaseModel):
    """RF-03: enlaces del PDF y el XML del documento en Amazon S3."""

    document_id: int = Field(..., description="Documento publicado.", examples=[24])
    pdf_url: Optional[str] = Field(
        None,
        description="Enlace del PDF en S3 una vez publicado.",
        examples=[
            "https://mi-bucket.s3.us-west-2.amazonaws.com/abacusplus/documentos/ikbo/FBC98359_pdf_2026-08-01.pdf"
        ],
    )
    xml_url: Optional[str] = Field(
        None, description="Enlace del XML en S3, si el documento lo tiene almacenado."
    )
    uploaded: list[str] = Field(
        default_factory=list,
        description="Archivos publicados en esta ejecución (`pdf`, `xml`).",
        examples=[["pdf", "xml"]],
    )
    skipped: list[str] = Field(
        default_factory=list,
        description=(
            "Archivos no publicados: o el documento no los tiene almacenados, o ya tenían "
            "enlace y no se pidió reemplazarlo."
        ),
        examples=[["xml"]],
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Motivo por el que un archivo no pudo publicarse, si lo hubo.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "document_id": 24,
                "pdf_url": "https://mi-bucket.s3.us-west-2.amazonaws.com/abacusplus/documentos/ikbo/FBC98359_pdf_2026-08-01.pdf",
                "xml_url": None,
                "uploaded": ["pdf"],
                "skipped": ["xml"],
                "warnings": [],
            }
        }
    }


class DocumentFileLinksBatchResponse(BaseModel):
    """RF-03: resultado de publicar en lote los documentos que aún no tienen enlace."""

    processed: int = Field(..., description="Documentos evaluados.", examples=[82])
    published: int = Field(
        ..., description="Documentos que obtuvieron al menos un enlace nuevo.", examples=[80]
    )
    failed: int = Field(
        ..., description="Documentos cuya subida falló; pueden reintentarse.", examples=[2]
    )
    results: list[DocumentFileLinksResponse] = Field(
        default_factory=list, description="Detalle por documento."
    )

    model_config = {
        "json_schema_extra": {
            "example": {"processed": 82, "published": 80, "failed": 2, "results": []}
        }
    }


class DocumentAccountingResponse(BaseModel):
    """RF-05: resultado de contabilizar un documento en SIIGO."""

    document_id: int = Field(..., description="Documento contabilizado.", examples=[12457])
    ok: bool = Field(..., description="True solo si SIIGO confirmó la creación.", examples=[True])
    status: int = Field(
        ...,
        description=(
            "Estado funcional resultante: 0 Error, 300 Aprobado, 400 Contabilizada. "
            "Cualquier fallo deja el documento en 0; lo que distingue un fallo de otro es "
            "`error_class` y `recommended_action`, no el estado."
        ),
        examples=[400],
    )
    siigo_id: Optional[str] = Field(
        None,
        description="Identificador devuelto por SIIGO. Es la prueba de la contabilización.",
        examples=["63f918c2-ca65-4edc-a7db-66bcdd5159fb"],
    )
    siigo_name: Optional[str] = Field(
        None, description="Consecutivo del comprobante en SIIGO.", examples=["FC-1-125"]
    )
    error: Optional[str] = Field(
        None, description="RF-06: motivo del fallo, en términos accionables por el contador."
    )
    error_class: Optional[str] = Field(
        None,
        description=(
            "Naturaleza técnica del fallo (`TRANSIENT`, `RATE_LIMIT`, `UNCERTAIN`, "
            "`DUPLICATE`, `CORRECTABLE`, `CONFIG`, `UNKNOWN`). Es información de "
            "diagnóstico y auditoría: **la interfaz no la muestra**. Para decidir qué "
            "ofrecer al usuario están `can_edit` y `can_retry`."
        ),
        examples=["CORRECTABLE"],
    )
    can_edit: bool = Field(
        False,
        description=(
            "Si el documento admite corrección de su causación antes de volver a enviarlo."
        ),
        examples=[True],
    )
    can_retry: bool = Field(
        False,
        description=(
            "Si el documento puede volver a enviarse a la cola. False cuando el desenlace "
            "del envío no consta: la factura pudo haberse creado y reenviarla duplicaría un "
            "asiento contable real."
        ),
        examples=[True],
    )
    error_code: Optional[str] = Field(
        None,
        description="Código de error devuelto por SIIGO, si lo hubo.",
        examples=["invalid_reference"],
    )
    needs_reconciliation: bool = Field(
        False,
        description=(
            "True cuando el documento quedó con el cerrojo de contabilización puesto porque "
            "no se pudo confirmar el desenlace. NO debe reenviarse: hay que verificar en "
            "SIIGO. Equivale a `recommended_action = VERIFICAR_EN_SIIGO`."
        ),
        examples=[False],
    )
    auto_retryable: bool = Field(
        False,
        description="True si la cola puede repetir el envío sola, sin que nadie corrija nada.",
        examples=[False],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "document_id": 12457,
                "ok": True,
                "status": 400,
                "siigo_id": "63f918c2-ca65-4edc-a7db-66bcdd5159fb",
                "siigo_name": "FC-1-125",
                "error": None,
                "needs_reconciliation": False,
            }
        }
    }


class DocumentAccountingBatchRequest(BaseModel):
    """RF-05: selección de documentos aprobados a contabilizar."""

    document_ids: list[int] = Field(
        ...,
        min_length=1,
        # El límite se importa del caso de uso en vez de repetir el número: estaba escrito
        # en dos sitios y nada garantizaba que cambiaran juntos. Aquí solo se valida antes
        # de entrar; quien lo hace cumplir de verdad sigue siendo el caso de uso.
        max_length=_MAX_LOTE,
        description=(
            f"Documentos a contabilizar. Máximo {_MAX_LOTE} por envío. Los documentos se "
            "encolan y se procesan en segundo plano, respetando el límite de peticiones por "
            "minuto de SIIGO."
        ),
        examples=[[12457, 12458]],
    )

    model_config = {"json_schema_extra": {"example": {"document_ids": [12457, 12458]}}}


class DocumentAccountingBatchResponse(BaseModel):
    """RF-05: resumen del lote, con el detalle individual de cada documento."""

    total: int = Field(..., description="Documentos recibidos.", examples=[10])
    successful: int = Field(..., description="Contabilizados con éxito.", examples=[8])
    failed: int = Field(..., description="Fallidos; revisar el error de cada uno.", examples=[2])
    skipped: int = Field(..., description="Omitidos por no existir.", examples=[0])
    results: list[DocumentAccountingResponse] = Field(
        default_factory=list, description="Resultado individual, en el orden recibido."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "total": 10,
                "successful": 8,
                "failed": 2,
                "skipped": 0,
                "results": [],
            }
        }
    }


class SiigoInvoiceMatch(BaseModel):
    """Una factura de compra que ya existe en SIIGO y podría ser la de este documento."""

    siigo_id: str = Field(
        ...,
        description="Identificador de la factura en SIIGO.",
        examples=["63f918c2-ca65-4edc-a7db-66bcdd5159fb"],
    )
    siigo_name: Optional[str] = Field(
        None, description="Consecutivo del comprobante en SIIGO.", examples=["FC-1-125"]
    )
    date: Optional[str] = Field(None, description="Fecha del comprobante.", examples=["2026-08-11"])
    total: Optional[float] = Field(
        None, description="Total del comprobante, para contrastarlo con el documento."
    )
    provider_invoice_number: Optional[str] = Field(
        None, description="Número de factura del proveedor registrado en SIIGO."
    )
    provider_invoice_prefix: Optional[str] = Field(None, description="Prefijo de esa factura.")


class DocumentReconciliationView(BaseModel):
    """RF-06: lo que SIIGO tiene sobre un documento bloqueado. No modifica nada."""

    document_id: int = Field(..., description="Documento consultado.", examples=[12457])
    status: int = Field(..., description="Estado actual del documento.", examples=[350])
    consulted: bool = Field(
        ...,
        description=(
            "True si la consulta a SIIGO se completó. False significa que NO se sabe si la "
            "factura existe, y en ese caso el documento no debe reenviarse."
        ),
        examples=[True],
    )
    matches: list[SiigoInvoiceMatch] = Field(
        default_factory=list,
        description="Facturas de SIIGO que corresponden a este documento.",
    )
    suggested_action: str = Field(
        "none",
        description=(
            "Qué propone el sistema: 'close' si la factura ya existe en SIIGO, 'release' si "
            "consta que no existe, 'none' si no se pudo determinar."
        ),
        examples=["close"],
    )
    message: str = Field("", description="Explicación en los términos del contador.")
    error: Optional[str] = Field(None, description="Motivo por el que no se pudo consultar.")


class DocumentReconciliationRequest(BaseModel):
    """Resolución que el contador confirma tras ver el resultado de la consulta."""

    siigo_id: Optional[str] = Field(
        None,
        description=(
            "Identificador de la factura hallada en SIIGO. Con él, el documento se cierra "
            "como Contabilizada SIN volver a llamar a SIIGO. Omitirlo (o enviarlo nulo) "
            "declara que SIIGO no tiene la factura y libera el documento para reenviarlo."
        ),
        examples=["63f918c2-ca65-4edc-a7db-66bcdd5159fb"],
    )
    siigo_name: Optional[str] = Field(
        None, description="Consecutivo del comprobante, para mostrarlo junto al documento."
    )
    siigo_total: Optional[float] = Field(
        None,
        description=(
            "Total de la factura tal como la tiene SIIGO. Viene de la consulta previa "
            "(`GET /documents/{id}/siigo-invoices`) y se guarda junto al documento: sin él, "
            "un documento cerrado por esta vía queda sin total en su ficha de confirmación, "
            "porque la reconciliación no registra ningún intento de contabilización."
        ),
        examples=[83800.00],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "siigo_id": "63f918c2-ca65-4edc-a7db-66bcdd5159fb",
                "siigo_name": "FC-1-125",
                "siigo_total": 83800.00,
            }
        }
    }


class DocumentReconciliationResponse(BaseModel):
    """Resultado de aplicar la reconciliación confirmada."""

    document_id: int = Field(..., description="Documento reconciliado.", examples=[12457])
    status: int = Field(
        ...,
        description="Estado resultante: 400 Contabilizada si se cerró, 0 Error si se liberó.",
        examples=[400],
    )
    siigo_id: Optional[str] = Field(None, description="Identificador con el que quedó cerrado.")
    siigo_name: Optional[str] = Field(None, description="Consecutivo del comprobante en SIIGO.")
    message: str = Field("", description="Qué ocurrió, en términos del contador.")


# ── RF-05: cola de contabilización ─────────────────────────────────────────────


class AccountingEnqueueItem(BaseModel):
    """Un documento aceptado por la cola."""

    document_id: int = Field(..., description="Documento encolado.", examples=[12457])
    job_id: int = Field(..., description="Trabajo creado para él.", examples=[884])


class AccountingRejectedItem(BaseModel):
    """Un documento que no entró en la cola, con el motivo."""

    document_id: int = Field(..., description="Documento rechazado.", examples=[12460])
    reason: str = Field(
        ...,
        description="Por qué no se encoló, en términos accionables por el contador.",
        examples=["El documento ya está contabilizado en SIIGO."],
    )


class AccountingEnqueueResponse(BaseModel):
    """RF-05: acuse de recibo del envío a la cola.

    Responde de inmediato, sin esperar a SIIGO. El progreso se consulta después con
    `GET /documents/accounting-batches/{batch_id}`.
    """

    batch_id: str = Field(
        ...,
        description="Identificador del lote, con el que consultar el progreso.",
        examples=["6f1c2d9a8b3e4f5061728394a5b6c7d8"],
    )
    total: int = Field(..., description="Documentos recibidos en la petición.", examples=[10])
    enqueued: list[AccountingEnqueueItem] = Field(
        default_factory=list, description="Documentos que entraron en la cola."
    )
    rejected: list[AccountingRejectedItem] = Field(
        default_factory=list,
        description=(
            "Documentos que no entraron y por qué. Que un documento sea rechazado no es un "
            "error del lote: los demás siguen su curso."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "batch_id": "6f1c2d9a8b3e4f5061728394a5b6c7d8",
                "total": 3,
                "enqueued": [{"document_id": 12457, "job_id": 884}],
                "rejected": [
                    {
                        "document_id": 12460,
                        "reason": "El documento ya está contabilizado en SIIGO.",
                    }
                ],
            }
        }
    }


class AccountingBatchProgress(BaseModel):
    """RF-05: progreso de un lote encolado."""

    batch_id: str = Field(..., description="Lote consultado.")
    total: int = Field(..., description="Trabajos del lote.", examples=[10])
    pending: int = Field(..., description="A la espera de un worker o de su backoff.", examples=[2])
    running: int = Field(..., description="Con la llamada a SIIGO en curso.", examples=[1])
    successful: int = Field(..., description="Contabilizados en SIIGO.", examples=[6])
    failed: int = Field(..., description="Fallidos; el documento queda en Error.", examples=[1])
    needs_reconciliation: int = Field(
        ...,
        description=(
            "Desenlace desconocido: hay que verificar en SIIGO si la factura existe antes "
            "de reenviar. Se cuentan aparte de los fallidos porque su tratamiento es otro."
        ),
        examples=[0],
    )
    cancelled: int = Field(..., description="Cancelados antes de salir hacia SIIGO.", examples=[0])
    finished: int = Field(..., description="Trabajos ya terminados.", examples=[7])
    done: bool = Field(..., description="True cuando no queda nada por procesar.", examples=[False])


class AccountingAttemptResponse(BaseModel):
    """RF-05: un intento de contabilización, tal como quedó registrado.

    Es la unidad de la auditoría: qué se le envió a SIIGO, qué contestó y cómo se
    interpretó. Nunca se modifica.
    """

    id: int = Field(..., description="Identificador del intento.")
    document_id: int = Field(..., description="Documento contabilizado.")
    job_id: Optional[int] = Field(None, description="Trabajo de la cola que lo originó.")
    attempt: int = Field(..., description="Número de intento, empezando en 1.", examples=[2])
    started_at: datetime = Field(..., description="Cuándo empezó el intento.")
    finished_at: Optional[datetime] = Field(None, description="Cuándo terminó.")
    duration_ms: Optional[int] = Field(None, description="Duración en milisegundos.")
    http_status: Optional[int] = Field(None, description="Código HTTP de la respuesta.")
    ok: bool = Field(..., description="True si SIIGO confirmó la creación.")
    siigo_id: Optional[str] = Field(None, description="Identificador devuelto por SIIGO.")
    siigo_name: Optional[str] = Field(None, description="Consecutivo del comprobante.")
    error_message: Optional[str] = Field(None, description="Error, en términos accionables.")
    error_code: Optional[str] = Field(None, description="Código de error de SIIGO.")
    error_class: Optional[str] = Field(None, description="Clase del error.")
    recommended_action: Optional[str] = Field(None, description="Acción recomendada.")
    triggered_by: Optional[str] = Field(
        None,
        description=(
            "Quién provocó el intento: el usuario que lo encoló, o `worker` si fue un "
            "reintento automático. Distinguirlos permite auditar si un envío lo originó una "
            "persona o una política de reintento."
        ),
    )
    request_payload: Optional[dict] = Field(
        None, description="Cuerpo enviado a SIIGO en ese intento."
    )
    response_body: Optional[dict] = Field(None, description="Respuesta recibida de SIIGO.")

    model_config = {"from_attributes": True}


class DocumentFieldChangeResponse(BaseModel):
    """RF-05: una corrección manual sobre la causación."""

    id: int = Field(..., description="Identificador del cambio.")
    document_id: int = Field(..., description="Documento corregido.")
    entity: str = Field(
        ...,
        description="Qué se cambió: `document`, `document_detail` o `document_tax`.",
        examples=["document_detail"],
    )
    entity_id: Optional[int] = Field(None, description="Fila afectada, si no es el documento.")
    field: str = Field(..., description="Campo modificado.", examples=["code"])
    old_value: Optional[str] = Field(None, description="Valor anterior.", examples=["510505"])
    new_value: Optional[str] = Field(None, description="Valor nuevo.", examples=["510506"])
    changed_by: Optional[str] = Field(None, description="Quién lo cambió.")
    reason: Optional[str] = Field(
        None, description="Contexto del cambio.", examples=["error_correction"]
    )
    created_at: datetime = Field(..., description="Cuándo se hizo.")

    model_config = {"from_attributes": True}


class DocumentAccountingAuditResponse(BaseModel):
    """RF-05: historial completo de contabilización de un documento."""

    document_id: int = Field(..., description="Documento consultado.")
    attempts: list[AccountingAttemptResponse] = Field(
        default_factory=list, description="Intentos contra SIIGO, del más reciente al primero."
    )
    changes: list[DocumentFieldChangeResponse] = Field(
        default_factory=list, description="Correcciones manuales, de la más reciente a la primera."
    )


class DocumentExistenceCheckRequest(BaseModel):
    cufes: list[str] = Field(
        ...,
        max_length=500,
        description="CUFE (trackId de la DIAN) de los documentos por consultar.",
        examples=[["74b429c33cb0d4e881db7361..."]],
    )


class DocumentExistenceCheckResponse(BaseModel):
    existing: list[str] = Field(
        ...,
        description=(
            "CUFE, en minúsculas, de los documentos que ya están guardados con su PDF y su XML "
            "oficiales. Los que falten o estén incompletos no aparecen."
        ),
    )
