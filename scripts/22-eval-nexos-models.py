#!/usr/bin/env python3
"""Srovnání modelů na nexos.ai pro aliasy `cheap` / `workhorse` / `backstop`.

Sesterský skript k `17-eval-cheap-models.py`. Prompty, testovací korpus
i parsovací funkce jsou z něj převzaté DOSLOVA, aby čísla šla porovnat
jedno k jednomu s měřením na OpenRouteru z 2026-08-10. Jediné, co se
mění, je koncový bod a odkud se bere klíč.

ROZDÍLY PROTI 17, na které pozor při čtení výsledků:

1. `nexos_credits_cost` v `usage` — nexos.ai vrací cenu každého volání.
   Skript ji sčítá, takže na konci je skutečná cena měření, ne odhad.

2. `x-nexos-cache` — gateway má vlastní cache NAD rámec té v LiteLLM.
   Stejná past jako `cache: true` v litellm-configu: zopakovaný prompt
   se vrátí zadarmo a netestuje nic. Skript hlavičku čte a počet zásahů
   hlásí; když není nula, měření je podezřelé.

3. Katalog je uzavřený. Model, který není v konzoli povolený, vrací
   `{"error":{"code":100110,"message":"Model not found"}}` — a to i při
   volání přes `nexos_model_id`. Ověřeno 2026-08-18 na `GPT 5 nano`,
   `Gemma 4 31B` a `GPT-OSS 20b`. Seznam dole tedy NENÍ výběr toho
   nejlepšího z katalogu, ale toho, co je zapnuté.

Spouštěj ze stanice:  python3 scripts/22-eval-nexos-models.py
Klíč čte z ~/.config/nexos/key.env (řádek NEXOS_API_KEY=...).
"""

# Skript ma bezet i na stanici (Python 3.8), nejen na brainu — anotace typu
# `tuple[str, float, str]` by se tam jinak vyhodnocovaly za behu a spadly.
from __future__ import annotations

import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request

API = "https://api.nexos.ai/v1/chat/completions"

# (zobrazovaný název na nexos.ai, poznámka do výstupu)
#
# Řazeno podle ceny vstupu. Pro srovnání, co dnes stojí OpenRouter:
#   ling-2.6-flash      0,010 / 0,030  $/M   = dnešní `cheap`
#   gemma-4-26b-a4b-it  0,070 / ?      $/M   = dnešní `cheap-fallback`
#   :free varianty      0     / 0            = dnešní `workhorse`, `backstop`
MODELS = [
    ("Gemini 3 Flash Preview", "0,500/3,000 $/M — nejlevnější zapnutý"),
    ("GPT 5.4 mini",           "0,750/4,500 $/M"),
    ("GPT-OSS 120b",           "0,800/1,600 $/M — hostuje nexos.ai"),
    ("Claude Haiku 4.5",       "1,100/5,500 $/M"),
]

# --- Prompty jsou DOSLOVA z retrieval-service/app/rewrite.py ---------------
REWRITE_PROMPT = (
    "Z dotazu vyber klíčová slova pro fulltextové hledání v poznámkách "
    "a urči jazyk dotazu.\n"
    "Pravidla pro klíčová slova: jen slova, která se pravděpodobně vyskytují "
    "v textu; zahoď tázací slova, předložky a spojky; NEPŘEKLÁDEJ, ponech je "
    "v jazyce dotazu; zachovej diakritiku; zachovej identifikátory a názvy "
    "přesně (např. maintenance_work_mem).\n"
    "Tvary slov: je-li dotaz česky, převeď je do 1. pádu jednotného čísla. "
    "V jiném jazyce je NECH přesně tak, jak jsou v dotazu — přehnaná "
    "normalizace shodu naopak zabije (anglické 'faster' se nesmí měnit "
    "na 'fast').\n"
    "Jazyk je jeden z: cs, en, de, la.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"lang":"<kód>","keywords":"<slova oddělená mezerou>"}\n\n'
    "Dotaz: "
)
DETECT_PROMPT = (
    "Urči jazyk následujícího textu. Možnosti: cs (čeština), en (angličtina), "
    "de (němčina), la (latina). Když to není přesně žádný z nich, vyber "
    "nejbližší.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"lang":"<kód>"}\n\n'
    "Text:\n"
)

