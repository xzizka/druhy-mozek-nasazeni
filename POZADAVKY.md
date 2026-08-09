# Požadavky

Zadané, **zatím neschválené k provedení**. Analýza a schválení probíhá
u každého tématu zvlášť. Hotové věci se odsud stěhují do `NASAZENI.md`.

Stav: `ZAZNAMENÁNO` → `ANALYZOVÁNO` → `SCHVÁLENO` → `HOTOVO`.

---

## P1 — Rozpoznat halucinačně rizikové dotazy a odkázat na `/korpus`

**Stav: HOTOVO (2026-08-09)** — P1a i P1b nasazeno a ověřeno živě.

Rozhodnuto 2026-08-09:

- **P1a — obojí.** Fakta o korpusu se přikládají do kontextu každého dotazu
  *a zároveň* heuristika u agregačně vypadajících dotazů zobrazí odkaz
  na `/korpus`. Hotovo, viz níž.
- **P1b — spočítat automaticky, do `/korpus` připnout až na kliknutí.**
  Práci dělá stroj, kanonizaci člověk. Důvod: sémanticky špatná agregace
  zapsaná natvrdo by udělala z `/korpus` zdroj nesmyslů, a `/korpus` je
  přitom právě ta stránka, proti které se halucinace poměřuje.
- **Pořadí:** nejdřív P1a, změřit, teprve pak P1b.

Zadání, jak bylo formulováno:

> Rozpoznat dotazy, kdy by mohlo dojít k halucinacím, a odkázat na data
> na `/korpus`. V případě, že na to model sám narazí, provede správný
> výpočet / analýzu / research a doplní to automaticky do `/korpus`.

Jsou to dvě oddělitelné části:

