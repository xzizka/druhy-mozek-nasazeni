-- =====================================================================
-- Fáze 1 / krok 4: context window expansion — hybrid_search vrací ordinal.
--
-- PROČ: hledání vrací chunky izolovaně. Chunkování je bez overlapu (viz
-- chunker.py), takže hranice chunku je otázka rozpočtu CHUNK_CHARS, ne
-- významu, a odpověď se na ní umí rozseknout přesně uprostřed myšlenky.
-- Aby služba mohla po hledání dotáhnout sousední chunky (ordinal ± okno
-- ve stejném dokumentu, viz app/expand.py), potřebuje znát ordinal
-- vítězného chunku — a ten `hybrid_search` dosud nevracel.
--
-- CREATE OR REPLACE nejde: přidání výstupního sloupce mění RETURNS TABLE,
-- což Postgres u existující funkce odmítá ("cannot change return type of
-- existing function"). Musí se DROP + CREATE. Parametry (vč. defaultů)
-- se NEMĚNÍ, jen přibývá `ordinal` do výstupu — tělo funkce je jinak
-- identické s verzí z 03-multilang.sql.
--
-- Spouštěj jako superuser proti databázi retrieval. Idempotentní.
-- =====================================================================

\set ON_ERROR_STOP on

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
    ordinal     int,
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
       ch.ordinal, doc.trust_level, fu.score, fu.r_dense, fu.r_lexical, fu.r_fuzzy
FROM fused fu
JOIN retrieval.chunk    ch  ON ch.id  = fu.id
JOIN retrieval.document doc ON doc.id = ch.document_id
WHERE doc.trust_level <= p_max_trust
ORDER BY fu.score DESC, ch.id
LIMIT p_limit;
$$;

GRANT EXECUTE ON FUNCTION retrieval.hybrid_search TO retrieval_app, platform_ro;

-- ---------------------------------------------------------------------
-- Ověření, že funkce reálně vrací `ordinal` a nespadla na typech.
--
-- Nulový vektor je ZÁMĚRNĚ nevypovídající o kvalitě hledání — ověřuje se
-- TVAR výstupu (sloupec existuje, dá se SELECTnout), ne přesnost. Kvalitu
-- ověřuje `scripts/18-context-expand-check.sh` proti reálným datům.
-- Pád tady znamená, že migrace nedoběhla (DROP se nepovedl, sloupec chybí,
-- nebo se restartu služby vrátila stará definice).
-- ---------------------------------------------------------------------
DO $$
DECLARE v_ordinal int;
        v_zero halfvec(1024);
BEGIN
    SELECT ('[' || array_to_string(array_fill(0::real, ARRAY[1024]), ',') || ']')::halfvec(1024)
        INTO v_zero;
    SELECT ordinal INTO v_ordinal
    FROM retrieval.hybrid_search(p_embedding => v_zero, p_query => 'test', p_limit => 1);
    RAISE NOTICE 'hybrid_search.ordinal je selectovatelny (hodnota nebo NULL kdyz je index prazdny): %', v_ordinal;
END $$;
