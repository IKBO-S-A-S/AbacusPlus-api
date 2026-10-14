#!/usr/bin/env bash
# Aprovisiona (crea/migra tablas de) uno o más tenants en todos los servicios.
#
# Uso:   ./scripts/provision-tenants.sh ikbo casabrava
#        ./scripts/provision-tenants.sh --all     (todas las bases abacus_t_* del servidor)
# Salto: BACKUP=0 ./scripts/provision-tenants.sh ikbo   (omite el pg_dump previo)
#
# Corre en el host de Docker. Llama a /internal/provision-tenant DENTRO de cada contenedor,
# así el INTERNAL_SECRET se toma del entorno del contenedor y nunca se imprime ni se pasa
# por argumentos. Se detiene al primer fallo. Pide confirmación antes de tocar nada.

set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "Uso: $0 <tenant_slug> [<tenant_slug> ...]  |  $0 --all" >&2
  exit 1
fi

# Orden obligatorio: integration-config-service antes que xml-processor (FK cruzadas).
SERVICES=(
  "abacus_integration_config_service:8007"
  "abacus_xml_processor:8001"
  "abacus_siigo_service:8006"
  "abacus_llm_service:8003"
  "abacus_rag_service:8002"
  "abacus_odoo_service:8005"
)
DB_CONTAINER="abacus_db"
BACKUP="${BACKUP:-1}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

# --all: descubre los tenants existentes listando las bases abacus_t_* del servidor.
if [ "$1" = "--all" ]; then
  mapfile -t slugs < <(docker exec "$DB_CONTAINER" sh -c \
    "psql -U \"\$POSTGRES_USER\" -d postgres -Atc \"SELECT substr(datname, 10) FROM pg_database WHERE datname LIKE 'abacus\\_t\\_%' ORDER BY 1\"")
  [ "${#slugs[@]}" -gt 0 ] || { echo "No se encontraron bases abacus_t_*." >&2; exit 1; }
  set -- "${slugs[@]}"
fi

echo "Tenants : $*"
echo "Bases   : $(printf 'abacus_t_%s ' "$@")"
echo "Backup  : $([ "$BACKUP" = "1" ] && echo "sí -> $BACKUP_DIR" || echo "NO")"
read -r -p "Continuar? (escribe 'si'): " answer
[ "$answer" = "si" ] || { echo "Cancelado."; exit 1; }

if [ "$BACKUP" = "1" ]; then
  mkdir -p "$BACKUP_DIR"
  for slug in "$@"; do
    file="$BACKUP_DIR/abacus_t_${slug}_$(date +%Y%m%d_%H%M%S).dump"
    echo "== backup $slug -> $file"
    docker exec "$DB_CONTAINER" sh -c "pg_dump -U \"\$POSTGRES_USER\" -Fc abacus_t_${slug}" > "$file"
    [ -s "$file" ] || { echo "Backup vacío, abortando." >&2; exit 1; }
  done
fi

for slug in "$@"; do
  for entry in "${SERVICES[@]}"; do
    container="${entry%%:*}"
    port="${entry##*:}"
    echo "== $slug / $container"
    docker exec -i "$container" python - "$port" "$slug" <<'PY'
import os, sys, urllib.request, urllib.error
port, slug = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    f"http://localhost:{port}/internal/provision-tenant?tenant_slug={slug}",
    method="POST",
    headers={"X-Internal-Secret": os.environ.get("INTERNAL_SECRET", "")},
)
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
    sys.exit(1)
PY
  done
done

echo "Listo. Tenants aprovisionados: $*"
