#!/usr/bin/env python3
"""Stáhne dlouhé články z Wikipedie jako čistý text. Doplněk ke Gutenbergu.

PROČ: Gutenberg má česky jen 11 knih, z toho 4 dost dlouhé (2 MB celkem) —
a čeština je přitom hlavní jazyk toho druhého mozku. Wikipedie je jediný
rozumně dostupný zdroj reálné dlouhé české prózy.

`list=allpages&apminsize=N` filtruje rovnou podle velikosti stránky, takže
se nestahují tisíce pahýlů. Plný extrakt umí API vrátit jen po jedné
stránce (`exlimit` platí jen s `exintro`), takže jeden článek = jeden
request; proto ta prodleva a User-Agent.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UA = "brain-corpus-builder/1.0 (osobni test retrieval sluzby; kontakt ozizka@legend.cz)"
OUT = "/root/corpus/raw"
DELAY = 0.4
MIN_CHARS = 20_000          # kratší článek nemá pro stochunkový dokument cenu


def api(lang: str, params: dict) -> dict:
    params = dict(params, format="json", formatversion="2")
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def long_titles(lang: str, minsize: int, want: int) -> list[str]:
    titles, cont = [], None
    while len(titles) < want:
        p = {"action": "query", "list": "allpages", "apminsize": minsize,
             "aplimit": "500", "apfilterredir": "nonredirects"}
        if cont:
            p["apcontinue"] = cont
        data = api(lang, p)
        batch = [x["title"] for x in data.get("query", {}).get("allpages", [])]
        if not batch:
            break
        titles.extend(batch)
        cont = data.get("continue", {}).get("apcontinue")
        if not cont:
            break
        time.sleep(DELAY)
    return titles[:want]


def extract(lang: str, title: str) -> str | None:
    data = api(lang, {"action": "query", "prop": "extracts", "explaintext": "1",
                      "redirects": "1", "titles": title})
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    return pages[0].get("extract")


def main() -> None:
    lang = sys.argv[1]
    target = int(sys.argv[2])
    minsize = int(sys.argv[3]) if len(sys.argv) > 3 else 60_000

    d = os.path.join(OUT, lang)
    os.makedirs(d, exist_ok=True)
    have = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
    if have >= target:
        print(f"[wiki {lang}] uz mam {have/1e6:.1f} MB, preskakuji", flush=True)
        return

    print(f"[wiki {lang}] hledam clanky >= {minsize} B, cil {target/1e6:.0f} MB, "
          f"mam {have/1e6:.1f} MB", flush=True)
    titles = long_titles(lang, minsize, 20_000)
    print(f"[wiki {lang}] {len(titles)} kandidatu", flush=True)

    got = 0
    for title in titles:
        if have >= target:
            break
        name = "".join(c if c.isalnum() else "_" for c in title)[:80]
        path = os.path.join(d, f"wiki_{name}.txt")
        if os.path.exists(path):
            continue
        try:
            text = extract(lang, title)
        except Exception as e:
            print(f"  preskakuji {title}: {e}", flush=True)
            time.sleep(2)
            continue
        time.sleep(DELAY)
        if not text or len(text) < MIN_CHARS:
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        have += len(text.encode("utf-8"))
        got += 1
        if got % 25 == 0:
            print(f"  [wiki {lang}] {got} clanku, {have/1e6:.1f} MB", flush=True)
    print(f"[wiki {lang}] HOTOVO: {got} clanku, celkem {have/1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
