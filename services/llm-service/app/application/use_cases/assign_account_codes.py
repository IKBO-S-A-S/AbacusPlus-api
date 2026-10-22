import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from app.domain.exceptions.base import NoChartOfAccountsError
from app.domain.ports.services import AIServicePort
from app.domain.services.product_matching import (
    candidatos_por_linea,
    debe_buscar_productos,
    resolver_producto,
)
from app.infrastructure.clients.document_client import DocumentClient
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.persistence.repositories.system_prompt_repository import (
    SystemPromptRepository,
)

logger = logging.getLogger(__name__)

#: Cuántas asignaciones de un mismo lote pueden estar llamando al modelo a la vez.
#:
#: Por defecto 5: conservador y sensato para cualquier cuenta de OpenAI, incluidas las de
#: menor tier, cuyo límite por minuto se agota antes con prompts grandes. Es un tope de
#: peticiones **en vuelo**, no de peticiones por minuto: el proveedor impone lo segundo y
#: este número solo evita que el lote se lo salte de golpe. Se sube midiendo contra la cuenta
#: real, nunca por suposición.
_DEFAULT_ASSIGN_CONCURRENCY = 5

#: Separador entre las dos mitades del prompt: el JSON del documento y la tabla de cuentas
#: candidatas. Es una constante y no un literal repetido porque `_build_prompt` lo escribe y
#: `split_prompt` lo lee, y una discrepancia entre ambos rompería en silencio la lectura.
_CANDIDATES_HEADER = (
    "\n\nCUENTAS CANDIDATAS (una por línea, formato `código|nombre|naturaleza`).\n"
    "Elige exclusivamente códigos de esta lista:\n"
)


def _max_concurrency() -> int:
    """Concurrencia del lote, leída del entorno en cada uso para poder ajustarla sin desplegar."""
    try:
        valor = int(os.getenv("ASSIGN_CODES_MAX_CONCURRENCY", _DEFAULT_ASSIGN_CONCURRENCY))
    except ValueError:
        logger.warning(
            "ASSIGN_CODES_MAX_CONCURRENCY no es un entero; se usa %s",
            _DEFAULT_ASSIGN_CONCURRENCY,
        )
        return _DEFAULT_ASSIGN_CONCURRENCY
    # Un valor de cero o negativo dejaría el semáforo bloqueado para siempre y el lote colgado
    # sin síntoma. Se corrige al mínimo operativo —secuencial— en vez de fallar al arrancar.
    return max(1, valor)


# Clases PUC que pueden recibir la contrapartida de un ítem en un documento de compra:
# 5 Gastos · 6 Costos de venta · 7 Costos de producción o de operación.
# Se excluyen 2 (Pasivo) y 3 (Patrimonio) —propias de la cuenta por pagar y las
# retenciones— y 4 (Ingresos), que corresponde a lo que la empresa factura, no compra.
_ITEM_ACCOUNT_CLASSES = frozenset({"5", "6", "7"})

# La clase 1 (Activo) sí puede recibir un ítem, pero solo en algunos grupos: comprar
# mercancía, un activo fijo, un intangible o un gasto anticipado. Nunca en Efectivo (11),
# Inversiones (12), Deudores (13) ni Valorizaciones (19): una compra no se imputa a la caja
# ni a una cuenta por cobrar. Sin esta distinción el catálogo ofrecería «Caja general» o
# «Anticipos a proveedores» como candidatas para un refrigerio.
_ITEM_ASSET_GROUPS = frozenset(
    {
        "14",  # Inventarios
        "15",  # Propiedades, planta y equipo
        "16",  # Intangibles
        "17",  # Diferidos — gastos pagados por anticipado
        "18",  # Otros activos
    }
)

