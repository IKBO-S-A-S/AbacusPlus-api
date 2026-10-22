"""RF-05: ejecutar UN intento de contabilización de un documento aprobado.

Este caso de uso es el dueño del ciclo de vida del documento durante la contabilización. El
siigo-service sabe hablar con SIIGO; aquí se decide **cuándo** se le habla y **qué** se hace
con el resultado.

El orden de las operaciones es la parte importante, y no es negociable:

    1. tomar el documento en exclusiva poniéndole el cerrojo (commit)
    2. mapear y validar
    3. llamar a SIIGO
    4. clasificar el desenlace, auditarlo y decidir si el cerrojo se abre

El paso 1 va antes que el 2 a propósito. Validar primero y bloquear después deja una ventana
en la que dos peticiones simultáneas pueden pasar ambas la validación. Bloquear primero
cuesta una escritura de más en los documentos que luego resultan inválidos, y a cambio
elimina la carrera por completo.

Qué cambió respecto a la versión anterior
------------------------------------------
El estado funcional del documento ya **no** se usa como cerrojo. Antes, un envío en curso
movía el documento a un sexto estado, «Contabilizando», que el contador veía en la tabla como
si fuera una etapa del ciclo contable. Ahora el cerrojo es `documents.accounting_locked` y
los estados siguen siendo cinco: un fallo, sea cual sea, deja el documento en **ERROR**.

Lo que distingue un fallo de otro es su *clasificación*, que se persiste junto al documento y
llega hasta el frontend como una acción recomendada —reintentar, editar y reintentar, o
verificar en SIIGO—. La protección contra la doble contabilización no se ha relajado en
ningún punto: sigue siendo el cerrojo el que impide reenviar un documento cuyo desenlace se
desconoce, solo que ahora es una columna en vez de un estado.

Este caso de uso ejecuta **un intento**. La política de cuántos intentos hacer, con cuánta
espera entre ellos y con cuánta concurrencia, vive en la cola y en el gestor de reintentos.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.domain.exceptions.base import EntityNotFoundException
from app.domain.services.payload_line_taxes import importe_declarado, impuestos_de_la_linea
from app.domain.services.siigo_error_classifier import (
    ErrorClassification,
    default_classifier,
)
from app.domain.services.tax_resolution import indice_por_porcentaje
from app.domain.services.total_verification import (
    total_de_la_respuesta,
    verificar_total_contabilizado,
)
from app.domain.value_objects.accounting_error import (
    ErrorClass,
    RecommendedAction,
    is_safe_to_resend,
)
from app.domain.value_objects.document_status import DocumentStatus
from app.domain.value_objects.retention_scope import (
    TIPOS_DE_RETENCION_EN_COMPRAS,
)
from app.infrastructure.clients.siigo_client import (
    ParametersUnavailableError,
    SiigoServiceClient,
)

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Falta configuración de la integración; el documento no tiene nada que corregir.

    Se separa de `ValueError` —que es «a este documento le falta un dato»— porque la acción
    que le corresponde al usuario es la contraria. Un documento sin cuenta PUC se arregla
    abriéndolo y completándolo; una plantilla de parámetros sin configurar se arregla en la
    pantalla de integraciones, y es la MISMA para todos los documentos.

    Confundirlas tiene un coste concreto y observable: sin esta distinción, un fallo de
    configuración le ofrecía al contador el botón «Editar documento», que no podía servir de
    nada porque no hay ningún dato del documento que cambiar. Se pasaba el rato buscando qué
    corregir en el sitio equivocado.
    """


@dataclass
class AccountingOutcome:
    """Resultado de contabilizar UN documento, pensado para mostrarse en la tabla."""

    document_id: int
    ok: bool
    status: int
    siigo_id: Optional[str] = None
    siigo_name: Optional[str] = None
    error: Optional[str] = None
    #: Clase técnica del fallo (`TRANSIENT`, `CORRECTABLE`, `UNCERTAIN`…). Se audita.
    error_class: Optional[str] = None
    #: Lo que el frontend interpreta para decidir qué botón ofrecer.
    recommended_action: Optional[str] = None
    #: Código de error de SIIGO, cuando lo hubo.
    error_code: Optional[str] = None
    #: True cuando el documento quedó con el cerrojo puesto y necesita verificarse en SIIGO.
    needs_reconciliation: bool = False
    #: True si la cola puede repetir este mismo envío sin que nadie corrija nada.
    auto_retryable: bool = False


@dataclass
class BatchAccountingOutcome:
    """Resumen de un lote. Los contadores son los que pide RF-05 para la barra de progreso."""

    total: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    results: list = field(default_factory=list)


