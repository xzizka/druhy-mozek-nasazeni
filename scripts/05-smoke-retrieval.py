#!/usr/bin/env python3
"""
Smoke test celého retrieval jádra — v ZIPu nebyl, přidáno při nasazení.

Projde celou cestu: Infinity embeddings -> retrieval.chunk (halfvec 1024)
-> generované sloupce content_tsv / content_norm -> retrieval.hybrid_search
-> RRF fúze všech tří větví.

Ověřuje mimo jiné CENTRÁLNÍ TVRZENÍ návrhu: že index je lemmatizovaný
s diakritikou, takže dotaz bez diakritiky lexikální větev minout musí,
a že ho zachrání trigramová větev.

BEZPEČNÉ pro produkci: vkládá řádky s source_path prefixem "_smoke/"
a na konci maže jen je. Nikdy nesahá na ostatní dokumenty.

Spouštěj jako root uvnitř kontejneru:  python3 05-smoke-retrieval.py
"""
import json
import subprocess
import sys
import urllib.request
import uuid

INFINITY = "http://127.0.0.1:7997"
PREFIX = "_smoke/"

NOTES = [
    ("hnsw-pamet.md", "Ladil jsem latenci vektorových indexů. Build HNSW se nevešel "
                      "do maintenance_work_mem a spadl do spill-to-disk."),
    ("hunspell.md",   "Hunspell cs_CZ musí být v konfiguraci před unaccent. Kdyby unaccent "
                      "běžel první, dostal by hunspell resim místo řeším."),
    ("page-cache.md", "V LXC je page cache hostitelská a sjednocená. Výkon HNSW scanů "
                      "je právě page cache."),
    ("svickova.md",   "Recept na svíčkovou na smetaně s brusinkami. S databázemi nesouvisí."),
]

fails = []


def embed(texts):
    req = urllib.request.Request(
        INFINITY + "/embeddings",
        data=json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    return [i["embedding"] for i in sorted(d["data"], key=lambda x: x["index"])]


def psql(sql):
    p = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-X", "-U", "postgres",
         "-d", "retrieval", "-v", "ON_ERROR_STOP=1", "-tA", "-F", "|"],
        input=sql, capture_output=True, text=True)
    if p.returncode:
        print("SQL CHYBA:", p.stderr[:400])
        sys.exit(2)
    return p.stdout.strip()


def search(query, limit=3, only_smoke=True):
    """Vrátí řádky hybrid_search.

    `only_smoke` odfiltruje všechno, co nepatří k fixturám tohoto skriptu.

    PROČ: hlavička slibuje, že je skript bezpečný nad produkcí, ale kontroly
    níž se dívají na 1. místo výsledku a předpokládají, že tam bude fixtura.
    Nad reálným korpusem to neplatí — narazilo se na to při ověřování
    vícejazyčnosti, kde poznámka `01-cesky.md` o vektorových indexech
    přebila `_smoke/hnsw-pamet.md` a kontrola spadla, přestože obě tvrzení
    o větvích (lexikální mlčí, fuzzy zachraňuje) platila.
    Limit se zvedá, aby po odfiltrování cizích dokumentů něco zbylo.
    """
    v = embed([query])[0]
    lit = "[" + ",".join("%.6f" % x for x in v) + "]"
    q = query.replace("'", "''")
    fetch = limit * 8 if only_smoke else limit
    out = psql(f"""SELECT source_path, coalesce(r_dense::text,'-'),
                          coalesce(r_lexical::text,'-'), coalesce(r_fuzzy::text,'-')
                     FROM retrieval.hybrid_search('{lit}'::halfvec(1024), '{q}', {fetch});""")
    rows = [l.split("|") for l in out.splitlines() if l.count("|") == 3]
    if only_smoke:
        rows = [r for r in rows if r[0].startswith(PREFIX)]
    return rows[:limit]


def check(name, ok, detail=""):
    print("  %-58s %s" % (name, "OK" if ok else "SELHALO"))
    if detail:
        print("      " + detail)
    if not ok:
        fails.append(name)


