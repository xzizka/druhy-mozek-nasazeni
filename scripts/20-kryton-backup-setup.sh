#!/usr/bin/env bash
# =====================================================================
# Jednorazove: vygeneruje sifrovaci klic pro zalohu secrets a nastavi
# denni timer na scripts/19-kryton-backup.sh. Idempotentni - klic se
# nikdy NEPREGENERUJE, kdyby uz existoval (zneuzitelnilo by to vsechny
# stare zalohy secrets).
#
# Spoustej na brainu JEDNOU po prvnim nasazeni backup skriptu:
#     /root/deploy/scripts/20-kryton-backup-setup.sh
#
# PO SPUSTENI: klic se vypise JEDNOU. Uloz si ho HNED mimo brain
# (password manager, papir - cokoliv, co prezije i zniceni brainu).
# Bez teto kopie je sifrovana zaloha secrets k nicemu, kdyby brain
# fyzicky zmizel (DB dumpy kryton/litellm na tomto klici NEZAVISI).
# =====================================================================
set -euo pipefail

if podman secret inspect backup_encryption_key >/dev/null 2>&1; then
    echo "backup_encryption_key uz existuje - NEPREGENEROVAVAM (rozbilo by to"
    echo "existujici zalohy secrets). Pokud jsi tu kopii ztratil/a, novy klic"
    echo "musi dostat i novy jmenny prostor (napr. rucne rotovat), jinak"
    echo "stare zalohy zustanou necitelne navzdy."
else
    KLIC=$(openssl rand -base64 32)
    printf '%s' "$KLIC" | podman secret create backup_encryption_key -
    echo "=========================================================================="
    echo "NOVY SIFROVACI KLIC PRO ZALOHU SECRETS - ULOZ HO HNED MIMO BRAIN:"
    echo
    echo "$KLIC"
    echo
    echo "Bez teto kopie (napr. v password manageru) se zasifrovana zaloha"
    echo "secrets nikdy nerozsifruje, kdyby brain fyzicky zmizel. Databazove"
    echo "dumpy (kryton, litellm) na tomto klici NEZAVISI - ty zustavaji citelne."
    echo "=========================================================================="
fi

cat > /etc/systemd/system/kryton-backup.service <<'EOF'
[Unit]
Description=Zaloha DB kryton+litellm a secrets na S3, s overenim obnovy
After=postgres.service kryton.service network-online.target
Requires=postgres.service

[Service]
Type=oneshot
ExecStart=/root/deploy/scripts/19-kryton-backup.sh
EOF

cat > /etc/systemd/system/kryton-backup.timer <<'EOF'
[Unit]
Description=Denni zaloha DB kryton+litellm a secrets

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kryton-backup.timer

echo
echo "Timer aktivni. Stav: systemctl list-timers kryton-backup.timer"
echo "Rucni beh:   /root/deploy/scripts/19-kryton-backup.sh"
