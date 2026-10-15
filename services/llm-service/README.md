# llm-service

Puerto: **8003**

## Responsabilidad

Consultas RAG-aumentadas con OpenAI y asignación automática de cuentas PUC a líneas de detalle de facturas. NO genera asientos contables completos — eso lo hace el software destino (SIIGO).

## Endpoints

| Metodo | Path | Descripcion |
|--------|------|-------------|
| POST | `/api/v1/query` | Consulta RAG-aumentada: genera embedding, recupera chunks desde pgvector, construye prompt y llama a OpenAI |
| POST | `/api/v1/analyses` | Prompt directo a OpenAI sin RAG; util para clasificaciones o analisis generales |
| POST | `/api/v1/accounting/code-assignments/{document_id}` | Asigna una cuenta PUC a cada linea de detalle del documento; llamado automaticamente por xml-processor al procesar un XML |
| GET  | `/api/v1/accounting/system-prompts` | Lista todos los system prompts almacenados (`is_active` indica cual esta en uso) |
| POST | `/api/v1/accounting/system-prompts` | Crea un nuevo system prompt (queda inactivo por defecto) |
| PATCH | `/api/v1/accounting/system-prompts/{id}` | Activa un system prompt y desactiva los demas (`{"is_active": true}`) |

## Dependencias internas

- **rag-service** — busqueda semantica de chunks (`POST /api/v1/chunks/search`); fallo propaga excepcion al cliente
- **xml-processor** — lee documento con sus lineas (`GET /api/v1/documents/{id}/full`) y escribe codigos PUC asignados (`PATCH /api/v1/documents/{id}/details`)
- **integration-config-service** — obtiene el plan de cuentas PUC completo para construir el prompt (`GET /api/v1/integrations/chart-accounts`)

## Variables de entorno

| Variable | Descripcion |
|----------|-------------|
| `OPENAI_API_KEY` | Clave de API de OpenAI |
| `RAG_SERVICE_URL` | URL base de rag-service (ej. `http://rag-service:8002`) |
| `XML_PROCESSOR_URL` | URL base de xml-processor (ej. `http://xml-processor:8001`) |
| `INTEGRATION_CONFIG_URL` | URL base de integration-config-service (ej. `http://integration-config-service:8007`) |
| `DATABASE_HOST` | Host PostgreSQL (para system prompts) |
| `DATABASE_USER` | Usuario PostgreSQL |
| `DATABASE_PASSWORD` | Password PostgreSQL |
| `DATABASE_NAME` | Nombre de base de datos |

## Tests

```bash
docker run --rm -v "$(pwd)/services/llm-service:/app" --workdir //app python:3.9-slim \
  bash -c "pip install -r requirements.txt -q && python -m pytest tests/ -v"
```

Los tests usan mocks de `AIService` y `RagClient`; no requieren OpenAI ni rag-service activos.

## Decisiones de diseno

- El LLM recibe el catalogo PUC completo en el prompt y asigna un codigo por item — no hace RAG sobre el PUC.
- La asignacion de cuentas es **best-effort**: si el LLM falla o retorna codigos invalidos, el documento queda guardado sin codigos y puede reintentarse manualmente con `POST /api/v1/accounting/code-assignments/{id}`.
- La respuesta del LLM puede venir como JSON puro o en markdown fenced (` ```json``` `); hay fallback con regex para extraer el JSON en ambos casos.
- Solo un system prompt puede estar activo a la vez; al arrancar el servicio se crea el prompt por defecto si no existe ninguno.
- La autenticacion de endpoints usa JWT (`PyJWT`); `POST /api/v1/analyses` requiere token valido.
