#!/usr/bin/env python3
"""Kontrola čtení JSONu z odpovědi modelu (`rewrite._json_object`).

Vzniklo po tiché chybě z 2026-08-09: model vrátil

    {"lang":"cs","keywords":"první věta kniha Paradise Lost"}}

tedy o závorku navíc. Hladový regex `\\{.*\\}` chytil i tu přebývající,
`json.loads` spadl, funkce vrátila None a zafungoval fallback „ber celou
odpověď jako klíčová slova". Do lexikální a fuzzy větve tak šel celý syrový
výstup včetně JSON syntaxe. Nic nespadlo, jen se hůř hledalo.

Testují se čisté funkce, takže to nepotřebuje databázi ani LiteLLM —
`config.py` si ale `DATABASE_URL` vyžádá už při importu, takže mimo
kontejner mu ji musíš podstrčit:

    podman exec -i retrieval python - < scripts/15-rewrite-json-check.py

    docker run --rm -i -e PYTHONPATH=/srv/rs -e DATABASE_URL=postgresql://stub/stub \\
        -v "$PWD/retrieval-service:/srv/rs:ro" -w /srv/rs python:3.13-slim \\
        sh -c 'pip install -q -r requirements.txt; cat > /t.py; python /t.py' \\
        < scripts/15-rewrite-json-check.py
"""
import sys

from app import rewrite

FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK   " if ok else "CHYBA", name,
                         ("" if ok else " — " + str(detail))))
    if not ok:
        FAIL.append(name)


def kw(text):
    return rewrite._json_field(text, "keywords")


print("== běžné tvary ==")
check("čistý JSON", kw('{"lang":"cs","keywords":"latence index"}') == "latence index")
check("code fence",
      kw('```json\n{"lang":"cs","keywords":"latence index"}\n```') == "latence index")
check("věta okolo",
      kw('Tady je výsledek: {"lang":"en","keywords":"query planner"} Snad pomůže.')
      == "query planner")
check("víc řádků",
      kw('{\n  "keywords": "a b",\n  "lang": "cs"\n}') == "a b")

print("== regrese: přebývající závorka (skutečná chyba z 2026-08-09) ==")
ROZBITY = '{"lang":"cs","keywords":"první věta kniha Paradise Lost"}}'
check("závorka navíc nerozbije čtení",
      kw(ROZBITY) == "první věta kniha Paradise Lost", repr(kw(ROZBITY)))
check("i jazyk se přečte", rewrite._json_field(ROZBITY, "lang") == "cs")
check("do klíčových slov se nedostane JSON syntaxe",
      "{" not in (kw(ROZBITY) or "") and "lang" not in (kw(ROZBITY) or ""))

print("== záludnosti, na které regex nestačí ==")
check("závorka uvnitř řetězce",
      kw('{"keywords":"foo } bar","lang":"cs"}') == "foo } bar",
      repr(kw('{"keywords":"foo } bar","lang":"cs"}')))
check("vnořený objekt",
      kw('{"meta":{"x":1},"keywords":"a b","lang":"cs"}') == "a b")
check("dva objekty za sebou, bere se první",
      kw('{"keywords":"prvni","lang":"cs"} {"keywords":"druhy"}') == "prvni")
check("uvozovka uvnitř hodnoty",
      kw('{"keywords":"rekl \\"ano\\" dnes","lang":"cs"}') == 'rekl "ano" dnes',
      repr(kw('{"keywords":"rekl \\"ano\\" dnes","lang":"cs"}')))
check("text se závorkou před JSONem",
      kw('funkce f() { return 1 } a odpověď: {"keywords":"a b","lang":"cs"}')
      == "a b",
      repr(kw('funkce f() { return 1 } a odpověď: {"keywords":"a b","lang":"cs"}')))

print("== co má vrátit None ==")
check("žádný JSON", kw("tady žádný JSON není") is None)
check("prázdný vstup", kw("") is None)
check("pole místo objektu", kw('[1, 2, 3]') is None)
check("chybějící klíč", kw('{"lang":"cs"}') is None)
check("vnořená struktura místo skaláru",
      rewrite._json_field('{"keywords":{"a":1},"lang":"cs"}', "keywords") is None)
check("rozbitý JSON bez záchrany", kw('{"keywords": ') is None)

print()
if FAIL:
    print("SELHALO %d: %s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("VŠE PROŠLO")
