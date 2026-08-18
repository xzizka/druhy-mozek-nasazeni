#!/usr/bin/env python3
"""Srovnání kandidátů na alias `reasoning` — nexos.ai proti OpenRouteru.

Sesterský skript k `22-eval-nexos-models.py`, ale MĚŘÍ NĚCO JINÉHO.
`22` testuje roli `cheap`: krátké prompty, `max_tokens` 20 a 120 —
a na těch reasoning modely selhávají tím, že spotřebují strop na úvahu
a vrátí prázdný `content`. Role `reasoning` je opak: Kryton posílá
~2800 tokenů kontextu a `ANSWER_MAX_TOKENS=8000`, kde na úvahu místo je.

Testuje se to, na čem u Krytona záleží:

  * odpověď VZNIKNE (neprázdný content, finish_reason=stop)
  * je česky a plynule, ne anglická úvaha
  * cituje [1]/[2] podle SYSTEM promptu
  * NEDOMÝŠLÍ SI — na otázku mimo kontext musí říct, že odpověď nemá
    (to je past z P4: model si u faktografických dotazů vymyslí citaci)
  * kolik to stojí a jak dlouho to trvá

HLAVNÍ DŮVOD VZNIKU (2026-08-18): `openai/gpt-oss-120b` stojí na
OpenRouteru **0,030/0,170 $/M**, kdežto na nexos.ai **0,800/1,600** —
tedy 27x víc na vstupu za TÝŽ MODEL. nexos.ai si ho hostuje sám,
OpenRouter routuje na levné poskytovatele (CoreWeave, DeepInfra, Novita).
Cenu tedy znám; co neznám, je jestli se obě cesty chovají stejně —
a jestli je gpt-oss-120b vůbec dost dobrý na roli `reasoning`.

Ta pochybnost má důvod: `gpt-oss-20b` v měření 2026-08-09 sice odpověděl
česky správně, ale obsahově SMYŠLENĚ. Věrohodný nesmysl je u Krytona
horší než chyba, protože ho nikdo nezachytí. Proto jsou dvě ze čtyř
otázek níže pasti, kde odpověď v úryvcích NENÍ.

Spouštěj:  python3 scripts/24-eval-reasoning.py
Klíče: NEXOS_API_KEY (nebo ~/.config/nexos/key.env) a OPENROUTER_API_KEY
(nebo ~/.config/openrouter/key.env).
"""
from __future__ import annotations

import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request

# Meri se PRES DVA POSKYTOVATELE, protoze tentyz model u nich stoji
# radove jinak. `openai/gpt-oss-120b` je na OpenRouteru 0,030/0,170 $/M,
# na nexos.ai 0,800/1,600 — tedy 27x drazsi na vstupu za TYZ MODEL.
# nexos.ai si ho hostuje sam, OpenRouter routuje na levne poskytovatele.
#
# (popis, poskytovatel, model)
#   nexos  -> model je `nexos_model_id` (jmeno by u stejnojmennych
#             variant nerozlisilo, viz 23-nexos-pripravenost.py)
#   or     -> model je bezne id na OpenRouteru
MODELY = [
    ("gpt-oss-120b @ OpenRouter   0,030/0,170", "or",
     "openai/gpt-oss-120b"),
    ("gpt-oss-20b  @ OpenRouter   0,030/0,130", "or",
     "openai/gpt-oss-20b"),
    ("gemma-4-26b  @ OpenRouter   0,070/0,340", "or",
     "google/gemma-4-26b-a4b-it"),
    ("GPT-OSS 120b @ nexos.ai     0,800/1,600", "nexos",
     "6d8aec78-893f-4c89-b4e0-40c20751cf0a"),
    ("Claude Haiku 4.5 @ nexos.ai 1,100/5,500", "nexos",
     "382a6532-5f7f-472e-bc0e-f4fbb96cc96f"),
]

API = {"nexos": "https://api.nexos.ai/v1/chat/completions",
       "or": "https://openrouter.ai/api/v1/chat/completions"}

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

URYVKY = """Úryvky z poznámek:

[1] Index HNSW se v pgvectoru staví nad sloupcem typu vector nebo halfvec.
Parametr m určuje počet spojení na uzel, ef_construction kvalitu stavby.
Pro korpus v řádu statisíců chunků se osvědčilo m=16 a ef_construction=64;
vyšší hodnoty stavbu prodlužují, ale dotaz už nezrychlí.

[2] maintenance_work_mem rozhoduje o tom, jestli se index postaví v paměti
nebo přes disk. Při stavbě HNSW nad větší tabulkou jsem ho zvedal na 2 GB
a čas stavby spadl z jedenácti minut na necelé tři. Po dokončení stavby
se hodnota vrací zpět, protože ji drží každé spojení zvlášť.

[3] Ranní procházka lesem trvá skoro hodinu a v září bývá chladněji, než
člověk čeká. Beru si svetr, i když odpoledne slibuje teplo."""

