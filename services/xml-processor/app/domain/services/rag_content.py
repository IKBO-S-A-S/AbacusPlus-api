"""Construcción del texto que se indexa en el RAG para un documento persistido.

El chunk es lo que el RAG recupera para "aprender" de documentos anteriores: por eso incluye
las DECISIONES confirmadas por el contador —la cuenta contable (PUC) asignada a cada concepto
y las retenciones aplicadas con su porcentaje—, no solo la cabecera de la factura. Así, ante
un nuevo documento del mismo tercero, la búsqueda semántica devuelve "para el emisor X se usó
la cuenta Y en el concepto Z y se aplicó ReteFuente 1%", que es lo que alimenta las sugerencias.

Es una función pura sobre objetos con atributos (documento, líneas, retenciones); no importa el
ORM ni la sesión, para poder reutilizarla desde el reindexado y desde el hook de aprobación.
"""

from typing import Any, Iterable, Mapping, Optional


def _fmt_pct(value) -> str:
    """Porcentaje legible: '1' en vez de '1.0', '3.5' en vez de '3.5000'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{num:g}"


def build_document_chunk_content(
    document,
    taxes: Iterable = (),
    tax_name_map: Optional[Mapping[int, str]] = None,
) -> str:
    """Texto indexable de un documento con sus decisiones contables confirmadas.

    - `document`: objeto con atributos de cabecera y `details` (cada uno con `description`,
      `code`, `quantity`, `price`).
    - `taxes`: retenciones aplicadas (`tax_id`, `percentage`, `taxable_base`, `value`).
    - `tax_name_map`: `tax_id -> nombre` del catálogo, para nombrar la retención.
    """
    names = tax_name_map or {}
    lines = [
        f"Factura {document.document_number} del {document.date}",
        f"Emisor: {document.issuer_name} NIT {document.issuer_nit}",
        f"Receptor: {document.receiver_name} NIT {document.receiver_nit}",
        f"Moneda: {document.currency} | Subtotal: {document.subtotal} | "
        f"Impuestos: {document.total_taxes} | Total: {document.total}",
        "Items (concepto y cuenta contable asignada):",
    ]
    for item in document.details:
        cuenta = item.code or "sin asignar"
        lines.append(
            f"  - {item.description} | cuenta: {cuenta} | "
            f"cant: {item.quantity} | precio: {item.price}"
        )

    tax_list = list(taxes)
    if tax_list:
        lines.append("Retenciones aplicadas:")
        for t in tax_list:
            name = names.get(t.tax_id, f"Impuesto #{t.tax_id}")
            pct = _fmt_pct(t.percentage)
            # El nombre del catálogo a veces ya trae la tasa, con o sin el signo (p. ej.
            # "Retefuente 10%" o "ReteICA 11.04"). Si el número ya aparece, no se repite; así
            # se evita "ReteICA 11.04 11.04%". Si no aparece, se añade la tasa con su signo.
            etiqueta = name if pct in name else f"{name} {pct}%"
            lines.append(f"  - {etiqueta} (base {t.taxable_base}, valor {t.value})")
    return "\n".join(lines)


# ── RF-08: conocimiento de una causación contabilizada ────────────────────────


def build_accounted_knowledge_content(
    document,
    taxes: Iterable = (),
    tax_name_map: Optional[Mapping[int, str]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    siigo_id: Optional[str] = None,
    siigo_name: Optional[str] = None,
    cost_center_name_map: Optional[Mapping[int, str]] = None,
) -> str:
    """Texto indexable de una causación EFECTIVAMENTE CONTABILIZADA en SIIGO (RF-08).

    Se diferencia de `build_document_chunk_content` en dos cosas, y ambas son la razón de que
    exista por separado:

    1. **De dónde salen los datos.** Lo que se indexa es el `payload` que se envió a SIIGO,
       no el estado que tenía el documento cuando la IA lo propuso. Entre una cosa y otra
       está la corrección del contador, que es justamente lo que da valor al precedente: si
       se indexara la sugerencia inicial, el sistema aprendería de sí mismo y repetiría sus
       propios errores. Los campos que el payload no lleva —la base gravable y la tarifa de
       cada retención, que SIIGO calcula por su cuenta a partir del `retention_id`— se toman
       de `document_taxes`, que es lo que el contador confirmó y lo que sustenta el cálculo.

    2. **Qué se escribe.** El texto nombra explícitamente cada elemento que RF-08 exige como
       conocimiento reutilizable (tipo de retención, concepto, base, tarifa, valor, cuenta,
       tercero, centro de costo) y añade una línea de rasgos de identificación, para que la
       búsqueda semántica pueda emparejar un documento posterior parecido aunque el texto de
       sus ítems no coincida palabra por palabra.

    Es una función pura sobre objetos con atributos; no conoce el ORM ni la sesión.
    """
    payload = payload or {}
    names = tax_name_map or {}
    centros = cost_center_name_map or {}

    lines: list[str] = [
        "CAUSACIÓN CONTABILIZADA (conocimiento validado)",
        f"Documento DIAN {document.document_number} del {document.date}",
    ]
    if siigo_id:
        etiqueta = f" ({siigo_name})" if siigo_name else ""
        lines.append(f"Contabilizada en SIIGO con el comprobante {siigo_id}{etiqueta}")

    lines += [
        "Tercero (proveedor): " f"{document.issuer_name} NIT {document.issuer_nit}",
        f"Empresa: {document.receiver_name} NIT {document.receiver_nit}",
        f"Moneda: {document.currency} | Subtotal: {document.subtotal} | "
        f"Impuestos: {document.total_taxes} | Total: {document.total}",
    ]

    cost_center = payload.get("cost_center") or getattr(document, "cost_center_id", None)
    if cost_center:
        nombre = centros.get(cost_center)
        lines.append(f"Centro de costo: {cost_center}{f' — {nombre}' if nombre else ''}")

    payment_id = payload.get("payment_id") or getattr(document, "payment_type_id", None)
    if payment_id:
        lines.append(f"Forma de pago: {payment_id}")

    # Las líneas se leen del payload cuando está disponible: es la imputación tal y como la
    # recibió SIIGO, con la cuenta ya definitiva. La descripción se conserva porque es el
    # texto por el que un documento posterior se parecerá a este.
    items = list(payload.get("items") or [])
    lines.append("Imputación contable (concepto → cuenta o producto):")
    if items:
        for item in items:
            lines.append(
                f"  - {item.get('description', '')} | "
                f"{_rotulo(item.get('type'))}: {item.get('code', 'sin asignar')} "
                f"| cant: {item.get('quantity', '')} | precio: {item.get('price', '')}"
            )
    else:
        for item in document.details:
            lines.append(
                f"  - {item.description} | {_rotulo(getattr(item, 'type', None))}: "
                f"{item.code or 'sin asignar'} | cant: {item.quantity} | precio: {item.price}"
            )

    tax_list = [t for t in taxes if float(getattr(t, "value", 0) or 0) != 0]
    if tax_list:
        lines.append("Retenciones practicadas:")
        for t in tax_list:
            name = names.get(t.tax_id, f"Impuesto #{t.tax_id}")
            pct = _fmt_pct(t.percentage)
            lines.append(
                f"  - tipo/concepto: {name} | tarifa: {pct}% | base gravable: {t.taxable_base} "
                f"| valor retenido: {t.value} | id SIIGO: {t.tax_id}"
            )
    else:
        # Que no se practicara ninguna retención también es conocimiento: evita que el
        # modelo proponga retenciones a un tercero al que esta empresa nunca se las practica.
        lines.append("Retenciones practicadas: ninguna.")

    # Rasgos con los que emparejar documentos posteriores parecidos.
    cuentas, productos = _codigos_por_tipo(items, document)
    productos_rasgo = f"productos {', '.join(productos)}; " if productos else ""
    retenciones_rasgo = (
        f"retenciones {', '.join(names.get(t.tax_id, str(t.tax_id)) for t in tax_list) or 'ninguna'}."
    )
    lines.append(
        "Rasgos para identificar documentos similares: "
        f"proveedor {document.issuer_name} (NIT {document.issuer_nit}); "
        f"cuentas {', '.join(cuentas) or 'sin asignar'}; "
        f"{productos_rasgo}{retenciones_rasgo}"
    )
    return "\n".join(lines)


def build_accounted_knowledge_signature(
    document,
    taxes: Iterable = (),
    tax_name_map: Optional[Mapping[int, str]] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> str:
    """Firma del caso: el texto que se EMBEBE, sin la plantilla que comparten todos.

    `build_accounted_knowledge_content` escribe el texto que el modelo LEE, y para eso necesita
    encabezados, rótulos y los datos que sitúan el documento («CAUSACIÓN CONTABILIZADA», la
    razón social de la propia empresa, el comprobante de SIIGO, la moneda). Todo eso es
    idéntico en cada causación.

    Embeber ese texto mide, sobre todo, el parecido de la plantilla. Medido sobre el corpus
    real del cliente: la similitud coseno entre CUALQUIER par de causaciones validadas daba
    0.94 de mediana, y solo 0.06 separaban a un documento de su vecino más cercano. Con esa
    escala, un proveedor de servicios de aseo y una factura de telecomunicaciones son
    prácticamente el mismo vector: ni el orden ni el umbral pueden discriminar nada.

    Esta firma deja únicamente lo que cambia de un caso a otro y decide la causación:
    el proveedor, los conceptos facturados, las cuentas a las que se imputaron y las
    retenciones que se practicaron. Sin encabezados, sin la empresa propia —que es siempre la
    misma— y sin identificadores de comprobante, que son irrepetibles por definición y solo
    añaden ruido.

    El texto completo se sigue guardando en `content` y es el que llega al prompt: cambia con
    qué se BUSCA, no qué se lee.
    """
    payload = payload or {}
    names = tax_name_map or {}

    partes: list[str] = [
        f"Proveedor: {document.issuer_name} NIT {_clean_nit(document.issuer_nit)}",
    ]

    items = list(payload.get("items") or [])
    conceptos = (
        [(i.get("description") or "", i.get("code") or "", i.get("type")) for i in items]
        if items
        else [(d.description or "", d.code or "", getattr(d, "type", None)) for d in document.details]
    )
    if conceptos:
        partes.append(
            "Conceptos: "
            + "; ".join(
                f"{desc} → {_rotulo(tipo)} {code or 'sin asignar'}" for desc, code, tipo in conceptos
            )
        )

    tax_list = [t for t in taxes if float(getattr(t, "value", 0) or 0) != 0]
    if tax_list:
        partes.append(
            "Retenciones: "
            + "; ".join(
                f"{names.get(t.tax_id, str(t.tax_id))} al {_fmt_pct(t.percentage)}%"
                for t in tax_list
            )
        )
    else:
        # «Ninguna» es un rasgo tan discriminante como cualquier retención: separa a los
        # proveedores a los que esta empresa nunca les retiene de los que sí.
        partes.append("Retenciones: ninguna")

    return "\n".join(partes)


def build_accounted_knowledge_metadata(
    document,
    taxes: Iterable = (),
    tax_name_map: Optional[Mapping[int, str]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    municipality_code: Optional[str] = None,
) -> dict[str, Any]:
    """Rasgos estructurados del caso contabilizado, para la búsqueda híbrida de RF-08.

    Ninguno de estos datos es nuevo: todos salen del documento, de sus retenciones o del
    payload que se envió a SIIGO. Se extraen aparte del texto porque cumplen una función
    distinta. El texto sirve para la similitud semántica —que capta el parecido de conceptos
    y descripciones—, y estos campos sirven para el **filtro**: dicen de qué proveedor es el
    caso, en qué municipio, con qué cuentas y qué retenciones se practicaron.

    La distinción importa porque un embedding no sabe qué es un NIT. Dos facturas del mismo
    proveedor y el mismo concepto son el precedente que se busca aunque no compartan una
    palabra; y dos textos casi idénticos de proveedores con régimen distinto llevan a
    retenciones distintas. Buscar solo por texto mezcla esos dos casos; filtrar primero por
    estos rasgos y ordenar por similitud dentro del resultado, no.

    Los valores se guardan como texto porque el filtro del rag-service compara sobre JSONB
    con `->>`, que devuelve texto: guardar un número obligaría a convertir en cada consulta.
    """
    payload = payload or {}
    names = tax_name_map or {}
    tax_list = [t for t in taxes if float(getattr(t, "value", 0) or 0) != 0]

    items = list(payload.get("items") or [])
    # `account_codes` es solo de cuentas PUC: un código de producto ahí lo haría pasar por una
    # cuenta ante cualquier filtro que compare contra el plan de cuentas. Los productos van en
    # su propio rasgo.
    cuentas, productos = _codigos_por_tipo(items, document)
    if items:
        conceptos = [str(i.get("description") or "").strip() for i in items]
    else:
        conceptos = [str(d.description or "").strip() for d in document.details]

    metadata: dict[str, Any] = {
        # El identificador del tercero es el filtro más discriminante de todos: la retención
        # depende de su régimen y sus responsabilidades, no de cómo redacte sus facturas.
        "issuer_nit": _clean_nit(document.issuer_nit),
        "issuer_name": str(document.issuer_name or "").strip(),
        "document_type": str(getattr(document, "document_type", "") or "").strip(),
        "account_codes": cuentas,
        "product_codes": productos,
        "concepts": [c for c in conceptos if c][:20],
        # Tipos de retención practicados, normalizados. Permite preguntar «¿a este proveedor
        # se le ha practicado ReteICA alguna vez?» sin leer el texto de cada caso.
        "retention_types": sorted(
            {_retention_type(names.get(t.tax_id, "")) for t in tax_list} - {""}
        ),
        "retention_tax_ids": sorted({str(t.tax_id) for t in tax_list}),
        "retention_count": str(len(tax_list)),
        "subtotal": str(document.subtotal or 0),
        "total": str(document.total or 0),
        "has_iva": "true" if float(document.total_taxes or 0) > 0 else "false",
        "date": str(document.date or ""),
    }

    cost_center = payload.get("cost_center") or getattr(document, "cost_center_id", None)
    if cost_center:
        metadata["cost_center_id"] = str(cost_center)
    # El municipio no está en el documento de la DIAN: lo aporta quien publica el
    # conocimiento, a partir de las tarifas de ReteICA configuradas.
    if municipality_code:
        metadata["municipality_code"] = str(municipality_code)
    return metadata


def _rotulo(tipo) -> str:
    """Cómo se llama el código de una línea en el texto: `producto` o `cuenta`."""
    return "producto" if tipo == "Product" else "cuenta"


def _codigos_por_tipo(items: list, document) -> tuple[list, list]:
    """Códigos de las líneas separados en cuentas PUC y productos, sin repetir y ordenados.

    Lee del payload enviado a SIIGO cuando lo hay (la imputación definitiva) y, si no, de las
    líneas del documento.
    """
    if items:
        pares = [(i.get("code"), i.get("type")) for i in items]
    else:
        pares = [(d.code, getattr(d, "type", None)) for d in document.details]
    cuentas = {str(c).strip() for c, t in pares if c and t != "Product"}
    productos = {str(c).strip() for c, t in pares if c and t == "Product"}
    return sorted(cuentas), sorted(productos)


def _clean_nit(nit) -> str:
    """NIT sin dígito de verificación ni separadores.

    Se normaliza porque el mismo proveedor llega como '900123456' o '900123456-7' según el
    emisor, y dos formas del mismo NIT partirían en dos el historial de un tercero: la mitad
    de sus precedentes dejaría de encontrarse justo cuando más falta hace.
    """
    if not nit:
        return ""
    limpio = str(nit).strip().replace(".", "").replace(" ", "")
    return limpio.split("-", 1)[0] if "-" in limpio else limpio


def _retention_type(tax_name: str) -> str:
    """Clasifica una retención por su nombre de catálogo: retefuente | reteica | reteiva.

    El catálogo sincronizado desde SIIGO no trae un campo de tipo, solo el nombre
    («ReteICA 9.66», «Retefuente 2.5%»), así que el tipo se deduce de ahí. Es la misma
    convención que ya usa el llm-service para agrupar candidatas.
    """
    nombre = (tax_name or "").strip().lower().replace(" ", "")
    if "reteiva" in nombre:
        return "reteiva"
    if "reteica" in nombre or nombre[:3] == "ica":
        return "reteica"
    if "retefuente" in nombre or "retefte" in nombre:
        return "retefuente"
    return ""
