# Indexační pipeline — návrh s inkrementálním reindexem

Podklad pro Retrieval Service (Python). Vychází z toho, co je na nasazeném
systému **změřené**, ne z odhadů. Doplňuje README, které popisuje jen postup
prvního naplnění.

## Proč inkrementálně

Embedding je jediná drahá operace celé cesty. Změřeno na nasazeném Infinity
(bge-m3, torch, CPU i7-8550U, 4 vlákna):

| operace | latence |
|---|---|
| embedding krátkého textu | 0,10–0,15 s |
| embedding dlouhého chunku (~2 kB) | **1,7–2,1 s** |
| dávka 5 dlouhých chunků | 9,2 s (tedy ~1,84 s na chunk) |

**Dávkování nepomáhá** — dávka pěti chunků trvá pětinásobek jednoho. Batch
velikosti 8 tedy šetří jen režii HTTP, ne výpočet. Jediná cesta ke rychlosti
je **neembeddovat to, co se nezměnilo**.

Řádový dopad: 1 000 chunků ≈ 30 minut, 10 000 chunků ≈ 5 hodin,
50 000 chunků ≈ den. Při inkrementálním běhu nad nezměněným korpusem jsou to
sekundy.

## Detekce změn — schéma na to už je připravené

Žádná migrace není potřeba, stačí to, co `02-retrieval.sql` vytvořil:

| sloupec | role v pipeline |
|---|---|
| `document.source_path` | UNIQUE, klíč proti souboru na disku |
| `document.content_hash` | sha256 celého markdown souboru → detektor změny |
| `document.indexed_at` | NULL nebo starší než hash ⇒ dokument nedokončený |
| `document.chunk_count` | kontrola konzistence |
| `chunk.content` | porovnání na úrovni chunku, viz níže |
| `chunk.embedding` | **NULL ⇒ čeká na embedding** ⇒ nese resumovatelnost |
| `UNIQUE (document_id, ordinal)` | stabilní identita chunku v dokumentu |
| `ON DELETE CASCADE` | smazání dokumentu uklidí chunky samo |

## Průběh jednoho běhu

### 1. Sken a klasifikace

Projdi `MARKDOWN_ROOT`, pro každý `*.md` spočítej sha256. Porovnej s DB:

| stav | akce |
|---|---|
| soubor je, dokument není | **NEW** → vlož dokument, chunkuj, embedduj |
| hash se liší | **CHANGED** → re-chunkuj, viz krok 2 |
| hash stejný a `indexed_at` není NULL | **UNCHANGED** → přeskoč úplně, nulová cena |
| hash stejný, ale `indexed_at IS NULL` nebo existuje chunk s `embedding IS NULL` | **RESUME** → dopočítej jen chybějící embeddingy |
| dokument je, soubor už není | **DELETED** → `DELETE FROM document`, chunky spadnou kaskádou |

Krok RESUME je to, co dělá běh přerušitelným. Při 1,8 s na chunk je přerušení
velkého prvního naplnění realita, ne hypotéza.

### 2. Změněný dokument — nepřeembeddovávej celý

Naivní postup smaže všechny chunky a embedduje znovu. U dlouhé poznámky, kde
jsi upravil jeden odstavec, je to plýtvání 1,8 s na každý nezměněný chunk.

Postup, který to obejde:

1. Načti staré chunky dokumentu jako mapu `content → embedding`.
2. Nachunkuj nový obsah.
3. Pro každý nový chunk: pokud **identický text** už v mapě je, **recykluj
   jeho embedding**. Jinak ho označ k embeddingu.
4. Nahraď chunky dokumentu novou sadou (recyklované rovnou s embeddingem).

Klíč je **obsah, ne `ordinal`** — vložením odstavce se všechny následující
ordinály posunou, ale jejich text zůstane stejný. Porovnávání podle ordinálu
by v takovém případě přeembeddovalo zbytek dokumentu zbytečně.

### 3. HNSW index — dvě různé strategie

