import pytest
from app.infrastructure.browser import playwright_client as pc
from app.infrastructure.browser.playwright_client import (
    AZURE_WAF,
    BROWSER_CRASHED,
    EMPTY_OR_HTML,
    VALID_ZIP,
    BrowserDownloadSession,
    DianDownloadError,
)

ZIP = b"PK\x03\x04contenido"


class _Page:
    url = "https://portal.example.com/Document/Received"
    closed = False

    def is_closed(self):
        return self.closed


class FakeSession(BrowserDownloadSession):
    """Sesión sin navegador: cada estrategia devuelve lo que el test le programe."""

    def __init__(self, fetch_results, nav_results=None, max_retries=3):
        super().__init__("https://portal.example.com/User/AuthToken", max_retries=max_retries)
        self._page = _Page()
        self._fetch = list(fetch_results)
        self._nav = list(nav_results or [])
        self.tokens = 0
        self.nav_calls = 0
        self.fetch_calls = 0

    async def _solve_turnstile(self):
        self.tokens += 1
        return f"tok{self.tokens}"

    async def _strategy_nav_download(self, url):
        self.nav_calls += 1
        return self._nav.pop(0) if self._nav else (b"", EMPTY_OR_HTML)

    async def _strategy_inpage_fetch(self, url):
        self.fetch_calls += 1
        return self._fetch.pop(0)


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    async def _nada(_):
        return None

    monkeypatch.setattr(pc.asyncio, "sleep", _nada)


@pytest.mark.asyncio
async def test_primer_documento_usa_el_flujo_completo_y_ceba_la_sesion():
    s = FakeSession(fetch_results=[(ZIP, VALID_ZIP)])
    timings = {}

    assert await s.download("a", timings) == ZIP

    assert s.nav_calls == 1 and s.tokens == 2  # navegación + segundo token + fetch
    assert timings["path"] == "full"
    assert s._fast_primed is True


@pytest.mark.asyncio
async def test_con_la_sesion_cebada_va_directo_a_fetch_con_un_solo_token():
    s = FakeSession(fetch_results=[(ZIP, VALID_ZIP)])
    s._fast_primed = True
    timings = {}

    assert await s.download("a", timings) == ZIP

    assert s.nav_calls == 0 and s.tokens == 1 and s.fetch_calls == 1
    assert timings["path"] == "fast"


@pytest.mark.asyncio
async def test_si_el_fetch_directo_falla_el_mismo_intento_sigue_con_el_flujo_completo():
    s = FakeSession(fetch_results=[(b"<html>", AZURE_WAF), (ZIP, VALID_ZIP)])
    s._fast_primed = True
    timings = {}

    assert await s.download("a", timings) == ZIP

    assert timings["attempts"] == 1  # no gastó un reintento
    assert timings["path"] == "full"
    assert s.nav_calls == 1 and s._fast_failures == 1
    assert s._fast_primed is True  # el flujo completo logró el ZIP y volvió a cebar


@pytest.mark.asyncio
async def test_un_error_del_navegador_en_el_camino_rapido_no_lo_propaga():
    s = FakeSession(fetch_results=[])  # fetch_results.pop(0) lanza IndexError
    s._fast_primed = True
    s._strategy_inpage_fetch = _lanza_y_luego(s, ZIP)

    assert await s.download("a") == ZIP
    assert s._fast_failures == 1


def _lanza_y_luego(session, zip_bytes):
    estado = {"n": 0}

    async def _fetch(url):
        estado["n"] += 1
        if estado["n"] == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return zip_bytes, VALID_ZIP

    return _fetch


@pytest.mark.asyncio
async def test_tras_dos_fallos_seguidos_el_camino_rapido_se_desactiva():
    malo = (b"<html>", AZURE_WAF)
    s = FakeSession(fetch_results=[malo, (ZIP, VALID_ZIP), malo, (ZIP, VALID_ZIP), (ZIP, VALID_ZIP)])
    s._fast_primed = True

    await s.download("a")  # fallo rápido 1 → completo → ceba
    await s.download("b")  # fallo rápido 2 → se apaga → completo
    assert s._fast_enabled is False

    s.nav_calls = 0
    await s.download("c")  # ya no intenta el camino rápido
    assert s.nav_calls == 1 and s._fast_failures == 2


@pytest.mark.asyncio
async def test_un_exito_rapido_reinicia_el_contador_de_fallos():
    s = FakeSession(fetch_results=[(b"<html>", AZURE_WAF), (ZIP, VALID_ZIP), (ZIP, VALID_ZIP)])
    s._fast_primed = True

    await s.download("a")  # 1 fallo, luego flujo completo
    await s.download("b")  # éxito rápido
    assert s._fast_failures == 0 and s._fast_enabled is True


@pytest.mark.asyncio
async def test_se_puede_apagar_con_la_variable_de_entorno(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_FAST_PATH", "false")
    s = FakeSession(fetch_results=[(ZIP, VALID_ZIP)])
    s._fast_primed = True

    await s.download("a")

    assert s.nav_calls == 1  # siempre flujo completo


@pytest.mark.asyncio
async def test_sin_zip_en_ningun_camino_sigue_fallando_como_antes():
    malo = (b"<html>", EMPTY_OR_HTML)
    s = FakeSession(fetch_results=[malo] * 10, max_retries=2)

    with pytest.raises(DianDownloadError) as exc:
        await s.download("a")

    assert exc.value.kind == EMPTY_OR_HTML


class TestNavegadorCaido:
    """Si Chrome muere a media descarga no tiene sentido seguir reintentando sobre él."""

    @pytest.mark.asyncio
    async def test_si_muere_durante_la_navegacion_se_corta_sin_gastar_mas_tokens(self):
        s = FakeSession(fetch_results=[(ZIP, VALID_ZIP)])

        async def _nav_que_mata_chrome(url):
            s._page.closed = True
            return b"", EMPTY_OR_HTML

        s._strategy_nav_download = _nav_que_mata_chrome

        with pytest.raises(DianDownloadError) as exc:
            await s.download("a")

        assert exc.value.kind == BROWSER_CRASHED
        assert s.tokens == 1 and s.fetch_calls == 0  # ni segundo token ni fetch

    @pytest.mark.asyncio
    async def test_si_muere_durante_el_fetch_no_agota_los_tres_intentos(self):
        s = FakeSession(fetch_results=[])
        timings = {}

        async def _fetch_que_mata_chrome(url):
            s._page.closed = True
            raise RuntimeError("Target page, context or browser has been closed")

        s._strategy_inpage_fetch = _fetch_que_mata_chrome

        with pytest.raises(DianDownloadError) as exc:
            await s.download("a", timings)

        assert exc.value.kind == BROWSER_CRASHED
        assert timings["attempts"] == 1

    @pytest.mark.asyncio
    async def test_un_crash_en_el_camino_rapido_no_cuenta_como_fallo_suyo(self):
        s = FakeSession(fetch_results=[])
        s._fast_primed = True

        async def _fetch_que_mata_chrome(url):
            s._page.closed = True
            raise RuntimeError("Target page, context or browser has been closed")

        s._strategy_inpage_fetch = _fetch_que_mata_chrome

        with pytest.raises(DianDownloadError) as exc:
            await s.download("a")

        assert exc.value.kind == BROWSER_CRASHED
        assert s._fast_failures == 0 and s._fast_enabled is True
