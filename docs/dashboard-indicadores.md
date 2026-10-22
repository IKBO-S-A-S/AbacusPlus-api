# Dashboard de indicadores: diccionario

Servicio: `xml-processor`. Ruta base por el gateway: `/api/v1/dashboard`. Swagger en
`http://localhost:8000/docs` (selector `xml-processor`, tag `dashboard`).

Incremento 1 (este documento): vista **ejecutiva**, vista de **operación** y **drill-down**,
calculados solo con los datos que existen hoy, sin tablas ni columnas nuevas. Las vistas de
aprobaciones, calidad, compras y técnica quedan para los siguientes incrementos.

## Reglas comunes

**Universo.** Todo indicador del periodo cuenta los documentos cuya **fecha de emisión**
(`documents.date`) está en `[from_date, to_date]` y los clasifica por su **estado actual**. No
hay historial de transiciones, así que NO se puede decir «en el periodo se contabilizaron N»;
se dice «de los documentos emitidos en el periodo, N están hoy contabilizados».

**Periodo anterior comparable.** La ventana inmediatamente anterior de la misma duración
(2026-10-01..2026-10-22 → 2026-09-09..2026-09-30), calculada con la misma regla.

**Variación %.** `(actual − anterior) / |anterior| × 100`. `null` si el anterior es 0 o no existe.

**Nulos.** Una tasa con denominador 0 es `null`, nunca `0`: «0 % de error» y «no hubo
documentos» son cosas distintas. Un periodo sin datos devuelve contadores en 0 y tasas en `null`.

**Semáforo (`light`).** `green` / `yellow` / `red` frente a la meta; `null` si no hay meta, no
hay valor o el indicador es `neutral`.
- `higher_is_better`: verde si `valor >= meta`; amarillo si `valor >= alerta`; rojo si no.
- `lower_is_better`: verde si `valor <= meta`; amarillo si `valor <= alerta`; rojo si no.
- Sin `alerta` no hay banda amarilla.

**Filtros comunes** (query): `from_date`, `to_date` (por defecto, los últimos 30 días hasta
hoy; máximo 731 días), `receiver_nit` (empresa), `issuer_nit` (proveedor), `cost_center_id`,
`status`. No existen filtros por **sede** ni por **fuente** (el dato no se guarda).

**Hoy y meses** se evalúan en la zona `DASHBOARD_TIMEZONE` (por defecto `America/Bogota`),
no en UTC.

**Acceso.** Lectura para cualquier usuario autenticado del cliente (igual que el resto de
GET). Cada cliente tiene su propia base, así que un usuario solo ve los datos de su empresa.
No hay todavía roles aprobador/analista/gerencia ni alcance por sede.

## Configuración (variables de entorno)

| Variable | Descripción |
|---|---|
| `DASHBOARD_TARGETS` | JSON `{codigo_kpi: {"meta": n, "alerta": n}}`. `alerta` opcional. Un JSON inválido se registra y deja los KPIs sin meta. |
| `DASHBOARD_TIMEZONE` | Zona IANA para «hoy» y el mes de contabilización. Por defecto `America/Bogota`. |

Ejemplo: `DASHBOARD_TARGETS={"accounting_rate":{"meta":90,"alerta":75},"error_rate":{"meta":2,"alerta":5}}`

Metas por variable de entorno y no por tabla: se pidió no crear tablas en este incremento. Si
se necesita editarlas desde la interfaz sin reiniciar, el siguiente paso es una tabla
`dashboard_settings` (hoy no existe).

## Indicadores: `GET /dashboard/executive`

| Código | Nombre | Unidad / sentido | Fórmula | Fuente | Drill-down (`indicator`) |
|---|---|---|---|---|---|
| `documents_total` | Documentos del periodo | conteo / neutral | `count(documents)` | `documents` | `documents` |
| `amount_total` | Valor total | moneda / neutral | `sum(total)` | `documents.total` | no |
| `accounting_rate` | Tasa de contabilización | % / mayor mejor | contabilizados (400) / total × 100 | `documents.status` | `accounted` |
| `error_rate` | Tasa de error | % / menor mejor | en Error (0) / total × 100 | `documents.status` | `in_error` |
| `pending_approval` | Pendientes de aprobación | conteo / menor mejor | status 200 (Causado) | `documents.status` | `pending_approval` |
| `approved_not_accounted` | Aprobados sin contabilizar | conteo / menor mejor | status 300 (Aprobado) | `documents.status` | `approved_not_accounted` |
| `accounted_on_time_rate` | Contabilizados en el periodo correcto | % / mayor mejor | contabilizados cuyo mes de `accounted_at` = mes de `date`, sobre contabilizados con `accounted_at` × 100 | `documents.accounted_at`, `date` | `accounted_outside_period` (lista los que NO lo cumplen) |
| `without_support` | Documentos sin soporte (PDF) | conteo / menor mejor | `pdf_url IS NULL` | `documents.pdf_url` | `without_support` |
| `duplicates` | Duplicados detectados | conteo / menor mejor | filas `processing_logs.status = 'duplicate'` con `processed_at` en el periodo | `processing_logs` | no |

Supuestos:
- «Pendiente de aprobación» = `CAUSADO`: el flujo no tiene un estado propio de «enviado a aprobar».
- Los contabilizados sin `accounted_at` (cerrados por reconciliación) no entran ni al numerador
  ni al denominador de `accounted_on_time_rate`.
