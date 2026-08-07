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
# Ověření, že to celé skutečně funguje. Tenhle test je tu proto, že
# selhání české FTS konfigurace je tiché - lexikální větev prostě
# přestane nacházet a projeví se to jako "špatný retrieval", ne jako chyba.
# ---------------------------------------------------------------------
echo "== ověření =="
podman exec -i postgres psql -X -U postgres -d retrieval -tA <<'SQL'
\set ON_ERROR_STOP on
DO $$
DECLARE tsv text; n int;
BEGIN
    SELECT to_tsvector('czech','Ladím latenci vektorových indexů')::text INTO tsv;
    IF tsv NOT LIKE '%ladit%' OR tsv NOT LIKE '%latence%' THEN
        RAISE EXCEPTION 'ceska lemmatizace NEFUNGUJE: %', tsv;
    END IF;

    SELECT count(*) INTO n FROM pg_indexes
     WHERE schemaname='retrieval'
       AND indexname IN ('chunk_embedding_hnsw','chunk_tsv_gin','chunk_trgm_gin');
    IF n <> 3 THEN RAISE EXCEPTION 'chybi indexy, naslo se %', n; END IF;

    PERFORM retrieval.hybrid_search(
        (SELECT ('['||string_agg('0.01',',')||']')::halfvec(1024)
           FROM generate_series(1,1024)), 'test', 1);

    RAISE NOTICE 'OK: FTS %, indexy 3/3, hybrid_search volatelna', tsv;
END $$;
SQL

echo "== hotovo. Dále: systemctl start infinity litellm retrieval kryton =="
