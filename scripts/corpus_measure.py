#!/usr/bin/env python3
"""Měření a mechanismové testy nad velkým korpusem. Volá 10-scale-test.sh,
ale jde spustit i samostatně, když indexace už proběhla.

Tohle je vlastní test. Indexace jen připraví podmínky.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

RS = "http://10.89.7.13:8080"
SCALE_DIR = "/srv/brain/markdown/_scale"
STATE = "/root/corpus/state"


def psql(sql: str) -> str:
    p = subprocess.run(["podman", "exec", "-i", "postgres", "psql", "-X", "-U", "postgres",
                        "-d", "retrieval", "-t", "-A", "-F", "|", "-c", sql],
                       capture_output=True, text=True)
    if p.returncode:
        return f"SQL CHYBA: {p.stderr[:300]}"
    return p.stdout.strip()


def post(path: str, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(RS + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(path: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(RS + path, timeout=timeout) as r:
        return json.load(r)


def h(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------
h("1. ROZSAH KORPUSU")
s = get("/stats")
print(f"dokumentů:            {s['documents']}")
print(f"chunků:               {s['chunks']}")
print(f"bez embeddingu:       {s['chunks_without_embedding']}")
print(f"nedokončených:        {s['documents_unfinished']}")
print(f"HNSW index:           {s['hnsw_index_present']}")
print(f"dokumenty po jazyce:  {s['documents_by_lang']}")
print(f"chunky po ts_config:  {s['chunks_by_ts_config']}")
print("\nchunků na dokument:")
print(psql("""SELECT lang, count(*) AS dokumentu, min(chunk_count), round(avg(chunk_count)),
                     max(chunk_count), sum(chunk_count)
              FROM retrieval.document GROUP BY lang ORDER BY lang;"""))
print("\nvelikosti v databázi:")
print(psql("""SELECT pg_size_pretty(pg_total_relation_size('retrieval.chunk')) AS chunk_celkem,
                     pg_size_pretty(pg_relation_size('retrieval.chunk')) AS chunk_data,
                     pg_size_pretty(pg_total_relation_size('retrieval.document')) AS dokumenty;"""))
print(psql("""SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid))
              FROM pg_stat_user_indexes WHERE schemaname='retrieval' ORDER BY 1;"""))
try:
    with open(f"{STATE}/hnsw_seconds") as f:
        print(f"\nstavba HNSW:          {int(f.read().strip())} s")
except OSError:
    print("\nstavba HNSW:          (neměřeno)")

# ---------------------------------------------------------------------
h("2. DETEKCE JAZYKA VE VELKÉM")
# Každý desátý dokument se generoval bez frontmatteru, takže jeho jazyk určil
# `cheap`. Pravda je v názvu souboru, který generátor odvodil od zdrojového
# textu — porovnáním vznikne skutečná přesnost, ne vzorek o velikosti 1.
rows = psql("""SELECT source_path, lang FROM retrieval.document
               WHERE source_path LIKE '_scale/%' ORDER BY source_path;""")
ok = bad = 0
misses: list[str] = []
for line in rows.splitlines():
    if "|" not in line:
        continue
    path, lang = line.rsplit("|", 1)
    m = re.match(r"_scale/([a-z]{2})-(\d+)\.md", path)
    if not m:
        continue
    truth, num = m.group(1), int(m.group(2))
    if num % 10 != 0:          # ostatní mají lang: ve frontmatteru, netestují detekci
        continue
    if lang == truth:
        ok += 1
    else:
        bad += 1
        if len(misses) < 15:
            misses.append(f"  {path}: čekáno {truth}, detekováno {lang}")
total = ok + bad
if total:
    print(f"dokumentů bez frontmatteru: {total}")
    print(f"správně detekováno:         {ok} ({100*ok/total:.1f} %)")
    print(f"špatně:                     {bad}")
    for m2 in misses:
        print(m2)
else:
    print("žádné dokumenty bez frontmatteru — přeskočeno")

# ---------------------------------------------------------------------
h("3. PŘESNOST S KNOWN-GOOD DOKUMENTEM (dotazy Z KORPUSU)")
# Dotazy MUSÍ vycházet z indexovaného textu. Vymyšlené otázky nad korpusem
# beletrie nenajdou nic a nezměří vůbec nic — ověřeno tím, že první verze
# tohoto skriptu se ptala na "paměť a rychlost" nad Čapkovým R.U.R.
#
# Postup je stejný jako u 06-eval-reranker.py: ke každému dotazu existuje
# právě jeden správný dokument, takže jde spočítat top-1 a MRR.
SAMPLES_PER_LANG = 10
STOPISH = re.compile(r"^[a-zà-öø-ÿāēīōū]{1,3}$", re.I)


def sample_chunks(lang: str, n: int) -> list[tuple[str, str]]:
    """(source_path, content) z náhodných chunků daného jazyka."""
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
    """Z chunku vybere delší slova — to je dotaz, který v textu prokazatelně je."""
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


for lang in ("cs", "en", "de", "la"):
    samples = sample_chunks(lang, SAMPLES_PER_LANG)
    if not samples:
        print(f"{lang}: žádné dokumenty, přeskakuji")
        continue
    top1 = 0
    rr_sum = 0.0
    branch_counts = {"dense": 0, "lexical": 0, "fuzzy": 0}
    for path, content in samples:
        kw = make_keywords(content)
        if not kw:
            continue
        try:
            # `keywords` se předává napřímo: měří se retrieval, ne přepis
            # dotazu. Kvalita rewritu se testuje jinde (bod 6).
            r = post("/search", {"query": kw, "keywords": kw, "lang": lang,
                                 "rerank": False, "limit": 10})
        except Exception as e:
            print(f"  {lang} CHYBA: {e}")
            continue
        paths = [x["source_path"] for x in r["results"]]
        if paths and paths[0] == path:
            top1 += 1
        if path in paths:
            rr_sum += 1.0 / (paths.index(path) + 1)
        for x in r["results"]:
            if x["source_path"] == path:
                for key, field in (("dense", "r_dense"), ("lexical", "r_lexical"),
                                   ("fuzzy", "r_fuzzy")):
                    if x[field] is not None:
                        branch_counts[key] += 1
                break
    n = len(samples)
    print(f"{lang}: top-1 {top1}/{n} ({100*top1/n:.0f} %)  MRR {rr_sum/n:.3f}  "
          f"větve u správného dokumentu: {branch_counts}")

# ---------------------------------------------------------------------
h("4. LATENCE NAD PLNÝM INDEXEM A CENA RERANKU")
# RERANK_TOP_K je podle NASAZENI.md nerozhodnutá otázka (20 = 3,8 s).
# Tohle měření je nad reálnými 1200znakovými chunky, ne nad krátkými
# testovacími poznámkami — a to je rozdíl, který rozhoduje.
probe = sample_chunks("cs", 1)
probe_kw = make_keywords(probe[0][1]) if probe else "test"
print(f"sonda: {probe_kw!r}\n")
print(f"{'rerank_top_k':>13}  {'latence':>9}")
for top_k in (0, 5, 10, 20, 40):
    body = {"query": probe_kw, "keywords": probe_kw, "lang": "cs",
            "rerank": bool(top_k), "rerank_top_k": top_k or None}
    body = {k: v for k, v in body.items() if v is not None}
    try:
        t0 = time.time()
        post("/search", body)
        dt = time.time() - t0
    except Exception as e:
        print(f"{top_k:>13}  CHYBA {e}")
        continue
    print(f"{top_k if top_k else 'bez reranku':>13}  {dt:8.2f}s")

# ---------------------------------------------------------------------
h("5. MECHANISMY NA STOCHUNKOVÉM DOKUMENTU")
# Tohle je to, co malý vzorek ověřit nemohl.
victim = None
for name in sorted(os.listdir(SCALE_DIR)):
    if name.startswith("cs-") and name.endswith(".md"):
        victim = os.path.join(SCALE_DIR, name)
        break
if not victim:
    print("nenalezen český dokument, přeskakuji")
else:
    rel = "_scale/" + os.path.basename(victim)
    n_chunks = psql(f"SELECT chunk_count FROM retrieval.document WHERE source_path='{rel}';")
    print(f"pokusný dokument: {rel}, {n_chunks} chunků\n")
    with open(victim, encoding="utf-8") as f:
        original = f.read()

    def reindex_and_report(label: str) -> dict:
        t0 = time.time()
        res = post("/reindex?wait=true", {}, timeout=7200)
        dt = time.time() - t0
        print(f"{label}\n  {json.dumps({k: res[k] for k in ('new','changed','unchanged','resumed','chunks_embedded','chunks_recycled')}, ensure_ascii=False)}")
        print(f"  reindex trval {dt:.1f} s (sken celého korpusu + zásah)")
        return res

    # 5a. Změna JEDNOHO odstavce uprostřed: má se přeembeddovat jen ten chunk.
    lines = original.split("\n")
    mid = len(lines) // 2
    for i in range(mid, len(lines)):
        if len(lines[i]) > 200:
            lines[i] = lines[i][:100] + " ZMENENY ODSTAVEC PRO TEST RECYKLACE " + lines[i][100:]
            break
    with open(victim, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    r1 = reindex_and_report("5a. změna jednoho odstavce uprostřed:")
    print(f"  -> očekáváno: pár embeddingů, zbytek recyklovaný. "
          f"Skutečnost: {r1['chunks_embedded']} embeddingů, {r1['chunks_recycled']} recyklovaných")

    # 5b. Vložení odstavce na ZAČÁTEK: posune ordinal všem následujícím.
    # Kdyby se recyklovalo podle pořadí, přeembedduje se celý dokument.
    with open(victim, encoding="utf-8") as f:
        cur = f.read()
    marker = "\n\nVLOZENY ODSTAVEC NA ZACATKU, KTERY POSUNE VSECHNY ORDINALY O JEDNA. " \
             "Text je dost dlouhy na to, aby vznikl samostatny chunk a nesloucil se " \
             "s nasledujicim, protoze chunker slepuje jen kusy pod osmdesat znaku.\n\n"
    idx = cur.find("\n## ")
    cur = cur[:idx] + marker + cur[idx:] if idx > 0 else marker + cur
    with open(victim, "w", encoding="utf-8") as f:
        f.write(cur)
    r2 = reindex_and_report("5b. vložení odstavce na začátek (posun ordinálů):")
    print(f"  -> pokud recyklace klíčuje podle OBSAHU, je embeddingů málo. "
          f"Skutečnost: {r2['chunks_embedded']} embeddingů, {r2['chunks_recycled']} recyklovaných")

    # 5c. Změna JEN jazyka: nesmí stát ani jeden embedding.
    with open(victim, encoding="utf-8") as f:
        cur = f.read()
    cur2 = re.sub(r"^lang: cs$", "lang: en", cur, count=1, flags=re.M)
    changed_lang = cur2 != cur
    with open(victim, "w", encoding="utf-8") as f:
        f.write(cur2)
    if changed_lang:
        r3 = reindex_and_report("5c. změna pouze řádku lang: cs -> en:")
        print(f"  -> očekáváno 0 embeddingů. Skutečnost: {r3['chunks_embedded']}")
        print("  ts_config po změně: " + psql(
            f"SELECT DISTINCT ts_config::text FROM retrieval.chunk c "
            f"JOIN retrieval.document d ON d.id=c.document_id WHERE d.source_path='{rel}';"))
    else:
        print("5c. dokument nemá frontmatter lang, přeskočeno")

    # Úklid: vrať původní obsah a doindexuj zpátky.
    with open(victim, "w", encoding="utf-8") as f:
        f.write(original)
    reindex_and_report("5d. návrat do původního stavu:")

print("\n\nHOTOVO " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
