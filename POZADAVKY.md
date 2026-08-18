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

**Stav: ETAPY 1–3 HOTOVY (etapa 1: 2026-08-09, etapy 2+3: 2026-08-11)** —
md, txt, PDF i DOCX nasazeno a ověřeno živě. Etapa 4 (migrace mezi S3
profily) čeká.

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

Ověřeno živě (etapa 1): soubor v cp1250 s diakritikou → kódování rozpoznáno,
`_uploads/zkouska-nahravani-<hash>.md` s `trust: 1`, originál na S3 pod
`originals/<sha256>.txt`, jazyk detekován `cs`, vyhledávání dokument našlo
první (rerank 0,852) a **`max_trust=0` ho správně vyřadilo**. `git check-ignore`
potvrdil, že soubor do repozitáře poznámek nejde. Po testu uklizeno.

**Ověřeno živě (etapy 2+3, 2026-08-11)**, proti skutečnému Postgresu, S3
a retrieval pipeline (voláno přímo přes `app.ingest.uloz()` v běžícím
kontejneru, ne přes web — přihlašovací heslo do Krytona nebylo potřeba znát
ani nikam posílat). PDF o dvou stránkách i DOCX o dvou odstavcích:
- extrakce vytáhla text správně (obě stránky PDF, oba odstavce DOCX
  s diakritikou), `trust: 1` ve frontmatteru u obou;
- originály na S3 pod `originals/<sha256>.pdf` a `.docx`;
- reindex zaindexoval oba jako `retrieval.document` s `trust_level=1`,
  jazyk `cs`;
- **druhé `POST /reindex` spuštěné 2 s po prvním dostalo `409 Conflict`**
  (reindex nedovolí souběh) — DOCX se zaindexoval, až se reindex spustil
  znovu. Při dvou uploadech rychle po sobě tedy druhý soubor počká na další
  reindex, ne na ten vyvolaný vlastním uploadem. Stejné riziko platí
  i pro etapu 1 (md/txt), není to nové u PDF/DOCX — jen se to poprvé
  projevilo, protože živý test nahrával dva soubory těsně za sebou;
- vyhledávání „Zivy test DOCX odstavec" vrátilo DOCX dokument první
  (rerank 0,874), PDF druhý (rerank 0,354), oba se správným `trust_level`;
- úklid: smazán řádek v `upload`, soubor z `_uploads/`, reindex spuštěn —
  `retrieval.document` i `upload` čisté, počet dokumentů zpět na 5.
  **S3 originály záměrně nesmazány** (politika z P3: nikdy neodstraňovat).

Nasazeno: `git push` (přes `ssh.github.com:443` — port 22 na GitHub je
z pracovní stanice blokovaný, funguje ale GitHubův obchvat přes port 443
se stejným klíčem), `git pull` na brainu, `podman build --network=host`,
`systemctl restart kryton`. `pypdf==5.9.0` přibyl do `requirements.txt`,
žádná nová závislost pro DOCX (jen stdlib `zipfile`/`xml.etree`).

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
4. ~~Mazat originál na S3 při smazání dokumentu?~~ — ne, viz P3.
5. ~~**Zálohovat vytažený text**, když `_uploads/` bude mimo git?~~ —
   **Rozhodnuto 2026-08-11: ne**, stejná logika jako u `_scale/` — derivovaná
   data se do zálohy nedávají, jsou levně obnovitelná reindexem z originálu.
6. ~~**Stropy** na velikost souboru a počet stránek?~~ — **Rozhodnuto
   2026-08-11: `UPLOAD_MAX_BYTES` 50 MB (beze změny z etapy 1),
   `UPLOAD_MAX_PAGES` 500 stránek u PDF.** DOCX nemá v XML pojem „stránka"
   bez plného vyrenderování, tam řeší zip bombu samostatný pevný strop na
   rozbalenou velikost (200 MB), kontrolovaný průběžně při čtení, ne podle
   (podvržitelných) metadat centrální adresáře zipu.
7. ~~**`trust_level` nahraných dokumentů**~~ — **Rozhodnuto 2026-08-11:
   `trust:1`, stejně jako etapa 1** (konzistentní „importované" napříč
   formáty).
