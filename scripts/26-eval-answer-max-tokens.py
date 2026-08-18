#!/usr/bin/env python3
"""Kolik tokenu opravdu potrebuje `reasoning` po prechodu na gpt-oss-120b?

Podklad k oprave P8(b). `ANSWER_MAX_TOKENS=8000` v Krytonovi je hodnota
zvolena kvuli big-pickle, ktery jako reasoning model utracel stovky tokenu
na `reasoning_content` driv, nez zacal psat. Vznikla po realnem selhani:
dotaz "udelej sumarizaci knih podle jazyka, kolik je kterych" spotreboval
2026-08-09 presne 3000 tokenu (tehdejsi strop) a vratil PRAZDNY content.

Ta hodnota se ale aplikuje i na FALLBACKY, ktere ji nepotrebuji, a je
primou pricinou zacykleni z 2026-08-17: gemma dostala 8000 tokenu volnosti,
melela 743 s, vyrobila 24 000 znaku a vysledek se ulozil do cache.

Krok 2 presunul `reasoning` na `openai/gpt-oss-120b`, u ktereho vyslo
reasoning_tokens median 109 a maximum 184. Timhle skriptem se overuje,
jestli jde strop bezpecne snizit — a na kolik.

MERI SE OBE CESTY, ktere `ANSWER_MAX_TOKENS` pouzivaji:
  * odpoved nad uryvky        (`core.py`, ANSWER_MODEL)
  * generovani SQL            (`analytics.py`, ANALYTICS_MODEL)
Obe jedou na tomtez aliasu a tomtez stropu, takze snizeni musi vyhovet
te horsi z nich.

Zamerne jsou v sade i AGREGACNI otazky — presne ten typ, ktery strop
vycerpal. U agregace nejde odpoved z uryvku slozit, takze model uvazuje
dlouho, misto aby rovnou napsal "na tohle je /korpus".

Spousteni:  python3 scripts/26-eval-answer-max-tokens.py
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

# Tentyz routing, jaky ma alias `reasoning` po kroku 2 — jinak by se
# merilo na jinem poskytovateli, nez na kterem to pobezi.
PROVIDER = {"order": ["DeepInfra", "Mancer 2", "BaseTen"],
            "allow_fallbacks": False}

# Strop pri MERENI je zamerne vysoky: chceme videt, kolik si model vezme,
# kdyz ho nic neomezuje. Kdyby tu bylo 2000, nezmerilo by se nic.
MAX_TOKENS_MERENI = 8000

SYSTEM = (
    "Jsi asistent nad osobními poznámkami uživatele. Odpovídej česky a POUZE "
    "na základě dodaného kontextu.\n"
    "Za každým tvrzením uveď odkaz na zdroj ve tvaru [1], [2] podle čísel "
    "úryvků níže.\n"
    "Když kontext na otázku neodpovídá, řekni to přímo — nedomýšlej si.\n"
    "Počty a souhrny ber VÝHRADNĚ z bloků s ověřenými čísly o korpusu "
    "a s výsledkem výpočtu nad databází, nikdy je nedopočítávej z úryvků. "
    "Když ověřená čísla na otázku neodpovídají, řekni to přímo a odkaž "
    "uživatele na stránku /korpus.\n"
    "Piš plynulou češtinou."
)

URYVKY = """Úryvky z poznámek:

[1] Index HNSW se v pgvectoru staví nad sloupcem typu vector nebo halfvec.
Parametr m určuje počet spojení na uzel, ef_construction kvalitu stavby.
Pro korpus v řádu statisíců chunků se osvědčilo m=16 a ef_construction=64;
vyšší hodnoty stavbu prodlužují, ale dotaz už nezrychlí.

[2] maintenance_work_mem rozhoduje o tom, jestli se index postaví v paměti
nebo přes disk. Při stavbě HNSW nad větší tabulkou jsem ho zvedal na 2 GB
a čas stavby spadl z jedenácti minut na necelé tři. Po dokončení se hodnota
vrací zpět, protože ji drží každé spojení zvlášť.

[3] Reranking přes bge-reranker-v2-m3 jsem měřil na lexikálních dotazech
a přínos nevyšel — pořadí prvních pěti výsledků se změnilo jen zřídka
a latence stoupla. Na sémantických dotazech neměřeno.

[4] Kniha Paradise Lost je v korpusu ve dvou jazycích, což rozbíjelo
statistiku, dokud se nepřidal sloupec lang. Dvojjazyčné knihy je potřeba
počítat jednou, ne dvakrát.

