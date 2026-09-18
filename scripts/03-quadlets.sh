#!/usr/bin/env bash
# =====================================================================
# Fáze 1 - Podman quadlets
#
# Quadlety jsou systemd unit soubory, ze kterých podman-system-generator
# vyrábí služby. Proti docker-compose máš nativní systemd závislosti,
# journald logy a `systemctl` jako jediné rozhraní pro provoz - a přechod
# na Kubernetes později je snazší, protože quadlet syntaxe je blízká
# pod specifikaci.
#
# Spouštěj jako root uvnitř kontejneru po 02-guest-bootstrap.sh.
# =====================================================================
set -euo pipefail

# Instancni promenna: osobni brain /srv/brain, komercni instance jinam.
# Komerce je konfigurace, ne fork - stejne quadlety, jen jiny APP_ROOT a
# hodnoty nize.
APP_ROOT="${APP_ROOT:-/srv/brain}"
# S3 bucket a prefix pro uloziste originalu a zalohy. Komerce ma vlastni
# bucket od pocatku (D3).
S3_BUCKET="${S3_BUCKET:-second-brain-kryton}"
BACKUP_S3_PREFIX="${BACKUP_S3_PREFIX:-db-backups/}"
# Infinity image: vychozi vseobecny (s CUDA libs, ~4,8 GB komprimovany), ale
# komerci staci CPU-only varianta (~0,75 GB), ktera se navic stahne z
# Docker Hubu spolehliveji. Obe bezi s `--engine torch --device cpu`.
INFINITY_IMAGE="${INFINITY_IMAGE:-docker.io/michaelf34/infinity:0.0.77}"
# Overovaci druhe volani (P4 varianta B, `core.over_odpoved()`). Vychozi 0,
# takze osobni brain bezi jako dosud; komerce si ho zapina. Duvod pro ten
# rozdil: vymyslene cislo ve smlouve je u ucetni firmy jina kategorie nez
# vymysleny rok u zakona v osobnich poznamkach.
#
# Zmereno 2026-09-19 na ucetni sade (druhymozek_rag/uat/overeni-vysledky.md):
# 0 falesne pozitivnich ze 41 overenych odpovedi, +968 ms medianu,
# $0,00007 na dotaz. Jednu fabrikaci (U029) eval sada propustila a chytila
# ji az tahle kontrola.
ANSWER_VERIFY="${ANSWER_VERIFY:-0}"
# Znacka image Krytona. Vychozi "latest" kvuli osobnimu brainu, kde stavi
# jen jeden clovek a nic se o tu znacku nepere.
#
# PROC TO VUBEC JE PARAMETR (2026-09-19). Na komerci se "latest" rozesel:
# vedle produkcniho buildu se do tehoz tagu postavila rozpracovana varianta
# s REST API. Bezici kontejner drzi image podle ID, takze navenek se nic
# nedelo — ale kterykoli restart, pad nebo reboot by produkci TISE preklopil
# na rozdelanou vetev. Presne ta trida tiche zameny, kvuli ktere je litellm
# pinnuty na digest a infinity na verzi.
#
# Komerce se proto generuje s konkretni znackou (KRYTON_TAG=2026-09-19),
# takze "latest" muze pri vyvoji volne cestovat a do produkce nedosahne.
# Po kazdem nasazeni: postav image, otaguj datem, regeneruj s tim datem.
#
# retrieval-service:latest ma tentyz tvar a zatim se nerozesel; az bude
# potreba, patri sem stejny parametr.
KRYTON_TAG="${KRYTON_TAG:-latest}"
QD=/etc/containers/systemd
install -d -m 0755 "$QD"

# ---------------------------------------------------------------------
# Google Keep (P13). Prazdny e-mail = sync vypnuty; Kryton se kvuli tomu
# nikdy neodmitne spustit, Keep je doplnkovy zdroj, ne podminka provozu.
#     KEEP_EMAIL=tvuj@gmail.com ./scripts/03-quadlets.sh
#
# `Secret=` se do quadletu pise JEN kdyz secret opravdu existuje.
# Past z NASAZENI.md: Secret= na NEEXISTUJICI podman secret znamena, ze
# unit VUBEC NENASTARTUJE - ne ze by chybela jedna promenna. Secret
# zaklada scripts/29-keep-setup.sh, ktery se spousti PRED timhle skriptem.
# ---------------------------------------------------------------------
KEEP_EMAIL="${KEEP_EMAIL:-}"
KEEP_SECRET_LINE=""
if podman secret inspect keep_master_token >/dev/null 2>&1; then
    KEEP_SECRET_LINE="Secret=keep_master_token,type=env,target=KEEP_MASTER_TOKEN"
else
    echo "POZNAMKA: podman secret keep_master_token neexistuje, Keep sync zustane vypnuty."
fi

