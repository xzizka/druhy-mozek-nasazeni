"""Markdown -> chunky s heading_path.

Schéma má sloupec `chunk.heading_path`, takže chunk má nést cestu nadpisů,
ve kterých leží. Reranker i odpověď z toho těží — „Fáze 1 > Rozpočet paměti"
je pro model i pro člověka mnohem víc informace než anonymní odstavec.

Frontmatter se odděluje PŘED chunkováním (`split_frontmatter`). Bez toho by
z YAML bloku vznikl obyčejný odstavec a `lang: en` by skončilo v embeddingu
i v `content_tsv` jako obsah dokumentu.

Strategie:
  1. rozděl podle nadpisů ATX (#, ##, ...) a drž zásobník cesty
  2. v každé sekci skládej odstavce do chunků do CHUNK_CHARS
  3. odstavec nikdy nerozděluj, pokud se sám nevejde — pak ho rozsekej
     na hranicích řádků a v krajním případě natvrdo

Bez overlapu. Overlap zdvojnásobí počet chunků a tím i cenu embeddingu,
která je při 1,7-2,1 s na chunk to jediné, co u indexace opravdu stojí.
Pro poznámky, kde nadpisy nesou kontext, je to špatný obchod.
"""
from __future__ import annotations

import re

from . import config

ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")

# Frontmatter musí začínat na prvním řádku souboru — `---` uprostřed textu je
# vodorovná čára, ne metadata. Non-greedy tělo končí prvním samostatným `---`.
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """YAML frontmatter -> (metadata, zbytek textu). Bez frontmatteru ({}, text).

    Záměrně NENÍ plný YAML parser: jediné, co z metadat potřebujeme, je skalární
    `lang:`, a PyYAML by byl závislost navíc do kontejneru s MemoryMax=700M.
    Vnořené mapy, seznamy a víceřádkové hodnoty se proto přeskakují — ne že by
    byly chyba, jen nás nezajímají.
    """
    if text.startswith("﻿"):       # BOM; _scan dekóduje utf-8 bez jeho odstranění
        text = text[1:]
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text

    meta: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        line = line.rstrip()
        # Odsazené řádky patří vnořené struktuře, `- ` je položka seznamu.
        if not line or line[0] in " \t#" or line.lstrip().startswith("- "):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, text[m.end():]


class Chunk:
    __slots__ = ("ordinal", "content", "heading_path")

    def __init__(self, ordinal: int, content: str, heading_path: str | None):
        self.ordinal = ordinal
        self.content = content
        self.heading_path = heading_path

    def __repr__(self) -> str:
        return f"Chunk({self.ordinal}, {self.heading_path!r}, {len(self.content)} znaku)"


def _split_long(text: str, budget: int) -> list[str]:
    """Odstavec, který se sám nevejde, rozsekej po řádcích, pak natvrdo."""
    if len(text) <= budget:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > budget:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    # Pokud i jednotlivý řádek přetéká (dlouhá tabulka, minifikovaný blok),
    # zbývá jen tvrdé krájení.
    final = []
    for part in out:
        while len(part) > budget:
            final.append(part[:budget])
            part = part[budget:]
        if part:
            final.append(part)
    return final


def _sections(text: str):
    """Generuje (heading_path, telo_sekce). Respektuje code fence."""
    stack: list[str] = []
    buf: list[str] = []
    in_fence = False
    path = None

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        return body

    for line in text.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else ATX.match(line)
        if m:
            body = flush()
            if body:
                yield path, body
            level = len(m.group(1))
            title = m.group(2).strip()
            del stack[level - 1:]
            stack.append(title)
            path = " > ".join(stack)
            continue
        buf.append(line)

    body = flush()
    if body:
        yield path, body


def chunk_markdown(text: str) -> list[Chunk]:
    budget = config.CHUNK_CHARS
    chunks: list[Chunk] = []
    ordinal = 0

    for path, body in _sections(text):
        # Odstavce = bloky oddělené prázdným řádkem.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        cur = ""
        for para in paragraphs:
            for piece in _split_long(para, budget):
                if cur and len(cur) + 2 + len(piece) > budget:
                    chunks.append(Chunk(ordinal, cur, path))
                    ordinal += 1
                    cur = piece
                else:
                    cur = f"{cur}\n\n{piece}" if cur else piece
        if cur:
            chunks.append(Chunk(ordinal, cur, path))
            ordinal += 1

    # Velmi krátké chunky (osamocený nadpis, jednořádková poznámka) nesou málo
    # signálu, ale embedding za ně zaplatíme stejně. Přilep je k předchozímu,
    # pokud se tam vejdou.
    merged: list[Chunk] = []
    for ch in chunks:
        if (merged and len(ch.content) < config.CHUNK_MIN_CHARS
                and merged[-1].heading_path == ch.heading_path
                and len(merged[-1].content) + 2 + len(ch.content) <= budget):
            merged[-1].content += "\n\n" + ch.content
            continue
        ch.ordinal = len(merged)
        merged.append(ch)
    return merged