# --- Parsování je DOSLOVA z rewrite.py ------------------------------------
LANGS = frozenset({"cs", "en", "de", "la"})
_ALIASES = {"cze": "cs", "ces": "cs", "czech": "cs", "cesky": "cs",
            "eng": "en", "english": "en", "ger": "de", "deu": "de",
            "german": "de", "deutsch": "de", "lat": "la", "latin": "la",
            "latina": "la"}


def normalize(value, default=None):
    if not value:
        return default
    code = str(value).strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in LANGS else _ALIASES.get(code, default)


def json_object(out: str):
    dec = json.JSONDecoder()
    start = 0
    while True:
        i = out.find("{", start)
        if i < 0:
            return None
        try:
            data, _ = dec.raw_decode(out, i)
        except ValueError:
            start = i + 1
            continue
        return data if isinstance(data, dict) else None


def json_field(out: str, key: str):
    data = json_object(out)
    if data is None:
        return None
    value = data.get(key)
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value).strip()


def clean_terms(raw: str) -> str:
    out = raw.splitlines()[0].strip().strip('"\'` ').lstrip("-•* ") if raw else ""
    if not out or len(out) > 300:
        raise RuntimeError(f"nepouzitelna odpoved: {out[:80]!r}")
    return out


# --- Testovací sady --------------------------------------------------------
# Vzorky na detekci: 5 na jazyk. Schválně jde o souvislý text bez vlastních
# jmen, která by jazyk prozradila zadarmo.
DETECT = [
    ("cs", "Databázový index se staví nad sloupcem, který se často objevuje "
           "v podmínkách dotazu. Bez něj musí server projít celou tabulku "
           "řádek po řádku, což je u větších dat neúnosně pomalé."),
    ("cs", "Ráno bývá chladněji, než člověk čeká, a tak si beru svetr i tehdy, "
           "když odpoledne slibuje teplo. Cesta lesem trvá skoro hodinu."),
    ("cs", "Když se rozhodneš měnit nastavení, zapiš si původní hodnoty. "
           "Vracet se poslepu k něčemu, co fungovalo, stojí mnohem víc času "
           "než ta poznámka na začátku."),
    ("cs", "Vzpomínka na dětství se mi vybavila nečekaně, u obyčejné vůně "
           "mokrého listí. Stál jsem chvíli na místě a nechtěl jít dál."),
    ("cs", "Vyhledávání kombinuje několik větví a jejich výsledky slučuje "
           "podle pořadí, ne podle skóre. Skóre z různých metod totiž nejsou "
           "vzájemně srovnatelná."),
    ("en", "The index is built over a column that appears frequently in query "
           "conditions. Without it the server must scan the entire table row "
           "by row, which becomes unbearably slow on larger data."),
    ("en", "Mornings are colder than one expects, so I take a sweater even "
           "when the afternoon promises warmth. The walk through the forest "
           "takes nearly an hour."),
    ("en", "If you decide to change a setting, write down the original value. "
           "Groping your way back to something that worked costs far more "
           "time than that note would have."),
    ("en", "The memory of childhood came back unexpectedly, at the ordinary "
           "smell of wet leaves. I stood still for a moment and did not want "
           "to walk on."),
    ("en", "Search combines several branches and merges their results by rank "
           "rather than by score, because scores from different methods are "
           "not comparable with one another."),
    ("de", "Der Index wird über einer Spalte aufgebaut, die häufig in den "
           "Bedingungen der Abfrage vorkommt. Ohne ihn muss der Server die "
           "ganze Tabelle Zeile für Zeile durchgehen."),
    ("de", "Morgens ist es kälter, als man erwartet, deshalb nehme ich einen "
           "Pullover mit, auch wenn der Nachmittag Wärme verspricht. Der Weg "
           "durch den Wald dauert fast eine Stunde."),
    ("de", "Wenn du eine Einstellung änderst, schreibe dir den ursprünglichen "
           "Wert auf. Sich blind zu etwas zurückzutasten, das funktioniert "
           "hat, kostet weit mehr Zeit."),
    ("de", "Die Erinnerung an die Kindheit kam unerwartet zurück, beim "
           "gewöhnlichen Geruch nasser Blätter. Ich blieb einen Augenblick "
           "stehen und wollte nicht weitergehen."),
    ("de", "Die Suche verbindet mehrere Zweige und führt ihre Ergebnisse nach "
           "dem Rang zusammen, nicht nach der Bewertung, denn Bewertungen "
           "verschiedener Verfahren sind nicht vergleichbar."),
    ("la", "Index super columna aedificatur quae saepe in condicionibus "
           "interrogationis apparet. Sine eo servus totam tabulam per singulos "
           "versus percurrere debet, quod in magnis datis lentum est."),
    ("la", "Mane frigidius esse solet quam quis exspectat, itaque vestem "
           "lanaeam sumo etiam cum meridies calorem promittit. Iter per "
           "silvam fere horam tenet."),
    ("la", "Si quid mutare constitueris, pristinos numeros adnota. Caeco modo "
           "ad id redire quod bene se habebat multo plus temporis constat "
           "quam illa nota."),
    ("la", "Memoria pueritiae subito rediit, ex odore vulgari foliorum "
           "umidorum. Paulisper stetti neque longius ire volui."),
    ("la", "Inquisitio plures ramos coniungit et eventus eorum secundum "
           "ordinem componit, non secundum aestimationem, quia aestimationes "
           "variarum rationum inter se comparari non possunt."),
]

