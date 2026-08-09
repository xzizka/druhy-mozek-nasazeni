"""Nahraný dokument -> text do indexu, originál na S3.

Etapa 1 umí `.md`, `.markdown`, `.txt`. PDF a DOCX přijdou v dalších etapách;
dokud nejsou, musí je aplikace odmítnout srozumitelně, ne mlčky.

Text jde do `MARKDOWN_ROOT/_uploads/` jako obyčejný markdown, takže se o něj
postará celá existující pipeline (chunkování, embeddingy, detekce jazyka,
inkrementální reindex podle hashe) bez jediné změny. `_uploads/` je
v `.gitignore` repozitáře poznámek — stejně jako `_scale/` — takže se text
nesynchronizuje na GitHub.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
from pathlib import Path

from . import config, core, db, storage

log = logging.getLogger("kryton")

PODPOROVANE = {".md", ".markdown", ".txt", ".text"}
# Formáty, které přijdou později. Rozlišeno schválně: „zatím neumím" je jiná
# informace než „tenhle formát sem nepatří".
CHYSTANE = {".pdf": "PDF", ".docx": "Word (DOCX)", ".doc": "Word (DOC)"}

# Pořadí není libovolné. UTF-8 je striktní, takže když projde, je to ono.
# cp1250 pak dekóduje skoro cokoliv bez chyby, proto až po něm — a je to
# nejčastější kódování českých textů z Windows.
KODOVANI = ("utf-8", "cp1250", "iso-8859-2")


class IngestError(RuntimeError):
    pass


def dekoduj(data: bytes) -> tuple[str, str]:
    """Text a použité kódování. Špatný odhad se do indexu propíše tiše,
    proto se kódování hlásí uživateli i ukládá do databáze."""
    for enc in KODOVANI:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/náhrada"


def extrahuj(data: bytes, filename: str) -> tuple[str, str]:
    ext = Path(filename).suffix.lower()
    if ext in CHYSTANE:
        raise IngestError(
            "%s zatím neumím — v téhle etapě jsou podporované jen "
            "markdown a prostý text." % CHYSTANE[ext])
    if ext not in PODPOROVANE:
        raise IngestError(
            "přípona %r není podporovaná; ber %s"
            % (ext or "(žádná)", ", ".join(sorted(PODPOROVANE))))
    text, enc = dekoduj(data)
    if not text.strip():
        raise IngestError("soubor neobsahuje žádný text")
    return text, enc


def _bezpecny_nazev(filename: str) -> str:
    """Název od uživatele je vstup jako každý jiný — projde slugem."""
    zaklad = os.path.basename(filename or "")
    zaklad = re.sub(r"\.[A-Za-z0-9]{1,10}$", "", zaklad)   # pryč přípona
    return core.slug(zaklad) or "dokument"


def _telo_markdownu(titulek: str, text: str) -> str:
    """Markdown s frontmatterem.

    `trust:` čte indexer retrievalu (`_resolve_trust`) — nahrané dokumenty
    dostávají 1 = „importované", takže je hledání umí odlišit od vlastních
    poznámek přes `max_trust`. `lang:` schválně NENÍ: detekci necháváme
    na retrievalu, jak bylo rozhodnuto u P1.
    """
    bezpecny_titulek = " ".join((titulek or "dokument").split())[:200]
    return ("---\ntitle: %s\ntrust: %d\n---\n\n# %s\n\n%s\n"
            % (bezpecny_titulek, config.UPLOAD_TRUST, bezpecny_titulek,
               text.strip()))


def uloz(data: bytes, filename: str) -> dict:
    """Celý tok: kontrola -> extrakce -> markdown na disk -> originál na S3.

    Pořadí je vědomé. Text na disk jde AŽ po úspěšném uploadu originálu:
    kdyby S3 selhalo, zůstal by v indexu dokument bez originálu, což je
    horší než nahrání, které se celé nepovedlo a jde zopakovat.
    """
    if not data:
        raise IngestError("prázdný soubor")
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise IngestError("soubor má %.1f MB, strop je %.0f MB"
                          % (len(data) / 1e6, config.UPLOAD_MAX_BYTES / 1e6))
    if not storage.enabled():
        raise IngestError("úložiště S3 není nakonfigurované")

    text, enc = extrahuj(data, filename)
    sha = hashlib.sha256(data).hexdigest()
    ext = Path(filename).suffix.lower()
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    key = storage.object_key(sha, ext)
    storage.put_original(data, key, filename, mime)

    titulek = " ".join(Path(filename).stem.split())[:200] or "dokument"
    # Hash v názvu drží soubory unikátní a zároveň dělá z opakovaného
    # nahrání téhož obsahu tentýž soubor, ne druhý dokument.
    rel = "%s/%s-%s.md" % (config.UPLOAD_DIR, _bezpecny_nazev(filename), sha[:8])
    cesta = core.safe_path(rel)
    cesta.parent.mkdir(parents=True, exist_ok=True)
    cesta.write_text(_telo_markdownu(titulek, text), encoding="utf-8")

    db.add_upload(source_path=rel, original_name=os.path.basename(filename),
                  mime=mime, size_bytes=len(data), sha256=sha,
                  s3_profile=config.S3_PROFILE, s3_bucket=config.S3_BUCKET,
                  s3_key=key, encoding=enc)
    core.trigger_reindex()
    log.info("nahrano %s -> %s (%s, %d B, %s)", filename, rel, key, len(data), enc)
    return {"source_path": rel, "s3_key": key, "sha256": sha,
            "size": len(data), "encoding": enc, "title": titulek}
