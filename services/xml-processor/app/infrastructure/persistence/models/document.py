from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON, Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.domain.services.line_taxes import (
    ESQUEMA_IVA,
    ESQUEMAS_DE_CONSUMO,
    desglose_de_impuesto,
)
from app.infrastructure.config.database import Base

#: JSON portable: JSONB en PostgreSQL (producción) y JSON en SQLite (pruebas). Es el mismo
#: patrón que ya usa `accounting.py` para el cuerpo de las peticiones a SIIGO.
_JSON = JSON().with_variant(JSONB(), "postgresql")


class DocumentStatus(Base):
    __tablename__ = "document_statuses"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False, unique=True)


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    document_name = Column(String(255), nullable=False)
    # `document_number` es la clave con la que el ingestor comprueba si una factura ya se
    # procesó: se consulta una vez por cada XML del ZIP. Sin índice, cada comprobación era un
    # recorrido completo de la tabla, y el coste de importar un lote crecía con el cuadrado
    # del histórico acumulado.
    document_number = Column(String(50), nullable=False, index=True)
    # `date` es el filtro de la pantalla principal y del backfill de S3; `status`, el filtro
    # que acompaña casi siempre. El índice compuesto de más abajo cubre la pareja.
    date = Column(Date, nullable=False, index=True)
    hour = Column(String(20), nullable=False)
    currency = Column(String(3), nullable=False)
    document_type = Column(String(50), nullable=False)
    uuid = Column(String(255), nullable=False)
    cufe = Column(String(512), nullable=True)
    issuer_name = Column(String(255), nullable=False)
    issuer_nit = Column(String(50), nullable=False)
    issuer_phone = Column(String(100))
    issuer_email = Column(String(255))
    receiver_name = Column(String(255), nullable=False)
    receiver_nit = Column(String(50), nullable=False)
    receiver_phone = Column(String(100))
    receiver_email = Column(String(255))
    # Responsabilidades fiscales del RECEPTOR (comprador) tomadas del RUT vía la factura
    # (cbc:TaxLevelCode). Definen si el comprador es agente de retención: si no lo es, no se
    # practica retención alguna, sin importar el vendedor. Códigos separados por ';'.
    receiver_responsibilities = Column(String(100), nullable=True)
    subtotal = Column(Float, nullable=False)
    total_taxes = Column(Float, nullable=False)
    retefuente = Column(Float, nullable=True, default=0.0)
    reteica = Column(Float, nullable=True, default=0.0)
    total = Column(Float, nullable=False)
    register_at = Column(DateTime, default=datetime.now(timezone.utc))
    status = Column(Integer, ForeignKey("document_statuses.id"), nullable=False, default=100)
    payment_type_id = Column(Integer, nullable=True)
    # RF-07: centro de costo A NIVEL DE DOCUMENTO. La API de factura de compra de SIIGO
    # (`POST /v1/purchases`) solo admite `cost_center` en la raíz del documento, no por línea
    # (ahí `items[]` no tiene ese campo). Por eso el centro de costo aplicable a la
    # contabilización vive aquí, en el documento, y no en cada detalle. La columna
    # `document_details.cost_center_id` se conserva tal como venía, pero no gobierna la
    # contabilización en compra.
    cost_center_id = Column(Integer, nullable=True)
    # Representación gráfica oficial descargada de la DIAN (durante la sesión autenticada de
    # "Procesar Documentos"). Se guarda para poder visualizarla luego sin volver a la DIAN.
    # Null si aún no se ha descargado; en ese caso se genera una representación gráfica local.
    pdf_data = Column(LargeBinary, nullable=True)
    pdf_source = Column(String(20), nullable=True)  # 'dian_official' | None
    # RF-03: enlace público/temporal del PDF subido a Amazon S3 (vía el Lambda del cliente).
    # Si está presente, la interfaz renderiza este enlace; si no, se sirve el PDF por bytes.
    pdf_url = Column(String(1024), nullable=True)
    # RF-03 (opcional XML): XML oficial del documento (dentro del ZIP de la DIAN) y su enlace S3.
    xml_data = Column(LargeBinary, nullable=True)
    xml_url = Column(String(1024), nullable=True)

    # RF-05: resultado de la contabilización en SIIGO.
    #
    # `siigo_id` es el identificador que devuelve `POST /v1/purchases` y es la única prueba
    # de que la factura de compra existe realmente en SIIGO. Va con índice único parcial
    # (ver tenant_migrations) para que la base de datos, y no solo el código, impida que dos
    # documentos queden apuntando a la misma factura en SIIGO.
    siigo_id = Column(String(120), nullable=True)
    # Consecutivo/número que SIIGO asigna al comprobante. Se guarda junto al id porque es lo
    # que el contador ve en SIIGO Nube; el id es un GUID que no le sirve para buscar.
    siigo_name = Column(String(120), nullable=True)
    # El total que SIIGO informa al aceptar la factura, y si coincide con el de la DIAN.
    #
    # Vive aquí y no solo dentro de `accounting_attempts.response_body` porque es un HECHO
    # del documento, no la evidencia de un intento: un documento cerrado por reconciliación
    # no registra intento alguno y se quedaba sin él.
    #
    # `siigo_total_matches_dian` en False señala un documento contabilizado por un importe
    # distinto al facturado. No impide nada —la factura ya existe en SIIGO— pero deja de ser
    # invisible, que era el verdadero problema.
    # Retenciones que el PROVEEDOR declaró en el XML (`cac:WithholdingTaxTotal`).
    #
    # Señal de contraste, no fuente de verdad. Se guardan enteras —incluido el esquema 08,
    # que antes se descartaba— porque son el único segundo par de ojos disponible: SIIGO no
    # devuelve qué retenciones practicó en una compra.
    xml_withholdings = Column(_JSON, nullable=True)
    siigo_total = Column(Numeric(18, 2), nullable=True)
    siigo_total_matches_dian = Column(Boolean, nullable=True)
    accounted_at = Column(DateTime(timezone=True), nullable=True)
    # RF-06: último error devuelto por SIIGO o por la validación previa, asociado al
    # documento para que el contador pueda corregir y reintentar.
    accounting_error = Column(String, nullable=True)
    # Marca de cuándo se tomó el cerrojo. Permite distinguir un envío en curso legítimo de un
    # documento que quedó colgado por una caída, sin reintentar solo.
    accounting_started_at = Column(DateTime(timezone=True), nullable=True)

    # RF-05: clasificación del último fallo. El estado funcional sigue siendo ERROR; estas
    # dos columnas son las que dicen QUÉ clase de error fue y QUÉ puede hacer el usuario.
    #
    # Se guardan desnormalizadas en `documents`, además de en el historial de intentos,
    # porque la tabla de documentos se lista completa en cada carga de la vista y resolver la
    # acción con un JOIN contra el último intento de cada documento costaría una subconsulta
    # correlacionada por fila. Aquí es un dato derivado y de solo lectura: la fuente de verdad
    # sigue siendo `accounting_attempts`.
    accounting_error_class = Column(String(30), nullable=True)
    accounting_recommended_action = Column(String(40), nullable=True)
    # Código de error de SIIGO (`invalid_reference`, `requests_limit`, …). Se conserva porque
    # es lo que permite afinar la tabla de clasificación con datos reales de producción.
    accounting_error_code = Column(String(60), nullable=True)

    # RF-05: cerrojo de contabilización. Sustituye al antiguo estado «Contabilizando».
    #
    # True significa «hay un envío en curso, o hubo uno cuyo desenlace no se conoce». En
    # ambos casos el documento NO puede volver a enviarse a SIIGO: en el primero porque ya
    # hay una petición viva, y en el segundo porque la factura pudo haberse creado y
    # /v1/purchases no admite `Idempotency-Key` para impedir el duplicado.
    #
    # Es una columna y no un estado a propósito: es información interna de la cola, no una
    # etapa del ciclo de vida contable. El contador ve el documento en ERROR con la acción
    # «Verificar en SIIGO»; el cerrojo es lo que hace que esa recomendación sea obligatoria y
    # no un consejo.
    #
    # Solo se abre por dos caminos: una respuesta concluyente de SIIGO, o una reconciliación
    # con verificación humana. Nunca por tiempo, y nunca solo.
    accounting_locked = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    # Número de intentos de contabilización acumulados. Vive en el documento además de en la
    # cola para que la vista pueda mostrar «intento 3 de 5» sin consultar la cola.
    accounting_attempts = Column(Integer, nullable=False, server_default=text("0"), default=0)
    pdf_storage_locations = Column(JSON, nullable=False, default=dict)
    xml_storage_locations = Column(JSON, nullable=False, default=dict)

    # Relación con DocumentDetail
    details = relationship("DocumentDetail", back_populates="document")

    @property
    def total_consumption_tax(self) -> float:
        """Impuesto al consumo del documento, sumando el de todas sus líneas.

        No hay columna que lo guarde y no hace falta: el INC vive en la lista de impuestos de
        cada línea, y `total_taxes` ya lo incluye mezclado con el IVA. Lo que faltaba era
        poder verlo APARTE, que es lo que el contador necesita para cuadrar.

        Se calcula sobre `details`, así que quien liste documentos debe cargarlas de forma
        anticipada o pagará una consulta por documento. `get_by_date_range` lo hace.
        """
        return round(sum(d.consumption_tax for d in self.details or []), 2)

    # Relación con DocumentTax
    taxes = relationship("DocumentTax", back_populates="document")

    __table_args__ = (
        # La consulta que más se ejecuta en el sistema es «documentos entre dos fechas, con
        # este estado, del más reciente al más antiguo». Un índice compuesto en ese mismo
        # orden la resuelve sin ordenar después.
        Index("ix_documents_date_status", "date", "status"),
        # RF-05: los workers de la cola buscan los documentos con el cerrojo puesto. Es un
        # índice parcial porque la inmensa mayoría de filas tiene `false` e indexarlas no
        # aporta nada.
        Index(
            "ix_documents_accounting_locked",
            "accounting_locked",
            postgresql_where=text("accounting_locked"),
        ),
    )