# (dotaz, očekávaný jazyk, [podřetězce, které MUSÍ zůstat],
#  [podřetězce, které NESMÍ ve výsledku být])
REWRITE = [
    ("Jak nastavit maintenance_work_mem při stavbě indexu?", "cs",
     ["maintenance_work_mem"], []),
    ("Proč jsou databases running faster po zvýšení paměti?", "en",
     ["faster"], []),
    ("Ubi manet amicorum memoria?", "la", [], ["ubi"]),
    ("Čím se ladí latence dotazu u HNSW indexu?", "cs", ["HNSW"], []),
    ("Wie funktioniert die Lemmatisierung im Suchindex?", "de", [], []),
    ("Jaká je první věta z knihy Paradise Lost?", "cs", ["Paradise"], []),
    ("What does ef_search change in an HNSW index?", "en",
     ["ef_search"], []),
    ("Které vektorové indexy se ukládají jako halfvec?", "cs",
     ["halfvec"], []),
    ("Warum ist die Seitenzwischenspeicherung im Container geteilt?", "de",
     [], []),
    ("Quid est fons veritatis et quid index derivatus?", "la", [], ["quid"]),
]

# Volné psaní česky. Skóre se nedá spočítat, výstup se tiskne k přečtení —
# `ling` sem propadl slovem „Viedeň" místo „Vídeň", což žádná metrika
# nezachytí, ale čtenář ano.
PROSE = [
    "Napiš dvě věty česky o tom, proč se hlavní město Rakouska turistům líbí.",
    "Napiš dvě věty česky o rozdílu mezi pamětí a diskem u databáze.",
    "Napiš dvě věty česky o tom, co dělá knihovník ve školní knihovně.",
]




def api_key() -> str:
    """Klíč z prostředí, jinak ze souboru mimo repo. Nikdy z argv.

    Na brainu poběží skript v kontejneru s `NEXOS_API_KEY` z podman secretu,
    na stanici se bere ze souboru. Pořadí je schválně tohle — kdyby se klíč
    někdy dostal do obojího, platí ten nasazený.
    """
    env = os.environ.get("NEXOS_API_KEY", "").strip()
    if env:
        return env
    p = pathlib.Path.home() / ".config" / "nexos" / "key.env"
    try:
        for line in p.read_text().splitlines():
            if line.startswith("NEXOS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"\'')
                if key:
                    return key
    except OSError as e:
        sys.exit(f"nepodarilo se precist {p}: {e}")
    sys.exit(f"v {p} chybi radek NEXOS_API_KEY=...")


