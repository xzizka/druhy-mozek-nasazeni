#!/usr/bin/env python3
"""
Evaluace rerankeru na češtině — v ZIPu nebyla, přidáno při nasazení.

Sedm českých dotazů nad osmi dokumenty, ke každému dotazu existuje právě jeden
správný dokument. Měří top-1, top-3, MRR a medianovou latenci.

Výsledky, kvůli kterým byl reranker z návrhu vyměněn:
    bge-reranker-base  : top-1 4/7 (57 %),  MRR 0,690, 20 kandidatu 1,19 s
    bge-reranker-v2-m3 : top-1 7/7 (100 %), MRR 1,000, 20 kandidatu 3,77 s
Base navic ve trech ze sedmi dotazu vratil na prvnim miste tentyz dokument
bez ohledu na otazku.

Pouziti (uvnitr kontejneru):
    python3 06-eval-reranker.py 127.0.0.1 bge-reranker-v2-m3
"""
import json, sys, time, urllib.request
ip, model = sys.argv[1], sys.argv[2]

DOCS = [
 "Slovník hunspell musí být v konfiguraci před unaccent, jinak se ztratí lemmatizace a dotaz latenci nenajde dokument s latence.",
 "PostgreSQL nedodává snowball stemmer pro češtinu, chybí mezi vestavěnými jazyky, proto je nutná vlastní slovníková konfigurace.",
 "Reciprocal Rank Fusion pracuje jen s pořadím, takže je nezávislá na škále skóre. Kosinová distance a ts_rank_cd nejsou srovnatelné.",
 "V LXC je page cache hostitelská a sjednocená, zatímco ve VM cachujete dvakrát. Výkon HNSW scanů je právě page cache.",
 "Build HNSW indexu se nevešel do maintenance_work_mem a spadl do spill-to-disk, takže běžel několikanásobně déle.",
 "halfvec ukládá dvě dvojice bajtů na dimenzi místo čtyř, index se zmenší na polovinu a dopad na recall je zanedbatelný.",
 "Markdown v /srv/brain je autoritativní zdroj, Postgres je jen derivovaný index, který musí být kdykoliv znovu postavitelný.",
 "Recept na svíčkovou na smetaně: kořenová zelenina, brusinky, houskový knedlík a citron.",
]
# (dotaz, index spravne odpovedi)
QUERIES = [
 ("Proč musí být hunspell v konfiguraci dřív než unaccent?", 0),
 ("Které jazyky Postgres pro stemming nepodporuje?", 1),
 ("Proč se skóre z různých větví nedá sčítat?", 2),
 ("Jakou výhodu má sdílená page cache proti virtuálnímu stroji?", 3),
 ("Co se stane, když se stavba indexu nevejde do paměti?", 4),
 ("Jak se dá zmenšit velikost vektorového indexu na polovinu?", 5),
 ("Co je zdroj pravdy a co jen derivovaná data?", 6),
]

def rerank(q):
    req = urllib.request.Request(f"http://{ip}:7997/rerank",
        data=json.dumps({"model":model,"query":q,"documents":DOCS}).encode("utf-8"),
        headers={"Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req, timeout=600) as r: d=json.load(r)
    return sorted(d["results"], key=lambda r:-r["relevance_score"]), time.time()-t0

print("MODEL: %s   (%d dotazu, %d dokumentu)" % (model, len(QUERIES), len(DOCS)))
print()
top1 = top3 = 0; mrr = 0.0; lat = []
for q, want in QUERIES:
    res, t = rerank(q); lat.append(t)
    order = [r["index"] for r in res]
    pos = order.index(want) + 1
    if pos == 1: top1 += 1
    if pos <= 3: top3 += 1
    mrr += 1.0/pos
    print("  pozice %d  %-52s (spravne=[%d], 1.=[%d])" % (pos, q[:52], want, order[0]))
n = len(QUERIES)
print()
print("  top-1 presnost : %d/%d = %.0f %%" % (top1, n, 100*top1/n))
print("  top-3 presnost : %d/%d = %.0f %%" % (top3, n, 100*top3/n))
print("  MRR            : %.3f" % (mrr/n))
print("  medianova latence pro 8 kandidatu: %.2f s" % sorted(lat)[len(lat)//2])
