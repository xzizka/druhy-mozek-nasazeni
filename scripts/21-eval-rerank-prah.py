#!/usr/bin/env python3
"""Práh na `rerank_score` jako signál „nic relevantního jsem nenašel".

Podklad k rozhodnutí o P4 a P7-B (`POZADAVKY.md`). Obojí je tentýž vzorec:
retrieval vrátí šum, model nad šumem odpoví sebejistě. U P4 si vymyslel
citaci, u P7-B tvrdil „žádné záznamy" o dni, který v indexu je. Reranker
ten rozdíl přitom zná — 2026-08-17 změřeno 0,983 u trefy proti 0,021
u temporálního dotazu — jen ho pipeline zahodí.

ČÍM SE LIŠÍ OD `16-rerank-value.py`: ten měří KVALITU ŘAZENÍ a dotazy si
generuje z korpusu (slova z chunků přes SQL), takže každý dotaz má lexikální
kotvu — proto jeho závěr „rerank nepřinesl nic" platí jen pro lexikální
dotazy. Tenhle skript měří skóre jako MÍRU JISTOTY a potřebuje přesně ty
kategorie, které autosampling vyrobit neumí: dotaz bez kotvy, dotaz na
něco, co v korpusu není, a dotaz vágně formulovaný na něco, co tam JE.
Sada je proto ručně označkovaná, ne vzorkovaná.

TŘI PASTI, KTERÉ TENHLE SKRIPT VĚDOMĚ OŠETŘUJE:

1. **`rrf_score` a nízká `rerank_score` se ROZSAHEM PŘEKRÝVAJÍ.** RRF fúze
   dává vždy ~0,016–0,033 (funkce pořadí, ne podobnosti), a nízká rerank
   skóre leží v 0,00002–0,021. Kód se zálohou `h.get("rerank_score") or
   h.get("rrf_score")` by tedy tiše porovnával nesouměřitelná čísla a práh
   by vyšel nesmyslně. Skript čte VÝHRADNĚ `rerank_score` a když chybí,
   spadne nahlas.
2. **Diakritika mění výsledky** („delal vcera" a „dělal včera" daly jiné
   #1), takže se temporální dotazy měří v obou variantách.
3. **Cross-encoder skóruje pár (dotaz, chunk) NEZÁVISLE**, nenormalizuje
   přes kandidáty. Práh je proto přenositelný mezi velikostmi korpusu
   podstatně lépe než metriky pořadí — ale viz varování o středním pásmu
   ve výstupu.

CO TENHLE SKRIPT NAD DNEŠNÍM KORPUSEM ZMĚŘIT NEMŮŽE: korpus má 13 dokumentů
(zátěžový korpus je smazaný od 2026-08-10), takže STŘEDNÍ PÁSMO skóre
(~0,05–0,5) — tedy zásahy slabé, ale ještě užitečné — v něm skoro nemá jak
vzniknout. Práh odvozený jen odsud je proto vědomě KONZERVATIVNÍ: řezat se
má jen tam, kde je jistota vysoká. Až korpus poroste, pusť skript znovu.

Spouští se na brainu (potřebuje síť na retrieval; `httpx` na hostiteli není,
proto stdlib urllib):

    /root/deploy/scripts/21-eval-rerank-prah.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.error
import urllib.request

RS = os.environ.get("RETRIEVAL_URL", "http://10.89.7.13:8080")
LIMIT = int(os.environ.get("LIMIT", "8"))

# Kandidáti na práh. Řídké dole (tam se rozhoduje) a hrubé nahoře.
PRAHY = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7)

# ---------------------------------------------------------------------------
# Sada dotazů. `prejit=True` znamená „tenhle dotaz MÁ projít" — tedy odpověď
# z poznámek existuje a práh, který ho utne, škodí. `prejit=False` znamená
# „tady se má přiznat, že nic není".
#
# Dotazy s kotvou míří na obsah, který v korpusu doopravdy je:
#   01-cesky.md  = ladění vektorových indexů (HNSW, ef_search,
#                  maintenance_work_mem, spill-to-disk)
#   denik/…      = osobní zápisy
# ---------------------------------------------------------------------------
DOTAZY = [
    # --- kotva: jednoznačné, MUSÍ přežít každý práh --------------------
    ("kotva", True, "maintenance_work_mem při stavbě indexu"),
    ("kotva", True, "co dělá ef_search"),
    ("kotva", True, "HNSW parametry m a ef_construction"),

    # --- vágní, ale odpověď v korpusu JE: riziková zóna prahu ----------
    # Tady se láme, jestli práh škodí. Formulace záměrně nepoužívá slova
    # z textu — uživatel se takhle ptá běžně.
    ("vagni", True, "proč mi stavba indexu trvá tak dlouho"),
    ("vagni", True, "jak zrychlit dotazy do databáze"),
    ("vagni", True, "co se nedá změnit po vytvoření indexu"),

    # --- P7-B: temporální, bez kotvy. Obě varianty diakritiky ----------
    ("temporal", False, "Co jsem dělal včera?"),
    ("temporal", False, "Co jsem delal vcera?"),
    ("temporal", False, "Jaké jsou poslední záznamy v deníku?"),
    ("temporal", False, "Jaké jsou nejnovější poznámky?"),

    # --- P1: agregační. Odpověď patří /korpus, ne RAGu -----------------
    ("agregacni", False, "Kolik mám dokumentů?"),
    ("agregacni", False, "Kolik je kterých knih podle jazyka?"),

    # --- P4: fakt nad už citovanými entitami ---------------------------
    ("fakt-p4", False, "Které z těch zákonů vznikly před rokem 2019?"),

    # --- v korpusu prokazatelně NENÍ -----------------------------------
    ("chybi", False, "recept na bramborový salát"),
    ("chybi", False, "kdy mi jede vlak do Berlína"),
    ("chybi", False, "jaké mám heslo k routeru"),
]


def search(query: str) -> dict:
    body = json.dumps({"query": query, "limit": LIMIT}).encode()
    req = urllib.request.Request(RS + "/search", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        sys.exit("retrieval %s nedostupny: %s" % (RS, e))


def skore(d: dict, query: str) -> list[float]:
    """Výhradně `rerank_score`. Past #1: kdyby se tady mlčky sáhlo na
    `rrf_score`, míchala by se nesouměřitelná čísla ze stejného rozsahu."""
    if not d.get("reranked"):
        sys.exit("dotaz %r se NEREANKOVAL (reranked=%r) — bez rerank skore\n"
                 "nema tohle mereni smysl; zkontroluj RERANK_* v retrievalu"
                 % (query, d.get("reranked")))
    out = []
    for h in d.get("results", []):
        s = h.get("rerank_score")
        if s is None:
            sys.exit("chunk %s u dotazu %r nema rerank_score — viz past #1 "
                     "v docstringu" % (h.get("chunk_id"), query))
        out.append(float(s))
    return out


def main() -> int:
    print("=" * 78)
    print("PRAH NA RERANK_SCORE — podklad k P4 a P7-B")
    print("retrieval: %s   limit: %d   dotazu: %d" % (RS, LIMIT, len(DOTAZY)))

    stats = json.load(urllib.request.urlopen(RS + "/stats", timeout=30))
    print("korpus: %s dokumentu, %s chunku"
          % (stats.get("documents"), stats.get("chunks")))
    print("=" * 78)

    mereni = []
    for kat, prejit, q in DOTAZY:
        d = search(q)
        sc = skore(d, q)
        top = max(sc) if sc else 0.0
        cesta = d["results"][0]["source_path"] if d.get("results") else "—"
        mereni.append({"kat": kat, "prejit": prejit, "q": q,
                       "top": top, "n": len(sc), "cesta": cesta, "sc": sc})
        print("%-10s %-5s top=%.6f  n=%d  %-26s %s"
              % (kat, "PROJIT" if prejit else "REZAT", top, len(sc),
                 cesta[:26], q[:34]))

    # --- rozdělení skóre ---------------------------------------------------
    maji_projit = [m["top"] for m in mereni if m["prejit"]]
    maji_rezat = [m["top"] for m in mereni if not m["prejit"]]
    print()
    print("-" * 78)
    print("ROZDELENI NEJLEPSICH SKORE")
    print("  ma projit (n=%d): min %.6f  median %.6f  max %.6f"
          % (len(maji_projit), min(maji_projit),
             statistics.median(maji_projit), max(maji_projit)))
    print("  ma rezat  (n=%d): min %.6f  median %.6f  max %.6f"
          % (len(maji_rezat), min(maji_rezat),
             statistics.median(maji_rezat), max(maji_rezat)))
    mezera = min(maji_projit) - max(maji_rezat)
    print("  MEZERA mezi nejhorsim 'projit' a nejlepsim 'rezat': %+.6f" % mezera)
    if mezera <= 0:
        print("  !! PREKRYV — zadny prah nerozdeli sadu bez chyby. Viz nize.")

    # --- co by dělal který práh -------------------------------------------
    print()
    print("-" * 78)
    print("CO BY UDELAL KTERY PRAH  (rez = vsechny chunky pod prahem zahodit)")
    print("%8s  %14s  %14s  %s" % ("prah", "chybne rezy", "spravne rezy", "verdikt"))
    nejlepsi = None
    for p in PRAHY:
        chybne = [m for m in mereni if m["prejit"] and m["top"] < p]
        spravne = [m for m in mereni if not m["prejit"] and m["top"] < p]
        verdikt = "OK" if not chybne else "SKODI (%s)" % ", ".join(
            m["kat"] for m in chybne)[:28]
        print("%8.2f  %8d /%4d  %8d /%4d  %s"
              % (p, len(chybne), len(maji_projit),
                 len(spravne), len(maji_rezat), verdikt))
        if not chybne and (nejlepsi is None or len(spravne) > nejlepsi[1]):
            nejlepsi = (p, len(spravne))

    # --- střední pásmo: co tenhle korpus NEUMÍ říct ------------------------
    stred = [s for m in mereni for s in m["sc"] if 0.05 <= s <= 0.5]
    vse = [s for m in mereni for s in m["sc"]]
    print()
    print("-" * 78)
    print("STREDNI PASMO (0,05-0,5): %d z %d vsech skore" % (len(stred), len(vse)))
    if len(stred) < 5:
        print("  Prakticky prazdne — dnesni korpus (13 dokumentu) slabe-ale-")
        print("  uzitecne zasahy skoro nevyrabi. Prah odsud je proto")
        print("  KONZERVATIVNI a PROVIZORNI: rez jen tam, kde je jistota velka,")
        print("  a po narustu korpusu pust skript znovu.")

    print()
    print("=" * 78)
    if nejlepsi:
        print("NAVRH: prah %.2f — utne %d/%d dotazu, ktere utnout ma,"
              % (nejlepsi[0], nejlepsi[1], len(maji_rezat)))
        print("       a ZADNY z %d, ktere projit maji." % len(maji_projit))
    else:
        print("NAVRH: zadny z testovanych prahu nerozdeli sadu bez skody.")
    print("=" * 78)
    print()
    print("POZOR: skript NIC NEMENI. Prah se nikde nepouziva, dokud se")
    print("cislo neschvali a nedopise do retrievalu/Krytona.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
