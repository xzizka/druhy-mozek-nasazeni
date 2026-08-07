-- =====================================================================
-- Fáze 1 / krok 2: retrieval schéma, česká full-text konfigurace,
-- indexy a hybridní vyhledávání s RRF fúzí.
--
-- Spouštěj jako superuser proti databázi retrieval (kvůli CREATE TEXT
-- SEARCH DICTIONARY, které vyžaduje čtení souborů z $SHAREDIR).
--
-- POZOR: PostgreSQL NEMÁ vestavěný snowball stemmer pro češtinu.
-- Dodávané jazyky jsou dánština, holandština, angličtina, finština,
-- francouzština, němčina, uherština, italština, norština, portugalština,
-- rumunština, ruština, španělština, švédština a turečtina. Čeština chybí.
-- Bez konfigurace níže by lexikální větev hybridního hledání běžela na
-- 'simple' configu, tedy bez lemmatizace, a "latenci" by nenašlo
-- dokument obsahující "latence".
-- =====================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------
-- Česká slovníková konfigurace nad hunspell cs_CZ.
--
-- Soubory cs_cz.dict a cs_cz.affix musí být v $SHAREDIR/tsearch_data
-- (u PG17 typicky /usr/share/postgresql/17/tsearch_data) a v UTF-8.
-- Debianí balík hunspell-cs je v UTF-8 už z distribuce, takže stačí
-- kopie bez iconv - viz Containerfile.
--
-- ROZHODNUTÍ O POŘADÍ SLOVNÍKŮ: hunspell MUSÍ být první, unaccent druhý.
-- Slovníky v konfiguraci nejsou pipeline, ale alternativy - první, který
-- token rozpozná, vyhrává; filtrovací slovník (unaccent) token upraví a
-- pošle dál. Kdyby unaccent běžel první, dostal by hunspell "resim"
-- místo "řeším", český slovník by to nenašel a lemmatizace by se úplně
-- ztratila. Ověřeno: pořadí (unaccent, hunspell) dá 'resim', pořadí
-- (hunspell, unaccent) dá správně 'řešit'.
--
-- DŮSLEDEK: index je lemmatizovaný, ale s diakritikou. Dotaz napsaný
-- bez diakritiky ("vektorovy index") lexikální větev nenajde. Právě
-- proto má hybrid_search třetí, trigramovou větev - viz níže.
-- ---------------------------------------------------------------------
CREATE TEXT SEARCH DICTIONARY cs_hunspell (
    TEMPLATE = ispell,
    DictFile = cs_cz,
    AffFile  = cs_cz
);

CREATE TEXT SEARCH CONFIGURATION czech ( COPY = simple );

ALTER TEXT SEARCH CONFIGURATION czech
    ALTER MAPPING FOR asciiword, asciihword, hword_asciipart,
                      word, hword, hword_part
    WITH cs_hunspell, unaccent, simple;

-- Ověření, že lemmatizace skutečně funguje. Když se sem dostaneš s
-- nezlemmatizovaným výstupem, chybí slovníkové soubory.
DO $$
DECLARE v text;
BEGIN
    SELECT to_tsvector('czech', 'Ladím latenci vektorových indexů')::text INTO v;
    IF v NOT LIKE '%ladit%' OR v NOT LIKE '%latence%' THEN
        RAISE EXCEPTION 'ceska lemmatizace nefunguje, dostal jsem: %', v;
    END IF;
    RAISE NOTICE 'ceska FTS konfigurace OK: %', v;
END $$;

-- ---------------------------------------------------------------------
-- Normalizace textu pro trigramovou větev.
--
-- unaccent(text) je STABLE, ne IMMUTABLE, a v generovaném sloupci by
-- ji PostgreSQL odmítl. Dvouargumentová varianta s explicitním
-- regdictionary IMMUTABLE je - to je dokumentovaný způsob, jak to obejít.
-- ---------------------------------------------------------------------
CREATE SCHEMA retrieval AUTHORIZATION retrieval_app;

CREATE OR REPLACE FUNCTION retrieval.norm_text(p_in text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, p_in)) $$;

-- ---------------------------------------------------------------------
-- Dokumenty. Markdown soubor zůstává autoritativním zdrojem, tahle
-- tabulka je jen derivovaný index - celé schéma musí být kdykoliv
-- znovu postavitelné z markdownu.
--
-- trust_level je tu už teď, i když bezpečnostní fázi řešíme později:
-- doplnit ho zpětně nad naplněným indexem znamená reindex všeho.
--   0 = vlastní poznámka, 1 = importované, 2 = automatický sync z venku
-- ---------------------------------------------------------------------
CREATE TABLE retrieval.document (
    id           uuid        PRIMARY KEY,
    source_path  text        NOT NULL UNIQUE,
    title        text,
    content_hash bytea       NOT NULL,
    trust_level  smallint    NOT NULL DEFAULT 0,
    chunk_count  int         NOT NULL DEFAULT 0,
    indexed_at   timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    meta         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT document_trust_level_ck CHECK (trust_level BETWEEN 0 AND 2)
);