8. **Etapy** — jde se 2+3 najednou (PDF i DOCX ve stejném kroku), etapa 4
   (migrace) čeká zvlášť, až bude co migrovat.

---

## P3 — Mazání nahraných dokumentů

**Stav: HOTOVO (2026-08-10).**

Zadání: co s dokumentem, který se nahraje a později se zjistí, že údaje
z něj už nejsou relevantní? Do dneška to nešlo řešit jinak než ručně přes
SSH — `/nahrat` dokumenty jen vypisoval, mazání nikde nebylo (na rozdíl
od konverzací a metrik, které svoje „Smazat" už měly).

Tři možnosti k odsouhlasení byly: (A) měkké vyřazení příznakem, beze
smazání; (B) tvrdé smazání textu, indexu a záznamu, S3 originál trvale
zachovat jako archiv; (C) totéž jako B, ale včetně S3 s počítáním odkazů.

**Rozhodnuto: B.** Odpovídá tomu, co „už není relevantní" znamená — pryč
z hledání, pryč ze seznamu — a nesahá na nevyřešený problém sdílených S3
objektů (klíč je odvozený z obsahu, `originals/<sha256>`, takže jeden
objekt může patřit víc řádkům `upload`; bezpečné mazání by vyžadovalo
počítání odkazů). Je to i konzistentní s tím, jak se u vás už mažou
konverzace — tvrdý `DELETE`, žádný příznak.

Nová route `POST /nahrat/smazat` v Krytonovi: smaže řádek v tabulce
`upload` (`db.delete_upload`, vrátí `source_path`), pak soubor
z `_uploads/` (`core.safe_path` — stejná ochrana cest jako při zápisu),
pak spustí reindex (`core.trigger_reindex`, fire-and-forget jako
u `/zachytit`). **S3 originál se nedotýká — odnikud, nikdy.** Tlačítko
„Smazat" u každého řádku v `/nahrat` s potvrzovacím dialogem, který
tohle přímo říká.

Pořadí kroků je vědomé: smazat řádek první (atomicky vrátí cestu), pak
soubor, pak reindex. Selhání posledních dvou nejhůř ponechá soubor na
disku o krok déle — horší by bylo smazat soubor a zůstat s osiřelým
řádkem, který už nejde dohledat.

`scripts/13-smoke-kryton.py` rozšířen o 8 kontrol (přihlášení vyžadováno,
řádek i soubor zmizí, S3 originál zůstává, neexistující id nespadne).
Cestou se našla a opravila latentní chyba ve stubu `_add_upload`: ID se
počítalo z `len(_uploads) + 1` i po odstranění duplicity při UPSERTu,
takže dva různé nahrané soubory mohly dostat stejné ID. Opraveno na
stabilní ID přes UPSERT (stejný `source_path` = stejné ID) s monotónním
čítačem pro nové řádky — věrněji odpovídá reálnému
`ON CONFLICT ... RETURNING id` v Postgresu.

---

## P4 — Fabrikované citace u přímých faktografických dotazů s nízkým rerank skóre

**Stav: ZAZNAMENÁNO (2026-08-11).** Časem k řešení, teď jen zapsáno.

Zadání, jak vzniklo: nalezeno při kontrole konverzace
`d0412179-974b-4543-ae58-1394c0729c56` (`/konverzace/...` v Krytonovi),
ne zadáno dopředu jako P1–P3.

### Co se stalo

Zpráva 45: „Které z těchto zákonů vznikly před rokem 2019?" (navazuje na
zprávu 44, výčet paragrafů z nahraného dokumentu
`_uploads/broumy-zastupitelstvo-zadost-6c0932c1.md`).

Zpráva 46 odpověděla: *„Z úryvků [1] lze přímo určit rok vzniku..."*
a vyjmenovala tři zákony s roky (2015, 2000, 2000), se závěrem, že všechny
vznikly před rokem 2019. **Roky vyšly fakticky správně** — `č. NNN/RRRR
Sb.` je přímo rok vyhlášení ve Sbírce zákonů, a to model netrefil náhodou,
zjevně to zná ze svých obecných znalostí.

