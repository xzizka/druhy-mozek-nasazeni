#!/usr/bin/env bash
# =====================================================================
# Fáze 1 - inicializace databází a schémat
#
# Spouštěj po startu kontejneru postgres. Idempotentní není - je to
# jednorázová inicializace, opakované spuštění selže na CREATE ROLE.
# =====================================================================
set -euo pipefail

sec() { podman secret inspect --showsecret --format '{{.SecretData}}' "$1"; }

PG="podman exec -i postgres psql -X -v ON_ERROR_STOP=1"

echo "== čekám na Postgres =="
for i in $(seq 1 30); do
    podman exec postgres pg_isready -U postgres -q && break
    sleep 2
done

echo "== 01-bootstrap.sql: role, databáze, rozšíření =="
$PG -U postgres -d postgres \
    -v kryton_pw="$(sec pg_kryton_pw)" \
    -v retrieval_pw="$(sec pg_retrieval_pw)" \
    -v litellm_pw="$(sec pg_litellm_pw)" \
    -v ro_pw="$(sec pg_ro_pw)" \
    -f /sql/01-bootstrap.sql

echo "== 02-retrieval.sql: česká FTS, schéma, indexy, hybrid_search =="
$PG -U postgres -d retrieval -f /sql/02-retrieval.sql

# ---------------------------------------------------------------------
# Migrace nad základem. DO 2026-09-18 SE TYHLE DVA SOUBORY NEPOUŠTĚLY —
# init končil na 02 a 03/04 zůstávaly na operátorovi, který o nich musel
# vědět. Komerční instance tak dva dny běžela na schématu bez `document.lang`
# a nikdo si toho nevšiml, protože prázdný korpus na ten sloupec nesáhne
# (D6, 2026-09-17). Obojí je idempotentní, takže je správně pustit vždycky.
#
# Pořadí je závazné: 04 dropuje a znovu staví `hybrid_search` na podpisu,
# který zavádí 03.
# ---------------------------------------------------------------------
echo "== 03-multilang.sql: jazyk per dokument, ts_config per chunk =="
$PG -U postgres -d retrieval -f /sql/03-multilang.sql

echo "== 04-context-expand.sql: hybrid_search vrací ordinal =="
$PG -U postgres -d retrieval -f /sql/04-context-expand.sql

# ---------------------------------------------------------------------
# Ověření, že to celé skutečně funguje. Tenhle test je tu proto, že
# selhání české FTS konfigurace je tiché - lexikální větev prostě
# přestane nacházet a projeví se to jako "špatný retrieval", ne jako chyba.
#
# Aserce žijí v `sql/05-verify.sql`, aby šla táž kontrola pustit i jindy než
# při initu (`scripts/41-schema-check.sh`). Blok, který tu stál dřív, ověřoval
# stav PO 02 — tedy přesně to, co 02 vytvoří — takže chybějící 03/04 mlčky
# prošly. Tím se z ověření stalo razítko a díra z D6 dva dny vydržela.
# ---------------------------------------------------------------------
echo "== ověření =="
$PG -U postgres -d retrieval -f /sql/05-verify.sql

echo "== hotovo. Dále: systemctl start infinity litellm retrieval kryton =="
