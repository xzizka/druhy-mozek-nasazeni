#!/usr/bin/env python3
"""Měření deníkové cesty z P15: sentiment a trend nad celým deníkem.

PROČ TENHLE SKRIPT EXISTUJE
===========================
P15 přidává cestu, která OBCHÁZÍ rerankový práh: neprázdné `extra` v
`core.answer()` projde bez ohledu na `ANSWER_MIN_RERANK`. To je záměr, ale
znamená to, že se u téhle cesty nedá spolehnout na práh jako na pojistku.
Musí se tedy měřit dvě věci zvlášť:

  POZITIVNÍ  spustí se deníková cesta tam, kde má, a dá použitelnou odpověď?
  NEGATIVNÍ  NESPUSTÍ se tam, kde nemá?

Ta druhá je důležitější a je to hlavní riziko celého P15. Kdyby
`je_denikovy_prehled()` chytalo běžné faktografické dotazy, vlilo by do
každého promptu dva tisíce tokenů deníku a odpovědi na technické otázky by
se zředily. Proto jsou negativní kontroly v sadě natvrdo a jejich selhání
se hlásí jako CHYBA, ne jako poznámka.

TISKNOU SE CELÉ ODPOVĚDI, A TO ZÁMĚRNĚ
======================================
Metodické poučení z P4 (2026-08-18, třikrát za den): detektor fabrikace ze
zakázaných slov nahlásil 1 ze 3, skutečnost byla 2 ze 3 — u `halfvec` nebylo
v seznamu „1024", protože se čekalo 4000 nebo 16000. Klíčová slova nad
volným textem měří FORMULACI, ne chování. Sentiment se navíc automaticky
vyhodnotit nedá; posoudit, jestli je odpověď k něčemu, umí jedině člověk,
který si ji přečte. Skript proto neříká „prošlo/neprošlo" o kvalitě, jen
o mechanice (spustilo se to? odseklo se to? kolik to stálo?).

CO SE MĚŘÍ
==========
U každého dotazu:

  denik_dni       rozpoznané období (z `stopa`), None = cesta se nespustila
  n_kandidatu     kolik chunků vrátil retrieval
  n_nad_prahem    kolik z nich přežilo `ANSWER_MIN_RERANK`
  max_rerank      nejlepší skóre PŘED prahem (diagnóza P7-B)
  odseknuto       narazila odpověď na strop tokenů?
  finish_reason   od LiteLLM, spolu s prompt/completion tokeny
  znaku           délka odpovědi — proti TELEGRAM_MAX_ZNAKU

SPOUŠTĚNÍ
=========
Na brainu, jako root:

    /root/deploy/scripts/33-eval-denik-prehled.py
    /root/deploy/scripts/33-eval-denik-prehled.py --jen negativni
    /root/deploy/scripts/33-eval-denik-prehled.py --json vysledek.json

Návratový kód 0 = mechanika v pořádku, 1 = aspoň jedna kontrola selhala,
2 = běh sám selhal.

JAK TO BĚŽÍ TECHNICKY
=====================
Dotazy se pouštějí UVNITŘ kontejneru `kryton` (`podman exec`), stejným
vzorem jako `32-zlata-sada.py` — jen tam je celá cesta včetně čtení
`denik/` z namountovaného MARKDOWN_ROOT. Skript na hostiteli by deníkové
soubory viděl na jiné cestě a `config.DENIK_DIR` by neplatil.

`core.zaznamenej()` se NEVOLÁ, takže běh sady nepíše do `message`
a nezkresluje `31-denni-report.py`.

POZOR NA CACHE LiteLLM: `cache: true`, TTL 3600. Opakovaný běh se stejnými
dotazy vrátí odpovědi z cache, tedy latenci k ničemu a cenu nula. Pro měření
latence pouštěj s odstupem, nebo dotazy drobně změň.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

ZNACKA = "---VYSLEDEK-JSON---"

# `ceka_denik`: má se deníková cesta spustit?
#   True  = musí (jinak chyba)
#   False = nesmí (jinak chyba) — negativní kontrola, hlavní riziko P15
#
# `dni` je explicitní období pro `core.answer(denik_dni=)`; None znamená
# „nech rozhodnout heuristiku", což je to, co se u pozitivních položek
# vlastně testuje.
SADA = [
    # --- pozitivní: heuristika má chytnout a období rozpoznat -------------
    {"id": "sentiment-puvodni", "ceka_denik": True, "dni": None,
     "dotaz": "Projdi moje záznamy na každodenní otázky a zjisti sentiment "
              "odpovědí. Jak působí?",
     "pozn": "DOSLOVA dotaz, který 2026-09-09 vrátil „nic jsem nenašel“ "
             "(message id 68, max_rerank 7,3e-05). Regresní kotva P15."},
    {"id": "nalada-mesic", "ceka_denik": True, "dni": None,
     "dotaz": "Jaká byla moje nálada za poslední měsíc?"},
    {"id": "trend-90", "ceka_denik": True, "dni": None,
     "dotaz": "Jaký je trend mých zápisů za posledních 90 dní?",
     "pozn": "Nad DENIK_DETAIL_MAX_DNI — má vynutit souhrn po týdnech, "
             "ne tabulku po dnech."},
    {"id": "mesic-jmenem", "ceka_denik": True, "dni": None,
     "dotaz": "Jak působí moje zápisy za srpen?",
     "pozn": "Pojmenovaný měsíc. Past: „cervenec“ obsahuje „cerven“."},
    {"id": "posledni-zaznamy", "ceka_denik": True, "dni": None,
     "dotaz": "Jaké jsou poslední záznamy v deníku?",
     "pozn": "Tohle je P7-B doslova. Do P15 vracelo „nic jsem nenašel“ "
             "(změřeno 2026-08-19, nejlepší skóre 0,0013)."},

    # --- pozitivní: explicitní cesta, heuristika se neptá -----------------
    {"id": "explicitni-30", "ceka_denik": True, "dni": 30,
     "dotaz": "Co mě v poslední době nejvíc zaměstnávalo?",
     "pozn": "Dotaz sám žádné deníkové slovo nemá — cestu zapíná jen "
             "explicitní období, jako rozbalovátko na webu nebo /denik 30."},
    {"id": "explicitni-rok", "ceka_denik": True, "dni": 365,
     "dotaz": "Jak se moje nálada měnila?"},

    # --- negativní: cesta se spustit NESMÍ --------------------------------
    {"id": "neg-maintenance", "ceka_denik": False, "dni": None,
     "dotaz": "Jaké maintenance_work_mem se použilo při stavbě indexu?",
     "pozn": "Faktografický dotaz s jedinou správnou trefou. Kdyby se "
             "zředil deníkem, je P15 čistá škoda."},
    {"id": "neg-istio", "ceka_denik": False, "dni": None,
     "dotaz": "Jak jsem řešil networkpolicy v Istio?"},
    {"id": "neg-vypnuto", "ceka_denik": False, "dni": 0,
     "dotaz": "Jaký je sentiment mých zápisů?",
     "pozn": "Dotaz, který heuristika chytit MUSÍ, ale uživatel deník "
             "výslovně vypnul (denik_dni=0). Testuje, že se ty dva stavy "
             "neslily do jednoho."},
]

VNITRNI = '''
import json, sys, time
sys.path.insert(0, "/srv")
from app import config, core

POLOZKY = json.loads(%(sada)r)

# Kolik toho v deníku vůbec je — bez tohohle čísla nejde poznat, jestli
# krátká odpověď znamená chybu, nebo prázdný deník.
import pathlib
koren = pathlib.Path(config.MARKDOWN_ROOT) / config.DENIK_DIR
souboru = sorted(koren.glob("*.md")) if koren.is_dir() else []
DENIK = {"dir": str(koren), "souboru": len(souboru),
         "znaku": sum(p.stat().st_size for p in souboru),
         "prvni": souboru[0].stem if souboru else None,
         "posledni": souboru[-1].stem if souboru else None}

out = []
for p in POLOZKY:
    z = {"id": p["id"]}
    t0 = time.time()
    try:
        res = core.search(p["dotaz"])
        odp = core.answer(p["dotaz"], res["results"], denik_dni=p["dni"])
        z["text"] = odp.text
        z["model"] = odp.model
        z["ms"] = odp.ms
        z["stopa"] = odp.stopa
        z["zdroje"] = sorted({h["source_path"] for h in res["results"]})
    except Exception as e:
        z["chyba"] = repr(e)
    z["celkem_ms"] = int((time.time() - t0) * 1000)
    out.append(z)
    print("hotovo %%s" %% p["id"], file=sys.stderr)

print("%(znacka)s")
print(json.dumps({"denik": DENIK, "vysledky": out}, ensure_ascii=False))
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kontejner", default="kryton")
    ap.add_argument("--jen", choices=("pozitivni", "negativni"),
                    help="jen jedna polovina sady")
    ap.add_argument("--json", metavar="SOUBOR", help="uložit surový výsledek")
    a = ap.parse_args()

    sada = SADA
    if a.jen == "pozitivni":
        sada = [p for p in SADA if p["ceka_denik"]]
    elif a.jen == "negativni":
        sada = [p for p in SADA if not p["ceka_denik"]]

    program = VNITRNI % {"sada": json.dumps(sada, ensure_ascii=False),
                         "znacka": ZNACKA}
    try:
        r = subprocess.run(["podman", "exec", "-i", a.kontejner, "python", "-"],
                           input=program, capture_output=True, text=True)
    except FileNotFoundError:
        print("podman není na PATH — tenhle skript patří na brain, ne na "
              "stanici.", file=sys.stderr)
        return 2
    if ZNACKA not in r.stdout:
        print("Běh v kontejneru neuspěl (návratový kód %d).\nstderr:\n%s"
              % (r.returncode, r.stderr[-3000:]), file=sys.stderr)
        return 2

    data = json.loads(r.stdout.split(ZNACKA, 1)[1].strip())
    if a.json:
        open(a.json, "w", encoding="utf-8").write(
            json.dumps(data, ensure_ascii=False, indent=2))

    d = data["denik"]
    print("=" * 72)
    print("DENÍK: %s" % d["dir"])
    print("  souborů %s, %s znaků, rozsah %s .. %s"
          % (d["souboru"], d["znaku"], d["prvni"], d["posledni"]))
    print("  ANSWER_MAX_TOKENS platí pro běžné dotazy, "
          "DENIK_ANSWER_MAX_TOKENS pro deníkové")
    print("=" * 72)

    podle_id = {p["id"]: p for p in sada}
    chyb = 0
    souhrn = []
    for v in data["vysledky"]:
        p = podle_id[v["id"]]
        stopa = v.get("stopa") or {}
        dni = stopa.get("denik_dni")
        spustilo = dni is not None

        if v.get("chyba"):
            verdikt = "BĚH SELHAL"
            chyb += 1
        elif spustilo != p["ceka_denik"]:
            verdikt = ("CHYBA: cesta se spustila, i když neměla"
                       if spustilo else
                       "CHYBA: cesta se NEspustila, i když měla")
            chyb += 1
        else:
            verdikt = "ok"

        print("\n" + "-" * 72)
        print("### %s   [%s]" % (v["id"], verdikt))
        print("dotaz: %s" % p["dotaz"])
        if p.get("pozn"):
            print("pozn.: %s" % p["pozn"])
        print("zadané dni=%s -> rozpoznané období=%s dnů"
              % (p["dni"], dni if spustilo else "cesta se nespustila"))
        if v.get("chyba"):
            print("chyba: %s" % v["chyba"])
            souhrn.append((v["id"], verdikt, dni, None, None))
            continue
        print("retrieval: %s kandidátů, %s nad prahem, max_rerank=%s"
              % (stopa.get("n_kandidatu"), stopa.get("n_nad_prahem"),
                 stopa.get("max_rerank")))
        print("model=%s  %s ms  odseknuto=%s  znaků=%d"
              % (v.get("model"), v.get("ms"), stopa.get("odseknuto"),
                 len(v.get("text") or "")))
        if stopa.get("odseknuto"):
            # Není to chyba skriptu ani mechaniky: varování v odpovědi je
            # PRÁVĚ to, co P15 zavádí. Ale je to signál, že pokyn k formátu
            # v deníkovém bloku na tohle období nestačí.
            print("!! odseknuto na stropu — zvaž DENIK_DETAIL_MAX_DNI "
                  "nebo DENIK_ANSWER_MAX_TOKENS")
        print("zdroje z retrievalu: %s" % (", ".join(v.get("zdroje") or []) or "-"))
        print("--- CELÁ ODPOVĚĎ ---")
        print(v.get("text") or "(prázdná)")
        souhrn.append((v["id"], verdikt, dni, stopa.get("odseknuto"),
                       len(v.get("text") or "")))

    print("\n" + "=" * 72)
    print("%-22s %-38s %6s %5s %7s" % ("id", "verdikt", "dní", "odsek", "znaků"))
    for i, verd, dni, ods, zn in souhrn:
        print("%-22s %-38s %6s %5s %7s"
              % (i, verd, dni if dni is not None else "-",
                 "ano" if ods else "-", zn if zn is not None else "-"))
    print("=" * 72)
    if chyb:
        print("SELHALO %d z %d kontrol mechaniky." % (chyb, len(souhrn)))
        print("Kvalitu odpovědí tenhle skript NEHODNOTÍ — přečti si je výš.")
        return 1
    print("Mechanika v pořádku (%d kontrol). Kvalitu odpovědí posuď sám "
          "z výpisů výš — sentiment automaticky ověřit nejde." % len(souhrn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
