"""Qué modo de asignación (cuenta o producto) se fija en un tercero tras una asignación del modelo."""

import pytest

from app.domain.services.item_assignment_mode import modo_a_fijar


@pytest.mark.parametrize("modo", ["account", "product"])
def test_un_modo_ya_definido_nunca_se_toca(modo):
    assert modo_a_fijar(modo, ["Product", "Account"], True) is None
    assert modo_a_fijar(modo, ["Account"], True) is None


def test_sin_definir_con_algun_producto_pasa_a_product():
    assert modo_a_fijar(None, ["Account", "Product", "Account"], True) == "product"


def test_sin_definir_y_ningun_producto_pasa_a_account():
    assert modo_a_fijar(None, ["Account", "Account"], True) == "account"


def test_sin_definir_si_la_deteccion_no_corrio_no_se_fija_nada():
    """Una caída del catálogo no debe quedar como «este tercero no vende productos»."""
    assert modo_a_fijar(None, ["Account", "Account"], False) is None
    assert modo_a_fijar(None, ["Product"], False) is None


def test_sin_lineas_asignadas_no_hay_evidencia():
    assert modo_a_fijar(None, [], True) is None
