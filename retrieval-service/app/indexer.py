"""Inkrementální reindex. Návrh a měření viz PIPELINE.md.

Embedding je jediná drahá operace (1,7-2,1 s na chunk ~2 kB) a dávkování
nepomáhá, takže celá logika stojí na tom NEEMBEDDOVAT nezměněné.

JAZYK DOKUMENTU se řeší tady, protože z něj plyne `chunk.ts_config` a tím
i celá lexikální větev. Pořadí (viz `_resolve_lang`):

  1. `lang:` ve frontmatteru — autoritativní, nikdy se nepřehlasuje
  2. uložený `document.lang`, jde-li o RESUME (shodný hash = identický obsah)
  3. detekce přes LiteLLM alias `cheap`
  4. `config.DEFAULT_LANG`

ZMĚNA JAZYKA NESTOJÍ EMBEDDINGY. `content_hash` se počítá nad syrovými bajty
souboru, takže úprava samotného `lang:` je CHANGED — ale tělo po odstranění
frontmatteru zůstane identické, recyklace v `_index_one()` klíčuje podle
obsahu chunku, a tak se přegeneruje jen `content_tsv`. To je záměr: náprava
lexikální větve má být levná.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from . import chunker, config, db, rewrite
from . import lang as langs
from .infinity import Infinity

log = logging.getLogger("indexer")

_lock = threading.Lock()
_state: dict = {"running": False, "started_at": None, "finished_at": None,
                "last_result": None, "error": None}


def state() -> dict:
    return dict(_state)


def _scan() -> dict[str, tuple[bytes, str]]:
    """source_path (relativní) -> (sha256 souboru, text)."""
    root = Path(config.MARKDOWN_ROOT)
    out = {}
    for p in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue  # .git, .obsidian a podobné
        try:
            raw = p.read_bytes()
        except OSError as e:
            log.warning("preskakuji %s: %s", p, e)
            continue
        out[str(p.relative_to(root))] = (hashlib.sha256(raw).digest(),
                                         raw.decode("utf-8", errors="replace"))
    return out


def _title(rel_path: str, text: str) -> str:
    """První nadpis H1, jinak jméno souboru."""
    for line in text.split("\n", 50)[:50]:
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return Path(rel_path).stem


def _resolve_lang(rel: str, meta: dict, body: str, existing: dict | None,
                  resumed: bool, res: dict) -> str:
    """Jazyk dokumentu. Pořadí zdrojů viz docstring modulu."""
    declared_raw = meta.get("lang")
    declared = langs.normalize(declared_raw)
    if declared:
        res["lang_sources"]["frontmatter"] += 1
        return declared
    if declared_raw:
        # Autor něco napsal, ale nedá se to přečíst. Tiché sklouznutí na
        # default by se hledalo těžko, protože se projeví až tím, že
        # lexikální větev nic nenajde.
        log.warning("%s: neznamy lang %r ve frontmatteru, urcuji jinak",
                    rel, declared_raw)

    # RESUME: hash sedí, takže obsah je bajt po bajtu stejný jako při minulém
    # běhu a uložený jazyk platí. Bez tohohle by přerušené naplnění velkého
    # korpusu detekovalo znovu úplně všechno.
    if resumed and existing and existing.get("lang"):
        res["lang_sources"]["stored"] += 1
        return existing["lang"]

    detected = rewrite.detect_language(body)
    if detected:
        res["lang_sources"]["detected"] += 1
        return detected

    res["lang_sources"]["default"] += 1
    return config.DEFAULT_LANG


def reindex() -> dict:
    """Jeden běh. Vrací souhrn. Nikdy neběží dvakrát paralelně."""
    if not _lock.acquire(blocking=False):
        raise RuntimeError("reindex uz bezi")
    _state.update(running=True, started_at=time.time(), finished_at=None, error=None)
    inf = Infinity()
    t0 = time.time()
    res = {"new": 0, "changed": 0, "unchanged": 0, "resumed": 0, "deleted": 0,
           "chunks_embedded": 0, "chunks_recycled": 0, "errors": [],
           # Kolik dokumentů skončilo v kterém jazyce a odkud ten jazyk je.
           # Levné a jinak nedohledatelné — detekce se do logu vejde, ale
           # souhrn běhu je to, na co se člověk dívá.
           "languages": {}, "lang_sources": {"frontmatter": 0, "stored": 0,
                                             "detected": 0, "default": 0}}
    try:
        files = _scan()
        docs = db.load_documents()

        # DELETED: dokument v DB, soubor už ne.
        gone = [p for p in docs if p not in files]
        res["deleted"] = db.delete_documents(gone)

        for rel, (sha, text) in files.items():
            existing = docs.get(rel)
            try:
                if existing and existing["content_hash"] == sha and existing["indexed_at"]:
                    res["unchanged"] += 1
                    continue
                kind = "new" if not existing else (
                    "resumed" if existing["content_hash"] == sha else "changed")
                _index_one(inf, rel, sha, text, existing, res)
                res[kind] += 1
            except Exception as e:                     # jeden soubor nesmí shodit běh
                log.exception("dokument %s selhal", rel)
                res["errors"].append(f"{rel}: {e}")

        # Dopočítat, co po případném pádu zůstalo bez embeddingu.
        while True:
            batch = db.pending_chunks(config.EMBED_BATCH)
            if not batch:
                break
            vecs = inf.embed([c for _, c in batch])
            db.set_chunk_embeddings([(cid, db.vec_literal(v))
                                     for (cid, _), v in zip(batch, vecs)])
            res["chunks_embedded"] += len(batch)

        db.analyze()
        res["seconds"] = round(time.time() - t0, 1)
        _state["last_result"] = res
        return res
    except Exception as e:
        _state["error"] = str(e)
        raise
    finally:
        inf.close()
        _state.update(running=False, finished_at=time.time())
        _lock.release()


def _resolve_trust(meta: dict) -> int:
    """`trust:` z frontmatteru, jinak globální TRUST_LEVEL.

    Podle komentáře u tabulky: 0 = vlastní poznámka, 1 = importované,
    2 = automatický sync z venku. Filtr v `hybrid_search` je
    `trust_level <= p_max_trust`, takže vyšší číslo znamená MENŠÍ důvěru.

    Cizí hodnota se ignoruje místo pádu: CHECK document_trust_level_ck by
    shodil INSERT a s ním celou indexaci kvůli překlepu v jednom souboru.
    """
    raw = meta.get("trust")
    if raw is None:
        return config.TRUST_LEVEL
    try:
        val = int(str(raw).strip())
    except ValueError:
        log.warning("neplatny trust %r ve frontmatteru, beru %s", raw,
                    config.TRUST_LEVEL)
        return config.TRUST_LEVEL
    if not 0 <= val <= 2:
        log.warning("trust %s mimo rozsah 0-2, beru %s", val, config.TRUST_LEVEL)
        return config.TRUST_LEVEL
    return val


def _index_one(inf: Infinity, rel: str, sha: bytes, text: str,
               existing: dict | None, res: dict) -> None:
    # Frontmatter pryč PŘED chunkováním, jinak by se `lang: en` a spol. staly
    # obsahem dokumentu a skončily v embeddingu i v content_tsv.
    meta, body = chunker.split_frontmatter(text)
    resumed = bool(existing) and existing["content_hash"] == sha
    doc_lang = _resolve_lang(rel, meta, body, existing, resumed, res)
    res["languages"][doc_lang] = res["languages"].get(doc_lang, 0) + 1

    doc_id = db.upsert_document(rel, _title(rel, body), sha,
                                _resolve_trust(meta), doc_lang)

    # Recyklace: mapa content -> embedding ze starých chunků TÉHOŽ dokumentu.
    # Klíčem je obsah, ne ordinal — viz db.document_chunk_embeddings.
    recycle = db.document_chunk_embeddings(doc_id) if existing else {}

    chunks = chunker.chunk_markdown(body)
    rows, to_embed = [], []
    for ch in chunks:
        vec = recycle.get(ch.content)
        if vec is not None:
            res["chunks_recycled"] += 1
        else:
            to_embed.append(len(rows))
        rows.append([ch.ordinal, ch.content, ch.heading_path, vec])

    # Embedduj chybějící po dávkách. Kdyby to spadlo, chunky zůstanou
    # s embedding IS NULL a indexed_at NULL, takže příští běh je dopočítá.
    for i in range(0, len(to_embed), config.EMBED_BATCH):
        idxs = to_embed[i:i + config.EMBED_BATCH]
        vecs = inf.embed([rows[j][1] for j in idxs])
        for j, v in zip(idxs, vecs):
            rows[j][3] = db.vec_literal(v)
        res["chunks_embedded"] += len(idxs)

    db.replace_chunks(doc_id, [tuple(r) for r in rows], langs.ts_config(doc_lang))
    if all(r[3] is not None for r in rows):
        db.finish_document(doc_id, len(rows))
