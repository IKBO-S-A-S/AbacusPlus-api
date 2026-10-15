"""Turnstile resuelto en el navegador (mundo principal) y su salida de emergencia.

patchright ejecuta `evaluate` en un contexto aislado por defecto; ahí no existe el
`window.turnstile` de la página y el solver respondía siempre NO_TURNSTILE, de modo que todos
los tokens se pagaban en CapSolver. Se pide ahora el mundo principal. Como nada garantiza que
DIAN acepte esos tokens, hay un corte: tras dos descargas fallidas seguidas con un token del
navegador, se vuelve a CapSolver.
"""

import pytest
from app.infrastructure.browser import playwright_client as pc
from app.infrastructure.browser.playwright_client import BrowserDownloadSession


class _Page:
    url = "https://portal.example.com/Document/Received"

    def __init__(self, resultado="tok-navegador"):
        self.resultado = resultado
        self.llamadas = []

    async def wait_for_function(self, *a, **k):
        return None

    async def evaluate(self, js, arg=None, isolated_context=True):
        self.llamadas.append({"arg": arg, "isolated_context": isolated_context})
        return self.resultado


def _sesion(page=None):
    s = BrowserDownloadSession("https://portal.example.com/User/AuthToken")
    s._page = page or _Page()
    return s


class TestMundoPrincipal:
    @pytest.mark.asyncio
    async def test_por_defecto_el_solver_corre_en_el_mundo_principal(self, monkeypatch):
        monkeypatch.delenv("TURNSTILE_MAIN_WORLD", raising=False)
        page = _Page()

        token = await _sesion(page)._turnstile_inbrowser()

        assert token == "tok-navegador"
        assert page.llamadas[0]["isolated_context"] is False

    @pytest.mark.asyncio
    async def test_se_puede_volver_al_contexto_aislado_con_la_variable(self, monkeypatch):
        monkeypatch.setenv("TURNSTILE_MAIN_WORLD", "false")
        page = _Page()

        await _sesion(page)._turnstile_inbrowser()

        assert page.llamadas[0]["isolated_context"] is True

    @pytest.mark.asyncio
    async def test_no_turnstile_sigue_sin_dar_token(self):
        assert await _sesion(_Page(resultado="NO_TURNSTILE"))._turnstile_inbrowser() is None


class TestCorteDeEmergencia:
    def test_dos_fallos_seguidos_con_token_del_navegador_lo_desactivan(self):
        s = _sesion()
        s._captcha_source, s._inbrowser_works = "inbrowser", True

        s._note_download_outcome(False)
        assert s._inbrowser_works is True
        s._note_download_outcome(False)

        assert s._inbrowser_works is False

    def test_un_exito_reinicia_la_cuenta(self):
        s = _sesion()
        s._captcha_source, s._inbrowser_works = "inbrowser", True

        s._note_download_outcome(False)
        s._note_download_outcome(True)
        s._note_download_outcome(False)

        assert s._inbrowser_works is True and s._inbrowser_rejections == 1

    def test_los_fallos_con_token_de_capsolver_no_cuentan(self):
        s = _sesion()
        s._captcha_source, s._inbrowser_works = "capsolver", None

        for _ in range(5):
            s._note_download_outcome(False)

        assert s._inbrowser_works is None and s._inbrowser_rejections == 0

    def test_registra_el_origen_del_token_en_las_metricas(self):
        s = _sesion()
        s._captcha_source = "capsolver"
        timings = {}

        s._note_download_outcome(True, timings)

        assert timings["token"] == "capsolver"

    @pytest.mark.asyncio
    async def test_desactivado_el_navegador_el_siguiente_token_sale_de_capsolver(self, monkeypatch):
        s = _sesion()
        s._captcha_source, s._inbrowser_works = "inbrowser", True
        s._note_download_outcome(False)
        s._note_download_outcome(False)

        async def _capsolver():
            return "tok-capsolver"

        async def _no_debe_llamarse():
            raise AssertionError("no debía usarse el navegador")

        monkeypatch.setattr(pc, "CAPSOLVER_API_KEY", "k")
        s._turnstile_capsolver = _capsolver
        s._turnstile_inbrowser = _no_debe_llamarse

        assert await s._solve_turnstile() == "tok-capsolver"
        assert s._captcha_source == "capsolver"
