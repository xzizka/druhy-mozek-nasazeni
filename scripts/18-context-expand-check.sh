#!/usr/bin/env bash
# =====================================================================
# Živá kontrola context window expansion (app/expand.py) proti
# SKUTEČNÉ databázi a SKUTEČNÉ službě — žádný smoke test s odstubovanou
# DB by hranici mezi chunky ani ordinal neodhalil (stejná třída chyb,
# kvůli které vznikl 14-analytics-check.sh).
#
# Postup: zapíše testovací dokument s pěti nadpisy (chunker.py dělá
# z každého nadpisu vlastní chunk, viz `_sections()`), zaindexuje,
# zeptá se přesně na PROSTŘEDNÍ chunk s `expand:true, expand_window:1`
# a ověří, že odpověď obsahuje i oba sousední chunky, ale ne ty za nimi.
# Bez funkční expanze by test spadl — to je součást kontroly, ne jen
# happy path. Nakonec smaže testovací soubor a přeindexuje, ať index
# nezůstane nekonzistentní (stejná disciplína jako u testu /zachytit
# v kryton-nasazen.md).
#
# Spouštěj na brainu po každé změně expand.py, main.py, db.py nebo
# hybrid_search (sql/04-context-expand.sql):
#     /root/deploy/scripts/18-context-expand-check.sh
# =====================================================================
set -uo pipefail

RS=http://10.89.7.13:8080
MD=/srv/brain/markdown/_test-expand.md
PG="podman exec -i postgres psql -X -U postgres -d retrieval -tAc"

FAIL=0

check() {
    local name="$1" ok="$2" detail="${3:-}"
    if [ "$ok" = "0" ]; then
        echo "  OK    $name"
    else
        echo "  CHYBA $name${detail:+ -- $detail}"
        FAIL=1
    fi
}

has() { grep -qF -- "$1" <<<"$2"; }

cleanup() {
    rm -f "$MD"
    curl -fsS -X POST "$RS/reindex?wait=true" --max-time 60 >/dev/null 2>&1
}
trap cleanup EXIT

# lang: cs vynechá detekci pres cheap - test nesmi zavisel na LiteLLM.
cat > "$MD" <<'EOF'
---
lang: cs
---

## Test Alfa

Znacka ALFA a doprovodny text, aby chunk prekrocil minimalni delku a
nesloucil se s jinou sekci pri chunkovani na zacatku dokumentu.

## Test Beta

Znacka BETA a doprovodny text, aby chunk prekrocil minimalni delku a
nesloucil se s jinou sekci pri chunkovani na zacatku dokumentu.

## Test Gama

Znacka GAMA a doprovodny text, aby chunk prekrocil minimalni delku a
nesloucil se s jinou sekci pri chunkovani na zacatku dokumentu.

## Test Delta

Znacka DELTA a doprovodny text, aby chunk prekrocil minimalni delku a
nesloucil se s jinou sekci pri chunkovani na zacatku dokumentu.

## Test Epsilon

Znacka EPSILON a doprovodny text, aby chunk prekrocil minimalni delku a
nesloucil se s jinou sekci pri chunkovani na zacatku dokumentu.
EOF

echo "Zaindexuji testovaci dokument..."
curl -fsS -X POST "$RS/reindex?wait=true" --max-time 60 >/dev/null
echo

N=$($PG "SELECT count(*) FROM retrieval.chunk c JOIN retrieval.document d ON d.id=c.document_id WHERE d.source_path='_test-expand.md';")
[ "$N" = "5" ]; check "vzniklo presne 5 chunku (jeden na nadpis)" "$?" "N=$N"

echo
echo "Hledam GAMA (prostredni chunk, ordinal 2) s expand:true, expand_window:1..."
RESP=$(curl -fsS -X POST "$RS/search" -H 'Content-Type: application/json' \
    -d '{"query":"GAMA","keywords":"GAMA","rerank":false,"limit":1,"expand":true,"expand_window":1,"max_trust":2}' \
    --max-time 30)

CONTENT=$(python3 -c "import json,sys; print(json.load(sys.stdin)['results'][0]['content'])" <<<"$RESP" 2>/dev/null)
HEADING=$(python3 -c "import json,sys; print(json.load(sys.stdin)['results'][0].get('heading_path',''))" <<<"$RESP" 2>/dev/null)
NRESULTS=$(python3 -c "import json,sys; print(len(json.load(sys.stdin)['results']))" <<<"$RESP" 2>/dev/null)

has GAMA "$CONTENT";    check "expanze dotahla GAMA (vitezny chunk)" "$?"
has BETA "$CONTENT";    check "expanze dotahla BETA (sousedni chunk, ordinal 1)" "$?"
has DELTA "$CONTENT";   check "expanze dotahla DELTA (sousedni chunk, ordinal 3)" "$?"
! has ALFA "$CONTENT";    check "expanze NEDOTAHLA ALFA (mimo okno +-1)" "$?"
! has EPSILON "$CONTENT"; check "expanze NEDOTAHLA EPSILON (mimo okno +-1)" "$?"

[[ "$HEADING" == *Beta* && "$HEADING" == *Gama* && "$HEADING" == *Delta* ]]
check "heading_path spoji vsechny tri slouzene nadpisy" "$?" "$HEADING"

[ "$NRESULTS" = "1" ]; check "limit:1 po expanzi vraci 1 slouceny vysledek" "$?" "N=$NRESULTS"

echo
echo "Overuji expand:false pro srovnani (kontrola, ze jde expanzi vypnout)..."
RESP2=$(curl -fsS -X POST "$RS/search" -H 'Content-Type: application/json' \
    -d '{"query":"GAMA","keywords":"GAMA","rerank":false,"limit":1,"expand":false,"max_trust":2}' \
    --max-time 30)
CONTENT2=$(python3 -c "import json,sys; print(json.load(sys.stdin)['results'][0]['content'])" <<<"$RESP2" 2>/dev/null)
! has BETA "$CONTENT2"; check "s expand:false NEOBSAHUJE sousedy" "$?"

echo
if [ "$FAIL" = "1" ]; then
    echo "SELHALO"
    exit 1
fi
echo "VSECHNY KONTROLY PROSLY"
