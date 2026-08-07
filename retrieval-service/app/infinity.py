"""Klient Infinity — embeddings a reranking.

Vědomě jde přímo na Infinity, ne přes LiteLLM. README to zdůvodňuje:
bulk indexace by z gateway udělala bottleneck a query-time embedding
nesnese hop navíc.
"""
from __future__ import annotations

import httpx

from . import config


class InfinityError(RuntimeError):
    pass


class Infinity:
    def __init__(self, base_url: str = None, timeout: float = None):
        self._client = httpx.Client(
            base_url=(base_url or config.EMBEDDING_URL).rstrip("/"),
            timeout=timeout or config.HTTP_TIMEOUT,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            return self._client.get("/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Vrátí embeddingy ve stejném pořadí jako vstup.

        Infinity vrací pole s indexy, na jejichž pořadí se nedá spoléhat,
        proto se řadí podle `index`.
        """
        if not texts:
            return []
        r = self._client.post("/embeddings",
                              json={"model": config.EMBEDDING_MODEL, "input": texts})
        if r.status_code != 200:
            raise InfinityError(f"/embeddings {r.status_code}: {r.text[:200]}")
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vecs = [d["embedding"] for d in data]
        if len(vecs) != len(texts):
            raise InfinityError(f"cekal jsem {len(texts)} vektoru, dostal {len(vecs)}")
        for v in vecs:
            if len(v) != config.EMBED_DIM:
                raise InfinityError(
                    f"model vraci {len(v)} dimenzi, sloupec chunk.embedding je "
                    f"halfvec({config.EMBED_DIM}) — neshoda modelu a schematu")
        return vecs

    def embed_batched(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), config.EMBED_BATCH):
            out.extend(self.embed(texts[i:i + config.EMBED_BATCH]))
        return out

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """Vrátí [(index_do_documents, skore)] seřazené od nejrelevantnějšího.

        POZOR na latenci: měřeno s bge-reranker-v2-m3 na CPU roste lineárně —
        10 kandidátů 2,0 s, 20 kandidátů 3,8 s, 60 kandidátů 12,6 s.
        RERANK_TOP_K je tedy přímý regulátor latence dotazu.
        """
        if not documents:
            return []
        r = self._client.post("/rerank",
                              json={"model": config.RERANK_MODEL,
                                    "query": query, "documents": documents})
        if r.status_code != 200:
            raise InfinityError(f"/rerank {r.status_code}: {r.text[:200]}")
        res = r.json()["results"]
        return sorted(((d["index"], d["relevance_score"]) for d in res),
                      key=lambda t: -t[1])