KEY = api_key()

# Rozestup a opakování — viz poznámka v 17: první měření tam bylo zkažené
# rate limitem, který se do výsledků zapsal jako selhání kvality modelu.
GAP = 1.5
RETRY_429 = 4

COST = {"total": 0.0, "calls": 0, "cache_hits": 0}


def call(model: str, prompt: str, max_tokens: int) -> tuple[str, float, str]:
    """(content, sekundy, chyba). Prázdný content je chyba, jako v `_call`."""
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0}).encode()
    last = ""
    for attempt in range(RETRY_429 + 1):
        req = urllib.request.Request(API, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KEY}",
            # POVINNE. Pred api.nexos.ai sedi Cloudflare a vychozi
            # `Python-urllib/3.x` odmita s HTTP 403 "error code: 1010"
            # (zakaz podle signatury klienta). Vypada to jako odmitnuty
            # klic, ale neni — s JAKOUKOLIV vlastni hodnotou UA projde.
            # Overeno 2026-08-18: bez hlavicky 403 u vsech ctyr modelu,
            # s "curl/7.81.0" i "druhy-mozek-eval/1.0" HTTP 200.
            # `httpx` v retrieval-service posila vlastni UA, takze se ho
            # to netyka; tyka se to nastroju psanych nad urllib.
            "User-Agent": "druhy-mozek-eval/1.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
                if (r.headers.get("x-nexos-cache") or "").lower() == "hit":
                    COST["cache_hits"] += 1
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"
            if e.code == 429 and attempt < RETRY_429:
                time.sleep(5 * (attempt + 1))
                continue
            return "", time.time() - t0, last
        except Exception as e:
            # Sitova chyba (typicky vypadek DNS) NENI vlastnost modelu.
            # Prvni beh 2026-08-18 takhle prisel o 3,5 modelu ze 4: uprostred
            # mereni spadlo rozliseni jmen a do vysledku se to zapsalo jako
            # "0/20 selhalo", tedy jako by model neodpovidal. Opakuj stejne
            # jako u 429, at se sitovy vypadek neplete do kvality.
            last = f"{type(e).__name__}: {e}"
            if attempt < RETRY_429:
                time.sleep(5 * (attempt + 1))
                continue
            return "", time.time() - t0, last
        dt = time.time() - t0
        usage = d.get("usage") or {}
        COST["total"] += float(usage.get("nexos_credits_cost") or 0)
        COST["calls"] += 1
        ch = (d.get("choices") or [{}])[0]
        out = ((ch.get("message") or {}).get("content") or "").strip()
        time.sleep(GAP)
        if not out:
            # Past reasoning modelu: tokeny padnou na uvahu, content zustane
            # prazdny. Presne tohle dela big-pickle i ling-3.0-flash.
            rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            extra = f", reasoning_tokens={rt}" if rt else ""
            return "", dt, f"prazdna odpoved (finish_reason={ch.get('finish_reason')}{extra})"
        return out, dt, ""
    return "", 0.0, last


results = {}

