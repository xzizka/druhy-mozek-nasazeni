#!/usr/bin/env bash
# =====================================================================
# Zaloha DB kryton + litellm a sifrovaneho balicku podman secrets na S3,
# s OVERENIM OBNOVY PRI KAZDEM BEHU.
#
# Netestovana zaloha je schrodingerovska zaloha - nevis, jestli funguje,
# dokud ji nepotrebujes, a pak uz je pozde. Skript proto po kazdem
# uploadu hned zkusi zalohu stahnout zpet a obnovit (DB do zahozitelne
# databaze, secrets jen dekryptovat a porovnat sadu jmen), a teprve
# PO overeni smaze zalohy starsi nez BACKUP_RETENTION_DAYS - kdyby
# aktualni beh selhal, stare zalohy zustanou, misto aby zbyla jen
# nefunkcni nejnovejsi.
#
# Rozsah:
#   - DB kryton (konverzace, nahravky) - neni derivovana, potreba.
#   - DB litellm (virtualni klice, rozpocty, spend logy) - stejne.
#   - DB retrieval NENI v zaloze - je to derivovany index, kdykoliv
#     postavitelny znovu z markdownu (ktery zalohuje brain-markdown-sync).
#   - podman secrets (hesla k DB, API klice, S3 pristup) - SIFROVANE,
#     protoze i "privatni" S3 bucket neni misto pro citelna hesla.
#     Sifrovaci klic (podman secret backup_encryption_key) zije JEN na
#     brainu - viz scripts/20-kryton-backup-setup.sh pro jeho vygenerovani
#     a POVINNOST ulozit kopii MIMO brain (napr. password manager).
#     Bez ni zalohu secrets nikdo nikdy nerozsifruje; DB dumpy na tomto
#     klici nezavisi.
#
# Format DB dumpu: pg_dump -Fc (custom, komprimovany sam o sobe zlibem).
# Uloziste: znovu S3 profil z app/storage.py (Backblaze, ale genericky
# pres S3 API) - jen jiny prefix (BACKUP_S3_PREFIX) nez originaly
# nahranych dokumentu (S3_KEY_PREFIX), a volitelne jiny bucket
# (BACKUP_S3_BUCKET, prazdny = stejny jako S3_BUCKET).
#
# Zadny docasny soubor na disku brainu - vsechno teka rourou mezi dvema
# `podman exec` (postgres <-> kryton), presne jako u P2/P3 uploadu.
#
# Spousti systemd timer kryton-backup.timer (denne, scripts/20-...).
# Rucne:
#     /root/deploy/scripts/19-kryton-backup.sh
# =====================================================================
set -uo pipefail

TS=$(date -u +%Y%m%dT%H%M%SZ)
PG="podman exec -i postgres psql -X -v ON_ERROR_STOP=1 -U postgres"
FAIL=0

# --- upload dat do S3 pres app/storage.py (kryton uz ma boto3 a S3 secrety) ---
_upload() {  # _upload <klic>   (data na stdin)
    podman exec -i -e KEY="$1" kryton python3 -c '
import os, sys
sys.path.insert(0, "/srv")
from app import storage
key = storage.config.BACKUP_S3_PREFIX + os.environ["KEY"]
storage.put_backup(sys.stdin.buffer.read(), key)
'
}

_download() {  # _download <klic>   (data na stdout)
    podman exec -i -e KEY="$1" kryton python3 -c '
import os, sys
sys.path.insert(0, "/srv")
from app import storage
key = storage.config.BACKUP_S3_PREFIX + os.environ["KEY"]
sys.stdout.buffer.write(storage.get_backup(key))
'
}

_rotate() {  # _rotate <podprefix, napr. "kryton/">
    podman exec -i -e SUBPREFIX="$1" kryton python3 -c '
import os, sys
sys.path.insert(0, "/srv")
from datetime import datetime, timedelta, timezone
from app import storage, config
hranice = datetime.now(timezone.utc) - timedelta(days=config.BACKUP_RETENTION_DAYS)
prefix = config.BACKUP_S3_PREFIX + os.environ["SUBPREFIX"]
smazano = 0
for b in storage.list_backups(prefix):
    if b["last_modified"] < hranice:
        storage.delete_backup(b["key"])
        print("  smazano:", b["key"])
        smazano += 1
print("  %s: smazano %d zaloh starsich nez %d dni" % (
    os.environ["SUBPREFIX"], smazano, config.BACKUP_RETENTION_DAYS))
'
}

