#!/usr/bin/env python3
"""Přečte /stats na stdin a vypíše ho čitelně. Volá 11-scale-report.sh.

Samostatný soubor záměrně: tenhle výpis byl původně inline `python3 -c` uvnitř
jednoduchých uvozovek v shellu, kde se escapované uvozovky rozbily a chybu
spolkl `|| echo`. Skript, který tiše hlásí „služba neodpovídá", místo aby
ukázal stav, je horší než žádný.
"""
import json
import sys

d = json.load(sys.stdin)

for k in ("documents", "chunks", "chunks_without_embedding",
          "documents_unfinished", "hnsw_index_present"):
    print("  %-26s %s" % (k, d[k]))
print("  %-26s %s" % ("documents_by_lang", d.get("documents_by_lang")))
print("  %-26s %s" % ("chunks_by_ts_config", d.get("chunks_by_ts_config")))

r = d.get("indexer", {})
print("  %-26s %s" % ("indexer.running", r.get("running")))

lr = r.get("last_result")
if lr:
    keys = ("new", "changed", "unchanged", "resumed",
            "chunks_embedded", "chunks_recycled", "seconds")
    summary = {k: lr[k] for k in keys if k in lr}
    print("  poslední běh:   " + json.dumps(summary, ensure_ascii=False))
    if lr.get("languages"):
        print("  jazyky:         %s" % lr["languages"])
    if lr.get("lang_sources"):
        print("  zdroje jazyka:  %s" % lr["lang_sources"])
    if lr.get("errors"):
        print("  CHYBY (%d): %s" % (len(lr["errors"]), lr["errors"][:5]))