for model, note in MODELS:
    print(f"\n{'=' * 72}\n{model}   — {note}\n{'=' * 72}", flush=True)
    r = {"detect_ok": 0, "detect_bad": [], "detect_fail": [],
         "rw_ok": 0, "rw_bad": [], "rw_fail": [], "lat": [], "prose": []}

    for want, text in DETECT:
        out, dt, err = call(model, DETECT_PROMPT + text[:1200], 20)
        r["lat"].append(dt)
        if err:
            r["detect_fail"].append((want, err))
            continue
        got = normalize(json_field(out, "lang"))
        if got is None:
            first = out.strip().strip('"\'`.').split()
            got = normalize(first[0] if first else None)
        if got == want:
            r["detect_ok"] += 1
        else:
            r["detect_bad"].append((want, got, out[:60]))

    for query, want_lang, must, forbid in REWRITE:
        out, dt, err = call(model, REWRITE_PROMPT + query, 120)
        r["lat"].append(dt)
        if err:
            r["rw_fail"].append((query[:38], err))
            continue
        raw = json_field(out, "keywords")
        got_lang = normalize(json_field(out, "lang"))
        try:
            kw = clean_terms(raw if raw is not None else out)
        except RuntimeError as e:
            r["rw_fail"].append((query[:38], str(e)))
            continue
        problems = []
        if raw is None:
            problems.append("JSON se nerozparsoval")
        if got_lang != want_lang:
            problems.append(f"jazyk {got_lang} misto {want_lang}")
        for m in must:
            if m.lower() not in kw.lower():
                problems.append(f"ztraceno {m!r}")
        for f in forbid:
            if f.lower() in kw.lower().split():
                problems.append(f"ponechano {f!r}")
        if problems:
            r["rw_bad"].append((query[:38], kw[:60], "; ".join(problems)))
        else:
            r["rw_ok"] += 1

    for p in PROSE:
        out, _dt, err = call(model, p, 200)
        r["prose"].append(err or out.replace("\n", " ")[:220])

    results[model] = r
    nd, nr = len(DETECT), len(REWRITE)
    print(f"  detekce jazyka : {r['detect_ok']}/{nd}"
          f"   (spatne {len(r['detect_bad'])}, selhalo {len(r['detect_fail'])})")
    print(f"  prepis dotazu  : {r['rw_ok']}/{nr}"
          f"   (spatne {len(r['rw_bad'])}, selhalo {len(r['rw_fail'])})")
    if r["lat"]:
        print(f"  latence        : median {statistics.median(r['lat']):.2f} s, "
              f"max {max(r['lat']):.2f} s")
    for want, got, out in r["detect_bad"]:
        print(f"    ! detekce: cekano {want}, dostal {got}   {out!r}")
    for q, err in r["detect_fail"][:4]:
        print(f"    ! detekce SELHALA ({q}): {err}")
    if len(r["detect_fail"]) > 4:
        print(f"    ! ... a dalsich {len(r['detect_fail']) - 4} selhani detekce")
    for q, kw, why in r["rw_bad"]:
        print(f"    ! prepis {q!r} -> {kw!r}   [{why}]")
    for q, err in r["rw_fail"][:4]:
        print(f"    ! prepis SELHAL {q!r}: {err}")
    if len(r["rw_fail"]) > 4:
        print(f"    ! ... a dalsich {len(r['rw_fail']) - 4} selhani prepisu")

print(f"\n\n{'=' * 72}\nSOUHRN\n{'=' * 72}")
print(f"{'model':<26} {'detekce':>9} {'prepis':>8} {'median':>8} {'max':>8}")
for model, note in MODELS:
    r = results[model]
    med = statistics.median(r["lat"]) if r["lat"] else float("nan")
    mx = max(r["lat"]) if r["lat"] else float("nan")
    print(f"{model:<26} {r['detect_ok']:>4}/{len(DETECT):<4} "
          f"{r['rw_ok']:>3}/{len(REWRITE):<4} {med:>7.2f}s {mx:>7.2f}s")

print(f"\ncena mereni: {COST['total']:.6f} kreditu ve {COST['calls']} volanich")
if COST["cache_hits"]:
    print(f"POZOR: {COST['cache_hits']} odpovedi prislo z cache gateway — "
          f"mereni je zkreslene, vysledky neber vazne")

print(f"\n{'=' * 72}\nVOLNÉ PSANÍ ČESKY — posoudit přečtením, ne metrikou\n{'=' * 72}")
for model, note in MODELS:
    print(f"\n--- {model} ({note})")
    for i, text in enumerate(results[model]["prose"]):
        print(f"  [{i + 1}] {text}")
