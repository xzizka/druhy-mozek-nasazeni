# Report nasazení — Druhý mozek, fáze 1

Nasazeno 2026-08-06 na Proxmox `pve1` (192.168.88.1), Proxmox VE 9.2.2.

## Přístup

| co | jak |
|---|---|
| Kontejner | CTID **201**, hostname `brain`, Debian 13.6 (Trixie) |
| Tailscale | **100.74.34.13** — `ssh root@100.74.34.13` (klíč `ozizka@tuxedo`) |
| Homelab LAN | **10.20.0.107** na `vmbr1` z DHCP (opnsense) |
| Postgres | `psql -h 100.74.34.13 -U platform_ro -d retrieval` |
| LiteLLM | `http://100.74.34.13:4000` |

Odsud (192.168.88.0/24) je kontejner dosažitelný **jen po Tailscale** — `vmbr1` je
interní most za opnsense bez fyzického portu a WAN→LAN se neroutuje.

## Co běží

| služba | stav | port | paměť |
|---|---|---|---|
| PostgreSQL 17 + pgvector 0.8.6 + hunspell cs | **healthy**, startuje při bootu | 5432 | 0,15 GB (prázdná DB) |
| LiteLLM | **healthy**, startuje při bootu | 4000 | 1,26 GB |
| Infinity (bge-m3 + bge-reranker-**v2-m3**) | **healthy**, startuje při bootu | 7997 | 4,39 GB RSS |
| Retrieval Service (Python) | **healthy**, startuje při bootu | 8080 (interní) | ~90 MB |
| Kryton | quadlet zapsán, obraz neexistuje | — | — |

Kontejner: 6 jader, **18432 MB RAM** (navýšeno z 13312 MB), swap 2048 MB,
rootfs 64 GB, unprivileged, `nesting=1`, `onboot=1`.
Node má 31,2 GB RAM. Změna se uplatnila za běhu, bez restartu.

Důvod navýšení: `bge-reranker-v2-m3` má 4,4 GB RSS místo 1,8 GB, které README
plánoval pro base variantu. Při 13 GB by po všech pěti službách zbyly na page
cache ~2,5 GB, a README ji považuje za zdroj výkonu HNSW scanů. Při 18 GB
zbývá ~7,5 GB.

**Pozor:** součet nakonfigurované paměti všech guestů je teď 53,3 GB na nodu
s 31,2 GB. U LXC je limit strop, ne rezervace, takže to nevadí — ale `brain`
(18 GB) a `ollama` (13,2 GB) spolu na tomhle stroji rozumně neběží.

## Ověřeno

- `podman info` → **overlay** driver (ne `vfs`)
- **Česká lemmatizace**: `to_tsvector('czech','Ladím latenci vektorových indexů')`
  → `'index':4 'ladit':1 'latence':2 'vektorový':3`
- Databáze `retrieval` a `kryton` s ICU providerem a locale `cs-CZ`
- Role `kryton_app`, `retrieval_app`, `litellm_app`, `platform_ro`;
  granty `platform_ro`=SELECT, `retrieval_app`=SELECT/INSERT/UPDATE/DELETE
- Sloupec `embedding` typu `halfvec(1024)`, indexy `chunk_embedding_hnsw`,
  `chunk_tsv_gin`, `chunk_trgm_gin` (3/3)
- `retrieval.hybrid_search(...)` volatelná i **z mého stroje přes Tailscale**
  rolí `platform_ro`
- Ladění Postgresu se načetlo: `shared_buffers` 2GB, `effective_cache_size` 5GB,
  `maintenance_work_mem` 1GB, `jit` off, `pg_stat_statements` přednahrané
- LiteLLM: 69 Prisma tabulek v DB `litellm`, `/health/liveliness` → `"I'm alive!"`
- `aardvark-dns` běží → služby se adresují jmény kontejnerů
- **Restart kontejneru**: postgres i litellm naběhly healthy, firewall se nahrál,
  pevné IP i DNAT sedí bez duplikátů, Tailscale si udržel stejnou adresu