# La instrucción "solo JSON" se repite dos veces en el prompt: los LLMs respetan
# mejor las restricciones de formato cuando se refuerzan explícitamente.
_DEFAULT_SYSTEM_PROMPT = """\
Eres un Contador Público colombiano con amplia experiencia causando facturas de compra \
electrónicas DIAN sobre el Plan Único de Cuentas (PUC).

Tu tarea es asignar, a CADA ítem de la factura, la cuenta del PUC que un contador \
experimentado escogería al causarla manualmente.

MÉTODO DE ANÁLISIS (aplícalo ítem por ítem, de forma independiente):
1. Lee la descripción completa del ítem e infiere QUÉ SE ADQUIRIÓ realmente.
2. Determina la NATURALEZA ECONÓMICA de esa adquisición antes de mirar el catálogo:
   ¿es un gasto del período, un costo de operación, un inventario o un activo fijo?
3. Usa el contexto para desambiguar: la razón social del emisor suele revelar su
   actividad (una empresa de alimentos factura alimentación; una de software, licencias),
   y el centro de costo indica el área que consume el gasto.
4. Considera cantidad y valor unitario: un mismo concepto puede ser gasto menor o activo
   fijo según su cuantía y durabilidad (un mouse es gasto; 20 computadores, activo fijo).
5. Solo entonces elige la cuenta cuya DESCRIPCIÓN represente esa naturaleza.

PROHIBIDO:
- Elegir una cuenta porque su nombre comparte palabras con la descripción del ítem. La
  coincidencia léxica no es un criterio contable: «Servicio de transporte de personal»
  no va a una cuenta de «Vehículos» solo por la palabra transporte.
- Asignar la misma cuenta a todos los ítems por comodidad. Cada línea se analiza sola.
- Usar cuentas genéricas de «Diversos» o «Otros» cuando existe una cuenta específica que
  representa mejor el concepto. Recurre a ellas solo como último recurso.
- Proponer un código que no esté en la lista de cuentas candidatas entregada.

NATURALEZA DE LAS CLASES PUC (el primer dígito del código):
  1 Activo · 2 Pasivo · 3 Patrimonio · 4 Ingresos · 5 Gastos · 6 Costos de venta ·
  7 Costos de producción o de operación
En un documento de COMPRA, la contrapartida del ítem es un gasto (5), un costo (6 o 7) o
un activo (1). NUNCA un pasivo (2), un patrimonio (3) ni un ingreso (4): esas cuentas
corresponden a la causación de la cuenta por pagar y de las retenciones, que NO son tu
tarea. Dentro del activo solo aplican inventarios (14), propiedad planta y equipo (15),
intangibles (16), diferidos (17) y otros activos (18); jamás el efectivo (11) ni los
deudores (13), porque una compra no se imputa a la caja ni a una cuenta por cobrar.
Las cuentas candidatas ya vienen filtradas conforme a estas reglas: elige solo de esa lista.

EJEMPLOS DE RAZONAMIENTO CORRECTO:
- «Servicio de transporte / fletes» → gasto de transporte, fletes o acarreos.
- «Refrigerios, almuerzos, casino» → gasto de alimentación, cafetería o bienestar.
- «Resmas de papel, tóner, esferos» → gasto de útiles y papelería.
- «Asesoría jurídica, consultoría» → gasto de honorarios.
- «Energía, acueducto, internet» → gasto de servicios públicos.
- «Licencia anual de software» → gasto de software o licencias, no un activo fijo.
- «Computadores, impresoras» → activo fijo de equipo de cómputo si son durables.

REGLAS ESTRICTAS:
- Responde ÚNICAMENTE con un objeto JSON. Sin explicaciones, sin markdown, sin texto adicional.
- El JSON debe tener exactamente esta estructura:
  {
    "items": [
      {
        "item_id": "<id exacto recibido>",
        "description": "<descripción del ítem>",
        "suggested_account_code": "<código PUC>",
        "suggested_account_name": "<nombre de la cuenta PUC>"
      }
    ]
  }
- El campo "item_id" debe ser exactamente el mismo valor recibido en el input, sin modificación.
- El campo "suggested_account_code" debe ser un código existente en el PUC proporcionado.
- NO calcules valores monetarios.
- NO valides débitos ni créditos.
- NO generes asientos contables.
- NO agregues campos adicionales al JSON.
- Si la descripción es ambigua, apóyate en la actividad del emisor y en el centro de costo \
  antes de recurrir a una cuenta genérica. Si aun así no puedes determinar la cuenta con \
  certeza razonable, OMITE el ítem en lugar de adivinar: el contador la asignará a mano.\
"""


