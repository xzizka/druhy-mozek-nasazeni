-- =====================================================================
-- Fáze 1 / krok 3: víceřečová lexikální větev — v ZIPu nebylo, přidáno.
--
-- PROČ: `02-retrieval.sql` má konfiguraci `czech` zadrátovanou v generovaném
-- sloupci, takže se aplikuje na VŠECHNY chunky bez ohledu na jazyk. Změřeno
-- na nasazeném systému, co to dělá s cizím textem:
--
--   angličtina přes czech:   'ar' 'databases' 'faster' 'indexes' 'running'
--                            'than' 'the' 'vector'
--   angličtina přes english: 'databas' 'faster' 'index' 'run' 'vector'
--
--   němčina přes czech:      'als' 'datenbanken' 'di' 'die' 'laufen'
--                            'schneller' 'vektorindizes'
--   němčina přes german:     'datenbank' 'lauf' 'schnell' 'vektorindiz'
--
-- Tedy: žádné stemmování, nevyfiltrované stopwords, a český hunspell cizí
-- slova občas ZKOMOLÍ (`are` -> `ar`, `die` -> `di`). Není to jen chybějící
-- funkce, je to šum v indexu.
--
-- ŘEŠENÍ: konfigurace per chunk. `to_tsvector(regconfig, text)` je IMMUTABLE
-- a generovaný sloupec smí odkazovat na jiné sloupce TÉHOŽ řádku, takže
-- `ts_config` může být obyčejný sloupec.
--
-- CENA: přepis tabulky, ale ŽÁDNÉ embeddingy. Vektory se nemění, takže je to
-- řádově levnější než cokoliv, co se dotýká `chunk.embedding`.
--
-- CO NEJDE: zřetězit stemmery. Slovníky v konfiguraci jsou alternativy a
-- snowball stemmery jsou nevybíravé — přijmou cokoliv a nikdy nepropadnou
-- dál. `cs_hunspell, english_stem, german_stem` by znamenalo, že vše, co
-- hunspell nepozná, dostane ANGLICKÉ stemmování včetně němčiny. Proto to
-- musí být rozhodnutí per dokument.
--
-- LATINA: Debian pro ni hunspell slovník nemá (`hunspell-la`, `myspell-la`
-- ani `ispell-latin` neexistují) a snowball ji nedodává. Dostává proto
-- `simple`: lowercase + unaccent, bez stemmování a bez stopwords. U silně
-- flektivního jazyka to lexikální větev oslabí; nese ji trigramová větev,
-- které flexe vadí méně, protože tvary mají velký trigramový překryv.
-- Až se objeví slovník, stačí přidat konfiguraci a přemapovat 'la'.
--
-- Spouštěj jako superuser proti databázi retrieval. Idempotentní.
-- =====================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------
-- Latinská konfigurace. Bez slovníku, ale s unaccent — makrony (ā, ē)
-- se v poznámkách píšou nekonzistentně a bez unaccentu by `amicus` a
-- `amīcus` byly dva různé lexémy.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'latin') THEN
        CREATE TEXT SEARCH CONFIGURATION latin ( COPY = simple );
        ALTER TEXT SEARCH CONFIGURATION latin
            ALTER MAPPING FOR asciiword, asciihword, hword_asciipart,
                              word, hword, hword_part
            WITH unaccent, simple;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- Jazyk na dokumentu (autoritativní) a jeho konfigurace na chunku
-- (denormalizovaná, protože generovaný sloupec nesmí sahat do jiné
-- tabulky).
-- ---------------------------------------------------------------------
ALTER TABLE retrieval.document
    ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT 'cs';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'document_lang_ck') THEN
        ALTER TABLE retrieval.document
            ADD CONSTRAINT document_lang_ck CHECK (lang IN ('cs','en','de','la'));
    END IF;
END $$;

COMMENT ON COLUMN retrieval.document.lang IS
    'cs|en|de|la; urcuje ts_config chunku. Deklaruje se ve frontmatteru markdownu.';

