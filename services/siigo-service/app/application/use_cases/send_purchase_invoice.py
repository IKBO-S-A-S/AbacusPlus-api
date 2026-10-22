"""RF-05: creación de la factura de compra en SIIGO.

Por qué `POST /v1/purchases` y no `POST /v1/journals`
-----------------------------------------------------
Los dos endpoints permiten imputar contra cuentas contables, así que la elección no es obvia
y se resolvió contra el contrato oficial, campo por campo:

- El alcance del proyecto establece que «el tipo de pago es utilizado por SIIGO para la
  generación de la cuenta por cobrar». En /v1/purchases `payments` es obligatorio; en
  /v1/journals ese bloque no existe.
- RF-02 administra retenciones **a nivel de documento**. /v1/purchases expone `retentions`
  como array de ids en la raíz; /v1/journals no tiene equivalente.
- RF-07 pide un centro de costo general para todo el documento. /v1/purchases expone
  `cost_center` en la raíz; en /v1/journals solo existe `items[].cost_center`.
- El detalle se maneja «únicamente a nivel de cuenta», que /v1/purchases cubre con
  `items[].type = "Account"`.

Además, /v1/journals exigiría construir el asiento cuadrado débito/crédito, que es
justamente lo que el proyecto delega en SIIGO.

Lo que este caso de uso NO hace
-------------------------------
No cambia el estado del documento ni lo persiste: eso pertenece al xml-processor, que es el
dueño del documento. Aquí solo se valida, se construye el JSON, se envía y se valida la
respuesta. Esa separación es la que permite que el estado «Contabilizando» se confirme en la
base ANTES de que esta llamada ocurra.
"""

import logging
import os
import re
from decimal import Decimal
from typing import Any, Optional

from app.application.dto.purchase_invoice import (
    PurchaseInvoiceItem,
    SendPurchaseInvoiceRequest,
    SendPurchaseInvoiceResponse,
)
from app.application.use_cases.manage_credentials import ManageCredentialsUseCase
from app.domain.exceptions.base import SiigoApiException, ValidationException
from app.infrastructure.siigo.siigo_client import SiigoApiClient

logger = logging.getLogger(__name__)

_PURCHASES_PATH = "/v1/purchases"

#: SIIGO solo admite estos tres tipos de ítem.
_VALID_ITEM_TYPES = ("Product", "FixedAsset", "Account")
#: SIIGO solo admite estos dos tipos de descuento.
_VALID_DISCOUNT_TYPES = ("Percentage", "Value")


