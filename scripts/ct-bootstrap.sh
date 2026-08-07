#!/bin/bash
# Minimalni bootstrap doruceny pres Proxmox konzoli.
# Jediny ukol: dostat kontejner do tailnetu, aby se dal pouzit SSH.
# Vse ostatni uz pojede po Tailscale.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Konzole, ze ktere se tento skript spousti, mela umask 077 (zapis .tskey).
# Bez tohoto radku vznikne keyring s pravy 600 a apt, ktery stahuje jako
# uzivatel _apt, ho neprecte: "Failed to parse keyring ... Permission denied".
umask 022

# Kontejner ma jen link-local IPv6, ale DNS vraci AAAA zaznamy - bez tohoto
# by apt cekal na kazdy IPv6 pokus do timeoutu.
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

echo "== apt update =="
apt-get update -qq

echo "== curl, ca-certificates, gnupg =="
apt-get install -y --no-install-recommends curl ca-certificates gnupg

echo "== tailscale repo (trixie) =="
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
    > /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
    > /etc/apt/sources.list.d/tailscale.list
# Explicitne, i kdyby umask presto zasahl - apt stahuje jako _apt.
chmod 0644 /usr/share/keyrings/tailscale-archive-keyring.gpg \
           /etc/apt/sources.list.d/tailscale.list \
           /etc/apt/apt.conf.d/99force-ipv4
apt-get update -qq

echo "== tailscale =="
apt-get install -y tailscale

if [ -c /dev/net/tun ]; then
    echo "== /dev/net/tun je k dispozici, normalni TUN mod =="
else
    echo "== /dev/net/tun CHYBI -> userspace networking =="
    # PORT musi zustat nastaveny, systemd unit ho pouziva v ExecStart.
    printf 'PORT="41641"\nFLAGS="--tun=userspace-networking"\n' > /etc/default/tailscaled
fi

systemctl enable tailscaled
systemctl restart tailscaled
sleep 4

echo "== tailscale up =="
# --accept-dns=false: DNS drzi opnsense, nechceme prepsat resolv.conf
tailscale up --authkey "$(cat /root/.tskey)" --accept-dns=false --hostname brain
rm -f /root/.tskey

echo "== VYSLEDEK =="
tailscale ip -4
tailscale status | head -5
echo "BOOTSTRAP-DONE"