**Problém není ve výsledku, ale v citaci.** Všechny čtyři dotažené úryvky
u zprávy 46 měly rerank skóre kolem nuly (`[1]` 0,00115, zbylé tři pod
0,00002 — pro srovnání zpráva 44 měla `[1]` na 0,977). Žádný z nich
neobsahuje rok vzniku žádného zákona. Model si citaci vymyslel — fakt
odvodil ze svých vlastních znalostí sbírkové notace, ne z dodaného
kontextu, a přesto tvrdil, že to „lze přímo určit z úryvků".

### Proč je to jiná díra než P1

`core.je_agregacni()` na tenhle dotaz nereaguje — „Které z těchto zákonů
vznikly před rokem 2019?" neobsahuje žádné z jejích spouštěcích slov
(kolik, součet, nejvíc, průměr…). P1 řeší agregace nad celým korpusem;
tohle je fabrikace citace u přímého faktografického dotazu nad už
citovanými entitami z předchozí zprávy — dotaz, který vůbec nevypadá
rizikově.

### Proč je to nebezpečné i když tahle odpověď škodu nenadělala

Formát fabrikované odpovědi je nerozeznatelný od odpovědi s pravdivou
citací. Model měl štěstí, že jeho obecné znalosti o české sbírkové notaci
byly spolehlivé. U méně známého zákona, jiné země/systému, nebo jakéhokoli
faktu, který model jen “tuší”, by stejný mechanismus vyrobil stejně
sebejistou větu se špatným rokem a falešnou citací — a nikdo by to
nepoznal bez ručního ověření rerank skóre, jako teď.

### Co by stálo za analýzu (až na to dojde)

Nezkoumáno, jen náměty:

- Práh na rerank skóre citace, pod kterým se buď odpověď odmítne, nebo se
  aspoň připojí varování „nízká jistota zdroje".
- Rozšíření `je_agregacni()`-stylové heuristiky o detekci „dotaz na fakt,
  který dodaný kontext explicitně neobsahuje" — obtížnější než klíčová
  slova, možná vyžaduje druhé volání modelu nebo kontrolu překryvu.
- Systémový prompt už dnes zakazuje dopočítávat z úryvků (viz P1) — tohle
  ukazuje, že to samo nestačí, model si přesto citaci připsal.

Souvisí: [[kryton-nasazen]] (66 anglických knih = knihy Bible, stejná
třída selhání), P1 výše.

---

## P5 — MCP server (`/mcp`): core.search/core.answer a core.capture pro OpenWork a další agenty

**Stav: IMPLEMENTOVÁNO A SMOKE-TESTOVÁNO LOKÁLNĚ (2026-08-12).** Čeká na
vygenerování secretu a nasazení — pak na nastavení v samotném OpenWorku
(to už je krok mimo tenhle repozitář).

### Odkud to vzniklo

