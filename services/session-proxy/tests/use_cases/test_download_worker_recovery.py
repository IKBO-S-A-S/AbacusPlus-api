"""Recuperación del worker cuando Chrome muere a media descarga.

Caso real: en el primer documento de un lote Chrome se cerró; el worker relanzó el navegador y
los siguientes se bajaron bien, pero el documento en curso quedó marcado como fallido aunque sí
existía en la DIAN. Ahora se reintenta una vez con el navegador nuevo.
"""

import pytest
from app.infrastructure.browser.playwright_client import BROWSER_CRASHED, DianDownloadError
from app.infrastructure.workers import download_worker as dw

ZIP = b"PK\x03\x04datos"


class FakeSession:
    instances: list = []
    script: dict = {}  # (n.º de sesión, trackId) -> "crash" | "invalid"; por defecto, ZIP
    fail_open_from = None  # a partir de qué sesión falla open()

    def __init__(self, auth_url, timeout=0, max_retries=0):
        self.index = len(FakeSession.instances)
        FakeSession.instances.append(self)
        self.alive = True
        self.profile_dir = f"/tmp/perfil-{self.index}"

    async def open(self):
        if FakeSession.fail_open_from is not None and self.index >= FakeSession.fail_open_from:
            raise RuntimeError("Chrome no abre")

    async def download(self, track_id, timings=None):
        accion = FakeSession.script.get((self.index, track_id))
        if accion == "crash":
            self.alive = False
            raise DianDownloadError("El navegador se cerró", BROWSER_CRASHED)
        if accion == "invalid":
            raise DianDownloadError("No existe en la DIAN", "DIAN_INVALID")
        return ZIP

    def is_alive(self):
        return self.alive

    async def close(self):
        pass


@pytest.fixture
def lote(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.script = {}
    FakeSession.fail_open_from = None
    registro = {"fallidos": [], "descargados": [], "notificados": []}

    async def _failed(progress, track_id, error, error_code="UNKNOWN"):
        registro["fallidos"].append((track_id, error_code))

    async def _downloaded(progress, track_id):
        registro["descargados"].append(track_id)

    async def _notify(filename, track_id, tenant_slug=""):
        registro["notificados"].append(track_id)

    monkeypatch.setattr(dw, "BrowserDownloadSession", FakeSession)
    monkeypatch.setattr(dw, "_mark_failed", _failed)
    monkeypatch.setattr(dw, "_mark_downloaded", _downloaded)
    monkeypatch.setattr(dw, "_trigger_xml_processing", _notify)
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    monkeypatch.setenv("EXTERNAL_BASE_URL", "https://portal.example.com")

    async def correr(track_ids):
        return await dw.download_batch(
            {"job_progress_store": object()}, "lote-1", track_ids, token="t", tenant_slug="ikbo"
        )

    return correr, registro


@pytest.mark.asyncio
async def test_si_chrome_muere_se_reintenta_el_mismo_documento_con_el_navegador_nuevo(lote):
    correr, registro = lote
    FakeSession.script = {(0, "a"): "crash"}

    resultado = await correr(["a", "b"])

    assert resultado["downloaded"] == 2 and resultado["failed"] == 0
    assert registro["fallidos"] == []
    assert registro["descargados"] == ["a", "b"]
    assert len(FakeSession.instances) == 2  # se relanzó una sola vez


@pytest.mark.asyncio
async def test_si_vuelve_a_morir_en_el_reintento_se_marca_fallido_y_el_lote_sigue(lote):
    correr, registro = lote
    FakeSession.script = {(0, "a"): "crash", (1, "a"): "crash"}

    resultado = await correr(["a", "b"])

    assert registro["fallidos"] == [("a", BROWSER_CRASHED)]
    assert resultado["downloaded"] == 1 and resultado["failed"] == 1
    assert registro["descargados"] == ["b"]


@pytest.mark.asyncio
async def test_si_no_se_puede_relanzar_el_resto_del_lote_se_marca_fallido(lote):
    correr, registro = lote
    FakeSession.script = {(0, "a"): "crash"}
    FakeSession.fail_open_from = 1

    resultado = await correr(["a", "b", "c"])

    assert resultado["downloaded"] == 0 and resultado["failed"] == 3
    assert [c for _, c in registro["fallidos"]] == [
        BROWSER_CRASHED,
        "BROWSER_RELAUNCH_FAILED",
        "BROWSER_RELAUNCH_FAILED",
    ]


@pytest.mark.asyncio
async def test_un_documento_que_no_existe_no_relanza_el_navegador(lote):
    correr, registro = lote
    FakeSession.script = {(0, "a"): "invalid"}

    resultado = await correr(["a", "b"])

    assert registro["fallidos"] == [("a", "DIAN_INVALID")]
    assert resultado["downloaded"] == 1
    assert len(FakeSession.instances) == 1  # el navegador sigue vivo: no hay relanzamiento
