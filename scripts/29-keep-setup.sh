#!/usr/bin/env bash
# =====================================================================
# Google Keep -> markdown (P13). Jednorazove nastaveni na brainu.
#
# Co skript dela:
#   1) zaklada podman secret keep_master_token (idempotentne)
#   2) zaklada /srv/brain/markdown/keep/ se spravnou skupinou
#   3) hlida, ze keep/ NENI v .gitignore repozitare poznamek
#   4) instaluje dva systemd timery: hodinovy sync a TYDENNI uklid
#
# Proc dva timery a ne jeden: pridavani je vratne, mazani neni. Kdyby
# neoficialni API vratilo neuplny seznam, hodinovy uklid by index vykuchal
# driv, nez by si toho kdokoli vsiml. Tydenni kadence da cas si nesrovnalost
# vsimnout a git v keep/ drzi historii i pro to, co uz je smazane.
#
# MASTER TOKEN je gpsoauth token s PLNYM PRISTUPEM K CELEMU GOOGLE UCTU,
# ne heslo aplikace. Ziskani (jednorazove, na stanici, ne na brainu):
#   1) v prohlizeci otevri https://accounts.google.com/EmbeddedSetup
#      a prihlas se cele vcetne dvoufaktoru
#   2) v devtools -> Application -> Cookies vezmi hodnotu `oauth_token`
#   3) na stanici:  pip install gpsoauth
#      python3 -c "import gpsoauth; print(gpsoauth.exchange_token(
#          'tvuj@gmail.com','<oauth_token>','0123456789abcdef')['Token'])"
#   Stary perform_master_login() s heslem uz vraci BadAuthentication.
#
# Spoustej jako root uvnitr kontejneru brain:
#     KEEP_EMAIL=tvuj@gmail.com /root/deploy/scripts/29-keep-setup.sh
# =====================================================================
set -euo pipefail

MD=/srv/brain/markdown
KEEP_EMAIL="${KEEP_EMAIL:-}"

command -v podman >/dev/null || { echo "CHYBA: tenhle skript patri na brain, ne na stanici." >&2; exit 1; }
[ -d "$MD" ] || { echo "CHYBA: $MD neexistuje." >&2; exit 1; }
[ -n "$KEEP_EMAIL" ] || { echo "CHYBA: nastav KEEP_EMAIL=tvuj@gmail.com" >&2; exit 1; }

# ---------------------------------------------------------------------
# 1) Secret. Token se NIKDY nebere z argumentu prikazu - byl by v historii
#    shellu i ve vypisu `ps`. Cte se z /dev/tty, aby ho nesnedla pripadna
#    roura do skriptu.
# ---------------------------------------------------------------------
if podman secret inspect keep_master_token >/dev/null 2>&1; then
    echo "== secret keep_master_token uz existuje, nechavam ho byt =="
    echo "   (rotace: podman secret rm keep_master_token && spust tenhle skript znovu)"
else
    TOKEN="${KEEP_MASTER_TOKEN:-}"
    if [ -z "$TOKEN" ]; then
        printf 'Vloz Google master token (nezobrazi se): '
        IFS= read -rs TOKEN < /dev/tty
        echo
    fi
    [ -n "$TOKEN" ] || { echo "CHYBA: prazdny token." >&2; exit 1; }
    case "$TOKEN" in
        aas_et/*) ;;
        *) echo "POZOR: token nezacina 'aas_et/'. Master tokeny z gpsoauth tak zacinaji;" >&2
           echo "       jestli jsi vlozil/a oauth_token z cookie, prihlaseni selze." >&2 ;;
    esac
    printf '%s' "$TOKEN" | podman secret create keep_master_token -
    unset TOKEN
    echo "== secret keep_master_token zalozen =="
fi

# ---------------------------------------------------------------------
# 2) Adresar. Kryton bezi jako root a mkdir by ho vyrobil jako root:root
#    0755, tedy citelny pro kohokoli na stroji - proti zameru 0750
#    u koreneho /srv/brain/markdown. Skupina `retrieval` je past c. 6
#    z NASAZENI.md: retrieval je jediny kontejner, ktery nebezi jako root
#    (USER 10001), a do adresare bez teto skupiny se nedostane. Projevilo
#    by se to zakerne - reindex by vratil {"new":0} a nic by nespadlo,
#    protoze rglob() nad necitelnym adresarem proste nic nenajde.
# ---------------------------------------------------------------------
install -d -m 0750 -g retrieval "$MD/keep"
echo "== $MD/keep pripraven ($(stat -c '%U:%G %a' "$MD/keep")) =="

# ---------------------------------------------------------------------
# 3) Git. Na rozdil od _uploads/ a _scale/ ma keep/ do repozitare PATRIT:
#    obsah Keepu nikde jinde nez v Google cloudu neni a poznamka smazana
#    tydennim uklidem musi zustat dohledatelna v historii.
# ---------------------------------------------------------------------
if [ -f "$MD/.gitignore" ] && grep -qE '^\s*/?keep/?\s*$' "$MD/.gitignore"; then
    echo "POZOR: keep/ je v $MD/.gitignore - poznamky se nebudou zalohovat do gitu." >&2
    echo "       Smaz ten radek, jinak prijdes o historii smazanych poznamek." >&2
