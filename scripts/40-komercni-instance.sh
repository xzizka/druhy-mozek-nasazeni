#!/usr/bin/env bash
# =====================================================================
# KOMERCNI INSTANCE - vytvoreni LXC na Proxmox hostu (pve1).
#
# Tohle je PROTIBLOK k 01-proxmox-host.sh, ale pro komercni instanci
# ucetni firmy. Oproti osobni instanci se lisi jen hodnotami nize -
# zadny fork kodu, jen parametry (princip 3 planu: komerce =
# konfigurace + vrstvy).
#
# ROZDILY PROTI OSOBNIMU BRAINU:
#   - CTID 202, hostname komerce, IP 10.20.0.108/24
#   - 14 GB RAM / 4 jadra (osobni brain ma 18 GB / 6 jader; soucet
#     limitu je zamerne nad fyzickou RAM hostitele, protoze cgroup
#     limity jsou stropy, ne rezervace - realna spotreba brainu je ~6 GB)
#   - APP_ROOT=/srv/rag (osobni /srv/brain) - priprava na tenancy D7
#   - zadny Telegram/Keep: osobni kanaly se do komerce nekopiruji
#
# POZOR NA ROOTFS STORAGE: Podman uvnitr potrebuje overlayfs a overlay
# nad ZFS datasetem nefunguje. STORAGE=local je directory storage
# (overeno: pvesm status -> local type dir). local-zfs NEPOUZIVEJ.
#
# Spoustej na Proxmox nodu jako root.
# =====================================================================
set -euo pipefail

CTID="${CTID:-202}"
HOSTNAME="${HOSTNAME_CT:-komerce}"
STORAGE="${STORAGE:-local}"          # NE ZFS, viz vyse
ROOTFS_GB="${ROOTFS_GB:-64}"
MEM_MB="${MEM_MB:-14336}"            # 14 GB - plny duplicitni stack
SWAP_MB="${SWAP_MB:-2048}"
CORES="${CORES:-4}"
BRIDGE="${BRIDGE:-vmbr1}"
IPV4="${IPV4:-10.20.0.108/24}"
GW="${GW:-10.20.0.1}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst}"

if pct status "$CTID" >/dev/null 2>&1; then
    echo "CHYBA: kontejner $CTID uz existuje. Nic nemenim." >&2
    exit 1
fi

if [ "$MEM_MB" -lt 10240 ]; then
    echo "CHYBA: pod 10 GB nezbude pamet na page cache, vykon HNSW spadne" >&2
    exit 1
fi

pct create "$CTID" "$TEMPLATE" \
    --hostname     "$HOSTNAME" \
    --unprivileged 1 \
    --features     nesting=1 \
    --cores        "$CORES" \
    --memory       "$MEM_MB" \
    --swap         "$SWAP_MB" \
    --rootfs       "${STORAGE}:${ROOTFS_GB}" \
    --net0         "name=eth0,bridge=${BRIDGE},ip=${IPV4},gw=${GW},firewall=1" \
    --onboot       1 \
    --ostype       debian \
    --description  "Komercni instance (RAG pro ucetni firmy)"

# ---------------------------------------------------------------------
# /dev/net/tun pro Tailscale. Bez nej Tailscale spadne do userspace
# networking modu a propustnost je radove horsi.
# ---------------------------------------------------------------------
CONF="/etc/pve/lxc/${CTID}.conf"
cat >> "$CONF" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

pct start "$CTID"
echo "Kontejner $CTID ($HOSTNAME, $IPV4) bezi."
echo "Pokracuj: pct enter $CTID a spust 02-guest-bootstrap.sh s APP_ROOT=/srv/rag"