README předepisuje pro první naplnění zahodit `chunk_embedding_hnsw`, naplnit
tabulku a index postavit znovu, protože inkrementální insert do HNSW je řádově
pomalejší než build nad hotovou tabulkou. **Pro inkrementální běh to ale
neplatí** — zahodit a znovu postavit index kvůli třem změněným chunkům je
mnohonásobně dražší než ty tři inserty.

Rozhoduj podle objemu:

```
chunku_k_embeddingu / celkem_chunku_v_tabulce
    > ~20 %  nebo tabulka prázdná
        → DROP INDEX chunk_embedding_hnsw
        → naplnit
        → SET maintenance_work_mem = '1GB'
        → CREATE INDEX ... USING hnsw (embedding halfvec_cosine_ops)
             WITH (m = 16, ef_construction = 64)
        → VACUUM ANALYZE retrieval.chunk
    jinak
        → obyčejné INSERT/UPDATE do existujícího indexu
        → ANALYZE retrieval.chunk
```

`maintenance_work_mem = '1GB'` nastavuj **jen pro tu session**, ne globálně —
je to špička, ne trvalá alokace, a globálně by konkurovala page cache.
Build spouštěj, když neběží nic jiného; `max_parallel_maintenance_workers = 2`
je v tuningu už nastavený.

### 4. Transakce a konzistence

Jeden dokument = jedna transakce. `indexed_at` nastav **až** po tom, co mají
všechny jeho chunky embedding. Když běh spadne v půlce dokumentu, zůstane
`indexed_at IS NULL` a příští běh ho vezme jako RESUME.

`chunk_count` udržuj konzistentní s reálným počtem chunků — je to jediná
levná kontrola, že se něco nerozešlo.

## Dotazovací cesta — dvě věci, které se snadno přehlédnou

Obojí je na nasazeném systému **změřené**, ne teoretické.

### Neposílej do hybrid_search celou otázku

`websearch_to_tsquery` spojuje termíny operátorem **AND**. Z otázky
„Proč záleží na pořadí slovníků při lemmatizaci?" vznikne

```
'proč' & 'záležet' & 'na' & 'pořadí' & 'slovník' & ('při'|'pře'|'přít') & 'lemmatizace'
```

a **nenajde nic**. Fuzzy větev zmlkne taky, protože `word_similarity` dlouhého
dotazu je ~0,24 proti prahu 0,5. Zůstane jen dense větev — hybridní hledání se
zdegeneruje na čistě vektorové, a to tiše.

Klíčová slova naopak rozsvítí všechny tři větve.

**Řešeno v `app/rewrite.py` přes LiteLLM alias `cheap`**, jak návrh zamýšlel —
`litellm-config.yaml` má na konci přímo příklad virtual key pro
retrieval-service s `"models":["cheap"]`. Klíč je omezený jen na tento alias
(ověřeno: `reasoning` odmítnut). Věta v README o tom, že retrieval vědomě nemá
přístup na LiteLLM, se týká **embeddingů**, ne přepisu dotazu.

Měřeno na nasazeném systému:

| zdroj klíčových slov | latence | výsledek pro „Proč záleží na pořadí slovníků při lemmatizaci v české fulltextové konfiguraci?" |
|---|---|---|
| `cheap` (LLM) | 1,7–2,9 s | `pořadí slovníků lemmatizace česká fulltextová konfigurace` |
| cache | 0,15 s | totéž |
| lokální extrakce | 0,14 s | `záleží pořadí slovníků lemmatizaci české fulltextové konfiguraci` |

LLM verze je lepší ve dvou věcech: zahodí sponová slova a **normalizuje do
1. pádu**. To druhé je podstatné pro trigramovou větev, která na rozdíl od
lexikální lemmatizovaná není — porovnává surový text bez diakritiky, takže
základní tvar tam trefuje lépe než skloňovaný.

Cena je ~2–3 s na první výskyt dotazu, opakování je z cache zdarma.
Vypnout jde per request (`"rewrite": false`) nebo globálně
`REWRITE_ENABLED=0`.

**Selhání nikdy nezastaví dotaz.** Ověřeno vstřikováním chyb: model, na který
klíč nemá právo → `local`; LiteLLM úplně nedostupný → `local`. Proto má
retrieval na litellm jen `After`, ne `Requires`. Je to důležité i proto, že
`cheap` po dohodě nemá v LiteLLM fallback, takže 429 se musí ošetřit tady.