[5] Ranní procházka lesem trvá skoro hodinu a v září bývá chladněji, než
člověk čeká. Beru si svetr, i když odpoledne slibuje teplo."""

# (popis, otazka) — odpoved nad uryvky
ODPOVEDI = [
    ("bezna faktografie",   "Jak zrychlit stavbu HNSW indexu?"),
    ("bezna faktografie",   "Co dělá parametr ef_construction?"),
    ("past: neni v uryvcich", "Kolik stojí měsíčně provoz serveru brain?"),
    ("AGREGACE (past z P8)", "Udělej sumarizaci knih podle jazyka, kolik je kterých."),
    ("AGREGACE",            "Kolik mám celkem poznámek a jak se dělí podle témat?"),
    ("vicedilna otazka",    "Porovnej přínos rerankingu s přínosem zvýšení "
                            "maintenance_work_mem a řekni, co má větší dopad a proč."),
]

# Schema je zjednodusena kopie skutecneho z sql/02-retrieval.sql
# a sql/03-multilang.sql, aby prompt mel realnou velikost.
SCHEMA = """retrieval.document(id uuid, source_path text, title text, content_hash bytea, trust_level smallint, chunk_count integer, indexed_at timestamp with time zone, updated_at timestamp with time zone, meta jsonb, lang text)
retrieval.chunk(id bigint, document_id uuid, ordinal integer, content text, token_count integer, heading_path text, embedding halfvec, content_tsv tsvector, content_norm text, created_at timestamp with time zone, ts_config regconfig)"""

SQL_PROMPT = (
    "Jsi SQL analytik nad PostgreSQL databází osobních poznámek.\n"
    "Napiš JEDEN dotaz SELECT, který odpoví na otázku uživatele.\n"
    "Pravidla:\n"
    "- výhradně SELECT nebo WITH, nikdy nic, co zapisuje;\n"
    "- používej jen tabulky a sloupce ze schématu níž, nic si nevymýšlej;\n"
    "- tabulky piš plně kvalifikovaně, tedy `retrieval.document`;\n"
    "- pojmenuj vypočtené sloupce srozumitelně česky přes AS;\n"
    "- když se na otázku ze schématu odpovědět NEDÁ, vrať prázdné sql.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"sql":"<dotaz>","vysvetleni":"<jedna věta česky>"}\n\n'
    "Schéma:\n%s\n\nOtázka: %s"
)

SQL_OTAZKY = [
    ("jednoduchy pocet",  "Kolik mám dokumentů?"),
    ("groupby s jazykem", "Kolik dokumentů mám v každém jazyce?"),
    ("slozitejsi join",   "Které dokumenty mají nejvíc chunků a kolik jich je?"),
    ("nezodpoveditelne",  "Kolik stojí měsíčně provoz serveru?"),
    ("nejtezsi",          "Ukaž průměrný počet tokenů na chunk podle jazyka "
                          "a jen pro dokumenty indexované posledních 30 dní."),
]


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


def call(messages):
    """(reasoning_tok, content_tok, celkem_tok, finish, sekundy, text, chyba)."""
    telo = {"model": MODEL, "messages": messages, "temperature": 0,
            "max_tokens": MAX_TOKENS_MERENI, "provider": PROVIDER}
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
        return None, None, None, None, time.time() - t0, "", \
            f"HTTP {e.code}: {e.read()[:150].decode(errors='replace')}"
    except Exception as e:
        return None, None, None, None, time.time() - t0, "", f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    u = d.get("usage") or {}
    ch = (d.get("choices") or [{}])[0]
    txt = ((ch.get("message") or {}).get("content") or "").strip()
    celkem = u.get("completion_tokens")
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    return rt, (celkem - rt) if celkem is not None else None, celkem, \
        ch.get("finish_reason"), dt, txt, ""


vsechny = []


def sada(nazev, polozky, stavitel):
    print("=" * 74)
    print(nazev)
    print("=" * 74, flush=True)
    for popis, otazka in polozky:
        rt, ct, celkem, fin, dt, txt, err = call(stavitel(otazka))
        if err:
            print("  SELHALO  %-34s %s" % (popis[:34], err))
            time.sleep(1.5)
            continue
        vsechny.append((nazev, popis, otazka, rt, ct, celkem, fin, dt))
        prazdno = "  !! PRAZDNY CONTENT" if not txt else ""
        print("  %-34s uvaha=%-5s text=%-5s celkem=%-5s %-6s %5.1f s%s" % (
            popis[:34], rt, ct, celkem, fin, dt, prazdno))
        if not txt:
            print("       (finish=%s — tohle je presne to tiche selhani z P8)" % fin)
        time.sleep(1.5)
    print()


sada("ODPOVED NAD URYVKY (core.py)", ODPOVEDI,
     lambda q: [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"{URYVKY}\n\nOtázka: {q}"}])
sada("GENEROVANI SQL (analytics.py)", SQL_OTAZKY,
     lambda q: [{"role": "user", "content": SQL_PROMPT % (SCHEMA, q)}])

print("=" * 74)
print("SOUHRN")
print("=" * 74)
celky = [v[5] for v in vsechny if v[5] is not None]
uvahy = [v[3] for v in vsechny if v[3] is not None]
if celky:
    print("celkem vystupnich tokenu: median %d, prumer %d, MAXIMUM %d (n=%d)" % (
        statistics.median(celky), sum(celky) / len(celky), max(celky), len(celky)))
    print("z toho uvaha:             median %d, MAXIMUM %d" % (
        statistics.median(uvahy), max(uvahy)))
    print()
    print("Nejzravejsi tri:")
    for v in sorted(vsechny, key=lambda x: -(x[5] or 0))[:3]:
        print("   %-5s tokenu  %-30s %s" % (v[5], v[1][:30], v[2][:44]))
    print()
    mx = max(celky)
    print("DOPORUCENY STROP: %d (maximum %d + 3x rezerva, zarovnano nahoru)" % (
        max(1000, ((mx * 3) // 500 + 1) * 500), mx))
    print("Dnesni hodnota je 8000. Pozor: rezerva ma smysl — merena sada")
    print("nikdy nepokryje vsechny budouci dotazy a strop je posledni")
    print("ochrana proti zacykleni, ne cilova hodnota.")
nep = [v for v in vsechny if v[6] == "length"]
if nep:
    print("\n!! %d odpovedi doslo na finish_reason=length i pri stropu %d —"
          % (len(nep), MAX_TOKENS_MERENI))
    print("   snizovat NELZE, dokud se to nevysvetli.")
