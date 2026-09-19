"""Nahraný dokument -> text do indexu, originál na S3.

Umí `.md`, `.markdown`, `.txt`, `.pdf` a `.docx` (etapy 1–3). `.doc` (starší
binární Word, ne ZIP+XML jako .docx) v plánu není — jiný parser, mimo rozsah.

Text jde do `MARKDOWN_ROOT/_uploads/` jako obyčejný markdown, takže se o něj
postará celá existující pipeline (chunkování, embeddingy, detekce jazyka,
inkrementální reindex podle hashe) bez jediné změny. `_uploads/` je
v `.gitignore` repozitáře poznámek — stejně jako `_scale/` — takže se text
nesynchronizuje na GitHub.
"""
from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from . import config, core, db, storage

log = logging.getLogger("kryton")

PODPOROVANE = {".md", ".markdown", ".txt", ".text", ".pdf", ".docx"}
# Rozlišeno schválně od obecně nepodporovaných přípon: „tenhle formát sem
# nepatří" je jiná informace než „přípona neznámá". .doc je starší binární
# (OLE) formát, ne ZIP+XML jako .docx — chtělo by to jiný parser a není
# naplánovaný.
MIMO_ROZSAH = {".doc": "starší binární formát Wordu (.doc); podporujeme jen .docx"}

# Pořadí není libovolné. UTF-8 je striktní, takže když projde, je to ono.
# cp1250 pak dekóduje skoro cokoliv bez chyby, proto až po něm — a je to
# nejčastější kódování českých textů z Windows.
KODOVANI = ("utf-8", "cp1250", "iso-8859-2")

_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Nezávislé na UPLOAD_MAX_BYTES (ten hlídá velikost NAHRANÉHO souboru) —
# tohle hlídá, na kolik se rozbalí. Kontrolováno při čtení po blocích, ne
# podle metadat centrální adresáře zipu, protože ta jde podvrhnout.
_DOCX_MAX_UNCOMPRESSED = 200 * 1024 * 1024


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


def _precti_omezene(soubor, limit: int) -> bytes:
    """Čte ze zipu po blocích a přeruší dřív, než velikost přeroste limit —
    funguje i proti podvrženým metadatům v centrální adresáři, protože se
    nespoléhá na deklarovanou velikost, jen na to, co skutečně přiteče."""
    data = bytearray()
    while True:
        blok = soubor.read(1024 * 1024)
        if not blok:
            break
        data += blok
        if len(data) > limit:
            raise IngestError(
                "DOCX se rozbaluje na víc než %.0f MB, to je podezřele moc"
                % (limit / 1e6))
    return bytes(data)


def _extrahuj_docx(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IngestError("soubor není platný DOCX (poškozený, nebo to není ZIP)")

    try:
        with zf.open("word/document.xml") as f:
            xml_data = _precti_omezene(f, _DOCX_MAX_UNCOMPRESSED)
    except KeyError:
        raise IngestError("soubor není platný DOCX (chybí word/document.xml)")

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as ex:
        raise IngestError("DOCX obsahuje nečitelné XML (%s)" % ex)

    odstavce = []
    for p in root.iter(_DOCX_NS + "p"):
        text = "".join(t.text or "" for t in p.iter(_DOCX_NS + "t"))
        if text:
            odstavce.append(text)
    return "\n\n".join(odstavce)


def _extrahuj_pdf(data: bytes) -> str:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as ex:
        raise IngestError("soubor není platné PDF (%s)" % ex)

    if reader.is_encrypted:
        try:
            reader.decrypt("")  # některé PDF mají jen prázdné heslo
        except Exception:
            pass
    if reader.is_encrypted:
        raise IngestError("PDF je heslem chráněné, to nepodporujeme")

    pocet = len(reader.pages)
    if pocet > config.UPLOAD_MAX_PAGES:
        raise IngestError("PDF má %d stránek, strop je %d"
                          % (pocet, config.UPLOAD_MAX_PAGES))

    stranky = [p.extract_text() or "" for p in reader.pages]
    text = "\n\n".join(stranky)
    # Sken bez textové vrstvy vrátí skoro nic bez ohledu na počet stránek —
    # OCR je mimo zadání („jen textové dokumenty"), proto srozumitelná
    # hláška místo tichého "soubor neobsahuje žádný text" u jedné stránky.
    if pocet and len(text.strip()) < 10 * pocet:
        raise IngestError(
            "PDF vypadá jako sken bez textové vrstvy (%d znaků z %d stránek); "
            "OCR nepodporujeme, jen textové dokumenty" % (len(text.strip()), pocet))
    return text


def extrahuj(data: bytes, filename: str) -> tuple[str, str]:
    ext = Path(filename).suffix.lower()
    if ext in MIMO_ROZSAH:
        raise IngestError("%s — %s" % (ext, MIMO_ROZSAH[ext]))
    if ext not in PODPOROVANE:
        raise IngestError(
            "přípona %r není podporovaná; ber %s"
            % (ext or "(žádná)", ", ".join(sorted(PODPOROVANE))))

    if ext == ".pdf":
        text, enc = _extrahuj_pdf(data), "pdf"
    elif ext == ".docx":
        text, enc = _extrahuj_docx(data), "docx"
    else:
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
