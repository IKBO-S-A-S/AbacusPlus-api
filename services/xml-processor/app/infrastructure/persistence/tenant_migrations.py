"""Punto único de verdad de las migraciones de la base de cada cliente.

Este archivo existe porque las mismas sentencias vivían repetidas en tres sitios de este
servicio —`main.py`, `adapters/api/routers/internal.py` y
`infrastructure/config/tenant_connection_manager.py`— con contenidos distintos entre sí.
La consecuencia de esa dispersión no es teórica: al añadir una columna había que acertar en
cuál de las tres listas ponerla, y olvidar una dejaba bases de clientes con un esquema
desactualizado. El fallo aparecía semanas después como «columna inexistente» en tiempo de
consulta, lejos de su causa.

Con una base por cliente el problema se multiplica por el número de clientes, así que la
única defensa es que exista **una sola lista** y que todos los puntos de arranque la usen.

Cómo añadir una migración:

1. Se agrega la sentencia **al final** de `TENANT_MIGRATIONS`, nunca en medio: el orden
   importa cuando una sentencia depende de otra anterior.
2. Debe ser **idempotente** (`IF NOT EXISTS`, `IF EXISTS`, `ON CONFLICT DO NOTHING`), porque
   se ejecuta en cada arranque y en cada aprovisionamiento.
3. Debe ser **aditiva y compatible hacia atrás**. Un `DROP COLUMN` o un cambio de tipo
   rompería las instancias de la versión anterior que sigan corriendo durante un despliegue.

Cuando el número de clientes crezca lo suficiente para que este archivo sea incómodo de
leer, el paso siguiente natural es Alembic, que además lleva registro de qué versión tiene
cada base. Hasta entonces, esta lista cumple el mismo propósito sin añadir dependencias.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Orden significativo: cada sentencia puede depender de las anteriores.
TENANT_MIGRATIONS: tuple[str, ...] = (
    # ── Catálogo de estados ────────────────────────────────────────────────────
    """
    INSERT INTO document_statuses (id, name) VALUES
        (0, 'Error'), (100, 'Procesado'), (200, 'Causado'),
        (300, 'Aprobado'), (400, 'Contabilizada')
    ON CONFLICT (id) DO NOTHING
    """,
    # Migración histórica: el estado se guardaba como texto y pasó a numérico. Se conserva
    # porque puede haber bases de clientes anteriores al cambio que aún no se hayan abierto.
    """
    DO $$
    BEGIN
        IF (SELECT data_type FROM information_schema.columns
            WHERE table_name='documents' AND column_name='status') = 'character varying' THEN
            ALTER TABLE documents ALTER COLUMN status TYPE INTEGER USING
                CASE status
                    WHEN 'Procesado'      THEN 100
                    WHEN 'procesado'      THEN 100
                    WHEN 'processed'      THEN 100
                    WHEN 'Causado'        THEN 200
                    WHEN 'causado'        THEN 200
                    WHEN 'Aprobado'       THEN 300
                    WHEN 'aprobado'       THEN 300
                    WHEN 'Contabilizada'  THEN 400
                    WHEN 'contabilizada'  THEN 400
                    ELSE 0
                END;
        END IF;
    END $$;
    """,
    # ── Líneas de detalle: todos los impuestos de la línea ─────────────────────
    #
    # Una línea puede llevar varios impuestos. Ocho facturas de telecomunicaciones del
    # cliente declaran IVA 19 % e impuesto al consumo 4 % en el mismo renglón, y conservar
    # solo el primero perdía $7.363,44 repartidos en 19 documentos.
    #
    # `tax_type`/`tax_value` se conservan como el impuesto principal —el de mayor importe—
    # porque los leen la interfaz y el RAG; esta columna guarda la lista completa.
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS taxes JSONB",
    # ── Total contabilizado en SIIGO ───────────────────────────────────────────
    #
    # El total que SIIGO informa al aceptar la factura. Se guardaba solo dentro del cuerpo
    # de la respuesta en `accounting_attempts`, así que un documento cerrado por
    # reconciliación —que no registra intento— se quedaba sin él: 2 de los 9 documentos
    # contabilizados del cliente no mostraban el total en la ficha de confirmación.
    #
    # Es además lo que permite comprobar que el importe contabilizado es el facturado.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS xml_withholdings JSONB",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS siigo_total NUMERIC(18,2)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS siigo_total_matches_dian BOOLEAN",
    # ── Registro de procesamiento ──────────────────────────────────────────────
    "ALTER TABLE processing_logs ADD COLUMN IF NOT EXISTS xml_filename VARCHAR(255)",
    "ALTER TABLE processing_logs ADD COLUMN IF NOT EXISTS accounting_status VARCHAR(20)",
    "ALTER TABLE processing_logs ADD COLUMN IF NOT EXISTS accounting_error TEXT",
    # ── Documentos ─────────────────────────────────────────────────────────────
    "ALTER TABLE documents "
    "ALTER COLUMN issuer_phone TYPE VARCHAR(100), "
    "ALTER COLUMN receiver_phone TYPE VARCHAR(100)",
    "ALTER TABLE issuers ADD COLUMN IF NOT EXISTS tipo_contribuyente VARCHAR(50)",
    # Refactor contable: se reemplazó el asiento por la asignación de cuenta por ítem.
    "ALTER TABLE documents DROP COLUMN IF EXISTS accounting_entry_id",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS payment_type_id INTEGER "
    "REFERENCES integration_payment_types(id)",
    # RF-07: centro de costo a nivel de documento (SIIGO solo lo admite general en compra).
    # Sin FK: el catálogo que alimenta el selector y valida esta asignación es `cost_centers`
    # (no `integration_cost_centers`), y el `payment_type_id` de al lado tampoco lleva FK. La
    # integridad la garantiza la validación del endpoint contra el catálogo activo.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS cost_center_id INTEGER",
    # Corrige instalaciones donde esta columna se creó con una FK a la tabla equivocada.
    "ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_cost_center_id_fkey",
    # RF-03: archivos oficiales de la DIAN y sus enlaces en S3.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pdf_data BYTEA",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pdf_source VARCHAR(20)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pdf_url VARCHAR(1024)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS xml_data BYTEA",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS xml_url VARCHAR(1024)",
    # ── Líneas de detalle · RF-01 y RF-04 ──────────────────────────────────────
    # `code` es la CUENTA DEL PUC de la línea; `cost_center_id`, su CENTRO DE COSTOS.
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS code VARCHAR(50)",
    "ALTER TABLE document_details "
    "ADD COLUMN IF NOT EXISTS type VARCHAR(20) NOT NULL DEFAULT 'Account'",
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS tax_id INTEGER "
    "REFERENCES integration_taxes(id)",
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS cost_center_id INTEGER "
    "REFERENCES integration_cost_centers(id)",
    # RF-04: origen de la cuenta del PUC ("llm" | "manual"). Las líneas que ya tenían cuenta
    # antes de esta columna se marcan como "llm", que es su origen real: hasta entonces la
    # única vía de asignación automática era el modelo.
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS code_source VARCHAR(10)",
    "UPDATE document_details SET code_source = 'llm' "
    "WHERE code IS NOT NULL AND code_source IS NULL",
    # RF-04: última cuenta propuesta por el modelo. Se conserva aunque el contador la
    # sobrescriba, para poder distinguir lo sugerido de lo editado a mano.
    "ALTER TABLE document_details ADD COLUMN IF NOT EXISTS code_suggested VARCHAR(50)",
    "UPDATE document_details SET code_suggested = code "
    "WHERE code_source = 'llm' AND code_suggested IS NULL",
    # ── Impuestos del documento · RF-02 y RF-08 ────────────────────────────────
    "ALTER TABLE document_taxes "
    "ADD COLUMN IF NOT EXISTS taxable_base DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE document_taxes "
    "ADD COLUMN IF NOT EXISTS percentage DOUBLE PRECISION NOT NULL DEFAULT 0",
    # RF-08: origen de la retención, equivalente de `code_source`. Las filas previas se
    # marcan como manuales porque hasta entonces la única vía de alta era el formulario.
    "ALTER TABLE document_taxes ADD COLUMN IF NOT EXISTS source VARCHAR(10)",
    "UPDATE document_taxes SET source = 'manual' WHERE source IS NULL",
    # ── Plan Único de Cuentas ──────────────────────────────────────────────────
    # Solo las cuentas hoja admiten imputación. Sin esta columna, tanto el selector como la
    # validación aceptaban cuentas de clase o de grupo. Se deja NULL en los catálogos ya
    # cargados: la importación del Excel la rellena, y hasta entonces el validador trata
    # NULL como «desconocido» y no bloquea.
    "ALTER TABLE puc_accounts ADD COLUMN IF NOT EXISTS accepts_movements BOOLEAN",
    # Responsabilidades del RECEPTOR (comprador) del RUT. Junto con las del emisor, deciden si
    # aplica retención: solo si el comprador es agente de retención se practica alguna.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS receiver_responsibilities VARCHAR(100)",
    # ── RF-05: contabilización en SIIGO ────────────────────────────────────────
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS siigo_id VARCHAR(120)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS siigo_name VARCHAR(120)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounted_at TIMESTAMPTZ",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_error TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_started_at TIMESTAMPTZ",
    # La última línea de defensa contra la doble contabilización, y la única que sobrevive a
    # un despliegue con varias réplicas: aunque dos procesos superaran a la vez la validación
    # de estado, PostgreSQL impedirá que ambos escriban el mismo id de SIIGO.
    #
    # Es un índice PARCIAL (WHERE siigo_id IS NOT NULL) porque la inmensa mayoría de los
    # documentos no está contabilizada, y un UNIQUE normal trataría cada NULL como distinto
    # —lo que funciona en Postgres, pero deja el índice cargando filas que nunca se consultan.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_siigo_id
        ON documents (siigo_id) WHERE siigo_id IS NOT NULL
    """,
    # La tabla se consulta por estado en cada carga de la vista y en la selección de lotes.
    "CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status)",
    # ── RF-08 · ReteICA por concepto ──────────────────────────────────────────
    #
    # La tarifa de ReteICA la fija el CONCEPTO de la operación (compra, servicios, honorarios,
    # comisiones…), no solo el municipio: en Bogotá hay una banda por actividad económica. La
    # tabla admitía UNA sola tarifa por municipio —restricción única sobre el código—, así que
    # era físicamente imposible cargar esas bandas: la segunda fila del mismo municipio se
    # rechazaba. El efecto era que ReteICA solo podía aplicarse con una tarifa para todo, o no
    # proponerse en absoluto.
    #
    # Las filas ya cargadas quedan con concepto 'todos' (aplica a cualquier concepto), que es
    # exactamente lo que significaban cuando el concepto no existía: no se reinterpreta nada.
    "ALTER TABLE retention_ica_rates ADD COLUMN IF NOT EXISTS retention_concept "
    "VARCHAR(120) NOT NULL DEFAULT 'todos'",
    "ALTER TABLE retention_ica_rates DROP CONSTRAINT IF EXISTS uq_retention_ica_municipality_code",
    # Base mínima por (municipio, concepto): el ICA es territorial y cada municipio fija su
    # tope. Estaba fija en el prompt con los valores de Bogotá (4 y 27 UVT), lo que proponía
    # retenciones improcedentes en municipios con topes más altos —Bucaramanga pide 25 y 50—.
    "ALTER TABLE retention_ica_rates ADD COLUMN IF NOT EXISTS minimum_base_uvt NUMERIC(10,2)",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_retention_ica_municipality_concept'
        ) THEN
            ALTER TABLE retention_ica_rates
                ADD CONSTRAINT uq_retention_ica_municipality_concept
                UNIQUE (municipality_code, retention_concept);
        END IF;
    END $$;
    """,
    # Repara los códigos que entraron con la cola decimal de una celda numérica ('11001.0').
    # El importador ya los normaliza, pero lo que se cargó antes sigue en la tabla, y un
    # código así no cruza con el real: ni en la restricción única, ni en el filtro por
    # municipio con el que RF-08 recupera casos, ni contra ningún catálogo.
    #
    # Solo toca los que terminan en '.0' seguido del fin de cadena, así que reejecutarla no
    # cambia nada una vez limpios.
    r"""
    UPDATE retention_ica_rates
       SET municipality_code = left(municipality_code, length(municipality_code) - 2)
     WHERE municipality_code ~ '^\d+\.0$'
    """,
    # ── RF-05 · Clasificación de errores, cerrojo y cola de contabilización ────
    #
    # Clasificación del último fallo. El estado funcional sigue siendo ERROR; estas columnas
    # son las que deciden qué acción se le ofrece al contador.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_error_class VARCHAR(30)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_recommended_action VARCHAR(40)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_error_code VARCHAR(60)",
    # El cerrojo que sustituye al antiguo estado «Contabilizando». NOT NULL con DEFAULT false
    # para que los documentos existentes queden desbloqueados, que es su situación real.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_locked BOOLEAN NOT NULL "
    "DEFAULT false",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS accounting_attempts INTEGER NOT NULL "
    "DEFAULT 0",
    # Retirada del sexto estado.
    #
    # El estado 350 («Contabilizando») representaba un cerrojo, no una etapa del ciclo de
    # vida contable. Los documentos que estén en él son exactamente los de desenlace
    # desconocido: se les traslada a ERROR —que es el estado funcional que les corresponde—
    # **conservando la protección**, ahora en `accounting_locked`. Ningún documento queda
    # reenviable por efecto de esta migración; ése era el riesgo a evitar y por eso el UPDATE
    # pone el cerrojo en la misma sentencia que cambia el estado.
    """
    UPDATE documents
       SET status = 0,
           accounting_locked = true,
           accounting_error_class = 'UNCERTAIN',
           accounting_recommended_action = 'VERIFICAR_EN_SIIGO',
           accounting_error = COALESCE(
               accounting_error,
               'La contabilización quedó sin desenlace conocido. Verifique en SIIGO si la '
               'factura se creó antes de reenviar el documento.'
           )
     WHERE status = 350
    """,
    # El catálogo se limpia DESPUÉS de mover los documentos: al revés, la clave foránea
    # `documents.status → document_statuses.id` rechazaría el borrado.
    "DELETE FROM document_statuses WHERE id = 350",
    # Los documentos que ya estaban en ERROR por un fallo de contabilización anterior a la
    # clasificación se quedan sin acción recomendada, y sin ella la interfaz no sabría qué
    # botón ofrecer.
    #
    # La acción se deduce del CERROJO, que es el único dato fiable que dejaron esos errores:
    #
    # - Sin cerrojo, el documento fue liberado en su momento, y liberarlo solo ocurría cuando
    #   constaba que SIIGO no había creado nada. Reintentar es seguro, así que se marcan como
    #   reintentables. Marcarlos como «revisión manual» —que fue el primer intento de esta
    #   migración— dejaba al contador con los dos botones apagados y sin forma de desatascar
    #   documentos que solo esperaban un reenvío; en la práctica, un error de configuración ya
    #   resuelto seguía bloqueando documentos para siempre.
    # - Con cerrojo, el desenlace no consta. Esos sí exigen verificar en SIIGO antes de nada.
    """
    UPDATE documents
       SET accounting_error_class = CASE
               WHEN accounting_locked THEN 'UNCERTAIN' ELSE 'TRANSIENT' END,
           accounting_recommended_action = CASE
               WHEN accounting_locked THEN 'VERIFICAR_EN_SIIGO' ELSE 'REINTENTAR' END
     WHERE status = 0
       AND accounting_error IS NOT NULL
       AND accounting_recommended_action IS NULL
    """,
    # Repara las filas que la primera versión de la migración de arriba dejó en revisión
    # manual sin que nada lo justificara. Solo toca las que no tienen cerrojo —las que consta
    # que SIIGO no llegó a crear— y por tanto no puede habilitar ningún reenvío arriesgado.
    """
    UPDATE documents
       SET accounting_error_class = 'TRANSIENT',
           accounting_recommended_action = 'REINTENTAR'
     WHERE status = 0
       AND accounting_error IS NOT NULL
       AND accounting_locked = false
       AND accounting_recommended_action = 'REVISION_MANUAL'
       AND accounting_attempts = 0
    """,
    # Cola de contabilización. Persistida y no en memoria: un reinicio no puede perder el
    # rastro de un documento que ya se envió a SIIGO.
    """
    CREATE TABLE IF NOT EXISTS accounting_jobs (
        id               SERIAL PRIMARY KEY,
        document_id      INTEGER NOT NULL REFERENCES documents(id),
        batch_id         VARCHAR(64),
        state            VARCHAR(30) NOT NULL DEFAULT 'PENDING',
        attempt          INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 5,
        next_attempt_at  TIMESTAMPTZ,
        last_attempt_at  TIMESTAMPTZ,
        error_class      VARCHAR(30),
        recommended_action VARCHAR(40),
        last_error       TEXT,
        last_error_code  VARCHAR(60),
        last_http_status INTEGER,
        siigo_id         VARCHAR(120),
        siigo_name       VARCHAR(120),
        enqueued_by      VARCHAR(120),
        locked_by        VARCHAR(120),
        locked_at        TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Barrera de base de datos contra el doble encolado: un documento no puede tener dos
    # trabajos activos a la vez. Es independiente del cerrojo del documento a propósito —dos
    # defensas que fallan por motivos distintos protegen más que una repetida—, y es la que
    # sigue en pie si algún día hay varias réplicas del servicio.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_jobs_active
        ON accounting_jobs (document_id)
     WHERE state IN ('PENDING', 'RUNNING')
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_accounting_jobs_pickup
        ON accounting_jobs (state, next_attempt_at)
    """,
    "CREATE INDEX IF NOT EXISTS ix_accounting_jobs_batch ON accounting_jobs (batch_id)",
    # Historial de intentos. Append-only: es la evidencia con la que se responde qué se envió
    # a SIIGO y qué contestó, meses después del hecho.
    """
    CREATE TABLE IF NOT EXISTS accounting_attempts (
        id                 SERIAL PRIMARY KEY,
        document_id        INTEGER NOT NULL REFERENCES documents(id),
        job_id             INTEGER REFERENCES accounting_jobs(id),
        attempt            INTEGER NOT NULL,
        started_at         TIMESTAMPTZ NOT NULL,
        finished_at        TIMESTAMPTZ,
        duration_ms        INTEGER,
        request_payload    JSONB,
        response_body      JSONB,
        http_status        INTEGER,
        ok                 BOOLEAN NOT NULL DEFAULT false,
        siigo_id           VARCHAR(120),
        siigo_name         VARCHAR(120),
        error_message      TEXT,
        error_code         VARCHAR(60),
        error_class        VARCHAR(30),
        recommended_action VARCHAR(40),
        triggered_by       VARCHAR(120),
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_accounting_attempts_doc_created
        ON accounting_attempts (document_id, created_at)
    """,
    # Correcciones manuales. Cierra la trazabilidad del ciclo «Error → Editar → Reintentar»:
    # el valor actual del campo muestra el resultado, no quién lo cambió ni desde qué valor.
    """
    CREATE TABLE IF NOT EXISTS document_field_changes (
        id          SERIAL PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES documents(id),
        entity      VARCHAR(40) NOT NULL,
        entity_id   INTEGER,
        field       VARCHAR(60) NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        changed_by  VARCHAR(120),
        reason      VARCHAR(120),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_document_field_changes_doc_created
        ON document_field_changes (document_id, created_at)
    """,
    # RF-02 — Las retenciones de un documento se buscan SIEMPRE por `document_id`: al abrir
    # la ficha, al conciliar contra SIIGO y al construir el payload de contabilización.
    # PostgreSQL no indexa las claves foráneas por su cuenta, y a esta tabla se le había
    # pasado —`document_details` sí lo tenía—, así que cada una de esas lecturas recorría
    # entera una tabla que crece con cada retención de cada factura.
    #
    # Va también aquí, y no solo en el modelo, porque `create_all` no añade índices a tablas
    # que ya existen: sin esta sentencia el índice solo aparecería en clientes nuevos.
    "CREATE INDEX IF NOT EXISTS ix_document_taxes_document ON document_taxes (document_id)",
    # RF-03 · Trazabilidad de la publicación en S3/SharePoint. La subida automática al
    # descargar de la DIAN es best-effort (un backend caído no puede tumbar el procesamiento
    # del XML), y esa misma tolerancia la hacía invisible: el fallo solo quedaba en el log de
    # texto del contenedor. Se guarda junto al resto del registro de procesamiento para que
    # un fallo de conexión con S3 o SharePoint sea consultable por API, no solo por logs.
    "ALTER TABLE processing_logs ADD COLUMN IF NOT EXISTS storage_status VARCHAR(20)",
    "ALTER TABLE processing_logs ADD COLUMN IF NOT EXISTS storage_error TEXT",
    # RF-03 · Ubicaciones de publicación por backend (S3/SharePoint/local). El modelo
    # (`Document.pdf_storage_locations` / `.xml_storage_locations`) las tiene desde que se
    # separó `PublishDocumentFilesUseCase`, pero nunca se agregó la migración: `create_all`
    # solo crea tablas nuevas, no columnas nuevas en una tabla que ya existía. El resultado
    # era `UndefinedColumn` en CADA consulta a `documents` de cualquier tenant aprovisionado
    # antes de esa columna — el pipeline de descarga bajaba el ZIP pero moría al intentar
    # leer o actualizar el documento, con el mensaje enterrado detrás de un `except Exception`
    # genérico que además reventaba de nuevo al mover el archivo a `errors/`.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pdf_storage_locations JSONB NOT NULL "
    "DEFAULT '{}'::jsonb",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS xml_storage_locations JSONB NOT NULL "
    "DEFAULT '{}'::jsonb",
)


def apply_tenant_migrations(engine, *, create_tables: bool = True, strict: bool = False) -> int:
    """Lleva la base de un cliente al esquema vigente. Retorna cuántas sentencias corrieron.

    `create_tables=True` crea antes las tablas que aún no existan, que es lo que necesita el
    aprovisionamiento de un cliente nuevo. Al abrir la conexión de un cliente ya existente se
    pasa False: las tablas están y recorrer los metadatos en cada arranque cuesta sin aportar.

    `strict=True` propaga el error. Se usa al aprovisionar, donde un fallo debe impedir que
    el cliente quede declarado como listo con el esquema a medias. En el arranque normal se
    deja en False: un servicio que no levanta por una migración deja al cliente sin sistema,
    mientras que un aviso en el log permite operar y corregir. En ambos casos **se registra**,
    porque tragarse el error en silencio fue justo lo que dejó esquemas desactualizados sin
    que nadie se enterara.
    """
    if create_tables:
        from app.infrastructure.config.database import Base

        Base.metadata.create_all(bind=engine, checkfirst=True)

    aplicadas = 0
    for sentencia in TENANT_MIGRATIONS:
        try:
            # Una transacción por sentencia: si una falla, las demás siguen aplicándose.
            # Con un único bloque, un error al principio abortaba todo lo posterior.
            with engine.begin() as conn:
                conn.execute(text(sentencia))
            aplicadas += 1
        except Exception as exc:
            resumen = " ".join(sentencia.split())[:90]
            logger.warning(
                "Migración de tenant omitida (%s: %s) — sentencia: %s",
                type(exc).__name__,
                exc,
                resumen,
            )
            if strict:
                raise

    return aplicadas