## Infinity — vyřešeno záměnou enginu

Příčinou nebyl model ani rozpočet paměti, ale **`--engine optimum`**, který návrh
předepisoval. Optimum stáhne fp32 ONNX (blob 2,11 GB) a při jeho zpracování
spotřeba přeroste 8 GB, aniž by health endpoint kdy odpověděl; konverzní cache
`infinity_onnx` zůstane prázdná, takže kvantizace nikdy nedoběhne.

| konfigurace | výsledek |
|---|---|
| `optimum`, MemoryMax 2600M (z README) | OOM kill, restart loop |
| `optimum`, 4096M | OOM kill |
| `optimum`, 8192M, i s `--dtype int8` | OOM kill |
| `optimum`, bez limitu | ~12 GB, vyčerpal celý kontejner |
| **`torch --no-bettertransformer`, 2600M** | **funguje, ustálená paměť 1,74 GB** |

**Rozpočet paměti z README byl tedy správný** (odhadoval 1,8 GB RSS), chyboval
jen výběr enginu. `MemoryMax` zůstal na původních 2600M.

`--no-bettertransformer`: výstupy jsou s ním i bez něj bitově identické, ale bez
něj je to rychlejší a úspornější — dlouhý chunk 1,7 s proti 2,9 s, paměť 1,68 GB
proti 2,87 GB, a start bez špičky load average 28.

Změřený výkon na CPU (i7-8550U, `OMP_NUM_THREADS=4`):

| operace | latence |
|---|---|
| embedding 1 krátký dotaz | 0,10–0,15 s |
| embedding dávka 8 krátkých | 0,14 s |
| embedding dlouhý chunk (2 kB) | 1,7–2,1 s |
| embedding dávka 5 dlouhých chunků | 9,2 s |
| rerank 20 kandidátů | 1,27 s |