else
    echo "== keep/ neni gitignorovany, brain-markdown-sync ho bude zalohovat =="
fi

# ---------------------------------------------------------------------
# 4) Timery. Hodinove pridavani, tydenni mazani.
#    Bez Requires=kryton.service: kdyz Kryton nebezi, ma `podman exec`
#    selhat a pockat na dalsi beh, ne kontejner startovat.
# ---------------------------------------------------------------------
cat > /etc/systemd/system/brain-keep-sync.service <<'EOS'
[Unit]
Description=Google Keep -> markdown (pridava a aktualizuje, NEMAZE)
After=kryton.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/podman exec kryton python3 -m app.keep
EOS

cat > /etc/systemd/system/brain-keep-sync.timer <<'EOS'
[Unit]
Description=Hodinovy sync poznamek z Google Keep

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOS

cat > /etc/systemd/system/brain-keep-cleanup.service <<'EOS'
[Unit]
Description=Google Keep uklid - smaze poznamky, ktere v Keepu uz nejsou
After=kryton.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/podman exec kryton python3 -m app.keep --uklid
EOS

cat > /etc/systemd/system/brain-keep-cleanup.timer <<'EOS'
[Unit]
Description=Tydenni uklid smazanych a archivovanych poznamek z Keepu

[Timer]
# Pondeli 04:30 UTC - po nocnim kryton-backup, aby zaloha zachytila stav
# JESTE PRED mazanim. Kdyby uklid smazal neco omylem, existuje zaloha,
# ktera to ma.
OnCalendar=Mon 04:30
Persistent=true

[Install]
WantedBy=timers.target
EOS

systemctl daemon-reload
systemctl enable brain-keep-sync.timer brain-keep-cleanup.timer >/dev/null

echo
echo "=== HOTOVO. Zbyva to, co tenhle skript delat nema ==="
echo
echo "1) Kryton potrebuje gkeepapi - je v requirements.txt, ale obraz je stary:"
echo "     cd /root/deploy/kryton && podman build --network=host -t localhost/kryton:latest -f Containerfile ."
echo
echo "2) Quadlet potrebuje KEEP_EMAIL a novy Secret= (generuje se jen kdyz"
echo "   secret existuje - ten uz existuje, takze teprve TEDA se zapise):"
echo "     cp /etc/containers/systemd/kryton.container /root/kryton.container.zaloha"
echo "     KEEP_EMAIL=$KEEP_EMAIL /root/deploy/scripts/03-quadlets.sh"
echo "     diff -u /root/kryton.container.zaloha /etc/containers/systemd/kryton.container"
echo "     systemctl daemon-reload && systemctl restart kryton"
echo
echo "3) NASUCHO, driv nez se cokoli zapise do poznamek:"
echo "     podman exec kryton python3 -m app.keep --nasucho"
echo "   Zkontroluj pocty. Az potom nechej bezet naostro:"
echo "     systemctl start brain-keep-sync.service"
echo
echo "4) Stav timeru:"
echo "     systemctl list-timers 'brain-keep-*'"
