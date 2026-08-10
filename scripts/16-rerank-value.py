#!/usr/bin/env python3
"""Přínos rerankingu nad RRF fúzí — měřeno nad reálným korpusem.

`NASAZENI.md` vede `RERANK_TOP_K` jako nerozhodnutou otázku. Latence změřená
je (bez reranku 2,30 s, `top_k=10` 13,24 s, `top_k=20` 26,15 s), kvalita ne:
měření přesnosti v `corpus_measure.py` volá `/search` s `"rerank": False`,
takže dosud existují jen čísla BEZ reranku. Tenhle skript doplňuje druhou
stranu — jinak se rozhoduje mezi 2,3 s a 26 s naslepo.

**Měření je párové.** Každý dotaz projde všemi konfiguracemi nad týmž indexem,
takže se neporovnávají dvě nezávislá měření, ale tytéž dotazy. Při deseti
dotazech na jazyk je rozdíl jednoho dokumentu v rámci šumu; párové srovnání
"kde se pořadí zlepšilo a kde zhoršilo" nese víc informace než rozdíl dvou
průměrů, proto se tiskne obojí.

Spouští se na brainu (potřebuje `podman exec postgres` a síť na retrieval):

    /root/deploy/scripts/16-rerank-value.py

Doba běhu roste s cenou reranku: 4 jazyky × SAMPLES_PER_LANG dotazů × součet
latencí konfigurací. Ve výchozím nastavení počítej s půl hodinou.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

RS = "http://10.89.7.13:8080"
LANGS = ("cs", "en", "de", "la")
SAMPLES_PER_LANG = int(os.environ.get("SAMPLES_PER_LANG", "12"))
LIMIT = 10

# (popisek, tělo navíc k /search). Pořadí je i pořadím sloupců ve výstupu.
CONFIGS = [
    ("bez reranku", {"rerank": False}),
    ("rerank 10", {"rerank": True, "rerank_top_k": 10}),
    ("rerank 20", {"rerank": True, "rerank_top_k": 20}),
]
BASELINE = "bez reranku"


def psql(sql: str) -> str:
    p = subprocess.run(["podman", "exec", "-i", "postgres", "psql", "-X", "-U", "postgres",
                        "-d", "retrieval", "-t", "-A", "-F", "|", "-c", sql],
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"SQL selhalo: {p.stderr[:300]}")
    return p.stdout.strip()


def post(path: str, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(RS + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --- Vzorkování je ZÁMĚRNĚ shodné s corpus_measure.py -----------------------
# Stejné SQL i stejný výběr slov, aby byly výsledky srovnatelné s oddílem 3
# reportu. `ORDER BY md5(c.id::text)` je stabilní, takže stejné N dá stejné
# dotazy. Kopie místo importu proto, že corpus_measure.py měří už při importu.
STOPISH = re.compile(r"^[a-zà-öø-ÿāēīōū]{1,3}$", re.I)


def sample_chunks(lang: str, n: int) -> list[tuple[str, str]]:
    out = psql(f"""SELECT d.source_path, replace(substring(c.content for 400), '|', ' ')
                   FROM retrieval.chunk c JOIN retrieval.document d ON d.id = c.document_id
                   WHERE d.lang = '{lang}' AND d.source_path LIKE '_scale/%'
                     AND length(c.content) > 600
                   ORDER BY md5(c.id::text) LIMIT {n};""")
    rows = []
    for line in out.splitlines():
        if "|" in line:
            path, content = line.split("|", 1)
            rows.append((path.strip(), content.strip()))
    return rows


def make_keywords(content: str, k: int = 6) -> str:
    words, seen = [], set()
    for w in re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ž_]+", content):
        lw = w.lower()
        if len(w) < 5 or STOPISH.match(w) or lw in seen:
            continue
        seen.add(lw)
        words.append(w)
        if len(words) >= k:
            break
    return " ".join(words)


def position(results: list[dict], want: str) -> int | None:
    """Pozice správného dokumentu (1 = první), None když v odpovědi není."""
    for i, x in enumerate(results):
        if x["source_path"] == want:
            return i + 1
    return None


# --- Měření ----------------------------------------------------------------
print(f"Přínos rerankingu nad RRF fúzí — {SAMPLES_PER_LANG} dotazů na jazyk, "
      f"limit {LIMIT}")
print("Dotazy jsou klíčová slova vytažená z náhodného chunku, takže správný")
print("dokument v indexu prokazatelně je. Předávají se napřímo — neměří se")
print("přepis dotazu, jen retrieval.\n")

# pos[config][(lang, path)] = pozice; lat[config] = [s, ...]
pos: dict[str, dict[tuple[str, str], int | None]] = {c: {} for c, _ in CONFIGS}
lat: dict[str, list[float]] = {c: [] for c, _ in CONFIGS}
queries: list[tuple[str, str, str]] = []  # (lang, path, keywords)

for lang in LANGS:
    samples = sample_chunks(lang, SAMPLES_PER_LANG)
    if not samples:
        print(f"{lang}: žádné dokumenty, přeskakuji")
        continue
    for path, content in samples:
        kw = make_keywords(content)
        if kw:
            queries.append((lang, path, kw))

total = len(queries) * len(CONFIGS)
done = 0
t_start = time.time()

for lang, path, kw in queries:
    for label, extra in CONFIGS:
        body = {"query": kw, "keywords": kw, "lang": lang, "limit": LIMIT, **extra}
        try:
            t0 = time.time()
            r = post("/search", body)
            dt = time.time() - t0
        except Exception as e:
            print(f"  CHYBA {lang} {label}: {e}", flush=True)
            pos[label][(lang, path)] = None
            continue
        lat[label].append(dt)
        pos[label][(lang, path)] = position(r["results"], path)
        done += 1
        if done % 10 == 0:
            el = time.time() - t_start
            print(f"  ... {done}/{total} hotovo, uplynulo {el/60:.1f} min, "
                  f"odhad zbývá {(el/done*(total-done))/60:.1f} min", flush=True)


def metrics(label: str, keys: list[tuple[str, str]]) -> tuple[int, int, float]:
    top1 = sum(1 for k in keys if pos[label].get(k) == 1)
    top3 = sum(1 for k in keys if (pos[label].get(k) or 99) <= 3)
    mrr = sum(1.0 / p for k in keys if (p := pos[label].get(k)))
    return top1, top3, mrr / len(keys) if keys else 0.0


all_keys = [(lang, path) for lang, path, _ in queries]

print(f"\n{'=' * 70}\nKVALITA CELKEM ({len(all_keys)} dotazů)\n{'=' * 70}")
print(f"{'konfigurace':>14}  {'top-1':>11}  {'top-3':>11}  {'MRR':>6}  {'medián lat.':>12}")
for label, _ in CONFIGS:
    t1, t3, mrr = metrics(label, all_keys)
    med = statistics.median(lat[label]) if lat[label] else float("nan")
    print(f"{label:>14}  {t1:>3}/{len(all_keys)} ({100*t1/len(all_keys):>3.0f} %)  "
          f"{t3:>3}/{len(all_keys)} ({100*t3/len(all_keys):>3.0f} %)  {mrr:>6.3f}  {med:>11.2f}s")

print(f"\n{'=' * 70}\nKVALITA PO JAZYCÍCH (top-1 / MRR)\n{'=' * 70}")
print(f"{'jazyk':>6}  " + "  ".join(f"{label:>18}" for label, _ in CONFIGS))
for lang in LANGS:
    keys = [k for k in all_keys if k[0] == lang]
    if not keys:
        continue
    cells = []
    for label, _ in CONFIGS:
        t1, _t3, mrr = metrics(label, keys)
        cells.append(f"{t1:>2}/{len(keys)}  MRR {mrr:.3f}".rjust(18))
    print(f"{lang:>6}  " + "  ".join(cells))

print(f"\n{'=' * 70}\nPÁROVÉ SROVNÁNÍ PROTI '{BASELINE}'\n{'=' * 70}")
print("Kde se pořadí správného dokumentu zlepšilo, kde zhoršilo. Tohle je")
print("podstatnější než rozdíl průměrů — při tomhle počtu dotazů je jeden")
print("dokument sem tam šum, ale systematický posun jedním směrem není.\n")
for label, _ in CONFIGS:
    if label == BASELINE:
        continue
    lepsi = horsi = stejne = 0
    ztraceno = ziskano = 0
    for k in all_keys:
        a, b = pos[BASELINE].get(k), pos[label].get(k)
        if a is None and b is None:
            stejne += 1
        elif a is None:
            ziskano += 1
        elif b is None:
            ztraceno += 1
        elif b < a:
            lepsi += 1
        elif b > a:
            horsi += 1
        else:
            stejne += 1
    print(f"  {label}: zlepšilo {lepsi}, zhoršilo {horsi}, beze změny {stejne}, "
          f"nově nalezeno {ziskano}, ztraceno {ztraceno}")

print(f"\n{'=' * 70}\nCENA\n{'=' * 70}")
base_med = statistics.median(lat[BASELINE]) if lat[BASELINE] else 0.0
for label, _ in CONFIGS:
    if not lat[label]:
        continue
    med = statistics.median(lat[label])
    t1, _t3, mrr = metrics(label, all_keys)
    b1, _b3, bmrr = metrics(BASELINE, all_keys)
    if label == BASELINE:
        print(f"  {label}: {med:.2f}s")
        continue
    d_top1 = t1 - b1
    print(f"  {label}: {med:.2f}s  (+{med - base_med:.2f}s proti základu)  "
          f"za to top-1 {d_top1:+d} dokumentů, MRR {mrr - bmrr:+.3f}")

print(f"\nCelkem {done}/{total} měření, trvalo {(time.time() - t_start)/60:.1f} min.")
print("Rozhodnutí o RERANK_TOP_K patří do NASAZENI.md, bod 2 dalších kroků.")
