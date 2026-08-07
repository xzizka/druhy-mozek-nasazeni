#!/usr/bin/env bash
# =====================================================================
# DOPLNĚK k 03-quadlets.sh — v ZIPu nebyl, přidáno při nasazení.
#
# Zapne include_dir v $PGDATA/postgresql.conf, aby se načetl
# conf/postgresql-tuning.conf montovaný do /etc/postgresql/conf.d.
#
# Proč samostatný skript: quadlet to původně zkoušel předat jako
#     Exec=postgres ... -c include_dir=/etc/postgresql/conf.d
# což Postgres odmítne s
#     FATAL: unrecognized configuration parameter "include_dir"
# protože include_dir není GUC nastavitelný z příkazové řádky, ale
# direktiva platná jen uvnitř konfiguračního souboru. Hlavička
# conf/postgresql-tuning.conf to tak i popisuje.
#
# Postup: postgres se musí jednou nastartovat, aby proběhl initdb a
# vznikl postgresql.conf. Pak se přípíše include_dir a služba restartuje.
#
# Spouštěj jako root uvnitř kontejneru. Idempotentní.
# =====================================================================
set -euo pipefail

MARK="# --- pridano pri nasazeni: nacteni /etc/postgresql/conf.d ---"
PGCONF="$(podman volume inspect pgdata --format '{{.Mountpoint}}')/pgdata/postgresql.conf"

[ -f "$PGCONF" ] || { echo "CHYBA: $PGCONF neexistuje — nastartuj nejdřív postgres, aby proběhl initdb" >&2; exit 1; }

if grep -qF "$MARK" "$PGCONF"; then
    echo "include_dir už je nastavený, přeskakuji"
else
    printf '\n%s\ninclude_dir = %s\n' "$MARK" "'/etc/postgresql/conf.d'" >> "$PGCONF"
    echo "include_dir přípsán do $PGCONF"
fi

systemctl restart postgres
echo "čekám na healthy..."
for i in $(seq 1 30); do
    if podman exec postgres pg_isready -U postgres -q 2>/dev/null; then
        echo "postgres je připraven"
        break
    fi
    sleep 3
done

echo "== ověření, že se ladění skutečně načetlo =="
podman exec postgres psql -X -U postgres -tA -c \
    "SELECT name || ' = ' || setting FROM pg_settings
      WHERE name IN ('shared_buffers','maintenance_work_mem','jit',
                     'effective_cache_size','shared_preload_libraries')
      ORDER BY name"
