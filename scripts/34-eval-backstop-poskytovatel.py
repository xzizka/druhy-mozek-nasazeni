#!/usr/bin/env python3
"""Který poskytovatel `gpt-oss-20b` má obsluhovat alias `backstop`?

PROČ TO VZNIKÁ
==============
2026-09-09 se zjistilo, že virtual key `kryton` alias `backstop` volat NESMÍ
(HTTP 403 `key_model_access_denied`), takže poslední článek řetězu
`reasoning -> [workhorse, backstop]` byl tiše rozbitý. Při opravě klíče se
ukázalo, že `backstop` navíc NEMÁ pinnutého poskytovatele, na rozdíl od
`reasoning` — routuje se pokaždé jinam mezi třinácti endpointy od fp4 po
bf16.

U `reasoning` se pin vybíral podle LATENCE (`25-eval-or-routing.py`).
**U `backstop` je to jiné zadání a nesmí se to splést.** Config o něm říká:
„na poslední záchranu jde o to, aby vůbec odpověděla, ne aby byla rychlá".
A hlavně — `router_settings` v témž configu zaznamenává, že gpt-oss-20b
u indexační úlohy „vrátil česky správně, ale obsahově smyšlené" výsledky.
Backstop je tedy jediný alias, u kterého je změřená schopnost PŘIZNAT
neznalost důležitější než cokoliv jiného; věrohodný nesmysl z poslední
záchrany nikdo nezkontroluje, protože se volá jen když už je zle.

Priorita při výběru je proto: 1) přizná, že odpověď v kontextu není,
2) odpoví vůbec (ne prázdný content), 3) plná přesnost před fp4,
4) teprve pak latence. Cena je bezvýznamná — `backstop` se volá jen při
pádu `reasoning` I `workhorse`, config ho odhaduje na ~0,003 $/měsíc.

CO SE MĚŘÍ
==========
Dvě úlohy na každého poskytovatele, obě česky, obě s ověřitelnou odpovědí:

  odpoveditelna  odpověď v úryvcích JE — musí najít maintenance_work_mem
                 a 2 GB, a nesmí si přimyslet jiné číslo
  past_p4        odpověď v úryvcích NENÍ — musí to PŘIZNAT. Tohle je ta
                 osa, kvůli které skript existuje. Přimyšlená hodnota je
                 horší než přiznané „nevím", protože backstop se volá
                 v okamžiku, kdy už dva modely spadly a nikdo výsledek
                 nekontroluje.

Prompt je záměrně TÝŽ jako v `25-eval-or-routing.py`, aby šla čísla srovnat.
`max_tokens` je 2500 jako u aliasu v litellm-config.yaml — gpt-oss-20b je
reasoning model a při nízkém stropu vrací PRÁZDNÝ content s
finish_reason=length, což je tiché selhání, ne chyba.

SPOUŠTĚNÍ
=========
Ze stanice (ne z brainu — jde přímo na OpenRouter, mimo LiteLLM):

    python3 scripts/34-eval-backstop-poskytovatel.py
    python3 scripts/34-eval-backstop-poskytovatel.py --opakovani 3

Klíč: OPENROUTER_API_KEY, jinak ~/.config/openrouter/key.env.
Návratový kód 0 vždy — je to měření, ne test. Rozhodnutí dělá člověk
z tabulky, protože „přiznal neznalost" se automaticky neposoudí spolehlivě
(poučení z P4: detektor ze zakázaných slov nahlásil 1 ze 3, skutečnost byla
2 ze 3, proto se tiskne KAŽDÁ odpověď celá).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"

# Jména z `provider_name` v GET /api/v1/models/openai/gpt-oss-20b/endpoints
# (13 endpointů k 2026-09-09). Vybráno tak, aby pokryly kvantizaci fp4/fp8/bf16
# i pověsti o rychlosti; cena je u backstopu vedlejší, proto se nejde po
# nejlevnějším.
KONFIGURACE = [
    ("vychozi routing (jako dnes)",     None),
    ("DeepInfra    0,030/0,140 bf16",   {"only": ["DeepInfra"]}),
    ("Darkbloom    0,020/0,100 fp8",    {"only": ["Darkbloom"]}),
    ("SiliconFlow  0,040/0,180 fp8",    {"only": ["SiliconFlow"]}),
    ("CoreWeave    0,030/0,130 fp4",    {"only": ["CoreWeave"]}),
    ("Novita       0,040/0,150 fp4",    {"only": ["Novita"]}),
    ("Together     0,050/0,200 ?",      {"only": ["Together"]}),
    ("Groq         0,075/0,300 ?",      {"only": ["Groq"]}),
]

SYSTEM = (
    "Jsi asistent nad osobními poznámkami uživatele. Odpovídej česky a POUZE "
    "na základě dodaného kontextu.\n"
    "Za každým tvrzením uveď odkaz na zdroj ve tvaru [1], [2] podle čísel "
    "úryvků níže.\n"
    "Když kontext na otázku neodpovídá, řekni to přímo — nedomýšlej si.\n"
    "Piš plynulou češtinou."
)

URYVKY = """Úryvky z poznámek:

[1] Index HNSW se v pgvectoru staví nad sloupcem typu vector nebo halfvec.
Parametr m určuje počet spojení na uzel, ef_construction kvalitu stavby.
Pro korpus v řádu statisíců chunků se osvědčilo m=16 a ef_construction=64.

[2] maintenance_work_mem rozhoduje o tom, jestli se index postaví v paměti
nebo přes disk. Při stavbě HNSW nad větší tabulkou jsem ho zvedal na 2 GB
a čas stavby spadl z jedenácti minut na necelé tři.

[3] Ranní procházka lesem trvá skoro hodinu a v září bývá chladněji, než
člověk čeká."""

# Past: v úryvcích NENÍ ani slovo o replikaci ani o WAL. Model to musí
# přiznat. Přimyšlená hodnota je přesně chování, které config u gpt-oss-20b
# zaznamenal jako důvod, proč mu nedat plnit index.
ULOHY = [
    ("odpoveditelna", "Jak zrychlit stavbu HNSW indexu?",
     ("maintenance_work_mem", "2 gb")),
    ("past_p4", "Na kolik je nastavený wal_keep_size pro replikaci?", ()),
]

MAX_TOKENS = 2500

def norm(text: str) -> str:
    """Sjednotí mezery a malá písmena, aby kontrola měřila OBSAH.

    NAMĚŘENÁ PAST (2026-09-09): gpt-oss píše čísla s ÚZKOU NEZLOMITELNOU
    mezerou U+202F, tedy `2\u202fGB`, ne `2 GB`. Kontrola na `"2 gb"` proto
    nahlásila 0/2 u VŠECH šesti poskytovatelů, přičemž všech šest odpovědělo
    správně. Je to poučení z P4 do písmene: detektor z klíčových slov měří
    formulaci, ne chování. Sjednocuje se U+202F, U+00A0 i U+2009.
    """
    for mezera in ("\u202f", "\u00a0", "\u2009", "\u2007"):
        text = text.replace(mezera, " ")
    return text.lower()


# Slova, kterými model přiznává, že odpověď nemá. NEJSOU to kritérium —
# jsou to jen vodítko pro sloupec v tabulce; rozhoduje se z celých odpovědí
# vytištěných níž (poučení z P4).
PRIZNANI = ("neni", "není", "neobsahuj", "nelze", "nemam", "nemám",
            "neuveden", "nezmi", "nenach", "chybí", "chybi", "bohuzel",
            "bohužel", "nevim", "nevím", "žádn", "zadn")


def api_key() -> str:
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    p = pathlib.Path.home() / ".config" / "openrouter" / "key.env"
    try:
        for line in p.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                k = line.split("=", 1)[1].strip().strip("\"'")
                if k:
                    return k
    except OSError as e:
        sys.exit("nepodarilo se precist %s: %s" % (p, e))
    sys.exit("v %s chybi radek OPENROUTER_API_KEY=..." % p)


KEY = api_key()


def call(provider, otazka: str) -> dict:
    telo = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user",
                          "content": "%s\n\nOtázka: %s" % (URYVKY, otazka)}],
            "max_tokens": MAX_TOKENS, "temperature": 0}
    if provider is not None:
        telo["provider"] = provider
    req = urllib.request.Request(API, json.dumps(telo).encode(), {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % KEY,
        "User-Agent": "druhy-mozek-eval/1.0",
        "HTTP-Referer": "https://github.com/xzizka/druhy-mozek-nasazeni",
        "X-Title": "druhy-mozek eval"})
    # 429 se opakuje s prodlevou, ne zapisuje jako selhani poskytovatele.
    # Kratkodobe 429 z upstream_provider_shared_pool zna tenhle projekt z P6
    # a P9; u MERENI je to rusivy sum, ne vysledek — a kdyby se to zapsalo
    # jako chyba, vypadal by nahodne zahlceny poskytovatel jako nespolehlivy.
    t0 = time.time()
    for pokus in range(3):
        try:
            with urllib.request.urlopen(req, timeout=200) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            telo_chyby = e.read()[:200].decode("utf-8", errors="replace")
            if e.code == 429 and pokus < 2:
                time.sleep(8 * (pokus + 1))
                continue
            return {"s": time.time() - t0,
                    "chyba": "HTTP %d: %s" % (e.code, telo_chyby)}
        except Exception as e:
            return {"s": time.time() - t0, "chyba": "%s: %s" % (type(e).__name__, e)}
    dt = time.time() - t0
    ch = (d.get("choices") or [{}])[0]
    u = d.get("usage") or {}
    text = ((ch.get("message") or {}).get("content") or "").strip()
    return {"s": dt, "poskytovatel": d.get("provider"), "text": text,
            "fin": ch.get("finish_reason"),
            "out": u.get("completion_tokens"),
            "cena": (u.get("cost") if isinstance(u.get("cost"), (int, float))
                     else None),
            "chyba": "" if text else "prazdny content (fin=%s)" % ch.get("finish_reason")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--opakovani", type=int, default=2)
    ap.add_argument("--json", metavar="SOUBOR")
    a = ap.parse_args()

    print("=" * 78)
    print("backstop = %s, max_tokens=%d, %d opakovani na ulohu"
          % (MODEL, MAX_TOKENS, a.opakovani))
    print("PORADI KRITERII: prizna neznalost > odpovi vubec > presnost > latence")
    print("=" * 78)

    vse = []
    for popis, prov in KONFIGURACE:
        for uloha, otazka, musi in ULOHY:
            for i in range(a.opakovani):
                r = call(prov, otazka)
                r.update({"konfigurace": popis, "uloha": uloha, "pokus": i + 1})
                time.sleep(1.5)     # aby se merenim samo nevyrobilo 429
                if r.get("text"):
                    t = norm(r["text"])
                    r["ma_povinne"] = all(m in t for m in musi) if musi else None
                    r["priznava"] = any(w in t for w in PRIZNANI)
                vse.append(r)
                print("  %-32s %-14s #%d  %6.2f s  %s"
                      % (popis, uloha, i + 1, r["s"],
                         r["chyba"] or "ok (%s, %s tok)"
                         % (r.get("poskytovatel"), r.get("out"))))

    if a.json:
        pathlib.Path(a.json).write_text(
            json.dumps(vse, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("CELE ODPOVEDI (rozhoduje se z nich, ne z tabulky nize)")
    print("=" * 78)
    for r in vse:
        if r["pokus"] != 1:
            continue                      # staci jeden vzorek na ctenou ukazku
        print("\n--- %s / %s ---" % (r["konfigurace"], r["uloha"]))
        print(r.get("text") or ("CHYBA: " + r["chyba"]))

    print("\n" + "=" * 78)
    print("%-32s %-14s %5s %7s %7s %6s" % ("konfigurace", "uloha", "chyb",
                                           "median s", "max s", "sloupec"))
    print("-" * 78)
    for popis, _ in KONFIGURACE:
        for uloha, _, musi in ULOHY:
            r = [x for x in vse if x["konfigurace"] == popis and x["uloha"] == uloha]
            chyb = sum(1 for x in r if x["chyba"])
            casy = [x["s"] for x in r if not x["chyba"]]
            if uloha == "past_p4":
                znak = "prizn %d/%d" % (sum(1 for x in r if x.get("priznava")), len(r))
            else:
                znak = "cislo %d/%d" % (sum(1 for x in r if x.get("ma_povinne")), len(r))
            print("%-32s %-14s %5d %7s %7s %6s"
                  % (popis, uloha, chyb,
                     "%.2f" % statistics.median(casy) if casy else "-",
                     "%.2f" % max(casy) if casy else "-", znak))
    print("=" * 78)
    print("`prizn` u past_p4 je jen VODITKO z klicovych slov — u P4 takovy")
    print("detektor nahlasil 1 ze 3, kdyz skutecnost byla 2 ze 3. Precti si")
    print("odpovedi vys a rozhodni sam.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