### Trigramová větev skutečně zachraňuje dotazy bez diakritiky

Ověřeno na živých datech: dokument obsahuje frázi „vektorových indexů".

| dotaz | lexikální | fuzzy | výsledek |
|---|---|---|---|
| `vektorových indexů` | 1 shoda | ano | správný dokument 1. |
| `vektorovych indexu` | **0 shod** | `word_similarity = 1,0000` | správný dokument 1. |
| `maintenence_work_mem` (překlep) | 0 shod | ano | správný dokument 1. |

Index je lemmatizovaný **s diakritikou**, takže `'vektorovych'` není lexém a
lexikální větev minout **musí**. Tohle je přesně důvod, proč tam ta třetí větev
je — a proč se `hybrid_search` nesmí zjednodušit na dvě větve.

### Context window expansion — sousední chunky se slučují po hledání

Hledání a rerank hodnotí chunky izolovaně. Chunkování je **bez overlapu**
(viz `chunker.py`), takže hranice chunku je otázka rozpočtu `CHUNK_CHARS`,
ne významu — odpověď se na ní umí rozseknout přesně uprostřed myšlenky
a model dostane jen tu polovinu, která zrovna vyhrála v hledání.

`hybrid_search` teď vrací i `ordinal` (`sql/04-context-expand.sql` — přidání
sloupce do `RETURNS TABLE` vyžaduje `DROP FUNCTION` + `CREATE`, `CREATE OR
REPLACE` na to nestačí). `app/expand.py` s ním dotáhne k finálním výsledkům
sousední chunky ze stejného dokumentu (`ordinal ± EXPAND_WINDOW`, výchozí
okno 1).

**Aplikuje se AŽ na finální (přerankované, oříznuté na `limit`) výsledky,**
ne dřív — reranker je na tomto systému změřený na atomických chuncích (viz
"Rerank" níž), a předřazení expanze by mu poslalo jiný vstup, než na jaký
byl vybraný. Cena expanze proto padá jen na délku promptu pro
`ANSWER_MODEL`, ne na rerank.

**Okna, která se dotýkají nebo překrývají, se SLUČUJÍ do jednoho souvislého
bloku** — dva hity ze dvou sousedních odstavců jedné pasáže se tím nerozpadnou
zpátky na dva zdroje s duplicitním textem. Počet vrácených `results` proto
může klesnout pod `limit`; je to záměr, promítá skutečnou strukturu poznámek.

Merged výsledek nese metadata (skóre, `chunk_id`, `trust_level`) od
**anchoru** — nejlépe skórujícího hitu ve skupině — a jen `content`/
`heading_path` nahrazuje sloučenou verzí. Citace v Krytonovi tedy dál míří
na chunk, který o dotazu skutečně rozhodl; `content` kolem něj nese víc.

Vypnout jde per request (`"expand": false`) nebo globálně `EXPAND_ENABLED=0`.
Ověřeno živě proti reálnému indexu, ne jen jednotkově nad čistou funkcí:
`scripts/18-context-expand-check.sh` zapíše dokument s pěti nadpisy (=
pěti chunky), zeptá se na prostřední a ověří, že odpověď obsahuje oba
sousedy, ale ne chunky za nimi — a že `expand:false` sousedy nepřidá.
Stejná třída chyby jako u `analytics.py` (`SET LOCAL` prošlo smoke testem,
spadlo na živém Postgresu) by se stubovanou DB nikdy neprojevila.

## Rerank — regulátor latence

`RERANK_TOP_K` je přímý regulátor latence dotazu. Změřeno s nasazeným
`bge-reranker-v2-m3` na **krátkých testovacích poznámkách**:

| kandidátů | latence |
|---|---|
| 5 | 0,83 s |
| 10 | 2,01 s |
| **20** (hodnota z návrhu) | **3,77 s** |
| 40 | 8,21 s |
| 60 | 12,55 s |

### Nad reálnými chunky je to 5,7× horší

Přeměřeno 2026-08-07 na korpusu z reálných knih, kde má chunk plných ~1200
znaků místo pár vět:

| `rerank_top_k` | krátké poznámky | **reálné chunky** |
|---|---|---|
| bez reranku | — | **0,15 s** |
| 5 (fakticky 8, viz níž) | 0,83 s | 10,48 s |
| 10 | 2,01 s | 10,39 s |
| **20** | 3,77 s | **21,64 s** |
| 40 | 8,21 s | 40,36 s |

**Cena reranku se řídí objemem textu, ne počtem kandidátů.** Původní měření
proběhlo nad dokumenty o pár větách, takže podhodnotilo skutečnou cenu
řádově šestinásobně. Predikce README „3–5 s na dotaz" pro skutečný korpus
neplatí — při `RERANK_TOP_K=20` stojí dotaz ~22 s, což je interaktivně
nepoužitelné.

Dva důsledky, které z toho plynou:

1. **`rerank_top_k=5` nedělá, co se zdá.** `main.py` počítá
   `fetch = max(top_k, limit)`, takže při `RESULT_LIMIT=8` se rerankuje
   osm kandidátů, ne pět. Proto 5 a 10 měří skoro stejně.
2. **Páky jsou dvě, ne jedna.** Kromě `RERANK_TOP_K` snižuje cenu i
   `CHUNK_CHARS` — kratší chunky znamenají méně tokenů na kandidáta.
   Zároveň ale zvyšují počet chunků, a tím cenu indexace.

Bez reranku je dotaz 0,15 s. Rozdíl mezi 0,15 s a 22 s je tak velký, že
stojí za změření, jestli reranking nad RRF fúzí vůbec přidává kvalitu —
`scripts/06-eval-reranker.py` na to je, ale měřil zase jen krátké dokumenty.

### Kvalita: změřeno 2026-08-19 nad skutečnými poznámkami — nerozhodnuto

Otázka z předchozího odstavce má konečně měření nad tím, k čemu systém
slouží: `scripts/28-rerank-value-denik.py`, 14 přirozených otázek nad
deníkem (10 záznamů 08-10 až 08-18 plus nahraná žádost zastupitelstvu),
párově, se **shodnými `keywords` i `lang` v obou ramenech** — přepis přes
`cheap` proběhne jednou předem, takže se neměří rozptyl LLM ani jeho
hodinová cache.

| konfigurace | top-1 | cíl v top-8 | medián |
|---|---|---|---|
| bez reranku | 13/14 | 14/14 | **0,13 s** |
| `rerank_top_k=10` | 14/14 | 14/14 | 3,95 s |
| `rerank_top_k=20` | 14/14 | 14/14 | 10,36 s |

Rerank zlepšil **jediný dotaz ze čtrnácti** a žádný nezhoršil (znaménkový
test p = 1,0, jedna neshodná dvojice). Ten jeden je ale přesně ten typ,
kvůli kterému se cross-encoder nasazuje: „Kvůli které komponentě byly
špatně nastavené síťové politiky?" musí rozlišit záznam o **istio**
(08-13) od záznamu o **network policy pro Kubernetes 1.36** (08-18).
Lexikálně vyhrává 08-18, správně je 08-13, a rerank ho posunul z 2. na
1. místo. `rerank_top_k=10` dal identický výsledek za třetinu času.

**Nerozhodnuto to je proto, že baseline je u stropu** — stejná vada, jakou
měl rozbor v `16-rerank-value.py` (44/47). Nad čtrnácti dokumenty najde
RRF fúze správný dokument skoro vždycky a rerank už může jen jinak
rozhodovat remízy. Měření tedy **neospravedlňuje vypnutí** a zároveň
neospravedlňuje ani ponechání `RERANK_TOP_K=20`. Zopakovat, až korpus
poroste.

**Pozor na čtení latencí v téhle tabulce:** deníkové záznamy jsou jedna až
tři věty, takže jde o režim „krátké poznámky", ne o sloupec „reálné
chunky". Dnešních 10 s vzniká tím, že se do kandidátů pokaždé dostane
nahraná žádost — jediný dokument s plnými ~1200 znaky na chunk. Ověřeno
přímo na Infinity: 20× 75 znaků = **2,12 s**, 20× 1218 znaků = **23,63 s**.
Pravidlo „cenu řídí objem textu, ne počet kandidátů" tím platí dál a beze
změny; až budou chunky plné, je `RERANK_TOP_K=20` zase těch ~22 s.

