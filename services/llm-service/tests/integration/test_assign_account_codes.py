"""
RF-04 · Protección de cambios manuales · RF-08 · Bloqueo sin PUC.

Cubre las dos reglas que gobiernan la sugerencia de cuentas del LLM:

- RF-04: una línea cuya cuenta fue editada a mano (`code_source == "manual"`) no se envía
  al modelo salvo que el contador confirme sobrescribirla (`overwrite_manual=True`).
- RF-08 (regla de negocio crítica): sin Plan Único de Cuentas cargado el proceso se detiene
  con el mensaje textual «No tienes un plan único de cuenta», en lugar de dejar que el
  modelo sugiera cuentas de un PUC colombiano genérico.
"""

import json

import pytest
from app.application.use_cases.assign_account_codes import AssignAccountCodesUseCase
from app.domain.exceptions.base import NoChartOfAccountsError

_PUC = [
    {
        "code": "613505",
        "name": "Comercio al por mayor y al por menor",
        "account_type": "Costos de venta",
    },
    {"code": "618001", "name": "Servicios", "account_type": "Costos de venta"},
]


class _FakeDocumentClient:
    """Registra las asignaciones enviadas para poder afirmar sobre ellas."""

    def __init__(self, details):
        self._details = details
        self.patched: list[dict] = []

    async def get_document_full(self, document_id):
        return {"id": document_id, "issuer_name": "PROVEEDOR SAS", "details": self._details}

    async def get_issuer(self, nit):
        # Sin datos del tercero la detección de productos no corre: el flujo es el de cuentas.
        return None

    async def patch_detail_codes(self, document_id, assignments, product_detection_ran=False):
        self.patched = assignments
        return len(assignments)


class _FakeIntegrationConfigClient:
    def __init__(self, chart_accounts, cost_centers=None):
        self._chart_accounts = chart_accounts
        self._cost_centers = cost_centers or []

    async def get_chart_accounts(self):
        return self._chart_accounts

    async def get_cost_centers(self):
        return self._cost_centers

    async def get_products(self):
        return None