# ---------------------------------------------------------------------
# Telegram je OSOBNI kanal (denni otazka, prepis hlasovek). Kdyz secret
# telegram_bot_token neexistuje, quadlet cely blok vynecha - Kryton se
# bez Telegramu normalne spusti. Komerci instance (ucetni) zadny osobni
# bot nema.
# ---------------------------------------------------------------------
TELEGRAM_ALLOWED_USER_ID="${TELEGRAM_ALLOWED_USER_ID:-819345451}"
TELEGRAM_LINES=""
if podman secret inspect telegram_bot_token >/dev/null 2>&1; then
    TELEGRAM_LINES="Environment=TELEGRAM_ALLOWED_USER_ID=${TELEGRAM_ALLOWED_USER_ID}
Secret=telegram_bot_token,type=env,target=TELEGRAM_BOT_TOKEN
Environment=TELEGRAM_DAILY_QUESTION_HOUR_UTC=18"
else
    echo "POZNAMKA: podman secret telegram_bot_token neexistuje, Telegram zustane vypnuty."
fi

# ---------------------------------------------------------------------
# Síť. Interní, bez publikování portů na LAN. Naven jde jen to, co
# připojíš na Tailscale interface.
# ---------------------------------------------------------------------
cat > "$QD/brain.network" <<'EOF'
[Unit]
Description=Interni sit platformy

[Network]
NetworkName=brain
Subnet=10.89.7.0/24
Gateway=10.89.7.1
# DNS mezi kontejnery řeší podman aardvark; služby se adresují
# jmény kontejnerů (postgres, infinity, litellm, ...).
EOF

cat > "$QD/pgdata.volume" <<'EOF'
[Volume]
VolumeName=pgdata
EOF

cat > "$QD/hfcache.volume" <<'EOF'
[Volume]
VolumeName=hfcache
EOF

# =====================================================================
# PostgreSQL
#
# --shm-size je tady podstatná: dynamic_shared_memory_type=posix používá
# /dev/shm, který má v kontejneru default 64 MB. Paralelní build HNSW
# indexu na tom spadne na "could not resize shared memory segment" a
# vypadá to jako chyba pgvector. 1 GB je s maintenance_work_mem=1GB
# konzistentní.
# =====================================================================
cat > "$QD/postgres.container" <<EOF
[Unit]
Description=PostgreSQL 17 + pgvector + ceske slovniky
After=network-online.target

[Container]
ContainerName=postgres
# ZMENA proti navrhu: pevna IP na brain.network.
# Bez ni dostane kontejner pri kazdem restartu novou adresu z 10.89.7.0/24.
# Kdyz je kontejner ukoncen nasilne (OOM kill, tvrdy restart LXC), podman
# nespusti teardown a netavarku zustanou stara DNAT pravidla pro publikovane
# porty. Ta se vyhodnocuji v poradi, takze prvni zastarale pravidlo prebije
# to spravne a port zacne odmitat spojeni, i kdyz sluzba bezi. Pevna IP
# tenhle rezim odstrani: duplicitni pravidlo miri na stejnou adresu.
IP=10.89.7.10
Image=localhost/postgres-cs:17
AutoUpdate=local
Network=brain.network
# ZMENA proti navrhu: publikovano na homelab LAN i Tailscale (bind 0.0.0.0).
# Nejcitlivejsi z publikovanych portu - za nim jsou vsechny poznamky.
# Pristup omezuje firewall, ne tento bind.
PublishPort=5432:5432
Volume=pgdata.volume:/var/lib/postgresql/data
Volume=${APP_ROOT}/conf/postgresql-tuning.conf:/etc/postgresql/conf.d/10-tuning.conf:ro,Z
Volume=${APP_ROOT}/sql:/sql:ro,Z
ShmSize=1g
Secret=pg_superuser_pw,type=env,target=POSTGRES_PASSWORD
Environment=POSTGRES_DB=postgres
Environment=PGDATA=/var/lib/postgresql/data/pgdata
Environment=POSTGRES_INITDB_ARGS=--data-checksums
# OPRAVA: puvodne zde bylo navic "-c include_dir=/etc/postgresql/conf.d".
# To Postgres odmitne: FATAL unrecognized configuration parameter "include_dir".
# include_dir NENI GUC nastavitelny pres -c, je to direktiva platna jen uvnitr
# konfiguracniho souboru. Radek
#     include_dir = '/etc/postgresql/conf.d'
# proto pripisuje do \$PGDATA/postgresql.conf skript 03b-pg-include.sh
# (po initdb, pred prvnim ostrym startem). Presne to rika i hlavicka
# conf/postgresql-tuning.conf.
Exec=postgres -c config_file=/var/lib/postgresql/data/pgdata/postgresql.conf
HealthCmd=pg_isready -U postgres -d postgres
HealthInterval=15s
HealthRetries=5
HealthStartPeriod=60s

[Service]
Restart=always
TimeoutStartSec=300

[Install]
WantedBy=default.target
EOF

