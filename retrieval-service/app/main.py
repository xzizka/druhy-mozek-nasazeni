"""HTTP API Retrieval Service.

Kontrakt z retrieval.container: /healthz na portu 8080.
"""
from __future__ import annotations

import logging
import threading

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config, db, indexer, rewrite
from . import lang as langs
from .infinity import Infinity, InfinityError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("retrieval")

app = FastAPI(title="Retrieval Service", version="1.0")
_inf: Infinity | None = None


@app.on_event("startup")
def _startup() -> None:
    global _inf
    db.init_pool()
    _inf = Infinity()
    log.info("start: infinity=%s model=%s rerank=%s",
             config.EMBEDDING_URL, config.EMBEDDING_MODEL, config.RERANK_MODEL)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _inf:
        _inf.close()
    db.close_pool()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    # Klíčová slova pro lexikální a fuzzy větev. Když je nepošleš, odvodí se
    # deterministicky (viz keywords.py). Kryton sem může dát lepší přepis
    # z LiteLLM aliasu `cheap`.
    keywords: str | None = None
    limit: int | None = None
    candidates: int | None = None
    rerank_top_k: int | None = None
    rerank: bool = True
    # None = podle REWRITE_ENABLED. False vynutí lokální extrakci bez LLM
    # (rychlejší o ~3,6 s, ale hrubší klíčová slova).
    rewrite: bool | None = None
    max_trust: int = 2
    # Jazyk dotazu pro lexikální větev (cs|en|de|la). Když chybí, doplní ho
    # `cheap` ve stejném volání jako klíčová slova; když ani to ne, platí
    # DEFAULT_LANG. Dense a fuzzy větev jazyk neřeší a jedou napříč vždy.
    lang: str | None = None


@app.get("/healthz")
def healthz():
    """Musí být levné — HealthCmd to volá každých 30 s."""
    ok_db = db.ping()
    if not ok_db:
        raise HTTPException(503, "databaze neodpovida")
    return {"status": "ok", "db": True, "indexing": indexer.state()["running"]}


@app.get("/readyz")
def readyz():
    ok_inf = _inf.health() if _inf else False
    if not ok_inf:
        raise HTTPException(503, "infinity neodpovida")
    return {"status": "ok", "db": db.ping(), "infinity": True}


@app.get("/stats")
def stats():
    s = db.stats()
    s["indexer"] = indexer.state()
    # Bez HNSW indexu dense větev jede sekvenčně; při větším korpusu to
    # znamená propad výkonu, který se nijak nehlásí jako chyba.
    if not s["hnsw_index_present"]:
        s["warning"] = ("chybi index chunk_embedding_hnsw — dense vetev jede "
                        "sekvencne; postav ho pres scripts/08-first-fill.sh")
    return s


@app.post("/search")
def search(req: SearchRequest):
    limit = req.limit or config.RESULT_LIMIT
    candidates = req.candidates or config.RRF_CANDIDATES
    top_k = req.rerank_top_k or config.RERANK_TOP_K

    # Explicitní jazyk se validuje tvrdě. Neznámý kód od volajícího je
    # překlep, a tiché sklouznutí na češtinu by se projevilo jen tím, že
    # lexikální větev mlčí — což vypadá jako "nic se nenašlo", ne jako chyba.
    explicit = langs.normalize(req.lang) if req.lang else None
    if req.lang and explicit is None:
        raise HTTPException(
            422, f"neznamy lang {req.lang!r}; podporovane: "
                 f"{', '.join(sorted(langs.LANGS))}")

    try:
        # Dense větev dostane embedding CELÉ otázky — s tou pracuje výborně.
        # Lexikální a fuzzy větev dostanou klíčová slova, protože
        # websearch_to_tsquery spojuje termíny AND a celá otázka nenajde nic.
        qvec = _inf.embed([req.query])[0]
    except InfinityError as e:
        raise HTTPException(502, f"embedding selhal: {e}")

    if req.keywords:
        # Volající poslal hotový přepis, takže se na `cheap` vůbec nechodí —
        # a tím pádem není odkud vzít detekovaný jazyk.
        terms, terms_source, detected = req.keywords, "caller", None
    else:
        terms, terms_source, detected = rewrite.terms(
            req.query, req.rewrite, fallback_lang=explicit)

    resolved = explicit or detected or config.DEFAULT_LANG
    lang_source = ("request" if explicit else
                   "rewrite" if detected else "default")

    fetch = max(top_k, limit) if req.rerank else limit
    hits = db.hybrid_search(db.vec_literal(qvec), terms, fetch, candidates,
                            req.max_trust, langs.ts_config(resolved))

    reranked = False
    if req.rerank and hits:
        try:
            order = _inf.rerank(req.query, [h["content"] for h in hits])
            hits = [dict(hits[i], rerank_score=score) for i, score in order]
            reranked = True
        except InfinityError as e:
            # Raději vrátit RRF pořadí než nic — ale řekni to, ať se nezdá,
            # že reranking proběhl.
            log.warning("rerank selhal, vracim RRF poradi: %s", e)

    return {"query": req.query, "keywords_used": terms,
            "keywords_source": terms_source, "lang": resolved,
            "lang_source": lang_source, "ts_config": langs.ts_config(resolved),
            "reranked": reranked, "candidates": candidates,
            "results": hits[:limit]}


@app.post("/reindex")
def reindex(background: BackgroundTasks, wait: bool = False):
    if indexer.state()["running"]:
        raise HTTPException(409, "reindex uz bezi")
    if wait:
        try:
            return indexer.reindex()
        except Exception as e:
            raise HTTPException(500, str(e))
    background.add_task(_reindex_bg)
    return {"status": "spusteno", "hint": "stav v GET /stats"}


def _reindex_bg() -> None:
    try:
        indexer.reindex()
    except Exception:
        log.exception("reindex na pozadi spadl")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
