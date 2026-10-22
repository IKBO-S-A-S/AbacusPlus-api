"""Validación de asignaciones `type="Product"`: se comprueban contra el catálogo de productos."""

from app.domain.services.account_assignment import validate_assignments

PUC = {"511500": {"is_active": True, "accepts_movements": True}}
PRODUCTOS = {
    "P-001": {"is_active": True},
    "P-OFF": {"is_active": False},
}


def _validar(*asignaciones, product_index=PRODUCTOS, enforce_item_class=True):
    return validate_assignments(
        assignments=list(asignaciones),
        own_detail_ids={1, 2},
        puc_index=PUC,
        valid_cost_center_ids=set(),
        enforce_item_class=enforce_item_class,
        product_index=product_index,
    )


def test_producto_activo_se_acepta_sin_aplicar_reglas_de_clase_puc():
    # «P-001» no empieza por 5/6/7: con las reglas de cuenta se rechazaría.
    salida = _validar({"detail_id": 1, "code": "P-001", "type": "Product"})
    assert len(salida.accepted) == 1
    assert salida.rejected == []


def test_producto_inexistente_se_rechaza():
    salida = _validar({"detail_id": 1, "code": "P-999", "type": "Product"})
    assert salida.accepted == []
    assert "no existe en el catálogo de productos" in salida.rejected[0].reason


def test_producto_inactivo_se_rechaza():
    salida = _validar({"detail_id": 1, "code": "P-OFF", "type": "Product"})
    assert salida.accepted == []
    assert "inactivo" in salida.rejected[0].reason


def test_catalogo_de_productos_vacio_no_desactiva_la_comprobacion():
    """A diferencia del PUC: sin catálogo no hay forma de saber que el código es un producto."""
    salida = _validar({"detail_id": 1, "code": "P-001", "type": "Product"}, product_index={})
    assert salida.accepted == []


def test_un_codigo_de_producto_no_se_acepta_como_cuenta():
    salida = _validar({"detail_id": 1, "code": "P-001", "type": "Account"})
    assert salida.accepted == []
    assert "catálogo PUC" in salida.rejected[0].reason


def test_mezcla_de_producto_y_cuenta_en_el_mismo_documento():
    salida = _validar(
        {"detail_id": 1, "code": "P-001", "type": "Product"},
        {"detail_id": 2, "code": "511500", "type": "Account"},
    )
    assert [a["detail_id"] for a in salida.accepted] == [1, 2]


def test_linea_ajena_al_documento_se_rechaza_tambien_siendo_producto():
    salida = _validar({"detail_id": 99, "code": "P-001", "type": "Product"})
    assert salida.accepted == []
    assert "no pertenece" in salida.rejected[0].reason


def test_limpiar_el_codigo_de_un_producto_es_legitimo():
    salida = _validar({"detail_id": 1, "code": None, "type": "Product"})
    assert len(salida.accepted) == 1
