"""Context window expansion: k výsledkům hledání dotáhne sousední chunky.

`hybrid_search` i reranker hodnotí chunky izolovaně — vrátí ten kousek textu,
který vyhrál, bez ohledu na to, že sousední chunk (`ordinal` ± 1 ve stejném
dokumentu) může nést druhou polovinu téže myšlenky. Chunkování je bez
overlapu (viz chunker.py), takže hranice chunku je čistě otázka rozpočtu
CHUNK_CHARS, ne významu — a přesně na ní se dá odpověď rozseknout.

Řeší se JEN na finálních výsledcích, po rerankingu a slicingu na `limit`
(viz main.py), ne dřív: reranker je na tomhle systému změřený na atomických
chuncích (NASAZENI.md, bod 2 dalších kroků), a předřazení expanze by mu
poslalo jiný vstup, než na jaký byl vybraný.

Sousedící a překrývající se okna z více hitů TÉHOŽ dokumentu se slučují do
jednoho souvislého bloku, aby model nedostal duplicitní text a aby dva
hity ze dvou sousedních odstavců jedné pasáže nesplynuly zpátky ve dva
oddělené zdroje. Počet vrácených výsledků proto může klesnout pod původní
`limit` — to je záměr, promítá skutečnou strukturu poznámek, ne chyba.
"""
from __future__ import annotations

from typing import Callable


def _score(hit: dict) -> float:
    """`rerank_score`, když proběhl rerank, jinak `rrf_score`. 0.0 je bezpečný
    default pro merged výsledek, kde by čistě teoreticky obojí chybělo."""
    v = hit.get("rerank_score")
    if v is None:
        v = hit.get("rrf_score")
    return float(v) if v is not None else 0.0


def plan_ranges(hits: list[dict], window: int) -> list[dict]:
    """Hity -> skupiny ke sloučení. Čistá funkce, bez I/O — testuj napřímo.

    Skupina je `{"document_id", "lo", "hi", "members"}`. Okna
    (`ordinal - window` .. `ordinal + window`) ve stejném dokumentu, která
    se DOTÝKAJÍ nebo PŘEKRÝVAJÍ, splynou do jedné skupiny s jedním spojitým
    rozsahem `[lo, hi]` — jeden izolovaný hit je skupina o jednom členovi
    se svým vlastním oknem. Pořadí skupin ve výstupu odpovídá pořadí, v jakém
    se první hit dané skupiny objevil v `hits`; `expand()` je pak přeřadí
    podle skóre.
    """
    by_doc: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for h in hits:
        doc = h["document_id"]
        if doc not in by_doc:
            by_doc[doc] = []
            order.append(doc)
        by_doc[doc].append((h["ordinal"], h))

    groups: list[dict] = []
    for doc in order:
        items = sorted(by_doc[doc], key=lambda t: t[0])
        lo = hi = None
        members: list[dict] = []
        for ordinal, h in items:
            h_lo, h_hi = max(0, ordinal - window), ordinal + window
            if lo is None:
                lo, hi, members = h_lo, h_hi, [h]
            elif h_lo <= hi + 1:              # dotyk nebo prekryv -> slouc
                hi = max(hi, h_hi)
                members.append(h)
            else:
                groups.append({"document_id": doc, "lo": lo, "hi": hi, "members": members})
                lo, hi, members = h_lo, h_hi, [h]
        if lo is not None:
            groups.append({"document_id": doc, "lo": lo, "hi": hi, "members": members})
    return groups


def expand(hits: list[dict], window: int,
          fetch_range: Callable[[str, int, int], list[dict]]) -> list[dict]:
    """Sloučené hity, seřazené sestupně podle skóre nejlepšího člena skupiny.

    `fetch_range(document_id, lo, hi)` vrátí chunky dokumentu v tom rozsahu
    ordinalu jako `[{"ordinal", "content", "heading_path"}, ...]`, seřazené
    podle ordinalu (viz `db.fetch_chunk_range`).

    Merged hit nese metadata (skóre, `chunk_id`, `trust_level`, ...) od
    ANCHORU — nejlépe skórujícího původního hitu ve skupině — a jen
    `content`/`heading_path` nahrazuje sloučenou verzí. `chunk_id` v citaci
    tedy zůstává tím chunkem, který o dotazu skutečně rozhodl; `content`
    kolem něj nese víc.
    """
    groups = plan_ranges(hits, window)
    out = []
    for g in groups:
        rows = fetch_range(g["document_id"], g["lo"], g["hi"])
        anchor = max(g["members"], key=_score)
        if not rows:
            # Nemelo by nastat - anchor svuj vlastni chunk najde vzdy - ale
            # padat kvuli chybejicimu textu okoli je zbytecne.
            out.append(anchor)
            continue
        headings: list[str] = []
        seen: set[str] = set()
        for r in rows:
            hp = r.get("heading_path")
            if hp and hp not in seen:
                seen.add(hp)
                headings.append(hp)
        merged = dict(anchor)
        merged["content"] = "\n\n".join(r["content"] for r in rows)
        merged["heading_path"] = " | ".join(headings) if headings else anchor.get("heading_path")
        merged["ordinal_range"] = [g["lo"], g["hi"]]
        merged["merged_chunk_ids"] = sorted({m["chunk_id"] for m in g["members"]})
        out.append(merged)
    out.sort(key=_score, reverse=True)
    return out