# --- jedna databaze: dump, upload, obnova do zahozitelne DB, porovnani ---
backup_db() {
    local DB="$1" KEY SCRATCH NESEDI=0 TBL ZIVA OBNOVENA
    KEY="${DB}/${DB}-${TS}.dump"
    SCRATCH="${DB}_restore_check"

    echo "== $DB: zaloha ($KEY) =="
    if ! podman exec -i postgres pg_dump -Fc -U postgres "$DB" | _upload "$KEY"; then
        echo "  CHYBA: dump nebo upload $DB selhal" >&2
        return 1
    fi

    echo "== $DB: overeni obnovy =="
    $PG -d postgres -c "DROP DATABASE IF EXISTS ${SCRATCH};" >/dev/null
    $PG -d postgres -c "CREATE DATABASE ${SCRATCH} OWNER postgres;" >/dev/null
    if ! _download "$KEY" | podman exec -i postgres pg_restore -U postgres -d "$SCRATCH"; then
        echo "  CHYBA: pg_restore $DB selhal - zaloha NEOVERENA, nerotuji stare" >&2
        $PG -d postgres -c "DROP DATABASE IF EXISTS ${SCRATCH};" >/dev/null
        return 1
    fi

    # Pocty radku pres VSECHNY tabulky dynamicky (information_schema), ne
    # napevno vypsane jmena - litellm ma 69 tabulek generovanych Prismou.
    while IFS= read -r TBL; do
        [ -z "$TBL" ] && continue
        # </dev/null je NUTNE: `podman exec -i` uvnitr tehle smycky by jinak
        # sdilel stdin s `< <(...)` procesni substitucí nize a vycerpal by ho
        # po prvni tabulce - presne tak se to prvni beh chytilo (1 tabulka
        # z 6/69 misto vsech).
        ZIVA=$($PG -d "$DB" -tAc "SELECT count(*) FROM ${TBL};" </dev/null 2>/dev/null || echo "?")
        OBNOVENA=$($PG -d "$SCRATCH" -tAc "SELECT count(*) FROM ${TBL};" </dev/null 2>/dev/null || echo "?")
        if [ "$ZIVA" = "$OBNOVENA" ]; then
            echo "  OK    $TBL: $ZIVA řádků"
        else
            echo "  CHYBA $TBL: živá=$ZIVA obnovená=$OBNOVENA"
            NESEDI=1
        fi
    done < <($PG -d "$DB" -tAc "SELECT quote_ident(table_schema)||'.'||quote_ident(table_name) \
                                FROM information_schema.tables \
                                WHERE table_schema NOT IN ('pg_catalog','information_schema') \
                                  AND table_type='BASE TABLE';")

    $PG -d postgres -c "DROP DATABASE IF EXISTS ${SCRATCH};" >/dev/null

    if [ "$NESEDI" = "1" ]; then
        echo "  CHYBA: obnovená $DB nesedí na živou - nerotuji staré zálohy" >&2
        return 1
    fi
    echo "  obnova $DB ověřena"
    _rotate "${DB}/"
}

# --- secrets: sifrovany balicek, overeni jen sady jmen (hodnoty nikam netecou) ---
backup_secrets() {
    local KEY="secrets/secrets-${TS}.env.enc" ZIVE OBNOVENE
    ZIVE=$(podman secret ls --format '{{.Name}}' | grep -vx backup_encryption_key | sort)

    echo "== secrets: záloha (šifrovaná, ${TS}) =="
    if ! { for s in $ZIVE; do
             printf '%s=%s\n' "$s" "$(podman secret inspect --showsecret --format '{{.SecretData}}' "$s")"
           done; } \
         | podman exec -i kryton openssl enc -aes-256-cbc -pbkdf2 -salt \
               -pass file:/run/secrets/backup_encryption_key \
         | _upload "$KEY"; then
        echo "  CHYBA: záloha secrets selhala" >&2
        return 1
    fi

    echo "== secrets: ověření (jen jména, hodnoty se nikam nepíšou) =="
    OBNOVENE=$(_download "$KEY" \
        | podman exec -i kryton openssl enc -d -aes-256-cbc -pbkdf2 \
              -pass file:/run/secrets/backup_encryption_key \
        | cut -d= -f1 | sort)

    if [ "$ZIVE" = "$OBNOVENE" ]; then
        echo "  OK    sada secretů sedí ($(echo "$ZIVE" | wc -l) položek)"
    else
        echo "  CHYBA sada secretů v záloze nesedí na živý stav (špatný klíč? poškozená záloha?)" >&2
        return 1
    fi
    _rotate "secrets/"
}

backup_db kryton   || FAIL=1
echo
backup_db litellm  || FAIL=1
echo
backup_secrets     || FAIL=1
echo

if [ "$FAIL" = "1" ]; then
    echo "NEKTERA ZALOHA SELHALA NEBO SE NEOVERILA - viz vypis vys" >&2
    exit 1
fi
echo "VSECHNY ZALOHY OK A OVERENE (obnova skutecne zkousena, ne jen predpokladana)"
