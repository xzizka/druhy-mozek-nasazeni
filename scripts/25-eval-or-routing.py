#!/usr/bin/env python3
"""Da se latence `gpt-oss-120b` na OpenRouteru srazit omezenim routingu?

Otevreny bod kroku 2. V mereni 2026-08-18 (`24-eval-reasoning.py`) vysel
`openai/gpt-oss-120b` pres OpenRouter na **median 11,0 s a maximum 33,6 s**,
zatimco TYZ model na nexos.ai dal **1,8 s**. Kvalita byla u obou 4/4, takze
rozhoduje jen rychlost — a ta je pro dotaz z Telegramu podstatna.

Podezreni: OpenRouter routuje pokazde jinam. V sesti voláních to byli ctyri
ruzni poskytovatele (DeepInfra, CoreWeave, AkashML, DigitalOcean) a katalog
jich nabizi dvacet, od fp4 kvantizace po fp16. Kdyz se routing omezi na
jednoho rychleho, mel by median spadnout.

MERI SE TYZ PROMPT jako v `24-eval-reasoning.py`, aby cislo slo srovnat
s onemi 11,0 s a 1,8 s. Je mensi nez skutecny dotaz Krytona (~2800 tokenu),
takze absolutni hodnoty jsou optimisticke pro vsechny stejne; zajima nas
POMER mezi konfiguracemi, ne absolutni cislo.

Spousteni:  python3 scripts/25-eval-or-routing.py
Klic: OPENROUTER_API_KEY, jinak ~/.config/openrouter/key.env.
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

API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
OPAKOVANI = 4

# (popis, hodnota pole `provider` v tele requestu)
#
# `only` bere jmena z `provider_name` v
# GET /api/v1/models/openai/gpt-oss-120b/endpoints.
# Vybrani ctyri pokryvaji rozpeti: nejlevnejsi fp4, levny bf16 (plna
# presnost), a dva poskytovatele povestni rychlosti.
KONFIGURACE = [
    ("vychozi routing (jako dnes)",      None),
    ("sort: throughput",                 {"sort": "throughput"}),
    ("sort: latency",                    {"sort": "latency"}),
    ("sort: price",                      {"sort": "price"}),
    ("only: CoreWeave    0,030 fp4",     {"only": ["CoreWeave"]}),
    ("only: DeepInfra    0,037 bf16",    {"only": ["DeepInfra"]}),
    ("only: Novita       0,050 fp4",     {"only": ["Novita"]}),
    ("only: SiliconFlow  0,050 fp8",     {"only": ["SiliconFlow"]}),
    ("only: DigitalOcean 0,055",         {"only": ["DigitalOcean"]}),
    ("only: Mancer 2     0,085 fp8",     {"only": ["Mancer 2"]}),
    ("only: Google       0,090",         {"only": ["Google"]}),
    ("only: BaseTen      0,100 fp4",     {"only": ["BaseTen"]}),
    ("only: Groq         0,150",         {"only": ["Groq"]}),
    ("only: Cerebras     0,350 fp16",    {"only": ["Cerebras"]}),
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
OTAZKA = "Jak zrychlit stavbu HNSW indexu?"

# Jako ANSWER_MAX_TOKENS u Krytona. Zamerne se NESNIZUJE — je to jedna
# z veci, ktere latenci ovlivnuji, a merit se ma stav, ktery nastane.
MAX_TOKENS = 8000


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
        sys.exit(f"nepodarilo se precist {p}: {e}")
    sys.exit(f"v {p} chybi radek OPENROUTER_API_KEY=...")


KEY = api_key()

# (poskytovatel, vstupni tokeny, vystupni tokeny) pro kazde volani
CENY = []


def call(provider):
    """(sekundy, poskytovatel, vystupni_tokeny, chyba)."""
    telo = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"{URYVKY}\n\nOtázka: {OTAZKA}"}],
            "max_tokens": MAX_TOKENS, "temperature": 0}
    if provider is not None:
        telo["provider"] = provider
    req = urllib.request.Request(API, json.dumps(telo).encode(), {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
        "User-Agent": "druhy-mozek-eval/1.0",
        "HTTP-Referer": "https://github.com/xzizka/druhy-mozek-nasazeni",
        "X-Title": "druhy-mozek eval"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return time.time() - t0, None, None, \
            f"HTTP {e.code}: {e.read()[:160].decode(errors='replace')}"
    except Exception as e:
        return time.time() - t0, None, None, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    u = d.get("usage") or {}
    ch = (d.get("choices") or [{}])[0]
    CENY.append((d.get("provider"), u.get("prompt_tokens"), u.get("completion_tokens")))
    out = ((ch.get("message") or {}).get("content") or "").strip()
    if not out:
        return dt, d.get("provider"), u.get("completion_tokens"), \
            f"prazdna odpoved (finish={ch.get('finish_reason')})"
    return dt, d.get("provider"), u.get("completion_tokens"), ""


vysledky = []
for popis, prov in KONFIGURACE:
    print("=" * 72)
    print(popis)
    print("=" * 72, flush=True)
    casy, poskytovatele, chyby = [], [], []
    for i in range(OPAKOVANI):
        dt, kdo, tok, err = call(prov)
        if err:
            chyby.append(err)
            print("  [%d] SELHALO po %.1f s  %s" % (i + 1, dt, err))
        else:
            casy.append(dt)
            poskytovatele.append(kdo)
            print("  [%d] %6.2f s  provider=%-16s vystup=%s tokenu" % (i + 1, dt, kdo, tok))
        time.sleep(1.5)
    med = statistics.median(casy) if casy else float("nan")
    vysledky.append((popis, med, min(casy) if casy else float("nan"),
                     max(casy) if casy else float("nan"),
                     sorted(set(poskytovatele)), len(chyby)))
    print("  --> median %.2f s, rozsah %.2f-%.2f s, poskytovatele: %s\n" % (
        med, min(casy) if casy else float("nan"), max(casy) if casy else float("nan"),
        ", ".join(str(p) for p in sorted(set(poskytovatele))) or "zadny"))

print("=" * 72)
print("SOUHRN — pro srovnani: nexos.ai dal 1,8 s, vychozi OpenRouter 11,0 s")
print("=" * 72)
print("%-34s %8s %8s %8s %6s" % ("konfigurace", "median", "min", "max", "chyb"))
for popis, med, mn, mx, kdo, nch in vysledky:
    print("%-34s %7.2fs %7.2fs %7.2fs %6d" % (popis, med, mn, mx, nch))
print("\nPoskytovatele, ktere routing skutecne vybral:")
for popis, med, mn, mx, kdo, nch in vysledky:
    print("  %-34s %s" % (popis, ", ".join(str(p) for p in kdo) or "-"))
