#!/usr/bin/env bash
# =====================================================================
# Fáze 4 / D7: proof izolace tenanta — „hybrid_search funguje se
# schématem tenantů bez změny SQL" (TENANCY.md).
#
# Spouští se UVNITŘ komerčního LXC (pct exec 202 -- /tmp/21-tenancy-proof.sh),
# protože používá `podman exec postgres psql` (superuser přes lokalní socket,
# vzor 04-init-db.sh) a embedding od Infinity (localhost:7997/embeddings).
# Nedotýká se produkčního schématu `retrieval` (kontrola počtů řádků).
#
# Ověřuje:
#   1. hybrid_search v t_prf_a vrací jen dokumenty tenanta A a naopak
#   2. trust_level filtr platí i per-schema (trust=2 venku s p_max_trust=0)
#   3. UNIQUE source_path je PER SCHÉMA (stejná cesta u dvou tenantů OK,
#      duplicita uvnitř jednoho schématu = unique_violation)
#   4. RBAC: platform_ro/cizí tenant nedostane cizí SELECT ani INSERT
#   5. produkční `retrieval` schéma zůstává nedotčeno
#
# Opakovatelné: na začátku smaže `t_prf_*` z minuhop běhu. Výstup na
# konci: `RESULT <jméno> OK/FAIL`, return code 0 jen když vše OK.
# =====================================================================
set -u
BASEDIR="$(cd "$(dirname "$0")" && pwd)"
SQL_TEMPLATE="${BASEDIR}/../sql/90-tenant.sql"
[ -f "$SQL_TEMPLATE" ] || SQL_TEMPLATE="${BASEDIR}/sql/90-tenant.sql"
PG="podman exec -i postgres psql -X -U postgres -d retrieval -tA"
PREFIX=prf_             # jména tenantů: t_${PREFIX}a, t_${PREFIX}b
A="t_${PREFIX}a"; B="t_${PREFIX}b"
A_ROLE="${A}_app"; B_ROLE="${B}_app"
DOC_A_SRC="/tenants/test-a/markdown/smlouva-o-dile.md"
DOC_B_SRC="/tenants/test-b/markdown/faktura-2026-041.md"

# --- embedding helper (LXC python3, fallback do kryton kontejneru) ---
_embed() { # $1 = text -> stdout: "[1024 floats]"
  local out
  out=$(curl -s -m 20 http://localhost:7997/embeddings -X POST \
        -H content-type:application/json \
        -d "{\"input\":[\"$1\"],\"model\":\"bge-m3\"}")
  echo "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("["+",".join("%.6f"%x for x in d["data"][0]["embedding"])+"]")' \
    || echo "$out" | podman exec -i kryton python3 -c 'import sys,json;d=json.load(sys.stdin);print("["+",".join("%.6f"%x for x in d["data"][0]["embedding"])+"]")'
}

FAILS=0
assert() { # assert <jméno> <očekávaný> <skutečný>
  if [ "$2" = "$3" ]; then echo "RESULT $1 OK"; else echo "RESULT $1 FAIL (očekaváno: $2 | skutečnost: $3)"; FAILS=$((FAILS+1)); fi
}

# --- 0. čistota: smazat případný minulý běh, změřit produkci ---
$PG -c "DROP SCHEMA IF EXISTS $A CASCADE; DROP SCHEMA IF EXISTS $B CASCADE; \
        DROP ROLE IF EXISTS $A_ROLE; DROP ROLE IF EXISTS $B_ROLE;" >/dev/null
PROD_DOC_BEFORE=$($PG -c "SELECT count(*) FROM retrieval.document;")
PROD_CHUNK_BEFORE=$($PG -c "SELECT count(*) FROM retrieval.chunk;")

# --- 1. role a schémata přes šablonu 90-tenant.sql ---
$PG -c "CREATE ROLE $A_ROLE NOLOGIN; CREATE ROLE $B_ROLE NOLOGIN;" >/dev/null
for T in "$A:$A_ROLE" "$B:$B_ROLE"; do
  S="${T%%:*}"; R="${T##*:}"
  podman exec -i postgres psql -X -U postgres -d retrieval -v ON_ERROR_STOP=1 \
      -v t="$S" -v owner="$R" -f - < "$SQL_TEMPLATE" >/dev/null || { echo "RESULT migrace $S FAIL"; exit 1; }