ALTER TABLE retrieval.chunk
    ADD COLUMN IF NOT EXISTS ts_config regconfig NOT NULL DEFAULT 'czech';

-- ---------------------------------------------------------------------
-- Přegenerování content_tsv. Generovaný sloupec nelze změnit na místě,
-- musí se zahodit a vytvořit znovu — to je ten přepis tabulky.
-- GIN index nad ním padá s ním, proto se staví taky znovu.
-- ---------------------------------------------------------------------
ALTER TABLE retrieval.chunk DROP COLUMN IF EXISTS content_tsv;

ALTER TABLE retrieval.chunk
    ADD COLUMN content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED;

CREATE INDEX IF NOT EXISTS chunk_tsv_gin ON retrieval.chunk USING gin (content_tsv);

-- ---------------------------------------------------------------------
-- hybrid_search: konfigurace pro dotazovou stranu jako parametr.
--
-- Dense a fuzzy větev jsou jazykově neutrální (bge-m3 je multijazyčný,
-- norm_text je jen lower+unaccent), takže napříč jazyky fungují dál.
-- Lexikální větev je per jazyk ZÁMĚRNĚ: dotaz stemmovaný anglicky nemá
-- proti českému tsvectoru co dělat, a předstírat opak by dávalo falešné
-- shody.
--
-- DROP PŘED CREATE, A TO JE OPRAVA Z 2026-09-18. Tady stálo `CREATE OR
-- REPLACE`, jenže `p_ts_config` je parametr NAVÍC proti verzi z 02 — a to
-- v Postgresu není náhrada, ale PŘETÍŽENÍ. Po doběhnutí 03 tedy v katalogu
-- ležely obě funkce vedle sebe a `GRANT EXECUTE ON FUNCTION
-- retrieval.hybrid_search` o pár řádků níž (bez seznamu argumentů) spadl
-- na `function name "retrieval.hybrid_search" is not unique`. Migrace se
-- přitom deklarovala jako idempotentní.
--
-- 04 tenhle stav neuklidilo: jeho `DROP` míří na DESETIARGUMENTOVÝ podpis,
-- tedy na verzi z 03, kdežto devítiargumentová z 02 mu proklouzne. Do D6
-- to nikdo nepoznal jen proto, že `04-init-db.sh` 03 ani 04 vůbec nepouštěl
-- a na komerci se dodělávaly ručně — tam se stará verze dropla po ruce.
--
-- Dropují se OBA známé podpisy: z 02 (bez p_ts_config) i vlastní (s ním),
-- aby šla migrace pustit znovu. Cena za druhý DROP je, že spuštění 03
-- samotného nad instancí po 04 SHODÍ `ordinal` a context expand přestane
-- fungovat — proto se 03 a 04 pouští vždycky jako dvojice a `05-verify.sql`
-- na chybějící `ordinal` hlídá.
-- ---------------------------------------------------------------------
DROP FUNCTION IF EXISTS retrieval.hybrid_search(
    halfvec, text, int, int, int, real, real, real, smallint);
DROP FUNCTION IF EXISTS retrieval.hybrid_search(
    halfvec, text, int, int, int, real, real, real, smallint, regconfig);

