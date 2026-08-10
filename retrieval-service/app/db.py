"""Přístup do databáze.

Role `retrieval_app` má podle návrhu jen DML — schéma nevlastní a měnit ho
nesmí. Ověřeno na nasazeném systému: tabulku `retrieval.chunk` vlastní
`postgres`, takže služba NEMŮŽE zahodit ani postavit HNSW index. Bulk
naplnění s drop+rebuild indexu je proto operátorský krok
(`scripts/08-first-fill.sh`), ne součást služby.

halfvec se předává jako textový literál s explicitním castem. Vyhnu se tím
závislosti na pgvector-python a při recyklaci embeddingu se hodnota přenese
beze změny, bez převodu na float a zpět.
"""
from __future__ import annotations

import uuid

import psycopg
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is None:
        # Malý pool: služba je jednovláknová v indexaci a dotazy jsou krátké.
        # max_connections v Postgresu je 50 a sdílí se s ostatními službami.
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=4,
                               kwargs={"autocommit": True}, open=True)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("pool neni inicializovany")
    return _pool


def vec_literal(vec) -> str:
    """Embedding -> literál pro ::halfvec. Recyklovanou hodnotu nechá být."""
    if isinstance(vec, str):
        return vec
    return "[" + ",".join(format(float(x), ".6f") for x in vec) + "]"


# ---------------------------------------------------------------------------
# Čtení stavu indexu
# ---------------------------------------------------------------------------

def load_documents() -> dict[str, dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT source_path, id, content_hash, indexed_at, chunk_count, lang "
            "FROM retrieval.document").fetchall()
    # `lang` se tahá kvůli RESUME: u dokumentu se shodným hashem je obsah
    # identický, takže uložený jazyk platí a nemusí se detekovat znovu.
    return {r[0]: {"id": r[1], "content_hash": bytes(r[2]), "indexed_at": r[3],
                   "chunk_count": r[4], "lang": r[5]} for r in rows}


