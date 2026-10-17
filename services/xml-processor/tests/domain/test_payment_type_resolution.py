from app.domain.services.payment_type_resolution import (
    SOURCE_ISSUER,
    SOURCE_ISSUER_HISTORY,
    SOURCE_SIMILAR_DOCUMENTS,
    resolve_payment_type,
)


def _boom():
    raise AssertionError("no debía consultarse el historial")


def test_campo_del_tercero_manda_y_no_consulta_historial():
    r = resolve_payment_type(3, _boom, _boom)
    assert (r.payment_type_id, r.source) == (3, SOURCE_ISSUER)
    assert r.persist_on_issuer is False


def test_historial_del_tercero_se_guarda_en_el_tercero():
    r = resolve_payment_type(None, lambda: 5, _boom)
    assert (r.payment_type_id, r.source) == (5, SOURCE_ISSUER_HISTORY)
    assert r.persist_on_issuer is True


def test_documentos_similares_no_se_guardan_en_el_tercero():
    r = resolve_payment_type(None, lambda: None, lambda: 7)
    assert (r.payment_type_id, r.source) == (7, SOURCE_SIMILAR_DOCUMENTS)
    assert r.persist_on_issuer is False


def test_sin_nada_devuelve_none():
    assert resolve_payment_type(None, lambda: None, lambda: None) is None
