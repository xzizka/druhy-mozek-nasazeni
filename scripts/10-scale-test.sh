#!/usr/bin/env bash
# =====================================================================
# Zátěžový a mechanismový test retrievalu na 1000 dokumentech
# o minimálně 100 chuncích z reálných textů (Gutenberg + Wikipedie).
#
# URČENO K BĚHU NA POZADÍ. Nejdelší fáze je embedding: ~100 000 chunků
# po ~1,2 s je 30-40 hodin. Session u toho být nemusí.
#
# SPUŠTĚNÍ (přežije odhlášení, logy v journalu):
#     systemd-run --unit=scale-test --collect \
#         /root/deploy/scripts/10-scale-test.sh
#     journalctl -u scale-test -f
#
# nebo klasicky:
#     nohup /root/deploy/scripts/10-scale-test.sh \
#         > /root/corpus/scale-test.log 2>&1 &
#
# KONTROLA KDYKOLIV POZDĚJI:
#     /root/deploy/scripts/11-scale-report.sh
#
# ZASTAVENÍ:  systemctl stop scale-test   (nebo kill)
# Běh je RESUMOVATELNÝ — po přerušení stačí spustit znovu, fáze se
# nepřepočítávají a nedokončená indexace pokračuje přes větev RESUME.
#
# ÚKLID:      /root/deploy/scripts/12-scale-cleanup.sh
# =====================================================================
set -uo pipefail

CORPUS=/root/corpus
STATE=$CORPUS/state
SCALE_DIR=/srv/brain/markdown/_scale
RS=http://10.89.7.13:8080
PG="podman exec -i postgres psql -X -U postgres -d retrieval -v ON_ERROR_STOP=1"
SCRIPTS=/root/deploy/scripts

mkdir -p "$STATE" "$CORPUS"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
done_phase()  { [ -f "$STATE/$1.done" ]; }
mark_phase()  { touch "$STATE/$1.done"; }

trap 'log "PŘERUŠENO — spusť znovu, naváže se"; exit 130' INT TERM

log "=== start, korpus $SCALE_DIR ==="
df -h / | tail -1

# ---------------------------------------------------------------------
# 0. Pojistky
# ---------------------------------------------------------------------
if ! curl -fsS --max-time 10 "$RS/healthz" >/dev/null; then
    log "CHYBA: retrieval neodpovídá na $RS/healthz"; exit 1
fi

# _scale/ nesmí skončit v gitu skutečných poznámek — brain-markdown-sync
# commituje `git add -A` a 120 MB testovacích dat tam nemá co dělat.
if ! grep -qx "_scale/" /srv/brain/markdown/.gitignore 2>/dev/null; then
    echo "_scale/" >> /srv/brain/markdown/.gitignore
    log "přidáno _scale/ do .gitignore"
fi
# POZOR: .gitignore neplatí na soubory, které už git SLEDUJE. Narazilo se na
# to při přípravě — sync job stihl testovací dokumenty commitnout dřív, než
# se .gitignore doplnil, a od té chvíle by je ignorování nezastavilo.
# Proto ještě tvrdé vyjmutí z indexu; soubory na disku zůstávají.
if git -C /srv/brain/markdown ls-files --error-unmatch _scale >/dev/null 2>&1; then
    git -C /srv/brain/markdown rm -r --cached -q _scale
    git -C /srv/brain/markdown -c user.name=brain -c user.email=brain@local \
        commit -q -m "Vyjmout _scale/ z gitu (zatezovy test)" || true
    log "_scale/ vyjmuto z git indexu"
fi

# ---------------------------------------------------------------------
# 1. Stažení reálných textů
# ---------------------------------------------------------------------
if done_phase fetch; then
    log "fáze 1 (stahování) už hotová, přeskakuji"
