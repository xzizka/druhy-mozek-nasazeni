#!/usr/bin/env bash
# Úklid po zátěžovém testu: smaže testovací dokumenty, doindexuje a postaví
# HNSW nad tím, co zbude. Surové stažené texty nechává (jsou znovupoužitelné);
# smaž je ručně přes `rm -rf /root/corpus/raw`, jde o desítky MB.
#
#     /root/deploy/scripts/12-scale-cleanup.sh
set -euo pipefail

RS=http://10.89.7.13:8080
PG="podman exec -i postgres psql -X -U postgres -d retrieval -v ON_ERROR_STOP=1"

if systemctl is-active --quiet scale-test 2>/dev/null; then
    echo "scale-test ještě běží. Nejdřív: systemctl stop scale-test" >&2
    exit 1
fi

N=$(ls -1 /srv/brain/markdown/_scale/*.md 2>/dev/null | wc -l)
echo "Smažu $N testovacích dokumentů z /srv/brain/markdown/_scale/."
read -r -p "Pokračovat? [ano/NE] " ans
[ "$ans" = "ano" ] || { echo "zrušeno"; exit 0; }

rm -rf /srv/brain/markdown/_scale
echo "smazáno, spouštím reindex (dokumenty zmizí z DB kaskádou)"
curl -fsS -X POST "$RS/reindex?wait=true" --max-time 3600
echo

echo "přestavuji HNSW nad zbytkem"
$PG -c "DROP INDEX IF EXISTS retrieval.chunk_embedding_hnsw;"
$PG <<'SQL'
SET maintenance_work_mem = '1GB';
CREATE INDEX chunk_embedding_hnsw ON retrieval.chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
SQL
$PG -c "VACUUM ANALYZE retrieval.chunk;"
$PG -c "VACUUM ANALYZE retrieval.document;"
rm -f /root/corpus/state/*.done /root/corpus/state/FINISHED
echo
curl -fsS "$RS/stats"; echo
echo "úklid hotov"