# (otázka, co se má stát, klíčová slova, která ROZHODUJÍ o správnosti)
OTAZKY = [
    ("Jak zrychlit stavbu HNSW indexu?", "odpoved",
     ["maintenance_work_mem"]),
    ("Co znamená parametr ef_construction?", "odpoved",
     ["ef_construction"]),
    # PAST Z P4: v úryvcích tahle informace NENÍ. Model musí říct, že ji
    # nemá — ne si vymyslet číslo a podložit ho citací.
    ("Kolik stojí měsíčně provoz serveru brain?", "priznat_nevim", []),
    ("Jaká je výchozí hodnota parametru m podle dokumentace pgvectoru?",
     "priznat_nevim", []),
]

MAX_TOKENS = 8000        # jako ANSWER_MAX_TOKENS u Krytona
TIMEOUT = 180            # jako ANSWER_TIMEOUT


def _ze_souboru(cesta: pathlib.Path, promenna: str):
    try:
        for line in cesta.read_text().splitlines():
            if line.startswith(promenna + "="):
                k = line.split("=", 1)[1].strip().strip("\"'")
                if k:
                    return k
    except OSError:
        return None
    return None


def api_key(kdo: str) -> str:
    """Klic z prostredi, jinak ze souboru mimo repo. Nikdy z argv."""
    promenna = {"nexos": "NEXOS_API_KEY", "or": "OPENROUTER_API_KEY"}[kdo]
    env = os.environ.get(promenna, "").strip()
    if env:
        return env
    # nexos: ~/.config/nexos/key.env, OpenRouter: ~/.config/openrouter/key.env
    slozka = {"nexos": "nexos", "or": "openrouter"}[kdo]
    p = pathlib.Path.home() / ".config" / slozka / "key.env"
    k = _ze_souboru(p, promenna)
    if k:
        return k
    sys.exit(f"chybi {promenna} — dej ho do prostredi nebo do {p} "
             f"jako radek {promenna}=...")


KLICE = {kdo: api_key(kdo) for kdo in sorted({k for _, k, _ in MODELY})}
COST = {"total": 0.0, "cache_hits": 0}


