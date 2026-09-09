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
| PostgreSQL 17 + pgvector 0.8.6 + hunspell cs | **healthy**, startuje při bootu | 5432 | 0,15 GB (měřeno nad prázdnou DB 2026-08-06) |
| LiteLLM | **healthy**, startuje při bootu | 4000 | 1,26 GB |
| Infinity (bge-m3 + bge-reranker-**v2-m3**) | **healthy**, startuje při bootu | 7997 | 4,39 GB RSS |
| Retrieval Service (Python) | **healthy**, startuje při bootu | 8080 (interní) | ~90 MB |
| Kryton | **healthy**, startuje při bootu — běží od 2026-08-09 | 3001 | 85 MB (limit 800M) |

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
- **Kryton je napsaný** — zdrojáky v `kryton/`, obraz `localhost/kryton:latest`
  postavený 2026-08-06, secrety, DB `kryton` i quadlet s `[Install]` na místě.
  FastAPI, server-rendered HTML se šablonami inline, žádný build step.
  Endpointy: `/healthz`, `/stats`, `/prihlasit`, `/odhlasit`, `/`, `/dotaz`,
  `/konverzace/{id}`, `/konverzace/smazat`, `/hodnoceni`, `/zachytit`,
  `/historie`, `/inbox`, `/korpus`, `/metrika/pripnout`, `/metrika/smazat`.

  **Analytika (P1b)** běží pod rolí `platform_ro` přes secret
  `platform_ro_url` — jen SELECT na schéma `retrieval`, ověřeno pokusem
  o zápis. Agregační dotaz se spočítá nad databází a výsledek se uloží jako
  nepřipnutá metrika; na `/korpus` ho připne až člověk. Kontrola:
  `scripts/14-analytics-check.sh` (běží proti skutečné DB — smoke test ji
  stubuje, takže chyby na spojení nechytí).

  **`/korpus` je tam schválně.** Agregační otázky („kolik je kterých knih")
  přes RAG nejdou — model dostane osm úryvků a z nich tisíc dokumentů
  nespočítá. Změřeno 2026-08-09: takový dotaz spotřeboval celý strop
  `ANSWER_MAX_TOKENS` na uvažování a vrátil prázdný content, zatímco běžné
  dotazy na tomtéž korpusu utratily 202 a 468 tokenů. Strop byl zvednut
  na 8000, ale to řeší jen symptom; přesná čísla bere `/korpus` z `/stats`
  retrievalu, tedy z databáze.

  **Do 2026-08-09 ale nebyl ANI JEDNOU spuštěný**, a první běh to hned ukázal:
  `page()` prohnala už vyrenderované tělo stránky druhým průchodem šablony
  s `autoescape=True`, takže se každá stránka zobrazovala jako vlastní zdroják
  (`<h1>Nadpis</h1>` jako viditelný text). Opraveno přes `markupsafe.Markup`;
  escapování dat zůstává, protože ho dělá vnitřní render. **Poučení: obraz
  postavený a nasazený neznamená ověřený.** Regresi hlídá
  `scripts/13-smoke-kryton.py` — projde všechny routy s odstubovanou DB,
  retrievalem a LLM, takže nepotřebuje běžící stack.

  **Kontrakt pro ten kód je ověřený** — `scripts/05-smoke-retrieval.py` projde
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

## Zátěžový test — DOKONČEN 2026-08-09T05:11:10Z

Běžel od 2026-08-07 13:15 UTC jako jednotka `scale-test`. Indexace hotová
05:07:05, HNSW 05:07:35, měření 05:11:10 — necelé dva dny.

**Proč vznikl:** dosavadní ověření vícejazyčnosti stálo na 4, později 8
dokumentech. To stačilo na tvrzení o mechanismech (zapisuje se `lang`, recykluje
se podle obsahu), ale na nic o kvalitě — při čtyřech dokumentech vrací dense
větev vždycky všechny, takže „funguje napříč jazyky" bylo pravda triviálně.

Korpus: **975 dokumentů / 114 183 chunků** z reálných knih (Gutenberg) a české
Wikipedie, cs 400 / en 301 / de 177 / la 101, každý dokument přes 100 chunků.
Uloženo v `/srv/brain/markdown/_scale/`, gitignorováno.

**Korpus byl 2026-08-10 uklizen** (`scripts/12-scale-cleanup.sh`): reindex smazal
975 dokumentů za 10,7 s bez chyby, HNSW přestavěn, v indexu zbyly 4 testovací
poznámky. Databáze 1186 → 246 MB, markdown strom 110 MB → 556 kB. Report zůstal
v `/root/corpus/state/report.txt` a čísla výš.

Z těch 246 MB je ale asi **218 MB mrtvé místo v GIN indexech** — `chunk_trgm_gin`
155 MB a `chunk_tsv_gin` 63 MB pro čtyři chunky. Skript přestavuje jen HNSW,
na zbytek pouští `VACUUM ANALYZE`, a ten místo označí za znovupoužitelné, ale
systému ho nevrátí; u GIN indexů zvlášť. Není to chyba, místo se znovu využije.
Kdo chce čistý základ, `REINDEX TABLE retrieval.chunk` ho nad čtyřmi řádky
vrátí okamžitě.

**Obnova korpusu je levnější, než vypadá:** surové stažené texty zůstaly
v `/root/corpus/raw` (142 MB), takže `corpus_build.py` je nemusí tahat znovu.
Zaplatí se ale indexace — při 1,2–1,6 s/chunk zhruba dva dny.

### Co měření ukázalo

**Rozsah zvládnutý bez chyby.** 0 chunků bez embeddingu, 0 nedokončených
dokumentů. DB 1170 MB, z toho `chunk_embedding_hnsw` 295 MB, `chunk_trgm_gin`
154 MB, `chunk_tsv_gin` 63 MB. **Stavba HNSW nad 114 tisíci chunky trvala 27 s.**

**Přesnost s known-good dokumentem, měřeno BEZ reranku** (10 dotazů na jazyk,
dotazy z korpusu). Ta podmínka je podstatná a v reportu se snadno přehlédne —
`corpus_measure.py` volá `/search` s `"rerank": False`, takže tahle tabulka
říká, jak dobrá je samotná RRF fúze, ne nasazená pipeline:

| jazyk | top-1 | MRR |
|---|---|---|
| cs | 9/10 (90 %) | 0,900 |
| en | 7/10 (70 %) | 0,850 |
| de | 10/10 (100 %) | 1,000 |
| la | 10/10 (100 %) | 1,000 |

Lexikální a fuzzy větev trefily správný dokument skoro vždy (9–10 z 10), dense
větev slabší (6–9). Nad velkým korpusem tedy nese kvalitu především lexikální
větev, ne vektorová — přesně opačně, než by se u „vektorového vyhledávání"
čekalo.

**Detekce jazyka: 91/96 = 94,8 % „hrubě", ale skutečná přesnost je 100 %.**
Rozdíl je potřeba umět přečíst. Všech pět chyb (`en-0230/0240/0250/0260`,
`la-0010`) jsou dokumenty, u kterých volání na `cheap` **vůbec neproběhlo** —
propadly na `DEFAULT_LANG=cs`. V logu je 14 takových selhání (13× 429 z vyčerpané
denní kvóty, 1× timeout); zbylých 9 se trefilo náhodou, protože šlo o české
dokumenty. Skutečně provedených detekcí sedělo **79/79**, dokumentů
s frontmatterem **860/860**.

Poučení má dvě části. Za prvé, **statistika „detekce trefila jazyk" je u českého
fallbacku systematicky nadhodnocená** a rozlišit to jde jen kombinací logu a DB —
z reportu samotného ne. Za druhé, **fallback nespouští jen kvóta**: ten jeden
timeout vyrobil úplně stejnou tichou chybu jako 429, takže grepovat jen `429`
nestačí:

    journalctl -u retrieval | grep "detekce jazyka pres cheap selhala"

Kvótu jako příčinu odstranil přechod aliasu `cheap` na placený model
(viz `PIPELINE.md`), tichost selhání ne.

**Náprava jazyka doběhla automaticky** (jednotka `scale-fix-lang`, 05:15:19)
a opravila přesně těch 5 dokumentů — za cenu **1 embeddingu a 100 recyklovaných
chunků**. Záměrně až po měření, aby report popisoval běh takový, jaký byl.

**Recyklace chunků podle obsahu ověřena na stochunkovém dokumentu**, a je to
nejlepší zpráva celého testu:

| zásah | embeddingů | recyklováno |
|---|---|---|
| změna odstavce uprostřed | 1 | 100 |
| vložení odstavce na začátek (posun ordinálů) | 1 | 101 |
| změna jen `lang: cs → en` | **0** | 102 |

Reindex celého korpusu při jednom zásahu trval 1,6–3,4 s. Posun ordinálů
nestojí nic navíc, protože se klíčuje podle obsahu, a změna jazyka nestojí
ani jeden embedding.

**Tempo indexace 1,04–1,62 s/chunk**, průměr po restartu ~1,21 s (návrhový
odhad byl 1,22 s). Kolísá po půlhodinách; ETA počítej z 1,2–1,6, ne z 1,04.

**Cena reranku nad plným indexem je řádově jinde než nad krátkými poznámkami** —
bez reranku 2,30 s, `top_k=5` 11,49 s, `10` 13,24 s, `20` 26,15 s, `40` 47,23 s.
Rozbor v `PIPELINE.md`; důsledek pro nastavení je bod 2 níž.

Práva na `/srv/brain/markdown` a izolace smoke testu, které příprava testu
odhalila, jsou body 6 a 7 výš.

## Virtual keys: co který klíč smí

**Klíče se nikde v repozitáři nevytvářejí.** Jsou to podman secrets
s hodnotou vrácenou z `POST /key/generate`, uložené jen v databázi LiteLLM
a v secretech na brainu. Tahle sekce je proto **jediný zápis o tom, co
který klíč smí** — a záloha secretů ji nekryje (viz README o nekrytých
podman secretech).

| klíč | smí volat | rozpočet | rpm |
|---|---|---|---|
| `kryton` | `reasoning`, `workhorse`, `cheap`, `cheap-fallback`, **`backstop`** | 20 USD / 30 d | 60 |
| `retrieval-service` | `cheap`, `cheap-fallback` | 5 USD / 30 d | 60 |
| `n8n` | `workhorse` | 5 USD / 30 d | — |

**SEZNAM MUSÍ OBSAHOVAT CELÝ FALLBACK ŘETĚZ, NE JEN PRIMÁRNÍ ALIAS.**
Fallback na `model_group`, který klíč volat nesmí, **se tiše rozbije** —
LiteLLM ho odmítne s `key_model_access_denied` a řetěz na tom místě končí.

Stalo se to dvakrát:

- `retrieval-service` měl původně jen `["cheap"]`, takže fallback na
  `cheap-fallback` neexistoval. Zachyceno 2026-08-09, `litellm-config.yaml`
  na to od té doby na dvou místech varuje.
- **`kryton` měl jen `reasoning/workhorse/cheap/cheap-fallback`, takže
  `backstop` vracel HTTP 403.** Zjištěno až 2026-09-09, tedy skoro měsíc
  po tom, co `backstop` vznikl kvůli incidentu P6. Za normálního provozu to
  není vidět: `backstop` se nezavolá ani jednou. Projevilo by se to jedině
  ve scénáři P6 (2026-08-12), kdy spadl `reasoning` i `workhorse` naráz —
  tedy právě v tom, pro který `backstop` existuje.

**Nejrychlejší kontrola téhle třídy chyb** je porovnat dva výpisy
`GET /v1/models`. Vrací seznam **filtrovaný podle klíče**, takže pod klíčem
komponenty chybí to, co volat nesmí:

    MK=$(podman exec litellm printenv LITELLM_MASTER_KEY)
    KK=$(podman exec kryton printenv LITELLM_API_KEY)
    for K in "$MK" "$KK"; do
      curl -s -H "Authorization: Bearer $K" http://127.0.0.1:4000/v1/models \
        | python3 -c 'import json,sys; print(sorted(m["id"] for m in json.load(sys.stdin)["data"]))'
    done

Rozdíl těch dvou řádků musí obsahovat jen aliasy, které daná komponenta
volat NEMÁ — ne aliasy, na které se propadá.

### Oprava seznamu modelů u existujícího klíče

Nezakládej nový klíč; `key/update` zachová rozpočet, spend i rpm:

    MK=$(podman exec litellm printenv LITELLM_MASTER_KEY)
    TOKEN=$(curl -s -H "Authorization: Bearer $MK" \
      "http://127.0.0.1:4000/key/list?return_full_object=true&size=20" \
      | python3 -c "
    import json,sys
    d=json.load(sys.stdin)
    for k in d.get('keys', d if isinstance(d,list) else []):
        if isinstance(k,dict) and k.get('key_alias')=='kryton':
            print(k['token']); break
    ")
    curl -s -X POST http://127.0.0.1:4000/key/update \
      -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
      -d "{\"key\":\"$TOKEN\",\"models\":[\"reasoning\",\"workhorse\",\"cheap\",\"cheap-fallback\",\"backstop\"]}"

`/key/list` vrací `token`, což je hash klíče — ten stačí jako identifikátor
pro `key/update` a `key/info`. **Samotný klíč z LiteLLM přečíst nejde**,
je jen v podman secretu; kdyby se ztratil, musí se vygenerovat nový
a přepsat secret `litellm_kryton_key`, pak restartovat Krytona.

Ověření, že oprava zabrala, je v `scripts/35-fallback-retez.py`.

## Doporučené další kroky

1. ~~Napsat Krytona~~ — **běží od 2026-08-09**, port 3001, unit `kryton.service`,
   startuje i po rebootu. Kód v `kryton/`, smoke test
   `scripts/13-smoke-kryton.py` (34 kontrol). Kontrakt retrievalu upraven pro
   Python: místo PDO DSN (`pgsql:host=…;dbname=…`, což je PHP) je teď jeden
   secret `retrieval_database_url` s libpq URL, stejně jako u litellm a kryton.

   **Poučení z prvního spuštění stojí za zapamatování: Kryton byl celý napsaný,
   postavený do obrazu, se secrety, DB i quadletem — a přitom ani jednou
   nespuštěný.** První běh hned ukázal, že `page()` prohnala už vyrenderované
   tělo stránky druhým průchodem šablony s `autoescape=True`, takže se **každá**
   stránka zobrazovala jako vlastní zdroják. Opraveno přes `markupsafe.Markup`.
   Nasazený a spustitelný neznamená ověřený — u čehokoliv dalšího, co je
   „hotové, jen to ještě neběželo", počítej s tím, že to neběželo.

   Přestavba obrazu po změně zdrojáků:

       git -C /root/deploy pull
       podman build --network=host -t localhost/kryton:latest \
           -f /root/deploy/kryton/Containerfile /root/deploy/kryton
       systemctl restart kryton && systemctl status kryton

   **Když se změnil i quadlet (nový `Environment=` nebo `Secret=`), tenhle
   recept NESTAČÍ** — obraz se přestaví, ale unit pořád jede podle staré
   generované jednotky. Navíc musí proběhnout:

       /root/deploy/scripts/03-quadlets.sh    # přepíše VŠECHNY quadlety + daemon-reload

   **Skript přepisuje celý `/etc/containers/systemd`, takže před spuštěním
   zazálohuj** (`cp -a /etc/containers/systemd /root/quadlet-zaloha-$(date +%F-%H%M%S)`)
   a po spuštění `diff -ru` proti záloze. Ruční úprava živého quadletu by se
   tím jinak tiše ztratila.

   **Past nalezená 2026-08-17: `Secret=` v quadletu na NEEXISTUJÍCÍ podman
   secret znamená, že unit VŮBEC NENASTARTUJE** — ne že by jen ta jedna
   funkce nešla (stejné chování je popsané u `openrouter_api_key`
   v `scripts/02b-secrets-extra.sh`). Po přidání `Secret=` do quadletu proto
   VŽDY nejdřív `podman secret ls`, jestli cíl existuje, a až potom restart.
   Jinak si restartem složíš běžící službu.

   **Past nalezená 2026-08-17: P5 (MCP server) bylo v gitu od 2026-08-13,
   ale na disku NIKDY.** Živý `kryton.container` byl z 2026-08-10, řádek
   `Secret=mcp_bearer_token` v něm nebyl a secret `mcp_bearer_token`
   neexistoval — běžící kontejner `MCP_BEARER_TOKEN` neměl, takže `/mcp`
   celou dobu odmítal každý požadavek. Zjistilo se to náhodou při nasazování
   Telegram kroku 3 (`diff` quadletu proti záloze), ne testem. Doplněno
   tehdy spuštěním `02b-secrets-extra.sh` (idempotentní, dogeneruje jen
   chybějící secret a vypíše MCP token).

   **Je to TŘETÍ výskyt téhož vzorce** (Kryton napsaný-ale-nespuštěný,
   litellm-config rozjezd git vs. disk, teď MCP quadlet): *„commitnuto"
   v tomhle projektu neznamená „nasazeno" a „nasazeno" neznamená „ověřeno".*
   Po každém nasazení ověř konkrétní věc, která se měla změnit — u env
   proměnných `podman exec kryton printenv NAZEV`, ne jen `systemctl status`.

   **Past nalezená 2026-08-17: smoke test NESPOUŠTĚJ uvnitř živého
   `kryton` kontejneru.** `podman exec -i kryton python - < scripts/13-smoke-kryton.py`
   projde, ale unit má `MemoryMax=800M` a druhý Python proces s celým
   fastmcp/uvicorn stackem strop překročí — OOM killer zabil testovací
   proces (`exit 137`), a při jiném pořadí obětí mohl zabít i službu.
   Správně izolovaně, jak radí docstring testu (podman místo dockeru):

       cd /root/deploy && podman run --rm -i --network=host \
           -v /root/deploy/kryton:/srv/kryton:ro,Z python:3.13-slim \
           sh -c 'pip install -q -r /srv/kryton/requirements.txt; cat > /s.py; python /s.py' \
           < scripts/13-smoke-kryton.py

   Test si `telegram._call` patchuje, takže žádná skutečná zpráva na
   Telegram neodejde — řádky „otazka dne odeslana" v jeho výstupu jsou ze
   stubované cesty a neznamenají, že bot něco poslal.

   **Past nalezená 2026-08-13, OPRAVENO TRVALE téhož dne: `git pull` do
   `/root/deploy` neaktualizoval `conf/litellm-config.yaml` u LiteLLM.**
   Kryton se staví z `/root/deploy` přímo (viz recept výš), ale LiteLLM
   quadlet mountoval samostatnou kopii —
   `Volume=/srv/brain/conf/litellm-config.yaml:/app/config.yaml:ro,Z` — a
   `/srv/brain/conf/litellm-config.yaml` **nebyl symlink** na
   `/root/deploy/conf/`, byl to oddělený soubor. `git pull` ho tiše
   neměnil; test `/model/info` po „nasazení" změny v `num_retries` (P6)
   ukázal starou hodnotu, dokud se soubor ručně nezkopíroval. Rozjezd
   trval už od 2026-08-10 (poznámka o ministralu v gitu, chyběla na disku).

   **Oprava:** `/srv/brain/conf/litellm-config.yaml` je teď symlink na
   `/root/deploy/conf/litellm-config.yaml`. `git pull` + `systemctl restart
   litellm` teď stačí samo, žádný `cp` navíc. Ověřeno živě — po přesměrování
   na symlink LiteLLM úspěšně nastartoval a `/model/info` hlásil správné
   hodnoty (`workhorse`/`backstop` `num_retries: 3`).

   Restart při přesměrování na symlink jednou krátce zaškobrtl na běžné
   podman/netavark chybě při úklidu síťového namespace
   (`netavark: open container netns: ... No such file or directory`) —
   nesouviselo to se symlinkem, `Restart=always` kontejner hned znovu
   nastartoval bez zásahu.

   **Ostatní soubory v `/srv/brain/conf/`** (`litellm-health.sh`,
   `brain-firewall.nft`, `postgresql-tuning.conf`, `Containerfile.postgres`)
   mají stejné riziko rozjezdu, jen se zatím neprojevilo, protože se needitují
   často — zatím ponechány jako kopie, symlinkován jen `litellm-config.yaml`,
   který se mění nejčastěji.

   **Vícejazyčnost:** `POST /search` bere volitelné `lang` (`cs|en|de|la`).
   Kryton ho **záměrně neposílá** — rozhodnuto 2026-08-09 nechat detekci na
   retrievalu. Když `rewrite` běží, jazyk určí `cheap` ve stejném volání jako
   klíčová slova, takže to nestojí volání navíc. Cena toho rozhodnutí je, že
   dotazy i zachycené poznámky spadají pod denní strop aliasu `cheap`
   (50/den) a jeho selhání je tiché — propadne na `DEFAULT_LANG=cs`.
   Kdyby to začalo vadit, stačí do `core.search()` doplnit `lang` a do
   `core.capture()` frontmatter `lang:`; odpověď pak nese `lang_source:
   "request"`. Neznámý kód vrací 422.
2. ~~Zvážit výměnu rerankeru za `bge-reranker-v2-m3`~~ — hotovo a změřeno,
   top-1 přesnost 57 % → 100 %. **`RERANK_TOP_K` byl nerozhodnutý až do
   2026-08-19; dnes je 10, viz vyřešení na konci tohoto bodu.** Čísla, se kterými se
   rozhodovalo dřív (20 → 3,8 s, 10 → 2,0 s), platila nad krátkými testovacími
   poznámkami. Nad plným indexem stojí dnešní nastavení `RERANK_TOP_K=20`
   **26,15 s na dotaz**, `10` je 13,24 s a bez reranku 2,30 s — cena se totiž
   řídí objemem textu, ne počtem kandidátů, a reálné chunky jsou mnohem delší.
   Interaktivně je 26 s nepoužitelné.

   Druhá strana rovnice — **kolik reranking nad RRF fúzí přidává kvality** —
   dosud změřená nebyla vůbec: oddíl 3 reportu měří `"rerank": False`, oddíl 4
   jen latenci.

   `scripts/06-eval-reranker.py` na tohle **není**, i když to podle názvu
   vypadá: měří reranker izolovaně, sedm dotazů nad osmi krátkými vymyšlenými
   větami poslanými přímo na Infinity. Jako srovnání dvou modelů rerankeru
   posloužil, o přínosu v pipeline neříká nic; dokumenty má natvrdo v kódu.

   ### Změřeno 2026-08-10: `scripts/16-rerank-value.py`

   47 dotazů (12 na jazyk, jeden bez použitelných klíčových slov vypadl),
   párově — každý dotaz prošel všemi konfiguracemi nad týmž indexem:

   | konfigurace | top-1 | MRR | medián latence |
   |---|---|---|---|
   | bez reranku | **44/47 (94 %)** | **0,968** | **2,26 s** |
   | rerank 10 | 40/47 (85 %) | 0,922 | 13,99 s |
   | rerank 20 | 40/47 (85 %) | 0,920 | 23,92 s |

   Párově: **zlepšilo 2 dotazy, zhoršilo 6, beze změny 39** — stejně v obou
   konfiguracích a ve všech čtyřech jazycích stejným směrem.

   **Tenhle výsledek se ale nesmí číst jako „reranking je k ničemu",** protože
   měření je k němu ze tří důvodů nespravedlivé:

   1. **Dotazy nejsou otázky.** `make_keywords()` vezme šest dlouhých slov
      přímo z cílového chunku — pytel vzácných slov opsaný z hledaného textu.
      Ideální vstup pro lexikální a fuzzy větev, skoro nejhorší možný pro
      sémantický cross-encoder. Metodika je převzatá z oddílu 3 kvůli
      srovnatelnosti a její vychýlení se přeneslo s ní.
   2. **Základ je u stropu.** 44/47 nenechává kam se zlepšovat; rerank už může
      jen jinak rozhodovat remízy, a v šesti případech rozhodl hůř.
   3. **6 proti 2 není průkazné.** Znaménkový test nad osmi rozdílnými páry
      dává p ≈ 0,29. Neříká to „rerank škodí", ale „přínos se nenašel".

   Pevné z toho je jediné, zato dost: **na tomhle typu dotazů si reranking
   nezasloužil ani vteřinu z těch 21,7.**

   ### Co tím pořád není zodpovězené

   Skutečné použití je **přirozená otázka nad osobními poznámkami** — přesně
   režim, kde cross-encoder pomáhá nejvíc a kde je pytel klíčových slov nejméně
   reprezentativní. Živý test Krytona („čím se ladí latence dotazu u HNSW")
   ten režim trefil, běžel **s** rerankem a vrátil správný dokument první.

   Rozhodne až sada přirozených otázek s known-good dokumentem. Nad korpusem
   beletrie se nevyrobí dobře — je to další důvod, proč je nejcennější věcí
   dostat do systému skutečné poznámky.

   **VYŘEŠENO 2026-08-19.** Skutečné poznámky v systému jsou (10 deníkových
   záznamů) a sada vznikla: `scripts/28-rerank-value-denik.py`, 14 přirozených
   otázek, párově, se shodnými `keywords` i `lang` v obou ramenech. Výsledek:
   bez reranku 13/14 top-1 za 0,13 s, `top_k=10` i `top_k=20` shodně 14/14
   za 3,95 s resp. 10,36 s. Rerank zlepšil jediný dotaz ze čtrnácti a žádný
   nezhoršil (znaménkový test p = 1,0).

   **`RERANK_TOP_K` snížen na 10** — ne naslepo, ale proto, že kvalita vyšla
   identická za třetinu času, a ten jediný zisk vznikl přerovnáním uvnitř
   vrácené osmičky (cíl byl na RRF pozici 2), což `top_k=10` umí dál.
   Rerank se nevypíná: ten jeden dotaz rozlišoval **istio** od **Kubernetes
   1.36**, a takových nad deníkem přibude.

   Pozor na to, co tím rozhodnuté NENÍ: baseline 13/14 je **u stropu**, tedy
   tatáž vada, jakou mělo měření nad zátěžovým korpusem (44/47). Nad čtrnácti
   dokumenty najde RRF správný dokument skoro vždy. Přeměřit, až korpus
   poroste — vedeno jako **P12 v `POZADAVKY.md`, nejpozději 2026-10-19**.
3. ~~Dodat skutečné OpenRouter a bigpickle klíče~~ — hotovo, ověřeno.
4. ~~Vícejazyčnost (cs, en, de, la)~~ — hotovo a ověřené na čtyřech poznámkách,
   viz `PIPELINE.md` a `scripts/09-multilang-test.py`.
5. ~~Zneplatnit použitý Tailscale auth key~~ — **hotovo**, klíč měl platnost
   jeden den a vypršel sám.
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

   **`git pull` se nesmí dělat, když z klonu běží nějaký skript.** Bash čte
   soubor z disku průběžně, ne celý dopředu, takže přepsání skriptu pod běžícím
   procesem umí rozbít provádění uprostřed. Kvůli tomu čekal pull dva dny, než
   doběhl zátěžový test z `10-scale-test.sh`. U dlouhých běhů to plánuj předem.

   **V repozitáři nejsou žádné secrets** a nemají tam být — hesla i API klíče
   jdou přes podman secrets a odkazy `os.environ/` v `litellm-config.yaml`
   (ověřeno grepem před prvním commitem). **Tuhle mezeru git neuzavíral —
   od 2026-08-10 ji zavírá `scripts/19-kryton-backup.sh`**, viz bod 10 níž.

   Databáze `retrieval` zálohu nepotřebuje — je to derivovaný index a dá se
   kdykoliv postavit znovu z markdownu.
9. **První naplnění skutečným korpusem.** Až přibudou opravdové poznámky,
   pusť `scripts/08-first-fill.sh` (drop HNSW → naplnit → postavit → VACUUM).
   Nad korpusem bez frontmatteru zvaž `DETECT_LANG_ENABLED=0` — detekce stojí
   ~2–3 s na dokument a jazyk jde doplnit později zadarmo (viz `PIPELINE.md`).
10. ~~Databáze `kryton` derivovaná nebude, bude potřebovat vlastní zálohu~~ —
    **hotovo 2026-08-10.** `scripts/19-kryton-backup.sh` + denní systemd timer
    `kryton-backup.timer` (03:15 UTC, `scripts/20-kryton-backup-setup.sh`).

    Rozsah je širší, než jen „data": zadání bylo, aby se z zálohy dal obnovit
    i **systém**, ne jen obsah. To znamenalo tři věci najednou:

    - `pg_dump -Fc` databází **`kryton`** (konverzace, nahrávky) a **`litellm`**
      (virtuální klíče, rozpočty, spend logy — bez ní by po obnově chyběly
      definice `cheap`/`workhorse`/`reasoning`). Databáze `retrieval` v záloze
      záměrně chybí — je derivovaná, viz bod 8 výš.
    - **Šifrovaný balíček všech podman secrets** kromě šifrovacího klíče
      samotného (kruhová závislost). I „privátní" S3 bucket není místo pro
      čitelná hesla.
    - Uloženo na S3 přes stejný profil jako originály z P2/P3 (`app/storage.py`,
      nové `put_backup`/`get_backup`/`list_backups`/`delete_backup`), jen jiný
      prefix (`BACKUP_S3_PREFIX`, výchozí `db-backups/`) a volitelně jiný
      bucket (`BACKUP_S3_BUCKET`, prázdné = stejný jako `S3_BUCKET`) — „do
      budoucna konfigurovatelné" bez nové abstrakce.

    **Klíčová vlastnost: ověření obnovy při KAŽDÉM běhu, ne jen při nasazení.**
    Netestovaná záloha je schrödingerovská záloha. Skript po každém uploadu
    dump stáhne zpět, obnoví do zahoditelné DB a porovná počty řádků VE
    VŠECH tabulkách — dynamicky přes `information_schema`, ne napevno
    vypsaná jména, protože `litellm` má 69 tabulek generovaných Prismou.
    Ověřeno živě 2026-08-10: všech 6 tabulek `kryton` i všech 69 tabulek
    `litellm` sedí, sada 19 secrets po dešifrování sedí. Rotace starých
    záloh (`BACKUP_RETENTION_DAYS`, výchozí 30) běží AŽ PO ověřené obnově,
    aby selhání aktuálního běhu nikdy nesmazalo poslední funkční zálohu.

    **Past, na kterou stojí za to pamatovat:** `podman exec -i` uvnitř
    `while read ... < <(process substitution)` sdílí stdin s tou substitucí.
    První živý běh proto ověřil jen JEDNU tabulku z šesti/69 místo všech —
    smyčka se provedla jednou a tiše skončila, `pg_restore` přitom neselhal,
    takže by to bez pozorného čtení výstupu prošlo jako úspěch. Oprava:
    `</dev/null` na vnořených voláních.

    **Šifrovací klíč (`podman secret backup_encryption_key`) žije jen na
    brainu a MUSÍ mít kopii mimo něj** (password manager) — bez ní je záloha
    secrets k ničemu, kdyby brain fyzicky zmizel. Databázové dumpy na tomto
    klíči nezávisí. Vygenerován a vypsán jednou 2026-08-10; `setup` skript ho
    už nikdy nepřegeneruje (rozbilo by to čitelnost starých záloh).

    **Co záloha řeší jen zčásti:** OpenRouter a OpenCode Zen API klíče zálohou
    procházejí (jsou to jen secrets), ale ztráta brainu je nerozbije — zůstávají
    platné u poskytovatele bez ohledu na to. Záloha je tu pro pohodlí (nemusí se
    ručně dohledávat staré hodnoty), ne proto, že by bez ní přestaly fungovat.