COMMENT ON COLUMN retrieval.document.content_hash IS
    'sha256 markdown souboru; inkrementální reindex porovnává tenhle hash';

-- ---------------------------------------------------------------------
-- Chunky.
--
-- halfvec(1024) místo vector(1024): bge-m3 má 1024 dimenzí, halfvec
-- ukládá 2 bajty na dimenzi místo 4. Index se zmenší na polovinu a
-- dopad na recall je při 1024 dimenzích zanedbatelný. Na 16 GB je to
-- rozdíl mezi indexem v page cache a indexem na disku.
--
-- content_tsv je generovaný sloupec: to_tsvector(regconfig, text) je
-- IMMUTABLE, jednoargumentová varianta by nebyla (závisí na
-- default_text_search_config).
-- ---------------------------------------------------------------------
CREATE TABLE retrieval.chunk (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  uuid    NOT NULL REFERENCES retrieval.document(id) ON DELETE CASCADE,
    ordinal      int     NOT NULL,
    content      text    NOT NULL,
    token_count  int,
    heading_path text,
    embedding    halfvec(1024),
    content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('czech', content)) STORED,
    content_norm text     GENERATED ALWAYS AS (retrieval.norm_text(content)) STORED,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chunk_document_ordinal_uq UNIQUE (document_id, ordinal)
);

-- ---------------------------------------------------------------------
-- Indexy.
--
-- m=16 / ef_construction=64 jsou konzervativní defaulty. Vyšší
-- ef_construction zlepší recall, ale build na CPU je pak výrazně delší -
-- a build je na tomhle stroji ta drahá operace, ne dotazování.
--
-- Index staví PRÁZDNÝ. Při prvním hromadném naplnění ho zahoď, nahraj
-- data a postav znovu - inkrementální insert do HNSW je řádově pomalejší
-- než build nad hotovou tabulkou.
-- ---------------------------------------------------------------------
CREATE INDEX chunk_embedding_hnsw ON retrieval.chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX chunk_tsv_gin  ON retrieval.chunk USING gin (content_tsv);
CREATE INDEX chunk_trgm_gin ON retrieval.chunk USING gin (content_norm gin_trgm_ops);
CREATE INDEX chunk_document_id_ix ON retrieval.chunk (document_id);

-- ---------------------------------------------------------------------
-- Hybridní vyhledávání s Reciprocal Rank Fusion.
--
-- TŘI VĚTVE, každá řeší jiný typ selhání:
--   dense   - semantika a parafráze; zvládá i dotaz bez diakritiky,
--             protože bge-m3 je multijazyčný a na tokenizaci necitlivý
--   lexical - přesné termíny a lemmatizovaná čeština; vysoká precision
--   fuzzy   - identifikátory, verze, překlepy A dotazy bez diakritiky,
--             které lexikální větev minout musí (viz komentář výše)
--
-- RRF místo vážené sumy skóre: kosinová distance a ts_rank_cd nejsou
-- na srovnatelné škále a normalizovat je napříč dotazy nelze. RRF
-- pracuje jen s pořadím, takže je na škále nezávislé.
--
-- p_k = 60 je hodnota z původního RRF paperu a v praxi funguje;
-- nižší k dá větší váhu prvním pozicím.
--
-- Funkce vrací i jednotlivá pořadí (r_dense, r_lexical, r_fuzzy) -
-- bez nich neladíš váhy, jen hádáš.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION retrieval.hybrid_search(
    p_embedding   halfvec(1024),
    p_query       text,
    p_limit       int  DEFAULT 20,
    p_candidates  int  DEFAULT 60,
    p_k           int  DEFAULT 60,
    p_w_dense     real DEFAULT 1.0,
    p_w_lexical   real DEFAULT 1.0,
    p_w_fuzzy     real DEFAULT 0.4,
    p_max_trust   smallint DEFAULT 2
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
    SELECT websearch_to_tsquery('czech', p_query) AS tsq,
           retrieval.norm_text(p_query)           AS qnorm
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

-- ---------------------------------------------------------------------
-- Granty. Aplikace nesmí měnit schéma - migrace pouští samostatná role
-- nebo superuser, běžící služba má jen DML.
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA retrieval TO retrieval_app, platform_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA retrieval TO retrieval_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA retrieval TO retrieval_app;
GRANT EXECUTE ON FUNCTION retrieval.hybrid_search TO retrieval_app, platform_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA retrieval TO platform_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA retrieval
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO retrieval_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA retrieval
    GRANT SELECT ON TABLES TO platform_ro;