def call(kdo: str, model: str, otazka: str):
    """(content, sekundy, reasoning_tokens, skutecny_model, chyba)."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"{URYVKY}\n\nOtázka: {otazka}"}],
        "max_tokens": MAX_TOKENS, "temperature": 0}).encode()
    hlavicky = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KLICE[kdo]}",
        # POVINNE u nexos.ai — Cloudflare blokuje vychozi Python-urllib
        # s HTTP 403 "error code: 1010". OpenRouteru to nevadi, ale
        # posilat to lze obema.
        "User-Agent": "druhy-mozek-eval/1.0"}
    if kdo == "or":
        hlavicky["HTTP-Referer"] = "https://github.com/xzizka/druhy-mozek-nasazeni"
        hlavicky["X-Title"] = "druhy-mozek eval"
    req = urllib.request.Request(API[kdo], body, hlavicky)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
            if (r.headers.get("x-nexos-cache") or "").lower() == "hit":
                COST["cache_hits"] += 1
            skutecny = r.headers.get("x-nexos-model-id") or d.get("model")
    except urllib.error.HTTPError as e:
        return "", time.time() - t0, None, None, \
            f"HTTP {e.code}: {e.read()[:150].decode(errors='replace')}"
    except Exception as e:
        return "", time.time() - t0, None, None, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    u = d.get("usage") or {}
    # nexos.ai vraci cenu primo; OpenRouter ne, ten ji ma az ve spend logu.
    COST["total"] += float(u.get("nexos_credits_cost") or 0)
    ch = (d.get("choices") or [{}])[0]
    out = ((ch.get("message") or {}).get("content") or "").strip()
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if not out:
        return "", dt, rt, skutecny, \
            f"prazdna odpoved (finish_reason={ch.get('finish_reason')})"
    return out, dt, rt, skutecny, ""


# Vodítko, ne měřítko. Hledá české tvary, kterými model přiznává, že
# odpověď nemá.
#
# TENHLE SEZNAM ZKAZIL MĚŘENÍ DVAKRÁT ZA SEBOU (2026-08-18):
#
#   1. běh — neznal „není uvedena", „není zmíněno", „nemohu odpovědět".
#      Tři modely vyšly 2/4 a jeden 3/4, což vypadalo jako rozdíl
#      v opatrnosti. Opakování dvou pastí 3x u dvou poskytovatelů dalo
#      12/12: přiznaly VŽDYCKY, jen jinými slovy.
#   2. běh — po doplnění dvanácti tvarů propadla gemma na větě
#      „V poskytnutých poznámkách není informace o tom, kolik stojí…",
#      což je samozřejmě správné přiznání. Třetí falešný poplach.
#
# Po dvou pokusech je závěr jasný: **klíčová slova nad volným textem
# měří formulaci, ne chování, a doplňovat další tvary problém neřeší** —
# čeština jich má víc, než kdo vypíše. Skóre níže proto ber jako
# UPOZORNĚNÍ „na tohle se podívej", ne jako výsledek. Rozhoduje text,
# který se u pastí tiskne vždycky.
#
# Kdyby to mělo měřit doopravdy, musí to posoudit druhý model (LLM jako
# soudce) nebo člověk. Na pět modelů a čtyři otázky se to nevyplatí.
PRIZNANI = ["neobsahuj", "není v poznámk", "nenacház", "nemám", "neuvád",
            "nelze", "chybí", "neposkytuj", "nezmiňuj", "nejsou",
            "není uved", "není zmín", "není k dispozici", "nemohu odpov",
            "není informace", "neni uved", "nezmiňuje", "se nemluví",
            "nevyplývá", "není obsaž", "neexistuj", "nedokážu", "nevím"]

vysledky = {}
nerozlisene = [m for _, k, m in MODELY if k == "nexos" and m.count("-") < 4]
if nerozlisene:
    print("!! POZOR: %s neni UUID. U nexos.ai se stejnojmenne varianty "
          "jmenem nerozlisi.\n" % ", ".join(repr(x) for x in nerozlisene))

for popis, kdo, model in MODELY:
    print("=" * 74)
    print(popis)
    print("=" * 74, flush=True)
    r = {"ok": 0, "chyby": [], "lat": [], "rt": [], "id": None}
    for otazka, ceka, musi in OTAZKY:
        out, dt, rt, skutecny, err = call(kdo, model, otazka)
        r["lat"].append(dt)
        if rt:
            r["rt"].append(rt)
        r["id"] = r["id"] or skutecny
        if err:
            r["chyby"].append((otazka[:40], err))
            print("  SELHALO  %-42s %s" % (otazka[:42], err))
            time.sleep(1.5)
            continue

        problemy = []
        low = out.lower()
        if ceka == "odpoved":
            if "[1]" not in out and "[2]" not in out:
                problemy.append("zadna citace [n]")
            for m in musi:
                if m.lower() not in low:
                    problemy.append(f"chybi {m!r}")
        else:
            # P4: tady je jedina spravna odpoved "nevim".
            if not any(p in low for p in PRIZNANI):
                problemy.append("?? detektor nenasel priznani — PRECTI TEXT NIZE")
        if not problemy:
            r["ok"] += 1
        else:
            r["chyby"].append((otazka[:40], "; ".join(problemy)))
        print("  %-8s %-42s %5.1f s  %s" % (
            "OK" if not problemy else "CHYBA", otazka[:42], dt,
            "; ".join(problemy)))
        # U PASTI se vzdycky tiskne, co model REKL. Detektor `PRIZNANI`
        # je jen seznam ceskych kmenu a muze se splest oboustranne —
        # rozhodnout, jestli si model vymyslel, musi clovek pri cteni.
        if ceka == "priznat_nevim" or problemy:
            print("           > %s" % out.replace("\n", " ")[:300])
        time.sleep(1.5)

    vysledky[popis] = r
    print("  --> %d/%d, median %.1f s, x-nexos-model-id=%s" % (
        r["ok"], len(OTAZKY),
        statistics.median(r["lat"]) if r["lat"] else float("nan"), r["id"]))
    if r["rt"]:
        print("      reasoning_tokens: median %d, max %d" % (
            statistics.median(r["rt"]), max(r["rt"])))
    print()

print("=" * 74)
print("SOUHRN")
print("=" * 74)
print("%-46s %7s %9s" % ("model", "skore", "median"))
for popis, _, _ in MODELY:
    r = vysledky[popis]
    med = statistics.median(r["lat"]) if r["lat"] else float("nan")
    print("%-46s %3d/%-3d %8.1f s" % (popis, r["ok"], len(OTAZKY), med))
print("\ncena mereni na nexos.ai: %.6f kreditu "
      "(OpenRouter cenu v odpovedi nevraci)" % COST["total"])
if COST["cache_hits"]:
    print("POZOR: %d odpovedi z cache gateway — mereni je zkreslene"
          % COST["cache_hits"])
print("\nPozor pri cteni: shodne `x-nexos-model-id` u dvou radku znamena, "
      "ze obe mirily na TUTEZ variantu a srovnani neplati.")
