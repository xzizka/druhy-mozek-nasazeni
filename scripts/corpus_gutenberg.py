#!/usr/bin/env python3
"""Stáhne z Gutenbergu dlouhé knihy podle jazyka. Ukládá do /root/corpus/raw/<lang>/.

Zdvořilost: sekvenčně, s prodlevou, s User-Agentem. Gutenberg hromadné
stahování z hlavního webu nemá rád, takže když začne odmítat, skript to řekne
a skončí, místo aby do toho bušil dál.
"""
import csv
import os
import re
import sys
import time
import urllib.error
import urllib.request

CATALOG = "/root/corpus/pg_catalog.csv"
OUT = "/root/corpus/raw"
UA = "brain-corpus-builder/1.0 (osobni test retrieval sluzby; kontakt ozizka@legend.cz)"
DELAY = 1.0

# Kolik bajtů čistého textu chci na jazyk. Přepsatelné kvůli zkušebnímu běhu:
#   TARGETS="la=900000,cs=900000" python3 fetch-gutenberg.py
TARGETS = {"en": 38_000_000, "de": 26_000_000, "la": 13_000_000, "cs": 99_000_000}
if os.environ.get("TARGETS"):
    TARGETS = {k: int(v) for k, v in
               (p.split("=") for p in os.environ["TARGETS"].split(","))}

# Hlavička a patička Gutenbergu nejsou dílo — jsou to licenční bloky, které by
# se v korpusu opakovaly ve stovkách dokumentů a zkreslily lexikální větev.
START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)


# Katalog Gutenbergu jazyku NEVĚŘÍ dost. Ověřeno: kniha vedená jako `la`
# obsahovala francouzský text ("On nommait succenteur celui qui dans les
# collégiales chantait..."). Kdyby to prošlo, dokumenty by se označily jako
# latina, lexikální větev by hledala nesmysl a měření přesnosti detekce by
# mělo špatnou ground truth.
#
# Rozlišuje se překryvem s funkčními slovy. Latina a románské jazyky sdílí
# spoustu krátkých slov (`de`, `et`, `non`, `si`, `in`), takže samotné
# latinské markery nestačí — proto jsou tu i jazyky, které chci ZAHODIT.
MARKERS = {
    "cs": {"a", "je", "se", "na", "že", "to", "v", "s", "do", "ale", "jsem", "byl", "který"},
    "en": {"the", "and", "of", "to", "in", "that", "was", "he", "it", "with", "for", "is"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "zu", "sich", "mit", "den", "auf"},
    "la": {"et", "in", "est", "non", "cum", "ad", "quod", "qui", "sed", "atque", "enim",
           "autem", "esse", "sunt", "ut", "ex"},
    # Jen k zahození, nikdy se nevrací jako přijatý jazyk.
    "_fr": {"les", "des", "une", "dans", "pour", "avec", "cette", "nous", "vous",
            "qui", "que", "plus", "être", "sur", "elle"},
    "_es": {"los", "las", "una", "por", "para", "con", "como", "pero", "más", "está"},
    "_it": {"gli", "delle", "nella", "sono", "anche", "come", "perché", "questo"},
    "_nl": {"het", "een", "van", "zijn", "niet", "dat", "maar", "voor", "worden"},
}


def looks_like(text: str, lang: str) -> bool:
    """Odpovídá text deklarovanému jazyku? Rozhoduje překryv funkčních slov."""
    words = re.findall(r"[a-zà-öø-ÿāēīōū]+", text[:200_000].lower())
    if len(words) < 500:
        return False
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    scores = {k: sum(freq.get(m, 0) for m in ms) for k, ms in MARKERS.items()}
    best = max(scores, key=lambda k: scores[k])
    # Musí vyhrát deklarovaný jazyk, a to VÝRAZNĚ. Práh 1,3 nestačil:
    # `la/15582.txt` je latinský text s rozsáhlým francouzským poznámkovým
    # aparátem (la=1666, fr=789) a prolezl, přestože skoro třetina objemu je
    # francouzsky. Takový dokument dostane jeden ts_config na celý soubor,
    # takže cizojazyčné pasáže skončí ve špatné fulltextové konfiguraci —
    # přesně ten šum, kvůli kterému 03-multilang.sql vznikla.
    #
    # Radši menší čistý korpus než větší smíchaný: build skript si s nedostatkem
    # suroviny poradí (vyrobí míň dokumentů), se znečištěnou by si neporadil.
    return best == lang and scores[lang] > 2.5 * max(
        (v for k, v in scores.items() if k != lang), default=0)


def strip_boilerplate(text: str) -> str:
    m = START.search(text)
    if m:
        text = text[m.end():]
    m = END.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def candidates(lang: str) -> list[str]:
    ids = []
    with open(CATALOG, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["Type"] == "Text" and row["Language"] == lang:
                ids.append(row["Text#"])
    return ids


def fetch(book_id: str) -> bytes | None:
    for url in (f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt",
                f"https://www.gutenberg.org/files/{book_id}/{book_id}-0.txt"):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                print(f"  ODMITNUTO {e.code} — Gutenberg blokuje, koncim", flush=True)
                sys.exit(3)
            continue
        except Exception:
            continue
    return None


def main() -> None:
    for lang, target in TARGETS.items():
        d = os.path.join(OUT, lang)
        os.makedirs(d, exist_ok=True)
        have = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
        if have >= target:
            print(f"[{lang}] uz mam {have/1e6:.1f} MB, preskakuji", flush=True)
            continue
        ids = candidates(lang)
        print(f"[{lang}] {len(ids)} knih v katalogu, cil {target/1e6:.0f} MB, "
              f"mam {have/1e6:.1f} MB", flush=True)
        got = 0
        for book_id in ids:
            if have >= target:
                break
            path = os.path.join(d, f"{book_id}.txt")
            if os.path.exists(path):
                continue
            raw = fetch(book_id)
            time.sleep(DELAY)
            if not raw:
                continue
            try:
                text = strip_boilerplate(raw.decode("utf-8"))
            except UnicodeDecodeError:
                try:
                    text = strip_boilerplate(raw.decode("latin-1"))
                except Exception:
                    continue
            # Krátké texty nemají cenu: chci dokumenty o 100+ chuncích.
            if len(text) < 120_000:
                continue
            if not looks_like(text, lang):
                print(f"  ZAHOZENO {book_id}: obsah neodpovídá jazyku {lang}", flush=True)
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            have += len(text.encode("utf-8"))
            got += 1
            if got % 10 == 0:
                print(f"  [{lang}] {got} knih, {have/1e6:.1f} MB", flush=True)
        print(f"[{lang}] HOTOVO: {got} knih, celkem {have/1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