# =====================================================================
# Infinity - embeddings a reranking v JEDNOM procesu
#
# Dva modely v jedné instanci místo dvou kontejnerů TEI: ušetří to ~300 MB
# režie procesu a jeden port. Cena je, že restart kvůli výměně rerankeru
# shodí i embeddings - při ladění retrievalu to poznáš, ale na 16 GB
# je to správný obchod.
#
# OPRAVA ENGINE: návrh předepisoval `--engine optimum` s tím, že drží modely
# v ONNX int8. V praxi to NEFUNGUJE - viz komentář u MemoryMax. Optimum
# stáhne fp32 ONNX (2,1 GB blob) a při jeho zpracování spotřeba přeroste
# 8 GB, aniž by health endpoint kdy odpověděl; konverzní cache
# `infinity_onnx` zůstane prázdná. Použit `--engine torch`, který ten krok
# vynechává. Změřeno po přechodu:
#     embedding 1 krátký text     0,10-0,15 s
#     embedding dávka 8 krátkých  0,14 s
#     embedding dlouhý chunk 2 kB 1,7-2,1 s
#     rerank 20 kandidátů         1,27 s   (README predikoval 1-2 s)
#     ustálená paměť              1,74 GB  (README odhadoval 1,8 GB RSS)
# Rozpočet paměti z README byl tedy správný, chyboval jen výběr enginu.
#
# --no-bettertransformer: BetterTransformer je defaultně zapnutý. Výstupy
# jsou s ním i bez něj BITOVĚ IDENTICKÉ (ověřeno na stejných dotazech),
# ale bez něj je to rychlejší a úspornější: dlouhý chunk 1,7 s proti 2,9 s,
# paměť 1,68 GB proti 2,87 GB, a start bez špičky load average 28.
#
# První start stahuje modely z HuggingFace (~1,5 GB), proto vysoký
# HealthStartPeriod. Cache je v pojmenovaném volume, další start je rychlý.
# =====================================================================
cat > "$QD/infinity.container" <<'EOF'
[Unit]
Description=Infinity - embeddings a reranking na CPU
After=network-online.target

[Container]
ContainerName=infinity
# ZMENA proti navrhu: pevna IP na brain.network.
# Bez ni dostane kontejner pri kazdem restartu novou adresu z 10.89.7.0/24.
# Kdyz je kontejner ukoncen nasilne (OOM kill, tvrdy restart LXC), podman
# nespusti teardown a netavarku zustanou stara DNAT pravidla pro publikovane
# porty. Ta se vyhodnocuji v poradi, takze prvni zastarale pravidlo prebije
# to spravne a port zacne odmitat spojeni, i kdyz sluzba bezi. Pevna IP
# tenhle rezim odstrani: duplicitni pravidlo miri na stejnou adresu.
IP=10.89.7.11
Image=__INFINITY_IMAGE__
Network=brain.network
# ZMENA proti navrhu: publikovano na homelab LAN i Tailscale (bind 0.0.0.0).
# Infinity nema autentizaci - omezeni resi firewall.
PublishPort=7997:7997
Volume=hfcache.volume:/app/.cache
Environment=HF_HOME=/app/.cache
Environment=OMP_NUM_THREADS=4
Exec=v2 \
  --model-id BAAI/bge-m3 \
  --served-model-name bge-m3 \
  --model-id BAAI/bge-reranker-v2-m3 \
  --served-model-name bge-reranker-v2-m3 \
  --engine torch \
  --no-bettertransformer \
  --device cpu \
  --batch-size 8 \
  --port 7997 \
  --host 0.0.0.0
HealthCmd=curl -fsS http://localhost:7997/health
HealthInterval=30s
HealthRetries=5
HealthStartPeriod=600s

[Service]
Restart=always
TimeoutStartSec=900
# Strop paměti. Když Infinity poroste nad tohle, sežere to page cache
# Postgresu a ANN dotazy se zpomalí, aniž by cokoliv hlásilo chybu.
#
# ZVYSENO kvuli vymene rerankeru za bge-reranker-v2-m3 (568M misto 278M).
# Merene RSS s bge-m3 + v2-m3: 4,36 GB. 5120M dava ~15 % rezervy.
# S puvodnim bge-reranker-base stacilo 2600M (RSS 1,74 GB).
#
# DUSLEDEK PRO ROZPOCET PAMETI: README planoval Infinity na 1,8 GB RSS.
# S v2-m3 je to 4,36 GB, tedy o 2,5 GB vic na ukor page cache, kterou
# README povazuje za zdroj vykonu HNSW scanu. Pri 13 GB kontejneru zbyde
# po vsech sluzbach ~3 GB cache. Node ma 31 GB a ~22 GB volnych, takze
# navyseni RAM kontejneru je varianta, jak cache vratit.
#
# Duvod vymeny - mereno na 7 ceskych dotazech nad 8 dokumenty:
#     bge-reranker-base : top-1 4/7 (57 %), MRR 0,690, 20 kandidatu 1,19 s
#     bge-reranker-v2-m3: top-1 7/7 (100 %), MRR 1,000, 20 kandidatu 3,55 s
# Base navic ve tvrech ze sedmi dotazu vratil na prvnim miste tentyz
# dokument bez ohledu na otazku, tedy v cestine nediskriminoval.
# Na trivialni ceske uloze dal base spravne odpovedi skore 0,4265,
# v2-m3 0,9996.
#
# Historie: s `--engine optimum` (jak navrh predepisoval) tento limit
# NESTACIL a unit se cyklil v OOM smycce:
#     MemoryMax=2600M -> OOM kill
#     MemoryMax=4096M -> OOM kill
#     MemoryMax=8192M (i s --dtype int8) -> OOM kill
#     bez limitu -> ~12 GB, vycerpal cely kontejner a shodil ho
# Health endpoint neodpovedel ani jednou.
#
# Po prechodu na `--engine torch --no-bettertransformer` je spotreba:
#     po startu                     1,59 GB
#     ustalena                      1,74 GB
#     po davce 5 dlouhych chunku    1,83 GB  (nejvyssi zmerena)
#     po rerank 20 kandidatu        1,77 GB
# (hodnoty vyse platily pro bge-reranker-base)
MemoryMax=5120M