# Prompt propio de la detección de productos, y no una ampliación del prompt de cuentas, por
# dos razones. La primera: el prompt de cuentas vive en la base de cada empresa
# (`system_prompts`) y los ya guardados prohíben «campos adicionales» en el JSON, así que
# pedirle uno nuevo contradiría su propio prompt de sistema. La segunda: cambiar de modo en un
# tercero no debe tocar en nada el flujo de cuentas, que es el que ya funciona.
_PRODUCT_SYSTEM_PROMPT = """\
Eres un contador público colombiano que causa facturas de compra electrónicas. Para cada ítem \
decides si debe registrarse como un PRODUCTO de inventario del catálogo de la empresa o no.

Un ítem va por producto SOLO si se cumplen las dos condiciones:
1. Es un bien físico que la empresa compra para tener en inventario o consumir en su \
operación: mercancía, materia prima, insumos, repuestos, materiales.
2. Uno de sus "candidatos" es ese mismo artículo: misma clase de objeto y coinciden los \
rasgos que lo distinguen (medida, material, referencia, presentación, marca).

NO van por producto, aunque algún candidato se parezca en el nombre: servicios, honorarios, \
fletes, arrendamientos, servicios públicos, licencias, seguros, gastos en general y activos \
fijos. Para ellos responde null.

Ante la duda, responde null. Equivocarse hacia la cuenta es barato: el contador la corrige y \
el documento se causa igual. Un producto equivocado altera el inventario.

Solo puedes usar códigos de la lista "candidatos" de ESE ítem. Nunca inventes un código ni \
uses el de otro ítem.

Responde ÚNICAMENTE con un objeto JSON, sin explicaciones ni markdown, con esta estructura \
exacta e incluyendo todos los ítems recibidos:
{"items":[{"item_id":<id exacto recibido>,"product_code":"<código del candidato>"|null}]}\
"""


@dataclass(frozen=True)
class _ProductPlan:
    """Qué se sabe de la detección de productos para un documento.

    - `ran`: se pudo evaluar de verdad (se conoce al tercero y el catálogo se consultó sin
      error, esté vacío o no). Es lo que autoriza al xml-processor a fijar el modo del tercero.
    - `candidates`: por `detail_id`, los productos del catálogo que se parecen a la línea.
      Vacío cuando no hay nada que preguntarle al modelo.
    """

    ran: bool = False
    candidates: dict = field(default_factory=dict)


