-- =====================================================================
-- Fáze 1 / krok 5: kontrola, že schéma `retrieval` odpovídá VŠEM migracím.
--
-- PROČ TO VZNIKLO (D6, 2026-09-17). Komerční instance běžela dva dny na
-- schématu, kterému chyběly migrace `03-multilang.sql` a `04-context-expand.sql`.
-- Nikdo si toho nevšiml, protože `04-init-db.sh` je nikdy neaplikoval
-- (pouštěl jen 01 a 02) a jeho ověřovací blok kontroloval stav PO 02 —
-- tedy přesně to, co tam bylo. Projevilo se to až při prvním naplnění
-- korpusu, chybou `column "lang" does not exist` z indexeru.
--
-- Mezi nasazením a tím pádem uběhly DVA DNY, ve kterých systém hlásil
-- „healthy" a smoke testy procházely. Prázdný korpus tu díru schoval:
-- dokud se neindexuje, na `document.lang` nikdo nesáhne. To je učebnicová
-- podoba „commitnuto ≠ nasazeno ≠ ověřeno" z NASAZENI.md, jen o patro níž
-- — tady bylo i nasazeno, jen ne celé.
--
-- CO TENHLE SOUBOR KONTROLUJE. Ne „proběhly migrace" (to by byl ledger
-- a ten by lhal u ručně opravované databáze), ale **pozorovatelný stav
-- schématu**: existují objekty, které jednotlivé migrace zavádějí, a mají
-- tvar, který od nich zbytek kódu čeká. Databáze opravená ručně projde
-- stejně jako čistě migrovaná, a to je záměr — zajímá nás, jestli systém
-- poběží, ne jak se do toho stavu dostal.
--
-- Nejcennější aserce je ta na `content_tsv` (03) a na POČET funkcí
-- `hybrid_search` (04): obě chytají neúplně doběhlou migraci, tedy stav,
-- ve kterém `ADD COLUMN IF NOT EXISTS` mlčky projde, ale chování je pořád
-- to staré. Právě takový stav se v D6 dva dny tvářil jako zdravý.
--
-- Spouštěj proti databázi retrieval. Nic nemění, jen čte — pouští se
-- i proti běžící produkci (viz `scripts/41-schema-check.sh`).
-- =====================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
    n       int;
    txt     text;
    chybi   text := '';