[Install]
WantedBy=default.target
EOF

# Image je v heredocu vymenen pres placeholder (heredoc je citovany kvuli
# backtickum v komentari). Komerce: INFINITY_IMAGE=...:0.0.77-cpu.
sed -i "s|__INFINITY_IMAGE__|${INFINITY_IMAGE}|" "$QD/infinity.container"

# =====================================================================
# LiteLLM
# =====================================================================
cat > "$QD/litellm.container" <<EOF
[Unit]
Description=LiteLLM Proxy
After=postgres.service
Requires=postgres.service

[Container]
ContainerName=litellm
# ZMENA proti navrhu: pevna IP na brain.network.
# Bez ni dostane kontejner pri kazdem restartu novou adresu z 10.89.7.0/24.
# Kdyz je kontejner ukoncen nasilne (OOM kill, tvrdy restart LXC), podman
# nespusti teardown a netavarku zustanou stara DNAT pravidla pro publikovane
# porty. Ta se vyhodnocuji v poradi, takze prvni zastarale pravidlo prebije
# to spravne a port zacne odmitat spojeni, i kdyz sluzba bezi. Pevna IP
# tenhle rezim odstrani: duplicitni pravidlo miri na stejnou adresu.
IP=10.89.7.12
# PIN NA DIGEST, 2026-08-18. Drive tu bylo :main-stable, coz je POHYBLIVY
# tag — a litellm-config.yaml sam nahore rika "Image pinuj na konkretni
# stable tag, ne main-latest, LiteLLM vydava velmi casto a DB migrace byvaji
# breaking". Tag :main-stable tu vetu porusoval, jen mene okate nez
# :main-latest.
#
# Overeno 2026-08-18 proti ghcr.io: :main-stable ukazoval na index
# sha256:468c25f3 (amd64 dite 4bc4fe17), zatimco brain bezel na indexu
# sha256:af806882 (amd64 dite 50e647bd) s LiteLLM 1.95.0. Tag se tedy
# posunul a jakykoliv "podman pull", prestavba hostitele nebo obnova ze
# zalohy by skocila na jinou verzi BEZ zmeny v repozitari — tichy posun
# stejne kategorie jako nesynchronizovany symlink z 2026-08-13.
#
# Digest, ne verzovany tag. Puvodni duvod znel "verzovane stable tagy
# (v1.97.0-stable apod.) v registru NEEXISTUJI" — to plati pro tvar
# s prponou -stable, ale ne obecne: ciste verzovane tagy jako v1.100.1
# v registru JSOU (overeno 2026-09-11). Digest se drzi presto, protoze
# je to jediny zapis, ktery je imunni i vuci prepsani existujiciho tagu.
#
# POZOR, KTERY DIGEST: pinuje se INDEX (multi-arch), ne jeho dite.
# "podman image inspect --format {{.Digest}}" vraci PLATFORM manifest te
# jedne architektury, tedy 50e647bd — ten se jako @sha256: pin chova hure
# (na ghcr.io na nej dotaz vratil 404) a je vazany na amd64. Spravny zdroj
# je "podman inspect <kontejner> --format {{.ImageDigest}}", ktery vraci
# index af806882. Overit lze dotazem na
# https://ghcr.io/v2/berriai/litellm/manifests/<digest> — index vraci
# mediaType application/vnd.oci.image.index.v1+json a seznam deti.
#
# UPGRADE se ted dela vedome: zmen digest tady, zaloz databazi litellm
# (Prisma migrace) a nasad.
#
# UPGRADE 2026-09-11: 1.95.0 (af806882) -> 1.100.1 (a3715fa7).
# a3715fa7 je INDEX tagu v1.100.1 a zaroven to, na co ten den ukazovaly
# pohyblive tagy :main-stable i :latest. Mezi 1.95.0 a 1.100.1 je pet
# vydani; zadne nema znamou zranitelnost, duvodem je zaostavani, ne CVE.
# Prisma migrace v tomhle okne jsou: opt-in REPLICA IDENTITY FULL (1.96.0),
# odolnejsi entrypoint migraci (1.97.0, 1.99.0) a volitelny
# USE_V2_MIGRATION_RESOLVER (1.99.0, ponechano vypnute).
#
# POZOR PRI EDITACI TOHOHLE BLOKU: heredoc nize je NEUVOZENY, takze shell
# v nem interpretuje $ i zpetne apostrofy. Pri prvnim zapisu tohohle
# komentare se zpetne apostrofy kolem jmen tagu vyhodnotily jako prikazy —
# do logu spadlo sest "command not found" a jeden "podman pull" se skutecne
# spustil (nastesti bez argumentu, takze jen zahlasil chybu). V komentarich
# uvnitr heredocu proto NEPOUZIVEJ zpetne apostrofy ani $.
Image=ghcr.io/berriai/litellm@sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf
Network=brain.network
# ZMENA proti navrhu: publikovano na homelab LAN i Tailscale (bind 0.0.0.0).
# Chraneno master key / virtual keys.
PublishPort=4000:4000
Volume=${APP_ROOT}/conf/litellm-config.yaml:/app/config.yaml:ro,Z
Volume=${APP_ROOT}/conf/litellm-health.sh:/health.sh:ro,Z
Secret=litellm_master_key,type=env,target=LITELLM_MASTER_KEY
Secret=litellm_salt_key,type=env,target=LITELLM_SALT_KEY
# OPRAVA: puvodne zde bylo
#   Secret=pg_litellm_pw,type=env,target=PGPW
#   Environment=DATABASE_URL=postgresql://litellm_app:\${PGPW}@postgres:5432/litellm
# To nefunguje: systemd/quadlet neexpanduje \${PGPW}, protoze podman secret
# se injektuje az v kontejneru. LiteLLM by dostal literalni \${PGPW} v DSN.
# Cely DSN je proto jeden secret, ktery vyrabi 02b-secrets-extra.sh.
Secret=litellm_database_url,type=env,target=DATABASE_URL
Secret=openrouter_api_key,type=env,target=OPENROUTER_API_KEY
Secret=bigpickle_api_key,type=env,target=BIGPICKLE_API_KEY
# OPRAVA: puvodne https://api.bigpickle.example/v1 — .example je rezervovana
# TLD (RFC 2606), tedy placeholder. Skutecny endpoint modelu big-pickle je
# OpenCode Zen, OpenAI-kompatibilni. Overeno: /v1/models vraci big-pickle.
Environment=BIGPICKLE_API_BASE=https://opencode.ai/zen/v1
Environment=STORE_MODEL_IN_DB=False
Exec=--config /app/config.yaml --port 4000
# OPRAVA: puvodne HealthCmd=curl -fsS http://localhost:4000/health/liveliness
# Image ghcr.io/berriai/litellm nema curl ani wget, takze healthcheck vzdy
# selhaval a kontejner byl trvale "unhealthy".
#
# Prikaz je ve skriptu, ne inline: quadlet pri parsovani HealthCmd spolkne
# koncovou dvojitou uvozovku a podman pak dostane neuzavreny retezec
#   ["CMD-SHELL", "python3 -c \"import ..."]
# coz konci na "/bin/sh: syntax error: unterminated quoted string".
# Prikaz "sh /health.sh" zadne uvozovky neobsahuje. Viz conf/litellm-health.sh.
# (Pozor: v tomto heredocu NEPOUZIVAT zpetne apostrofy - je neuvozeny,
#  takze by je bash vyhodnotil jako substituci prikazu.)
HealthCmd=sh /health.sh
HealthInterval=30s
HealthStartPeriod=90s

