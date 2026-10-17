import pytest

from app.adapters.api.error_handlers import STATUS_MAP
from app.dependencies import get_openai_service
from app.domain.exceptions.base import AIServiceNotConfiguredError


@pytest.mark.parametrize("valor", [None, "", "   "])
def test_sin_api_key_falla_con_error_de_configuracion(monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", valor)
    monkeypatch.setattr("app.dependencies.load_dotenv", lambda *a, **k: None, raising=False)

    with pytest.raises(AIServiceNotConfiguredError) as exc:
        get_openai_service()

    assert exc.value.code == "AI_NOT_CONFIGURED"
    assert "OPENAI_API_KEY" in exc.value.message


def test_con_api_key_construye_el_servicio(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert get_openai_service() is not None


def test_errores_de_ia_se_mapean_a_503():
    assert STATUS_MAP["AI_NOT_CONFIGURED"] == 503
    assert STATUS_MAP["AI_AUTHENTICATION_FAILED"] == 503
