#!/usr/bin/env python3
"""Přínos rerankingu nad SKUTEČNÝMI poznámkami — párově, přirozené otázky.

Doplňuje `16-rerank-value.py`, jehož závěr ("bez reranku 94 % za 2,26 s,
s rerankem 85 % za 14-24 s") **nelze na tenhle režim použít**: dotazy tam
byly pytle šesti dlouhých slov opsaných z cílového chunku, tedy ideální
vstup pro lexikální větev a skoro nejhorší možný pro sémantický
cross-encoder. Zátěžový korpus, nad kterým to běželo, byl navíc 2026-08-10
smazán.

Teď je v indexu 10 skutečných deníkových záznamů (2026-08-10 až 08-18),
nad kterými sada přirozených otázek se známým cílovým dokumentem vyrobit
JDE. Otázky níž jsou psané tak, aby se s textem záznamu lexikálně
překrývaly co nejmíň ("Bál jsem se, že přijdu o zaměstnání?" proti záznamu
o přesunu práce do Indie) — tedy přesně režim, kde má reranker pomáhat.

**Proč se posílají hotová `keywords` a `lang`:**
Bez nich by každé volání `/search` šlo na alias `cheap` pro přepis. Dvě
ramena měření by pak mohla dostat různá klíčová slova a neměřil by se
rerank, ale rozptyl LLM. Skript proto nejdřív jedním voláním s přepisem
zjistí `keywords_used` + `lang` a do OBOU měřených ramen je pošle
explicitně. Vedlejší efekt: z měřené cesty zmizí LLM i jeho hodinová
cache, takže naměřená latence je čistě retrieval + rerank.

**Co se tiskne:** celé pořadí výsledků u každého dotazu, ne jen skóre.
Metrika nad LLM výstupem i nad pořadím svádí k závěru, který se pak
neopírá o nic — 2026-08-18 to takhle selhalo třikrát za den.

Spouští se na brainu (potřebuje síť na podman IP retrievalu):

    /root/deploy/scripts/28-rerank-value-denik.py

Doba běhu: 14 dotazů x (0,2 s bez reranku + ~6 s top_k 10 + ~11 s top_k 20)
plus jeden přepis na dotaz, tedy zhruba pět minut.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
import urllib.request

RS = os.environ.get("RETRIEVAL_URL", "http://10.89.7.13:8080")
LIMIT = 8

# (popisek, tělo navíc k /search). BASELINE je první.
CONFIGS = [
    ("bez reranku", {"rerank": False}),
    ("rerank 10", {"rerank": True, "rerank_top_k": 10}),
    ("rerank 20", {"rerank": True, "rerank_top_k": 20}),
]
BASELINE = "bez reranku"

# (otázka, {cílové dokumenty}). Cíle jsou určené ČETBOU záznamů, ne odhadem;
# kde na otázku legitimně odpovídají dva záznamy, jsou v množině oba.
DOTAZY = [
    ("Bál jsem se, že přijdu o zaměstnání?",
     {"denik/2026-08-10.md"}),
    ("Co mi napsal starosta zpátky?",
     {"denik/2026-08-11.md"}),
    ("Kdy bylo vidět zatmění Slunce?",
     {"denik/2026-08-12.md"}),
    ("Na co jsem napojil n8n?",
     {"denik/2026-08-12.md"}),
    ("Kvůli které komponentě byly špatně nastavené síťové politiky?",
     {"denik/2026-08-13.md"}),
    ("Jak se instaluje Oracle 26?",
     {"denik/2026-08-14.md"}),
    ("Co mě zaskočilo na mém synovi?",
     {"denik/2026-08-15.md"}),
    ("Proč jsem v noci nemohl usnout?",
     {"denik/2026-08-17.md"}),
    ("Co potřebuje Filenet, aby běžel v Kubernetes?",
     {"denik/2026-08-17.md"}),
    ("Kdy mi vytkli, že jsem pomalý?",
     {"denik/2026-08-18.md"}),
    ("Co obnáší bojovat za nějakou věc?",
     {"denik/2026-08-16.md", "denik/2026-08-10.md"}),
    ("Čeho se týká moje žádost zastupitelstvu obce?",
     {"_uploads/broumy-zastupitelstvo-zadost-6c0932c1.md"}),
    ("Jaký nástroj mi pomohl najít problém v práci?",
     {"denik/2026-08-13.md"}),
    ("Co jsem si zapsal o rozvoji obce, kde bydlím?",
     {"denik/2026-08-10.md"}),
]


def search(body: dict, timeout: float = 300) -> tuple[float, dict]:
    req = urllib.request.Request(
        RS + "/search", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return time.time() - t, json.loads(r.read())


def poradi(vysledky: list[dict], cile: set[str]) -> int | None:
    """1-based pozice prvního cílového dokumentu, nebo None."""
    for i, h in enumerate(vysledky, 1):
        if h["source_path"] in cile:
            return i
    return None


def znamenkovy_test(lepsi: int, horsi: int) -> float:
    """Oboustranný znaménkový test. Shody se podle definice vynechávají."""
    n = lepsi + horsi
    if n == 0:
        return 1.0
    k = min(lepsi, horsi)
    ocas = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * ocas)


def main() -> None:
    print(f"retrieval: {RS}   limit={LIMIT}   dotazů={len(DOTAZY)}\n")

    # jméno konfigurace -> seznam (poradi, top1_ok) a latence
    poradi_all: dict[str, list] = {c[0]: [] for c in CONFIGS}
    lat: dict[str, list] = {c[0]: [] for c in CONFIGS}

    for qi, (q, cile) in enumerate(DOTAZY, 1):
        # 1) přepis přes `cheap` jednou, pak do obou ramen explicitně
        try:
            _, prime = search({"query": q, "limit": LIMIT, "rerank": False})
        except Exception as e:
            sys.exit(f"přepis selhal u {q!r}: {e!r}")
        kw, lang = prime["keywords_used"], prime["lang"]

        print(f"=== [{qi}/{len(DOTAZY)}] {q}")
        print(f"    cíl: {', '.join(sorted(cile))}")
        print(f"    klíčová slova ({prime['keywords_source']}): {kw!r}   lang={lang}")

        for jmeno, extra in CONFIGS:
            body = {"query": q, "limit": LIMIT, "keywords": kw, "lang": lang}
            body.update(extra)
            try:
                s, d = search(body)
            except Exception as e:
                print(f"    {jmeno:12} CHYBA {e!r}")
                poradi_all[jmeno].append((None, False))
                continue
            res = d["results"]
            p = poradi(res, cile)
            top1 = bool(res) and res[0]["source_path"] in cile
            poradi_all[jmeno].append((p, top1))
            lat[jmeno].append(s)

            # celé pořadí, ne jen skóre — komprimovaná metrika tady lhala už třikrát
            radek = " > ".join(
                ("*" if h["source_path"] in cile else "") + h["source_path"]
                for h in res)
            znacka = "OK " if top1 else ("~" + str(p) if p else "MIMO")
            print(f"    {jmeno:12} {s:6.2f}s  {znacka:5} | {radek}")
        print()

    # --- souhrn ---
    print("=" * 78)
    print(f"{'konfigurace':14} {'top-1':>7} {'v top-8':>9} {'medián poz.':>12} {'medián s':>10}")
    for jmeno, _ in CONFIGS:
        zaznamy = poradi_all[jmeno]
        top1 = sum(1 for p, t in zaznamy if t)
        nasel = [p for p, _ in zaznamy if p]
        med = statistics.median(nasel) if nasel else float("nan")
        ml = statistics.median(lat[jmeno]) if lat[jmeno] else float("nan")
        print(f"{jmeno:14} {top1:3}/{len(zaznamy):<3} {len(nasel):5}/{len(zaznamy):<3} "
              f"{med:12.1f} {ml:10.2f}")

    print("\npárově proti baseline (jen dotazy, kde se pozice cíle liší):")
    zaklad = poradi_all[BASELINE]
    for jmeno, _ in CONFIGS:
        if jmeno == BASELINE:
            continue
        lepsi = horsi = stejne = 0
        detail = []
        for (q, _), (pz, _), (pn, _) in zip(DOTAZY, zaklad, poradi_all[jmeno]):
            a = pz if pz else 99
            b = pn if pn else 99
            if b < a:
                lepsi += 1
                detail.append(f"    + {q}  ({a} -> {b})")
            elif b > a:
                horsi += 1
                detail.append(f"    - {q}  ({a} -> {b})")
            else:
                stejne += 1
        p = znamenkovy_test(lepsi, horsi)
        print(f"  {jmeno}: zlepšilo {lepsi}, zhoršilo {horsi}, beze změny {stejne}"
              f"   (znaménkový test p = {p:.3f})")
        for d in detail:
            print(d)


if __name__ == "__main__":
    main()