CREATE FUNCTION retrieval.hybrid_search(
    p_embedding   halfvec(1024),
    p_query       text,
    p_limit       int  DEFAULT 20,
    p_candidates  int  DEFAULT 60,
    p_k           int  DEFAULT 60,
    p_w_dense     real DEFAULT 1.0,
    p_w_lexical   real DEFAULT 1.0,
    p_w_fuzzy     real DEFAULT 0.4,
    p_max_trust   smallint DEFAULT 2,
    p_ts_config   regconfig DEFAULT 'czech'
)
RETURNS TABLE (
    chunk_id    bigint,
    document_id uuid,
    source_path text,
    heading_path text,
    content     text,
    trust_level smallint,
    score       real,
    r_dense     int,
    r_lexical   int,
    r_fuzzy     int
)
LANGUAGE sql STABLE PARALLEL SAFE
SET hnsw.ef_search = 100
SET pg_trgm.word_similarity_threshold = 0.5
AS $$
WITH q AS (
    SELECT websearch_to_tsquery(p_ts_config, p_query) AS tsq,
           retrieval.norm_text(p_query)               AS qnorm
),
dense AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> p_embedding) AS r
    FROM retrieval.chunk c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> p_embedding
    LIMIT p_candidates
),
lexical AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.tsq) DESC) AS r
    FROM retrieval.chunk c, q
    WHERE q.tsq IS NOT NULL
      AND c.content_tsv @@ q.tsq
    ORDER BY ts_rank_cd(c.content_tsv, q.tsq) DESC
    LIMIT p_candidates
),
fuzzy AS (
    SELECT c.id, row_number() OVER (ORDER BY word_similarity(q.qnorm, c.content_norm) DESC) AS r
    FROM retrieval.chunk c, q
    WHERE q.qnorm <% c.content_norm
    ORDER BY word_similarity(q.qnorm, c.content_norm) DESC
    LIMIT p_candidates
),
fused AS (
    SELECT COALESCE(d.id, l.id, f.id) AS id,
           ( p_w_dense   * COALESCE(1.0 / (p_k + d.r), 0)
           + p_w_lexical * COALESCE(1.0 / (p_k + l.r), 0)
           + p_w_fuzzy   * COALESCE(1.0 / (p_k + f.r), 0) )::real AS score,
           d.r::int AS r_dense,
           l.r::int AS r_lexical,
           f.r::int AS r_fuzzy
    FROM dense d
    FULL OUTER JOIN lexical l ON l.id = d.id
    FULL OUTER JOIN fuzzy   f ON f.id = COALESCE(d.id, l.id)
)
SELECT ch.id, ch.document_id, doc.source_path, ch.heading_path, ch.content,
       doc.trust_level, fu.score, fu.r_dense, fu.r_lexical, fu.r_fuzzy
FROM fused fu
JOIN retrieval.chunk    ch  ON ch.id  = fu.id
JOIN retrieval.document doc ON doc.id = ch.document_id
WHERE doc.trust_level <= p_max_trust
ORDER BY fu.score DESC, ch.id
LIMIT p_limit;
$$;

GRANT EXECUTE ON FUNCTION retrieval.hybrid_search TO retrieval_app, platform_ro;

-- ---------------------------------------------------------------------
-- Ověření, že každý jazyk stemmuje po svém.
-- ---------------------------------------------------------------------
DO $$
DECLARE en text; de text; cs text; la text;
BEGIN
    SELECT to_tsvector('english','The databases are running faster')::text INTO en;
    SELECT to_tsvector('german','Die Datenbanken laufen schneller')::text  INTO de;
    SELECT to_tsvector('czech','Ladím latenci vektorových indexů')::text   INTO cs;
    SELECT to_tsvector('latin','Amīcus amici amicorum')::text              INTO la;
    IF en NOT LIKE '%databas%' OR en LIKE '%the%' THEN
        RAISE EXCEPTION 'anglicke stemmovani nefunguje: %', en; END IF;
    IF de NOT LIKE '%datenbank%' THEN
        RAISE EXCEPTION 'nemecke stemmovani nefunguje: %', de; END IF;
    IF cs NOT LIKE '%ladit%' OR cs NOT LIKE '%latence%' THEN
        RAISE EXCEPTION 'ceska lemmatizace nefunguje: %', cs; END IF;
    IF la NOT LIKE '%amicus%' THEN
        RAISE EXCEPTION 'latinsky unaccent nefunguje: %', la; END IF;
    RAISE NOTICE 'en % | de % | cs % | la %', en, de, cs, la;
END $$;
