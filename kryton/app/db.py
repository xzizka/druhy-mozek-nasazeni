"""Databáze Krytona + migrace.

Proti retrievalu je to obráceně: DB `kryton` vlastní `kryton_app` a smí v ní
zakládat tabulky (ověřeno), takže si schéma spravuje služba sama. Retrieval
naopak schéma vlastní nesmí a migrace tam pouští superuser.

Migrace jsou idempotentní CREATE IF NOT EXISTS a běží při startu. Pro tři
tabulky je verzovaná migrační tabulka zbytečná režie.
"""
from __future__ import annotations

import json
import uuid

from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None

SCHEMA = """
-- Historie konverzací. Ukládá i použité citace, protože bez nich se nedá
-- zpětně ověřit, z čeho odpověď vznikla.
CREATE TABLE IF NOT EXISTS conversation (
    id          uuid PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    title       text
);

CREATE TABLE IF NOT EXISTS message (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user','assistant')),
    content         text NOT NULL,
    -- citace = [{source_path, heading_path, chunk_id, rerank_score}, ...]
    citations       jsonb NOT NULL DEFAULT '[]'::jsonb,
    model           text,
    latency_ms      int,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS message_conversation_ix ON message (conversation_id, id);

-- Zpětná vazba pro eval loop. README plánuje ve fázi 2 Langfuse datasety
-- s ~50 anotovanými dotazy; tohle je způsob, jak ta data sbírat průběžně
-- místo zpětně.
CREATE TABLE IF NOT EXISTS feedback (
    message_id  bigint PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
    rating      smallint NOT NULL CHECK (rating IN (-1, 1)),
    -- indexy citací, které byly skutečně užitečné
    useful      jsonb NOT NULL DEFAULT '[]'::jsonb,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Fronta zachycených poznámek: co jsi uložil, ale ještě nezpracoval.
CREATE TABLE IF NOT EXISTS inbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_path  text NOT NULL,
    excerpt      text,
    processed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inbox_open_ix ON inbox (created_at) WHERE processed_at IS NULL;

-- Spočítané metriky nad korpusem (P1b). Spočítá se automaticky, ale do
-- /korpus se dostane až připnutím (`pinned_at`) — model umí napsat SQL,
-- které je syntakticky v pořádku a sémanticky mimo, a /korpus je právě ta
-- stránka, proti které se halucinace poměřuje.
--
-- `fingerprint` je stav korpusu při výpočtu. Bez něj by se z /korpus stalo
-- muzeum zastaralých čísel, protože korpus se mění každou indexací.
--
-- ON DELETE SET NULL, ne CASCADE: smazání konverzace nesmí odnést metriku,
-- kterou sis mezitím připnul na /korpus.
CREATE TABLE IF NOT EXISTS metric (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid REFERENCES conversation(id) ON DELETE SET NULL,
    question        text NOT NULL,
    label           text,
    sql_text        text NOT NULL,
    result_cols     jsonb NOT NULL DEFAULT '[]'::jsonb,
    result_rows     jsonb NOT NULL DEFAULT '[]'::jsonb,
    fingerprint     text NOT NULL DEFAULT '',
    computed_at     timestamptz NOT NULL DEFAULT now(),
    pinned_at       timestamptz
);
CREATE INDEX IF NOT EXISTS metric_pinned_ix ON metric (pinned_at)
    WHERE pinned_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS metric_conv_ix ON metric (conversation_id);

-- Nahrané dokumenty (P2): mapa mezi textem v indexu a originálem na S3.
--
-- Proč tady a ne v `retrieval.document.meta`: ten sloupec sice existuje, ale
-- indexer do něj nikdy nic nezapisuje a Kryton do databáze retrievalu psát
-- nesmí (má tam jen SELECT přes platform_ro). Držet mapu tady je navíc
-- výhodnější pro migraci mezi úložišti — je to UPDATE řádků, ne přepis
-- frontmatteru ve stovkách souborů a reindex.
--
-- `s3_profile` je tu právě kvůli migraci: bez něj by po přepnutí endpointu
-- nešlo poznat, který originál leží kde.
CREATE TABLE IF NOT EXISTS upload (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_path   text NOT NULL UNIQUE,
    original_name text NOT NULL,
    mime          text,
    size_bytes    bigint NOT NULL,
    sha256        text NOT NULL,
    s3_profile    text NOT NULL,
    s3_bucket     text NOT NULL,
    s3_key        text NOT NULL,
    encoding      text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS upload_sha_ix ON upload (sha256);
"""