class DocumentDetail(Base):
    __tablename__ = "document_details"

    id = Column(Integer, primary_key=True, index=True)
    # PostgreSQL no crea índice para las claves foráneas. Cada vez que se abre el detalle de
    # un documento se buscan sus líneas por aquí, y sin índice eso era un recorrido de toda la
    # tabla de líneas — la más grande del esquema, con varias filas por factura.
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    description = Column(String(255), nullable=False)
    concept_description_id = Column(Integer, ForeignKey("concept_descriptions.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String(20), nullable=False)
    price = Column(Float, nullable=False)
    subtotal = Column(Float, nullable=False)
    # El impuesto PRINCIPAL de la línea: el de mayor importe. Se conserva escalar porque lo
    # leen la interfaz y el RAG, y porque describe la naturaleza de la línea.
    tax_type = Column(String(50), nullable=False)
    tax_value = Column(Float, nullable=False)
    # TODOS los impuestos de la línea, tal como los declara la DIAN.
    #
    # Una línea puede llevar varios: ocho facturas de telecomunicaciones del cliente declaran
    # IVA 19 % e impuesto al consumo 4 % en el mismo renglón. Conservar solo el primero perdía
    # $7.363,44 repartidos en 19 documentos, y la línea de ajuste que cuadraba el total
    # después hacía la pérdida invisible.
    #
    # Va como JSON y no como tabla hija a propósito: la lista es corta, siempre se lee entera
    # junto a su línea, y así no hay join que mantener ni filas huérfanas que perseguir.
    taxes = Column(_JSON, nullable=True)
    total = Column(Float, nullable=False)

    # ── Desglose de IVA e INC ────────────────────────────────────────────────
    #
    # `tax_type` y `tax_value` guardan el impuesto PRINCIPAL de la línea —el de mayor
    # importe—, no el IVA. La interfaz los pintaba bajo el rótulo «Tipo IVA», y eso es cierto
    # en la mayoría de líneas pero falso en dos casos que sí existen en los datos del cliente:
    #
    #   · Línea con SOLO INC (3 líneas): `tax_type` es la tarifa del INC —8 %— y se mostraba
    #     como si fuera IVA. El impuesto se leía con el nombre del que no era.
    #   · Línea con IVA e INC (9 líneas): `tax_type` es el IVA y el INC quedaba sin tarifa
    #     visible por ninguna parte.
    #
    # Estas cuatro propiedades leen la lista completa y separan cada impuesto con SU tarifa y
    # SU importe. No sustituyen a `tax_type`/`tax_value`, que siguen intactos porque los usan
    # la contabilización y el RAG: se añaden al lado.

    @property
    def _iva(self) -> tuple:
        return desglose_de_impuesto(self.taxes, {ESQUEMA_IVA})

    @property
    def _inc(self) -> tuple:
        return desglose_de_impuesto(self.taxes, ESQUEMAS_DE_CONSUMO)

    @property
    def iva_percentage(self):
        """Tarifa del IVA de la línea, o None si no lleva IVA.

        Cuando la línea no tiene lista de impuestos —documentos anteriores a que se
        conservaran todos— se recurre a `tax_type`. No es una suposición gratuita: en esos
        documentos ese campo ERA el impuesto de la línea y así se ha mostrado siempre. Lo que
        no se puede hacer con ellos es distinguir IVA de INC, porque el dato para hacerlo no
        se guardó; por eso el respaldo alimenta el IVA y nunca el INC, que se deja vacío en
        lugar de inventarlo.
        """
        if self.taxes:
            return self._iva[0]
        try:
            tarifa = float(str(self.tax_type or "0").strip())
        except (TypeError, ValueError):
            return None
        return tarifa or None

    @property
    def iva_value(self) -> float:
        if self.taxes:
            return self._iva[1]
        return round(float(self.tax_value or 0), 2)

    @property
    def inc_percentage(self):
        """Tarifa del INC, o None. Sin lista de impuestos no se puede afirmar que haya INC."""
        return self._inc[0] if self.taxes else None

    @property
    def inc_value(self) -> float:
        return self._inc[1] if self.taxes else 0.0

    @property
    def consumption_tax(self) -> float:
        """Alias de `inc_value`. Lo consume `Document.total_consumption_tax`."""
        return self.inc_value

    code = Column(String(50), nullable=True)
    type = Column(String(20), nullable=False, default="Account")
    tax_id = Column(Integer, nullable=True)
    cost_center_id = Column(Integer, nullable=True)
    # RF-04: los tres estados posibles de la cuenta de una línea se derivan de este par.
    #   · valor original      → code_source IS NULL (el XML de la DIAN no trae cuenta PUC)
    #   · sugerido por el LLM → code_source = "llm";    code == code_suggested
    #   · editado a mano      → code_source = "manual"; code != code_suggested
    # `code_suggested` conserva la última propuesta del modelo incluso después de que el
    # contador la sobrescriba, para poder mostrarla y detectar el cambio manual.
    code_source = Column(String(10), nullable=True)
    code_suggested = Column(String(50), nullable=True)

    # Relación con Document
    document = relationship("Document", back_populates="details")
    # Relación con ConceptDescription
    concept_description = relationship("ConceptDescription", back_populates="details")