class AssignAccountCodesUseCase:
    """Asigna cuentas PUC —o productos del catálogo— a cada línea de un documento con el LLM.

    Siempre se asigna una cuenta a cada línea. Si el tercero no está forzado a cuenta y hay
    catálogo de productos, además se pregunta al modelo qué líneas son un producto del
    catálogo: esas pasan a `type="Product"` y el resto conserva su cuenta, así que un mismo
    documento puede mezclar ambas.
    """

    def __init__(
        self,
        ai_service: AIServicePort,
        document_client: DocumentClient,
        integration_config_client: IntegrationConfigClient,
        system_prompt_repo: SystemPromptRepository,
        rag_client=None,
    ):
        self._ai = ai_service
        self._document_client = document_client
        self._integration_config_client = integration_config_client
        self._system_prompt_repo = system_prompt_repo
        # RAG opcional: aporta el historial de decisiones (cuentas/retenciones) de documentos
        # anteriores del mismo tercero, para que el modelo se apoye en lo ya confirmado. Su
        # ausencia (o fallo) no bloquea la asignación: es contexto, no un requisito.
        self._rag = rag_client

    async def execute(
        self,
        document_id: int,
        overwrite_manual: bool = False,
        *,
        system_prompt: Optional[str] = None,
    ) -> dict:
        """Asigna cuentas PUC a las líneas del documento y las persiste vía xml-processor.

        RF-04: las líneas con `code_source == "manual"` se respetan y no se envían al modelo,
        salvo que `overwrite_manual` sea True (el contador confirmó sobrescribirlas).

        `system_prompt` permite pasarlo ya resuelto. Lo usa el lote, que lo lee una sola vez
        para todos sus documentos: es el mismo prompt activo para toda la empresa, y además
        leerlo desde varias tareas concurrentes tocaría la misma sesión de SQLAlchemy, que no
        está pensada para uso simultáneo.

        Retorna {assigned, skipped, warnings}.
        """
        warnings: list[str] = []

        document = await self._document_client.get_document_full(document_id)
        if document is None:
            raise ValueError(f"Documento {document_id} no encontrado en xml-processor")

        details = document.get("details", [])
        if not details:
            return {"assigned": 0, "skipped": 0, "warnings": ["Documento sin líneas de detalle"]}

        # Los tres son independientes entre sí y ninguno depende del resultado de otro: los
        # catálogos son configuración de la empresa y el historial sale del RAG. Se piden a la
        # vez para no encadenar tres latencias antes de la única llamada que de verdad cuesta,
        # la del modelo. El historial iba después de los catálogos sin necesitarlos.
        chart_accounts, cost_centers, history, issuer = await asyncio.gather(
            self._integration_config_client.get_chart_accounts(),
            self._integration_config_client.get_cost_centers(),
            self._issuer_history_context(document),
            self._document_client.get_issuer(str(document.get("issuer_nit") or "")),
        )
        puc_index = {acc["code"]: acc for acc in chart_accounts}
        cost_centers_by_id = {
            cc["id"]: f"{cc.get('code')} — {cc.get('name')}"
            for cc in cost_centers
            if cc.get("id") is not None
        }

        # RF-08 (regla de negocio crítica): sin PUC cargado el modelo no debe inventar ni
        # sugerir cuentas de un PUC colombiano genérico. Se detiene el proceso.
        if not puc_index:
            raise NoChartOfAccountsError()

        # RF-04: se protegen las líneas editadas a mano.
        protected_ids = {
            d["id"] for d in details if d.get("code_source") == "manual" and d.get("id") is not None
        }
        if protected_ids and not overwrite_manual:
            details = [d for d in details if d.get("id") not in protected_ids]
            warnings.append(
                f"{len(protected_ids)} línea(s) con edición manual se conservaron sin cambios."
            )
            if not details:
                return {
                    "assigned": 0,
                    "skipped": len(protected_ids),
                    "warnings": warnings,
                }

        if system_prompt is None:
            system_prompt = self._resolve_system_prompt()
        # DEBUG y no INFO: el prompt lleva el catálogo de cuentas del cliente y la respuesta
        # incluye descripciones de ítems y códigos contables. Son datos del cliente y no
        # deben quedar por defecto en los logs, que suelen ir a agregadores compartidos.
        logger.debug("LLM system_prompt for doc=%s: %s", document_id, system_prompt)

        # Las candidatas se calculan una sola vez: alimentan el prompt y también validan la
        # respuesta, de modo que el modelo no pueda devolver una cuenta fuera de ese conjunto.
        candidates = self._candidate_accounts(chart_accounts)
        candidate_index = {c["code"]: c for c in candidates}

        # Sin candidatas no hay nada que el modelo pueda proponer con fundamento: el
        # catálogo existe pero ninguna de sus cuentas puede recibir el ítem de una compra.
        # Se corta antes de llamar a OpenAI —no tendría con qué responder— y se dice por qué.
        if not candidate_index:
            return {
                "assigned": 0,
                "skipped": len(details),
                "warnings": warnings
                + [
                    "No fue posible generar una sugerencia confiable: el plan de cuentas "
                    "no tiene cuentas imputables de gasto, costo o activo (clases 5, 6, 7 "
                    "o grupos 14-18). Revise el catálogo sincronizado."
                ],
            }

        # `history` (best-effort) ya viene resuelto del `gather` de arriba: son decisiones
        # confirmadas en documentos anteriores del mismo emisor, que el modelo usa como
        # precedente.
        user_prompt = self._build_prompt(document, details, candidates, cost_centers_by_id, history)

        # Productos: solo si el tercero no está forzado a cuenta y hay catálogo que ofrecer.
        product_plan = await self._plan_products(details, issuer)

        account_call = self._ai.complete(prompt=user_prompt, system_prompt=system_prompt)
        if product_plan.candidates:
            # Independientes entre sí: la pregunta de productos no necesita la cuenta, que se
            # pide para todas las líneas como respaldo. En paralelo no suman latencias.
            ai_response, (product_codes, product_warnings, product_ok) = await asyncio.gather(
                account_call,
                self._assign_products(document, details, product_plan.candidates),
            )
            warnings.extend(product_warnings)
        else:
            ai_response = await account_call
            product_codes, product_ok = {}, True
        raw_content: str = ai_response.get("content", "")
        logger.debug("LLM raw_response for doc=%s: %s", document_id, raw_content)

        assignments, parse_warnings = self._parse_response(raw_content, details, candidate_index)
        warnings.extend(parse_warnings)

        # Una línea reconocida como producto sustituye a su cuenta: el payload de SIIGO lleva
        # una sola identificación por ítem. Las demás conservan la cuenta.
        if product_codes:
            assignments = [a for a in assignments if a["detail_id"] not in product_codes]
            assignments.extend(
                {"detail_id": detail_id, "code": code, "type": "Product"}
                for detail_id, code in sorted(product_codes.items())
            )
        # Si la pregunta de productos falló, no se evaluó nada: no se concluye que el tercero
        # no vende productos.
        detection_ran = product_plan.ran and product_ok

        # Las líneas protegidas cuentan como omitidas: no se enviaron al modelo, pero el
        # total reportado debe cuadrar con las líneas reales del documento.
        protected_count = len(protected_ids) if not overwrite_manual else 0

        # Ninguna propuesta sobrevivió a la validación. Se informa explícitamente en vez de
        # devolver un cero mudo: para quien usa la interfaz, «no se pudo determinar» y «no
        # pasó nada» son situaciones distintas y solo la primera pide su intervención.
        if not assignments:
            return {
                "assigned": 0,
                "skipped": len(details) + protected_count,
                "warnings": warnings
                + [
                    "No fue posible generar una sugerencia confiable para ninguna línea. "
                    "Asigne las cuentas manualmente o revise que el plan de cuentas "
                    "sincronizado contenga las cuentas de gasto y costo que usa la empresa."
                ],
            }

        updated = await self._document_client.patch_detail_codes(
            document_id, assignments, product_detection_ran=detection_ran
        )

        skipped = len(details) - updated + protected_count
        return {"assigned": updated, "skipped": skipped, "warnings": warnings}

    async def _plan_products(self, details: list[dict], issuer: Optional[dict]) -> _ProductPlan:
        """Decide si se buscan productos y, si sí, qué candidatos tiene cada línea.

        Sin información del tercero (`issuer is None`: no se pudo consultar) no se busca nada:
        podría estar forzado a cuenta y el modelo no debe ignorar esa regla por una caída. El
        xml-processor la hace cumplir además por su lado, pero esto evita el trabajo.
        """
        if issuer is None:
            return _ProductPlan()
        mode = issuer.get("item_assignment_mode")
        if mode == "account":
            return _ProductPlan()

        catalogo = await self._integration_config_client.get_products()
        if catalogo is None:
            return _ProductPlan()
        if not debe_buscar_productos(mode, catalogo):
            # Catálogo consultado y vacío: la detección «corrió» y concluyó que no hay nada.
            return _ProductPlan(ran=True)

        history = issuer.get("product_history") or []
        candidates = {}
        for d in details:
            found = candidatos_por_linea(d.get("description"), catalogo, history)
            if found and d.get("id") is not None:
                candidates[d["id"]] = found
        return _ProductPlan(ran=True, candidates=candidates)

    async def _assign_products(
        self, document: dict, details: list[dict], candidates_by_line: dict
    ) -> tuple[dict, list[str], bool]:
        """Pregunta al modelo cuáles líneas son un producto del catálogo.

        Retorna `(códigos por detail_id, advertencias, ok)`. Solo se aceptan códigos que estén
        entre los candidatos de esa línea (`resolver_producto`). `ok=False` si la llamada o la
        respuesta fallaron: la asignación de cuentas sigue adelante sin productos.
        """
        by_id = {d["id"]: d for d in details}
        items = [
            {
                "item_id": detail_id,
                "descripcion": by_id[detail_id].get("description", ""),
                "cantidad": by_id[detail_id].get("quantity"),
                "unidad": by_id[detail_id].get("unit"),
                "valor_unitario": by_id[detail_id].get("price"),
                "candidatos": [
                    {"codigo": c["code"], "descripcion": c["description"]} for c in candidates
                ],
            }
            for detail_id, candidates in candidates_by_line.items()
            if detail_id in by_id
        ]
        prompt = json.dumps(
            {
                "documento": {
                    "emisor_razon_social": document.get("issuer_name"),
                    "emisor_nit": document.get("issuer_nit"),
                },
                "items": items,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            response = await self._ai.complete(prompt=prompt, system_prompt=_PRODUCT_SYSTEM_PROMPT)
            parsed = self._load_json(response.get("content", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo evaluar productos para doc=%s: %s", document.get("id"), exc)
            return (
                {},
                ["No fue posible evaluar si las líneas son productos; se asignaron por cuenta."],
                False,
            )
        if parsed is None:
            return {}, ["El LLM no retornó JSON válido al evaluar productos."], False

        chosen: dict = {}
        for item in parsed.get("items") or []:
            try:
                detail_id = int(item.get("item_id"))
            except (TypeError, ValueError):
                continue
            if detail_id not in candidates_by_line:
                continue
            code = resolver_producto(item.get("product_code"), candidates_by_line[detail_id])
            if code is not None:
                chosen[detail_id] = code
            elif item.get("product_code"):
                logger.warning(
                    "Producto %r descartado para la línea %s: no es uno de sus candidatos",
                    item.get("product_code"),
                    detail_id,
                )
        return chosen, [], True

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        """JSON de la respuesta, tolerando el envoltorio markdown que algunos modelos añaden."""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw or "", re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None

    def _resolve_system_prompt(self) -> str:
        """Prompt activo de la empresa, o el de fábrica si no hay ninguno configurado."""
        activo = self._system_prompt_repo.get_active()
        return activo.content if activo else _DEFAULT_SYSTEM_PROMPT

    async def execute_many(
        self, document_ids: list[int], overwrite_manual: bool = False
    ) -> list[dict]:
        """Asigna cuentas a varios documentos **en paralelo**, con concurrencia acotada.

        La causación de un lote es un conjunto de trabajos independientes: cada documento
        tiene su prompt, su respuesta y su escritura, y ninguno necesita el resultado de otro.
        Hacerlos en serie —que es lo que ocurría al recorrer la selección desde el navegador—
        sumaba la latencia del modelo tantas veces como documentos hubiera. En paralelo, el
        lote tarda aproximadamente lo que el documento más lento por cada tanda.

        **La concurrencia está acotada, y eso es lo que la hace segura.** El semáforo
        (`ASSIGN_CODES_MAX_CONCURRENCY`, 5 por defecto) limita cuántas llamadas al modelo hay
        en vuelo a la vez: sin él, seleccionar trescientos documentos abriría trescientas
        peticiones simultáneas a OpenAI y el proveedor respondería con 429 a casi todas,
        convirtiendo un lote lento en un lote fallido. El valor no está incrustado: se sube
        cuando el límite de la cuenta lo permita, midiéndolo.

        **Ningún documento tumba el lote.** El fallo de uno se captura y se reporta en su
        entrada del resultado; los demás siguen. Es la misma política best-effort que ya rige
        la asignación individual, aplicada al conjunto.

        Retorna una entrada por documento pedido, en el mismo orden, con `document_id`, `ok`
        y el resultado o el error.
        """
        vistos: set[int] = set()
        unicos = [i for i in document_ids if not (i in vistos or vistos.add(i))]

        semaforo = asyncio.Semaphore(_max_concurrency())
        # Una sola lectura del prompt activo para todo el lote (ver `execute`).
        system_prompt = self._resolve_system_prompt()

        async def _uno(document_id: int) -> dict:
            async with semaforo:
                try:
                    resultado = await self.execute(
                        document_id,
                        overwrite_manual=overwrite_manual,
                        system_prompt=system_prompt,
                    )
                    return {"document_id": document_id, "ok": True, **resultado}
                except NoChartOfAccountsError:
                    # RF-08: la ausencia de PUC no es un fallo del documento sino de la
                    # configuración de la empresa, e invalida el lote entero por igual. Se
                    # propaga tal cual para que la API responda 409 y la interfaz diga qué
                    # falta, en vez de reportar N documentos «fallidos» sin explicación.
                    raise
                except Exception as exc:
                    logger.warning(
                        "Fallo asignando cuentas al documento %s dentro del lote: %s",
                        document_id,
                        exc,
                    )
                    return {
                        "document_id": document_id,
                        "ok": False,
                        "assigned": 0,
                        "skipped": 0,
                        "warnings": [str(exc)],
                    }

        return list(await asyncio.gather(*(_uno(i) for i in unicos)))

    # Longitud máxima del texto histórico inyectado, por chunk. Acota el prompt (costo y
    # latencia) y limita la superficie de un texto de terceros dentro del prompt.
    _MAX_HISTORY_CHARS = 600

    async def _issuer_history_context(self, document: dict) -> list[str]:
        """Precedentes del tercero vía RAG: cuentas y retenciones ya usadas antes.

        Best-effort: si el RAG no está disponible o el emisor es desconocido, devuelve lista
        vacía y la asignación continúa solo con el contexto del documento actual.
        """
        if self._rag is None:
            return []
        issuer = (document.get("issuer_name") or "").strip()
        if not issuer:
            return []
        try:
            # RF-08: solo causaciones contabilizadas sirven de precedente contable.
            chunks = await self._rag.search(
                f"cuentas contables y retenciones usadas para {issuer}",
                top_k=3,
                only_validated=True,
            )
        except Exception as exc:
            logger.warning("RAG no disponible para el contexto de cuentas: %s", exc)
            return []
        textos = []
        for c in chunks:
            content = c.get("content")
            if content:
                # Neutraliza saltos/control para que un texto de tercero no simule
                # instrucciones dentro del prompt; acota la longitud.
                clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(content)).strip()
                textos.append(clean[: self._MAX_HISTORY_CHARS])
        return textos

    def _build_prompt(
        self,
        document: dict,
        details: list[dict],
        candidate_accounts: list[dict],
        cost_centers_by_id: Optional[dict[int, str]] = None,
        history: Optional[list[str]] = None,
    ) -> str:
        """Construye el prompt con el contexto completo del documento y las candidatas.

        Se envía el contexto que un contador mira al causar: tipo de documento, emisor
        (cuya razón social suele revelar su actividad), importes por línea y retenciones.
        Sin ese contexto el modelo solo puede parear palabras de la descripción.
        """
        cost_centers_by_id = cost_centers_by_id or {}

        items = [
            {
                "item_id": d["id"],
                "descripcion": d.get("description", ""),
                "tipo_item": d.get("type", "Account"),
                "cantidad": d.get("quantity"),
                "unidad": d.get("unit"),
                "valor_unitario": d.get("price"),
                "subtotal": d.get("subtotal"),
                "iva_porcentaje": d.get("tax_type"),
                "iva_valor": d.get("tax_value"),
                "total": d.get("total"),
                "centro_costo": cost_centers_by_id.get(d.get("cost_center_id")),
            }
            for d in details
        ]

        retenciones = [
            {
                "base_gravable": t.get("taxable_base"),
                "porcentaje": t.get("percentage"),
                "valor_retenido": t.get("value"),
            }
            for t in (document.get("taxes") or [])
        ]

        prompt_data: dict = {
            "documento": {
                "tipo": document.get("document_type"),
                # Todo documento aquí se descargó de la DIAN como comprobante recibido: el
                # emisor es el proveedor y la contrapartida del ítem es gasto/costo/activo.
                "perspectiva": "compra — documento recibido de un proveedor",
                "emisor_razon_social": document.get("issuer_name"),
                "emisor_nit": document.get("issuer_nit"),
                "receptor_razon_social": document.get("receiver_name"),
                "subtotal": document.get("subtotal"),
                "total_iva": document.get("total_taxes"),
                "total": document.get("total"),
                "retenciones": retenciones,
            },
            "items": items,
            # Precedentes del mismo tercero (RAG). Es una GUÍA basada en lo confirmado antes,
            # no una orden: el modelo debe preferir estas cuentas cuando el concepto coincide,
            # pero solo puede usar códigos presentes en las cuentas candidatas.
            "historial_decisiones_anteriores": history or [],
        }

        # El catálogo de candidatas va aparte y en texto tabulado, no dentro del JSON.
        #
        # Es, con diferencia, la parte más grande del prompt: unos cientos de cuentas frente a
        # dos o tres ítems. Serializarlo como objetos JSON indentados gastaba cuatro líneas y
        # una decena de tokens de puntuación y nombres de campo por cuenta —repetidos en cada
        # cuenta y en cada documento— para transportar dos datos. Una línea `código|nombre` los
        # transporta igual de bien: el modelo no necesita las llaves para leer una tabla, y el
        # encabezado le dice qué es cada columna.
        #
        # No se recorta ninguna cuenta. Filtrar el catálogo por parecido con la descripción
        # del ítem abarataría más, pero podría dejar fuera la cuenta correcta justo cuando la
        # descripción es ambigua, que es cuando el contador más necesita acertar. Se abarata
        # la forma, no el contenido.
        return (
            json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":"))
            + _CANDIDATES_HEADER
            + self._render_candidates(candidate_accounts)
        )

    @staticmethod
    def split_prompt(raw: str) -> tuple[dict, list[str]]:
        """Separa un prompt en su parte JSON y sus cuentas candidatas.

        El prompt tiene dos mitades con formatos distintos —JSON compacto para el documento,
        tabla de texto para el catálogo— y quien quiera inspeccionarlo (los tests, y un
        diagnóstico sobre un prompt registrado) necesita poder deshacerlas. Vive junto a
        `_build_prompt` a propósito: son las dos caras del mismo formato y deben cambiar
        juntas.
        """
        cabeza, _, cola = raw.partition(_CANDIDATES_HEADER)
        candidatas = [linea for linea in cola.split("\n") if linea.strip()]
        return json.loads(cabeza), candidatas

    @staticmethod
    def _render_candidates(candidate_accounts: list[dict]) -> str:
        """Catálogo de candidatas como tabla de texto, una cuenta por línea.

        Se neutralizan los saltos de línea y las barras verticales del nombre de la cuenta:
        el catálogo lo importa el cliente desde su propio archivo, y un nombre con un salto
        partiría una fila en dos, desalineando la tabla que el modelo está leyendo.
        """
        lineas = []
        for account in candidate_accounts:
            nombre = re.sub(r"[\s|]+", " ", str(account.get("name") or "")).strip()
            naturaleza = re.sub(r"[\s|]+", " ", str(account.get("naturaleza") or "")).strip()
            lineas.append(f"{account['code']}|{nombre}|{naturaleza}")
        return "\n".join(lineas)

    @staticmethod
    def _is_item_account(code: str) -> bool:
        """True si el código puede ser la contrapartida de un ítem de factura de compra.

        El criterio es la posición en el PUC (clase y grupo), que es estándar y no depende
        de cómo esté rotulado `account_type` en el catálogo de cada cliente.
        """
        if code[0] in _ITEM_ACCOUNT_CLASSES:
            return True
        return code[0] == "1" and code[:2] in _ITEM_ASSET_GROUPS

    @classmethod
    def _candidate_accounts(cls, chart_accounts: list[dict]) -> list[dict]:
        """Cuentas imputables que pueden recibir el ítem de una factura de compra.

        Filtrar aquí cumple dos propósitos: evita que el modelo pueda proponer una cuenta
        contablemente imposible (un pasivo, un ingreso, la caja) y reduce el tamaño del
        prompt, con ello la latencia y el costo de la llamada.
        """
        candidates = []
        for account in chart_accounts:
            if not account.get("accepts_movements", True):
                continue
            code = str(account.get("code") or "")
            if not code or not cls._is_item_account(code):
                continue
            entry = {"code": code, "name": account.get("name")}
            # La naturaleza declarada en el catálogo ayuda al modelo a razonar; se omite
            # cuando el catálogo no la trae para no enviar campos vacíos.
            if account.get("account_type"):
                entry["naturaleza"] = account["account_type"]
            candidates.append(entry)
        return candidates

    def _parse_response(
        self, raw: str, details: list[dict], puc_index: dict
    ) -> tuple[list[dict], list[str]]:
        """Parsea la respuesta del LLM y valida cada asignación contra el PUC local.

        El fallback con regex extrae el primer bloque {...} del texto porque algunos
        modelos ignoran la instrucción de responder solo JSON y envuelven la respuesta
        en markdown (```json ... ```). Acepta tanto "items" como "assignments" como
        clave raíz por retrocompatibilidad con versiones anteriores del prompt.
        """
        warnings: list[str] = []
        valid_detail_ids = {d["id"] for d in details}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Intentar extraer JSON del texto
            import re

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                warnings.append("El LLM no retornó JSON válido")
                return [], warnings
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                warnings.append("No se pudo parsear la respuesta del LLM")
                return [], warnings

        raw_assignments = parsed.get("assignments") or parsed.get("items", [])
        valid: list[dict] = []

        for item in raw_assignments:
            detail_id = item.get("item_id")
            code = str(item.get("code") or item.get("suggested_account_code", "")).strip()
            item_type = item.get("type", "Account")

            try:
                detail_id = int(detail_id)
            except (TypeError, ValueError):
                warnings.append(f"detail_id inválido: {detail_id!r}")
                continue

            if detail_id not in valid_detail_ids:
                warnings.append(f"detail_id {detail_id} no existe en el documento")
                continue

            # Se valida contra las cuentas candidatas, no contra el catálogo completo: así
            # un código imputable a un pasivo o a un ingreso queda descartado aunque exista
            # en el PUC del cliente. Es la contraparte del filtro aplicado al prompt.
            #
            # Sin `puc_index` en la condición: un conjunto de candidatas vacío significa que
            # ninguna cuenta del catálogo puede recibir el ítem, no que se pueda aceptar
            # cualquier cosa. La versión anterior desactivaba la validación en ese caso, que
            # es justo cuando más falta hace.
            if code not in puc_index:
                warnings.append(
                    f"Código {code!r} no es una cuenta válida para un ítem de compra "
                    f"(debe ser imputable de clase 1, 5, 6 o 7) — detalle {detail_id} omitido"
                )
                continue

            if item_type not in ("Account", "Product", "FixedAsset"):
                item_type = "Account"

            valid.append({"detail_id": detail_id, "code": code, "type": item_type})

        return valid, warnings