class SendPurchaseInvoiceUseCase:
    def __init__(self, credentials: ManageCredentialsUseCase):
        self.credentials = credentials

    def execute(self, request: SendPurchaseInvoiceRequest) -> SendPurchaseInvoiceResponse:
        # El orden importa: se valida ANTES de autenticar y de llamar a SIIGO. Enviar un
        # documento incompleto para que SIIGO lo rechace gasta cupo del límite de 100
        # peticiones por minuto y, sostenido, cuenta para el bloqueo por proporción de
        # errores que SIIGO aplica cuando superan el 80% durante 7 días.
        self._validate(request)

        credential = self.credentials.ensure_token(request.account_key)
        client = SiigoApiClient(credential)

        # La configuración del comprobante la manda SIIGO, no una copia local: si el
        # contador cambia allí si el centro de costo es obligatorio, Abacus lo respeta sin
        # que nadie tenga que actualizar nada aquí.
        tipo_comprobante = client.get_document_type(request.document_id)

        # Un comprobante de numeración manual obliga a enviar el consecutivo, y ese dato
        # caduca en cuanto se emite cualquier documento —desde Abacus o desde el propio
        # SIIGO—. Se relee sin caché justo antes de construir el cuerpo: es una petición más
        # por documento, a cambio de no arriesgar un número repetido.
        if tipo_comprobante and tipo_comprobante.get("automatic_number") is False:
            tipo_comprobante = (
                client.get_document_type(request.document_id, refresh=True) or tipo_comprobante
            )

        self._validate_cost_center(request, tipo_comprobante)

        payload = self._build_payload(request, tipo_comprobante)

        try:
            raw = client.post_document(_PURCHASES_PATH, payload)
        except SiigoApiException as exc:
            total_esperado = self._total_que_espera_siigo(exc)
            if total_esperado is None:
                raise
            # SIIGO acaba de decir, con su propio cálculo, cuánto debía valer el pago. Se
            # reenvía una única vez con esa cifra.
            #
            # Es preferible a afinar indefinidamente nuestra réplica de su fórmula: el total
            # depende de cómo esté configurada la empresa —qué retenciones practica el
            # comprobante, con qué tarifas, sobre qué topes— y eso cambia sin avisar. Aquí la
            # cifra la pone SIIGO, así que sigue cuadrando aunque la configuración cambie.
            #
            # Reenviar es seguro precisamente en este error y no en otros: `invalid_total_payments`
            # es un rechazo de validación, anterior a cualquier escritura, de modo que no hay
            # comprobante creado que se pueda duplicar. Se hace UNA sola vez: si el segundo
            # intento también descuadra, el problema no es la cifra y el error debe salir.
            anterior = payload["payments"][0].get("value")
            logger.info(
                "SIIGO esperaba un total de %s y se había enviado %s; se reenvía con el "
                "valor que SIIGO calculó.",
                total_esperado,
                anterior,
            )
            payload["payments"][0]["value"] = total_esperado
            raw = client.post_document(_PURCHASES_PATH, payload)

        return self._build_response(
            raw, supports_consumption_tax=self.admite_impuesto_al_consumo(tipo_comprobante)
        )

    #: SIIGO devuelve el total que esperaba dentro del mensaje de `invalid_total_payments`:
    #: «The total payments must be equal to the total purchase. The total purchase calculated
    #: is 50529.32».
    _TOTAL_ESPERADO = re.compile(
        r"total\s+purchase\s+calculated\s+is\s*:?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
    )

    @classmethod
    def _total_que_espera_siigo(cls, exc: SiigoApiException) -> Optional[float]:
        """El total que SIIGO dice esperar, o None si este error no es un descuadre.

        Se exige que el error sea `invalid_total_payments` **y** que traiga la cifra. Un
        reenvío automático solo es admisible cuando consta que SIIGO no creó nada, y ese
        código es el único que lo garantiza aquí.
        """
        mensaje = str(getattr(exc, "message", "") or exc)
        if "invalid_total_payments" not in mensaje:
            return None
        encontrado = cls._TOTAL_ESPERADO.search(mensaje)
        if not encontrado:
            return None
        try:
            return round(float(encontrado.group(1)), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def admite_impuesto_al_consumo(tipo_comprobante: Optional[dict]) -> Optional[bool]:
        """Si el comprobante de compra maneja impuesto al consumo, según SIIGO.

        `GET /v1/document-types?type=FC` lo declara en `consumption_tax`: «Indica si el
        documento maneja impuesto al consumo». Es la misma fuente de la que ya se leen
        `cost_center_mandatory` y `automatic_number`.

        Importa porque hay dos formas de contabilizar ese impuesto y no son equivalentes:

        - **Nativa** — el identificador del impuesto en `items[].taxes`. SIIGO lo registra
          como impuesto, con su naturaleza contable correcta. Comprobado en F78P21635.
        - **Línea de ajuste** — una línea extra por la diferencia. Necesaria cuando el XML no
          desglosa el impuesto por línea, pero registra como «ajuste» algo que es un impuesto.

        Devuelve **None cuando no se pudo consultar**, y eso no es lo mismo que False. Es la
        misma postura que ya se aplica al centro de costo: sin catálogo no se inventa una
        regla. Tratar un fallo de red como «no lo admite» haría que la forma de contabilizar
        dependiera de la disponibilidad de un servicio.
        """
        if not tipo_comprobante:
            return None
        valor = tipo_comprobante.get("consumption_tax")
        return valor if isinstance(valor, bool) else None

    # ── Validación previa ──────────────────────────────────────────────────────

    def _validate(self, request: SendPurchaseInvoiceRequest) -> None:
        """Comprueba lo que SIIGO exige, con mensajes que digan qué falta y dónde.

        Los mensajes van dirigidos al contador, no al desarrollador: nombran el dato que
        falta en los términos del documento, porque quien los va a leer es quien tiene que
        corregirlo en la interfaz.
        """
        faltantes: list[str] = []

        for idx, item in enumerate(request.items, start=1):
            if item.type not in _VALID_ITEM_TYPES:
                faltantes.append(
                    f"La línea {idx} tiene un tipo de ítem no admitido por SIIGO "
                    f"('{item.type}'); solo acepta {', '.join(_VALID_ITEM_TYPES)}."
                )
            if not (item.code or "").strip():
                faltantes.append(
                    f"La línea {idx} no tiene cuenta contable ni producto asignado. "
                    "Asigne la cuenta PUC o el producto antes de contabilizar."
                )
            if item.quantity <= 0:
                faltantes.append(f"La línea {idx} tiene una cantidad inválida ({item.quantity}).")
            if item.price < 0:
                faltantes.append(f"La línea {idx} tiene un valor unitario negativo.")

        if request.discount_type and request.discount_type not in _VALID_DISCOUNT_TYPES:
            faltantes.append(
                f"El tipo de descuento '{request.discount_type}' no es válido; SIIGO solo "
                f"admite {', '.join(_VALID_DISCOUNT_TYPES)}."
            )

        # SIIGO exige la tasa junto con la moneda: enviar una sin la otra produce un 400.
        if request.currency_code and request.currency_exchange_rate is None:
            faltantes.append(
                "Se indicó moneda extranjera pero no la tasa de cambio; SIIGO exige ambas."
            )

        if request.observations and len(request.observations) > 4000:
            faltantes.append("Las observaciones superan el límite de 4.000 caracteres de SIIGO.")

        if faltantes:
            raise ValidationException(
                "El documento no puede contabilizarse porque le falta información requerida "
                "por SIIGO: " + " ".join(faltantes)
            )

    # ── Construcción del JSON ──────────────────────────────────────────────────

    def _validate_cost_center(
        self, request: SendPurchaseInvoiceRequest, tipo_comprobante: Optional[dict]
    ) -> None:
        """Exige centro de costo solo si el comprobante de la empresa lo exige.

        Sin catálogo disponible no se bloquea: se envía y SIIGO decide. Es preferible a
        inventar una regla, porque el único que sabe si es obligatorio es SIIGO.
        """
        if not tipo_comprobante or not tipo_comprobante.get("cost_center_mandatory"):
            return
        if request.cost_center or tipo_comprobante.get("cost_center_default"):
            return
        raise ValidationException(
            "El comprobante de factura de compra configurado en SIIGO exige centro de costo "
            "y el documento no tiene uno asignado. Asígnelo al documento o defina un centro "
            "de costo por defecto en el comprobante dentro de SIIGO."
        )

    def _build_payload(
        self,
        request: SendPurchaseInvoiceRequest,
        tipo_comprobante: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Arma el cuerpo exactamente con los campos que documenta `POST /v1/purchases`.

        Los campos opcionales se omiten cuando no hay dato, en lugar de enviarse en null:
        SIIGO valida presencia, y mandar `cost_center: null` no es lo mismo que no mandarlo.
        """
        payload: dict[str, Any] = {
            "document": {"id": request.document_id},
            "date": request.date.isoformat(),
            "supplier": {
                "identification": request.supplier_identification,
                "branch_office": request.supplier_branch_office,
            },
            "items": [self._build_item(item) for item in request.items],
            "payments": [self._build_payment(request)],
        }

        # Numeración manual: SIIGO no asigna el consecutivo, lo exige en el cuerpo, y sin él
        # responde `parameter_required` sobre el campo `number`. Con numeración automática
        # ocurre lo contrario —lo asigna SIIGO y enviarlo sería pisarlo—, así que el campo
        # solo aparece cuando la propia configuración del comprobante dice que hace falta.
        if tipo_comprobante and tipo_comprobante.get("automatic_number") is False:
            consecutivo = tipo_comprobante.get("consecutive")
            if consecutivo is None:
                raise ValidationException(
                    "El comprobante está configurado en SIIGO con numeración manual, pero "
                    "SIIGO no informa cuál es el siguiente consecutivo. Revise la numeración "
                    "del comprobante en SIIGO antes de contabilizar."
                )
            payload["number"] = int(consecutivo)

        provider_invoice = self._build_provider_invoice(
            request.provider_invoice_prefix, request.provider_invoice_number
        )
        if provider_invoice:
            payload["provider_invoice"] = provider_invoice

        # El centro de costo del documento manda; si no viene, se usa el que la propia
        # empresa configuró como predeterminado del comprobante (`cost_center_default`).
        # Tomarlo de SIIGO y no de la plantilla evita imputar a un centro elegido por
        # nosotros: el que aplica cuando nadie decide lo decidió el contador en SIIGO.
        cost_center = request.cost_center
        if cost_center is None and tipo_comprobante:
            cost_center = tipo_comprobante.get("cost_center_default")
        if cost_center is not None:
            payload["cost_center"] = cost_center

        if request.retention_ids:
            # AMBIGÜEDAD DE LA DOCUMENTACIÓN DE SIIGO, RESUELTA CONTRA EL AMBIENTE REAL.
            #
            # La tabla descriptiva de POST /v1/purchases documenta `retentions` como «Array
            # con los id de los impuestos tipo ReteICA, ReteIVA», mientras que la estructura
            # formal `PurchasesIn` del mismo blueprint no lista el campo ni ofrece ningún
            # ejemplo con retenciones. Se implementó primero la lectura literal —un array de
            # enteros— por ser la única que el texto respaldaba.
            #
            # SIIGO la rechazó: `422 invalid_type: retentions[0]`, es decir, el array se
            # acepta pero sus elementos no son del tipo esperado. La forma correcta es la
            # misma con la que SIIGO modela los impuestos de cada ítem unas líneas más abajo
            # (`taxes = [{"id": n}]`), que sí acepta sin objeción.
            payload["retentions"] = [{"id": rid} for rid in request.retention_ids]

        if request.observations:
            payload["observations"] = request.observations

        if request.discount_type:
            payload["discount_type"] = request.discount_type

        if request.tax_included is not None:
            payload["tax_included"] = request.tax_included

        if request.currency_code:
            payload["currency"] = {
                "code": request.currency_code,
                "exchange_rate": self._number(request.currency_exchange_rate),
            }

        return payload

    #: Límites que documenta SIIGO para el bloque `provider_invoice`.
    #: prefix: «alfanumérico de máximo 6 caracteres». number: «solo admite números y debe ser
    #: de 11 enteros».
    MAX_PREFIJO = 6
    MAX_CONSECUTIVO = 11

    #: Prefijo con el que viaja una factura cuyo número no trae ninguno.
    #:
    #: `provider_invoice` es OBLIGATORIO, y con sus dos campos. El esquema del blueprint no lo
    #: lista entre los `required` del documento, pero la API real discrepa y manda ella: al
    #: enviar el bloque sin `prefix` responde `The field provider_invoice.prefix is required`,
    #: y al omitirlo entero, `The field provider_invoice is required`. Se comprobaron los dos
    #: casos contra el ambiente real con la factura «941457814», que es solo dígitos.
    #:
    #: Así que hace falta un prefijo incluso cuando el proveedor no usa ninguno. Manda el que
    #: se configure en la plantilla de parámetros; este es el último recurso para que un
    #: número sin prefijo no bloquee la contabilización. «FV» por «factura de venta», que es
    #: el tipo de documento que emite el proveedor, y es la forma del ejemplo oficial («FV1»).
    PREFIJO_POR_DEFECTO = os.getenv("SIIGO_PROVIDER_INVOICE_PREFIX", "FV")

    @classmethod
    def _build_provider_invoice(
        cls, prefijo: Optional[str], numero: Optional[str]
    ) -> dict[str, Any]:
        """Bloque `provider_invoice`, completo o ausente. Nunca a medias.

        El esquema del endpoint lo declara así: `provider_invoice` no está entre los campos
        obligatorios del documento —lo son `document`, `date`, `supplier`, `items` y
        `payments`—, pero si se envía, exige los dos:

            "provider_invoice": { ..., "required": ["prefix", "number"] }

        Antes se omitía `prefix` cuando el número del proveedor era solo dígitos
        —«941457814»— por no mandar una cadena vacía. Con eso el bloque viajaba incompleto y
        SIIGO respondía `The field provider_invoice.prefix is required`. La lectura correcta
        no es «omitir el campo vacío» sino «omitir el bloque entero»: es opcional, y sin
        prefijo no hay bloque válido que construir.

        SIIGO modela el número del proveedor en dos campos, y `number` solo admite dígitos.
        Los números de la facturación electrónica colombiana llegan de la DIAN como una sola
        cadena —«FBC98359», «TOFV21215»—, así que se parte por la última racha de dígitos:
        esa es el consecutivo y lo anterior es el prefijo. Un prefijo recibido explícitamente
        manda sobre el deducido, porque lo configuró alguien que conoce la nomenclatura.

        El prefijo puede contener dígitos —«G3Z9338669», «003B54597», «F78P21635» son números
        reales de proveedores—, así que no sirve buscar la frontera entre letras y dígitos:
        hay que anclarse al final de la cadena.
        """
        crudo = (numero or "").strip()
        if not (prefijo or crudo):
            return {}

        if prefijo:
            # Con prefijo explícito, se retira del consecutivo si viene repetido en él.
            if crudo.upper().startswith(prefijo.upper()):
                crudo = crudo[len(prefijo) :]
            # Y aun así hay que anclarse a los dígitos finales: el prefijo configurado es el
            # de la empresa, no el del proveedor, así que retirarlo puede no quitar nada y
            # dejar letras dentro de `number`.
            crudo = _consecutivo_final(crudo)
            deducido = prefijo
        else:
            coincidencia = re.match(r"^(?P<prefijo>.*?)(?P<numero>\d+)$", crudo)
            if not coincidencia:
                # Sin dígitos finales no hay consecutivo que enviar.
                return {}
            deducido = coincidencia.group("prefijo")
            crudo = coincidencia.group("numero")

        # Sin consecutivo no hay bloque posible: `number` es obligatorio y no se inventa.
        if not crudo:
            return {}

        # Sin prefijo sí hay salida, porque el campo es obligatorio y omitir el bloque
        # tampoco vale: se recurre al configurado. No es un dato inventado sobre la factura
        # —el proveedor no usa prefijo— sino la etiqueta con la que la empresa decide
        # registrar esas facturas en su contabilidad.
        if not deducido:
            deducido = cls.PREFIJO_POR_DEFECTO
            logger.info(
                "El número del proveedor no trae prefijo y SIIGO lo exige; se usa %r. "
                "Defínelo en la plantilla de parámetros si debe ser otro.",
                deducido,
            )

        # `number` «solo admite números»: lo que no lo sea no es un consecutivo.
        if not crudo.isdigit():
            return {}

        # «debe ser de 11 enteros». Un consecutivo más largo NO se recorta: recortarlo lo
        # convertiría en un número distinto del que aparece en la factura del proveedor, que
        # es justo lo que este campo sirve para cruzar. Se omite el bloque y se deja
        # constancia.
        if len(crudo) > cls.MAX_CONSECUTIVO:
            logger.info(
                "El consecutivo del proveedor tiene %s dígitos y SIIGO admite %s; se omite "
                "`provider_invoice` en lugar de enviar un número recortado.",
                len(crudo),
                cls.MAX_CONSECUTIVO,
            )
            return {}

        # El prefijo sí se recorta: es una etiqueta, no un identificador numérico, y SIIGO lo
        # limita a 6 caracteres.
        return {"prefix": deducido[: cls.MAX_PREFIJO], "number": crudo}

    def _build_item(self, item: PurchaseInvoiceItem) -> dict[str, Any]:
        built: dict[str, Any] = {
            "type": item.type,
            "code": item.code,
            "quantity": self._number(item.quantity),
            "price": self._number(item.price),
        }
        if item.description:
            built["description"] = item.description
        if item.tax_ids:
            built["taxes"] = [{"id": tax_id} for tax_id in item.tax_ids]
        return built

    def _build_payment(self, request: SendPurchaseInvoiceRequest) -> dict[str, Any]:
        payment: dict[str, Any] = {
            "id": request.payment_id,
            "value": self._number(request.payment_value),
        }
        if request.payment_due_date:
            payment["due_date"] = request.payment_due_date.isoformat()
        return payment

    @staticmethod
    def _number(value: Optional[Decimal]) -> Optional[float]:
        """Convierte a float para serializar en JSON, conservando None."""
        return None if value is None else float(value)

    # ── Validación de la respuesta ─────────────────────────────────────────────

    @staticmethod
    def _build_response(
        raw: dict[str, Any], supports_consumption_tax: Optional[bool] = None
    ) -> SendPurchaseInvoiceResponse:
        """Exige que la respuesta contenga un id antes de darla por buena.

        Un 201 no basta. Sin id no hay forma de demostrar después qué se creó en SIIGO ni de
        reconciliar, así que un 201 sin id se trata como fallo: es preferible dejar el
        documento bloqueado para revisión que marcarlo contabilizado sin evidencia.
        """
        if not isinstance(raw, dict):
            raise SiigoApiException(
                "SIIGO respondió con una estructura inesperada (no es un objeto JSON).",
                retryable=False,
            )

        siigo_id = raw.get("id")
        if siigo_id is None or str(siigo_id).strip() == "":
            raise SiigoApiException(
                "SIIGO aceptó la petición pero no devolvió el identificador del comprobante. "
                "Verifique en SIIGO si la factura quedó creada antes de reenviarla.",
                retryable=False,
            )

        return SendPurchaseInvoiceResponse(
            siigo_id=str(siigo_id),
            siigo_name=str(raw["name"]) if raw.get("name") else None,
            siigo_response=raw,
            supports_consumption_tax=supports_consumption_tax,
        )


def _consecutivo_final(valor: str) -> str:
    """La última racha de dígitos de la cadena, o la cadena tal cual si no termina en dígito.

    No se inventa un número cuando no lo hay: se devuelve el original y que SIIGO decida.
    Adivinar la forma de un identificador ajeno es peor que dejar que el error salga con el
    dato a la vista.
    """
    coincidencia = re.search(r"(\d+)$", valor or "")
    return coincidencia.group(1) if coincidencia else valor
