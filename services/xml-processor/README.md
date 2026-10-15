# xml-processor

Puerto: **8001**

## Responsabilidad

Procesa facturas electrónicas DIAN en formato ZIP o XML, las valida (NIT con dígito de verificación), las persiste en PostgreSQL y coordina el enriquecimiento posterior con otros servicios.

No genera asientos contables completos. No asigna cuentas PUC directamente — ese rol es del llm-service. No interactua con SIIGO.

## Endpoints

| Metodo | Path | Descripcion |
|--------|------|-------------|
| POST | `/api/v1/documents` | Procesa ZIP o XML DIAN, crea documento (201). Duplicados: 409 |
| GET | `/api/v1/documents` | Lista documentos por rango de fechas (`?from_date=&to_date=&status=`) |
| GET | `/api/v1/documents/{id}` | Detalle de un documento con sus lineas |
| GET | `/api/v1/documents/{id}/full` | Documento con cuentas PUC asignadas por el LLM (`code`, `type`) |
| PATCH | `/api/v1/documents/{id}/approve` | Cambia estado Causado → Aprobado |
| PATCH | `/api/v1/documents/{id}` | Actualiza estado (`{"status": 200}` revierte a Causado) |
| PATCH | `/api/v1/documents/{id}/details` | Escribe `code`/`type` en lineas de detalle (llamado por llm-service) |
| PATCH | `/api/v1/documents/{id}/payment-type` | Asigna o cambia `payment_type_id` del documento |
| GET | `/api/v1/documents/{id}/taxes` | Lista impuestos asociados al documento |
| POST | `/api/v1/documents/{id}/taxes` | Agrega un impuesto al documento |
| GET | `/api/v1/documents/{id}/taxes/{tax_id}` | Obtiene un impuesto especifico del documento |
| PATCH | `/api/v1/documents/{id}/taxes/{tax_id}` | Actualiza `tax_id` o `value` de un impuesto |
| DELETE | `/api/v1/documents/{id}/taxes/{tax_id}` | Elimina un impuesto del documento |
| GET | `/api/v1/receivers` | Lista receptores registrados |
| GET | `/api/v1/issuers/{nit}` | Datos del emisor por NIT (cuenta CxP, regimen tributario) |
| GET | `/api/v1/catalog/cost-centers` | Centros de costo activos |
| GET | `/api/v1/catalog/puc-accounts` | Cuentas PUC activas |
| GET | `/api/v1/catalog/retention-fuente-rates` | Tasas de retencion en la fuente por concepto |
| GET | `/api/v1/catalog/retention-ica-rates` | Tasas de reteICA por municipio |
| POST | `/api/v1/batch-jobs/downloads` | Escanea `DOWNLOADS_DIR` y encola todos los ZIPs pendientes (202) |
| POST | `/api/v1/batch-jobs/file` | Encola un ZIP especifico por nombre de archivo (202) |
| GET | `/api/v1/batch-logs` | Historial de procesamiento batch con estado de causacion |
| POST | `/internal/provision-tenant` | Crea/migra tablas del servicio en la BD del tenant (no aparece en Swagger) |
| GET | `/internal/documents/{id}/full` | Lectura de documento para uso inter-servicio (requiere `X-Internal-Secret`) |
| PATCH | `/internal/documents/{id}/details` | Escritura de cuentas PUC para uso inter-servicio (requiere `X-Internal-Secret`) |
| GET | `/health` | Health check |

**Estados de documento:** `0` Error, `100` Procesado, `200` Causado, `300` Aprobado, `400` Contabilizada.

## Dependencias internas

| Servicio | URL | Proposito | Comportamiento ante fallo |
|---------|-----|-----------|--------------------------|
| rag-service | `RAG_SERVICE_URL` | Indexa el contenido de cada factura para busqueda semantica | Best-effort: warning en log, el documento se guarda igual |
| llm-service | `LLM_SERVICE_URL` | Dispara la asignacion de cuentas PUC por linea de detalle | Best-effort: warning en log, el documento queda sin `code` |
| integration-config-service | `INTEGRATION_CONFIG_URL` | Obtiene catalogo de impuestos para enriquecer lineas al procesar | Best-effort: si no esta disponible, `tax_id` queda `null` |

## Variables de entorno

```
DATABASE_HOST=database
DATABASE_PORT=5432
DATABASE_USER=master
DATABASE_PASSWORD=master
DATABASE_NAME=abacus

RAG_SERVICE_URL=http://rag-service:8002
LLM_SERVICE_URL=http://llm-service:8003
INTEGRATION_CONFIG_URL=http://integration-config-service:8007

REDIS_URL=redis://redis:6379
DOWNLOADS_DIR=/app/downloads
INTERNAL_SECRET=<secreto compartido para endpoints /internal/*>
```

## Tests

```bash
docker run --rm -v "$(pwd)/services/xml-processor:/app" --workdir //app python:3.9-slim \
  bash -c "pip install -r requirements.txt -q && python -m pytest tests/ -v"
```

Los tests usan SQLite in-memory. No requieren PostgreSQL, Redis ni servicios externos.

## Decisiones de diseno

- **Best-effort en servicios externos:** fallo en rag-service o llm-service no bloquea el procesamiento del XML. La factura siempre se guarda.
- **Enriquecimiento automatico de lineas:** al procesar cada factura se asigna `tax_id` (lookup en integration-config-service), `cost_center_id` (historial de la misma empresa) y `payment_type_id` (del emisor) antes de persistir el documento.
- **Multi-tenant:** `POST /internal/provision-tenant` crea y migra las tablas del servicio en la base de datos del tenant (`abacus_t_{slug}`). Idempotente. Requiere `X-Internal-Secret`.
- **LLM escribe via PATCH /details:** el llm-service escribe los codigos PUC llamando a `PATCH /api/v1/documents/{id}/details`. La separacion permite reasignar cuentas sin reprocesar el XML.
- **Batch via Redis:** el procesamiento de ZIPs en `POST /batch-jobs/downloads` y `POST /batch-jobs/file` usa un worker arq con Redis como cola.
