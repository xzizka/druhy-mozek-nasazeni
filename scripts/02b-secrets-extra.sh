#!/usr/bin/env bash
# =====================================================================
# DOPLNĚK k 02-guest-bootstrap.sh — v ZIPu nebyl, přidáno při nasazení.
#
# Řeší dvě věci, které původní skripty nepokrývají:
#
# 1) DSN secrets. Quadlety litellm a kryton skládaly DATABASE_URL z ${PGPW}:
#       Environment=DATABASE_URL=postgresql://litellm_app:${PGPW}@postgres:5432/litellm
#       Secret=pg_litellm_pw,type=env,target=PGPW
#    To nefunguje. Systemd/quadlet ${PGPW} neexpanduje, protože podman secret
#    se injektuje až v kontejneru — do kontejneru by šel literální "${PGPW}"
#    a LiteLLM by dostal rozbitý DSN. Celý DSN proto ukládáme jako jeden secret.
#
# 2) Placeholder externí API klíče. Quadlet litellm secrets openrouter_api_key
#    a bigpickle_api_key VYŽADUJE — bez nich unit nenastartuje vůbec, ne že by
#    jen selhávala volání. Vytváříme je s jasně rozpoznatelnou hodnotou.
#
# Spouštěj jako root uvnitř kontejneru po 02-guest-bootstrap.sh
# a před 03-quadlets.sh. Idempotentní.
# =====================================================================
set -euo pipefail

PLACEHOLDER='PLACEHOLDER-NAHRAD-SKUTECNYM-KLICEM'

sec() { podman secret inspect --showsecret --format '{{.SecretData}}' "$1"; }

mk_dsn() {
    local name="$1" user="$2" db="$3" pwsecret="$4"
    if podman secret exists "$name" 2>/dev/null; then
        echo "secret $name už existuje, přeskakuji"
        return
    fi
    if ! podman secret exists "$pwsecret" 2>/dev/null; then
        echo "CHYBA: secret $pwsecret neexistuje — spusť nejdřív 02-guest-bootstrap.sh" >&2
        exit 1
    fi
    # Hesla z mk_secret jsou alfanumerická (openssl rand | tr -d '/+='),
    # takže je není třeba percent-encodovat do URL.
    printf 'postgresql://%s:%s@postgres:5432/%s' "$user" "$(sec "$pwsecret")" "$db" \
        | podman secret create "$name" -
    echo "secret $name vytvořen"
}

mk_dsn litellm_database_url   litellm_app   litellm   pg_litellm_pw
mk_dsn kryton_database_url    kryton_app    kryton    pg_kryton_pw
# Analytika Krytona (P1b): agregační dotazy se počítají nad databází
# retrievalu pod rolí, která tam má JEN SELECT. To je ta podstatná pojistka
# proti SQL, které píše model — kontrola řetězce v analytics.py je až druhá
# vrstva. Bez tohohle secretu se analytika prostě nezapne.
mk_dsn platform_ro_url        platform_ro   retrieval pg_ro_pw
# Retrieval Service bude v Pythonu (psycopg chce libpq URL), ne v PHP,
# jak naznačoval původní PDO DSN v quadletu.
mk_dsn retrieval_database_url retrieval_app retrieval pg_retrieval_pw

for s in openrouter_api_key bigpickle_api_key; do
    if podman secret exists "$s" 2>/dev/null; then
        echo "secret $s už existuje, přeskakuji"
    else
        printf '%s' "$PLACEHOLDER" | podman secret create "$s" -
        echo "POZOR: secret $s vytvořen jako PLACEHOLDER — volání modelů budou selhávat"
    fi
done

echo
echo "Hotovo. Až budeš mít skutečné klíče:"
echo "  podman secret rm openrouter_api_key"
echo "  printf '%s' 'sk-or-...' | podman secret create openrouter_api_key -"
echo "  systemctl restart litellm"
