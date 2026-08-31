#!/usr/bin/env python3
"""Regresní běh zlaté sady proti SKUTEČNÉ odpovědní cestě Krytona.

PROČ TENHLE SKRIPT EXISTUJE
===========================
V repu je třicet měřicích skriptů a ani jeden neodpovídá na otázku
„zhoršilo se něco?". Každý je jednorázové měření spouštěné ručně, s výsledkem
v prozaickém textu dokumentace. Důsledek se ukázal 2026-08-19: práh
`ANSWER_MIN_RERANK` z P4 utnul VŠECHNY temporální dotazy (P7-B) a přišlo se
na to až živě, o den později. Pět temporálních otázek v pevné sadě to chytne
ve stejné session.

CO SE MĚŘÍ A CO SE ZÁMĚRNĚ NEMĚŘÍ
=================================
Nepočítá se jedno souhrnné skóre. Agregát by přesně ty dvě regrese, kvůli
kterým sada vzniká, schoval — P4 i P7-B se v „průměrné kvalitě" utopí.
Místo toho každá položka projde nezávislými kontrolami:

  najde          vrátil systém vůbec něco, nebo řekl „nic jsem nenašel"?
                 (osa P7-B; u položek `musi_najit: false` je to obráceně
                 a přiznané selhání je SPRÁVNÁ odpověď — osa P4)
  zdroj          je mezi citacemi aspoň jeden z očekávaných dokumentů?
                 (chytne odpověď, která vznikla, ale ze špatného podkladu)
  obsah          povinné a zakázané podstringy
  stat           číslo z /stats retrievalu se musí v odpovědi objevit (P1a)

BĚH SE POROVNÁVÁ S BASELINE, NE S IDEÁLEM
=========================================
Sada obsahuje položky, o kterých VÍME, že dneska selhávají (`znamy_problem`,
dnes tři temporální dotazy). Kdyby skript končil nenulově při každém
selhání, byl by od prvního dne červený a nikdo by ho nespouštěl — tatáž
smrt, jakou umřel check na `ANSWER_MAX_TOKENS` v smoke testu, který byl od
2026-08-18 šest dní červený, aniž si to kdo všiml.

Skript proto hlásí chybu jen při ZHORŠENÍ proti `eval/baseline.json`.
Položka, která selhávala a selhává dál, je poznámka; položka, která
přestala fungovat, je chyba. Nová baseline se zapíše jedině na výslovné
`--uloz-baseline`, aby regrese nešla „opravit" tím, že se schválí.

SPOUŠTĚNÍ
=========
Na brainu, jako root:

    /root/deploy/scripts/32-zlata-sada.py                   # běh a diff
    /root/deploy/scripts/32-zlata-sada.py --uloz-baseline    # schválit stav
    /root/deploy/scripts/32-zlata-sada.py --osa temporalni   # jen jedna osa
    /root/deploy/scripts/32-zlata-sada.py --json vysledek.json

Návratový kód 0 = žádná regrese, 1 = regrese proti baseline, 2 = běh sám
selhal (nedostupný kontejner, rozbitá sada).

JAK TO BĚŽÍ TECHNICKY
=====================
Dotazy se pouštějí UVNITŘ kontejneru `kryton` (`podman exec`), protože jen
tam je celá cesta: přepis dotazu, hybridní hledání, rerank, práh
`dost_relevantni()`, skládání promptu i fakta o korpusu. Kdyby skript mluvil
na retrieval přímo po HTTP jako `28-rerank-value-denik.py`, minul by práh
i prompt — tedy přesně ta dvě místa, kde P4 a P7-B žijí.

Vnitřní program se generuje sem a posílá na stdin, protože zlatá sada leží
v repu na hostiteli a do obrazu Krytona se nepřibaluje. Sada se do něj vloží
jako JSON literál. Stejný vzor jako `14-analytics-check.sh`.

`core.zaznamenej()` se NEVOLÁ, takže běh sady nepiluje `message` v databázi
a nezkresluje `31-denni-report.py`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
SADA = KOREN / "eval" / "zlata-sada.json"
BASELINE = KOREN / "eval" / "baseline.json"
ZNACKA = "---VYSLEDEK-JSON---"

# Vnitřní program. Vypisuje log na stderr (basicConfig ho tam dává sám),
# takže na stdout po značce je čistý JSON.
VNITRNI = '''
import json, sys, time
sys.path.insert(0, "/srv")
from app import config, core

POLOZKY = json.loads(%(sada)r)
try:
    STATS = core.corpus_stats()
except Exception as e:
    STATS = {"__chyba__": repr(e)}

out = []
for p in POLOZKY:
    z = {"id": p["id"]}
    t0 = time.time()
    try:
        res = core.search(p["dotaz"])
        odp = core.answer(p["dotaz"], res["results"])
        z["text"] = odp.text
        z["model"] = odp.model
        z["ms"] = odp.ms
        z["stopa"] = odp.stopa
        # Citují se chunky, které prošly prahem — ne všechno, co vrátil
        # retrieval. Kontrola `zdroj` se musí ptát na to, co model
        # OPRAVDU viděl, jinak by prošla i odpověď složená ze šumu.
        z["zdroje"] = sorted({h["source_path"] for h in res["results"]
                              if h.get("rerank_score") is None
                              or float(h["rerank_score"]) >= config.ANSWER_MIN_RERANK})
        z["zdroje_vse"] = sorted({h["source_path"] for h in res["results"]})
    except Exception as e:
        z["chyba"] = repr(e)
    z["celkem_ms"] = int((time.time() - t0) * 1000)
    out.append(z)
    print("hotovo %%s" %% p["id"], file=sys.stderr)

print("%(znacka)s")
print(json.dumps({"stats": STATS, "vysledky": out}, ensure_ascii=False))
'''


def bez_diakritiky(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.casefold())
                   if unicodedata.category(c) != "Mn")


def obsahuje(text: str, co: str) -> bool:
    """Bez ohledu na velikost písmen a diakritiku.

    Odpověď je psaná modelem, takže „nenašel" a „nenasel" jsou totéž
    tvrzení a kontrola, která by je rozlišovala, by lhala.
    """
    return bez_diakritiky(co) in bez_diakritiky(text)


ODMITNUTI = ("nic jsem nenašel", "jsem nic nenašel", "v poznámkách jsem")


def vyhodnot(p: dict, v: dict, stats: dict) -> dict:
    """Vrátí {kontrola: True/False/None}. None = na tuhle položku se nevztahuje."""
    k: dict = {}
    if v.get("chyba"):
        return {"beh": False}
    text = v.get("text", "")
    stopa = v.get("stopa") or {}
    odmitl = bool(stopa.get("odmitnuto")) or any(obsahuje(text, o) for o in ODMITNUTI)

    k["najde"] = (not odmitl) if p["musi_najit"] else odmitl

    if p.get("ocekavane_zdroje"):
        k["zdroj"] = bool(set(p["ocekavane_zdroje"]) & set(v.get("zdroje") or []))
    if p.get("musi_obsahovat"):
        k["obsah"] = all(obsahuje(text, s) for s in p["musi_obsahovat"])
    if p.get("nesmi_obsahovat"):
        k["obsah"] = k.get("obsah", True) and not any(
            obsahuje(text, s) for s in p["nesmi_obsahovat"])
    if p.get("musi_obsahovat_stat"):
        hodnota = stats.get(p["musi_obsahovat_stat"])
        k["stat"] = hodnota is not None and str(hodnota) in text
    return k


def prosla(k: dict) -> bool:
    return all(v for v in k.values() if v is not None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--osa", help="pusť jen jednu osu (faktograficka, "
                                  "temporalni, mimo-korpus, agregacni)")
    ap.add_argument("--uloz-baseline", action="store_true",
                    help="zapiš tenhle běh jako novou baseline")
    ap.add_argument("--json", help="ulož surový výsledek do souboru")
    ap.add_argument("--kontejner", default="kryton")
    a = ap.parse_args()

    sada = json.loads(SADA.read_text(encoding="utf-8"))
    polozky = [p for p in sada["polozky"]
               if not a.osa or p.get("osa") == a.osa]
    if not polozky:
        print("Sada je po filtru prázdná.", file=sys.stderr)
        return 2
    print("Zlatá sada: %d položek, kontejner %s\n" % (len(polozky), a.kontejner))

    program = VNITRNI % {"sada": json.dumps(polozky, ensure_ascii=False),
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
              % (r.returncode, r.stderr[-2000:]), file=sys.stderr)
        return 2
    data = json.loads(r.stdout.split(ZNACKA, 1)[1].strip())
    stats = data["stats"]
    podle_id = {v["id"]: v for v in data["vysledky"]}
    if a.json:
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")

    base = {}
    if BASELINE.exists():
        base = json.loads(BASELINE.read_text(encoding="utf-8")).get("polozky", {})

    print("%-26s %-14s %-7s %-9s %s" % ("položka", "osa", "stav", "ms", "detail"))
    print("-" * 100)
    stav_nyni: dict[str, bool] = {}
    regrese, opravy = [], []
    for p in polozky:
        v = podle_id.get(p["id"], {"chyba": "chybí výsledek"})
        k = vyhodnot(p, v, stats)
        ok = prosla(k)
        stav_nyni[p["id"]] = ok
        stopa = v.get("stopa") or {}

        znak = "OK" if ok else "CHYBA"
        if not ok and p.get("znamy_problem"):
            znak = "zn.%s" % p["znamy_problem"]
        detail = ", ".join("%s=%s" % (n, "ano" if s else "NE")
                           for n, s in k.items())
        if stopa:
            detail += "  [kand %s, nad prahem %s, max_rerank %s]" % (
                stopa.get("n_kandidatu"), stopa.get("n_nad_prahem"),
                stopa.get("max_rerank"))
        if v.get("chyba"):
            detail = v["chyba"][:60]
        print("%-26s %-14s %-7s %-9s %s" % (
            p["id"], p.get("osa", "-"), znak, v.get("celkem_ms", "-"), detail))

        byl = base.get(p["id"])
        if byl is True and not ok:
            regrese.append(p["id"])
        elif byl is False and ok:
            opravy.append(p["id"])

    proslo = sum(1 for x in stav_nyni.values() if x)
    print("\nProšlo %d z %d." % (proslo, len(polozky)))
    znama = [p["id"] for p in polozky
             if p.get("znamy_problem") and not stav_nyni[p["id"]]]
    if znama:
        print("Známé otevřené problémy (nepovažuje se za regresi): %s"
              % ", ".join(znama))

    if a.uloz_baseline:
        BASELINE.write_text(json.dumps(
            {"popis": "Schválený stav zlaté sady. Zapisuje se jen na "
                      "--uloz-baseline, aby regrese nešla schválit mimochodem.",
             "polozky": stav_nyni}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print("\nBaseline zapsána do %s" % BASELINE)
        return 0

    if not base:
        print("\nBaseline neexistuje. Až budeš s tímhle stavem srovnaný, "
              "spusť skript s --uloz-baseline.")
        return 0
    if opravy:
        print("\nZLEPŠENÍ: %s — až to bude záměr, potvrď přes --uloz-baseline."
              % ", ".join(opravy))
    if regrese:
        print("\nREGRESE (%d): %s" % (len(regrese), ", ".join(regrese)))
        return 1
    print("\nŽádná regrese proti baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