done

# --- 2. embeddingy dokumentů a dotazu ---
DOC_A_TEXT="Smlouva o dílo mezi zhotovitelem a objednatelem na dodávku uctovniho poradce. Cena díla je 120000 Kc bez DPH. Termin predani díla je do desateho dne nasledujiciho mesice."
DOC_B_TEXT="Faktura cislo 2026-041 vystavena dne 2026-02-10 odberateli Albert sro. Castka k uhrade 54360 Kc, DPH 21 procent, splatnost bez prodleni."
QUERY_TEXT="Kolik stojí dílo podle smlouvy?"
VEC_A=$(_embed "$DOC_A_TEXT")
VEC_B=$(_embed "$DOC_B_TEXT")
VEC_Q=$(_embed "$QUERY_TEXT")
assert "embed dokumentu A (1024 dimenzi)" "$(echo "$VEC_A" | tr -cd ',' | wc -c)" "1023"
assert "embed dokumentu B (1024 dimenzi)" "$(echo "$VEC_B" | tr -cd ',' | wc -c)" "1023"
assert "embed dotazu (1024 dimenzi)" "$(echo "$VEC_Q" | tr -cd ',' | wc -c)" "1023"

# --- 3. vložení: A do t_prf_a (trust 0), B do t_prf_b (trust 1) ---
I_A=$($PG -v ON_ERROR_STOP=1 <<SQL
INSERT INTO $A.document (id, source_path, title, content_hash, trust_level, chunk_count)
VALUES ('00000000-0000-0000-0000-00000000000a', '$DOC_A_SRC', 'smlouva a', decode('aa','hex'), 0, 1);
INSERT INTO $A.chunk (document_id, ordinal, content, embedding)
VALUES ('00000000-0000-0000-0000-00000000000a', 1, '$DOC_A_TEXT', '$VEC_A');
SELECT count(*) FROM $A.chunk;
SQL
)
I_B=$($PG -v ON_ERROR_STOP=1 <<SQL
INSERT INTO $B.document (id, source_path, title, content_hash, trust_level, chunk_count)
VALUES ('00000000-0000-0000-0000-00000000000b', '$DOC_B_SRC', 'faktura b', decode('bb','hex'), 1, 1);
INSERT INTO $B.chunk (document_id, ordinal, content, embedding)
VALUES ('00000000-0000-0000-0000-00000000000b', 1, '$DOC_B_TEXT', '$VEC_B');
SELECT count(*) FROM $B.chunk;
SQL
)
assert "tenant A má 1 chunk v indexu" "$(echo "$I_A" | tail -1)" "1"
assert "tenant B má 1 chunk v indexu" "$(echo "$I_B" | tail -1)" "1"
$PG -c "ANALYZE $A.chunk; ANALYZE $B.chunk;" >/dev/null

