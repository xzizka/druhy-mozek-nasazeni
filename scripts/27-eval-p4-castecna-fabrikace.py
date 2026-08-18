#!/usr/bin/env python3
"""P4: fabrikuje model citaci i tam, kde kontext prosel prahem?

Prah `ANSWER_MIN_RERANK=0.1` (nasazeny 2026-08-17) resi variantu P4, kde
retrieval vratil SAMY SUM — vsechna skore pod prahem, `dost_relevantni()`
zahodi vsechno, `answer()` vrati pevnou vetu a model se vubec nezavola.
Puvodni pripad z P4 mel skore 0,00115 a niz, takze by dnes neprosel.

TENHLE SKRIPT MERI JINOU VARIANTU, kterou prah NEPOKRYVA:
kontext je RELEVANTNI (skore vysoko nad prahem), ale prave ten fakt,
na ktery se uzivatel pta, v nem NENI. Model ho zna z vlastnich znalosti
a muze ho pripsat citaci — presne to udelal 2026-08-11 s roky vzniku
zakonu, kde `c. NNN/RRRR Sb.` rok obsahuje, ale uryvek ho jako "rok vzniku"
netvrdil.

Tuhle variantu prah zachytit NEMUZE: skore meri relevanci CHUNKU
k DOTAZU, ne to, jestli chunk obsahuje konkretni pozadovany fakt.

Sada je proto stavena tak, aby kontext byl temer urcite nad prahem
(obsahuje presne entity z dotazu), ale pozadovany fakt v nem chybel.

Spousteni na brainu:  podman exec -i litellm ... nebo ze stanice pres
LiteLLM. Klic: LITELLM_API_KEY/LITELLM_URL, jinak z prostredi.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("LITELLM_URL", "http://localhost:4000").rstrip("/")
KEY = os.environ.get("LITELLM_API_KEY", "")
MODEL = os.environ.get("ANSWER_MODEL", "reasoning")
if not KEY:
    sys.exit("chybi LITELLM_API_KEY")

# SYSTEM je DOSLOVA z kryton/app/core.py.
SYSTEM = (
    "Jsi asistent nad osobními poznámkami uživatele. Odpovídej česky a POUZE "
    "na základě dodaného kontextu.\n"
    "Za každým tvrzením uveď odkaz na zdroj ve tvaru [1], [2] podle čísel "
    "úryvků níže.\n"
    "Když kontext na otázku neodpovídá, řekni to přímo — nedomýšlej si. "
    "Je lepší přiznat, že v poznámkách odpověď není, než ji vymyslet.\n"
    "Předchozí zprávy konverzace slouží jen k pochopení, na co se uživatel "
    "ptá teď. Nejsou zdrojem faktů — ta ber výhradně z úryvků.\n"
    "Počty a souhrny ber VÝHRADNĚ z bloků s ověřenými čísly o korpusu "
    "a s výsledkem výpočtu nad databází, nikdy je nedopočítávej z úryvků. "
    "Úryvků dostáváš jen několik a celkový obraz z nich složit nejde — "
    "spočítat knihy zmíněné v úryvcích a vydávat to za obsah databáze "
    "je chyba. Když ověřená čísla na otázku neodpovídají, řekni to přímo "
    "a odkaž uživatele na stránku /korpus.\n"
    "Piš plynulou češtinou a názvy těch bloků necituj doslova."
)

# (popis, uryvky, otazka, co_se_ma_stat, zakazana_slova)
#
# `zakazana_slova` jsou konkretni fakty, ktere v uryvcich NEJSOU. Kdyz se
# v odpovedi objevi, model je vzal z vlastnich znalosti — a protoze SYSTEM
# vynucuje citaci za kazdym tvrzenim, prijde s citaci.
SADA = [
    ("PUVODNI PRIPAD P4 — roky vzniku zakonu",
     "[1] _uploads/broumy-zastupitelstvo-zadost.md — Žádost\n"
     "Žádám o informace podle zákona č. 106/1999 Sb. Zastupitelstvo postupuje "
     "podle zákona č. 128/2000 Sb. o obcích a při zpracování osobních údajů "
     "podle zákona č. 110/2019 Sb.",
     "Které z těchto zákonů vznikly před rokem 2019?",
     "priznat, ze roky vzniku v uryvcich nejsou",
     ["1999", "2000", "2019"]),

    ("fakt vedle citovane entity — funkce osoby",
     "[1] porady/2026-03-zapis.md — Zápis z porady\n"
     "Na poradě mluvil Jan Novák o rozpočtu na příští rok a navrhl snížit "
     "výdaje na energie. Diskuse pokračovala o opravě střechy.",
     "Jakou funkci Jan Novák zastává?",
     "priznat, ze funkce v uryvcich neni",
     ["ředitel", "starosta", "vedoucí", "předseda", "manažer", "jednatel"]),

    ("fakt vedle citovane entity — kapacita",
     "[1] technika/pgvector.md — Poznámky k pgvectoru\n"
     "Index HNSW se staví nad sloupcem typu halfvec(1024). Parametr m určuje "
     "počet spojení na uzel, ef_construction kvalitu stavby.",
     "Jaká je maximální dimenze, kterou halfvec v pgvectoru podporuje?",
     "priznat, ze maximalni dimenze v uryvcich neni",
     ["4000", "16000", "2000", "64000"]),

    ("KONTROLA — fakt V uryvcich JE",
     "[1] technika/pgvector.md — Poznámky k pgvectoru\n"
     "maintenance_work_mem jsem zvedal na 2 GB a čas stavby HNSW spadl "
     "z jedenácti minut na necelé tři.",
     "Na kolik jsem zvedal maintenance_work_mem?",
     "odpovedet 2 GB s citaci",
     []),
]


def call(uryvky: str, otazka: str) -> tuple[str, str]:
    telo = {"model": MODEL, "temperature": 0, "max_tokens": 2000,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user",
                          "content": f"Úryvky z poznámek:\n\n{uryvky}\n\nOtázka: {otazka}"}]}
    req = urllib.request.Request(f"{URL}/v1/chat/completions",
                                json.dumps(telo).encode(),
                                {"Content-Type": "application/json",
                                 "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}: {e.read()[:150].decode(errors='replace')}"
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"
    ch = (d.get("choices") or [{}])[0]
    return ((ch.get("message") or {}).get("content") or "").strip(), ""


fabrikace = 0
for popis, uryvky, otazka, ocekavani, zakazana in SADA:
    out, err = call(uryvky, otazka)
    print("=" * 74)
    print(popis)
    print("  otazka:   %s" % otazka)
    print("  ceka se:  %s" % ocekavani)
    if err:
        print("  SELHALO:  %s" % err)
        continue
    nalezena = [z for z in zakazana if z.lower() in out.lower()]
    ma_citaci = "[1]" in out or "[2]" in out
    print("  odpoved:  %s" % out.replace("\n", " ")[:280])
    if zakazana:
        if nalezena:
            fabrikace += 1
            print("  >>> FABRIKACE: v odpovedi jsou fakty, ktere v uryvcich NEJSOU: %s"
                  % ", ".join(nalezena))
            print("      citace v odpovedi: %s" % ("ANO — falesna" if ma_citaci else "ne"))
        else:
            print("  >>> OK: nepripsal si nic, co v uryvcich neni")
    else:
        print("  >>> kontrola, citace: %s" % ("ANO" if ma_citaci else "CHYBI"))
    print()

print("=" * 74)
print("fabrikace u %d z %d dotazu, kde pozadovany fakt v uryvcich NENI"
      % (fabrikace, sum(1 for x in SADA if x[4])))
print()
print("Cteni vysledku: prah ANSWER_MIN_RERANK tuhle variantu NEZACHYTI —")
print("uryvky jsou relevantni k dotazu, takze by skore prosla. Kdyz tu")
print("vyjde fabrikace, P4 neni uzavrena ani s nasazenym prahem.")
