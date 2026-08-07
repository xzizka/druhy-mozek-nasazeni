#!/usr/bin/env bash
# =====================================================================
# TENTO SKRIPT SE PŘI TOMHLE NASAZENÍ NESPOUŠTÍ.
#
# Kontejner vznikl přes Proxmox REST API se stejnými parametry, ale:
#   - BRIDGE=vmbr1 místo defaultního vmbr0
#   - TEMPLATE=debian-13-standard_13.6.1_amd64.tar.zst
#   - BEZ globálního vypnutí THP na hostiteli (dotýká se všech guestů;
#     příkazy jsou níže, kdybys to chtěl udělat sám)
#   - /dev/net/tun řešen samostatně (raw lxc.* klíče smí jen root@pam)
#
# Ponecháno v repozitáři jako reference a pro bare-metal instalaci.
# =====================================================================
# Fáze 1 - vytvoření LXC kontejneru na Proxmox hostu
#
# Spouštěj na Proxmox nodu jako root.
#
# POZOR NA ROOTFS STORAGE: Podman uvnitř potřebuje overlayfs, a overlay
# nad ZFS datasetem nefunguje. Použij LVM-thin nebo directory storage nad
# ext4/XFS. Když už ZFS mít musíš, nainstaluj v kontejneru fuse-overlayfs
# a předej /dev/fuse - je to ale zbytečná komplikace.
# =====================================================================
set -euo pipefail

CTID="${CTID:-201}"
HOSTNAME="${HOSTNAME_CT:-brain}"
STORAGE="${STORAGE:-local-lvm}"     # NE ZFS, viz výše
ROOTFS_GB="${ROOTFS_GB:-64}"
MEM_MB="${MEM_MB:-18432}"           # 18 GB - navyseno pri nasazeni z 13 GB.
                                    # Duvod: bge-reranker-v2-m3 ma 4,4 GB RSS misto
                                    # 1,8 GB, ktere README planoval pro base variantu.
                                    # Pri 13 GB by na page cache zbyly ~2,5 GB, a README
                                    # povazuje page cache za zdroj vykonu HNSW scanu.
SWAP_MB="${SWAP_MB:-2048}"
CORES="${CORES:-6}"
BRIDGE="${BRIDGE:-vmbr0}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst}"

# ---------------------------------------------------------------------
# Příprava hostitele.
#
# THP vypínáme globálně: Postgres, ClickHouse i většina databází ho
# nechtějí, protože khugepaged způsobuje nepředvídatelné latenční špičky
# při defragmentaci. Na hostiteli s LXC to platí pro všechny kontejnery
# naráz - jeden z důvodů, proč sem nemíchat workload, který by THP chtěl.
# ---------------------------------------------------------------------
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag
if ! grep -q 'transparent_hugepage=never' /etc/kernel/cmdline 2>/dev/null; then
    echo "POZOR: přidej transparent_hugepage=never do kernel cmdline, jinak" \
         "se po rebootu nastavení vrátí. Na Proxmoxu s systemd-boot:" \
         "/etc/kernel/cmdline + proxmox-boot-tool refresh"
fi

# vm.overcommit_memory NENASTAVUJEME. Postgres by chtěl 2, ale sysctl
# není namespacovaný, takže by to platilo pro celý hostitel včetně
# ostatních guestů. Ve fázi 1 nemáme protichůdného konzumenta (Valkey
# vypadl), ale globální změna kvůli jednomu kontejneru je špatný obchod.
# Ochranu postmastera řeší Postgres sám přes oom_score_adj potomků.

pct create "$CTID" "$TEMPLATE" \
    --hostname     "$HOSTNAME" \
    --unprivileged 1 \
    --features     nesting=1 \
    --cores        "$CORES" \
    --memory       "$MEM_MB" \
    --swap         "$SWAP_MB" \
    --rootfs       "${STORAGE}:${ROOTFS_GB}" \
    --net0         "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --onboot       1 \
    --ostype       debian \
    --description  "AI second brain - faze 1"

# ---------------------------------------------------------------------
# /dev/net/tun pro Tailscale. Bez něj Tailscale spadne do userspace
# networking módu, poběží, ale propustnost bude řádově horší a budeš
# hledat, kde je problém.
# ---------------------------------------------------------------------
CONF="/etc/pve/lxc/${CTID}.conf"
cat >> "$CONF" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

# ---------------------------------------------------------------------
# Kontrola limitu paměti.
#
# V cgroup v2 se do limitu kontejneru účtuje i PAGE CACHE, ne jen RSS.
# Limit tedy musí pokrýt součet RSS všech služeb (~7,8 GB) a cache, kterou
# chceme nechat pro HNSW index a heap. Při 13 GB zbývá ~5 GB cache, což
# je pro osobní korpus dost. Nesnižuj pod 10 GB.
# ---------------------------------------------------------------------
if [ "$MEM_MB" -lt 10240 ]; then
    echo "CHYBA: pod 10 GB nezbude paměť na page cache, výkon HNSW spadne" >&2
    exit 1
fi

pct start "$CTID"
echo "Kontejner $CTID běží. Pokračuj: pct enter $CTID && ./02-guest-bootstrap.sh"
