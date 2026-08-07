#!/usr/bin/env bash
# =====================================================================
# Git repozitář pro /srv/brain/markdown — v ZIPu nebyl, přidáno.
#
# Proč: záloha neexistuje. Jeden stroj, jeden disk, archive_mode = off.
# Markdown je autoritativní zdroj, Postgres je derivovaný index, který
# umíme kdykoliv postavit znovu. Přijít o index nic neznamená, přijít
# o poznámky znamená všechno. README to označuje za nejlevnější možnou
# pojistku a má pravdu.
#
# Skript řeší jen stranu kontejneru. Na GitHubu musíš předem:
#   1) vytvořit PRIVÁTNÍ repozitář, prázdný (bez README a .gitignore)
#   2) po prvním běhu tohoto skriptu přidat vypsaný veřejný klíč
#      jako Deploy key s právem zápisu
#
# Spouštěj jako root uvnitř kontejneru. Idempotentní.
#
# Použití:
#   ./07-markdown-git.sh git@github.com:uzivatel/druhy-mozek-poznamky.git
# =====================================================================
set -euo pipefail
umask 022

REMOTE="${1:-}"
MD=/srv/brain/markdown
KEY=/root/.ssh/id_ed25519_markdown

[ -n "$REMOTE" ] || { echo "CHYBA: zadej SSH URL repozitáře, např. git@github.com:uzivatel/repo.git" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null || { echo "== instaluji git =="; apt-get update -qq; apt-get install -y --no-install-recommends git; }

# ---------------------------------------------------------------------
# Deploy key. Vlastní klíč jen pro tento repozitář, ne ten, kterým se
# do kontejneru chodí po SSH.
# ---------------------------------------------------------------------
if [ ! -f "$KEY" ]; then
    echo "== generuji deploy key =="
    ssh-keygen -t ed25519 -N '' -C "brain-markdown@$(hostname)" -f "$KEY"
fi
install -d -m 0700 /root/.ssh
if ! grep -q 'Host github.com' /root/.ssh/config 2>/dev/null; then
    cat >> /root/.ssh/config <<EOF

Host github.com
    IdentityFile $KEY
    IdentitiesOnly yes
EOF
    chmod 0600 /root/.ssh/config
fi
grep -q '^github.com' /root/.ssh/known_hosts 2>/dev/null || \
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null

# ---------------------------------------------------------------------
# Repozitář.
# ---------------------------------------------------------------------
cd "$MD"
if [ ! -d .git ]; then
    echo "== git init =="
    git init -q -b main
    git config user.name  "brain"
    git config user.email "brain@$(hostname)"
    # Poznámky jsou UTF-8 text s diakritikou; ať git nedělá konverze konců řádků.
    printf '* text=auto eol=lf\n*.md diff\n' > .gitattributes
    # Do repozitáře nepatří nic derivovaného ani dočasného.
    printf '.obsidian/\n.trash/\n*.tmp\n*.swp\n.DS_Store\n' > .gitignore
fi
git remote get-url origin >/dev/null 2>&1 && git remote set-url origin "$REMOTE" || git remote add origin "$REMOTE"

# ---------------------------------------------------------------------
# Timer. Repozitář, do kterého nikdo necommituje, není záloha.
# Kryton do markdownu zapisuje, takže se to musí dít samo.
# ---------------------------------------------------------------------
cat > /usr/local/sbin/brain-markdown-sync <<'EOS'
#!/bin/bash
set -euo pipefail
cd /srv/brain/markdown
[ -d .git ] || exit 0
git add -A
git diff --cached --quiet && exit 0            # nic nového, nic nedělej
git commit -q -m "sync $(date -Is)"
git push -q origin main 2>&1 || {
    echo "brain-markdown-sync: push selhal (deploy key? prazdny remote?)" >&2
    exit 1
}
EOS
chmod 0755 /usr/local/sbin/brain-markdown-sync

cat > /etc/systemd/system/brain-markdown-sync.service <<'EOS'
[Unit]
Description=Commit a push /srv/brain/markdown
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/brain-markdown-sync
EOS

cat > /etc/systemd/system/brain-markdown-sync.timer <<'EOS'
[Unit]
Description=Pravidelny sync /srv/brain/markdown do gitu

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOS

systemctl daemon-reload
systemctl enable --now brain-markdown-sync.timer >/dev/null

echo
echo "=== HOTOVO na straně kontejneru ==="
echo
echo "Přidej tento veřejný klíč na GitHubu do repozitáře jako"
echo "Deploy key s zaškrtnutým 'Allow write access':"
echo "  Settings -> Deploy keys -> Add deploy key"
echo
cat "$KEY.pub"
echo
echo "Potom ověř a odešli první commit:"
echo "  ssh -T git@github.com                   # ma odpovedet uvitanim"
echo "  /usr/local/sbin/brain-markdown-sync"
echo
echo "Stav timeru:  systemctl list-timers brain-markdown-sync.timer"