[Service]
Restart=always
# OPRAVA: puvodne MemoryMax=800M podle rozpoctu v README (RSS 0,5 GB).
# Merenim zjisteno, ze tato verze LiteLLM ma po startu 1,13 GB - cgroup
# limit 800M ji zabijel jeste behem startu ("conmon exited prematurely",
# zadny zapis do journalu, jen restart loop). DUSLEDEK: rozpocet pameti
# se posouva na ukor page cache, kterou README povazuje za zdroj vykonu
# HNSW scanu.
#
# 1600M -> 1800M, 2026-09-11. Duvod: 1600M NESTACILO. Toho dne v 09:37
# dostalo litellm OOM kill (status=137) po 45 hodinach behu a nikdo si
# toho nevsiml - naslo se to az pri predletove kontrole pred upgradem.
# Dukaz, ze slo o strop a ne o pad: MemoryPeak se rovnal MemoryMax na
# bajt (1677721600).
#
# Po upgradu na 1.100.1 je merena spicka 1,32 GiB a dela ji START
# s Prisma migracemi, ne ustaleny provoz. Pri 1600M z toho zbyvala
# rezerva ~255 MB, pri 1800M je ~443 MB. Hostitel ma ~14,9 GB volnych,
# takze tech 200 MB navic je proti riziku dalsiho OOM levne.
MemoryMax=1800M

[Install]
WantedBy=default.target
EOF

# =====================================================================
# Retrieval Service - vlastní kód
#
# Image si postav sám; tohle je kontrakt, který od něj platforma čeká.
# Vědomě NEMÁ přístup na LiteLLM pro embeddings - jde přímo na Infinity,
# protože bulk indexace by z gateway udělala bottleneck a query-time
# embedding nesnese hop navíc.
# =====================================================================
cat > "$QD/retrieval.container" <<EOF
[Unit]
Description=Retrieval Service - hybridni vyhledavani
After=postgres.service infinity.service litellm.service
Requires=postgres.service

