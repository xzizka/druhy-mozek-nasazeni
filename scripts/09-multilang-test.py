#!/usr/bin/env python3
"""Ověření vícejazyčnosti retrieval-service. Spouštěj uvnitř kontejneru brain."""
import json
import urllib.error
import urllib.request

URL = "http://10.89.7.13:8080/search"


def search(**body):
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:300]}


def show(label, r):
    print("\n=== " + label)
    if "_http" in r:
        print("    HTTP {}: {}".format(r["_http"], r["_body"][:160]))
        return r
    print("    lang={!r} ({})  ts_config={!r}".format(
        r["lang"], r["lang_source"], r["ts_config"]))
    print("    keywords={!r} ({})".format(r["keywords_used"], r["keywords_source"]))
    for h in r["results"]:
        print("      {:16} dense={:4} lexical={:4} fuzzy={:4} rrf={:.5f}".format(
            h["source_path"], str(h["r_dense"]), str(h["r_lexical"]),
            str(h["r_fuzzy"]), h["rrf_score"]))
    return r


print("############ 1. dotaz v jazyce dokumentu -> lexikalni vetev zabere")
CASES = [
    ("cs", "01-cesky.md", "Jak se ladi latence vektorovych indexu?"),
    ("cs", "01-cesky.md", "Jak se ladí latence vektorových indexů?"),
    ("en", "02-english.md", "What makes the databases run faster?"),
    ("de", "03-deutsch.md", "Warum laufen die Datenbanken schneller?"),
    # Slova musí v dokumentu být: latina nestemuje, takže AND je nemilosrdné.
    # Původní "dicitur" v textu nebylo a lexikální větev správně mlčela.
    ("la", "04-latina.md", "Ubi manet amicorum memoria?"),
]
for expect_lang, expect_doc, q in CASES:
    r = show("[{}] {}".format(expect_lang, q), search(query=q, rerank=False))
    hit = next((h for h in r.get("results", []) if h["source_path"] == expect_doc), None)
    verdict = []
    verdict.append("lang OK" if r.get("lang") == expect_lang
                   else "lang CHYBA (ceka {})".format(expect_lang))
    if hit is None:
        verdict.append("dokument {} VUBEC NENALEZEN".format(expect_doc))
    else:
        verdict.append("lexical na {} {}".format(
            expect_doc, "OK" if hit["r_lexical"] is not None else "MLCI"))
        verdict.append("dense {}".format("OK" if hit["r_dense"] is not None else "MLCI"))
    print("    -> " + " | ".join(verdict))

print("\n\n############ 2. lexikalni vetev je jazykova (cizi dokumenty mlci)")
r = show("[en] dotaz, sledujeme r_lexical u NE-anglickych dokumentu",
         search(query="What makes the databases run faster?", rerank=False))
foreign = [h for h in r["results"] if h["source_path"] != "02-english.md"]
bad = [h["source_path"] for h in foreign if h["r_lexical"] is not None]
print("    -> {} (cizojazycne s lexikalnim zasahem: {})".format(
    "OK, cizi mlci" if not bad else "POZOR", bad or "zadne"))
print("    -> dense napric: {}".format(
    "OK" if all(h["r_dense"] is not None for h in r["results"]) else "CHYBA"))

print("\n\n############ 3. explicitni lang ma prednost")
show("[en dotaz + lang=de] ma vyhrat de",
     search(query="What makes the databases run faster?", lang="de", rerank=False))
show("[lang=cs-CZ] normalizace", search(query="vektorove indexy", lang="cs-CZ", rerank=False))
show("[lang=xx] ma vratit chybu", search(query="cokoliv", lang="xx", rerank=False))

print("\n\n############ 4. rewrite vypnuty -> lang_source=default")
show("[en dotaz, rewrite=false]",
     search(query="What makes the databases run faster?", rewrite=False, rerank=False))

print("\n\n############ 5. keywords od volajiciho -> na cheap se nechodi")
show("[keywords=databases, bez lang]",
     search(query="What makes the databases run faster?",
            keywords="databases", rerank=False))

print("\n\n############ 6. dotaz bez diakritiky dal chytá fuzzy vetev")
show("[cs bez diakritiky] vektorovych indexu",
     search(query="vektorovych indexu", rerank=False))

print("\n\n############ 7. rerank zapnuty (regrese)")
r = show("[cs] s rerankingem", search(query="Jak se ladí latence vektorových indexů?"))
print("    -> reranked={}".format(r.get("reranked")))
