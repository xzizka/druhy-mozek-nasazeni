#!/usr/bin/env bash
# =====================================================================
# Kontrola, že schéma `retrieval` odpovídá všem migracím v sql/.
#
# Obálka nad `sql/05-verify.sql` — samotné aserce jsou tam, tady je jen
# spuštění proti běžící instanci. Nic nemění, jen čte, takže se dá pustit
# kdykoliv i proti produkci.
#
# PROČ SAMOSTATNÝ SKRIPT, KDYŽ TÉŽ KONTROLU DĚLÁ `04-init-db.sh`. Protože
# init se pouští JEDNOU, při zakládání instance, a díra z D6 vznikla POTOM:
# migrace 03 a 04 dorazily do repozitáře později a na komerci je nikdo
# neaplikoval. Kontrola, která běží jen při initu, takový rozjezd z principu
# nechytí — musí jít spustit po každém `git pull` a po každém nasazení.
#
# Návratový kód 1 = schéma neodpovídá. Je to záměrně tvrdá chyba, ne
# varování: běžet na půl zmigrovaném schématu znamená tichou degradaci
# kvality hledání (viz hlavička 05-verify.sql), a ta se sama neprojeví.
#
# POUŽITÍ (uvnitř LXC):
#   scripts/41-schema-check.sh
# Ze stanice:
#   ssh root@192.168.88.1 'pct exec 202 -- /root/deploy/scripts/41-schema-check.sh'
# =====================================================================
set -euo pipefail

SQL_FILE="${SQL_FILE:-/sql/05-verify.sql}"

if ! podman exec postgres pg_isready -U postgres -q; then
    echo "CHYBA: postgres nebeži" >&2
    exit 1
fi

if ! podman exec postgres test -f "$SQL_FILE"; then
    echo "CHYBA: $SQL_FILE v kontejneru není — nasadil jsi aktuální sql/?" >&2
    exit 1
fi

echo "== kontrola schematu retrieval proti migracim =="
if podman exec -i postgres psql -X -v ON_ERROR_STOP=1 \
        -U postgres -d retrieval -f "$SQL_FILE"; then
    echo "== schema odpovida migracim =="
else
    echo >&2
    echo "== SCHEMA NEODPOVIDA. Doaplikuj chybejici migrace: ==" >&2
    echo "   podman exec -i postgres psql -X -v ON_ERROR_STOP=1 -U postgres \\" >&2
    echo "       -d retrieval -f /sql/03-multilang.sql" >&2
    echo "   (pak 04-context-expand.sql; oboji je idempotentni)" >&2
    exit 1
fi