## Kontrakt služby

Z `retrieval.container`, plus změna DSN pro Python:

```
Volume     /srv/brain/markdown -> /data/markdown  (read-only)
DATABASE_URL          postgresql://retrieval_app:***@postgres:5432/retrieval
EMBEDDING_URL         http://infinity:7997
EMBEDDING_MODEL       bge-m3                 -> halfvec(1024)
RERANK_MODEL          bge-reranker-v2-m3
RRF_CANDIDATES        60
RERANK_TOP_K          20
RESULT_LIMIT          8
MARKDOWN_ROOT         /data/markdown
HealthCmd             GET http://localhost:8080/healthz
MemoryMax             700M
```

**Pozor na `MemoryMax=700M`.** Python s HTTP klientem a chunkovacím kódem se
tam vejde, ale nedrž v paměti celý korpus ani velké dávky embeddingů —
1024 float32 na chunk je 4 kB, deset tisíc chunků naráz tedy 40 MB jen na
vektory, plus režie. Zpracovávej po dokumentech a streamuj.

Vlastní SQL už existovat nemusí: `retrieval.hybrid_search(...)` je hotová
a otestovaná, role `retrieval_app` má na ni `EXECUTE` a na tabulky DML.
Schéma služba měnit nesmí a nemá na to práva — migrace pouští superuser.

---

# Vícejazyčnost — stav k 2026-08-06

Podpora pro **cs, en, de, la**. Databázová část je hotová a ověřená,
aplikační část zbývá.

## Hotovo a nasazené

`sql/03-multilang.sql` je aplikovaná na DB `retrieval`. Přidala:

| změna | detail |
|---|---|
| `document.lang` | `text NOT NULL DEFAULT 'cs'`, CHECK `IN ('cs','en','de','la')` |
| `chunk.ts_config` | `regconfig NOT NULL DEFAULT 'czech'` (denormalizované — generovaný sloupec nesmí sahat do jiné tabulky) |
| `chunk.content_tsv` | přegenerováno na `to_tsvector(ts_config, content)`, GIN index znovu postaven |
| konfigurace `latin` | `COPY = simple` + `unaccent` (Debian pro latinu slovník nemá) |
| `hybrid_search` | nový parametr `p_ts_config regconfig DEFAULT 'czech'` |

Ověřené stemmování:

```
en  'databas':2 'faster':5 'run':4              (stopwords odfiltrovány)
de  'datenbank':2 'lauf':3 'schnell':4
cs  'index':4 'ladit':1 'latence':2 'vektorový':3
la  'amici':2 'amicorum':3 'amicus':1           (jen unaccent, bez stemmování)
```

Migrace je **zpětně kompatibilní** — defaulty `'cs'` a `'czech'`, takže služba
běžela dál bez úprav. Smoke test i `/search` po migraci prošly.

**PAST, na kterou jsem narazil:** `CREATE OR REPLACE FUNCTION` s jinou
signaturou funkci NENAHRADÍ, ale vytvoří druhý overload. Po migraci tam byly
dvě `hybrid_search` a volání s pěti argumenty by bylo nejednoznačné. Starou
9argumentovou verzi je nutné explicitně zahodit:

```sql
DROP FUNCTION retrieval.hybrid_search(halfvec,text,int,int,int,real,real,real,smallint);
```

Zahozeno, zbyla jedna. Kdyby se migrace pouštěla na čisté DB, tenhle krok
není potřeba — je jen pro instance, kde už stará verze existovala.

## Aplikační část — hotová a ověřená (2026-08-07)

Nasazeno a změřeno na čtyřech poznámkách, jedné v každém jazyce
(`/srv/brain/markdown/0{1,2,3,4}-*.md`). Reprodukovatelný test:
`scripts/09-multilang-test.py`.

### 1. Indexer jazyk nastavuje

`app/lang.py` (nový) drží mapu jazyk → regconfig, `app/chunker.py` umí
`split_frontmatter()`, `app/indexer.py` z toho skládá jazyk dokumentu.

