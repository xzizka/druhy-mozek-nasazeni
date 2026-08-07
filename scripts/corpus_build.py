#!/usr/bin/env python3
"""Z reálných textů poskládá markdown dokumenty o minimálně 100 chuncích.

Počet chunků se NEODHADUJE — ověřuje se zavoláním `app.chunker.chunk_markdown()`,
tedy přesně toho kódu, který pak poběží při indexaci. Odhad podle délky by
selhal, protože chunker slepuje krátké kusy a láme podle nadpisů a odstavců.

Struktura dokumentu je záměrně členitá (H1 > H2 > H3), aby se prověřil
`heading_path` a aby vložení odstavce doprostřed posunulo `ordinal` —
což je přesně případ, kvůli kterému recyklace klíčuje podle obsahu, ne
podle pořadí.

Část dokumentů se generuje BEZ frontmatteru, aby se detekce jazyka
neověřovala na jediném vzorku.
"""
from __future__ import annotations

import os
import random
import re
import sys

sys.path.insert(0, "/root/deploy/retrieval-service")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
from app import chunker                                    # noqa: E402

RAW = "/root/corpus/raw"
OUT = "/srv/brain/markdown/_scale"
MIN_CHUNKS = 100
NO_FRONTMATTER_EVERY = 10        # každý desátý dokument bez `lang:`

# Kolik dokumentů na jazyk. Čeština dostává nejvíc, protože je to hlavní
# jazyk korpusu; latina nejmíň, protože jí je reálně k dispozici nejmíň.
PLAN = {"cs": 400, "en": 300, "de": 200, "la": 100}

TITLES = {
    "cs": "Poznámky", "en": "Notes", "de": "Notizen", "la": "Commentarii",
}
SECTION = {
    "cs": "Oddíl", "en": "Section", "de": "Abschnitt", "la": "Pars",
}
SUB = {
    "cs": "Podkapitola", "en": "Subsection", "de": "Unterabschnitt", "la": "Caput",
}


def paragraphs(lang: str):
    """Generátor odstavců ze všech surových textů daného jazyka."""
    d = os.path.join(RAW, lang)
    files = sorted(os.listdir(d))
    for name in files:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            text = f.read()
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            # Kratičké řádky jsou v knihách zbytky sazby (čísla stran, hvězdičky).
            if len(para) >= 120:
                yield para


def build_body(src, lang: str, rnd: random.Random) -> str | None:
    """Skládá odstavce a nadpisy, dokud skutečný chunker nedá >= MIN_CHUNKS."""
    parts: list[str] = []
    n_para = 0
    chars = 0
    section = 0
    while True:
        try:
            para = next(src)
        except StopIteration:
            return None
        if n_para % 8 == 0:
            section += 1
            parts.append(f"\n## {SECTION[lang]} {section}\n")
            if section % 3 == 0:
                parts.append(f"\n### {SUB[lang]} {section}.1\n")
        parts.append(para)
        n_para += 1
        chars += len(para)
        # Ověřovat po každém odstavci je drahé, ale po dvaceti zase hrubé:
        # německé knihy mají dlouhé odstavce a jedna dvacetiodstavcová dávka
        # dokument přestřelila ze 100 rovnou na ~140 chunků. Každý německý
        # dokument pak spotřeboval o třetinu víc suroviny a z 24 MB jich
        # vyšlo 176 místo 200.
        #
        # Řešení má dvě části: nekontrolovat vůbec, dokud nemůže být hotovo
        # (levná zarážka podle počtu znaků), a pak kontrolovat po pěti.
        if chars >= MIN_CHUNKS * 1000 and n_para % 5 == 0:
            body = "\n\n".join(parts)
            if len(chunker.chunk_markdown(body)) >= MIN_CHUNKS:
                return body
        if n_para > 4000:                      # pojistka proti nekonečnu
            return "\n\n".join(parts)


def main() -> None:
    limit_per_lang = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rnd = random.Random(20260807)
    os.makedirs(OUT, exist_ok=True)

    total, stats = 0, {}
    for lang, count in PLAN.items():
        if limit_per_lang:
            count = min(count, limit_per_lang)
        src = paragraphs(lang)
        made, chunks_total = 0, 0
        for i in range(1, count + 1):
            path = os.path.join(OUT, f"{lang}-{i:04d}.md")
            if os.path.exists(path):
                made += 1
                continue
            body = build_body(src, lang, rnd)
            if body is None:
                print(f"[{lang}] doslo suroviny po {made} dokumentech", flush=True)
                break
            n = len(chunker.chunk_markdown(body))
            chunks_total += n
            title = f"{TITLES[lang]} {i}"
            head = f"# {title}\n\n"
            # Každý desátý bez frontmatteru — test detekce jazyka ve velkém.
            if i % NO_FRONTMATTER_EVERY == 0:
                doc = head + body + "\n"
            else:
                doc = f"---\nlang: {lang}\ntitle: {title}\n---\n\n" + head + body + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(doc)
            made += 1
            if made % 25 == 0:
                print(f"  [{lang}] {made}/{count} dokumentu, "
                      f"prumer {chunks_total/max(1,made):.0f} chunku", flush=True)
        stats[lang] = (made, chunks_total)
        total += made
        print(f"[{lang}] HOTOVO {made} dokumentu, ~{chunks_total} chunku", flush=True)

    print(f"\nCELKEM {total} dokumentu")
    for lang, (made, ch) in stats.items():
        print(f"  {lang}: {made} dokumentu, ~{ch} chunku, "
              f"prumer {ch/max(1,made):.0f}")
    grand = sum(c for _, c in stats.values())
    print(f"  odhad celkem ~{grand} chunku -> pri 1,2 s/chunk ~{grand*1.2/3600:.1f} h embeddingu")


if __name__ == "__main__":
    main()