class _FakeAI:
    """Devuelve una asignación válida para cada línea que reciba en el prompt."""

    def __init__(self, code="613505"):
        self._code = code
        self.prompts: list[str] = []

    async def complete(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        payload, _ = AssignAccountCodesUseCase.split_prompt(prompt)
        assignments = [
            {
                "item_id": item["item_id"],
                "suggested_account_code": self._code,
                "suggested_account_name": "Comercio al por mayor y al por menor",
            }
            for item in payload["items"]
        ]
        return {"content": json.dumps({"assignments": assignments})}


class _FakeSystemPromptRepo:
    def get_active(self):
        return None


def _detail(detail_id: int, code=None, code_source=None):
    return {
        "id": detail_id,
        "description": f"Ítem {detail_id}",
        "quantity": 1,
        "subtotal": 10000,
        "total": 10000,
        "code": code,
        "code_source": code_source,
    }


def _use_case(details, chart_accounts=None, ai=None):
    doc_client = _FakeDocumentClient(details)
    use_case = AssignAccountCodesUseCase(
        ai_service=ai or _FakeAI(),
        document_client=doc_client,
        integration_config_client=_FakeIntegrationConfigClient(
            _PUC if chart_accounts is None else chart_accounts
        ),
        system_prompt_repo=_FakeSystemPromptRepo(),
    )
    return use_case, doc_client


class TestNoChartOfAccountsBlocks:
    """RF-08: sin PUC no se sugiere nada."""

    @pytest.mark.asyncio
    async def test_raises_when_chart_of_accounts_is_empty(self):
        use_case, _ = _use_case([_detail(1)], chart_accounts=[])

        with pytest.raises(NoChartOfAccountsError):
            await use_case.execute(document_id=1)

    @pytest.mark.asyncio
    async def test_error_carries_the_exact_wording_from_the_scope(self):
        use_case, _ = _use_case([_detail(1)], chart_accounts=[])

        with pytest.raises(NoChartOfAccountsError) as exc:
            await use_case.execute(document_id=1)

        assert exc.value.message == "No tienes un plan único de cuenta"
        assert exc.value.code == "NO_CHART_OF_ACCOUNTS"

    @pytest.mark.asyncio
    async def test_model_is_never_called_without_puc(self):
        ai = _FakeAI()
        use_case, _ = _use_case([_detail(1)], chart_accounts=[], ai=ai)

        with pytest.raises(NoChartOfAccountsError):
            await use_case.execute(document_id=1)

        assert ai.prompts == []


class TestCandidateAccounts:
    """Solo clases 1, 5, 6 y 7 pueden recibir el ítem de una factura de compra."""

    _CATALOG = [
        {"code": "11050501", "name": "Caja general", "account_type": "Activo"},
        {"code": "130505", "name": "Clientes nacionales", "account_type": "Activo"},
        {"code": "143505", "name": "Mercancías no fabricadas", "account_type": "Activo"},
        {"code": "152405", "name": "Equipo de cómputo", "account_type": "Activo"},
        {"code": "160505", "name": "Licencias de software", "account_type": "Activo"},
        {"code": "170505", "name": "Seguros pagados por anticipado", "account_type": "Activo"},
        {"code": "233545", "name": "Retención en la fuente", "account_type": "Pasivo"},
        {"code": "310505", "name": "Capital suscrito", "account_type": "Patrimonio"},
        {"code": "413505", "name": "Comercio al por mayor", "account_type": "Ingresos"},
        {"code": "519560", "name": "Casino y restaurante", "account_type": "Gastos"},
        {"code": "613505", "name": "Comercio al por mayor", "account_type": "Costos de venta"},
        {"code": "710505", "name": "Materia prima", "account_type": "Costos de producción"},
    ]

    def _codes(self, catalog=None):
        use_case = AssignAccountCodesUseCase.__new__(AssignAccountCodesUseCase)
        return {c["code"] for c in use_case._candidate_accounts(catalog or self._CATALOG)}

    def test_keeps_expense_and_cost_classes(self):
        assert {"519560", "613505", "710505"} <= self._codes()

    def test_keeps_only_the_asset_groups_a_purchase_can_hit(self):
        assets = {c for c in self._codes() if c[0] == "1"}

        assert assets == {"143505", "152405", "160505", "170505"}

    @pytest.mark.parametrize(
        "code,motivo",
        [
            ("233545", "pasivo: es la causación de la retención, no el ítem"),
            ("310505", "patrimonio"),
            ("413505", "ingreso: es lo que la empresa factura, no lo que compra"),
            ("11050501", "efectivo: una compra no se imputa a la caja"),
            ("130505", "deudores: una compra no se imputa a una cuenta por cobrar"),
        ],
    )
    def test_excludes_accounts_that_are_never_the_item_counterpart(self, code, motivo):
        assert code not in self._codes(), f"no debe ser candidata — {motivo}"

    def test_excludes_grouping_accounts(self):
        catalog = [
            {"code": "5195", "name": "Diversos", "accepts_movements": False},
            {"code": "519560", "name": "Casino y restaurante", "accepts_movements": True},
        ]
        assert self._codes(catalog) == {"519560"}

    def test_carries_the_nature_so_the_model_can_reason(self):
        use_case = AssignAccountCodesUseCase.__new__(AssignAccountCodesUseCase)
        candidates = use_case._candidate_accounts(self._CATALOG)
        by_code = {c["code"]: c for c in candidates}

        assert by_code["519560"]["naturaleza"] == "Gastos"
        assert by_code["152405"]["naturaleza"] == "Activo"

    def test_omits_nature_when_the_catalog_lacks_it(self):
        use_case = AssignAccountCodesUseCase.__new__(AssignAccountCodesUseCase)
        candidates = use_case._candidate_accounts([{"code": "519560", "name": "Casino"}])

        assert "naturaleza" not in candidates[0]


class TestPromptContext:
    """El prompt debe llevar el contexto que un contador mira al causar."""

    def _prompt(self) -> dict:
        use_case = AssignAccountCodesUseCase.__new__(AssignAccountCodesUseCase)
        document = {
            "document_type": "Factura de venta",
            "issuer_name": "BODEGA Y COCINA SAS",
            "issuer_nit": "830044885",
            "receiver_name": "IKBO",
            "subtotal": 148600.0,
            "total_taxes": 28234.0,
            "total": 176834.0,
            "taxes": [{"taxable_base": 148600.0, "percentage": 1.0, "value": 1486.0}],
        }
        details = [
            {
                "id": 58,
                "description": "Servicio De Refrigerio IVA",
                "quantity": 6,
                "unit": "und",
                "price": 18100.0,
                "subtotal": 108600.0,
                "tax_type": "19.0",
                "tax_value": 20634.0,
                "total": 129234.0,
                "cost_center_id": 3,
            }
        ]
        raw = use_case._build_prompt(
            document,
            details,
            [{"code": "519560", "name": "Casino y restaurante"}],
            {3: "2-3 — Administracion"},
        )
        payload, _ = AssignAccountCodesUseCase.split_prompt(raw)
        return payload

    def test_includes_document_context(self):
        doc = self._prompt()["documento"]

        assert doc["tipo"] == "Factura de venta"
        assert doc["emisor_razon_social"] == "BODEGA Y COCINA SAS"
        assert doc["total_iva"] == 28234.0
        # La perspectiva evita que el modelo confunda una venta del emisor con un ingreso.
        assert "compra" in doc["perspectiva"]

    def test_includes_retentions(self):
        assert self._prompt()["documento"]["retenciones"] == [
            {"base_gravable": 148600.0, "porcentaje": 1.0, "valor_retenido": 1486.0}
        ]

    def test_includes_amounts_and_cost_center_per_item(self):
        item = self._prompt()["items"][0]

        assert item["item_id"] == 58
        assert item["descripcion"] == "Servicio De Refrigerio IVA"
        assert item["cantidad"] == 6
        assert item["valor_unitario"] == 18100.0
        assert item["iva_porcentaje"] == "19.0"
        assert item["centro_costo"] == "2-3 — Administracion"

    def test_cost_center_is_null_when_the_line_has_none(self):
        use_case = AssignAccountCodesUseCase.__new__(AssignAccountCodesUseCase)
        raw = use_case._build_prompt({}, [{"id": 1, "description": "X"}], [], {})
        payload, _ = AssignAccountCodesUseCase.split_prompt(raw)

        assert payload["items"][0]["centro_costo"] is None


class TestManualEditsAreProtected:
    """RF-04: el LLM no sobrescribe una cuenta escrita por el contador."""

    @pytest.mark.asyncio
    async def test_manual_line_is_not_sent_to_the_model(self):
        details = [_detail(1, code="999999", code_source="manual"), _detail(2)]
        ai = _FakeAI()
        use_case, _ = _use_case(details, ai=ai)

        await use_case.execute(document_id=1)

        payload, _ = AssignAccountCodesUseCase.split_prompt(ai.prompts[0])
        sent_ids = {item["item_id"] for item in payload["items"]}
        assert sent_ids == {2}

    @pytest.mark.asyncio
    async def test_manual_line_is_never_patched(self):
        details = [_detail(1, code="999999", code_source="manual"), _detail(2)]
        use_case, doc_client = _use_case(details)

        await use_case.execute(document_id=1)

        patched_ids = {a["detail_id"] for a in doc_client.patched}
        assert 1 not in patched_ids
        assert patched_ids == {2}

    @pytest.mark.asyncio
    async def test_warns_the_user_that_manual_lines_were_kept(self):
        details = [_detail(1, code="999999", code_source="manual"), _detail(2)]
        use_case, _ = _use_case(details)

        result = await use_case.execute(document_id=1)

        assert any("manual" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_llm_assigned_lines_are_not_protected(self):
        """Solo «manual» protege; una cuenta puesta antes por el modelo sí se recalcula."""
        details = [_detail(1, code="618001", code_source="llm"), _detail(2)]
        use_case, doc_client = _use_case(details)

        await use_case.execute(document_id=1)

        assert {a["detail_id"] for a in doc_client.patched} == {1, 2}

    @pytest.mark.asyncio
    async def test_overwrite_manual_includes_the_protected_line(self):
        details = [_detail(1, code="999999", code_source="manual"), _detail(2)]
        use_case, doc_client = _use_case(details)

        await use_case.execute(document_id=1, overwrite_manual=True)

        assert {a["detail_id"] for a in doc_client.patched} == {1, 2}

    @pytest.mark.asyncio
    async def test_all_lines_manual_skips_the_model_entirely(self):
        details = [
            _detail(1, code="999999", code_source="manual"),
            _detail(2, code="888888", code_source="manual"),
        ]
        ai = _FakeAI()
        use_case, doc_client = _use_case(details, ai=ai)

        result = await use_case.execute(document_id=1)

        assert ai.prompts == []
        assert doc_client.patched == []
        assert result["assigned"] == 0
        assert result["skipped"] == 2

    @pytest.mark.asyncio
    async def test_totals_account_for_protected_lines(self):
        details = [_detail(1, code="999999", code_source="manual"), _detail(2), _detail(3)]
        use_case, _ = _use_case(details)

        result = await use_case.execute(document_id=1)

        # 2 asignadas por el modelo + 1 conservada = las 3 líneas del documento.
        assert result["assigned"] + result["skipped"] == 3
        assert result["assigned"] == 2
        assert result["skipped"] == 1
