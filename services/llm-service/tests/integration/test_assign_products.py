"""Asignación por producto o por cuenta según el tercero.

Reglas que se verifican:

- Tercero en `account`: se ignora por completo la detección de productos (ni se consulta el
  catálogo ni se pregunta nada al modelo sobre productos).
- Tercero en `product` o sin definir: las líneas que el modelo reconoce como un producto del
  catálogo van como `Product`; las demás conservan su cuenta, así que un documento puede mezclar.
- El modelo solo puede elegir entre los candidatos de cada línea.
- El xml-processor solo puede fijar el modo de un tercero sin definir cuando la detección corrió
  de verdad: una caída del catálogo o del modelo no concluye nada.
"""

import json

import pytest

from app.application.use_cases.assign_account_codes import (
    _PRODUCT_SYSTEM_PROMPT,
    AssignAccountCodesUseCase,
)

_PUC = [{"code": "613505", "name": "Comercio", "account_type": "Costos de venta"}]

_CATALOGO = [
    {"code": "P-001", "description": "Tornillo hexagonal 1/4 acero", "type": "product"},
    {"code": "P-002", "description": "Cable eléctrico calibre 12", "type": "product"},
]


class _Doc:
    def __init__(self, details, issuer):
        self._details = details
        self._issuer = issuer
        self.patched: list[dict] = []
        self.detection_ran = None

    async def get_document_full(self, document_id):
        return {
            "id": document_id,
            "issuer_name": "FERRETERIA SAS",
            "issuer_nit": "900",
            "details": self._details,
        }

    async def get_issuer(self, nit):
        return self._issuer

    async def patch_detail_codes(self, document_id, assignments, product_detection_ran=False):
        self.patched = assignments
        self.detection_ran = product_detection_ran
        return len(assignments)


class _Config:
    def __init__(self, productos):
        self._productos = productos
        self.products_calls = 0

    async def get_chart_accounts(self):
        return _PUC

    async def get_cost_centers(self):
        return []

    async def get_products(self):
        self.products_calls += 1
        return self._productos


class _AI:
    """Responde cuentas al prompt de cuentas y `elecciones` al de productos."""

    def __init__(self, elecciones=None, falla_productos=False):
        self._elecciones = elecciones or {}
        self._falla_productos = falla_productos
        self.product_prompts: list[dict] = []

    async def complete(self, prompt, system_prompt=None):
        if system_prompt == _PRODUCT_SYSTEM_PROMPT:
            if self._falla_productos:
                raise RuntimeError("timeout")
            data = json.loads(prompt)
            self.product_prompts.append(data)
            items = [
                {"item_id": i["item_id"], "product_code": self._elecciones.get(i["item_id"])}
                for i in data["items"]
            ]
            return {"content": json.dumps({"items": items})}
        payload, _ = AssignAccountCodesUseCase.split_prompt(prompt)
        items = [
            {"item_id": i["item_id"], "suggested_account_code": "613505"}
            for i in payload["items"]
        ]
        return {"content": json.dumps({"items": items})}


class _PromptRepo:
    def get_active(self):
        return None


def _detail(detail_id, description):
    return {
        "id": detail_id,
        "description": description,
        "quantity": 10,
        "unit": "UND",
        "price": 100,
        "subtotal": 1000,
        "total": 1000,
    }


def _caso(details, issuer, productos=_CATALOGO, ai=None):
    doc = _Doc(details, issuer)
    config = _Config(productos)
    ai = ai or _AI()
    use_case = AssignAccountCodesUseCase(
        ai_service=ai,
        document_client=doc,
        integration_config_client=config,
        system_prompt_repo=_PromptRepo(),
    )
    return use_case, doc, config, ai


def _tercero(modo=None, historial=None):
    return {"nit": "900", "item_assignment_mode": modo, "product_history": historial or []}


@pytest.mark.asyncio
async def test_tercero_en_cuenta_ignora_la_deteccion_de_productos():
    use_case, doc, config, ai = _caso(
        [_detail(1, "Tornillo hexagonal 1/4")], _tercero("account"), ai=_AI({1: "P-001"})
    )

    await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]
    assert config.products_calls == 0, "ni siquiera se consulta el catálogo"
    assert ai.product_prompts == []
    assert doc.detection_ran is False


