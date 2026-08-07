#!/usr/bin/env bash
# Stav zátěžového testu. Spustitelné kdykoliv, i za dva dny, i opakovaně.
#     /root/deploy/scripts/11-scale-report.sh
set -uo pipefail

STATE=/root/corpus/state
RS=http://10.89.7.13:8080

echo "=== FÁZE ==="
for p in fetch build dropidx index hnsw measure; do
    if [ -f "$STATE/$p.done" ]; then
        printf "  %-8s hotovo  (%s)\n" "$p" "$(date -r "$STATE/$p.done" -u +%F\ %T)"
    else
        printf "  %-8s ČEKÁ\n" "$p"
    fi
done

echo
if [ -f "$STATE/FINISHED" ]; then
    echo "=== BĚH DOKONČEN: $(cat "$STATE/FINISHED") ==="
else
    echo "=== BĚH JEŠTĚ NEDOKONČEN ==="
    if systemctl is-active --quiet scale-test 2>/dev/null; then
        echo "    služba scale-test běží"
    else
        echo "    služba scale-test NEBĚŽÍ — buď skončila, nebo spadla."
        echo "    log: journalctl -u scale-test --no-pager | tail -40"
    fi
fi

echo
echo "=== AKTUÁLNÍ STAV INDEXU ==="
curl -fsS --max-time 20 "$RS/stats" 2>/dev/null \
    | python3 /root/deploy/scripts/corpus_status.py \
    || echo "  retrieval neodpovídá nebo vrátil neplatná data"

echo
echo "=== KORPUS NA DISKU ==="
printf "  dokumentů: %s\n" "$(ls -1 /srv/brain/markdown/_scale/*.md 2>/dev/null | wc -l)"
du -sh /srv/brain/markdown/_scale 2>/dev/null | sed 's/^/  /'
du -sh /root/corpus/raw 2>/dev/null | sed 's/^/  surové texty: /'
df -h / | tail -1 | sed 's/^/  disk: /'

if [ -f "$STATE/report.txt" ]; then
    echo
    echo "=== VÝSLEDKY MĚŘENÍ ==="
    cat "$STATE/report.txt"
fi
