#!/usr/bin/env bash
# =====================================================================
# První hromadné naplnění indexu — operátorský krok.
#
# PROČ NENÍ SOUČÁSTÍ SLUŽBY: role retrieval_app má podle návrhu jen DML.
# Ověřeno, že tabulku retrieval.chunk vlastní `postgres`, takže služba
# nemůže zahodit ani postavit index — DDL na cizí tabulce vyžaduje
# vlastnictví. Návrh to tak chce: "aplikace nesmí měnit schéma".
#
# Postup podle README: inkrementální insert do HNSW je řádově pomalejší
# než build nad hotovou tabulkou, takže při velkém naplnění se index
# zahodí, tabulka naplní a index postaví znovu.
#
# POZOR: pro malé inkrementální běhy tohle NEPOUŽÍVEJ — přestavět index
# kvůli několika chunkům je mnohonásobně dražší než ty inserty. Stačí
#     curl -X POST http://localhost:8080/reindex
#
# Spouštěj jako root uvnitř kontejneru.
# =====================================================================
set -euo pipefail

PG="podman exec -i postgres psql -X -U postgres -d retrieval -v ON_ERROR_STOP=1"
RS="http://127.0.0.1:8080"

echo "== stav pred =="
curl -fsS "$RS/stats" || { echo "retrieval sluzba neodpovida" >&2; exit 1; }
echo

echo "== zahazuji chunk_embedding_hnsw =="
$PG -c "DROP INDEX IF EXISTS retrieval.chunk_embedding_hnsw;"

echo "== reindex (synchronne, muze trvat: ~1,8 s na dlouhy chunk) =="
curl -fsS -X POST "$RS/reindex?wait=true" --max-time 86400
echo

echo "== stavim HNSW index =="
# maintenance_work_mem jen pro tuto session - je to spicka, ne trvala alokace.
# Kdyz se build nevejde, spadne do spill-to-disk a poteče nekolikanasobne dele.
$PG <<'SQL'
SET maintenance_work_mem = '1GB';
SET max_parallel_maintenance_workers = 2;
CREATE INDEX chunk_embedding_hnsw ON retrieval.chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
SQL

echo "== VACUUM ANALYZE =="
$PG -c "VACUUM ANALYZE retrieval.chunk;"
$PG -c "VACUUM ANALYZE retrieval.document;"

echo "== stav po =="
curl -fsS "$RS/stats"; echo