def init() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=4,
                               kwargs={"autocommit": True}, open=True)
    with _pool.connection() as conn:
        conn.execute(SCHEMA)


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ping() -> bool:
    try:
        with _pool.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def new_conversation(title: str) -> uuid.UUID:
    cid = uuid.uuid4()
    with _pool.connection() as conn:
        conn.execute("INSERT INTO conversation (id, title) VALUES (%s, %s)",
                     (cid, title[:200]))
    return cid


def add_message(conversation_id, role: str, content: str, citations=None,
                model: str = None, latency_ms: int = None) -> int:
    with _pool.connection() as conn:
        return conn.execute(
            "INSERT INTO message (conversation_id, role, content, citations, model, latency_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (conversation_id, role, content, json.dumps(citations or []),
             model, latency_ms)).fetchone()[0]


def conversations(limit: int = 50) -> list[dict]:
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, count(m.id) "
            "FROM conversation c LEFT JOIN message m ON m.conversation_id = c.id "
            "GROUP BY c.id ORDER BY c.created_at DESC LIMIT %s", (limit,)).fetchall()
    return [{"id": str(r[0]), "title": r[1], "created_at": r[2], "messages": r[3]}
            for r in rows]


def messages(conversation_id) -> list[dict]:
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.citations, m.model, m.latency_ms, "
            "       m.created_at, f.rating "
            "FROM message m LEFT JOIN feedback f ON f.message_id = m.id "
            "WHERE m.conversation_id = %s ORDER BY m.id", (conversation_id,)).fetchall()
    return [{"id": r[0], "role": r[1], "content": r[2], "citations": r[3],
             "model": r[4], "latency_ms": r[5], "created_at": r[6], "rating": r[7]}
            for r in rows]


def delete_conversation(conversation_id) -> int:
    """Smaže konverzaci i s obsahem. Vrací počet smazaných řádků (0 = nebyla).

    Zprávy a hodnocení jdou s ní: `message.conversation_id` má ON DELETE
    CASCADE na conversation a `feedback.message_id` na message, takže stačí
    smazat kořen a nezůstanou sirotci.
    """
    with _pool.connection() as conn:
        return conn.execute("DELETE FROM conversation WHERE id = %s",
                            (conversation_id,)).rowcount


def set_feedback(message_id: int, rating: int, useful=None, note: str = None) -> None:
    with _pool.connection() as conn:
        conn.execute(
            "INSERT INTO feedback (message_id, rating, useful, note) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (message_id) DO UPDATE SET rating = EXCLUDED.rating, "
            "  useful = EXCLUDED.useful, note = EXCLUDED.note, created_at = now()",
            (message_id, rating, json.dumps(useful or []), note))


def add_inbox(source_path: str, excerpt: str) -> None:
    with _pool.connection() as conn:
        conn.execute("INSERT INTO inbox (source_path, excerpt) VALUES (%s, %s)",
                     (source_path, (excerpt or "")[:500]))


def inbox_open(limit: int = 100) -> list[dict]:
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, source_path, excerpt, created_at FROM inbox "
            "WHERE processed_at IS NULL ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [{"id": r[0], "source_path": r[1], "excerpt": r[2], "created_at": r[3]}
            for r in rows]


def inbox_done(item_id: int) -> None:
    with _pool.connection() as conn:
        conn.execute("UPDATE inbox SET processed_at = now() WHERE id = %s", (item_id,))


def _metric_row(r) -> dict:
    return {"id": r[0], "conversation_id": str(r[1]) if r[1] else None,
            "question": r[2], "label": r[3], "sql": r[4], "cols": r[5],
            "rows": r[6], "fingerprint": r[7], "computed_at": r[8],
            "pinned_at": r[9]}


_METRIC_COLS = ("id, conversation_id, question, label, sql_text, result_cols, "
                "result_rows, fingerprint, computed_at, pinned_at")