Pořadí zdrojů, funkce `_resolve_lang()`:

| # | zdroj | kdy |
|---|---|---|
| 1 | `lang:` ve frontmatteru | autoritativní, nikdy se nepřehlasuje |
| 2 | uložený `document.lang` | jen RESUME (shodný hash = identický obsah) |
| 3 | detekce přes alias `cheap` | dokument bez `lang:`, stav NEW/CHANGED |
| 4 | `DEFAULT_LANG` | detekce vypnutá, selhala, nebo LiteLLM nedostupný |

Krok 2 je tam kvůli přerušenému prvnímu naplnění: bez něj by RESUME detekoval
znovu úplně všechno. Ověřeno — `indexed_at := NULL` na dokumentu bez
frontmatteru dá `lang_sources: {stored: 1, detected: 0}`.

`POST /reindex` vrací nově `languages` a `lang_sources`, `GET /stats` má
`documents_by_lang` a `chunks_by_ts_config`. Naměřeno na čtyřech poznámkách:

```
{"new":4, "chunks_embedded":4, "languages":{"cs":1,"en":1,"de":1,"la":1},
 "lang_sources":{"frontmatter":3,"stored":0,"detected":1,"default":0},
 "seconds":3.9}
```

**Frontmatter se odděluje PŘED chunkováním.** Bez toho by z YAML bloku vznikl
obyčejný odstavec a `lang: en` by skončilo v embeddingu i v `content_tsv` jako
obsah dokumentu. Není to plný YAML parser — čte jen skalární `key: value`,
protože jediné, co je potřeba, je `lang:`, a PyYAML by byl závislost navíc do
kontejneru s `MemoryMax=700M`.

**Změna jazyka opravdu nestojí embeddingy** — ověřeno, ne odvozeno. Úprava
samotného řádku `lang: cs` → `lang: en` v `01-cesky.md`:

```
{"changed":1, "unchanged":3, "chunks_embedded":0, "chunks_recycled":1,
 "seconds":0.0}
```

a přitom `ts_config` czech → english a `content_tsv` přegenerované
(`'běh':43` → `'běhu':43`). Hash se počítá nad syrovými bajty souboru, takže
změna frontmatteru je CHANGED, ale tělo je identické a recyklace podle obsahu
chunku převezme všechny vektory.

### 2. Dotazová strana konfiguraci předává

`SearchRequest` má `lang`, `db.hybrid_search()` předává
`p_ts_config => %s::regconfig`, odpověď nese `lang`, `lang_source` a
`ts_config`.

**Rozhodnutí, které bylo otevřené, padlo takto: jazyk doplní `cheap`.**
Přepis dotazu na klíčová slova stejně probíhá, takže model vrátí v témže
volání i kód jazyka — detekce tedy nestojí ani sekundu navíc a cachuje se
spolu s klíčovými slovy. Pořadí: explicitní `lang` v requestu → jazyk
z rewrite → `DEFAULT_LANG`. Neznámý **explicitní** kód je HTTP 422, ne tiché
sklouznutí na češtinu; kód z detekce se naopak jen zahodí.

`keywords.py` má stopword listy per jazyk (cs, en, de, la). Nikdy se
nesjednocují — německé `die` je člen, kdežto anglické `die` je sloveso.

Naměřeno, všechny čtyři jazyky trefují svůj dokument a cizí mlčí:

| dotaz | jazyk | klíčová slova | výsledek |
|---|---|---|---|
| Jak se ladí latence vektorových indexů? | cs (rewrite) | `ladit latence vektorový index` | `01-cesky.md` dense=3 lex=1 fuzzy=1 |
| What makes the databases run faster? | en (rewrite) | `database run faster` | `02-english.md` dense=1 lex=1 fuzzy=1 |
| Warum laufen die Datenbanken schneller? | de (rewrite) | `Datenbanken schneller` | `03-deutsch.md` dense=2 lex=1 fuzzy=1 |
| Ubi manet amicorum memoria? | la (rewrite) | `manet amicorum memoria` | `04-latina.md` dense=1 lex=1 fuzzy=1 |

