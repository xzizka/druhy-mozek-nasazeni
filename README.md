# Fáze 1 — jádro second brainu na 16 GB

Kompletní, funkční second brain: PostgreSQL s pgvector, Kryton, Retrieval
Service s hybridním vyhledáváním, lokální embeddings a reranking na CPU,
veškerá generativní inference externě.

Bez n8n, bez agentů, bez lokálního LLM, bez Valkey, bez ClickHouse.
Ty přicházejí ve fázích 2–4.

---

## Rozhodnutí: LXC, ne VM

Pro fázi 1 je správná volba **jeden unprivilegovaný LXC kontejner**
s Podmanem uvnitř. Dřívější doporučení „VM pro Postgres“ platilo pro
128 GB rozpočet a tady neplatí ze čtyř důvodů.

**Konflikt sysctls zmizel s Valkey.** Jediný pádný argument pro VM byl,
že `vm.overcommit_memory` není namespacovaný a Valkey ho chce na `1`,
zatímco Postgres na `2`. Ve fázi 1 Valkey není a jiného konzumenta
`vm.*` tady nemáš — takže potřeba druhého kernelu padá.

**Page cache je společná.** To je rozhodující argument. V LXC je page
cache hostitelská a sjednocená; ve VM buď cachuješ dvakrát (guest i host),
nebo nastavíš `cache=none` a guestova cache je omezená jeho přidělenou
pamětí. Na 16 GB, kde výkon HNSW scanů **je** page cache, je sdílená
cache měřitelná výhoda, ne kosmetika.

**Limit je strop, ne rezervace.** Cgroup limit LXC nechává nevyužitou
paměť k dispozici hostiteli. VM si svých 13 GB drží, ať je používá, nebo ne
— a ballooning proti Postgresu se `shared_buffers` nechceš.

**Bezpečnostní argument pro VM se fáze 1 netýká.** Kernel LPE hranice je
potřeba proti n8n task runneru, který spouští libovolný JavaScript, a proti
agentům, kteří zpracovávají model-generovaný obsah. **Ani jedno ve fázi 1
neběží.** Až přijde fáze 3, dej n8n a agenta do vlastní VM, nebo tu slabší
hranici vědomě přijmi. To je konkrétní spouštěč, kdy se k tomuhle
rozhodnutí vrátit.

Co si LXC vybírá jako cenu: rootfs nesmí být na ZFS (overlayfs nad ZFS
nefunguje, spadne to na `vfs` driver a každá vrstva image se duplikuje),
`nesting=1` je nutný, a **do cgroup limitu se účtuje i page cache**, takže
limit musí pokrýt RSS i cache.

**Pokud Proxmox běží jen kvůli téhle platformě, nepoužívej ani LXC** —
běž bare-metal Ubuntu/Debian s Podmanem. Ušetříš hypervizor i ARC a celá
otázka LXC versus VM je bezpředmětná. Skripty `02` a `03` fungují
bez úprav i tam; přeskoč `01`.

---

## Rozpočet paměti

| Komponenta | RSS | `MemoryMax` |
|---|---|---|
| OS, systemd, Podman, Tailscale | 0,8 GB | — |
| PostgreSQL 17 (`shared_buffers` 2 GB) | 2,5 GB | — |
| Infinity (bge-m3 + reranker, ONNX int8) | 1,8 GB | 2600M |
| LiteLLM | 0,5 GB | 800M |
| Retrieval Service | 0,4 GB | 700M |
| Kryton | 0,5 GB | 800M |
| **Součet RSS** | **6,5 GB** | |
| Page cache (HNSW index + heap) | ~5 GB | |
| Limit LXC | | 13 GB |

Postgres úmyslně nemá `MemoryMax`. Cgroup OOM kill postmastera je horší
než swap, a jeho spotřebu už omezuje `shared_buffers` a `work_mem`.

---

## Postup nasazení

```
# na Proxmox hostu (vynech při bare-metal)
CTID=201 STORAGE=local-lvm ./scripts/01-proxmox-host.sh
pct enter 201

# uvnitř
./scripts/02-guest-bootstrap.sh
tailscale up --ssh
# doplň externí klíče:
printf '%s' 'sk-or-...' | podman secret create openrouter_api_key -
printf '%s' '...'        | podman secret create bigpickle_api_key -

cp -r conf sql /srv/brain/
./scripts/03-quadlets.sh
podman build -t localhost/postgres-cs:17 -f /srv/brain/conf/Containerfile.postgres
systemctl start postgres
./scripts/04-init-db.sh
systemctl start infinity litellm      # první start stahuje ~1,5 GB modelů
systemctl start retrieval kryton
```

---

## Česká full-text konfigurace — proč je to vlastní krok

**PostgreSQL nedodává snowball stemmer pro češtinu.** Vestavěné jazyky
jsou dánština, holandština, angličtina, finština, francouzština, němčina,
uherština, italština, norština, portugalština, rumunština, ruština,
španělština, švédština a turečtina. Čeština chybí, takže bez konfigurace
v `sql/02-retrieval.sql` by lexikální větev jela na `simple` bez
lemmatizace a dotaz „latenci“ by nenašel dokument s „latence“.