BEGIN
    -- ----------------------------------------------------------------
    -- 02-retrieval.sql — základ: schéma, tabulky, česká FTS, indexy
    -- ----------------------------------------------------------------
    SELECT to_tsvector('czech','Ladím latenci vektorových indexů')::text INTO txt;
    IF txt NOT LIKE '%ladit%' OR txt NOT LIKE '%latence%' THEN
        chybi := chybi || format(E'  02: ceska lemmatizace NEFUNGUJE (%s)\n', txt);
    END IF;

    SELECT count(*) INTO n FROM pg_indexes
     WHERE schemaname = 'retrieval'
       AND indexname IN ('chunk_embedding_hnsw','chunk_tsv_gin',
                         'chunk_trgm_gin','chunk_document_id_ix');
    IF n <> 4 THEN
        chybi := chybi || format(E'  02: indexu je %s ze 4\n', n);
    END IF;

    -- ----------------------------------------------------------------
    -- 03-multilang.sql — jazyk per dokument, ts_config per chunk
    -- ----------------------------------------------------------------
    -- Tohle je sloupec, na kterém v D6 spadl indexer.
    IF to_regclass('retrieval.document') IS NULL THEN
        chybi := chybi || E'  02: tabulka retrieval.document NEEXISTUJE\n';
    ELSE
        SELECT count(*) INTO n FROM pg_attribute
         WHERE attrelid = 'retrieval.document'::regclass
           AND attname = 'lang' AND NOT attisdropped;
        IF n <> 1 THEN
            chybi := chybi || E'  03: chybi document.lang (indexer na nem spadne)\n';
        END IF;
    END IF;

    IF to_regclass('retrieval.chunk') IS NULL THEN
        chybi := chybi || E'  02: tabulka retrieval.chunk NEEXISTUJE\n';
    ELSE
        SELECT count(*) INTO n FROM pg_attribute
         WHERE attrelid = 'retrieval.chunk'::regclass
           AND attname = 'ts_config' AND NOT attisdropped;
        IF n <> 1 THEN
            chybi := chybi || E'  03: chybi chunk.ts_config\n';
        END IF;

        -- Nejzákeřnější stav, jaký tu může nastat: sloupce z 03 přibyly
        -- (`ADD COLUMN IF NOT EXISTS` projde vždycky), ale `content_tsv`
        -- se pořád generuje přes natvrdo zadrátované 'czech' z 02. Schéma
        -- pak vypadá zmigrovaně a lexikální větev přitom tiše mele cizí
        -- jazyky přes český hunspell — tedy přesně ta vada, kvůli které
        -- 03 vznikla. Poznat to jde jen z generujícího výrazu.
        SELECT pg_get_expr(d.adbin, d.adrelid) INTO txt
          FROM pg_attrdef d
          JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         WHERE d.adrelid = 'retrieval.chunk'::regclass
           AND a.attname = 'content_tsv';
        IF txt IS NULL THEN
            chybi := chybi || E'  03: content_tsv neni generovany sloupec\n';
        ELSIF txt NOT LIKE '%ts_config%' THEN
            chybi := chybi || format(
                E'  03: content_tsv se generuje BEZ ts_config (%s)\n', txt);
        END IF;
    END IF;

    -- ----------------------------------------------------------------
    -- 04-context-expand.sql — hybrid_search vrací ordinal
    -- ----------------------------------------------------------------
    -- POČET, ne existence. 03 i 04 vytvářejí `hybrid_search` s různým
    -- podpisem, takže po neúplné migraci můžou v katalogu ležet OBĚ
    -- najednou. Volání pak skončí na `function is not unique` — ale až
    -- za běhu, na konkrétním dotazu. V D6 tenhle stav skutečně nastal
    -- (starou variantu bylo nutné ručně DROPnout, jinak spadl GRANT).
    SELECT count(*) INTO n FROM pg_proc p
      JOIN pg_namespace ns ON ns.oid = p.pronamespace
     WHERE ns.nspname = 'retrieval' AND p.proname = 'hybrid_search';
    IF n = 0 THEN
        chybi := chybi || E'  04: hybrid_search NEEXISTUJE\n';
    ELSIF n > 1 THEN
        chybi := chybi || format(
            E'  04: hybrid_search existuje %sx (ruzne podpisy) -> volani spadne na "function is not unique"\n', n);
    ELSE
        -- Výstupní sloupec `ordinal` je celý přínos 04; bez něj
        -- `app/expand.py` nemá podle čeho dotáhnout sousední chunky.
        SELECT count(*) INTO n FROM pg_proc p
          JOIN pg_namespace ns ON ns.oid = p.pronamespace
          JOIN unnest(p.proargnames) AS an(name) ON true
         WHERE ns.nspname = 'retrieval' AND p.proname = 'hybrid_search'
           AND an.name = 'ordinal';
        IF n < 1 THEN
            chybi := chybi || E'  04: hybrid_search nevraci ordinal (context expand nepobezi)\n';
        END IF;

        -- Parametr z 03. Kdyby chybělo, běží se na podpisu z 02.
        SELECT count(*) INTO n FROM pg_proc p
          JOIN pg_namespace ns ON ns.oid = p.pronamespace
          JOIN unnest(p.proargnames) AS an(name) ON true
         WHERE ns.nspname = 'retrieval' AND p.proname = 'hybrid_search'
           AND an.name = 'p_ts_config';
        IF n < 1 THEN
            chybi := chybi || E'  03: hybrid_search nema p_ts_config\n';
        END IF;
    END IF;

    IF chybi <> '' THEN
        RAISE EXCEPTION E'schema retrieval NEODPOVIDA migracim:\n%', chybi;
    END IF;

    RAISE NOTICE 'OK: schema retrieval odpovida migracim 02, 03, 04';
END $$;

-- Volatelnost se ověřuje AŽ po strukturálních asercích a mimo DO blok:
-- když tohle spadne, je z chyby rovnou vidět podpis, který Postgres hledal.
SELECT count(*) AS hybrid_search_volatelna
  FROM retrieval.hybrid_search(
       (SELECT ('['||string_agg('0.01',',')||']')::halfvec(1024)
          FROM generate_series(1,1024)), 'test', 1);
