#!/usr/bin/env python3
"""Smí každý klíč volat CELÝ svůj fallback řetěz? (kontrola oprávnění)

PROČ TENHLE SKRIPT EXISTUJE
===========================
2026-09-09 se zjistilo, že virtual key `kryton` nesmí volat alias
`backstop` — HTTP 403 `key_model_access_denied`. `backstop` je přitom
POSLEDNÍ článek řetězu `reasoning -> [workhorse, backstop]`, přidaný kvůli
incidentu P6 (2026-08-12), kdy spadl `reasoning` i `workhorse` naráz.

**Ta chyba byla tichá skoro měsíc, a to je na ní to podstatné.** Za
normálního provozu se `backstop` nezavolá ani jednou, takže 403 nikdo
nikdy neuvidí. Projeví se jedině v okamžiku, kdy už dva modely spadly —
tedy když na něm nejvíc záleží. `litellm-config.yaml` na tuhle past na
dvou místech sám varuje (u klíče `retrieval-service`, kde se to stalo
2026-08-09), a přesto se to stalo znovu u jiného klíče.

Tenhle skript tu proto je, aby se to potřetí poznalo dřív než při výpadku.

CO SE KONTROLUJE
================
Řetězy se čtou z `router_settings.fallbacks` v configu, ne z pevného
seznamu — jinak by skript zestárnul s první změnou configu.

  1. PRÁVA: pro každý klíč a každý alias v jeho řetězech se porovná, co
     klíč smí (`/key/info`) proti tomu, co řetěz potřebuje. Chybějící
     alias = CHYBA.
  2. DOSAŽITELNOST: každý potřebný alias se pod klíčem té komponenty
     skutečně ZAVOLÁ. Právo v databázi a živá odpověď nejsou totéž —
     model může být na OpenRouteru mrtvý (viz `cheap`/ling-2.6-flash,
     2026-08-27), i když klíč právo má.

CO SE ZÁMĚRNĚ NEKONTROLUJE
==========================
Že LiteLLM při pádu prvního fallbacku zkusí druhý. To je chování knihovny,
ne naší konfigurace, a **ověřilo se jednorázově a destruktivně**: 2026-09-09
se `reasoning` i `workhorse` v configu přepsaly na neexistující model ID,
LiteLLM se restartoval a `reasoning` pod klíčem `kryton` vrátil
`model=openai/gpt-oss-20b provider=Groq` za 1 318 ms — tedy backstop.
Config se vrátil přes `git checkout`. Do opakovaného skriptu to nepatří:
rozbíjet primární modely na běžícím systému kvůli testu je horší než ta
chyba, kterou by to hledalo.

Mimochodem se tím potvrdila i domněnka, kterou `core.zkontroluj_fallback()`
označuje jako POZOROVANOU, ne zaručenou: při propadu vrátí LiteLLM
v poli `model` KONKRÉTNÍ model, ne jméno aliasu.

SPOUŠTĚNÍ
=========
Na brainu, jako root (potřebuje `podman exec` na secrety):

    python3 /root/deploy/scripts/35-fallback-retez.py
    python3 /root/deploy/scripts/35-fallback-retez.py --bez-volani   # jen práva

Návratový kód 0 = řetězy jsou průchodné, 1 = aspoň jeden má díru,
2 = běh sám selhal.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONFIG = "/root/deploy/conf/litellm-config.yaml"
URL = "http://127.0.0.1:4000"

# Který klíč obsluhuje které VSTUPNÍ aliasy. Odsud se přes `fallbacks`
# dopočítá celý řetěz, který ten klíč musí umět projít.
#
# Zdroj pravdy pro to, kdo co volá, je `scripts/03-quadlets.sh`
# (`ANSWER_MODEL`, `ANALYTICS_MODEL`, `REWRITE_MODEL`).
#
# `n8n` tu ZÁMĚRNĚ NENÍ, a není to opomenutí. Je to čtvrtý virtual key,
# který nepatří k žádnému kontejneru na brainu (n8n běží jinde), takže se
# jeho hodnota nedá přečíst přes `podman exec` a živě zavolat. Kontrolu práv
# nepotřebuje: volá jen `workhorse`, a ten fallback ZÁMĚRNĚ nemá — plní
# index a věrohodný nesmysl je tam horší než nahlas spadlá indexace, viz
# `router_settings` v configu. Jeho řetěz je tedy jednoprvkový a nemá kde
# mít díru. Kdyby n8n někdy dostal alias s fallbackem, musí se sem doplnit.
KLICE = {
    "kryton": {"secret": ("kryton", "LITELLM_API_KEY"),
               "vstupni": ["reasoning"]},
    "retrieval-service": {"secret": ("retrieval", "LITELLM_API_KEY"),
                          "vstupni": ["cheap"]},
}


def secret(kontejner: str, promenna: str) -> str:
    r = subprocess.run(["podman", "exec", kontejner, "printenv", promenna],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("nepodarilo se precist %s z kontejneru %s: %s"
                 % (promenna, kontejner, r.stderr.strip()[:200]))
    return r.stdout.strip()


def master() -> str:
    return secret("litellm", "LITELLM_MASTER_KEY")


def fallbacky() -> dict[str, list[str]]:
    """`{alias: [fallback, ...]}` z configu.

    Čte se řádkově, ne přes PyYAML: brain nemá pyyaml nainstalovaný mimo
    kontejnery a `fallbacks` je plochý seznam jednoklíčových map, takže
    tři řádky regexu jsou lepší než závislost. Táž úvaha jako
    u `split_frontmatter()` v chunkeru.
    """
    out: dict[str, list[str]] = {}
    uvnitr = False
    for radek in open(CONFIG, encoding="utf-8"):
        if radek.startswith("  fallbacks:"):
            uvnitr = True
            continue
        if uvnitr:
            if radek.strip().startswith("- ") and ":" in radek:
                telo = radek.strip()[2:]
                alias, _, zbytek = telo.partition(":")
                cile = [c.strip().strip('"\'')
                        for c in zbytek.strip().strip("[]").split(",") if c.strip()]
                out[alias.strip()] = cile
            elif radek.strip() and not radek.startswith((" ", "\t")):
                break
            elif radek.strip() and not radek.strip().startswith(("-", "#")):
                break
    return out


def retez(vstupni: str, fb: dict[str, list[str]]) -> list[str]:
    """Vstupní alias + všechno, na co se z něj dá propadnout (tranzitivně)."""
    videno, front = [], [vstupni]
    while front:
        a = front.pop(0)
        if a in videno:
            continue
        videno.append(a)
        front.extend(fb.get(a, []))
    return videno


def key_info(mk: str, alias: str) -> list[str] | None:
    """Seznam `models` klíče podle aliasu. None = klíč nenalezen."""
    req = urllib.request.Request(
        URL + "/key/list?return_full_object=true&size=50",
        headers={"Authorization": "Bearer " + mk})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    for k in d.get("keys", d if isinstance(d, list) else []):
        if isinstance(k, dict) and k.get("key_alias") == alias:
            return k.get("models") or []
    return None


def zavolej(key: str, alias: str) -> tuple[bool, str]:
    """Živé volání aliasu. `max_tokens` se ZÁMĚRNĚ NEPOSÍLÁ.

    NAMĚŘENÁ PAST (2026-09-09, hned při prvním běhu tohohle skriptu):
    sonda posílala `max_tokens: 300` a `backstop` vrátil PRÁZDNÝ content
    s `finish_reason=length`, tedy falešný poplach. gpt-oss-20b je reasoning
    model a strop spotřebuje na `reasoning_content` dřív, než začne psát —
    změřeno 241-368 tokenů jen na uvažování. Je to táž past jako u big-pickle
    v P8, jen obrácená: tam nízký strop rozbil provoz, tady rozbil test.

    Bez `max_tokens` v požadavku platí hodnota u aliasu v litellm-config.yaml
    (backstop 2500, reasoning 4000) — a to je správně, protože sonda má měřit
    NASAZENOU konfiguraci, ne můj override. Kryton sám strop posílá vždycky
    (`ANSWER_MAX_TOKENS`, `DENIK_ANSWER_MAX_TOKENS`), ale to je jeho rozpočet
    na odpověď, ne otázka dosažitelnosti.
    """
    body = {"model": alias,
            "messages": [{"role": "user",
                          "content": "Odpověz jediným slovem: ano"}]}
    req = urllib.request.Request(URL + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=200))
    except urllib.error.HTTPError as e:
        return False, "HTTP %d: %s" % (e.code, e.read()[:150].decode("utf-8", "replace"))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:150])
    ms = int((time.time() - t0) * 1000)
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        # Reasoning modely utrácejí strop na uvažování; prázdný content je
        # tiché selhání, ne úspěch. Táž past jako u big-pickle (P8).
        return False, "prazdny content, fin=%s" % (d.get("choices") or [{}])[0].get("finish_reason")
    return True, "%d ms, provider=%s" % (ms, d.get("provider"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bez-volani", action="store_true",
                    help="jen prava z /key/list, zadna volani na modely")
    a = ap.parse_args()

    try:
        mk = master()
        fb = fallbacky()
    except SystemExit:
        raise
    except Exception as e:
        print("beh selhal: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2

    print("=" * 72)
    print("Fallback řetězy z %s:" % CONFIG)
    for alias, cile in fb.items():
        print("  %s -> %s" % (alias, ", ".join(cile) or "(žádné)"))
    print("=" * 72)

    chyb = 0
    for klic, cfg in KLICE.items():
        try:
            smi = key_info(mk, klic)
        except Exception as e:
            print("\n### %s — /key/list selhalo: %s" % (klic, e))
            chyb += 1
            continue
        if smi is None:
            print("\n### %s — klíč s tímhle aliasem NEEXISTUJE" % klic)
            chyb += 1
            continue

        potreba = []
        for v in cfg["vstupni"]:
            for x in retez(v, fb):
                if x not in potreba:
                    potreba.append(x)

        print("\n### klíč `%s`" % klic)
        print("  vstupní aliasy: %s" % ", ".join(cfg["vstupni"]))
        print("  celý řetěz:     %s" % ", ".join(potreba))
        print("  klíč smí:       %s" % ", ".join(smi))

        chybi = [x for x in potreba if x not in smi]
        if chybi:
            chyb += len(chybi)
            print("  ==> CHYBA: v řetězu chybí %s. Fallback na ně se TIŠE "
                  "ROZBIJE (HTTP 403) a projeví se to až při výpadku "
                  "primárního modelu." % ", ".join(chybi))
        else:
            print("  ==> práva: celý řetěz je povolený")

        if a.bez_volani:
            continue
        kkey = secret(*cfg["secret"])
        for alias in potreba:
            ok, detail = zavolej(kkey, alias)
            if not ok:
                chyb += 1
            print("      %-16s %s  %s" % (alias, "OK  " if ok else "CHYBA", detail))

    print("\n" + "=" * 72)
    if chyb:
        print("SELHALO: %d problémů. Řetěz s dírou se projeví až ve chvíli, "
              "kdy na něm záleží." % chyb)
        return 1
    print("Všechny řetězy průchodné — práva i živá volání.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