@pytest.mark.asyncio
async def test_documento_mixto_producto_donde_casa_y_cuenta_en_el_resto():
    use_case, doc, _, ai = _caso(
        [_detail(1, "Tornillo hexagonal 1/4 acero"), _detail(2, "Servicio de instalación")],
        _tercero("product"),
        ai=_AI({1: "P-001"}),
    )

    await use_case.execute(document_id=1)

    por_linea = {a["detail_id"]: a for a in doc.patched}
    assert por_linea[1] == {"detail_id": 1, "code": "P-001", "type": "Product"}
    assert por_linea[2] == {"detail_id": 2, "code": "613505", "type": "Account"}
    # La línea 2 no tiene ningún parecido con el catálogo: ni siquiera se le pregunta al modelo.
    assert [i["item_id"] for i in ai.product_prompts[0]["items"]] == [1]


@pytest.mark.asyncio
async def test_el_modelo_dice_que_no_es_producto_y_la_linea_queda_por_cuenta():
    use_case, doc, *_ = _caso(
        [_detail(1, "Flete tornillo hexagonal")], _tercero("product"), ai=_AI({1: None})
    )

    await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]


@pytest.mark.asyncio
async def test_codigo_que_no_es_candidato_de_la_linea_se_descarta():
    # P-002 existe en el catálogo pero no se parece a esta línea: no se le ofreció.
    use_case, doc, *_ = _caso(
        [_detail(1, "Tornillo hexagonal 1/4")], _tercero("product"), ai=_AI({1: "P-002"})
    )

    await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]


@pytest.mark.asyncio
async def test_sin_definir_con_producto_detectado_avisa_que_la_deteccion_corrio():
    use_case, doc, *_ = _caso(
        [_detail(1, "Tornillo hexagonal 1/4")], _tercero(None), ai=_AI({1: "P-001"})
    )

    await use_case.execute(document_id=1)

    assert doc.patched[0]["type"] == "Product"
    assert doc.detection_ran is True


@pytest.mark.asyncio
async def test_sin_definir_y_catalogo_vacio_va_por_cuenta_y_la_deteccion_corrio():
    use_case, doc, _, ai = _caso([_detail(1, "Tornillo")], _tercero(None), productos=[])

    await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]
    assert ai.product_prompts == []
    assert doc.detection_ran is True


@pytest.mark.asyncio
async def test_catalogo_inaccesible_no_concluye_nada():
    """`None` = no se pudo consultar: distinto de «no hay productos»."""
    use_case, doc, *_ = _caso([_detail(1, "Tornillo")], _tercero(None), productos=None)

    await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]
    assert doc.detection_ran is False


@pytest.mark.asyncio
async def test_fallo_del_modelo_de_productos_no_tumba_las_cuentas_ni_concluye_nada():
    use_case, doc, *_ = _caso(
        [_detail(1, "Tornillo hexagonal 1/4")],
        _tercero(None),
        ai=_AI({1: "P-001"}, falla_productos=True),
    )

    resultado = await use_case.execute(document_id=1)

    assert doc.patched == [{"detail_id": 1, "code": "613505", "type": "Account"}]
    assert doc.detection_ran is False
    assert resultado["assigned"] == 1


@pytest.mark.asyncio
async def test_sin_datos_del_tercero_no_se_buscan_productos():
    """Si no se pudo consultar al tercero podría estar forzado a cuenta: no se arriesga."""
    use_case, doc, config, _ = _caso([_detail(1, "Tornillo hexagonal")], None)

    await use_case.execute(document_id=1)

    assert doc.patched[0]["type"] == "Account"
    assert config.products_calls == 0
    assert doc.detection_ran is False


@pytest.mark.asyncio
async def test_el_historial_del_tercero_aporta_candidatos_con_su_forma_de_llamar_al_articulo():
    historial = [{"code": "P-002", "uses": 4, "descriptions": ["Alambre cobre rollo"]}]
    use_case, _, _, ai = _caso(
        [_detail(1, "Alambre cobre rollo 100m")], _tercero("product", historial), ai=_AI({1: "P-002"})
    )

    await use_case.execute(document_id=1)

    candidatos = ai.product_prompts[0]["items"][0]["candidatos"]
    assert [c["codigo"] for c in candidatos] == ["P-002"]