# --- 4. vlastní ověření hybrid_search per schema (reálný dotaz) ---
Q_A=$($PG -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $A.hybrid_search(p_embedding=>'$VEC_Q'::halfvec(1024), p_query=>'$QUERY_TEXT');")
Q_A_SRC=$($PG -v ON_ERROR_STOP=1 -c "SELECT r.source_path FROM $A.hybrid_search(p_embedding=>'$VEC_Q'::halfvec(1024), p_query=>'$QUERY_TEXT') r;")
Q_B=$($PG -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $B.hybrid_search(p_embedding=>'$VEC_Q'::halfvec(1024), p_query=>'$QUERY_TEXT', p_max_trust=>2::smallint);")
Q_B_SRC=$($PG -v ON_ERROR_STOP=1 -c "SELECT r.source_path FROM $B.hybrid_search(p_embedding=>'$VEC_Q'::halfvec(1024), p_query=>'$QUERY_TEXT', p_max_trust=>2::smallint) r;")
assert "hybrid_search A vrací přesně 1 chunk" "$Q_A" "1"
assert "hybrid_search A vrací jen vlastní dokument" "$Q_A_SRC" "$DOC_A_SRC"
assert "hybrid_search B vrací přesně 1 chunk" "$Q_B" "1"
assert "hybrid_search B vrací jen vlastní dokument" "$Q_B_SRC" "$DOC_B_SRC"

# --- 5. trust_level filtr funguje i per-schema (B je trust 1, hledame s max=0) ---
Q_B_T0=$($PG -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $B.hybrid_search(p_embedding=>'$VEC_Q'::halfvec(1024), p_query=>'$QUERY_TEXT', p_max_trust=>0::smallint);")
assert "p_max_trust=0 vyřadí trust 1 i v tenantě" "$Q_B_T0" "0"

# --- 6. UNIQUE source_path je PER SCHÉMA ---
#  (a) A-ovská cesta u B -> OK; (b) duplicita uvnitř A -> unique_violation
DUP_OK=$($PG -v ON_ERROR_STOP=1 -c "INSERT INTO $B.document (id, source_path, title, content_hash, trust_level) VALUES ('00000000-0000-0000-0000-00000000000c', '$DOC_A_SRC', 'duplicita cesty', decode('aa','hex'), 0); SELECT 'ok';" | tail -1)
assert "stejná source_path u druhého tenanta projde" "$DUP_OK" "ok"
DUP_FAIL=$($PG -v ON_ERROR_STOP=1 -c "INSERT INTO $A.document (id, source_path, title, content_hash, trust_level) VALUES ('00000000-0000-0000-0000-00000000000d', '$DOC_A_SRC', 'duplicita', decode('aa','hex'), 0); SELECT 'duplicita_prosla';" 2>&1 | grep -c "duplicate key value violates unique constraint")
assert "duplicita source_path uvnitř tenanta = unique_violation" "$DUP_FAIL" "1"

# --- 7. RBAC: cizí tenant/služba nevidí a nepíše ---
#  (a) čtenář t_prf_a nemá číst schéma B; (b) platform_ro nemá INSERT do A
$PG -c "GRANT USAGE ON SCHEMA $A TO $A_ROLE; SELECT 'grant';" >/dev/null
READ_DENIED=$($PG -v ON_ERROR_STOP=1 -c "SET ROLE $A_ROLE; SELECT count(*) FROM $B.document;" 2>&1 | grep -c "permission denied")
assert "cizí tenant nedostane cizí SELECT" "$READ_DENIED" "1"
HAS_EXEC=$($PG -v ON_ERROR_STOP=1 -c "SET ROLE $A_ROLE; SELECT count(*) FROM $A.document;" 2>&1 | tail -1)
assert "vlastník schématu čte vlastní data" "$HAS_EXEC" "1"
WRITE_DENIED=$($PG -v ON_ERROR_STOP=1 -c "SET ROLE platform_ro; INSERT INTO $A.document (id, source_path, title, content_hash) VALUES ('00000000-0000-0000-0000-00000000000e','x','y',decode('aa','hex'));" 2>&1 | grep -c "permission denied")
assert "čtenářská role nesmí zapisovat (permission denied)" "$WRITE_DENIED" "1"

# --- 8. produkce nedotčena ---
PROD_DOC_AFTER=$($PG -c "SELECT count(*) FROM retrieval.document;")
PROD_CHUNK_AFTER=$($PG -c "SELECT count(*) FROM retrieval.chunk;")
assert "produkční dokumenty (6) nezměněny" "$PROD_DOC_AFTER" "$PROD_DOC_BEFORE"
assert "produkční chunky (27) nezměněny" "$PROD_CHUNK_AFTER" "$PROD_CHUNK_BEFORE"

echo "---"
echo "prodit: $PROD_DOC_BEFORE/$PROD_CHUNK_BEFORE -> $PROD_DOC_AFTER/$PROD_CHUNK_AFTER"
if [ "$FAILS" -gt 0 ]; then echo "PROOF D7: $FAILS selhání"; exit 1; fi
echo "PROOF D7: VŠE OK"