class AccountDocumentUseCase:
    def __init__(
        self,
        document_repo,
        parameters_provider,
        siigo_client: SiigoServiceClient,
        knowledge_publisher=None,
        audit_repo=None,
        classifier=None,
    ):
        self.document_repo = document_repo
        self.parameters_provider = parameters_provider
        self.siigo_client = siigo_client
        #: RF-08. Opcional: si no se inyecta, la contabilización funciona igual y
        #: simplemente no se genera conocimiento (es lo que ocurre en los tests unitarios).
        self.knowledge_publisher = knowledge_publisher
        #: Auditoría. Opcional por el mismo motivo, pero en producción siempre se inyecta:
        #: sin ella se pierde la evidencia de qué se le envió a SIIGO y qué contestó.
        self.audit_repo = audit_repo
        self.classifier = classifier or default_classifier

    # ── Un documento ───────────────────────────────────────────────────────────

    def execute(
        self,
        document_id: int,
        force: bool = False,
        *,
        triggered_by: Optional[str] = None,
        job_id: Optional[int] = None,
        attempt: int = 1,
    ) -> AccountingOutcome:
        """Ejecuta un intento de contabilización. No lanza por errores de SIIGO: los reporta.

        Los fallos de SIIGO se devuelven en el resultado y no como excepción, porque en un
        lote un documento que falla no debe interrumpir a los demás. Las únicas excepciones
        que salen de aquí son las que impiden siquiera intentarlo (documento inexistente).

        `force` salta el cerrojo. Solo debe llegar en True desde la reconciliación, que es el
        único camino en el que alguien verificó contra SIIGO que la factura no existe. Nunca
        desde un lote ni desde un botón de reintento: en un documento bloqueado la factura
        pudo haberse creado, y el reenvío la duplicaría.
        """
        doc = self.document_repo.get_by_id(document_id)
        if doc is None:
            raise EntityNotFoundException("Document", str(document_id))

        # Se rechaza aquí lo que ya se sabe imposible, para dar un mensaje útil en vez de un
        # «no se pudo tomar el documento». La verificación que cuenta es la del claim.
        if doc.status == DocumentStatus.CONTABILIZADA:
            return AccountingOutcome(
                document_id=document_id,
                ok=False,
                status=doc.status,
                siigo_id=doc.siigo_id,
                error="El documento ya está contabilizado en SIIGO.",
            )
        if getattr(doc, "accounting_locked", False) and not force:
            return AccountingOutcome(
                document_id=document_id,
                ok=False,
                status=doc.status,
                error=(
                    "El documento tiene una contabilización en curso o interrumpida. "
                    "Verifique en SIIGO antes de reintentar."
                ),
                error_class=ErrorClass.UNCERTAIN,
                recommended_action=RecommendedAction.RECONCILE,
                needs_reconciliation=True,
            )

        # Cerrojo: a partir de aquí, ninguna otra petición puede tomar este documento.
        claimed = self.document_repo.claim_for_accounting(document_id, force=force)
        if claimed is None:
            # Otro proceso ganó la carrera, o el documento no estaba en un estado enviable.
            actual = self.document_repo.get_by_id(document_id)
            bloqueado = bool(getattr(actual, "accounting_locked", False))
            return AccountingOutcome(
                document_id=document_id,
                ok=False,
                status=actual.status if actual else DocumentStatus.ERROR,
                error=(
                    "El documento no está aprobado ni con error de contabilización, o ya "
                    "se está contabilizando. Actualice la vista para ver su estado real."
                ),
                error_class=ErrorClass.UNCERTAIN if bloqueado else None,
                recommended_action=(RecommendedAction.RECONCILE if bloqueado else None),
                needs_reconciliation=bloqueado,
            )

        inicio = datetime.now(timezone.utc)

        try:
            payload = self._build_payload(claimed)
        except ConfigurationError as exc:
            # Configuración de la integración, no del documento. SIIGO no se llegó a llamar,
            # así que reintentar es seguro en cuanto la configuración esté puesta — y por eso
            # se clasifica como CONFIG y no como corregible: el botón que ayuda aquí es
            # «reintentar», nunca «editar».
            clasificacion = ErrorClassification(
                error_class=ErrorClass.CONFIG,
                recommended_action=RecommendedAction.FIX_CONFIGURATION,
                message=str(exc),
            )
            self._registrar_intento(
                document_id=document_id,
                job_id=job_id,
                attempt=attempt,
                started_at=inicio,
                clasificacion=clasificacion,
                triggered_by=triggered_by,
                error_message=str(exc),
            )
            return self._fallo(document_id, clasificacion, error=str(exc))
        except ValueError as exc:
            # Falta información obligatoria y SIIGO no se llegó a llamar. Es la única certeza
            # absoluta de que no se creó nada, porque la petición no salió de aquí: el
            # documento queda en ERROR, con el cerrojo abierto y marcado como corregible.
            clasificacion = self.classifier.classify(message=str(exc), local_validation=True)
            self._registrar_intento(
                document_id=document_id,
                job_id=job_id,
                attempt=attempt,
                started_at=inicio,
                clasificacion=clasificacion,
                triggered_by=triggered_by,
                error_message=str(exc),
            )
            return self._fallo(document_id, clasificacion, error=str(exc))

        result = self.siigo_client.create_purchase_invoice(payload)

        if result.ok and result.siigo_id:
            # POST-CONDICIÓN: ¿lo que SIIGO contabilizó es lo que la factura dice?
            #
            # Ante `invalid_total_payments` el envío reenvía con la cifra que SIIGO calcula, y
            # SIIGO la calcula a partir de los ítems que nosotros mandamos. Si una línea está
            # mal extraída, esa cifra es coherente con el error y el reenvío la acepta: el
            # documento quedaba CONTABILIZADO, en verde, por un importe distinto al facturado.
            #
            # No se impide contabilizar —la factura ya existe en SIIGO y nada la deshace— pero
            # la diferencia deja de ser invisible.
            verificacion = verificar_total_contabilizado(
                total_de_la_respuesta(result.response_body),
                getattr(claimed, "total", None),
            )
            updated = self.document_repo.mark_accounted(
                document_id,
                result.siigo_id,
                result.siigo_name,
                siigo_total=verificacion.total_siigo,
                total_matches_dian=verificacion.coincide,
            )
            if verificacion.comprobado and not verificacion.coincide:
                logger.error(
                    "RF-05: documento %s contabilizado con DIFERENCIA de %.2f. %s",
                    document_id,
                    verificacion.diferencia,
                    verificacion.mensaje,
                )
            self._registrar_intento(
                document_id=document_id,
                job_id=job_id,
                attempt=attempt,
                started_at=inicio,
                clasificacion=None,
                triggered_by=triggered_by,
                request_payload=payload,
                response_body=result.response_body,
                http_status=result.status_code,
                ok=True,
                siigo_id=result.siigo_id,
                siigo_name=result.siigo_name,
            )
            # RF-08: este es el único punto del flujo normal en el que nace conocimiento.
            #
            # Va DESPUÉS de `mark_accounted` y no antes, para que la condición que autoriza
            # el aprendizaje —el estado CONTABILIZADO— esté ya persistida cuando el
            # publicador la comprueba. Y va con el `payload` que se acaba de enviar, que es
            # la causación final: la que el contador corrigió y SIIGO aceptó, no la que la
            # IA propuso al principio.
            self._publish_knowledge(document_id, payload, result.siigo_id, result.siigo_name)
            return AccountingOutcome(
                document_id=document_id,
                ok=True,
                status=updated.status if updated else DocumentStatus.CONTABILIZADA,
                siigo_id=result.siigo_id,
                siigo_name=result.siigo_name,
            )

        # Un solo sitio decide qué significa el fallo. El cliente trajo la evidencia; el
        # clasificador la interpreta; aquí solo se persiste la conclusión.
        clasificacion = self.classifier.classify(
            status_code=result.status_code,
            siigo_codes=result.siigo_codes,
            message=result.error or "",
            no_response=result.no_response,
            local_validation=result.local_validation,
        )
        self._registrar_intento(
            document_id=document_id,
            job_id=job_id,
            attempt=attempt,
            started_at=inicio,
            clasificacion=clasificacion,
            triggered_by=triggered_by,
            request_payload=payload,
            response_body=result.response_body,
            http_status=result.status_code,
            error_message=result.error,
        )
        return self._fallo(document_id, clasificacion, error=clasificacion.message)

    # ── Persistencia del desenlace ─────────────────────────────────────────────

    def _fallo(
        self, document_id: int, clasificacion: ErrorClassification, *, error: str
    ) -> AccountingOutcome:
        """Guarda el fallo clasificado y devuelve el resultado.

        La decisión de una sola línea que sostiene toda la seguridad contable es
        `release`: proviene de `is_safe_to_resend`, que solo dice que sí cuando la
        clasificación afirma que SIIGO no creó nada. Ante cualquier duda el cerrojo se queda
        puesto y el documento sale por la vía de la verificación, no por la del reintento.
        """
        seguro = is_safe_to_resend(clasificacion.recommended_action)
        self.document_repo.mark_accounting_failed(
            document_id,
            error or "Error desconocido",
            release=seguro,
            error_class=clasificacion.error_class,
            recommended_action=clasificacion.recommended_action,
            error_code=clasificacion.siigo_code,
        )
        return AccountingOutcome(
            document_id=document_id,
            ok=False,
            # El estado funcional es SIEMPRE ERROR. Ésta es la regla de RF-05: lo que
            # distingue un fallo de otro es la clasificación, no un estado nuevo.
            status=DocumentStatus.ERROR,
            error=error,
            error_class=clasificacion.error_class,
            recommended_action=clasificacion.recommended_action,
            error_code=clasificacion.siigo_code,
            needs_reconciliation=not seguro,
            auto_retryable=clasificacion.auto_retryable,
        )

    def _registrar_intento(self, **campos: Any) -> None:
        """Escribe la línea de auditoría del intento, sin que un fallo aquí afecte al envío.

        La auditoría es importante pero nunca es más importante que el desenlace del
        documento: si el historial no se puede escribir, se registra el problema y el flujo
        sigue. Perder una línea de historial es recuperable; dejar un documento en un estado
        incoherente porque la auditoría falló, no.
        """
        if self.audit_repo is None:
            return
        clasificacion: Optional[ErrorClassification] = campos.pop("clasificacion", None)
        if clasificacion is not None:
            campos.setdefault("error_message", clasificacion.message)
            campos["error_code"] = clasificacion.siigo_code
            campos["error_class"] = clasificacion.error_class
            campos["recommended_action"] = clasificacion.recommended_action
        try:
            self.audit_repo.record_attempt(**campos)
        except Exception:  # noqa: BLE001
            logger.exception(
                "RF-05: no se pudo auditar el intento del documento %s",
                campos.get("document_id"),
            )

    def _publish_knowledge(
        self,
        document_id: int,
        payload: dict,
        siigo_id: str,
        siigo_name: Optional[str],
    ) -> None:
        """RF-08: convierte la causación contabilizada en conocimiento del RAG.

        Aislado en su propio método y con el error contenido: la factura ya existe en SIIGO y
        el documento ya está cerrado, así que nada de lo que ocurra aquí puede cambiar el
        resultado que se le devuelve al usuario. Si el RAG no responde, se pierde un
        precedente —recuperable con el backfill—, no una contabilización.
        """
        if self.knowledge_publisher is None:
            return
        try:
            self.knowledge_publisher.publish(
                document_id=document_id,
                payload=payload,
                siigo_id=siigo_id,
                siigo_name=siigo_name,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "RF-08: fallo al generar el conocimiento del documento %s", document_id
            )

    # ── Lote ───────────────────────────────────────────────────────────────────

    def execute_batch(self, document_ids: list) -> BatchAccountingOutcome:
        """Contabiliza varios documentos en serie, dentro de la propia petición.

        **Se conserva para uso interno y para los tests**; el camino normal de la interfaz es
        ahora la cola (`AccountingQueueService`), que encola y responde de inmediato en lugar
        de mantener abierta una petición HTTP durante minutos.

        Un documento que falla no detiene el lote: se registra su resultado y se sigue. Es lo
        que evita que un solo proveedor mal configurado bloquee el cierre contable del mes.
        """
        outcome = BatchAccountingOutcome(total=len(document_ids))

        for document_id in document_ids:
            try:
                # Sin `force`: un lote nunca salta el cerrojo de un documento. Ese salto
                # exige una verificación individual contra SIIGO.
                result = self.execute(document_id, force=False)
            except EntityNotFoundException:
                outcome.skipped += 1
                outcome.results.append(
                    AccountingOutcome(
                        document_id=document_id,
                        ok=False,
                        status=DocumentStatus.ERROR,
                        error="El documento no existe.",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                # Un fallo inesperado en un documento no puede tumbar el lote entero. Se
                # registra con traza y se continúa con los demás.
                logger.exception("Fallo inesperado al contabilizar el documento %s", document_id)
                outcome.failed += 1
                outcome.results.append(
                    AccountingOutcome(
                        document_id=document_id,
                        ok=False,
                        status=DocumentStatus.ERROR,
                        error=f"Error inesperado: {exc}",
                        error_class=ErrorClass.UNKNOWN,
                        recommended_action=RecommendedAction.MANUAL_REVIEW,
                    )
                )
                continue

            outcome.results.append(result)
            if result.ok:
                outcome.successful += 1
            else:
                outcome.failed += 1

        return outcome

    # ── Mapeo del documento real al contrato de SIIGO ──────────────────────────

    def _build_payload(self, doc) -> dict[str, Any]:
        """Traduce el documento de la DIAN al cuerpo que espera siigo-service.

        Cada campo sale de un dato que ya existe en el sistema. Lo que no existe no se
        inventa: se detiene el envío con un mensaje que nombra el dato que falta.
        """
        try:
            parameters = self.parameters_provider()
        except ParametersUnavailableError as exc:
            # No es un dato que falte en el documento: es la integración que no responde o
            # que no está configurada. Va como ConfigurationError para que el usuario reciba
            # la acción que de verdad le sirve.
            raise ConfigurationError(str(exc)) from exc

        if not parameters:
            raise ConfigurationError(
                "No hay una plantilla de parámetros de factura de compra configurada para "
                "SIIGO. Configúrela en Integraciones (Parámetros de factura de compra): sin "
                "ella no se conoce el tipo de comprobante que SIIGO exige, y ese dato no "
                "viene en el documento de la DIAN. Es una configuración única para toda la "
                "empresa; una vez guardada, reintente la contabilización."
            )

        document_type_id = parameters.get("document_id")
        if not document_type_id:
            raise ConfigurationError(
                "La plantilla de parámetros de factura de compra no tiene definido el tipo "
                "de comprobante (document_id) que SIIGO exige. Complétela en Integraciones y "
                "reintente."
            )

        details = list(getattr(doc, "details", []) or [])
        if not details:
            raise ValueError("El documento no tiene líneas de detalle para contabilizar.")

        catalogo_iva = self._catalogo_de_impuestos_por_porcentaje()
        # id → tipo, para aplicar las reglas de SIIGO sobre qué impuestos pueden convivir en
        # un mismo ítem (hasta 3, uno por tipo, sin retenciones, sin IVA junto a AdValorem).
        tipos_de_impuesto = self._tipos_de_impuesto_por_id()
        warnings_impuestos: list[str] = []
        # Impuesto que la factura declara en sus líneas y que NO viaja a SIIGO por no tener
        # equivalente válido en el catálogo. Se lleva la cuenta porque contamina la base de
        # las retenciones: lo que no puede ir como impuesto acaba en la línea de ajuste, que
        # para SIIGO es base gravable. Ver la comprobación antes de armar el payload.
        impuestos_descartados = 0.0
        items = []
        sin_cuenta = []
        total_calculado = 0.0
        # Se llevan aparte porque son las bases de las retenciones: ReteICA se aplica sobre
        # el subtotal y ReteIVA sobre el IVA facturado (documentación de `invalid_retentions`).
        subtotal_bases = 0.0
        iva_facturado = 0.0
        for idx, detail in enumerate(details, start=1):
            code = (detail.code or "").strip()
            if not code:
                sin_cuenta.append(str(idx))
                continue
            cantidad = float(detail.quantity or 0)
            precio = self._precio_base(detail, cantidad)
            item: dict[str, Any] = {
                "type": detail.type or "Account",
                "code": code,
                "quantity": cantidad,
                "price": precio,
            }
            if detail.description:
                item["description"] = detail.description[:255]

            # TODOS los impuestos de la línea. Una línea puede llevar varios —IVA 19 % e
            # impuesto al consumo 4 % en las facturas de telecomunicaciones—, y contar solo
            # uno dejaba el total anticipado corto y perdía el otro impuesto.
            base = round(cantidad * precio, 2)
            tax_ids, impuesto_linea, avisos_linea = impuestos_de_la_linea(
                detail, catalogo_iva, tipos_de_impuesto, base=base
            )
            if tax_ids:
                item["tax_ids"] = tax_ids
            for aviso in avisos_linea:
                warnings_impuestos.append(f"Línea {idx}: {aviso}")
            descartado = round(importe_declarado(detail) - impuesto_linea, 2)
            if descartado > 0:
                impuestos_descartados += descartado

            # La fórmula es la que documenta SIIGO en `invalid_total_payments`:
            #     ValorBase = Redondear(Cantidad * ValorUnitario - Descuento, 2)
            #     Impuesto  = Redondear((ValorBase * Porcentaje) / 100, 2)
            #     TotalItem = Redondear(ValorBase + Impuesto, 2)
            #
            # `impuesto_linea` es la suma de TODOS los impuestos de la línea, tomada de los
            # importes que declara la DIAN cuando están disponibles.
            subtotal_bases += base
            # La base de la ReteIVA es el IVA facturado, no todos los impuestos: el impuesto
            # al consumo no es IVA y no se le practica ReteIVA.
            iva_facturado += self._iva_de_la_linea(detail, base)
            total_calculado += base + impuesto_linea
            items.append(item)

        if sin_cuenta:
            raise ValueError(
                "Las siguientes líneas no tienen cuenta contable ni producto asignado: "
                f"{', '.join(sin_cuenta)}. Asigne la cuenta PUC o el producto antes de "
                "contabilizar."
            )

        payment_id = doc.payment_type_id or parameters.get("default_payment_id")
        if not payment_id:
            raise ValueError(
                "El documento no tiene forma de pago asignada y la plantilla no define una "
                "por defecto. SIIGO la exige para generar la cuenta por pagar."
            )

        supplier = self._clean_nit(doc.issuer_nit)
        if not supplier:
            raise ValueError("El documento no tiene NIT del proveedor (emisor).")

        total_dian = float(doc.total or 0)
        faltante = round(total_dian - total_calculado, 2)

        # Subtotal de las líneas REALES del documento, antes de cualquier ajuste. Es la base
        # contra la que se contrasta la de una retención: la línea del impuesto al consumo la
        # añadimos nosotros y no forma parte de la base gravable que el contador registró.
        subtotal_de_las_lineas = subtotal_bases
        indices_de_lineas_reales = len(items)

        if faltante >= self.UMBRAL_DE_AJUSTE:
            # La factura vale más que la suma de sus líneas. Es el impuesto al consumo: la
            # DIAN lo reporta a nivel de documento y, en las facturas «BEC», sin ninguna
            # línea que lo represente. Contabilizar solo las líneas dejaría la deuda con el
            # proveedor por debajo de lo facturado —entre 192 y 506 pesos por documento—, así
            # que se añade como una línea propia y el total contabilizado pasa a ser
            # exactamente el total de la DIAN.
            cuenta = (
                parameters.get("inc_account_code")
                or self._cuenta_de_impuesto_al_consumo(details)
                or self.CUENTA_IMPUESTO_AL_CONSUMO
            )
            # Se nombra por lo que realmente es. El impuesto al consumo se reconoce porque
            # la diferencia coincide con los impuestos del documento que ninguna línea
            # declara (`total_taxes` menos el IVA de las líneas). Cuando no coincide, la
            # diferencia es el redondeo a peso de los totales de la DIAN, y llamarla
            # «impuesto al consumo» sería mentir en el asiento.
            es_impuesto_al_consumo = (
                abs(faltante - self._impuestos_no_desglosados(doc, details)) < 1.0
            )
            items.append(
                {
                    "type": "Account",
                    "code": str(cuenta),
                    "quantity": 1.0,
                    "price": faltante,
                    "description": (
                        "Impuesto al consumo"
                        if es_impuesto_al_consumo
                        else "Ajuste al total facturado"
                    ),
                }
            )
            total_calculado += faltante
            # El ajuste entra sin IVA, así que suma a la base y no al impuesto. Importa
            # porque el subtotal es la base sobre la que SIIGO calcula la ReteICA.
            subtotal_bases += faltante
            logger.info(
                "RF-05: documento %s — se añade el impuesto al consumo por %.2f en la cuenta "
                "%s; el total contabilizado iguala el total de la DIAN (%.2f).",
                getattr(doc, "id", "?"),
                faltante,
                cuenta,
                total_dian,
            )
        elif faltante <= -self.UMBRAL_DE_AJUSTE:
            # Las líneas valen MÁS que la factura. No se ajusta a la baja inventando un
            # descuento: es señal de un dato mal extraído, y taparlo lo haría indetectable.
            logger.warning(
                "RF-05: el documento %s declara %.2f pero sus líneas suman %.2f. Se envía por "
                "las líneas; revise la extracción del XML.",
                getattr(doc, "id", "?"),
                total_dian,
                total_calculado,
            )

        # RF-02: las retenciones, cada una a su sitio dentro del cuerpo.
        #
        # Se resuelven ANTES del payload porque el valor del pago depende de ellas: en una
        # factura de compra lo que queda por pagar al proveedor es el total menos lo retenido,
        # y es esa cifra la que SIIGO compara contra `payments`.
        retention_ids = self._retention_ids(doc)

        # ── El impuesto que no viajó no puede convertirse en base de una retención ────────
        #
        # SIIGO NO admite que se le envíe el valor de una retención: solo el id
        # (`retentions: [{"id": n}]`). La calcula él, sobre la base que deduce de los ítems.
        # Eso significa que la base de la ReteICA es la suma de las líneas que le mandamos,
        # y no una cifra que podamos corregir después.
        #
        # Cuando un impuesto de línea no encuentra equivalente en el catálogo, su importe se
        # traslada a la línea de ajuste para que el total cuadre. Esa línea es un `Account`,
        # así que para SIIGO ES BASE GRAVABLE. El resultado: se retiene ICA sobre un impuesto,
        # que no es ingreso del proveedor y no forma parte de la base del ICA.
        #
        # En BEC520526814 son 499 pesos de INC que engordan la base y hacen retener 4,82 de
        # más sobre dinero de un tercero. El total cuadra —y por eso el error es invisible—,
        # pero la retención es incorrecta.
        #
        # No se practica de más en silencio. Se detiene el envío nombrando el dato que falta,
        # que es además el único arreglo posible: crear ese impuesto en SIIGO.
        #
        # La comprobación es deliberadamente estrecha. Solo salta si se dan las DOS
        # condiciones, porque solo entonces el resultado es demostrablemente incorrecto:
        #   · quedó impuesto sin viajar, y
        #   · hay una retención que SIIGO calcula sobre el subtotal (ReteICA, Retefuente).
        # Una ReteIVA no se ve afectada —su base es el IVA, no el subtotal—, y un documento
        # sin retenciones tampoco: ahí la línea de ajuste solo cuadra el total y no distorsiona
        # nada. Esos siguen contabilizándose igual que antes.
        if impuestos_descartados >= self.UMBRAL_DE_AJUSTE and retention_ids:
            afectadas = self._retenciones_sobre_el_subtotal(retention_ids)
            if afectadas:
                raise ValueError(
                    f"Hay {impuestos_descartados:,.2f} de impuesto de línea que no puede "
                    "enviarse a SIIGO porque no existe en su catálogo de impuestos, y este "
                    f"documento lleva {afectadas}, que SIIGO calcula sobre el subtotal. Al no "
                    "poder viajar como impuesto, ese importe entraría como base gravable y la "
                    "retención saldría mayor de la que corresponde. Cree el impuesto que falta "
                    "en SIIGO, sincronice el catálogo de impuestos y reintente. Detalle: "
                    + " | ".join(warnings_impuestos)
                )

        # Los avisos que no llegan a detener el envío sí quedan registrados: antes se
        # recopilaban y no se leían en ninguna parte, así que un impuesto descartado no
        # dejaba rastro.
        for aviso in warnings_impuestos:
            logger.warning("RF-05: documento %s — %s", getattr(doc, "id", "?"), aviso)

        retenciones_de_item = self._retenciones_por_item(doc, subtotal_de_las_lineas)
        # Solo a las líneas del documento. La línea de ajuste del impuesto al consumo queda
        # fuera: es un impuesto, no una base gravable, y aplicarle retención cambiaría el
        # importe que el contador registró.
        self._aplicar_retenciones_a_los_items(items[:indices_de_lineas_reales], retenciones_de_item)

        total_retenido = self._retencion_que_aplicara_siigo(
            retention_ids, subtotal_bases, iva_facturado
        )
        # La retención de línea se calcula sobre el subtotal, que es la suma de las bases a
        # las que SIIGO le va a aplicar la tarifa línea por línea.
        total_retenido += self._retencion_que_aplicara_siigo(
            retenciones_de_item, subtotal_de_las_lineas, iva_facturado
        )

        payload: dict[str, Any] = {
            "document_id": document_type_id,
            "date": doc.date.isoformat(),
            "supplier_identification": supplier,
            "supplier_branch_office": parameters.get("supplier_branch_office") or 0,
            "items": items,
            "payment_id": payment_id,
            # El valor de la forma de pago debe coincidir EXACTAMENTE con el total que
            # SIIGO calcula a partir de los ítems, o responde
            # `The total payments must be equal to the total purchase`.
            #
            # Por eso no se envía `doc.total`, que es el total de la factura DIAN. Ambos
            # coinciden en la mayoría de documentos, pero no en todos: los de
            # telecomunicaciones traen impuestos —el consumo de voz— que el XML no desglosa
            # por línea, y ahí `doc.total` supera a la suma de las líneas. Enviar el total
            # de la DIAN hacía que SIIGO rechazara el documento entero.
            "payment_value": round(total_calculado - total_retenido, 2),
            # SIIGO exige `due_date` cuando el medio de pago maneja vencimiento, y los
            # típicos de compra a crédito («Crédito proveedores», «Otras cuentas por pagar»)
            # lo manejan. El documento de la DIAN no trae fecha de vencimiento, así que se
            # usa la del comprobante: SIIGO la ignora si el medio de pago no la necesita, y
            # sin ella rechazaría la factura con `parameter_required`.
            "payment_due_date": (
                parameters.get("default_payment_due_date") or doc.date.isoformat()
            ),
            "account_key": parameters.get("account_key") or "default",
        }

        # RF-07: centro de costo general. Prevalece el del documento sobre el de la plantilla.
        #
        # Es obligatorio o no según cómo esté configurado el tipo de comprobante en SIIGO
        # (el campo `cost_center` de /v1/document-types). Cuando lo exige y no se envía, SIIGO
        # responde `parameter_required`; se valida antes para no gastar una llamada ni sumar
        # al contador de errores que puede bloquear el usuario API.
        # RF-07: el centro de costo es opcional a nivel de documento. Si el documento no
        # trae uno, no se inventa aquí: quien decide si el comprobante lo exige, y cuál usar
        # por defecto, es la propia configuración de la empresa en SIIGO
        # (`cost_center_mandatory` y `cost_center_default` de /v1/document-types). El
        # siigo-service la consulta y la aplica; duplicar esa regla aquí solo crearía dos
        # verdades que pueden discrepar.
        cost_center = self._cost_center_siigo(doc.cost_center_id) or parameters.get("cost_center")
        if cost_center:
            payload["cost_center"] = cost_center

        if retention_ids:
            payload["retention_ids"] = retention_ids

        # Trazabilidad: el número del documento DIAN, para poder cruzarlo desde SIIGO.
        if doc.document_number:
            payload["provider_invoice_number"] = str(doc.document_number)
        prefix = parameters.get("provider_invoice_prefix")
        if prefix:
            payload["provider_invoice_prefix"] = prefix

        for clave in ("discount_type", "tax_included"):
            if parameters.get(clave) is not None:
                payload[clave] = parameters[clave]

        return payload

    @staticmethod
    def _impuestos_no_desglosados(doc, details) -> float:
        """Impuestos que el documento declara y que ninguna línea recoge.

        `documents.total_taxes` viene del `TaxTotal` del documento y lo incluye todo; la suma
        de `tax_value` de las líneas solo tiene el IVA. La diferencia es el impuesto al
        consumo, que la DIAN reporta a nivel de documento.
        """
        crudo = getattr(doc, "total_taxes", None)
        if crudo is None:
            # Sin el total de impuestos del documento no hay con qué comparar. Devolver la
            # resta daría un negativo que se leería como «sobran impuestos», que es lo
            # contrario de lo que ocurre.
            return 0.0
        try:
            total_impuestos = float(crudo or 0)
        except (TypeError, ValueError):
            return 0.0
        iva_de_lineas = 0.0
        for detail in details:
            try:
                iva_de_lineas += float(getattr(detail, "tax_value", 0) or 0)
            except (TypeError, ValueError):
                continue
        return round(total_impuestos - iva_de_lineas, 2)

    @staticmethod
    def _cuenta_de_impuesto_al_consumo(details) -> Optional[str]:
        """Cuenta que el proveedor ya usó para este impuesto en el propio documento.

        En las facturas «BEV» el impuesto al consumo sí viene como línea, con base cero y el
        importe en el precio. Si esa línea está, su cuenta es la mejor referencia posible:
        la eligió quien emitió la factura, no nosotros.
        """
        for detail in details:
            subtotal = getattr(detail, "subtotal", None) or 0
            precio = getattr(detail, "price", None) or 0
            descripcion = (getattr(detail, "description", "") or "").lower()
            if not subtotal and precio and "consumo" in descripcion:
                codigo = (getattr(detail, "code", "") or "").strip()
                if codigo:
                    return codigo
        return None

    @staticmethod
    def _precio_base(detail, cantidad: float) -> float:
        """Precio unitario **sin impuestos y ya neto de descuentos**.

        SIIGO calcula la base gravable como `quantity * price`, así que el precio que se le
        manda determina lo que se contabiliza. Enviar `detail.price` tal cual es incorrecto en
        dos casos frecuentes, y en ambos infla la base:

        - El precio viene **con el impuesto incluido**. «DONA RELL CHOC»: 2 x 3500 = 7000,
          pero la base gravable es 5882 y 7000 es el total con IVA.
        - La línea trae **descuento**. «SERVICIO DE ASEO EVENTUAL»: 4 x 129579.98 = 518319.92
          frente a una base real de 497587.12.

        `subtotal` es la base gravable que la propia DIAN reporta, ya neta de descuentos y sin
        impuestos: es exactamente lo que SIIGO debe recibir. Dividirlo entre la cantidad
        devuelve el unitario coherente, y `quantity * price` reconstruye la base exacta.

        Se recurre a `price` solo cuando no hay subtotal utilizable, para no alterar el
        comportamiento de los documentos que no traen ese dato.
        """
        subtotal = getattr(detail, "subtotal", None)
        try:
            subtotal = float(subtotal) if subtotal is not None else None
        except (TypeError, ValueError):
            subtotal = None

        if subtotal and cantidad:
            return round(subtotal / cantidad, 6)
        return float(getattr(detail, "price", 0) or 0)

    def _catalogo_de_impuestos_por_porcentaje(self) -> dict:
        """Porcentaje → id de SIIGO, para traducir el impuesto de cada línea.

        El XML de la DIAN trae el impuesto como un porcentaje («19.00», «5.00»), no como un
        identificador. Sin la traducción los ítems viajan sin impuestos, SIIGO suma solo las
        bases y responde `invalid_total_payments`.

        La regla de elección —preferir IVA, desempatar por el id menor— vive en
        `domain/services/tax_resolution.py` y la comparte con el parseo. Estaban duplicadas y
        no coincidían: la misma línea quedaba sin `tax_id` en la base y con impuesto en el
        envío.
        """
        db = getattr(self.document_repo, "db", None)
        if db is None:
            return {}
        try:
            filas = db.execute(
                text(
                    "SELECT id, type, percentage FROM integration_taxes "
                    "WHERE active = true ORDER BY id ASC"
                )
            ).fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("RF-05: no se pudo leer el catálogo de impuestos")
            return {}

        return indice_por_porcentaje(
            [{"id": fila[0], "type": fila[1], "percentage": fila[2]} for fila in filas]
        )

    def _impuesto_de_la_linea(self, detail, catalogo: dict) -> tuple:
        """(id de impuesto o None, porcentaje) de una línea.

        El porcentaje se devuelve siempre, tenga o no traducción, porque es lo que permite
        calcular el total que SIIGO va a esperar en el pago.

        Un porcentaje de cero no lleva impuesto: enviar un «IVA 0%» explícito no cambia el
        total y sí añade una referencia más que puede fallar, así que se omite.
        """
        try:
            crudo = getattr(detail, "tax_type", None)
            porcentaje = round(float(str(crudo if crudo is not None else "0").strip() or 0), 2)
        except (TypeError, ValueError):
            porcentaje = 0.0

        if getattr(detail, "tax_id", None):
            return detail.tax_id, porcentaje

        if porcentaje == 0:
            return None, 0.0

        tax_id = catalogo.get(porcentaje)
        if tax_id is None:
            logger.warning(
                "RF-05: ningún impuesto del catálogo tiene el %s%% que trae la línea '%s'. "
                "Se envía sin impuesto y el total puede no cuadrar.",
                porcentaje,
                (getattr(detail, "description", "") or "")[:40],
            )
            return None, 0.0
        return tax_id, porcentaje

    def _tipos_de_impuesto_por_id(self) -> dict:
        """id → tipo, en minúsculas, de todo el catálogo activo.

        Hace falta para aplicar las reglas de composición de SIIGO, que se expresan sobre el
        TIPO y no sobre el identificador: «un mismo tipo de impuesto más de una vez» es un
        rechazo, y el catálogo del cliente tiene cinco impuestos distintos al 19 %.
        """
        db = getattr(self.document_repo, "db", None)
        if db is None:
            return {}
        try:
            filas = db.execute(
                text("SELECT id, type FROM integration_taxes WHERE active = true")
            ).fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("RF-05: no se pudieron leer los tipos de impuesto del catálogo")
            return {}
        return {int(f[0]): str(f[1] or "").strip().lower() for f in filas}

    @staticmethod
    def _iva_de_la_linea(detail, base: float) -> float:
        """El IVA de la línea, que es la base sobre la que SIIGO calcula la ReteIVA.

        Se separa del resto de impuestos a propósito. La documentación de
        `invalid_retentions` dice que «ReteIVA: se aplica sobre el valor del IVA facturado»,
        y en una línea con IVA e impuesto al consumo el segundo no es IVA: incluirlo aquí
        retendría de más sobre una base que no corresponde.
        """
        lista = getattr(detail, "taxes", None)
        if lista:
            total = 0.0
            for impuesto in lista:
                if isinstance(impuesto, dict) and str(impuesto.get("esquema") or "") == "01":
                    total += float(impuesto.get("valor") or 0)
            return round(total, 2)

        # Documento anterior a que se conservara la lista: el porcentaje suelto es el del
        # impuesto principal, que en la práctica totalidad de esos documentos es el IVA.
        try:
            porcentaje = round(float(str(getattr(detail, "tax_type", "0") or "0").strip()), 2)
        except (TypeError, ValueError):
            return 0.0
        return round(base * porcentaje / 100.0, 2)

    def _cost_center_siigo(self, cost_center_id) -> Optional[int]:
        """Traduce el centro de costo local al identificador que SIIGO reconoce.

        Hay dos catálogos de centros de costo con claves propias: `cost_centers`, que es al
        que apuntan los documentos, e `integration_cost_centers`, que es el único que guarda
        el id de SIIGO en `external_id`. Enviar la clave local (5, 6, 7…) donde SIIGO espera
        la suya (735, 736, 769…) produce el mismo `The id doesn't exist` que producían las
        retenciones antes de corregir su sincronización.

        El puente es `code`, que ambos catálogos comparten porque los dos lo recibieron de
        SIIGO. Se traduce aquí, al construir el envío, y no migrando `documents.cost_center_id`
        a la clave de SIIGO, porque esa columna la escriben la interfaz y el enriquecimiento
        con la clave local: cambiarle el significado obligaría a tocar los dos y dejaría los
        documentos ya guardados apuntando a lo que no es.

        Cuando no hay traducción posible se devuelve el valor tal cual. Eso cubre el caso
        del documento que ya guarda el identificador de SIIGO —que no aparece en el catálogo
        local y no necesita traducirse— sin alterar lo que se venía enviando.
        """
        if not cost_center_id:
            return None

        db = getattr(self.document_repo, "db", None)
        if db is None:
            return cost_center_id

        try:
            fila = db.execute(
                text(
                    "SELECT i.external_id FROM cost_centers c "
                    "JOIN integration_cost_centers i ON i.code = c.code "
                    "WHERE c.id = :id AND i.external_id IS NOT NULL"
                ),
                {"id": int(cost_center_id)},
            ).first()
        except Exception:  # noqa: BLE001
            logger.exception(
                "RF-07: no se pudo traducir el centro de costo %s a su id de SIIGO",
                cost_center_id,
            )
            return cost_center_id

        if not fila or fila[0] is None:
            return cost_center_id

        try:
            return int(fila[0])
        except (TypeError, ValueError):
            return cost_center_id

    #: Diferencia mínima, en pesos, para añadir la línea del impuesto al consumo. Por debajo
    #: es redondeo del proveedor —los totales de la DIAN vienen redondeados a peso— y añadir
    #: una línea de céntimos ensuciaría la contabilidad sin corregir nada.
    UMBRAL_DE_AJUSTE = 1.0

    #: Cuenta por defecto del impuesto al consumo. Es la que el propio proveedor usa en las
    #: facturas «BEV», donde ese impuesto sí viene como línea. Se puede sustituir por
    #: `inc_account_code` en la plantilla de parámetros.
    CUENTA_IMPUESTO_AL_CONSUMO = "51159509"

    #: Dónde viaja cada retención dentro del cuerpo de `POST /v1/purchases`.
    #:
    #: SIIGO expone DOS sitios, y no son intercambiables:
    #:
    #: - `retentions` (raíz del documento). La tabla del endpoint lo dice literalmente:
    #:   «Array con los id de los impuestos tipo **ReteICA, ReteIVA**». Son las que se
    #:   calculan sobre magnitudes del documento entero —el subtotal y el IVA facturado—, así
    #:   que no tendrían sentido colgadas de una línea.
    #: - `items[].taxes` (cada línea). La documentación de errores acota qué NO cabe ahí:
    #:   «Si envías un reteIVA o reteICA en los items de factura». La retención en la fuente
    #:   no está en esa prohibición, y su tarifa depende del **concepto** de la operación
    #:   —honorarios 11 %, servicios 4 %, compras 2,5 %—, que es justamente lo que define la
    #:   línea con su cuenta contable.
    #:
    #: Mandar una Retefuente en `retentions` devuelve `invalid_array: The array id has
    #: invalid values` y rechaza la factura entera. Antes se descartaba por eso; ahora va al
    #: sitio que le corresponde en lugar de perderse.
    #: Se importa del dominio para que la interfaz y el envío no puedan divergir: si el
    #: selector ofrece una retención, es porque SIIGO puede practicarla.
    TIPOS_DE_RETENCION_ACEPTADOS = TIPOS_DE_RETENCION_EN_COMPRAS

    #: Retenciones que viajan dentro del ítem: NINGUNA. Comprobado contra el ambiente real.
    #:
    #: Se dedujo lo contrario a partir de la documentación —el enum `TaxType` incluye
    #: Retefuente, y la única prohibición escrita para los ítems es «Si envías un reteIVA o
    #: reteICA en los items de factura»—, así que por eliminación parecía su sitio. La API lo
    #: desmintió:
    #:
    #:     items[0].taxes → invalid_array: "The array taxes has invalid values"
    #:
    #: enviando Retefuente 1 %, Autorretención 1,10 % e Impoconsumo 8 % en la factura
    #: 941457814. Antes había respondido lo mismo al mandar la Retefuente en `retentions`.
    #:
    #: Con los dos sitios cerrados, la conclusión es que `POST /v1/purchases` NO recibe la
    #: retención en la fuente: SIIGO la practica por su propia configuración. `items[].taxes`
    #: sí acepta el IVA —así se contabilizan hoy todos los documentos—, de modo que admite
    #: impuestos de línea, no retenciones.
    #:
    #: La tupla se deja vacía en lugar de borrar el mecanismo: si SIIGO habilita algún tipo
    #: por ítem, basta declararlo aquí y el reparto vuelve a funcionar sin tocar nada más.
    TIPOS_DE_RETENCION_POR_ITEM: tuple = ()

    #: SIIGO admite «hasta 3 impuestos» por ítem y rechaza el mismo tipo repetido. Se respeta
    #: al componer la línea en lugar de dejar que lo descubra el servidor: un rechazo por este
    #: motivo tumba el documento completo.
    MAX_IMPUESTOS_POR_ITEM = 3

    def _retenciones_por_item(self, doc, subtotal_bases: float) -> list[int]:
        """Retenciones que viajan dentro de cada ítem, en `items[].taxes`.

        Hoy es la retención en la fuente. Se devuelve la lista de ids para añadirlos a TODAS
        las líneas, que es lo que reproduce el importe que el contador definió: SIIGO aplica
        la tarifa a la base de cada línea, y la suma de esas bases es el subtotal, que es la
        base sobre la que Abacus la calculó.

        Esa equivalencia es una condición, no una suposición: si el contador ajustó la base
        de la retención a algo distinto del subtotal, repartirla por líneas retendría sobre
        una base que él no eligió. En ese caso NO se envía y se deja constancia, porque
        alterar en silencio la base de una retención es peor que no practicarla.
        """
        candidatos = self._retenciones_con_valor(doc)
        if not candidatos:
            return []

        catalogo = self._retenciones_del_catalogo([tax_id for tax_id, _ in candidatos])
        if not catalogo:
            return []

        aceptados: list[int] = []
        for tax_id, base_declarada in candidatos:
            datos = catalogo.get(tax_id)
            if not datos:
                continue
            tipo, _ = datos
            if tipo not in self.TIPOS_DE_RETENCION_POR_ITEM:
                continue

            # La base debe ser el subtotal para que repartirla por líneas dé el mismo importe.
            if base_declarada and abs(base_declarada - subtotal_bases) >= 1.0:
                logger.warning(
                    "RF-02: la retención %s no se envía: su base (%.2f) no coincide con el "
                    "subtotal de las líneas (%.2f), y SIIGO la aplicaría sobre una base "
                    "distinta de la registrada.",
                    tax_id,
                    base_declarada,
                    subtotal_bases,
                )
                continue
            if tax_id not in aceptados:
                aceptados.append(tax_id)
        return aceptados

    def _aplicar_retenciones_a_los_items(self, items: list, retenciones: list) -> None:
        """Añade las retenciones de línea a `tax_ids`, respetando los límites de SIIGO.

        No se sustituye lo que ya lleva la línea —el IVA—: se suma. Y se respeta el tope de
        tres impuestos por ítem; si no caben, se deja constancia en vez de enviar una línea
        que SIIGO rechazaría y con ella el documento entero.
        """
        if not retenciones:
            return
        for indice, item in enumerate(items, start=1):
            actuales = list(item.get("tax_ids") or [])
            for tax_id in retenciones:
                if tax_id in actuales:
                    continue  # nunca dos veces el mismo impuesto en un ítem
                if len(actuales) >= self.MAX_IMPUESTOS_POR_ITEM:
                    logger.warning(
                        "RF-02: la línea %s ya lleva %s impuestos; la retención %s no cabe "
                        "y no se envía.",
                        indice,
                        len(actuales),
                        tax_id,
                    )
                    break
                actuales.append(tax_id)
            if actuales:
                item["tax_ids"] = actuales

    def _retenciones_con_valor(self, doc) -> list:
        """(tax_id, base gravable) de las retenciones que el contador dejó con valor.

        Una retención con valor cero no se practicó, así que no viaja. Se deduplica por
        identificador: dos filas del mismo impuesto son la misma retención.
        """
        vistos: list = []
        ids: set = set()
        for tax in getattr(doc, "taxes", []) or []:
            tax_id = getattr(tax, "tax_id", None)
            if not tax_id or tax_id in ids:
                continue
            try:
                valor = float(getattr(tax, "value", 0) or 0)
            except (TypeError, ValueError):
                continue
            if valor == 0:
                continue
            try:
                base = float(getattr(tax, "taxable_base", 0) or 0)
            except (TypeError, ValueError):
                base = 0.0
            ids.add(tax_id)
            vistos.append((tax_id, base))
        return vistos

    def _retention_ids(self, doc) -> list[int]:
        """Retenciones del documento que SIIGO acepta en `retentions`, sin duplicados.

        Se toman de `document_taxes`, que es donde RF-02 guarda lo que el contador confirmó,
        y se filtran por tipo. Solo se envían las que tienen valor: una retención con valor
        cero no se practicó.

        El filtro por tipo no es cosmético. En este cliente 19 de las 30 retenciones
        registradas son Retefuente, y cada una de ellas hacía que SIIGO rechazara el
        documento completo. Lo que se descarta aquí no se pierde: SIIGO lo calcula por su
        cuenta a partir de la configuración del proveedor.
        """
        candidatos: list[int] = []
        for tax in getattr(doc, "taxes", []) or []:
            tax_id = getattr(tax, "tax_id", None)
            if tax_id and float(getattr(tax, "value", 0) or 0) != 0 and tax_id not in candidatos:
                candidatos.append(tax_id)

        if not candidatos:
            return []

        tipos = self._tipos_de_impuesto(candidatos)
        if tipos is None:
            # No se pudo consultar el catálogo. Se mantiene el comportamiento anterior en
            # lugar de descartar en silencio: un fallo de infraestructura no debe cambiar
            # qué se contabiliza.
            return candidatos

        aceptados = []
        for tax_id in candidatos:
            tipo = (tipos.get(tax_id) or "").strip().lower()
            if tipo in self.TIPOS_DE_RETENCION_ACEPTADOS:
                aceptados.append(tax_id)
            else:
                # WARNING: el contador la registró y SIIGO no la va a practicar por esta
                # vía. `POST /v1/purchases` solo recibe ReteICA y ReteIVA, en `retentions`;
                # se comprobó que el resto es rechazado tanto ahí como en `items[].taxes`.
                logger.warning(
                    "RF-02: la retención %s (tipo %r) NO se envía a SIIGO: `POST "
                    "/v1/purchases` solo recibe ReteICA y ReteIVA. Si debe practicarse, "
                    "configúrela en SIIGO.",
                    tax_id,
                    tipo or "desconocido",
                )
        return aceptados

    #: Cómo se aplica cada tipo de retención, según la documentación de `invalid_retentions`:
    #: «ReteIVA: se aplica sobre el valor del IVA facturado. ReteICA: se aplica sobre el
    #: subtotal de la factura».
    #:
    #: El divisor no es el mismo para las dos. La tarifa de ReteICA se maneja **por mil**,
    #: que es como la publica el catálogo de impuestos de SIIGO: una ReteICA de «8.66» retiene
    #: el 0,866%, no el 8,66%. Se comprobó contra el ambiente real —SIIGO retuvo 370.68 sobre
    #: una base de 42804.00, que es exactamente 8.66/1000— y coincide con lo que ya advertía
    #: la propia interfaz al sugerir retenciones: el catálogo trae «11.04» donde la tabla de
    #: ReteICA trae «1.104». Confundir las unidades retiene diez veces de más o de menos.
    DIVISOR_POR_TIPO_DE_RETENCION = {
        "reteica": 1000.0,
        "reteiva": 100.0,
        "retefuente": 100.0,
    }

    #: Sobre qué magnitud aplica cada retención, según `invalid_retentions`: «ReteIVA: se
    #: aplica sobre el valor del IVA facturado. ReteICA: se aplica sobre el subtotal de la
    #: factura». La retención en la fuente también parte del subtotal: SIIGO la aplica a la
    #: base de cada línea, y la suma de esas bases es el subtotal.
    BASE_IVA = "iva"
    BASE_SUBTOTAL = "subtotal"
    BASE_POR_TIPO_DE_RETENCION = {
        "reteiva": BASE_IVA,
        "reteica": BASE_SUBTOTAL,
        "retefuente": BASE_SUBTOTAL,
    }

    def _retenciones_sobre_el_subtotal(self, retention_ids: list) -> str:
        """Nombre de las retenciones enviadas cuya base es el subtotal, o cadena vacía.

        Son las únicas a las que afecta que la línea de ajuste engorde la base: la ReteIVA se
        calcula sobre el IVA facturado y no la toca.
        """
        catalogo = self._retenciones_del_catalogo(retention_ids)
        if not catalogo:
            return ""
        tipos = [
            tipo
            for _, (tipo, _) in catalogo.items()
            if self.BASE_POR_TIPO_DE_RETENCION.get(tipo) == self.BASE_SUBTOTAL
        ]
        return ", ".join(sorted(set(tipos)))

    def _retencion_que_aplicara_siigo(
        self, retention_ids: list, subtotal: float, iva_facturado: float
    ) -> float:
        """Lo que SIIGO va a retener, para poder anticipar el valor del pago.

        En una factura de compra el pago es lo que queda por entregar al proveedor: el total
        menos lo retenido. SIIGO compara `payments` contra esa cifra, y si se le manda el
        bruto responde `invalid_total_payments` indicando el neto que esperaba.

        No se usan los valores que Abacus calculó en `document_taxes`: quien decide cuánto
        retiene es SIIGO, con la tarifa de **su** catálogo, y el pago debe cuadrar con esa
        decisión y no con la nuestra. Reproducir aquí su fórmula es lo que permite que
        coincidan; tomar nuestro valor volvería a descuadrar en cuanto las tarifas difieran.
        """
        if not retention_ids:
            return 0.0

        catalogo = self._retenciones_del_catalogo(retention_ids)
        if not catalogo:
            return 0.0

        total = 0.0
        for retencion_id in retention_ids:
            datos = catalogo.get(retencion_id)
            if not datos:
                continue
            tipo, porcentaje = datos
            divisor = self.DIVISOR_POR_TIPO_DE_RETENCION.get(tipo)
            if divisor is None:
                continue
            base = (
                iva_facturado
                if self.BASE_POR_TIPO_DE_RETENCION.get(tipo) == self.BASE_IVA
                else subtotal
            )
            valor = round(base * porcentaje / divisor, 2)
            total += valor
            logger.info(
                "RF-02: SIIGO retendrá %.2f por la retención %s (%s, tarifa %s sobre una "
                "base de %.2f).",
                valor,
                retencion_id,
                tipo,
                porcentaje,
                base,
            )
        return round(total, 2)

    def _retenciones_del_catalogo(self, tax_ids: list) -> dict:
        """(tipo en minúsculas, porcentaje) de cada retención, desde el catálogo local."""
        db = getattr(self.document_repo, "db", None)
        if db is None or not tax_ids:
            return {}
        try:
            filas = db.execute(
                text("SELECT id, type, percentage FROM integration_taxes WHERE id = ANY(:ids)"),
                {"ids": list(tax_ids)},
            ).fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("RF-02: no se pudieron leer las tarifas de retención")
            return {}

        catalogo = {}
        for id_impuesto, tipo, porcentaje in filas:
            try:
                catalogo[id_impuesto] = (
                    str(tipo or "").strip().lower(),
                    float(porcentaje or 0),
                )
            except (TypeError, ValueError):
                continue
        return catalogo

    def _tipos_de_impuesto(self, tax_ids: list[int]) -> Optional[dict[int, str]]:
        """Tipo de cada impuesto según el catálogo, o None si no se puede consultar."""
        db = getattr(self.document_repo, "db", None)
        if db is None:
            return None
        try:
            filas = db.execute(
                text("SELECT id, type FROM integration_taxes WHERE id = ANY(:ids)"),
                {"ids": list(tax_ids)},
            ).fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("RF-02: no se pudieron leer los tipos de impuesto %s", tax_ids)
            return None
        return {fila[0]: fila[1] for fila in filas}

    @staticmethod
    def _clean_nit(nit: Optional[str]) -> str:
        """Devuelve el NIT sin dígito de verificación ni separadores.

        SIIGO identifica al tercero por el número, no por el NIT con DV. Los documentos de la
        DIAN traen ambas formas según el emisor ('900123456' y '900123456-7'), así que se
        normaliza aquí en lugar de confiar en cómo venga.
        """
        if not nit:
            return ""
        limpio = str(nit).strip().replace(".", "").replace(" ", "")
        if "-" in limpio:
            limpio = limpio.split("-", 1)[0]
        return limpio


def build_siigo_service_client(
    bearer_token: str = "", tenant_slug: Optional[str] = None
) -> SiigoServiceClient:
    """Construye el cliente con la URL del siigo-service.

    Con `tenant_slug` el cliente habla por las rutas internas, que es lo que necesitan los
    workers de la cola: no tienen token de usuario y no deben depender de uno.
    """
    url = os.getenv("SIIGO_SERVICE_URL", "http://siigo-service:8006")
    return SiigoServiceClient(base_url=url, bearer_token=bearer_token, tenant_slug=tenant_slug)