Uživatel se zeptal, jestli lze propojit vlastní [OpenWork](https://github.com/different-ai/openwork)
(desktopová appka sjednocující MCP capabilities napříč AI nástroji — Claude
Code, Cursor, ChatGPT, Codex) s Druhým mozkem. OpenWork umí registrovat
libovolný vlastní MCP server (Settings → Extensions → Add Custom App →
jméno, URL, případně autentizační hlavička) a zpřístupní ho pak jednotně
všem připojeným nástrojům přes `search_capabilities`/`execute_capability`.

### Rozhodnutí

1. **Vrací hotovou odpověď z `core.answer()`, ne syrové úryvky.** Pro
   hlubší zkoumání zdrojů slouží Kryton samotný; tenhle nástroj je pro
   rychlý dotaz odjinud.
2. **Čtení i zápis** — kromě `core.search()`+`core.answer()` i
   `core.capture()`, aby šlo zachytit poznámku přímo z Claude Code/Cursor/ChatGPT.
3. **Autentizace sdíleným tokenem v `Authorization: Bearer` hlavičce**,
   ne bez autentizace (jako Infinity) a ne plný OAuth server. Ověřeno
   přímo v OpenWorku, že appka takovou hlavičku při vypnutém „OAuth
   requirement" pošle — bez toho by `TokenVerifier` byl k ničemu.

### Implementace

`kryton/app/mcp_server.py` — `FastMCP` instance se dvěma nástroji
(`hledat`, `zachytit`) a vlastním `TokenVerifier` (`_SdilenyToken`,
porovnání `hmac.compare_digest`, ne `==`). Bez nastaveného
`MCP_BEARER_TOKEN` je endpoint zavřený pro každého, ne otevřený.
Připojeno do `kryton/app/main.py` přes `app.mount("/mcp", ...)`,
`_startup`/`_shutdown` (`@app.on_event`) nahrazeny jedním `lifespan`,
protože MCP appka nese vlastní lifespan (spouští session manager) a oboje
musí běžet společně.

### Pasti, na které stojí za to pamatovat

1. **`mcp.server.fastmcp` (z balíčku `mcp`) je dnes legacy.** Aktivně se
   vyvíjí samostatný balíček `fastmcp` (jlowin), teď na v3.x/v4 beta —
   ekosystém k němu přešel, oficiální SDK ho drží jen pro zpětnou
   kompatibilitu.
2. **`fastmcp==3.4.7` (i beta 4.0.0b2) má rozbitou závislost** —
   `fastmcp-slim` vyžaduje `starlette>=1.0.1`, což zatím nemá žádné
   vydání, co by to splnilo. Nejde nainstalovat vedle `fastapi==0.118.0`
   (ani samostatně, pip prostě nemá co nabídnout). Použito `fastmcp==2.14.7`
   — poslední zralá řada, bez konfliktu, ale s podstatně větším stromem
   závislostí (redis, cryptography, keyring, typer…).
3. **`fastmcp` 2.x nemá `combine_lifespans`** (ta je jen ve 3.x) — lifespan
   MCP appky (`_mcp_app.lifespan(_mcp_app)`) se musí vnořit do vlastního
   `lifespan` ručně přes `async with`.
4. **`http_app()` defaultně registruje vlastní routu na `/mcp`.** Po
   připojení přes `app.mount("/mcp", mcp_app)` by výsledná cesta byla
   `/mcp/mcp`, ne `/mcp`. Oprava: `http_app(path="/")`.
5. **`fastmcp.Client` s `FastMCP` instancí napřímo (in-process transport)
   autentizaci vůbec neřeší** — `ValueError: This transport does not
   support auth`. Ověřit auth jde jen přes skutečné HTTP (i lokálně, přes
   `uvicorn.Server` v threadu).

### Ověřeno

Smoke test rozšířen o `_SdilenyToken.verify_token()` (správný token,
špatný token, chybějící secret), přímé volání `hledat.fn()`/`zachytit.fn()`
nad stejnými stuby jako zbytek testu, a **skutečný HTTP test** — reálný
`uvicorn` server nad `main.app`, MCP klient přes něj zavolá nástroj se
správným tokenem (uspěje), špatným (zamítnuto) a bez tokenu (zamítnuto).
Všech 164 kontrol prochází v `python:3.13-slim`.

RSS po importu `main.py` s `fastmcp` zabudovaným: **~105 MB** — v pohodě
vůči `MemoryMax=800M`, nezvyšováno preventivně.

### Co zbývá

- Vygenerovat `mcp_bearer_token` (`scripts/02b-secrets-extra.sh`,
  idempotentní, vypíše hodnotu jednou — na rozdíl od
  `backup_encryption_key` jde volně rotovat, ztráta není nenávratná).
- Nasadit (rebuild image, restart `kryton.service`).
- V OpenWorku: Settings → Extensions → Add Custom App, URL na `/mcp`,
  hlavička `Authorization: Bearer <token>`.

---

## P6 — Fallback řetěz `reasoning` může spadnout celý najednou

**Stav: ZAZNAMENÁNO (2026-08-12).** Nalezeno vyšetřením reálného selhání
v Telegramu, ne zadáno dopředu. Časem k řešení.

### Co se stalo

Dotaz přes Telegram 2026-08-12 v 18:30:05 UTC (20:30 tvého času) selhal
s LiteLLM 429. Log (`journalctl -u litellm`) ukázal celý sled:

1. **`reasoning`** (`openai/big-pickle` přes OpenCode Zen) selhal na
   **svém vlastním** rate limitu ("Error from provider (Console): Rate
   limit exceeded").
2. Fallback `reasoning → workhorse`. **`workhorse`** (gemma přes
   OpenRouter) dostal 429 s `"limit_source":"upstream_provider_shared_pool"`
   — to je sdílený fond zdarma u Google AI Studio přes **celý OpenRouter**,
   ne účtový denní strop 50/den (ten je jiná věc, viz [[cheap-alias-denni-limit]]).
3. Fallback `workhorse → backstop`. **`backstop`** (`gpt-oss-20b:free`,
   taky OpenRouter, jiný poskytovatel „Darkbloom") dostal **stejný typ 429**,
   `retry_after_seconds: 3`.
4. `backstop` už další fallback nemá (návrhem — viz komentář u `fallbacks`
   v `litellm-config.yaml`), takže chyba se vrátila až do Krytona a
   Telegram dostal selhání.

**Tři nezávislí poskytovatelé byli zahlcení ve stejnou chvíli** — smůla,
ne systémová chyba, a **nesouvisí s testováním n8n** (`upstream_provider_shared_pool`
je sdílený přes celý OpenRouter, pár volání z jednoho účtu na něj nemá
měřitelný dopad).

### Co z toho plyne

Fallback řetěz `reasoning → workhorse → backstop` je odolný proti výpadku
**jednoho** poskytovatele, ne proti korelovanému zahlcení víc poskytovatelů
najednou — a nic v dnešní konfiguraci proti tomu nechrání.

**Nekonzistence v `num_retries`:** `reasoning` a `workhorse` ho mají
explicitně na `2`, `backstop` a `cheap-fallback` ne — jedou na defaultu
LiteLLM, ne na promyšlené hodnotě. Nikde v `router_settings`/`litellm_settings`
není žádný globální default.

### `num_retries` — rozhodnuto 2026-08-12

**Zvýšeno na `3` u `workhorse` i `backstop`** (`conf/litellm-config.yaml`).
`backstop` předtím neměl `num_retries` vůbec — jel na defaultu LiteLLM,
teď explicitně `3`, shodně s `workhorse`. `retry_after_seconds: 3`
z incidentu naznačuje, že třetí pokus s odstupem pár sekund by tohle
konkrétní selhání pravděpodobně přečkal.

Vědomý tradeoff: delší čekání na chybu/odpověď u interaktivního Telegramu.
`cheap-fallback` zůstal beze změny (nesouvisel s incidentem, jiná role).

**Co tím pořád není řešeno:** žádný počet opakování neochrání proti tomu,
že spadnou **všechny** modely v řetězu zároveň o něco déle, než opakování
stihnou přečkat — to by řešilo jen doplnění dalšího, nezávislého
poskytovatele do `backstop`ova fallbacku, což dnešní návrh záměrně nemá
(řetěz `reasoning` u `backstop` končí).

Souvisí: [[cheap-alias-denni-limit]].

---

## P7 — Temporální dotazy („včera", „poslední záznamy") dostanou špatnou odpověď

**Stav: ČÁST A OPRAVENA (2026-08-17), ČÁST B ZAZNAMENÁNA.** Nalezeno
vyšetřením špatných odpovědí na hlasovky přes Telegram, ne zadáno dopředu.

### Co se stalo

Dotazy přes Telegram 2026-08-17 dostaly dvě věcně špatné odpovědi:
Kryton tvrdil, že **včera bylo 15. 8. 2026** (správně 16. 8.) a že
**poslední záznamy jsou z 13. 8. a 16. 8.**, přestože deník se ukládá denně.

### Co NENÍ chyba: deník i index jsou v pořádku

Ověřeno, protože podezření mířilo na ukládání:

- na disku `denik/2026-08-10.md` … `2026-08-16.md`, **všech 7 dnů**
- v `retrieval.document` **taky všech 7** včetně `denik/2026-08-16.md`
  (`indexed_at 2026-08-16 19:46`)
- hodiny na brainu i v kontejneru správné a synchronizované (UTC)

Chyba je výhradně v **odpovídání**, ne v zápisu ani indexaci.

### Chyba A — v promptu nebylo dnešní datum (OPRAVENO)

`core.answer()` skládal prompt jako SYSTEM + historie + `corpus_facts()`
+ extra + úryvky + otázka. **Datum nikde.** `date.today()` bylo v `core.py`
použité jen na jména souborů při zápisu. Model proto „včera" neměl z čeho
spočítat a odhadoval ho z datumů, která viděl v cestách dodaných úryvků
(08-10, 08-13, 08-12) — vyšlo 15. 8.

### Chyba B — řazení podle relevance, ne chronologicky (ZAZNAMENÁNO)

Temporální dotaz je dotaz na **řazení metadat**, což vektorové ani lexikální
hledání strukturálně neumí. Retrieval vrací top-N podle relevance. Změřeno:

| dotaz | co se vrátilo (v tomto pořadí) |
|---|---|
| Co jsem dělal včera? | `denik/2026-08-13`, `08-10`, `_uploads/broumy…`, `08-12` |
| Jaké jsou poslední záznamy v deníku? | `_uploads/broumy-zastupitelstvo`, `01-cesky.md`, `03-deutsch.md`, `08-14` |
| Jaké jsou nejnovější poznámky? | `_uploads/broumy…`, `08-16`, `08-15` |

U dotazu na „včera" se **16. 8. do výsledků vůbec nedostalo**. Model pak
čte datumy z cest úryvků a nejvýš postavené vydá za „poslední" — odtud ta
konkrétní dvojice 13. 8. / 16. 8.

**Rerank skóre je u všech těchto dotazů skoro nula** (0,0032 / 0,0013 /
2,1e-05) — stejný podpis jako P4: dotaz nemá v textu deníku lexikální ani
sémantickou kotvu, retrieval vrací šum a model nad šumem odpoví sebejistě.

**`je_agregacni()` to nezachytí** — ověřeno `False` pro všechny temporální
varianty (`True` jen pro „Kolik mám dokumentů?"). `AGREGACNI_SLOVA` obsahuje
kolik/pocet/nejvic/serad…, ale nic časového (posledni, nejnovejsi, vcera,
dnes), takže nepadne ani odkaz na `/korpus`.

**Proč je to vlastní třída, ne duplikát P1 ani P4:** neodpovědtelné z RAGu
jako P1, sebejisté při nulovém skóre jako P4, ale navíc si model datumy
nebere ze svých znalostí — **čte je z cest souborů v úryvcích**, takže
odpověď působí ocitovaně podložená, i když je to jen top-N relevance.

**Náměty k budoucí analýze (nezkoumáno):** rozšířit `AGREGACNI_SLOVA`
o časová slova; metadata cesta po vzoru P1b (SQL `ORDER BY` nad
`retrieval.document` pod rolí `platform_ro`); práh na rerank skóre.
Pozor: diakritika mění řazení — „delal vcera" a „dělal včera" daly jiné #1.

### Co se ukázalo TEPRVE po nasazení opravy A

Obojí ověřeno živě po nasazení `b59d9bf`, obojí **ponecháno k samostatnému
rozhodnutí** (2026-08-17), nic z toho se neopravuje teď.

**1) Chyba B se překlopila z falešně pozitivní na falešně negativní.**
Odpověď na „Co jsem dělal včera?" je teď:

> Včera (16. 08. 2026) v poznámkách nejsou žádné záznamy.

Datum už sedí, ale tvrzení je **nepravdivé** — `denik/2026-08-16.md` existuje
a je zaindexovaný, jen ho retrieval na tenhle dotaz nevrátí (viz tabulka
výš). Je to poctivější chování než dřív (model už netvrdí nic o obsahu,
který v kontextu nemá), ale pro uživatele pořád špatná odpověď — a v tomhle
tvaru možná zrádnější, protože „nemám žádné záznamy" zní důvěryhodně.
**Oprava A tedy chybu B neodstranila, jen jí změnila tvar.**

**2) Blok s datem si model ocitoval jako `[0]`** — „Dnešní datum je pondělí
17. srpna 2026 **[0]**." Chunky se číslují od `[1]` a datum žádný chunk
není. Je to **regrese způsobená opravou A**: systémový prompt vyžaduje
citaci za každým tvrzením, tak si model pro nový blok vymyslel číslo.
Stejná kategorie kosmetického průsaku jako kdysi „Podle FAKTA O KORPUSU…"
u P1a. Řešení by byla jedna věta v `dnesni_datum()` („tenhle údaj necituj"),
neprovedeno.

**Mimochodem:** odpovídal `workhorse` (gemma), ne `reasoning` — tedy
`reasoning` propadl fallbackem, viz P6.

---

## P9 — `cheap` nemá `num_retries` a propadá na 7× dražší model

**Stav: ZAZNAMENÁNO (2026-08-18).** Nalezeno při smoke testu po nasazení
kroků 2 a 3, ne zadáno dopředu. Časem k řešení.

### Co se stalo

Kontrolní volání aliasu `cheap` přes LiteLLM vrátilo v poli `model`
hodnotu `google/gemma-4-26b-a4b-it` místo `cheap` — tedy propad na
`cheap-fallback` (viz mechanismus rozpoznávání propadu z opravy (f)).

Zopakováno s `"fallbacks": []`, aby se chyba neschovala: **`cheap`
uspěl**, ling funguje. Přímé volání `inclusionai/ling-2.6-flash` na
OpenRouteru pak ukázalo příčinu:

    429 "inclusionai/ling-2.6-flash is temporarily rate-limited upstream"
    provider_name: Novita
    limit_source: upstream_provider_shared_pool

Je to **tentýž `upstream_provider_shared_pool` jako u P6**, jen na jiném
modelu. Fallback zafungoval správně a uživatel by nic nepoznal.

### Proč to stojí za řešení

1. **`cheap` nemá `num_retries` vůbec** — jede na defaultu LiteLLM,
   zatímco `workhorse` i `backstop` mají po P6 nastavené `3`. Je to
   nekonzistence, která vznikla tím, že se `cheap` řešil dřív (2026-08-09)
   než P6 (2026-08-12).
2. **Propad stojí 7× víc.** `ling-2.6-flash` je 0,010/0,030 $/M,
   `cheap-fallback` (placená gemma) 0,070/0,340 — tedy sedminásobek na
   vstupu a jedenáctinásobek na výstupu. A podle měření z 2026-08-09 je
   gemma i pomalejší (medián 1,44 s proti 0,88 s u linga).
3. **OpenRouter u toho 429 sám radí „retry shortly"**, takže jeden
   opakovaný pokus by pravděpodobně prošel.

### Co pomoct NEMŮŽE

Trik s `extra_body.provider`, který 2026-08-18 vyřešil rozptyl latence
u `reasoning`, tady **nefunguje**: `ling-2.6-flash` má na OpenRouteru
**jediného poskytovatele, Novitu**, takže není kam přesměrovat.

Za pozornost stojí, že `uptime_last_1d` u Novity hlásil **100 %**,
a přesto 429 přišel — **uptime v katalogu OpenRouteru rate limity
nezachycuje** a nelze se na něj při výběru poskytovatele spoléhat.

### Návrh k rozhodnutí

`num_retries: 2` u aliasu `cheap`. Rozpočet na to je: `REWRITE_TIMEOUT`
je 12 s a ustálené volání `cheap` trvá 1,1–4,8 s, takže jeden opakovaný
pokus se vejde, tři ne.

**Rozhodnutí odloženo**, protože jde o horkou cestu **každého hledání**
a jeden výskyt ve třech voláních může být souběh. Před změnou změřit,
jak často ten 429 chodí — třicet volání `cheap` s vypnutým fallbackem
a spočítat, kolik jich spadne.

Alternativa, kdyby se ukázalo, že 429 chodí často: vlastní klíč k Novitě
přes BYOK (OpenRouter to sám nabízí jako remedy), čímž se z rate limitu
sdíleného fondu vystoupí. Nezanalyzováno.

---

## K zamyšlení (nezadané, nezanalyzované — jen nápady)

Volnější sekce než P1–P4: věci, které stojí za zvážení časem, ale ještě
nemají tvar požadavku k analýze.

- **Telegram (a Kryton obecně): volný chat bez vyhledávání v poznámkách.**
  Dnes každá čerstvá zpráva vždy jde přes `core.search()` + `core.answer()`
  a systémový prompt vynucuje odpovídat „POUZE na základě dodaného
  kontextu" — když nic nenajde, vrátí se rovnou pevná hláška, model se ani
  nezavolá. Žádný režim „jen pokecej" bez týhle vazby na poznámky
  neexistuje. Mohl by to být samostatný příkaz/přepínač, co obejde
  `core.search()`/`core.answer()` a jede jen s LLM bez omezení na kontext.
  Zvažováno 2026-08-11, nezadáno k realizaci.
