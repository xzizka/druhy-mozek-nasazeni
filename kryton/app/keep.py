"""Google Keep -> markdown v MARKDOWN_ROOT/keep/. Jednosměrně, jen čtení.

Keep nemá pro osobní účty žádné oficiální API. To na `keep.googleapis.com`
existuje, ale je jen pro Google Workspace přes domain-wide delegation
a otevření pro osobní účty Google odmítá od 2022. Zbývá `gkeepapi` —
neoficiální klient nad privátním API, který se autentizuje **master tokenem**
z gpsoauth. Ten token má plný přístup k celému účtu, ne jen ke Keepu; proto
jde přes podman secret a nikdy ne přes Environment.

**Do Keepu se nezapisuje nic.** Ani mazání, ani úprava, ani značky. Keep je
zdroj, markdown je kopie. Kdyby se sem někdy přidával zápis, musí to být
vědomé rozhodnutí, ne vedlejší efekt refaktoru.

Text jde do `MARKDOWN_ROOT/keep/` jako obyčejný markdown, takže se o něj
postará celá existující pipeline (chunkování, embeddingy, inkrementální
reindex podle hashe) bez jediné změny — stejný trik, jaký používá
`ingest.py` pro nahrané PDF a DOCX.

Rozdíl proti `_uploads/`: `keep/` se **verzuje v gitu**. Obsah Keepu nikde
jinde než v Google cloudu není, takže `brain-markdown-sync` z něj dělá
zálohu á 15 minut — a hlavně: poznámka, kterou týdenní úklid smaže, zůstane
dohledatelná v historii repozitáře.

Dva režimy běhu, protože mazání je nevratné a přidávání ne:

    sync()              hodinově — vytvoří a aktualizuje soubory, NEMAŽE
    sync(uklid=True)    týdně    — navíc smaže soubory bez protějšku v Keepu

Co se považuje za „není v Keepu": smazaná (v koši) i **archivovaná**
poznámka. Archiv se neindexuje vůbec (volba uživatele 2026-08-20), takže
zarchivování je z pohledu druhého mozku totéž co smazání.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger("kryton")

# Do jména souboru pouštíme jen alfanumeriku a pomlčku. Keep ID vypadá jako
# `1a2b3c4d5e6f.7g8h9i0j` — tečka by ve jménu fungovala, ale cesta, ve které
# je jediná tečka až před příponou, se lépe čte i skriptuje.
_NEALFANUM = re.compile(r"[^a-zA-Z0-9]+")


def enabled() -> bool:
    return bool(config.KEEP_EMAIL and config.KEEP_MASTER_TOKEN)


# ---------------------------------------------------------------------------
# Převod poznámky na markdown
#
# VŠECHNO ZDE MUSÍ BÝT DETERMINISTICKÉ. Detekce změn v indexeru je sha256
# celého souboru, takže cokoliv, co se mezi dvěma běhy nad NEZMĚNĚNOU
# poznámkou liší — čas syncu ve frontmatteru, pořadí štítků z množiny,
# pořadí klíčů — znamená nový hash, přeembeddování a commit do gitu.
# Při hodinovém běhu by to bylo 24 zbytečných commitů denně nad celým
# adresářem a embedding, který je jediná drahá operace celé pipeline.
# ---------------------------------------------------------------------------

def _cas(ts) -> str:
    """Timestamp z Keepu na ISO 8601 v UTC. Nikdy `now()` — viz komentář výše."""
    if ts is None:
        return ""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _polozky(note) -> list[tuple[str, bool]]:
    """Položky checklistu jako (text, zaškrtnuto).

    V gkeepapi 0.17.1 je `items` property (ověřeno 2026-08-20). Varianty
    se přesto zkoušejí: knihovna je neoficiální a její API se mezi verzemi
    hýbe, kdežto pád tady by znamenal, že se poznámka tiše nezapíše.

    `note.text` se u checklistu ZÁMĚRNĚ nepoužívá — je to serializace
    položek se znaky ☐/☑, které by skončily v embeddingu místo markdownu.
    """
    raw = getattr(note, "items", None)
    if raw is None:
        raw = getattr(note, "items_", None)
    if callable(raw):
        raw = raw()
    if not raw:
        return []
    out = []
    for it in raw:
        text = (getattr(it, "text", "") or "").strip()
        if text:
            out.append((text, bool(getattr(it, "checked", False))))
    return out


def _stitky(note) -> list[str]:
    """Jména štítků, seřazená. Pořadí z Keepu není zaručené — viz determinismus."""
    labels = getattr(note, "labels", None)
    if labels is None:
        return []
    if hasattr(labels, "all"):
        labels = labels.all()
    out = []
    for lab in labels or []:
        name = (getattr(lab, "name", None) or str(lab)).strip()
        if name:
            out.append(name)
    return sorted(set(out))


def _prilohy(note) -> int:
    for atribut in ("blobs", "images", "drawings"):
        val = getattr(note, atribut, None)
        if val:
            return len(val)
    return 0


def rel_path(note) -> str:
    """`keep/RRRR-MM-DD-<id>.md`.

    Jméno souboru drží **Keep ID, ne titulek**: `document.source_path` je
    UNIQUE klíč detekce změn, takže přejmenování souboru je pro pipeline
    smazání starého dokumentu a založení nového — tedy přeembeddování celé
    poznámky. Titulek se mění běžně, ID nikdy.

    Datum je v cestě kvůli temporálním dotazům (P7): frontmatter se před
    chunkováním odřezává, takže `vytvoreno:` se do indexu nedostane, ale
    `source_path` se do kontextu pro model posílá. Je to datum VYTVOŘENÍ,
    které se nemění — datum úpravy by soubor přejmenovalo při každé editaci.
    """
    ident = _NEALFANUM.sub("-", str(getattr(note, "id", "") or "")).strip("-")
    den = _cas(getattr(getattr(note, "timestamps", None), "created", None))[:10]
    return "%s/%s-%s.md" % (config.KEEP_DIR, den or "0000-00-00", ident)


def render(note) -> str | None:
    """Markdown jedné poznámky, nebo None když v ní není co indexovat."""
    titulek = (getattr(note, "title", "") or "").strip()
    text = (getattr(note, "text", "") or "").strip()
    polozky = _polozky(note)
    prilohy = _prilohy(note)

    # Checklist má text i položky; `text` u něj bývá jejich serializace,
    # takže by se obsah v souboru objevil dvakrát.
    if polozky:
        telo = "\n".join("- [%s] %s" % ("x" if ok else " ", t) for t, ok in polozky)
    else:
        telo = text

    if not telo and not titulek:
        # Poznámka, která je jen fotka nebo kresba. Prázdný dokument by
        # v indexu zabral místo a v odpovědích byl k ničemu.
        return None

    if not titulek:
        # Bez nadpisu by `_title()` v indexeru sáhl po jménu souboru, což je
        # datum a hash — v UI i v citacích nečitelné. První řádek textu je
        # duplicita jednoho řádku v embeddingu, ale nadpis jde do
        # `heading_path`, se kterým pracuje reranker, takže se to vrátí.
        prvni = next((r.strip() for r in telo.split("\n") if r.strip()), "")
        titulek = prvni[:60].rstrip() or "poznámka"

    fm = [
        "keep_id: %s" % (getattr(note, "id", "") or ""),
        "vytvoreno: %s" % _cas(getattr(getattr(note, "timestamps", None), "created", None)),
        "zmeneno: %s" % _cas(getattr(getattr(note, "timestamps", None), "updated", None)),
    ]
    stitky = _stitky(note)
    if stitky:
        fm.append("stitky: [%s]" % ", ".join(stitky))
    if getattr(note, "pinned", False):
        fm.append("pripnuto: ano")
    # `lang` natvrdo: poznámky jsou česky (volba uživatele 2026-08-20)
    # a autodetekce na třech slovech je loterie. Hodnota z frontmatteru je
    # v indexeru autoritativní a nikdy se nepřehlasuje, takže tohle je
    # zároveň pojistka proti špatnému stemmingu ve fulltextové větvi.
    fm.append("lang: %s" % config.KEEP_LANG)
    # 0 = vlastní poznámka, 1 = importované, 2 = automatický sync z venku.
    # POZOR: `trust_level` dnes NIC neváží — je to jen filtr
    # `trust_level <= p_max_trust` v hybrid_search a `max_trust` je vždy 2.
    # Odlišení váhy keepových útržků od deníku je tím připravené, ne hotové.
    fm.append("trust: %d" % config.KEEP_TRUST)

    # Titulek jde do těla i do nadpisu, ne jen do nadpisu. `chunker.py`
    # nadpis do `content` NEDÁVÁ (jde jen do `heading_path`) - obvykle
    # neškodí, protože tělo stejná slova zopakuje v próze. U Keepu je to
    # ale časté selhání: poznámka, která je jen odkaz ("Drenáž" + URL),
    # by měla `content_tsv` složené výhradně z URL - titulek, jediné
    # smysluplné slovo, by v indexu nebyl vůbec. A poznámka bez těla vůbec
    # ("Objednat", nic pod tím) by chunker.py přeskočil úplně - nula chunků,
    # v indexu neexistuje. Zjištěno 2026-08-27 na dotazu o drenáži: 412
    # z 653 poznámek je jen odkaz, 4 jsou bez těla vůbec.
    zaklad = titulek if not telo else "%s\n\n%s" % (titulek, telo)
    casti = ["---", "\n".join(fm), "---", "", "# " + titulek, "", zaklad, ""]
    if prilohy:
        casti += ["_(V poznámce %d příloh; obrázky se neindexují.)_" % prilohy, ""]
    return "\n".join(casti)


# ---------------------------------------------------------------------------
# Běh
# ---------------------------------------------------------------------------

def _stahni() -> list:
    """Přihlášení a plný pull. Stav se ZÁMĚRNĚ neukládá.

    gkeepapi umí `dump()`/`restore()` a inkrementální `sync()`, ale stav je
    další věc, která může zastarat, rozejít se s realitou a tiše držet
    smazanou poznámku naživu. Plný pull je při hodinovém běhu jeden
    požadavek a pár set kB — cena, která se nevyplatí optimalizovat stavem.
    """
    import gkeepapi

    keep = gkeepapi.Keep()
    keep.authenticate(config.KEEP_EMAIL, config.KEEP_MASTER_TOKEN)
    return list(keep.all())


def _k_indexaci(notes: list) -> list:
    """Koš pryč vždy, archiv podle konfigurace (dnes: taky pryč)."""
    out = []
    for n in notes:
        if getattr(n, "trashed", False):
            continue
        if getattr(n, "archived", False) and not config.KEEP_INCLUDE_ARCHIVED:
            continue
        out.append(n)
    return out


def _varuj(zprava: str) -> None:
    """Telegram, když se úklid zastaví o pojistku. Selhání se jen loguje —
    varování, které shodí sync, by bylo horší než varování, které nedorazí."""
    log.warning("keep: %s", zprava)
    try:
        from . import telegram
        if telegram.enabled():
            telegram._send(config.TELEGRAM_ALLOWED_USER_ID, "Keep sync: " + zprava)
    except Exception as e:                                   # noqa: BLE001
        log.warning("keep: varovani se nepodarilo poslat: %s", e)


def sync(uklid: bool = False, nasucho: bool = False,
         vynutit: bool = False) -> dict:
    """Jeden běh. Vrací souhrn pro log i pro ruční spuštění.

    `uklid=False` (hodinově) soubory jen zakládá a přepisuje. Mazání je
    nevratné a Keep je neoficiální API — kdyby jeden běh vrátil neúplný
    seznam, hodinový úklid by index vykuchal dřív, než by si toho někdo
    všiml. Týdenní kadence dává čas si nesrovnalosti všimnout a git
    v `keep/` drží historii.
    """
    if not enabled():
        raise RuntimeError("keep: chybi KEEP_EMAIL nebo KEEP_MASTER_TOKEN")

    root = Path(config.MARKDOWN_ROOT)
    adresar = root / config.KEEP_DIR
    res = {"z_keepu": 0, "k_indexaci": 0, "nove": 0, "zmenene": 0,
           "beze_zmeny": 0, "prazdne": 0, "smazane": 0, "zablokovano": None}

    notes = _stahni()
    res["z_keepu"] = len(notes)
    vybrane = _k_indexaci(notes)
    res["k_indexaci"] = len(vybrane)

    if not nasucho:
        adresar.mkdir(parents=True, exist_ok=True)

    ocekavane = set()
    zmena = False
    for note in vybrane:
        text = render(note)
        if text is None:
            res["prazdne"] += 1
            continue
        rel = rel_path(note)
        ocekavane.add(rel)
        cesta = root / rel
        stary = cesta.read_text(encoding="utf-8") if cesta.exists() else None
        if stary == text:
            res["beze_zmeny"] += 1
            continue
        res["nove" if stary is None else "zmenene"] += 1
        zmena = True
        if not nasucho:
            cesta.write_text(text, encoding="utf-8")

    if uklid:
        na_disku = {str(p.relative_to(root)) for p in adresar.rglob("*.md")} \
            if adresar.exists() else set()
        k_smazani = sorted(na_disku - ocekavane)
        duvod = None if vynutit else _pojistka(k_smazani, na_disku, res)
        if duvod:
            res["zablokovano"] = duvod
            if nasucho:
                log.warning("keep (nasucho): %s", duvod)
            else:
                _varuj(duvod)
        else:
            for rel in k_smazani:
                if not nasucho:
                    (root / rel).unlink()
                res["smazane"] += 1
                zmena = True

    if zmena and not nasucho:
        # Import az tady: modul pak jde importovat (a testovat prevod
        # poznamky na markdown) bez DB a bez sitovych zavislosti core.
        from . import core
        core.trigger_reindex()
    log.info("keep sync: %s", res)
    return res


def _pojistka(k_smazani: list[str], na_disku: set[str], res: dict) -> str | None:
    """Vrací důvod, proč úklid NEPROVÁDĚT, nebo None.

    Neoficiální API může vrátit neúplný seznam — vypršelý token, výpadek,
    změna na straně Googlu. Bez pojistky by z toho bylo tiché vykuchání
    indexu; s ní přijde zpráva na Telegram a soubory zůstanou.

    Práh je relativní I absolutní zároveň. Samotné procento je u malé
    sbírky k ničemu (u deseti poznámek je 20 % jedna poznámka, tedy běžné
    úterý), samotné absolutní číslo zase u velké.
    """
    if not k_smazani:
        return None
    if res["k_indexaci"] == 0 and na_disku:
        return ("Keep vrátil nula použitelných poznámek, ale na disku jich je %d. "
                "Nic jsem nesmazal — vypadá to na chybu přihlášení, ne na úklid. "
                "Kdybys Keep vyprázdnil/a doopravdy: podman exec kryton "
                "python3 -m app.keep --uklid --potvrdit" % len(na_disku))
    podil = len(k_smazani) / max(len(na_disku), 1)
    if len(k_smazani) > config.KEEP_DELETE_MIN_ABS and podil > config.KEEP_DELETE_MAX_PODIL:
        return ("Úklid by smazal %d z %d poznámek (%.0f %%), což je nad prahem. "
                "Nic jsem nesmazal. Když je to v pořádku, spusť úklid ručně: "
                "podman exec kryton python3 -m app.keep --uklid --potvrdit"
                % (len(k_smazani), len(na_disku), podil * 100))
    return None


# ---------------------------------------------------------------------------
# CLI. Spouští systemd timer uvnitř kontejneru Krytona:
#     podman exec kryton python3 -m app.keep            # hodinově
#     podman exec kryton python3 -m app.keep --uklid    # týdně
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="app.keep", description="Google Keep -> markdown, jednosmerne.")
    ap.add_argument("--uklid", action="store_true",
                    help="smazat soubory bez protejsku v Keepu (tydne)")
    ap.add_argument("--nasucho", action="store_true",
                    help="nic nezapisovat ani nemazat, jen vypsat, co by se stalo")
    ap.add_argument("--potvrdit", action="store_true",
                    help="obejit pojistku na rozsah mazani (jen po rucni kontrole)")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s")
    try:
        res = sync(uklid=a.uklid, nasucho=a.nasucho, vynutit=a.potvrdit)
    except Exception as e:                                   # noqa: BLE001
        # Token vyprsi, Google zmeni API, sit vypadne. Chybu vypsat cele,
        # ale nikdy ne s tokenem — ten se v hlaskach gkeepapi neobjevuje,
        # a kdyby zacal, chytne to tenhle radek na jednom miste.
        log.error("keep sync selhal: %s", e)
        return 1
    if a.nasucho:
        print("NASUCHO — nic se nezapsalo ani nesmazalo.")
    for k, v in res.items():
        if v not in (None, 0):
            print("  %-12s %s" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