[Container]
ContainerName=retrieval
# ZMENA proti navrhu: pevna IP na brain.network.
# Bez ni dostane kontejner pri kazdem restartu novou adresu z 10.89.7.0/24.
# Kdyz je kontejner ukoncen nasilne (OOM kill, tvrdy restart LXC), podman
# nespusti teardown a netavarku zustanou stara DNAT pravidla pro publikovane
# porty. Ta se vyhodnocuji v poradi, takze prvni zastarale pravidlo prebije
# to spravne a port zacne odmitat spojeni, i kdyz sluzba bezi. Pevna IP
# tenhle rezim odstrani: duplicitni pravidlo miri na stejnou adresu.
IP=10.89.7.13
Image=localhost/retrieval-service:latest
Network=brain.network
Volume=${APP_ROOT}/markdown:/data/markdown:ro,Z
# ZMENA: puvodne
#   Secret=pg_retrieval_pw,type=env,target=PGPW
#   Environment=DATABASE_DSN=pgsql:host=postgres;port=5432;dbname=retrieval
#   Environment=DATABASE_USER=retrieval_app
# Format "pgsql:host=...;dbname=..." je PDO DSN, tedy PHP. Sluzba bude v Pythonu,
# kde je to k nicemu - psycopg chce libpq URL. Sjednoceno s litellm a kryton
# na jeden secret s celym DSN, cimz zaroven odpada cela trida chyb kolem
# skladani hesla v prostredi (viz \${PGPW} bug u litellm).
# Secret vyrabi 02b-secrets-extra.sh.
Secret=retrieval_database_url,type=env,target=DATABASE_URL
Environment=EMBEDDING_URL=http://infinity:7997
Environment=EMBEDDING_MODEL=bge-m3
# ZMENA: puvodne bge-reranker-base. Musi odpovidat --served-model-name
# v infinity.container, jinak retrieval dostane 404 na neznamy model.
Environment=RERANK_MODEL=bge-reranker-v2-m3
# Retrieval fáze: 60 kandidátů z RRF, rerank top-10, vrať top-8.
# Rerank na CPU je nejdražší krok, proto 10 a ne 50.
#
# ZMENA 2026-08-19: z 20 na 10, na základě scripts/28-rerank-value-denik.py
# (14 přirozených otázek nad skutečným deníkem, párově). Kvalita vyšla
# IDENTICKÁ — 14/14 top-1 při top_k=10 i 20 — za třetinu času: medián
# 3,95 s proti 10,36 s. Jediný dotaz, kde rerank vůbec něco přidal
# (rozlišení istio od Kubernetes 1.36), měl cíl na RRF pozici 2, takže
# zisk vznikl PŘEROVNÁNÍM uvnitř vrácené osmičky, ne vytažením dokumentu
# z hloubky — a to top_k=10 umí dál.
#
# Co se tím obětuje: main.py počítá fetch = max(top_k, limit), takže
# při RESULT_LIMIT=8 se rerankuje 10 kandidátů a dokument na RRF pozici
# 11-20 se už nahoru dostat nemůže. Nad 14 dokumenty to nevadilo (cíl byl
# vždy v top-8 už podle RRF), nad větším korpusem vadit může.
# Proto P12 v POZADAVKY.md: přeměřit, až korpus poroste, nejpozději
# 2026-10-19.
Environment=RRF_CANDIDATES=60
Environment=RERANK_TOP_K=10
# Prepis dotazu na klicova slova pres LiteLLM alias \`cheap\`. Navrh to
# zamyslel - litellm-config.yaml ma na konci prikladovy virtual key
# s "models":["cheap"] prave pro retrieval-service. Veta v README o tom,
# ze retrieval nema pristup na LiteLLM, se tyka EMBEDDINGU, ne prepisu.
# Klic je omezeny jen na alias cheap (overeno: reasoning odmitnut).
# Pri jakekoliv chybe nebo timeoutu se pouzije deterministicka extrakce,
# takze vypadek LiteLLM dotazy nezastavi - proto jen After, ne Requires.
Environment=LITELLM_URL=http://litellm:4000
Environment=REWRITE_MODEL=cheap
Environment=REWRITE_ENABLED=1
Secret=litellm_retrieval_key,type=env,target=LITELLM_API_KEY
# Vicejazycnost (sql/03-multilang.sql: cs|en|de|la).
# DEFAULT_LANG plati, kdyz jazyk neurci ani \`lang:\` ve frontmatteru,
# ani detekce pres alias cheap - tedy i kdyz je LiteLLM nedostupny.
Environment=DEFAULT_LANG=cs
# Detekce jazyka dokumentu pri indexaci. Tyka se JEN dokumentu bez \`lang:\`
# ve frontmatteru a jen stavu NEW/CHANGED, takze ustaleny inkrementalni beh
# nedetekuje nic. Pri prvnim naplneni velkeho korpusu bez frontmatteru je to
# ale ~2-3 s na dokument navic - tehdy stoji za zvazeni DETECT_LANG_ENABLED=0.
Environment=DETECT_LANG_ENABLED=1
Environment=RESULT_LIMIT=8
# Context window expansion (app/expand.py): k finalnim vysledkum dotahne
# sousedni chunky (ordinal +- okno) ze stejneho dokumentu, protoze
# chunkovani je bez overlapu a hranice chunku je otazka rozpoctu
# CHUNK_CHARS, ne vyznamu. Aplikuje se AZ po reranku, takze ho nedrazi -
# jen prodluzuje prompt pro ANSWER_MODEL. Okno 1 pridava nejvys 2 sousedy
# na hit.
Environment=EXPAND_ENABLED=1
Environment=EXPAND_WINDOW=1
Environment=MARKDOWN_ROOT=/data/markdown
HealthCmd=curl -fsS http://localhost:8080/healthz
HealthInterval=30s