Řešením je hunspell slovník `cs_CZ` z distribuce, zabalený do image
(`conf/Containerfile.postgres`). Debianí balík je v UTF-8, takže stačí
kopie bez konverze.

**Na pořadí slovníků záleží.** Slovníky v konfiguraci nejsou pipeline,
ale alternativy: první, který token rozpozná, vyhrává, a filtrovací
slovník (`unaccent`) token upraví a pošle dál. Kdyby `unaccent` běžel
první, dostal by hunspell „resim“ místo „řeším“, český slovník by to
nenašel a lemmatizace by se ztratila úplně. Ověřeno:

| pořadí | `řeším latenci vektorových indexů` |
|---|---|
| `unaccent, cs_hunspell` | `resim, latence, vektorovych, index` — bez lemmat |
| `cs_hunspell, unaccent` | `řešit, latence, vektorový, index` — správně |

Volíme druhé. **Důsledek:** index je lemmatizovaný, ale s diakritikou,
takže dotaz napsaný bez diakritiky lexikální větev minout musí. Proto má
`hybrid_search` třetí, trigramovou větev.

---

## Hybridní vyhledávání — tři větve, RRF fúze

| větev | index | co řeší |
|---|---|---|
| dense | HNSW nad `halfvec(1024)` | semantika, parafráze, tolerance k zápisu |
| lexical | GIN nad `tsvector` (czech) | přesné termíny, lemmatizovaná čeština, precision |
| fuzzy | GIN `gin_trgm_ops` nad unaccent textem | identifikátory, verze, překlepy, dotazy bez diakritiky |

Fúze je **Reciprocal Rank Fusion**, ne vážená suma skóre: kosinová
distance a `ts_rank_cd` nejsou na srovnatelné škále a normalizovat je
napříč dotazy nelze. RRF pracuje jen s pořadím, takže je na škále
nezávislé. `p_k = 60` je hodnota z původního paperu.

Funkce vrací i jednotlivá pořadí (`r_dense`, `r_lexical`, `r_fuzzy`) —
bez nich váhy neladíš, jen hádáš. Ověřeno na testovacích datech, že
dotaz bez diakritiky správně vytáhne fuzzy větev do popředí tam, kde
lexikální nevrátí nic.

Reranking (`bge-reranker-base` přes Infinity) běží **v aplikaci nad
top-20 kandidáty z RRF**, ne v SQL. Cross-encoder na CPU je nejdražší
krok celé cesty; `bge-reranker-v2-m3` (568M) by na CPU zabral 3–5 s
na dotaz, base varianta (278M) nad 20 kandidáty zvládne 1–2 s.

---

## Co je ověřené a co ne

Ověřeno spuštěním proti PostgreSQL 16 s pgvector 0.8.1 a hunspell `cs_CZ`:
česká FTS konfigurace včetně lemmatizace, `halfvec` sloupec, generované
sloupce `content_tsv` a `content_norm`, všechny tři indexy, funkce
`hybrid_search` včetně RRF fúze, `trust_level` filtru a grantů pro
`retrieval_app`.

Neověřeno, protože to potřebuje cílové prostředí: build image, quadlety,
Infinity CLI přepínače (mezi verzemi se měnily — projdi
`podman run --rm IMAGE v2 --help`), a LXC provisioning.

`sql/*.sql` je psané pro PG17, testované na PG16 — nic v nich není
verzově specifické nad PG16.

---

## První naplnění indexu

Nejdražší operace celé fáze. Postup, který se vyplatí:

1. `DROP INDEX chunk_embedding_hnsw` — inkrementální insert do HNSW je
   řádově pomalejší než build nad hotovou tabulkou.
2. Naplň `document` a `chunk` bez embeddingů.
3. Dávkově dopočítej embeddingy přes Infinity (batch 8–16, `COPY` nebo
   `UPDATE ... FROM (VALUES ...)`).
4. `SET maintenance_work_mem = '1GB'; CREATE INDEX ...` — když se build
   nevejde, spadne do spill-to-disk a poteče několikanásobně déle.
5. `VACUUM ANALYZE retrieval.chunk`.

**Nemíchej embeddingy z různých modelů v jednom indexu.** Pokud tě láká
zrychlit první naplnění externím embedding API, musí to být tentýž model
ve téže verzi — jinak musíš při přechodu na lokální model přeembeddovat
všechno.

---

## Známé mezery — vědomě odložené

Bezpečnost a chybějící komponenty řešíme později, ale dvě věci si zaslouží
zmínku už teď, protože jsou důsledkem redukce na jeden stroj:

**Není záloha.** Jeden stroj, jeden disk, žádné WAL archiving
(`archive_mode = off`). Markdown v `/srv/brain/markdown` je autoritativní
zdroj a Postgres je derivovaný index — udělej z markdownu git repozitář
a odklop ho jinam, než na tomhle začneš skutečně pracovat. To je nejlevnější
možná pojistka: obnovujeme git, index se regeneruje.

**Není eval loop.** Bez něj nezjistíš, jestli změna chunkingu nebo vah RRF
pomohla. Ve fázi 2 přidej Langfuse datasety s ~50 ručně anotovanými dotazy
a měř context recall — u second brainu je retrieval kvalita ten produkt.