def add_metric(conversation_id, question: str, sql: str, cols, rows,
               fingerprint: str) -> int:
    with _pool.connection() as conn:
        return conn.execute(
            "INSERT INTO metric (conversation_id, question, sql_text, "
            "  result_cols, result_rows, fingerprint) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (conversation_id, question[:500], sql, json.dumps(cols or []),
             json.dumps(rows or []), fingerprint or "")).fetchone()[0]


def metrics_for_conversation(conversation_id) -> list[dict]:
    """Nepřipnuté metriky konverzace — u nich se nabízí tlačítko Připnout."""
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT " + _METRIC_COLS + " FROM metric "
            "WHERE conversation_id = %s AND pinned_at IS NULL ORDER BY id",
            (conversation_id,)).fetchall()
    return [_metric_row(r) for r in rows]


def pinned_metrics() -> list[dict]:
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT " + _METRIC_COLS + " FROM metric "
            "WHERE pinned_at IS NOT NULL ORDER BY pinned_at").fetchall()
    return [_metric_row(r) for r in rows]


def pin_metric(metric_id: int, label: str = None) -> None:
    with _pool.connection() as conn:
        conn.execute("UPDATE metric SET pinned_at = now(), "
                     "label = coalesce(nullif(%s,''), label) WHERE id = %s",
                     (label, metric_id))


def delete_metric(metric_id: int) -> int:
    with _pool.connection() as conn:
        return conn.execute("DELETE FROM metric WHERE id = %s",
                            (metric_id,)).rowcount


def update_metric_result(metric_id: int, cols, rows, fingerprint: str) -> None:
    """Přepočet připnuté metriky — nové číslo, nový otisk, nový čas."""
    with _pool.connection() as conn:
        conn.execute(
            "UPDATE metric SET result_cols=%s, result_rows=%s, fingerprint=%s, "
            "computed_at=now() WHERE id=%s",
            (json.dumps(cols or []), json.dumps(rows or []),
             fingerprint or "", metric_id))


def add_upload(source_path: str, original_name: str, mime: str,
               size_bytes: int, sha256: str, s3_profile: str, s3_bucket: str,
               s3_key: str, encoding: str) -> int:
    """Zapíše nahraný soubor. Opakované nahrání téhož obsahu i názvu vede
    na stejný `source_path`, takže se řádek jen aktualizuje."""
    with _pool.connection() as conn:
        return conn.execute(
            "INSERT INTO upload (source_path, original_name, mime, size_bytes, "
            "  sha256, s3_profile, s3_bucket, s3_key, encoding) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (source_path) DO UPDATE SET "
            "  original_name = EXCLUDED.original_name, mime = EXCLUDED.mime, "
            "  size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256, "
            "  s3_profile = EXCLUDED.s3_profile, s3_bucket = EXCLUDED.s3_bucket, "
            "  s3_key = EXCLUDED.s3_key, encoding = EXCLUDED.encoding, "
            "  created_at = now() RETURNING id",
            (source_path, original_name, mime, size_bytes, sha256,
             s3_profile, s3_bucket, s3_key, encoding)).fetchone()[0]


def uploads(limit: int = 200) -> list[dict]:
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, source_path, original_name, mime, size_bytes, sha256, "
            "       s3_profile, s3_bucket, s3_key, encoding, created_at "
            "FROM upload ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [{"id": r[0], "source_path": r[1], "original_name": r[2],
             "mime": r[3], "size_bytes": r[4], "sha256": r[5],
             "s3_profile": r[6], "s3_bucket": r[7], "s3_key": r[8],
             "encoding": r[9], "created_at": r[10]} for r in rows]


def stats() -> dict:
    with _pool.connection() as conn:
        c = conn.execute("SELECT count(*) FROM conversation").fetchone()[0]
        m = conn.execute("SELECT count(*) FROM message").fetchone()[0]
        f = conn.execute("SELECT count(*) FROM feedback").fetchone()[0]
        i = conn.execute("SELECT count(*) FROM inbox WHERE processed_at IS NULL").fetchone()[0]
    return {"conversations": c, "messages": m, "feedback": f, "inbox_open": i}