[Service]
Restart=always
MemoryMax=700M

# Obraz je nyni k dispozici - zdrojaky v retrieval-service/, build
# viz Containerfile (pozor: --network=host, chybi /dev/net/tun).
[Install]
WantedBy=default.target
EOF

# =====================================================================
# Kryton - vlastní kód
# =====================================================================
cat > "$QD/kryton.container" <<EOF
[Unit]
Description=Kryton second brain
After=postgres.service retrieval.service litellm.service
Requires=postgres.service

[Container]
ContainerName=kryton
# ZMENA proti navrhu: pevna IP na brain.network.
# Bez ni dostane kontejner pri kazdem restartu novou adresu z 10.89.7.0/24.
# Kdyz je kontejner ukoncen nasilne (OOM kill, tvrdy restart LXC), podman
# nespusti teardown a netavarku zustanou stara DNAT pravidla pro publikovane
# porty. Ta se vyhodnocuji v poradi, takze prvni zastarale pravidlo prebije
# to spravne a port zacne odmitat spojeni, i kdyz sluzba bezi. Pevna IP
# tenhle rezim odstrani: duplicitni pravidlo miri na stejnou adresu.
IP=10.89.7.14
Image=localhost/kryton:${KRYTON_TAG}
Network=brain.network
Volume=${APP_ROOT}/markdown:/data/markdown:Z
# OPRAVA stejneho bugu jako u litellm: \${PGPW} se neexpanduje.
Secret=kryton_database_url,type=env,target=DATABASE_URL
Environment=RETRIEVAL_URL=http://retrieval:8080
Environment=LITELLM_URL=http://litellm:4000
Environment=MARKDOWN_ROOT=/data/markdown
# Overovaci druhe volani (P4 varianta B). Hodnota z promenne nahore: osobni
# brain 0, komerce 1. Alias "verify" MUSI byt v seznamu modelu klice (nize),
# jinak kontrola skonci na 403 a over_odpoved() vrati None — tedy tise
# neovereno misto ochrany.
#
# ZADNE ZPETNE APOSTROFY V TOMHLE HEREDOCU. Je neuvozeny (<<EOF), takze je
# shell provede jako prikaz a slovo z komentare zmizi. Tenhle soubor na to
# uz najel trikrat (viz commit "zpetne apostrofy v kryton heredocu, potreti")
# a 2026-09-19 pocvrte.
Environment=ANSWER_VERIFY=${ANSWER_VERIFY}
# LLM klic omezeny na reasoning/workhorse/cheap/cheap-fallback/backstop
# a "verify", rozpocet 20 USD / 30 dni.
#
# POZOR, TENHLE SEZNAM MUSI OBSAHOVAT CELY FALLBACK RETEZ. Zjisteno
# 2026-09-09: klic mel jen reasoning/workhorse/cheap/cheap-fallback, takze
# "backstop" — POSLEDNI clanek retezu "reasoning -> [workhorse, backstop]"
# z litellm-config.yaml — vracel HTTP 403 "key_model_access_denied".
# Za normalniho provozu to nebylo videt: "backstop" se nezavola ani jednou.
# Projevilo by se to jedine ve scenari P6 (2026-08-12), kdy spadl
# "reasoning" i "workhorse" naraz, tedy presne v tom, pro ktery "backstop"
# vznikl. Je to tataz past, na kterou litellm-config.yaml na dvou mistech
# sam varuje u klice "retrieval-service".
#
# Klice se v tomhle skriptu NEVYTVARI (jsou to podman secrets s hodnotou
# z /key/generate), takze tenhle komentar je jediny zapis o tom, co klic
# smi. Oprava a postup pri obnove jsou v NASAZENI.md, sekce
# "Virtual keys: co ktery klic smi".
#
# Nejrychlejsi kontrola teto tridy chyb: "GET /v1/models" vraci seznam
# FILTROVANY podle klice, takze staci porovnat vypis pod klicem komponenty
# proti vypisu pod master key.
Secret=litellm_kryton_key,type=env,target=LITELLM_API_KEY
# Autentizace. Port 3001 je publikovany na 0.0.0.0 a firewall pousti cely
# segment 10.20.0.0/24 - bez hesla by byly poznamky otevrene celemu homelabu.
# Kryton se bez tehle dvou secretu ZAMERNE odmitne spustit.
Secret=kryton_auth_password,type=env,target=AUTH_PASSWORD
Secret=kryton_session_secret,type=env,target=SESSION_SECRET
# Analytika (P1b). Role platform_ro ma na schema retrieval JEN SELECT,
# takze SQL psane modelem nemuze nic zapsat ani kdyby proslo kontrolou
# v analytics.py. Bez tohohle secretu se analytika jen nezapne.
Secret=platform_ro_url,type=env,target=ANALYTICS_DATABASE_URL
# Uloziste originalu nahranych dokumentu (P2). Endpoint a bucket nejsou
# tajemstvi, takze jdou pres Environment - lip se s nimi ladi a jmeno
# profilu se uklada ke kazdemu souboru kvuli budouci migraci.
Environment=S3_PROFILE=backblaze
Environment=S3_ENDPOINT=https://s3.eu-central-003.backblazeb2.com
Environment=S3_BUCKET=${S3_BUCKET}
Secret=s3_access_key_id,type=env,target=S3_ACCESS_KEY_ID
Secret=s3_secret_access_key,type=env,target=S3_SECRET_ACCESS_KEY
# Zaloha DB (kryton, litellm) a sifrovaneho balicku secrets, viz
# scripts/19-kryton-backup.sh a scripts/20-kryton-backup-setup.sh.
# BACKUP_S3_BUCKET prazdny = stejny jako S3_BUCKET (viz config.py).
Environment=BACKUP_S3_PREFIX=${BACKUP_S3_PREFIX}
Environment=BACKUP_RETENTION_DAYS=30
# type=mount, ne env: openssl cte klic jako soubor (-pass file:...), ne
# jako promennou prostredi - zabranuje se tim naslednemu logovani hodnoty
# pri pripadnem \`env\` vypisu procesu.
Secret=backup_encryption_key,type=mount,target=backup_encryption_key
# Telegram mustek (krok 1: jen text), viz app/telegram.py. TELEGRAM_LINES
# je prazdny, kdyz telegram_bot_token secret neexistuje (komerce) - tim
# cely kanal zmizi a Kryton se bez nej spusti.
${TELEGRAM_LINES}
# Krok 3: prepis hlasovek pres OpenRouter (app/stt.py), stejny endpoint jako
# chat (viz litellm-config.yaml). ZAMERNE stejny secret jako openrouter_api_key
# nize u litellm - zadny novy ucet ani secret, jen dalsi cil pro uz existujici
# klic. Bez tohodle secretu STT jen zustane vypnute - bot na hlasovku odpovi,
# ze prepis neumi.
Environment=STT_MODEL=openai/whisper-1
Environment=STT_LANGUAGE=cs
Secret=openrouter_api_key,type=env,target=STT_API_KEY
# MCP server (/mcp) - core.search/core.answer a core.capture pro externi
# agenty (napr. OpenWork). Jina autentizace nez web UI, viz app/mcp_server.py.
# Bez tohohle secretu endpoint existuje, ale odmitne uplne kazdy pozadavek.
Secret=mcp_bearer_token,type=env,target=MCP_BEARER_TOKEN
# Google Keep (P13), viz app/keep.py a scripts/29-keep-setup.sh. Jednosmerne,
# jen cteni - do Keepu se nezapisuje nic. Prazdny KEEP_EMAIL nebo chybejici
# secret = sync vypnuty, Kryton bezi dal.
#
# KEEP_MASTER_TOKEN je gpsoauth master token, tedy PLNY PRISTUP K UCTU, ne
# heslo aplikace a ne token omezeny na Keep. Proto secret, nikdy Environment.
Environment=KEEP_EMAIL=${KEEP_EMAIL}
${KEEP_SECRET_LINE}
# ZMENA proti navrhu: puvodne PublishPort=100.64.0.1:3001:3001, tedy jen na
# Tailscale adresu. Dohodnuto publikovat i na homelab LAN 10.20.0.0/24, a
# protoze DHCP i Tailscale adresa jsou dynamicke, bindujeme 0.0.0.0.
# Omezeni resi firewall.
PublishPort=3001:3001
HealthCmd=curl -fsS http://localhost:3001/healthz
HealthInterval=30s

[Service]
Restart=always
MemoryMax=800M

# Obraz je nyni k dispozici - zdrojaky v kryton/.
[Install]
WantedBy=default.target
EOF

systemctl daemon-reload
echo "Quadlety zapsány. Postup:"
echo "  1) podman build -t localhost/postgres-cs:17 -f ${APP_ROOT}/conf/Containerfile.postgres ${APP_ROOT}/conf"
echo "  2) systemctl start postgres && sleep 30"
echo "  3) ./04-init-db.sh"
echo "  4) systemctl start infinity litellm    # infinity stahuje modely, chvíli to trvá"
echo "  5) retrieval a kryton NESPOUŠTĚT - obrazy neexistují (viz komentáře výše)"
echo "  Stav:  systemctl list-units 'postgres*' 'infinity*' 'litellm*'"
