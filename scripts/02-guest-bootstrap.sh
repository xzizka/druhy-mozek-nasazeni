#!/usr/bin/env bash
# =====================================================================
# Fáze 1 - bootstrap uvnitř kontejneru (nebo na bare-metal hostu)
#
# Spouštěj jako root uvnitř LXC. Skript je idempotentní.
#
# Podman běží jako root uvnitř UNPRIVILEGOVANÉHO LXC. Rootless Podman
# by znamenal vnořené user namespaces, což jde, ale přináší problémy
# s idmapem a subuid rozsahy zbytečně. Root uvnitř unprivilegovaného
# kontejneru je už namapovaný na neprivilegovaný UID hostitele, takže
# hranice je stejná a provoz jednodušší.
# =====================================================================
set -euo pipefail

# Instancni promenna: osobni brain /srv/brain, komercni instance jinam.
# Komerce BEZE ZMENY KODU - jedna repo, druha konfigurace.
APP_ROOT="${APP_ROOT:-/srv/brain}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
    podman podman-compose containers-storage uidmap slirp4netns \
    curl ca-certificates jq gnupg openssl \
    postgresql-client-17 || \
apt-get install -y --no-install-recommends postgresql-client

# ---------------------------------------------------------------------
# Storage driver.
#
# Ověř, že Podman dostal overlay a ne vfs. vfs funguje, ale duplikuje
# každou vrstvu image na disk a při třech image to znamená jednotky GB
# navíc. Když tu uvidíš vfs, rootfs kontejneru leží na ZFS - viz
# poznámka v 01-proxmox-host.sh.
# ---------------------------------------------------------------------
podman info --format '{{.Store.GraphDriverName}}' | tee /tmp/driver
if ! grep -qx overlay /tmp/driver; then
    echo "POZOR: storage driver je $(cat /tmp/driver), ne overlay." >&2
    echo "Přesuň rootfs na LVM-thin/ext4, nebo nainstaluj fuse-overlayfs." >&2
fi

# ---------------------------------------------------------------------
# Tailscale. Kontejner je jediný vstupní bod platformy, nic
# nepublikujeme na LAN.
# ---------------------------------------------------------------------
if ! command -v tailscale >/dev/null; then
    curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
        > /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
        > /etc/apt/sources.list.d/tailscale.list
    apt-get update -qq && apt-get install -y tailscale
fi
systemctl enable --now tailscaled
echo "Spusť ručně: tailscale up --ssh --advertise-tags=tag:brain"

# ---------------------------------------------------------------------
# Adresářová struktura.
#
# markdown/ je autoritativní zdroj dat. Postgres je derivovaný index a
# musí být kdykoliv znovu postavitelný z tohohle adresáře - proto sem
# míří první záloha a proto z něj později uděláme git repozitář.
# ---------------------------------------------------------------------
install -d -m 0750 "$APP_ROOT"/{conf,sql,markdown,backup}
install -d -m 0700 "$APP_ROOT"/secrets

# OPRAVA: markdown musí být čitelný pro retrieval-service.
#
# Podman tu běží rootful bez user namespace, takže uid/gid v kontejneru se
# rovnají uid/gid na hostu. `retrieval` je jediný kontejner, který záměrně
# neběží jako root (Containerfile: USER 10001), takže na 0750 root:root
# do markdown/ nevleze. Projeví se to zákeřně: reindex proběhne, vrátí
# nula dokumentů a nic nespadne, protože rglob() na nečitelném adresáři
# jen nic nenajde.
#
# Řešení drží původní záměr 0750 (poznámky nejsou čitelné pro kohokoliv
# na stroji) — jen se mění skupina. gid 10001 je připíchnutý v Containerfile.
groupadd --system --gid 10001 retrieval 2>/dev/null || true
chgrp retrieval "$APP_ROOT/markdown"
chmod 0750 "$APP_ROOT/markdown"

# ---------------------------------------------------------------------
# Secrets jako podman secrets, ne environment soubory.
#
# Quadlet je umí předat do kontejneru jako env proměnnou, ale na disku
# leží v podman storage, ne v unit souboru, takže je nepustíš omylem do
# gitu ani je neuvidíš v `systemctl cat`.
# ---------------------------------------------------------------------
mk_secret() {
    local name="$1" value="${2:-}"
    if podman secret exists "$name" 2>/dev/null; then
        echo "secret $name už existuje, přeskakuji"
        return
    fi
    [ -n "$value" ] || value="$(openssl rand -base64 36 | tr -d '\n/+=' | head -c 40)"
    printf '%s' "$value" | podman secret create "$name" -
    echo "secret $name vytvořen"
}

mk_secret pg_kryton_pw
mk_secret pg_retrieval_pw
mk_secret pg_litellm_pw
mk_secret pg_ro_pw
mk_secret pg_superuser_pw
mk_secret litellm_master_key   "sk-$(openssl rand -hex 24)"
mk_secret litellm_salt_key

# Externí API klíče doplň ručně - generovat je nemůžeme:
for s in openrouter_api_key bigpickle_api_key langfuse_public_key langfuse_secret_key; do
    podman secret exists "$s" 2>/dev/null || \
        echo "CHYBÍ secret $s -> printf '%s' 'VALUE' | podman secret create $s -"
done

# ---------------------------------------------------------------------
# Sysctls, které v LXC nastavit LZE, protože jsou namespacované.
# vm.* namespacované nejsou - ty patří na hostitele.
# ---------------------------------------------------------------------
cat > /etc/sysctl.d/90-brain.conf <<'EOF'
net.core.somaxconn = 1024
net.ipv4.ip_unprivileged_port_start = 80
EOF
sysctl --system >/dev/null

echo "Bootstrap hotov. Nakopíruj conf/ a sql/ do $APP_ROOT a spusť 03-quadlets.sh"