else
    log "--- fáze 1: stahuji Gutenberg (en, de, la, cs) ---"
    python3 "$SCRIPTS/corpus_gutenberg.py" || log "gutenberg skončil s chybou, pokračuji"
    log "--- fáze 1: doplňuji češtinu z Wikipedie ---"
    # Gutenberg má česky jen 11 knih; zbytek musí přijít odjinud.
    python3 "$SCRIPTS/corpus_wiki.py" cs 50000000 60000 || log "wiki cs selhala, pokračuji"
    # Latina z Gutenbergu je taky tenká, doplň encyklopedií.
    python3 "$SCRIPTS/corpus_wiki.py" la 13000000 20000 || log "wiki la selhala, pokračuji"
    du -sh "$CORPUS"/raw/* 2>/dev/null
    mark_phase fetch
fi

# ---------------------------------------------------------------------
# 2. Sestavení dokumentů (>= 100 chunků, ověřeno skutečným chunkerem)
# ---------------------------------------------------------------------
if done_phase build; then
    log "fáze 2 (sestavení) už hotová, přeskakuji"
else
    log "--- fáze 2: skládám dokumenty ---"
    python3 "$SCRIPTS/corpus_build.py" || { log "CHYBA při sestavení"; exit 1; }
    log "dokumentů na disku: $(ls -1 "$SCALE_DIR"/*.md 2>/dev/null | wc -l)"
    du -sh "$SCALE_DIR"
    mark_phase build
fi

# ---------------------------------------------------------------------
# 3. Indexace — ta dlouhá část
#
# HNSW se před hromadným naplněním zahazuje: inkrementální insert do
# indexu je řádově pomalejší než build nad hotovou tabulkou (README).
# Pro pár chunků by to byla pitomost, pro 100 000 je to ten správný postup.
# ---------------------------------------------------------------------
if done_phase index; then
    log "fáze 3 (indexace) už hotová, přeskakuji"
else
    if ! done_phase dropidx; then
        log "--- fáze 3a: zahazuji chunk_embedding_hnsw ---"
        $PG -c "DROP INDEX IF EXISTS retrieval.chunk_embedding_hnsw;"
        mark_phase dropidx
    fi

    log "--- fáze 3b: indexace (tohle je těch 30-40 h) ---"
    ROUND=0
    LAST_CHUNKS=-1
    STALE=0
    while true; do
        ROUND=$((ROUND + 1))
        RUNNING=$(curl -fsS --max-time 20 "$RS/stats" \
                  | python3 -c 'import sys,json;print(json.load(sys.stdin)["indexer"]["running"])' 2>/dev/null)
        if [ "$RUNNING" != "True" ]; then
            log "spouštím reindex (kolo $ROUND)"
            curl -fsS -X POST "$RS/reindex" --max-time 30 >/dev/null \
                || log "start reindexu selhal, zkusím znovu"
        fi

        # Poll: každou minutu zapiš, kam se to dostalo.
        while true; do
            sleep 60
            S=$(curl -fsS --max-time 20 "$RS/stats" 2>/dev/null) || continue
            read -r DOCS CHUNKS PENDING UNFIN RUN <<<"$(printf '%s' "$S" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d["documents"], d["chunks"], d["chunks_without_embedding"],
      d["documents_unfinished"], d["indexer"]["running"])')"
            log "  dokumentů=$DOCS chunků=$CHUNKS bez_embeddingu=$PENDING nedokončených=$UNFIN běží=$RUN"
            [ "$RUN" = "True" ] || break
        done

        S=$(curl -fsS --max-time 20 "$RS/stats")
        read -r CHUNKS PENDING UNFIN <<<"$(printf '%s' "$S" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d["chunks"], d["chunks_without_embedding"], d["documents_unfinished"])')"

        if [ "$PENDING" = "0" ] && [ "$UNFIN" = "0" ]; then
            log "indexace dokončena: $CHUNKS chunků"
            break
        fi
        # Bez postupu dvakrát po sobě = něco je špatně, nesmyčkuj donekonečna.
        if [ "$CHUNKS" = "$LAST_CHUNKS" ]; then
            STALE=$((STALE + 1))
            if [ "$STALE" -ge 3 ]; then
                log "CHYBA: tři kola bez postupu (chunků=$CHUNKS, čeká=$PENDING). Končím."
                exit 1
            fi
        else
            STALE=0
        fi
        LAST_CHUNKS=$CHUNKS
        log "kolo $ROUND nedoběhlo do konce, pokračuji (RESUME)"
    done
    mark_phase index
fi

# ---------------------------------------------------------------------
# 4. Postavení HNSW nad hotovou tabulkou
# ---------------------------------------------------------------------
if done_phase hnsw; then
    log "fáze 4 (HNSW) už hotová, přeskakuji"
else
    log "--- fáze 4: stavím HNSW index ---"
    T0=$(date +%s)
    $PG <<'SQL'
SET maintenance_work_mem = '1GB';
SET max_parallel_maintenance_workers = 2;
CREATE INDEX IF NOT EXISTS chunk_embedding_hnsw ON retrieval.chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
SQL
    T1=$(date +%s)
    echo $((T1 - T0)) > "$STATE/hnsw_seconds"
    log "HNSW postaven za $((T1 - T0)) s"
    $PG -c "VACUUM ANALYZE retrieval.chunk;"
    $PG -c "VACUUM ANALYZE retrieval.document;"
    mark_phase hnsw
fi

# ---------------------------------------------------------------------
# 5. Měření a mechanismové testy
# ---------------------------------------------------------------------
log "--- fáze 5: měření ---"
python3 "$SCRIPTS/corpus_measure.py" > "$STATE/report.txt" 2>&1
RC=$?
mark_phase measure
log "hotovo, report v $STATE/report.txt (návratový kód $RC)"
echo "SCALE-TEST-FINISHED $(date -u +%FT%TZ)" > "$STATE/FINISHED"
cat "$STATE/report.txt"