print("== priprava: embedduju a vkladam %d testovacich poznamek ==" % len(NOTES))
vecs = embed([t for _, t in NOTES])
check("Infinity vraci 1024 dimenzi", len(vecs[0]) == 1024, "dimenze=%d" % len(vecs[0]))

psql(f"DELETE FROM retrieval.document WHERE source_path LIKE '{PREFIX}%';")
sql = []
for (path, text), v in zip(NOTES, vecs):
    did = str(uuid.uuid4())
    lit = "[" + ",".join("%.6f" % x for x in v) + "]"
    t = text.replace("'", "''")
    sql.append(f"INSERT INTO retrieval.document (id, source_path, title, content_hash, trust_level, chunk_count, indexed_at) "
               f"VALUES ('{did}', '{PREFIX}{path}', '{path}', sha256('{t}'::bytea), 0, 1, now());")
    sql.append(f"INSERT INTO retrieval.chunk (document_id, ordinal, content, embedding) "
               f"VALUES ('{did}', 0, '{t}', '{lit}'::halfvec(1024));")
psql("\n".join(sql))
psql("ANALYZE retrieval.chunk;")

n = psql(f"SELECT count(*) FROM retrieval.chunk c JOIN retrieval.document d ON d.id=c.document_id "
         f"WHERE d.source_path LIKE '{PREFIX}%' AND c.content_tsv IS NOT NULL AND c.content_norm IS NOT NULL;")
check("generovane sloupce content_tsv a content_norm", n == str(len(NOTES)), "%s z %d" % (n, len(NOTES)))

print()
print("== vetve hybrid_search ==")

rows = search("hunspell unaccent")
top = rows[0] if rows else ["?", "-", "-", "-"]
check("klicova slova rozsviti vsechny tri vetve",
      top[1] != "-" and top[2] != "-" and top[3] != "-",
      "1. misto %s (dense=%s lex=%s fuzzy=%s)" % (top[0], top[1], top[2], top[3]))

rows = search("maintenence_work_mem")   # zamerny preklep
top = rows[0] if rows else ["?", "-", "-", "-"]
check("preklep najde dokument diky fuzzy vetvi",
      top[0].endswith("hnsw-pamet.md") and top[3] != "-" and top[2] == "-",
      "1. misto %s (lex=%s fuzzy=%s)" % (top[0], top[2], top[3]))

rows = search("vektorových indexů")
top = rows[0] if rows else ["?", "-", "-", "-"]
check("s diakritikou lexikalni vetev zabere", top[2] != "-",
      "1. misto %s (lex=%s)" % (top[0], top[2]))

rows = search("vektorovych indexu")     # bez diakritiky
top = rows[0] if rows else ["?", "-", "-", "-"]
check("BEZ diakritiky lexikalni mlci, fuzzy zachrani vysledek",
      top[2] == "-" and top[3] != "-" and top[0].endswith("hnsw-pamet.md"),
      "1. misto %s (lex=%s fuzzy=%s) <- centralni tvrzeni navrhu" % (top[0], top[2], top[3]))

rows = search("Jak souvisí page cache s výkonem HNSW?")
top = rows[0] if rows else ["?", "-", "-", "-"]
check("dense vetev zvladne celou otazku", top[0].endswith("page-cache.md"),
      "1. misto %s" % top[0])

print()
print("== ceska lemmatizace ==")
tsv = psql("SELECT to_tsvector('czech','Ladím latenci vektorových indexů')::text;")
check("hunspell lemmatizuje", "'ladit'" in tsv and "'latence'" in tsv, tsv)

print()
print("== uklid ==")
psql(f"DELETE FROM retrieval.document WHERE source_path LIKE '{PREFIX}%';")
left = psql(f"SELECT count(*) FROM retrieval.document WHERE source_path LIKE '{PREFIX}%';")
check("testovaci data smazana", left == "0", "zbylo %s" % left)

print()
if fails:
    print("SELHALO %d kontrol: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("VSE OK")