- «Sin soporte» mide solo el PDF (`pdf_url`); el XML oficial no se considera soporte faltante.
- `duplicates` no admite filtros de documento (el log no guarda proveedor, empresa ni centro
  de costo): con cualquiera de ellos devuelve `value = null` y una `note`. Cuenta archivos
  rechazados, no documentos.
- Un documento duplicado se detecta por **número de documento** únicamente.

## Indicadores: `GET /dashboard/operations`

| Bloque | Contenido | Fórmula / regla |
|---|---|---|
| `by_status` | conteo e importe de los 5 estados | `group by status`; siempre devuelve los cinco |
| `funnel` | procesados → causados → aprobados → contabilizados | Acumulativo por estado actual. Procesados = todos. Causados = 200 + 300 + 400 + Error de contabilización. Aprobados = 300 + 400 + Error de contabilización. Contabilizados = 400. Un Error con `accounting_error` ya pasó por causación y aprobación; un Error sin él (XML no procesable) solo cuenta como procesado. `pct_of_total` sobre procesados; `conversion_from_previous_pct` sobre la etapa anterior |
| `series` | documentos e importe por `day`/`week`/`month` de emisión | Semana = lunes a domingo. Omite buckets vacíos. `count_variation_pct` contra el bucket anterior de la serie. `trend` compara los dos últimos buckets **completos** (`to_date < hoy`): `up`/`down`/`flat` (±1 %); `null` con menos de dos |
| `backlog` | pendientes por etapa y antigüedad | Etapas: Procesado (100), Causado (200), Aprobado (300), Error (0). Antigüedad = hoy − fecha de emisión, en rangos `0-3`, `4-7`, `8-15`, `15+`. **No usa el rango de fechas** (es lo pendiente hoy); sí los demás filtros. Siempre devuelve los cuatro rangos |

Supuesto del backlog: la antigüedad se mide desde la **emisión**, no desde que el documento
entró a la etapa (esa fecha no se guarda).

## Drill-down: `GET /dashboard/documents`

`indicator` ∈ `documents`, `accounted`, `pending_approval`, `approved_not_accounted`,
`in_error`, `without_support`, `accounted_outside_period`, `backlog`. Con `backlog`, `stage`
(`processed`/`caused`/`approved`/`error`) es obligatorio y `age_range` opcional; `stage` y
`age_range` con otro indicador dan 422. Paginado con `limit` (1-500, 50 por defecto) y `offset`.

`total` e `amount` describen **todo el conjunto**, no la página. Para cada indicador, `total`
coincide con la cifra del KPI (o de la celda del backlog) con los mismos filtros; una prueba lo
verifica. Orden: más reciente primero; el backlog, del más antiguo al más reciente.

## No disponible con los datos actuales

| Indicador | Qué falta |
|---|---|
| Tiempo de ciclo total y por etapa (promedio, mediana, P90) | fecha de cada transición de estado |
| Tasa de automatización (STP), horas ahorradas | quién o qué movió cada documento |
| Tiempo de aprobación, por aprobador | fecha de aprobación y aprobador |
| Tasa de aprobación vs. rechazo y motivos | el rechazo/devolución no se registra |
| Cuentas por pagar por vencimiento | la fecha de vencimiento no se guarda (se lee del XML pero no se persiste) |
| Documentos sin orden de compra | no se lee del XML |
| Gasto por sede, estado frente a la DIAN, score de IA | el dato no existe |
| Descargas fallidas por fuente | solo en Redis, 7 días |

## Rendimiento: decisiones y pendientes

- **Todo se agrega en la base** (`count`/`sum`/`group by`); las únicas filas que se devuelven
  son las de la página del drill-down. Las vistas calculan sus contadores en una sola pasada.
- **Sin caché ni vistas materializadas en este incremento.** Cada vista son 2 a 5 agregados
  sobre `documents` filtrados por `date`, que ya tiene índice (`ix_documents_date_status`). Con
  el volumen de desarrollo (50 documentos) no hay nada que medir, y una caché añade datos
  desactualizados en pantallas que el contador mira para decidir qué hacer ahora. Revisar con
  `EXPLAIN ANALYZE` sobre un tenant real; si una vista pasa de ~1 s, un caché de 30-60 s por
  (tenant, filtros) es el primer paso, antes que una vista materializada.
- **Índices sugeridos (NO creados; requieren migración y su confirmación):**
  `documents (issuer_nit, date)` para el filtro por proveedor,
  `documents (receiver_nit, date)` y `documents (cost_center_id, date)` para empresa y centro
  de costo. Hoy esos filtros se resuelven filtrando primero por `date`. Los planes de consulta
  no se han revisado contra datos de producción.
- `processing_logs.processed_at` y `status` no tienen índice; `duplicates` recorre la tabla
  del log. Es aceptable mientras el log sea pequeño.

## Pruebas

`tests/domain/test_dashboard_metrics.py` (fórmulas y casos borde) y
`tests/integration/test_dashboard.py` (agregaciones, periodo anterior, drill-down contra KPI,
endpoints HTTP y OpenAPI). Corren con SQLite en memoria. Las expresiones solo de PostgreSQL
(`date_trunc`, `to_char`, `timezone`) se ejercitaron además con una ejecución de solo lectura
contra la base de desarrollo, pero no hay prueba automática contra PostgreSQL.