U každého z nich mají ostatní tři dokumenty `r_lexical = NULL` a zároveň
`r_dense` ne-NULL — tedy přesně to dělení práce, které migrace zamýšlela.

### 3. Dvě pasti, na které se přišlo až měřením

**Normalizace tvarů se nesmí dělat mimo češtinu.** První znění promptu žádalo
1. pád jednotného čísla bez rozlišení jazyka. U češtiny je to správně, ale
snowball `faster` na `fast` neredukuje:

```
to_tsvector('english','databases running faster fast')
    -> 'databas':1 'fast':4 'faster':3 'run':2
```

Model tedy vrátil `database run fast`, vznikl dotaz
`'databas' & 'run' & 'fast'`, dokument obsahuje `'faster'` — a AND semantika
vrátila nulu. Lexikální větev tiše zmlkla, přestože dokument byl správný.
Prompt teď normalizuje **jen češtinu** a jinde nechává tvary z dotazu.

**Jedno funkční slovo navíc shodí celou větev.** Prompt sice říká „zahoď tázací
slova", ale model to nedodrží vždy — u latinského „Ubi manet amicorum
memoria?" vrátil `ubi manet amicorum memoria`. Slovo `ubi` v dokumentu není
a AND kvůli němu shodilo lexikální větev, ačkoliv zbylá tři slova seděla.
Výstup z LLM proto ještě prochází lokálním stopword filtrem — deterministickou
sítí, která tohle chytí. Filtruje se jen při známém jazyce a `extract()` nikdy
nevrátí prázdno, takže to nemůže uškodit.

### 4. `REWRITE_TIMEOUT` zvednut z 8 na 12 s

Změřeno na nasazeném systému: ustálené volání `cheap` trvá **1,1–4,8 s**, ale
**první po restartu 8,5 s** (navazuje se spojení na upstream). Osm sekund tedy
spolehlivě uříznulo právě ten cold start — po každém restartu první čtyři
dotazy spadly na lokální extrakci.

Zvýšení skoro nic nestojí, protože **timeout se platí taky**: při 8 s zaplatíš
8 s A dostaneš horší klíčová slova, kdežto počkat do 8,5 s dá lepší výsledek
za srovnatelnou cenu. Rozdíl se projeví až u skutečně zaseknutého volání.

LiteLLM si navíc identický request cachuje sám — opakované volání 0,005 s.

## Známé omezení

**Latina nemá stemmování.** Debian pro ni hunspell slovník nemá
(`hunspell-la`, `myspell-la`, `ispell-latin` neexistují) a snowball ji
nedodává. Konfigurace `latin` dělá jen lowercase + unaccent, takže lexikální
větev u latiny matchuje jen přesné tvary. U silně flektivního jazyka to je
citelné; nese to trigramová větev, které flexe vadí méně. Až se objeví
slovník, stačí přidat `CREATE TEXT SEARCH DICTIONARY` a přemapovat `latin`.

**Kvalita `bge-m3` na latině není ověřená.** Je to nízkozdrojový jazyk;
dense větev na něm může být slabší než na cs/en/de. Stojí za změření na
skutečných datech, ne za předpoklad. (Na čtyřech testovacích poznámkách dense
větev latinu trefila na 1. místo, ale čtyři dokumenty nic nedokazují.)

**Detekce jazyka stojí volání na dokument.** Týká se jen dokumentů bez `lang:`
a jen stavů NEW/CHANGED, takže ustálený inkrementální běh nedetekuje nic.
Při prvním naplnění velkého korpusu bez frontmatteru je to ale ~2–3 s na
dokument vedle embeddingů — tehdy se vyplatí `DETECT_LANG_ENABLED=0`, doindexovat
s češtinou a jazyk doplnit později frontmatterem (což, jak je změřeno výš,
nestojí žádné embeddingy).

**Jazyk se určuje z celého dokumentu, ne per sekci.** Poznámka, kde je český
komentář nad anglickým citátem, dostane jeden `ts_config` pro všechny chunky.
Schéma by per-chunk jazyk uneslo (`chunk.ts_config` je sloupec, ne odvozenina),
ale indexer ho takhle nepoužívá.