def document_chunk_embeddings(document_id) -> dict[str, str]:
    """content -> embedding literál, pro recyklaci u změněného dokumentu.

    Klíčem je OBSAH, ne ordinal. Vložením odstavce se všechny následující
    ordinály posunou, ale jejich text zůstane stejný — porovnání podle
    ordinálu by zbytečně přeembeddovalo celý zbytek dokumentu.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT content, embedding::text FROM retrieval.chunk "
            "WHERE document_id = %s AND embedding IS NOT NULL",
            (document_id,)).fetchall()
    return {r[0]: r[1] for r in rows}


def stats() -> dict:
    with pool().connection() as conn:
        docs = conn.execute("SELECT count(*) FROM retrieval.document").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM retrieval.chunk").fetchone()[0]
        pending = conn.execute(
            "SELECT count(*) FROM retrieval.chunk WHERE embedding IS NULL").fetchone()[0]
        unfinished = conn.execute(
            "SELECT count(*) FROM retrieval.document WHERE indexed_at IS NULL").fetchone()[0]
        hnsw = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname='retrieval' "
            "AND indexname='chunk_embedding_hnsw')").fetchone()[0]
        # Rozpad po jazycích: nejlevnější způsob, jak poznat, že indexer jazyk
        # skutečně zapisuje a že se ts_config nerozešel s document.lang.
        by_lang = conn.execute(
            "SELECT lang, count(*) FROM retrieval.document "
            "GROUP BY lang ORDER BY lang").fetchall()
        by_config = conn.execute(
            "SELECT ts_config::text, count(*) FROM retrieval.chunk "
            "GROUP BY ts_config ORDER BY 1").fetchall()
    return {"documents": docs, "chunks": chunks, "chunks_without_embedding": pending,
            "documents_unfinished": unfinished, "hnsw_index_present": hnsw,
            "documents_by_lang": {r[0]: r[1] for r in by_lang},
            "chunks_by_ts_config": {r[0]: r[1] for r in by_config}}


def ping() -> bool:
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except psycopg.Error:
        return False


# ---------------------------------------------------------------------------
# Zápis
# ---------------------------------------------------------------------------

def upsert_document(source_path: str, title: str, content_hash: bytes,
                    trust_level: int, lang: str) -> uuid.UUID:
    """Vrátí id dokumentu. indexed_at se ZÁMĚRNĚ nenastavuje.

    `lang` musí být z lang.LANGS — sloupec má CHECK document_lang_ck a cizí
    hodnota by INSERT shodila. Volající to zajišťuje přes lang.normalize().
    """
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO retrieval.document "
            "  (id, source_path, title, content_hash, trust_level, lang, "
            "   chunk_count, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0, now()) "
            "ON CONFLICT (source_path) DO UPDATE SET "
            "  title = EXCLUDED.title, content_hash = EXCLUDED.content_hash, "
            "  lang = EXCLUDED.lang, trust_level = EXCLUDED.trust_level, "
            "  indexed_at = NULL, updated_at = now() "
            "RETURNING id",
            (uuid.uuid4(), source_path, title, content_hash, trust_level,
             lang)).fetchone()
    return row[0]


def replace_chunks(document_id, chunks: list[tuple[int, str, str | None, str | None]],
                   ts_config: str) -> None:
    """Nahradí chunky dokumentu. `chunks` = [(ordinal, content, heading_path, vec|None)].

    `ts_config` je denormalizovaný jazyk dokumentu (generovaný sloupec
    content_tsv nesmí sahat do jiné tabulky, viz 03-multilang.sql). Zapisuje se
    na každý chunk a content_tsv se z něj přegeneruje sám.

    Celé v jedné transakci — po pádu nesmí zůstat dokument s půlkou chunků.
    """
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM retrieval.chunk WHERE document_id = %s", (document_id,))
            with conn.cursor() as cur:
                for ordinal, content, heading, vec in chunks:
                    cur.execute(
                        "INSERT INTO retrieval.chunk "
                        "  (document_id, ordinal, content, heading_path, embedding, ts_config) "
                        "VALUES (%s, %s, %s, %s, %s::halfvec, %s::regconfig)",
                        (document_id, ordinal, content, heading, vec, ts_config))


def pending_chunks(limit: int) -> list[tuple[int, str]]:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT id, content FROM retrieval.chunk WHERE embedding IS NULL "
            "ORDER BY document_id, ordinal LIMIT %s", (limit,)).fetchall()


def set_chunk_embeddings(pairs: list[tuple[int, str]]) -> None:
    with pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for chunk_id, vec in pairs:
                    cur.execute(
                        "UPDATE retrieval.chunk SET embedding = %s::halfvec WHERE id = %s",
                        (vec, chunk_id))


def finish_document(document_id, chunk_count: int) -> None:
    """Nastaví indexed_at. Volat AŽ když mají všechny chunky embedding."""
    with pool().connection() as conn:
        conn.execute(
            "UPDATE retrieval.document SET indexed_at = now(), updated_at = now(), "
            "chunk_count = %s WHERE id = %s", (chunk_count, document_id))


def delete_documents(source_paths: list[str]) -> int:
    """Chunky spadnou kaskádou (ON DELETE CASCADE)."""
    if not source_paths:
        return 0
    with pool().connection() as conn:
        cur = conn.execute("DELETE FROM retrieval.document WHERE source_path = ANY(%s)",
                           (source_paths,))
        return cur.rowcount


def analyze() -> None:
    """ANALYZE smí i ne-vlastník s právem MAINTAIN? Ne — tiše přeskoč.

    Statistiky po větší změně aktualizuje operátorský skript jako superuser.
    """
    try:
        with pool().connection() as conn:
            conn.execute("ANALYZE retrieval.chunk")
    except psycopg.Error:
        pass


# ---------------------------------------------------------------------------
# Dotazování
# ---------------------------------------------------------------------------

def hybrid_search(embedding_literal: str, query_terms: str, limit: int,
                  candidates: int, max_trust: int,
                  ts_config: str = "czech") -> list[dict]:
    """`ts_config` řídí JEN lexikální větev — dense a fuzzy jsou jazykově
    neutrální (bge-m3 je multijazyčný, norm_text je lower+unaccent), takže
    fungují napříč jazyky bez ohledu na tenhle parametr.

    Že dotaz stemmovaný anglicky nenajde nic v českém tsvectoru, je ZÁMĚR
    migrace 03-multilang.sql, ne chyba — předstírat opak by dávalo falešné
    shody. Cizojazyčné dokumenty v tu chvíli nese dense a fuzzy větev.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, document_id, source_path, heading_path, content, "
            "       ordinal, trust_level, score, r_dense, r_lexical, r_fuzzy "
            "FROM retrieval.hybrid_search("
            "  p_embedding => %s::halfvec, p_query => %s, p_limit => %s, "
            # p_max_trust je smallint; int4 -> smallint neni implicitni cast,
            # takze bez explicitniho ::smallint by Postgres funkci nenasel.
            # Totez plati pro p_ts_config: text -> regconfig neni implicitni.
            "  p_candidates => %s, p_max_trust => %s::smallint, "
            "  p_ts_config => %s::regconfig)",
            (embedding_literal, query_terms, limit, candidates, max_trust,
             ts_config)).fetchall()
    return [{"chunk_id": r[0], "document_id": str(r[1]), "source_path": r[2],
             "heading_path": r[3], "content": r[4], "ordinal": r[5],
             "trust_level": r[6],
             "rrf_score": float(r[7]) if r[7] is not None else None,
             "r_dense": r[8], "r_lexical": r[9], "r_fuzzy": r[10]} for r in rows]


def fetch_chunk_range(document_id: str, lo: int, hi: int) -> list[dict]:
    """Chunky dokumentu s `ordinal` v [lo, hi], seřazené. Pro context window
    expansion (viz expand.py) — dotahuje sousedy vítězného chunku.

    `document_id` přichází jako str (viz hybrid_search výš, string kvůli
    JSON serializaci), proto explicitní ::uuid — stejná konvence jako
    ::smallint a ::regconfig u hybrid_search.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT ordinal, content, heading_path FROM retrieval.chunk "
            "WHERE document_id = %s::uuid AND ordinal BETWEEN %s AND %s "
            "ORDER BY ordinal", (document_id, lo, hi)).fetchall()
    return [{"ordinal": r[0], "content": r[1], "heading_path": r[2]} for r in rows]
