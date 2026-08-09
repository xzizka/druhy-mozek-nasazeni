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


def stats() -> dict:
    with _pool.connection() as conn:
        c = conn.execute("SELECT count(*) FROM conversation").fetchone()[0]
        m = conn.execute("SELECT count(*) FROM message").fetchone()[0]
        f = conn.execute("SELECT count(*) FROM feedback").fetchone()[0]
        i = conn.execute("SELECT count(*) FROM inbox WHERE processed_at IS NULL").fetchone()[0]
    return {"conversations": c, "messages": m, "feedback": f, "inbox_open": i}
