#!/usr/bin/env python3
"""Je nexos.ai připravený na přepnutí? Zkontroluje pět navržených modelů.

Katalog nexos.ai je uzavřený: model, který není v konzoli zapnutý
(Models -> add a model), vrací `{"error":{"code":100110,"message":
"Model not found"}}` — a to i při volání přes `nexos_model_id`.
K 2026-08-18 bylo ze 145 chat modelů zapnutých 14 a ani jeden z pěti
navržených mezi nimi nebyl.

Tenhle skript se ptá jen na jednu věc: **co z návrhu už jde zavolat.**
Kvalitu neměří, na to je `22-eval-nexos-models.py`.

Spouštěj po každém zásahu v konzoli:
    python3 scripts/23-nexos-pripravenost.py

Klíč: proměnná NEXOS_API_KEY, jinak ~/.config/nexos/key.env.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = "https://api.nexos.ai/v1"

# Poskytovatele, ktere ma organizace pripojene. Odvozeno 2026-08-18 z toho,
# od koho je vubec neco zapnuteho. Model od jineho poskytovatele se
# v konzoli nenabidne, i kdyz ho `/v1/models/all` vraci.
PRIPOJENI = {"Azure", "Agent Platform (Vertex AI)", "nexos.ai"}

# (alias v litellm-config.yaml, jméno na nexos.ai, cena vstup/výstup $/M,
#  poznámka)
NAVRH = [
    ("cheap-nexos",          "GPT 5 nano",            "Azure",
     "nejlevnější chat model, který si org MŮŽE přidat (varianta EU)"),
    ("cheap-fallback-nexos", "Gemini 2.5 Flash Lite", "Agent Platform (Vertex AI)",
     "záloha pro cheap; Gemma 4 31B nejde, je jen od DeepInfra"),
    ("workhorse-nexos",      "Gemini 2.5 Flash Lite", "Agent Platform (Vertex AI)",
     "titulek a tagy při indexaci"),
    ("backstop-nexos",       "GPT 4.1 nano",          "Azure",
     "poslední záchrana; GPT-OSS 20b nejde, je jen od Groqu"),
    ("reasoning-nexos",      "GPT-OSS 120b",          "nexos.ai",
     "UŽ ZAPNUTÝ. Varianta od Azure je 5,3x levnější, ale mimo EU"),
]


def klic() -> str:
    env = os.environ.get("NEXOS_API_KEY", "").strip()
    if env:
        return env
    p = pathlib.Path.home() / ".config" / "nexos" / "key.env"
    try:
        for line in p.read_text().splitlines():
            if line.startswith("NEXOS_API_KEY="):
                k = line.split("=", 1)[1].strip().strip("\"'")
                if k:
                    return k
    except OSError as e:
        sys.exit(f"nepodarilo se precist {p}: {e}")
    sys.exit(f"v {p} chybi radek NEXOS_API_KEY=...")


KEY = klic()

# `User-Agent` je POVINNY: pred api.nexos.ai sedi Cloudflare a vychozi
# `Python-urllib/3.x` odmita s HTTP 403 "error code: 1010". Vypada to
# jako odmitnuty klic, ale neni. Viz komentar v 22-eval-nexos-models.py.
HLAVICKY = {"Content-Type": "application/json",
            "Authorization": f"Bearer {KEY}",
            "User-Agent": "druhy-mozek-pripravenost/1.0"}


def _get(cesta: str) -> list:
    req = urllib.request.Request(f"{BASE}{cesta}", headers=HLAVICKY)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["data"]


def katalog():
    """({jmeno: [varianty]}, {jmeno: nexos_model_id}).

    POZOR NA DVE SOUSTAVY UUID — spletl jsem si je a stalo to chybny zaver:

      /v1/models      -> `nexos_model_id` = instance ve workspace, VOLATELNA
      /v1/models/all  -> `id`             = globalni ID katalogu, NEVOLATELNE

    Globalni ID vrati stejne `Model not found` jako vypnuty model, takze
    z jeho selhani NELZE usuzovat, ze model neni zapnuty. Overeno na
    Gemini 3 Flash Preview: workspace UUID odpovi, globalni ne.
    """
    varianty = {}
    for m in _get("/models/all"):
        p = m.get("pricing") or {}
        varianty.setdefault(m["name"], []).append({
            "zapnuty": bool(m.get("available")),
            "in": float(p.get("input_cost_per_token") or 0) * 1e6,
            "out": float(p.get("output_cost_per_token") or 0) * 1e6,
            "kdo": m.get("owned_by") or "?",
            "region": m.get("region") or "?"})
    # `nexos_model_id` existuje JEN u zapnutych modelu.
    uuid = {m["id"]: m.get("nexos_model_id") for m in _get("/models")}
    return varianty, uuid


def zkus(jmeno: str) -> str:
    """Prazdny retezec = model odpovedel. Jinak popis chyby."""
    body = json.dumps({"model": jmeno, "max_tokens": 5,
                       "messages": [{"role": "user", "content": "OK"}]}).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", body, HLAVICKY)
    try:
        with urllib.request.urlopen(req, timeout=60):
            return ""
    except urllib.error.HTTPError as e:
        telo = e.read()[:200].decode(errors="replace")
        return f"HTTP {e.code}: {telo}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


varianty, uuid = katalog()
zapnutych = sum(1 for vs in varianty.values() for v in vs if v["zapnuty"])
print(f"katalog: {len(varianty)} jmen modelu, {sum(len(v) for v in varianty.values())} "
      f"variant, zapnutych {zapnutych}\n")

chybi = []
dvojznacne = []
for alias, jmeno, kdo_chci, pozn in NAVRH:
    vs = varianty.get(jmeno)
    print(f"{alias}  ->  {jmeno}")
    print(f"    {pozn}")
    if vs is None:
        print("    STAV: NENI — v katalogu vubec neni")
        chybi.append(alias)
        print()
        continue

    zap = [v for v in vs if v["zapnuty"]]
    for v in vs:
        znak = "*" if v["zapnuty"] else " "
        # Org ma pripojene jen Azure, Vertex a nexos.ai — varianty od
        # jinych poskytovatelu se v konzoli VUBEC NENABIDNOU, i kdyz je
        # katalog vraci. Presne na tohle jsem 2026-08-18 najel: doporucil
        # jsem Gemma 4 31B (DeepInfra) a GPT-OSS 20b (Groq), a ani jeden
        # nesel v UI najit.
        dosah = "" if v["kdo"] in PRIPOJENI else "  [nedosazitelne]"
        print("    %s %-26s %-6s %6.3f / %6.3f $/M%s" % (
            znak, v["kdo"], v["region"], v["in"], v["out"], dosah))

    if not zap:
        moje = [v for v in vs if v["kdo"] == kdo_chci]
        if not moje:
            print(f"    STAV: NEDOSAZITELNY — {kdo_chci} tenhle model nema, "
                  f"a ostatni poskytovatele org pripojene nema")
        else:
            nej = min(moje, key=lambda v: v["in"])
            print(f"    STAV: VYPNUTY — zapni v konzoli variantu {nej['kdo']} "
                  f"({nej['region']}) za {nej['in']:.3f}/{nej['out']:.3f}")
        chybi.append(alias)
        print()
        continue

    chyba = zkus(jmeno)
    if chyba:
        print(f"    STAV: CHYBA — {chyba}")
        chybi.append(alias)
        print()
        continue

    print(f"    STAV: OK   nexos_model_id = {uuid.get(jmeno)}")
    if zap[0]["kdo"] != kdo_chci:
        print(f"    !! zapnuta varianta je od {zap[0]['kdo']}, cekal jsem {kdo_chci}")
    # Az bude zapnuta vic nez jedna varianta tehoz jmena, prestane byt
    # jmeno jednoznacne a config MUSI adresovat pres UUID.
    if len(zap) > 1:
        dvojznacne.append(jmeno)
        print(f"    !! {len(zap)} zapnute varianty stejneho jmena — "
              f"v configu pouzij UUID vyse, ne jmeno")
    levnejsi = [v for v in vs if not v["zapnuty"] and v["in"] < zap[0]["in"] - 1e-9]
    if levnejsi:
        n = min(levnejsi, key=lambda v: v["in"])
        print(f"    !! zapnuta varianta stoji {zap[0]['in']:.3f}, ale "
              f"{n['kdo']} nabizi {n['in']:.3f} ({zap[0]['in'] / n['in']:.1f}x levneji)")
    print()

if chybi:
    print(f"NEPRIPRAVENO: {len(chybi)} z {len(NAVRH)} aliasu nejde zavolat "
          f"({', '.join(chybi)}).")
    print("Dokud tohle neni cele OK, prepinat neni na co.")
    sys.exit(1)
if dvojznacne:
    print("POZOR: stejnojmenne zapnute varianty u " + ", ".join(dvojznacne))
    print("V litellm-config.yaml adresuj pres nexos_model_id, ne pres jmeno.")
print("Vsech pet aliasu odpovida. Dalsi krok je zmerit kvalitu:")
print("    python3 scripts/22-eval-nexos-models.py")