Rerank odpovídá predikci README („base varianta nad 20 kandidáty zvládne 1–2 s").
Indexace dlouhých chunků je ale pomalá — ~1,8 s na chunk je při prvním naplnění
korpusu ta drahá operace.

### Reranker vyměněn za bge-reranker-v2-m3

`bge-reranker-base` z návrhu byl na češtině měřitelně slabý. Evaluace na
**7 českých dotazech nad 8 dokumenty**, kde ke každému dotazu existuje jeden
správný dokument:

| metrika | bge-reranker-base | **bge-reranker-v2-m3** |
|---|---|---|
| top-1 přesnost | 4/7 = **57 %** | 7/7 = **100 %** |
| top-3 přesnost | 5/7 = 71 % | 7/7 = 100 % |
| MRR | 0,690 | **1,000** |
| skóre na trivální české úloze | 0,4265 | **0,9996** |
| latence 8 kandidátů | 0,42 s | 1,40 s |
| latence 20 kandidátů | 1,19 s | 3,77 s |
| RSS (s bge-m3) | 1,74 GB | 4,39 GB |

Base měl navíc ošklivý režim: ve **třech ze sedmi** dotazů vrátil na prvním místě
tentýž dokument bez ohledu na otázku — v češtině prostě nediskriminoval.
Na angličtině dával obě varianty srovnatelné výsledky, takže nešlo o obecnou
slabost modelu, ale o jazykové pokrytí.

Reprodukovatelná evaluace: `scripts/06-eval-reranker.py`.

**Cena výměny.** Latence rerankingu podle počtu kandidátů (i7-8550U, 4 vlákna):

| kandidátů | latence |
|---|---|
| 5 | 0,83 s |
| 10 | 2,01 s |
| **20** (`RERANK_TOP_K` z návrhu) | **3,77 s** |
| 40 | 8,21 s |
| 60 | 12,55 s |

Roste to lineárně, takže `RERANK_TOP_K` je přímý regulátor latence dotazu.
Při 20 kandidátech platí každý dotaz ~3,8 s jen za rerank, plus 0,14 s
za embedding dotazu. README to předpověděl („3–5 s na dotaz"). Snížení na 10
latenci skoro půlí — stojí za zvážení, jestli je rozdíl v kvalitě mezi top-10
a top-20 kandidáty vůbec měřitelný.

**Paměť.** `MemoryMax` zvednut z 2600M na **5120M**. Účetnictví je zrádné:
cgroup hlásí `memory.peak` jen 1,84 GB, ale VmRSS procesu je 4,39 GB — rozdíl
jsou váhy modelu namapované ze souborů v `hfcache`, které jsou účtované tomu,
kdo je do page cache načetl první. Po studeném startu, kdy je cache prázdná,
si je naúčtuje sám kontejner, proto konzervativních 5120M.

**Důsledek pro rozpočet paměti.** README plánoval Infinity na 1,8 GB RSS.
S v2-m3 je to 4,4 GB, tedy o 2,5 GB víc na úkor page cache, kterou README
považuje za zdroj výkonu HNSW scanů. Node má 31 GB a ~22 GB volných, takže
navýšení RAM kontejneru nad 13 GB je varianta, jak cache vrátit.

`hfcache` mimochodem narostl na 19 GB, z toho 8,7 GB byl adresář `xet`
(`chunk-cache` HuggingFace Xet backendu — stahovací mezicache, váhy modelů leží
v `hub/*/blobs`). **Smazáno 2026-08-06**, `hfcache` je teď 9,6 GB a na rootfs je
38 GB volných místo 29 GB. Cena je jen to, že případné znovustažení modelu
nemá proti čemu deduplikovat. Adresář se při dalším stahování obnoví; zabránit
tomu jde `Environment=HF_HUB_DISABLE_XET=1` v `infinity.container`.

Zbytek `hfcache` (9,6 GB) je: `bge-m3` 6,4 GB (tři ~2,3GB bloby — safetensors,
pytorch_model.bin a ONNX z neúspěšného pokusu s `--engine optimum`),
`bge-reranker-v2-m3` 2,2 GB a `bge-reranker-base` 1,1 GB. Base reranker se už
nepoužívá; drží se jen kvůli reprodukovatelnosti `scripts/06-eval-reranker.py`.

## Další věci, které nefungují

- **Retrieval Service je napsaný a běží** — zdrojáky v `retrieval-service/`,
  návrh v `PIPELINE.md`. Endpointy: `/healthz`, `/readyz`, `/stats`,
  `POST /search`, `POST /reindex`. Ověřeno na 3 testovacích poznámkách:
  první reindex 3 dokumenty / 5 chunků za 1,3 s; druhý běh bez změny
  0 embeddingů; změna jedné sekce = **1 embedding + 1 recyklovaný**;
  smazání a vrácení souboru detekováno správně.
  Vyhledávání: celá otázka → klíčová slova `hunspell unaccent` → všechny tři
  větve, rerank 0,995. Bez diakritiky lexikální větev mlčí a fuzzy zachrání
  výsledek. Bez rerankingu 0,12 s, s rerankingem 1,1-1,3 s.

  **Vícejazyčný (cs, en, de, la) od 2026-08-07** — jazyk se bere z `lang:`
  ve frontmatteru, a když chybí, určí ho `cheap`. Dotazová strana předává
  `p_ts_config`. Podrobnosti a měření v `PIPELINE.md`, test
  `scripts/09-multilang-test.py`.
- **Kryton** — v ZIPu není obraz ani zdrojový kód a nebyl napsán. Quadlet je
  připravený a bez `[Install]`, port 3001 je v něm, ale nic za ním neposlouchá.

  **Kontrakt pro ten kód je ale ověřený** — `scripts/05-smoke-retrieval.py` projde
  celou cestu Infinity → `halfvec(1024)` → generované sloupce → `hybrid_search`
  → RRF fúze. Dvě věci, které z toho pro retrieval kód vyplývají:

  1. **Neposílej do `hybrid_search` celou otázku.** `websearch_to_tsquery` spojuje
     termíny operátorem AND, takže z „Proč záleží na pořadí slovníků při
     lemmatizaci?" vznikne `'proč' & 'záležet' & 'na' & 'pořadí' & …` a **nenajde
     nic**. Lexikální větev mlčí a fuzzy taky — `word_similarity` dlouhého dotazu
     je ~0,24 proti prahu 0,5. Zůstane jen dense větev. Klíčová slova naopak
     rozsvítí všechny tři. Přepis dotazu na klíčová slova je přesně to, na co
     návrh vyhradil alias `cheap`.

  2. **Centrální tvrzení návrhu platí a je ověřené na živých datech.** Dotaz
     `vektorových indexů` → lexikální větev najde 1 shodu. Týž dotaz bez
     diakritiky `vektorovych indexu` → `'vektorovych' & 'index'`, lexikálně
     **0 shod** (index je lemmatizovaný s diakritikou), ale
     `word_similarity = 1,0000` a trigramová větev vrátí dokument na 1. místě.
     Stejně tak překlep `maintenence_work_mem` najde správný dokument jen díky
     fuzzy větvi. Ta třetí větev dělá právě to, kvůli čemu tam je.
- **Generativní inference funguje celá.** Všechny tři aliasy ověřeny reálným
  voláním, `x-litellm-attempted-fallbacks: 0` u každého, upstream potvrzen
  z LiteLLM spend logu:

  | alias | model | upstream |
  |---|---|---|
  | `cheap` | `openrouter/google/gemini-2.5-flash` | openrouter.ai |
  | `workhorse` | `openrouter/anthropic/claude-sonnet-4.5` | openrouter.ai |
  | `reasoning` | `openai/big-pickle` | opencode.ai/zen/v1 |
- **Langfuse telemetrie** — callbacky zakomentované v `conf/litellm-config.yaml`,
  postup zapnutí je v komentáři.
### bigpickle — dohledáno, opraveno, funkční

V ZIPu byl `model: openai/big-pickle-large` s poznámkou „uprav podle skutečného
API" a `BIGPICKLE_API_BASE=https://api.bigpickle.example/v1`. `.example` je
rezervovaná TLD (RFC 2606), takže to nikdy nemohlo fungovat.

Skutečný poskytovatel je **OpenCode Zen**, OpenAI-kompatibilní:

| co | hodnota |
|---|---|
| api_base | `https://opencode.ai/zen/v1` |
| model | `big-pickle` (v LiteLLM `openai/big-pickle`) |
| klíč | zdarma na <https://opencode.ai/auth>, bez platebních údajů |

Ověřeno proti živému API: `/v1/models` vrací 61 modelů včetně `big-pickle`,
a přímé volání `/v1/chat/completions` s `model: big-pickle` odpovídá.

**Past:** endpoint pouští requesty i **bez** `Authorization` hlavičky (free
preview), ale **neplatný** klíč aktivně odmítne (`AuthError: Invalid API key`).
Placeholder v secretu tedy nestačí — projeví se to tak, že alias `reasoning`
tiše spadne na fallback `workhorse` a v odpovědi je
`model: anthropic/claude-sonnet-4.5` místo `big-pickle`. Právě podle toho pole
se pozná, jestli bigpickle skutečně jede.

`big-pickle` je reasoning model — utrácí tokeny na `reasoning_content`, takže
při nízkém `max_tokens` vrátí prázdný `content` a `finish_reason: length`.
Pro testy dávej `max_tokens` alespoň v řádu stovek.
- **Index obsahuje jen 4 testovací poznámky** (`01-cesky.md`, `02-english.md`,
  `03-deutsch.md`, `04-latina.md` — jazykový test, commitnuté v gitu). Skutečný
  korpus zatím žádný. První hromadné naplnění je `scripts/08-first-fill.sh`
  (drop HNSW → naplnit → `CREATE INDEX` → `VACUUM ANALYZE`); pro pár dokumentů
  je zbytečné, stačí `POST /reindex`.
- **Není záloha ani eval loop** — návrh je odkládá do fáze 2.

## Zásahy do Proxmoxu

1. **`local` storage dostala content type `rootdir`.** Byla to jediná cesta, jak
   nedat rootfs na ZFS: `local-zfs` je `zfspool` a overlayfs nad ZFS nefunguje
   (Podman by spadl na `vfs`). `local` je `dir` nad `/var/lib/vz` a kontejnerový
   rootfs tam vznikl jako `vm-201-disk-0.raw` s **ext4 uvnitř**, takže overlayfs
   funguje. Změna je aditivní, ostatních guestů se nedotkla.
   *Poznámka:* ten raw soubor fyzicky leží na rpool, takže Postgres píše
   ext4 → raw → ZFS. Pro databázi to znamená write amplifikaci.
2. **`cmode=shell` na CT 201.** Bylo nutné, aby konzole dala shell bez loginu
   (heslo roota v kontejneru není nastavené). Nechal jsem to tak jako záchrannou
   cestu, protože jinak je Tailscale jediný přístup. Důsledek: kdo má
   `VM.Console` na 201, dostane root shell bez hesla. Vrácení: `cmode=tty`.
3. Nic jiného. THP na hostiteli jsem nechal být.

## `/dev/net/tun` — pořád chybí

Nešlo přidat přes API: `dev0=/dev/net/tun` vrátí
`configuring device passthrough is only allowed for root@pam` a raw `lxc.*` klíče
API vůbec nevystavuje (`property is not defined in schema`).

Důsledky, které to má **dnes**:
- Tailscale běží v **userspace networking** módu — funguje, ale s výrazně nižší
  propustností
- `podman build` vyžaduje `--network=host`, protože `pasta` bez `/dev/net/tun`
  spadne na `Failed to open() /dev/net/tun`

Doplnit na hostu do `/etc/pve/lxc/201.conf` a restartovat CT:
```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```
Pak v kontejneru smazat `/etc/default/tailscaled` a `systemctl restart tailscaled`.

## Firewall

Clusterový firewall v Proxmoxu je **vypnutý** a nezapínal jsem ho — dotkl by se
hostu i všech šesti existujících guestů a při chybném pravidle zamkne přístup
k Proxmoxu. Místo toho běží nftables uvnitř kontejneru:
`brain-firewall.service` → `conf/brain-firewall.nft`, tabulka `inet brain`,
zapnuté při bootu.

Pouští: loopback, established, ICMP, DHCP, `tailscale0`, podman bridge
(kvůli aardvark-dns), a z **10.20.0.0/24** porty 22, 3001, 4000, 5432, 7997.
Zbytek dropuje. Publikované porty se filtrují ve `forward`, protože po DNAT
už nejdou do `input` — omezit jen `input` by je nechalo otevřené.

Tabulku `netavark` **záměrně nemaže** (žádný `flush ruleset`), jinak by se
rozbil DNAT publikovaných portů a SNAT pro `brain.network`.

Pokud chceš raději firewall na úrovni Proxmoxu, řekni — je to čistší (filtruje
před kontejnerem), ale znamená to zapnout clusterový firewall.

## Opravy v souborech ze ZIPu

Opravené zdroje jsou v tomto adresáři, chování ověřené na běžícím systému.
`02-guest-bootstrap.sh`, `04-init-db.sh`, oba `.sql`, `postgresql-tuning.conf`
a `Containerfile.postgres` jsou **beze změny** — fungovaly na první pokus.

### Reálné bugy

1. **`${PGPW}` v `DATABASE_URL`** (`litellm.container`, `kryton.container`).
   Quadlet předal do kontejneru literální `${PGPW}` — podman secret se injektuje
   až v kontejneru, systemd o něm neví. LiteLLM by dostal rozbitý DSN.
   Oprava: celý DSN jako jeden podman secret (`02b-secrets-extra.sh`).

2. **`-c include_dir=...`** (`postgres.container`). Postgres to odmítne:
   `FATAL: unrecognized configuration parameter "include_dir"`. Není to GUC
   nastavitelný z příkazové řádky, ale direktiva platná jen uvnitř konfiguračního
   souboru — což hlavička `postgresql-tuning.conf` sama říká. Bez opravy Postgres
   vůbec nenastartuje a ladění se nenačte. Oprava: `03b-pg-include.sh` připíše
   `include_dir` do `$PGDATA/postgresql.conf`.

3. **`HealthCmd=curl ...`** (`litellm.container`). Image `ghcr.io/berriai/litellm`
   nemá curl ani wget → kontejner byl trvale `unhealthy`. Oprava:
   `conf/litellm-health.sh` a `HealthCmd=sh /health.sh`.
   Navíc past: quadlet při parsování `HealthCmd` **spolkne koncovou dvojitou
   uvozovku**, takže inline python one-liner skončil na
   `/bin/sh: syntax error: unterminated quoted string`. Proto skript, ne inline.

4. **`MemoryMax=800M`** (`litellm.container`). Měřeno 1,13 GB → cgroup zabíjela
   kontejner během startu, v journalu jen `conmon exited prematurely` a restart
   loop. Zvednuto na 1600M.

5. **Chybějící balíčky.** `02-guest-bootstrap.sh` instaluje s
   `--no-install-recommends` a vyjmenovává `slirp4netns`, ale Podman 5.4 chce
   `passt` (`pasta`). Hlavně ale chybělo **`aardvark-dns`** — bez něj nefunguje
   rozlišování jmen mezi kontejnery, na kterém stojí celý návrh
   (`postgres`, `infinity`, `litellm`). Doinstalováno `passt`, `aardvark-dns`,
   `catatonit`.

6. **`/srv/brain/markdown` byl pro retrieval nečitelný.** Nalezeno 2026-08-07
   při nasazování vícejazyčnosti. `02-guest-bootstrap.sh` zakládá datové
   adresáře jako `install -d -m 0750` (tedy `root:root`), ale `retrieval` je
   **jediný kontejner, který záměrně neběží jako root** — Containerfile má
   `USER 10001`. Podman tu běží rootful bez user namespace, takže uid/gid
   v kontejneru se rovnají uid/gid na hostu, a uid 10001 se do adresáře
   s právy 0750 root:root nedostane.

   **Projevovalo se to zákeřně:** reindex proběhl, vrátil
   `{"new":0,...,"seconds":0.0}` a *nic nespadlo*, protože `rglob()` nad
   nečitelným adresářem prostě nic nenajde. Vypadá to jako prázdný korpus, ne
   jako chyba práv. Skrývalo se to za tím, že databáze byla stejně prázdná.

   Navíc: bez `--gid` přidělí `useradd` v obrazu první volný systémový gid,
   což u `python:3.13-slim` vyšlo na 999 — a to je na hostu `systemd-journal`.
   Spoléhat se na takovou shodu nejde.

   Opraveno tak, aby zůstal původní záměr 0750 (poznámky nejsou čitelné pro
   kohokoliv na stroji), mění se jen skupina: `groupadd --system --gid 10001
   retrieval` na hostu i v Containerfile, `chgrp retrieval /srv/brain/markdown`.

7. **`05-smoke-retrieval.py` nebyl izolovaný od reálného korpusu.** Hlavička
   slibuje, že je skript bezpečný nad produkcí, ale kontroly se dívají na
   1. místo výsledku a předpokládají, že tam bude jeho vlastní fixtura
   `_smoke/*`. Jakmile v indexu leží skutečné poznámky, přebijí ji — narazilo
   se na to hned, poznámka `01-cesky.md` o vektorových indexech vytlačila
   `_smoke/hnsw-pamet.md` a kontrola spadla, přestože obě tvrzení o větvích
   (lexikální mlčí, fuzzy zachraňuje) platila. `search()` teď filtruje podle
   prefixu `_smoke/`.

### Změny podle dohody

8. **Publikované porty** 5432, 7997, 4000, 3001 na `0.0.0.0`.
   U krytonu nahrazen placeholder `PublishPort=100.64.0.1:3001:3001`.
9. **Langfuse callbacky** zakomentované (chybí klíče).
10. **`[Install] WantedBy` vynecháno** u `retrieval`, `kryton` (neexistují obrazy)
   a `infinity` (nevyřešená paměť).
11. **`01-proxmox-host.sh` se nespouští** — kontejner vznikl přes API s `vmbr1`
   a bez globálního vypnutí THP. Skript má o tom hlavičku.

### Provozní vylepšení

12. **Pevné IP na `brain.network`** (`10.89.7.10`–`.14`). Při násilném ukončení
    kontejneru (OOM kill, tvrdý restart LXC) podman nespustí teardown a
    netavarku zůstanou stará DNAT pravidla. Vyhodnocují se v pořadí, takže
    první zastaralé přebije správné a **port začne odmítat spojení, i když
    služba běží** — přesně na tohle jsem narazil u portu 4000. Pevná IP ten
    režim odstraní.
    Kdyby se to přesto stalo: `systemctl stop <sluzby>` →
    `nft delete table inet netavark` → nastartovat znovu.

## Pomocné nástroje

- `scripts/pvcon.py` — klient Proxmox termproxy konzole přes WebSocket. Proxmox
  API neumí spustit příkaz v LXC a na `vmbr1` odsud není routa, takže konzole
  byla jediný kanál pro první krok. Použit jen na instalaci Tailscale.
- `scripts/ct-bootstrap.sh` — to, co se přes konzoli poslalo.

## PRÁVĚ BĚŽÍ: zátěžový test (od 2026-08-07 13:15 UTC)

Systemd jednotka `scale-test` na brainu. Konec indexace čekán **2026-08-09
kolem 03:45 UTC**, s HNSW a měřením kolem 05:00.

| | |
|---|---|
| korpus | **975 dokumentů / ~113 764 chunků**, každý dokument přes 100 chunků |
| jazyky | cs 399, en 300, de 176, la 100 |
| zdroj | reálné knihy z Gutenbergu + česká Wikipedie |
| tempo | **1,22 s/chunk** (shodné s validačním měřením) |
| umístění | `/srv/brain/markdown/_scale/`, gitignorováno i vyjmuto z indexu |

Stav a výsledek kdykoliv: `scripts/11-scale-report.sh`. Běh je resumovatelný,
po pádu stačí spustit `10-scale-test.sh` znovu. Úklid `12-scale-cleanup.sh`.

**Proč to běží:** dosavadní ověření vícejazyčnosti stálo na 4, později 8
dokumentech. To stačilo na tvrzení o mechanismech (zapisuje se `lang`, recykluje
se podle obsahu), ale na nic o kvalitě — při čtyřech dokumentech vrací dense
větev vždycky všechny, takže „funguje napříč jazyky" bylo pravda triviálně.

**Dvě čísla, na která se v reportu dívat:** přesnost detekce jazyka na ~97
dokumentech bez frontmatteru (ground truth je v názvu souboru, takže je to
skutečné měření, ne kruh) a per-jazyk top-1 s MRR proti known-good dokumentu.

**Dva kroky čekají na doběhnutí:** `git -C /root/deploy pull` (nesmí se dělat za
běhu — bash čte běžící skript z disku průběžně) a `rm -rf /root/deploy.old`.

Co příprava testu odhalila, je zapsané jinde: cena reranku nad reálnými chunky
v `PIPELINE.md`, práva na `/srv/brain/markdown` a izolace smoke testu jako
body 6 a 7 výš.

## Doporučené další kroky

1. **Napsat Krytona** — jediná zbývající velká věc. Retrieval Service hotový
   a nasazený, návrh pipeline i naměřené chování v **`PIPELINE.md`**.
   Kontrakt retrievalu upraven pro Python: místo PDO DSN
   (`pgsql:host=…;dbname=…`, což je PHP) je teď jeden secret
   `retrieval_database_url` s libpq URL, stejně jako u litellm a kryton.

   **Co z vícejazyčnosti pro Krytona plyne:** `POST /search` bere volitelné
   `lang` (`cs|en|de|la`). Posílat ho nemusí — když chybí, určí jazyk `cheap`
   ve stejném volání jako klíčová slova. Když ho ale Kryton zná z kontextu
   konverzace, ať ho pošle: je to spolehlivější a odpověď pak nese
   `lang_source: "request"`. Neznámý kód vrací 422.
2. ~~Zvážit výměnu rerankeru za `bge-reranker-v2-m3`~~ — hotovo a změřeno,
   top-1 přesnost 57 % → 100 %. Zbývá rozhodnout `RERANK_TOP_K`: 20 znamená
   3,8 s na dotaz, 10 asi 2,0 s.
3. ~~Dodat skutečné OpenRouter a bigpickle klíče~~ — hotovo, ověřeno.
4. ~~Vícejazyčnost (cs, en, de, la)~~ — hotovo a ověřené na čtyřech poznámkách,
   viz `PIPELINE.md` a `scripts/09-multilang-test.py`.
5. Zneplatnit použitý Tailscale auth key.
6. `/dev/net/tun` — vědomě odloženo. Propustnost Tailscale je i v userspace módu
   57–64 MB/s při 2 ms, takže to není problém; jediný dopad je, že
   `podman build` potřebuje `--network=host`.
7. ~~Udělat z `/srv/brain/markdown` git repozitář a odklopit ho jinam~~ —
   **hotovo celé.** `scripts/07-markdown-git.sh`, remote
   `git@github.com:xzizka/druhy-mozek-poznamky.git`, timer
   `brain-markdown-sync` každých 15 minut commituje **i pushuje**.
   Ověřeno 2026-08-07: `origin/main..main` prázdné, pracovní strom čistý.

8. ~~Zdrojáky nasazení nejsou ve verzování~~ — **vyřešeno 2026-08-07.**
   Do té chvíle nebyl git repozitář ani `/root/deploy` na brainu, ani pracovní
   adresář na stanici, přestože je v nich celý systém. Poznámky ztrátu obou
   strojů přežily, systém, který je indexuje, ne.

   Teď: `xzizka/druhy-mozek-nasazeni`, privátní (ověřeno neautentizovaným
   dotazem na API — vrací 404, přitom `ls-remote` prokázal existenci).
   `/root/deploy` je **klon**, ne kopie, a nasazuje se `git pull`em.

   **Brain má read-only deploy key** (`git push --dry-run` odmítnut). Je to
   záměr: co nemůže pushnout, nemůže rozejít obě kopie.

   Klíč je vlastní, `id_ed25519_nasazeni`, a v `/root/.ssh/config` je pod
   aliasem `Host github-nasazeni`. **Alias je nutnost, ne kosmetika:** GitHub
   nedovolí použít jeden deploy key na dvou repozitářích a `IdentitiesOnly yes`
   přibíjí jeden klíč na jeden `Host`. Bez aliasu by se na tenhle repozitář
   nabídl klíč od poznámek a spojení skončí `Permission denied`.

   Pravidlo pro udržení: **na brainu se soubory needitují.** Kontrola:
   `git -C /root/deploy status --short` musí být prázdné.

   **V repozitáři nejsou žádné secrets** a nemají tam být — hesla i API klíče
   jdou přes podman secrets a odkazy `os.environ/` v `litellm-config.yaml`
   (ověřeno grepem před prvním commitem). Jejich ztráta ale znamená
   přegenerovat hesla k DB a znovu vydat klíče (OpenRouter, OpenCode Zen),
   a **tuhle mezeru git neuzavírá**.

   Databáze `retrieval` zálohu nepotřebuje — je to derivovaný index a dá se
   kdykoliv postavit znovu z markdownu. Databáze `kryton` derivovaná **nebude**
   (historie konverzací), takže až Kryton pojede, bude potřebovat vlastní zálohu.
9. **První naplnění skutečným korpusem.** Až přibudou opravdové poznámky,
   pusť `scripts/08-first-fill.sh` (drop HNSW → naplnit → postavit → VACUUM).
   Nad korpusem bez frontmatteru zvaž `DETECT_LANG_ENABLED=0` — detekce stojí
   ~2–3 s na dokument a jazyk jde doplnit později zadarmo (viz `PIPELINE.md`).
