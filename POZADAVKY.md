# Požadavky

Zadané, **zatím neschválené k provedení**. Analýza a schválení probíhá
u každého tématu zvlášť. Hotové věci se odsud stěhují do `NASAZENI.md`.

Stav: `ZAZNAMENÁNO` → `ANALYZOVÁNO` → `SCHVÁLENO` → `HOTOVO`.

---

## P1 — Rozpoznat halucinačně rizikové dotazy a odkázat na `/korpus`

**Stav: P1a HOTOVO (2026-08-09) · P1b SCHVÁLENO, nezačato**

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

### Otevřené otázky

1. Čím dotaz klasifikovat — heuristika nad klíčovými slovy, nebo LLM
   klasifikátor (alias `cheap`, tedy volání navíc na každý dotaz)?
2. U P1b: kdo píše SQL? Text-to-SQL přes LLM je nejmocnější, ale generovaný
   dotaz nad `platform_ro` je pořád nová plocha (náklad, chybné agregace,
   nekonečné dotazy). Alternativou je katalog předpřipravených metrik.
3. Kam se doplněné výsledky ukládají a jak stárnou? Korpus se mění při každé
   indexaci, takže uložené číslo je za chvíli neaktuální.
4. Má `/korpus` zůstat statická stránka, nebo se stát rostoucím katalogem
   metrik s vlastní tabulkou?

---

## P2 — Nahrávání dokumentů, text do DB, originály na S3

**Stav: ZAZNAMENÁNO (2026-08-09)**

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

### Otevřené otázky

1. **Jaké S3?** Hetzner Object Storage, MinIO na brainu, AWS, něco jiného?
   Bez toho nejde řešit endpoint, region, ceny ani zálohu.
2. Kam se ukládá **vytažený text**? Do markdown stromu (znovupoužije celou
   pipeline, ale text z PDF se tím dostane do git repa poznámek), nebo mimo
   něj do vlastního úložiště?
3. Kdo dělá extrakci a kde? PDF a DOCX potřebují knihovny navíc a Kryton má
   `MemoryMax=800M`. Velký sken by ho mohl položit.
4. Limity: maximální velikost souboru, počet stránek, chování u PDF, které
   je jen obrázek (bez textové vrstvy) — OCR je mimo zadání („jen textové
   dokumenty"), ale uživatel to nahraje a musí dostat srozumitelnou odpověď.
5. Deduplikace a verze: co když se tentýž soubor nahraje dvakrát, nebo
   v novější verzi?
6. Co se stane při smazání — mizí i objekt na S3?