**P1a — detekce a odklon.** Poznat, že dotaz je agregační nebo statistický
(„kolik", „shrň všechny", „který je nejčastější"), a místo do RAGu ho
poslat na data z `/korpus`.

**P1b — automatické doplnění.** Když se na takový dotaz odpověď v `/korpus`
zatím nenachází, systém si ji sám spočítá nad databází a **trvale ji
do `/korpus` přidá**, takže příště je hotová.

### Proč to vzniklo

Změřeno 2026-08-09. Dotaz „Udělej mi sumarizaci knih podle jazyka. Kolik je
kterých?" nejdřív spotřeboval celý strop `ANSWER_MAX_TOKENS` (3000 výstupních
tokenů) na uvažování a vrátil prázdný obsah. Po zvýšení stropu na 8000 model
odpověděl — a odpověděl **špatně**: napočítal knihy zmíněné uvnitř osmi
dodaných úryvků („66 anglických knih" = knihy Bible), místo dokumentů
v indexu. Skutečnost je cs 400 / en 301 / de 177 / la 101.

Je to přesně ten druh selhání, kvůli kterému `cheap` a `workhorse` nemají
fallback na `backstop`: věrohodně vypadající nesmysl je horší než přiznané
selhání, protože ho nikdo nezachytí.

### Co už pro to existuje

- `/korpus` v Krytonovi (dokumenty a chunky po jazycích, stav indexu).
- Role **`platform_ro`** s `SELECT` na celé schéma `retrieval`
  (`sql/01-bootstrap.sql`, `sql/02-retrieval.sql`) — hotová bezpečná
  identita pro generované analytické dotazy.
- Systémový prompt už modelu říká, ať přizná, když kontext neodpovídá.
  Nestačí to: model se k psaní vůbec nedostal, respektive si odpověď složil
  z úryvků.

### Jak to dopadlo

**P1a.** Do promptu každé odpovědi jde blok „Ověřená čísla o korpusu"
z `/stats` (~100 tokenů) a systémový prompt zakazuje dopočítávat počty
z úryvků. Navíc heuristika `core.je_agregacni()` zobrazí u agregačních
dotazů odkaz na `/korpus` — nic neodklání, jen upozorní.

Změřeno na dotazu, který předtím vyrobil nesmysl: místo „66 anglických knih
= knihy Bible" vrátí 979 / cs 400 / en 301 / de 177 / la 101, sám odliší
„dokument" od „knihy" a odmítne si domýšlet.

**P1b.** `kryton/app/analytics.py`. Agregační dotaz → model napíše SELECT →
spustí se nad `retrieval` pod rolí `platform_ro` → výsledek jde modelu jako
další ověřený podklad a uloží se jako **nepřipnutá** metrika. Do `/korpus`
ji připne až člověk kliknutím; vidí přitom otázku, SQL i výsledek.

Bezpečnost stojí na roli, ne na kontrole řetězce — `platform_ro` má jen
SELECT, ověřeno pokusem o zápis (`permission denied for table document`).
`check_sql()` je druhá vrstva: jediný SELECT/WITH, žádný středník, žádná
DDL. K tomu read-only transakce, `statement_timeout` a strop řádků vynucený
obalením do poddotazu.

Stárnutí: k metrice se ukládá otisk korpusu (počet dokumentů +
`max(updated_at)`) a `/korpus` ji při změně označí za zastaralou.

Ověřeno živě: model napsal
`SELECT lang AS jazyk, COUNT(*) AS pocet_dokumentu FROM retrieval.document
GROUP BY lang`, výsledek sedí na databázi, připnutí i zobrazení fungují.

### Co z toho stojí za zapamatování

`SET LOCAL statement_timeout = %s` prošlo všemi 89 kontrolami smoke testu
a na živém Postgresu spadlo na `syntax error at or near "$1"` — SET LOCAL
je utility příkaz a placeholder do něj nepatří. Smoke test databázi stubuje,
takže tuhle třídu chyb nikdy nechytí. Proto vznikl
**`scripts/14-analytics-check.sh`** — integrační kontrola proti skutečné
databázi. Pouštěj ji, když se sáhne na `analytics.py`.

---

## P2 — Nahrávání dokumentů, text do DB, originály na S3

**Stav: ETAPA 1 HOTOVA (2026-08-09)** — md a txt nasazeno a ověřeno živě.
Etapy 2–4 (PDF, DOCX, migrace) čekají.

Rozhodnuto 2026-08-09: text do `_uploads/` pod `MARKDOWN_ROOT` s řádkem
v `.gitignore`; zálohu vytaženého textu neřešíme, je obnovitelný z originálu;
nahrané dokumenty mají nižší důvěru než vlastní poznámky; jde se po etapách.

**Dvě věci vyšly jinak, než analýza předpokládala** — obojí se ukázalo až
při čtení kódu:

1. **Mapa na S3 je v tabulce `upload` v databázi Krytona, ne
   v `retrieval.document.meta`.** Do toho sloupce indexer nikdy nic nezapisuje
   a Kryton do databáze retrievalu psát nesmí (má tam jen SELECT přes
   `platform_ro`). Pro migraci je to navíc výhodnější: `UPDATE` řádků místo
   přepisu frontmatteru ve stovkách souborů a reindexu.
2. **`trust_level` nebyl per-dokument**, byla to jediná globální hodnota
   z configu. Doplněno čtení `trust:` z frontmatteru (`_resolve_trust`
   v indexeru) a `upsert_document` ho nově aktualizuje i při konfliktu —
   jinak by změna nikdy neprošla.

Ověřeno živě: soubor v cp1250 s diakritikou → kódování rozpoznáno,
`_uploads/zkouska-nahravani-<hash>.md` s `trust: 1`, originál na S3 pod
`originals/<sha256>.txt`, jazyk detekován `cs`, vyhledávání dokument našlo
první (rerank 0,852) a **`max_trust=0` ho správně vyřadilo**. `git check-ignore`
potvrdil, že soubor do repozitáře poznámek nejde. Po testu uklizeno.

Zadání, jak bylo formulováno:

> Systém by měl umožňovat i nahrání dokumentů a jejich zpracování
> (PDF, Word, Markdown, TXT soubor… jen textové dokumenty). Informace
> z těchto dokumentů uloží do databáze, ale samotné dokumenty nahraje na S3.

Tedy: nahrát soubor → vytáhnout z něj text → text zaindexovat do databáze
(chunky, embeddingy, fulltext) → **originál uložit na S3**, ne na disk brainu.

### Co už pro to existuje a co ne

- Indexer prochází **výhradně `*.md`** (`root.rglob("*.md")` v `indexer.py`)
  pod `/srv/brain/markdown`. Jiné formáty pipeline dnes nezná.
- `retrieval.document` má sloupec **`meta jsonb`** — přirozené místo pro
  klíč objektu na S3, původní název, MIME typ a hash originálu.
- `document.source_path` je `NOT NULL UNIQUE` a dnes to je cesta v markdown
  stromu; u nahraných souborů bude potřeba rozhodnout, co tam patří.
- `trust_level` (0–2, CHECK) se propisuje do vyhledávání přes `max_trust` —
  nahrané cizí dokumenty možná nemají mít stejnou důvěru jako vlastní poznámky.
- **S3 v projektu zatím není nikde** — žádný klient, žádný secret, žádná
  konfigurace.
- `/srv/brain/markdown` je git repo, které se každých 15 minut samo pushuje
  na GitHub. Cokoliv, co tam spadne, jde do veřejné cesty toho repozitáře.

### Úložiště: Backblaze B2 (rozhodnuto 2026-08-09)

| | |
|---|---|
| endpoint | `https://s3.eu-central-003.backblazeb2.com` |
| bucket | `second-brain-kryton` |
| region | `eu-central-003` (u B2 je součástí hostitele) |

**Přístup ověřen 2026-08-09** proti živému bucketu, ne odhadnut: zápis,
čtení zpět s ověřením obsahu, `head_object`, výpis i smazání prošly.
Uživatelská metadata (`x-amz-meta-*`) se zachovávají — to je místo pro
původní název souboru a hash. Testovací objekt smazán, bucket je prázdný.

`ListBuckets` vrací `AccessDenied: not entitled`, což je **v pořádku a záměr**:
klíč je omezený na jeden bucket. Kód se tedy nesmí spoléhat na výpis bucketů
ani na `head_bucket` jako test dostupnosti.

Drobnost, která umí zmást při porovnávání: B2 normalizuje `Content-Type`
a z `text/plain; charset=utf-8` udělá `text/plain;charset=utf-8`.

**Výměna endpointu a migrace jsou požadavek, ne možnost.** Z toho plyne:
konfigurace musí být čistě S3-kompatibilní (endpoint, region, bucket, klíče
jako proměnné), nikde žádná zabudovaná znalost Backblaze, a v databázi
u dokumentu musí být uložený i **profil úložiště**, ne jen klíč objektu —
jinak po migraci nepůjde poznat, kde který originál leží.

### Analýza (2026-08-09) — k odsouhlasení před implementací

#### Kam s vytaženým textem

| | co to je | dopad |
|---|---|---|
| A | zapsat `.md` přímo do `MARKDOWN_ROOT` | text z PDF se každých 15 min pushne na GitHub, repo poznámek nabobtná |
| **B** | **`_uploads/` pod `MARKDOWN_ROOT` + řádek v `.gitignore`** | **nulová změna pipeline, text zůstane na brainu** |
| C | vlastní kořen nebo jen DB, mimo markdown model | největší zásah, přijdeme o inkrementální reindex podle hashe zadarmo |

**Doporučuji B a není to odhad — je na to precedens přímo v repu.**
`.gitignore` poznámek už dnes obsahuje `_scale/`, tedy 975 dokumentů
zátěžového korpusu leží pod `MARKDOWN_ROOT`, řádně se indexují a na GitHub
nejdou. `_scan()` v `indexer.py` prochází `root.rglob("*.md")` a `source_path`
skládá jako cestu relativní ke kořeni, takže podadresář vezme sám od sebe.

**Důsledek, který je potřeba vyslovit nahlas:** co je v `.gitignore`, to
`brain-markdown-sync` nezálohuje. Originály budou na S3, ale *vytažený text*
by existoval jen na brainu. Buď to vědomě přijmeme (text je z originálu
kdykoliv obnovitelný), nebo mu dáme vlastní zálohu.

#### Extrakce textu

Kryton má RSS 79 MB při `MemoryMax=800M`, tedy ~720 MB rezervy; na stroji
je volných 12 GB RAM a 36 GB disku. Místo na to je.

- **MD, TXT** — bez nové závislosti. Pozor na kódování: český `.txt` bývá
  cp1250, ne UTF-8, a špatně odhadnuté kódování se do indexu propíše tiše.
- **PDF** — `pypdf`, čistě pythonní, čte po stránkách, takže paměť roste
  s jednou stránkou, ne s celým souborem.
- **DOCX** — je to ZIP s XML. Dá se rozebrat přes `zipfile` + `xml.etree`
  ze standardní knihovny a ušetřit `python-docx` i jeho `lxml`.

**PDF bez textové vrstvy (sken) musí skončit srozumitelnou hláškou.** OCR je
mimo zadání („jen textové dokumenty"), ale uživatel takový soubor nahraje —
poznat to jde podle nepoměru mezi počtem stránek a množstvím vytěženého textu.

#### Originál na S3

- Klíč **odvozený z obsahu**: `originals/<sha256>` — dvojí nahrání téhož
  souboru je tím zadarmo a bez duplicit.
- Do `x-amz-meta-*` původní název a hash; ověřeno, že B2 metadata drží.
- Vazba v `retrieval.document.meta` (jsonb, existuje):
  `{"storage": {"profile", "bucket", "key", "sha256", "size", "mime",
  "original_name"}}`.

#### Výměna endpointu a migrace

Požadavek, ne možnost, takže hned od začátku:

- konfigurace jako **profily úložiště** (název → endpoint, region, bucket,
  klíče), aktivní profil se vybírá proměnnou;
- `meta.storage.profile` u každého dokumentu, jinak po migraci nepůjde
  poznat, kde který originál leží;
- migrační skript: kopie profil A → B, ověření podle hashe, pak teprve úklid v A;
- **dostupnost se testuje `list_objects_v2` nad bucketem, ne `ListBuckets`
  ani `head_bucket`** — klíč je omezený na jeden bucket a ty operace nesmí.

#### Rizika, která chci pojmenovat předem

1. **Mazání.** Klíč odvozený z obsahu znamená, že jeden objekt může patřit
   víc dokumentům. Mazat originál při smazání dokumentu jde jen s počítáním
   odkazů. Návrh: zpočátku ze S3 nemazat vůbec, originály jsou archiv.
2. **Kódování TXT** — tichá chyba, viz výš.
3. **Zip bomba v DOCX** a PDF s desetitisíci stránkami → tvrdé stropy na
   velikost souboru i počet stránek.
4. **Název souboru od uživatele** → stejná ochrana cest jako `core.safe_path`.
5. `_uploads/` není v gitu, tedy ani v záloze.

#### Návrh etap

| etapa | obsah | proč takhle |
|---|---|---|
| 1 | MD + TXT, S3, `meta.storage`, UI pro nahrání | projde celá cesta bez jediné nové závislosti |
| 2 | PDF přes `pypdf` | přidá závislost, ale cesta je už ověřená |
| 3 | DOCX přes `zipfile` + `xml.etree` | bez `lxml` |
| 4 | migrační skript mezi profily | až bude co migrovat |

### Otevřené otázky k rozhodnutí

1. ~~Jaké S3?~~ — Backblaze B2, ověřeno.
2. ~~Kam s vytaženým textem?~~ — návrh B, k odsouhlasení.
3. ~~Kdo dělá extrakci?~~ — Kryton, rezerva paměti stačí.
4. **Mazat originál na S3 při smazání dokumentu?** Návrh: ne, jen odpojit.
5. **Zálohovat vytažený text**, když `_uploads/` bude mimo git?
6. **Stropy** na velikost souboru a počet stránek?
7. **`trust_level` nahraných dokumentů** — stejná důvěra jako vlastní
   poznámky, nebo nižší? Propisuje se do hledání přes `max_trust`.
8. **Etapy** — jít po etapách 1–4, nebo rovnou všechny formáty naráz?
