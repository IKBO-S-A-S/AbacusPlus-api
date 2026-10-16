"""Un error de base de datos al procesar un ZIP debe quedar en `processing_logs`.

Caso real: el nombre de un emisor excedía la columna y el INSERT fallaba. La sesión quedaba en
estado de fallo, el intento de registrar el error en `processing_logs` fallaba también y el
`suppress` lo tragaba: el ZIP iba a `errors/` sin dejar ninguna fila que lo explicara.
"""

import zipfile

import pytest
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.models.processing_log import ProcessingLog
from app.infrastructure.queue import download_queue as dq


class _ProcesadorQueFalla:
    """Provoca un error real de base de datos que deja la sesión inutilizable."""

    def __init__(self, *args, **kwargs):
        self._db = kwargs["document_repo"].db

    async def execute(self, file):
        self._db.add(Issuer(name=None, nit="1", email="a@b.co"))  # name es NOT NULL
        self._db.commit()


@pytest.mark.asyncio
async def test_un_error_de_bd_deja_su_registro_y_mueve_el_zip_a_errors(
    db_session, tmp_path, monkeypatch
):
    zip_path = tmp_path / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("factura.xml", "<x/>")
    monkeypatch.setattr(dq, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(dq, "ProcessXmlUseCase", _ProcesadorQueFalla)

    await dq._process_single_file(zip_path, None, "")

    filas = db_session.query(ProcessingLog).filter_by(status="error").all()
    assert len(filas) == 1
    assert filas[0].filename == "doc.zip"
    assert filas[0].error_message
    assert (tmp_path / "errors" / "doc.zip").exists()
