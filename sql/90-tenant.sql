-- =====================================================================
-- Fáze 4 / D7: šablona tenanta — schéma + objekty per tenant.
--
-- Tenancy model (TENANCY.md): jeden deployment procesů, každý zákazník
-- má vlastní PostgreSQL schéma (`t_<slug>`) a vlastní adresář v
-- /srv/rag/tenants/<slug>/markdown. Schéma izoluje UNIQUE source_path
-- i filtr trust_level (D10), nepotřebuje žádnou změnu v logice
-- hybrid_search — jediný rozdíl oproti `retrieval.hybrid_search`
-- (04-context-expand.sql) je NÁZEV SCHÉMATA (`:t.`), tělo je identické.
--
-- Spouští se parametrický přes psql proměnné (substituce `:t` funguje
-- i uvnitř dollar-quoted funkčního těla):
--
--   podman exec -i postgres psql -X -U postgres -d retrieval \
--       -v ON_ERROR_STOP=1 \
--       -v t=t_demo -v owner=t_demo_app \
--       -f sql/90-tenant.sql
--
-- Roli `:owner` si vytvoří volající (onboarding), NE tato šablona —
-- heslo role patří do tenant secrets. Role se předpokládá existující.
--
-- Idempotentní: DROP ... IF EXISTS + CREATE. Přes nezměněné podpisy
-- funkcí stačí CREATE OR REPLACE; kvůli proudění migrací děláme
-- DROP + CREATE (stejný vzor jako 03/04).
-- =====================================================================

\set ON_ERROR_STOP on

-- Schema tenanta (vlastníkem je tenant role). Volající dál zřídí roli
-- a heslo (tenant secret); tady jen schéma + objekty.
CREATE SCHEMA IF NOT EXISTS :t AUTHORIZATION :owner;

-- ---------------------------------------------------------------------
-- Normalizace textu (stejná jako retrieval.norm_text; cizí pomocné
-- funkce zůstávají v `retrieval` / public, jen se nemusí sdílet).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION :t.norm_text(p_in text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, p_in)) $$;

-- ---------------------------------------------------------------------
-- Dokumenty a chunky. Definice IDENTICKÉ s 02-retrieval.sql, jediný
-- rozdíl: `retrieval.` → `:t.`. UNIQUE source_path je tím pádem PER
-- SCHÉMA — stejná cesta souboru u dvou různých tenantů je platná.
--
-- trust_level: 0 = vlastní, 1 = importované, 2 = automatický sync.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS :t.document (
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

CREATE TABLE IF NOT EXISTS :t.chunk (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  uuid    NOT NULL REFERENCES :t.document(id) ON DELETE CASCADE,
    ordinal      int     NOT NULL,
    content      text    NOT NULL,
    token_count  int,
    heading_path text,
    embedding    halfvec(1024),
    content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('czech', content)) STORED,
    content_norm text     GENERATED ALWAYS AS (:t.norm_text(content)) STORED,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chunk_document_ordinal_uq UNIQUE (document_id, ordinal)
);

DROP INDEX IF EXISTS :t.chunk_embedding_hnsw;
DROP INDEX IF EXISTS :t.chunk_tsv_gin;
DROP INDEX IF EXISTS :t.chunk_trgm_gin;
DROP INDEX IF EXISTS :t.chunk_document_id_ix;

CREATE INDEX chunk_embedding_hnsw ON :t.chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX chunk_tsv_gin  ON :t.chunk USING gin (content_tsv);
CREATE INDEX chunk_trgm_gin ON :t.chunk USING gin (content_norm gin_trgm_ops);
CREATE INDEX chunk_document_id_ix ON :t.chunk (document_id);

-- ---------------------------------------------------------------------
-- Hybridní vyhledávání per tenant. POZOR na index názvů: `chunk_...`,
-- `:t.chunk` — index názvy jsou per-schema (jádro nemusí být
-- schéma-qualifikované, globální UNIQUE by bránilo reindexu).
--
-- Tělo funkce je BEZE ZMĚNY oproti 04-context-expand.sql (RRF, tři
-- větve, p_max_trust filtr), jen NEPOUŽÍVÁ schéma-qualifikaci —
-- `SET search_path = :t, public` ve vlastnostech funkce vyřeší všechny
-- nekválifikované názvy (chunk, document, norm_text) na tenantovo
-- schéma za běhu. To je celá „úprava" pro tenancy — výpočet je
-- identický s tím, co denně běží na osobní instanci.
-- ---------------------------------------------------------------------
DROP FUNCTION IF EXISTS :t.hybrid_search(
    halfvec, text, int, int, int, real, real, real, smallint, regconfig);

CREATE FUNCTION :t.hybrid_search(
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
    chunk_id     bigint,
    document_id  uuid,
    source_path  text,
    heading_path text,
    content      text,
    ordinal      int,
    trust_level  smallint,
    score        real,
    r_dense      int,
    r_lexical    int,
    r_fuzzy      int
)
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = :t, public
SET hnsw.ef_search = 100
SET pg_trgm.word_similarity_threshold = 0.5
AS $$
WITH q AS (
    SELECT websearch_to_tsquery(p_ts_config, p_query) AS tsq,
           norm_text(p_query)                         AS qnorm
),
dense AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> p_embedding) AS r
    FROM chunk c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> p_embedding
    LIMIT p_candidates
),
lexical AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.tsq) DESC) AS r
    FROM chunk c, q
    WHERE q.tsq IS NOT NULL
      AND c.content_tsv @@ q.tsq
    ORDER BY ts_rank_cd(c.content_tsv, q.tsq) DESC
    LIMIT p_candidates
),
fuzzy AS (
    SELECT c.id, row_number() OVER (ORDER BY word_similarity(q.qnorm, c.content_norm) DESC) AS r
    FROM chunk c, q
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
       ch.ordinal, doc.trust_level, fu.score, fu.r_dense, fu.r_lexical, fu.r_fuzzy
FROM fused fu
JOIN chunk    ch  ON ch.id  = fu.id
JOIN document doc ON doc.id = ch.document_id
WHERE doc.trust_level <= p_max_trust
ORDER BY fu.score DESC, ch.id
LIMIT p_limit;
$$;

-- ---------------------------------------------------------------------
-- Oprávnění. Owner = provozní role pro tenanta, retrieval_app = služba
-- (umí DML všech tenantů podle tokenu requestu), platform_ro = čtení
-- pro interní BI/reporting. Zápis cizím schématem = permission denied,
-- viz 21-tenancy-proof.sh / D10.
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA :t TO :owner, retrieval_app, platform_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA :t TO :owner, retrieval_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA :t TO :owner, retrieval_app;
GRANT EXECUTE ON FUNCTION :t.hybrid_search TO :owner, retrieval_app, platform_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA :t TO platform_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA :t
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :owner, retrieval_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA :t
    GRANT SELECT ON TABLES TO platform_ro;

-- ---------------------------------------------------------------------
-- Ověření tvaru (stejný vzor jako 04): nulový vektor je nevypovídající,
-- jen potvrdí, že funkce je selectovatelná a `ordinal` ve výstupu je.
-- ---------------------------------------------------------------------
SET search_path = :t, public;
SELECT 'tenant ' || :'t'
    || ' hybrid_search.ordinal je selectovatelny (hodnota nebo NULL kdyz je schema prazdne): '
    || COALESCE(ordinal::text, 'NULL') AS check_shape
FROM hybrid_search(
    p_embedding => ('[' || array_to_string(array_fill(0::real, ARRAY[1024]), ',') || ']')::halfvec(1024),
    p_query     => 'test',
    p_limit     => 1
);