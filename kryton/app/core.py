"""Klienti, autentizace a zápis poznámek."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import httpx

from . import config, db

log = logging.getLogger("kryton")

# ---------------------------------------------------------------------------
# Autentizace: podepsaná session cookie. Bez externího identity provideru,
# pro jednoho uživatele je to dost. Heslo ani secret nikdy neopustí server.
# ---------------------------------------------------------------------------

def _sign(payload: str) -> str:
    mac = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256)
    return mac.hexdigest()


def make_session() -> str:
    exp = str(int(time.time()) + config.SESSION_HOURS * 3600)
    return f"{exp}.{_sign(exp)}"


def valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, sig = cookie.rsplit(".", 1)
    # compare_digest kvůli timing attacku — je to levné a jinak by to byl
    # zbytečně slabý bod.
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def check_password(given: str) -> bool:
    return hmac.compare_digest(given or "", config.AUTH_PASSWORD)


# ---------------------------------------------------------------------------
# Retrieval a LiteLLM
# ---------------------------------------------------------------------------

def search(query: str, limit: int = None, rewrite: bool | None = None) -> dict:
    body = {"query": query, "limit": limit or config.CONTEXT_CHUNKS}
    if rewrite is not None:
        body["rewrite"] = rewrite
    r = httpx.post(config.RETRIEVAL_URL.rstrip("/") + "/search", json=body, timeout=180)
    r.raise_for_status()
    return r.json()


# Kódy odpovídají CHECK constraintu na `document.lang` a mapě v lang.py
# retrievalu. Neznámý kód se nezahodí, jen se ukáže holý.
LANG_NAMES = {"cs": "čeština", "en": "angličtina", "de": "němčina", "la": "latina"}
LANG_TS = {"cs": "czech", "en": "english", "de": "german", "la": "latin"}

# Dotazy, na které se z osmi úryvků odpovědět nedá, protože se ptají na celek.
# Bez diakritiky, protože uživatel ji nemusí psát — porovnává se přes _ascii().
AGREGACNI_SLOVA = (
    "kolik", "pocet", "poctu", "poctem", "celkem", "prumer", "median",
    "statistik", "nejvic", "nejvice", "nejmene", "nejdelsi", "nejkratsi",
    "nejcastej", "serad", "seradit", "rozlozeni", "zastoupen", "podle jazyka",
    "vsech dokumentu", "vsechny dokumenty", "kazdeho jazyka",
    # P14 (2026-08-31): extremální temporální dotazy ("kdy jsem vložil první
    # záznam z telegramu") — stejná mezera, jakou u "vcera"/"posledni" popsal
    # P7. Nerozlišuje kanál, jen nasměruje na odkaz na /korpus; skutečná
    # data teď dává kanal_facts() níž.
    "prvni", "poprve", "nejstarsi",
)


def _ascii(text: str) -> str:
    return (unicodedata.normalize("NFKD", text or "")
            .encode("ascii", "ignore").decode().lower())


def je_agregacni(query: str) -> bool:
    """Vypadá dotaz na souhrn nebo počty nad celým korpusem?

    Schválně hrubé a schválně jen na zobrazení odkazu na /korpus — nic to
    neodklání ani neblokuje. Falešně pozitivní nález stojí jednu nenápadnou
    větu navíc, falešně negativní nezpůsobí nic horšího než dosud.
    """
    return any(w in _ascii(query) for w in AGREGACNI_SLOVA)


def corpus_facts() -> str:
    """Skutečná čísla o korpusu do promptu, ~100 tokenů.

    Tohle je oprava měřeného selhání: model dostane osm úryvků a na otázku
    „kolik je kterých knih" si počty složil z nich (66 anglických = knihy
    Bible) místo z indexu. Fakta v kontextu ho stojí zlomek promptu a dávají
    mu z čeho odpovědět správně.

    Když retrieval neodpovídá, vrátí prázdno — odpověď se tím nezablokuje,
    jen přijde o tenhle blok.
    """
    try:
        s = corpus_stats()
    except Exception as e:
        log.warning("fakta o korpusu nedostupna: %s", e)
        return ""
    by_lang = s.get("documents_by_lang") or {}
    parts = ", ".join("%s %s" % (LANG_NAMES.get(c, c), by_lang[c])
                      for c in sorted(by_lang, key=lambda c: -by_lang[c]))
    # Hlavička je schválně čitelná věta, ne interní nadpis verzálkami:
    # model ji cituje doslova a uživateli se pak v odpovědi objeví
    # „Podle FAKTA O KORPUSU…", což vypadá jako uniklá vnitřnost.
    return ("Ověřená čísla o korpusu (přímo z databáze):\n"
            "- dokumentů celkem: %s\n"
            "- dokumentů podle jazyka: %s\n"
            "- textových úseků (chunků) celkem: %s\n"
            % (s.get("documents", "?"), parts or "neznámé", s.get("chunks", "?")))


def kanal_facts() -> str:
    """Ověřená fakta o tom, odkud a kdy přišly ZÁPISY poznámek (P14).

    Stejná úvaha jako u `corpus_facts()`: dotaz typu „kdy jsem vložil první
    záznam z telegramu" se z osmi dovolávaných úryvků spočítat nedá (žádná
    poznámka o sobě netvrdí, že je první svého kanálu), takže se to bere
    přímo z `inbox`, ne z modelu ani z reranku.

    Sleduje se jen od nasazení sloupce `inbox.kanal` (P14, 2026-08-31) —
    starší zápisy mají `kanal='web'` jako výchozí hodnotu ALTERu, ne
    skutečný původ, takže číslo pro "web" před tímhle datem NEVĚŘIT.
    Bez toho by model tichým zobecněním z výchozí hodnoty prohlásil něco
    o historii, kterou databáze fakticky nezaznamenala.
    """
    try:
        stats = db.kanal_stats()
    except Exception as e:
        log.warning("fakta o kanalech nedostupna: %s", e)
        return ""
    if not stats:
        return ""
    radky = "\n".join(
        "- %s: %d zápisů, první %s, poslední %s" % (
            s["kanal"], s["pocet"],
            s["prvni"].date().isoformat() if s["prvni"] else "?",
            s["posledni"].date().isoformat() if s["posledni"] else "?")
        for s in stats)
    return ("Ověřená čísla o zápisech poznámek podle kanálu (přímo z "
            "databáze, sleduje se od 2026-08-31 — starší 'web' je výchozí "
            "hodnota migrace, ne ověřený původ):\n%s\n" % radky)


# ---------------------------------------------------------------------------
# Deník jako celek (P15)
# ---------------------------------------------------------------------------
# Slova, po kterých se dotaz bere jako dotaz na DENÍK JAKO CELEK, ne na obsah
# jednotlivého zápisu. Bez diakritiky, porovnává se přes `_ascii()` — P7
# naměřil, že „delal vcera" a „dělal včera" daly jiné #1.
#
# ZÁMĚRNĚ SAMOSTATNÝ SEZNAM, ne rozšíření `AGREGACNI_SLOVA`. Ta dvě slova
# dělají různou práci: agregační jen zobrazí odkaz na `/korpus`, deníkové
# vlijí do promptu dva tisíce tokenů. Slití by zaneslo falešné pozitivy
# do obou směrů.
DENIK_SLOVA = (
    "denik", "denicek", "denikove", "zapisy", "zapisu", "zapisech",
    "zaznamy", "zaznamu", "zaznamech",
    "otazky dne", "otazku dne", "otazkach dne", "otazek dne",
    "kazdodenni", "kazdy den", "kazdeho dne",
    "sentiment", "nalad", "emoce", "emocn", "rozpolozeni",
    "trend", "prehled", "souhrn", "shrn",
    "jak pusobi", "jak to pusobi", "jak mi bylo", "jak se mi vedlo",
)


def je_denikovy_prehled(query: str) -> bool:
    """Ptá se dotaz na deník jako celek (sentiment, nálada, trend)?

    SELHÁVÁ ZÁMĚRNĚ OTEVŘENÝM SMĚREM, a to je hlavní argument pro heuristiku
    nad volným textem — tenhle projekt už třikrát naměřil, že klíčová slova
    měří formulaci, ne chování:

      falešně negativní -> přesně dnešní chování (retrieval + práh), tedy
                           žádná regrese proti stavu před P15
      falešně pozitivní -> model dostane ~2000 tokenů deníku navíc
                           za $0,0001 a odpoví o něco hůř

    Obojí je levné. Táž úvaha jako u `je_agregacni()` výš. Kdo chce jistotu,
    použije explicitní cestu: rozbalovátko období na webu nebo `/denik [N]`
    v Telegramu — tam se nehádá nic.
    """
    return any(w in _ascii(query) for w in DENIK_SLOVA)


# Měsíce pro „za srpen". VYPSANÉ TVARY A `\b` NA OBOU STRANÁCH, ne kmeny
# a `in` — dvě naměřené pasti (2026-09-09), obě z podřetězců uvnitř
# nesouvisejících slov:
#
#   „ledn"  je uvnitř „POSLEDNí"  -> „za poslední rok" vracelo LEDEN
#   „zari"  je na začátku „ZAŘÍdil" -> „jak jsem zařídil…" vracelo ZÁŘÍ
#
# První z nich zasáhla nejpřirozenější formulaci vůbec, takže to není
# okrajový případ. `\b` na konci je proto stejně důležité jako na začátku:
# bez něj by „\bzari" pořád sedlo na „zaridil".
#
# Vedlejší efekt: „cervenec" a „cerven" jsou teď různé tokeny, takže na
# pořadí už nezáleží. Zůstává červenec první, aby se ta past nevrátila,
# kdyby se někdo ke kmenům vracel.
_MESICE = (
    (7, r"cervenec|cervence|cervenci"),
    (6, r"cerven|cervna|cervnu"),
    (1, r"leden|ledna|lednu"),
    (2, r"unor|unora|unoru"),
    (3, r"brezen|brezna|breznu"),
    (4, r"duben|dubna|dubnu"),
    (5, r"kveten|kvetna|kvetnu"),
    (8, r"srpen|srpna|srpnu"),
    (9, r"zari"),
    (10, r"rijen|rijna|rijnu"),
    (11, r"listopad|listopadu"),
    (12, r"prosinec|prosince|prosinci"),
)

# Násobky dnů pro „za poslední 3 měsíce". Delší tvary jsou v každé
# alternativě první, aby „mesic" nesebral „mesicich" — `re` bere první
# alternativu, která sedne, ne tu nejdelší.
_JEDNOTKY = (
    (1, r"dn(?:i|u|y|ech)|den"),
    (7, r"tydn(?:u|y|ech|e)|tyden"),
    (30, r"mesic(?:ich|u|e)?"),
    (365, r"let(?:ech|a)?|rok(?:y|u)?"),
)

# Období bez čísla: „za poslední rok", „tenhle měsíc". Musí se řešit zvlášť,
# protože `_JEDNOTKY` vyžadují číslici — a bez tohohle pravidla by „za
# poslední rok" spadlo na výchozích 30 dnů, tedy o řád jinam.
#
# „minulý měsíc" se tu bere jako klouzavých 30 dnů, ne jako předešlý
# KALENDÁŘNÍ měsíc, což striktně vzato znamená. Je to vědomé zjednodušení:
# přesný kalendářní měsíc umí pojmenovaná cesta („za srpen") a rozsah se
# stejně píše nahlas v hlavičce bloku, takže uživatel vidí, co dostal.
_BEZ_CISLA = (
    (365, r"rok|roce|roku"),
    (30, r"mesic|mesice|mesici"),
    (7, r"tyden|tydne|tydnu"),
)

_CELY_DENIK = ("cely denik", "od zacatku", "vsechny zapisy", "vsechny zaznamy",
               "za vsechna leta", "kompletni denik")


def obdobi_z_dotazu(query: str) -> tuple[date, date]:
    """Rozsah datumů, o který dotaz žádá. Default `DENIK_DEFAULT_DNI` dnů.

    Schválně pár regexů nad `_ascii()`, ne knihovna na přirozený čas: chybný
    odhad období je tu levný (rozsah se v bloku píše nahlas, takže je vidět),
    zatímco závislost navíc v kontejneru s MemoryMax=700M levná není. Táž
    úvaha jako u `split_frontmatter()` v chunkeru, který taky není plný
    YAML parser.

    `do` se vždycky zaráží dneškem. U „za září" v září by konec měsíce ležel
    v budoucnosti a hlavička bloku by pak tvrdila „zápis má 8 z 30 dnů",
    přičemž 21 z nich ještě nenastalo.
    """
    q = _ascii(query)
    dnes = date.today()

    if any(w in q for w in _CELY_DENIK):
        return dnes - timedelta(days=config.DENIK_MAX_DNI - 1), dnes

    if "letos" in q or "tento rok" in q:
        return date(dnes.year, 1, 1), dnes

    if "pul roku" in q:
        return dnes - timedelta(days=182), dnes
    if "pul mesice" in q:
        return dnes - timedelta(days=14), dnes

    for nasobek, vzor in _JEDNOTKY:
        m = re.search(r"(\d{1,4})\s*(?:%s)\b" % vzor, q)
        if m:
            dni = max(1, min(int(m.group(1)) * nasobek, config.DENIK_MAX_DNI))
            return dnes - timedelta(days=dni - 1), dnes

    # Pojmenovaný měsíc má přednost před „za poslední měsíc", protože je
    # konkrétnější: „za srpen" je jeden určitý měsíc, „za poslední měsíc"
    # jen délka. U dotazu, kde je obojí, vyhrává ten určitý.
    for cislo, vzor in _MESICE:
        if re.search(r"\b(?:%s)\b" % vzor, q):
            rok = dnes.year
            # Měsíc, který letos ještě nezačal, se míní loni.
            if cislo > dnes.month:
                rok -= 1
            od = date(rok, cislo, 1)
            do = (date(rok + 1, 1, 1) if cislo == 12
                  else date(rok, cislo + 1, 1)) - timedelta(days=1)
            return od, min(do, dnes)

    for dni, vzor in _BEZ_CISLA:
        if re.search(r"\b(?:%s)\b" % vzor, q):
            dni = min(dni, config.DENIK_MAX_DNI)
            return dnes - timedelta(days=dni - 1), dnes

    return dnes - timedelta(days=config.DENIK_DEFAULT_DNI - 1), dnes


def denik_kontext(od: date, do: date) -> str:
    """Deníkové zápisy z rozsahu jako blok pro `extra` v `answer()`.

    ČTE SE Z DISKU, NE Z INDEXU, a to ze tří důvodů:
      1. disk je autoritativní zdroj (viz `safe_path()` níž), index derivát;
      2. dnešní zápis je na disku HNED, zatímco index dobíhá reindexem —
         a u dotazu „jak mi bylo poslední měsíc" je vynechání dneška to
         nejhorší možné selhání;
      3. nezávisí to na běžícím retrievalu.

    Rozsah datumů je filtr na JMÉNO SOUBORU (`denik/RRRR-MM-DD.md`), takže
    proti metadatové cestě navržené v P7-B tu není potřeba žádné SQL.

    Prázdný výsledek se vrací jako "" a volající se tím vrátí k dosavadnímu
    chování. To je záměr: kdyby se místo toho vlil blok „nic tu není",
    obešel by falešně pozitivní nález heuristiky rerankový práh a model by
    odpovídal nad prázdnem místo poctivého „nic jsem nenašel".
    """
    root = _root() / config.DENIK_DIR
    if not root.is_dir():
        log.info("denik %s neexistuje, denikovy kontext se vynechava", root)
        return ""

    dny: list[tuple[date, str]] = []
    for p in sorted(root.glob("*.md")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            # Ručně pojmenovaný soubor v denik/ — přeskoč, datum nehádej.
            continue
        if not (od <= d <= do):
            continue
        try:
            telo = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("denikovy zapis %s se nepodarilo precist: %s", p, e)
            continue
        # `capture()` píše na první řádek `# RRRR-MM-DD`, tedy totéž datum,
        # jaké nese značka `[RRRR-MM-DD]` níž. Odstraní se jen při PŘESNÉ
        # shodě s datem ze jména souboru — jakýkoli jiný nadpis je obsah
        # a zahodit ho by znamenalo tiše ztratit kus zápisu.
        nadpis = "# " + d.isoformat()
        if telo.startswith(nadpis):
            telo = telo[len(nadpis):].lstrip("\n")
        if telo:
            dny.append((d, telo))

    if not dny:
        return ""

    # Počet dnů se zápisem se bere PŘED uříznutím, a to je podstatné: po
    # uříznutí by hlavička tvrdila „zápis má 6 z 30 dnů", zatímco zápis má
    # 28 dnů a jen 6 se jich vešlo. To je jiné tvrzení a model by z něj
    # odvodil, že v tom období skoro nic není.
    se_zapisem = len(dny)

    # Rozpočet: uříznout NEJSTARŠÍ a říct to. Tiché uříznutí by znamenalo,
    # že model odpovídá o jiném období, než o jaké byl požádán.
    znaku = sum(len(t) for _, t in dny)
    urizli = 0
    while len(dny) > 1 and znaku > config.DENIK_CONTEXT_CHARS:
        _, vyhozene = dny.pop(0)
        znaku -= len(vyhozene)
        urizli += 1

    if len(dny) <= config.DENIK_DETAIL_MAX_DNI:
        pokyn = "Zápisů je %d, takže můžeš jít den po dni." % len(dny)
    else:
        # ZMĚŘENO 2026-09-09: tabulka den po dni stojí ~70 výstupních tokenů
        # na den, takže 29 dnů narazilo na finish_reason=length při stropu
        # 2000. Bez tohohle pokynu se odpověď na delší období odsekne
        # uprostřed tabulky.
        pokyn = ("Zápisů je %d — NEVYPISUJ je den po dni. Shrň je po týdnech "
                 "nebo měsících a jmenuj jen výrazné výjimky; tabulka po "
                 "dnech by se do limitu odpovědi nevešla a odpověď by se "
                 "uprostřed odsekla." % len(dny))

    # „ÚPLNÝ obsah" se tvrdí JEN když se nic neuřízlo. Po uříznutí by to byla
    # lež přímo v tom řádku, který má modelu dát jistotu, že nemusí nic
    # domýšlet — a rozpor s varováním o vynechaných dnech níž.
    uplnost = ("je to ÚPLNÝ obsah toho období, ne ukázka" if not urizli
               else "je to obsah toho období, ale ZKRÁCENÝ, viz upozornění níž")

    hlavicka = (
        "Deníkové zápisy uživatele za období %s až %s. Načteno přímo ze "
        "souborů %s/RRRR-MM-DD.md, ne vyhledáváním — %s.\n"
        "Zápis má %d z %d dnů rozsahu. Dny bez zápisu prostě chybí "
        "a NEZNAMENAJÍ, že se ten den nic nedělo.\n"
        "O době mimo uvedený rozsah neodvozuj nic.\n"
        "Zápisy cituj DATEM v hranatých závorkách, třeba [%s]. Čísla úryvků "
        "jako [1] nebo [0] tyhle zápisy nemají, tak je nepoužívej.\n"
        "%s\n"
        % (od.isoformat(), do.isoformat(), config.DENIK_DIR, uplnost,
           se_zapisem, (do - od).days + 1, dny[-1][0].isoformat(), pokyn))

    if urizli:
        hlavicka += (
            "POZOR: požadované období se do kontextu nevešlo, takže %d "
            "nejstarších dnů BYLO VYNECHÁNO. Zápisy níž začínají %s, ne %s "
            "— napiš to uživateli.\n"
            % (urizli, dny[0][0].isoformat(), od.isoformat()))
        log.info("denikovy kontext: urizmuto %d nejstarsich dnu, strop %d znaku",
                 urizli, config.DENIK_CONTEXT_CHARS)

    telo = "\n\n".join("[%s]\n%s" % (d.isoformat(), t) for d, t in dny)
    return hlavicka + "\n" + telo + "\n"


def corpus_stats() -> dict:
    """`/stats` retrievalu: kolik je čeho v indexu.

    Agregační otázky („kolik je kterých knih") přes RAG nejdou — model dostane
    osm úryvků a z nich se tisíc dokumentů spočítat nedá. Odpověď je přitom
    v databázi přesně, takže se pro ni chodí sem, ne k modelu.
    """
    r = httpx.get(config.RETRIEVAL_URL.rstrip("/") + "/stats", timeout=20)
    r.raise_for_status()
    return r.json()


def trigger_reindex() -> None:
    """Po zápisu poznámky. Selhání se jen zaloguje — poznámka je uložená,
    což je to podstatné; index se dorovná při dalším běhu."""
    if not config.REINDEX_AFTER_WRITE:
        return
    try:
        httpx.post(config.RETRIEVAL_URL.rstrip("/") + "/reindex", timeout=10)
    except httpx.HTTPError as e:
        log.warning("reindex se nepodarilo spustit: %s", e)


DNY_CZ = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")


def dnesni_datum() -> str:
    """Dnešní datum do promptu, ~25 tokenů.

    Oprava měřeného selhání (P7, 2026-08-17): prompt datum NEOBSAHOVAL vůbec,
    takže na dotaz „co jsem dělal včera" model datum odhadoval z názvů souborů
    v dodaných úryvcích (`denik/RRRR-MM-DD.md`) — a spletl se o den (tvrdil
    15. 8. místo 16. 8.). Proto ta věta o neodvozování z názvů: přesně tuhle
    cestu model volil, když neměl nic lepšího.

    Hlavička je schválně čitelná věta, ne nadpis verzálkami — stejný důvod
    jako u `corpus_facts()`: model ji cituje doslova a „Podle DNEŠNÍ DATUM…"
    vypadá jako uniklá vnitřnost.

    Datum je v UTC, tedy ve stejné časové zóně, v jaké se pojmenovávají
    soubory deníku (`capture()` používá `date.today()` a brain běží v UTC) —
    když jsou obojí stejná konvence, aspoň si neodporují. Cena: mezi místní
    půlnocí a 02:00 (CEST) je UTC datum o den pozadu, takže zápis z pozdní
    noci padne do souboru předešlého dne. To je stávající chování `capture()`,
    tenhle blok ho jen neopravuje ani nezhoršuje.
    """
    d = date.today()
    return ("Dnešní datum (autoritativní — neodvozuj ho z názvů souborů "
            "v úryvcích): %s %s.\n" % (DNY_CZ[d.weekday()], d.isoformat()))


SYSTEM = (
    "Jsi asistent nad osobními poznámkami uživatele. Odpovídej česky a POUZE "
    "na základě dodaného kontextu.\n"
    "Za každým tvrzením uveď odkaz na zdroj ve tvaru [1], [2] podle čísel "
    "úryvků níže.\n"
    "Když kontext na otázku neodpovídá, řekni to přímo — nedomýšlej si. "
    "Je lepší přiznat, že v poznámkách odpověď není, než ji vymyslet.\n"
    "Předchozí zprávy konverzace slouží jen k pochopení, na co se uživatel "
    "ptá teď. Nejsou zdrojem faktů — ta ber výhradně z úryvků.\n"
    "Počty a souhrny ber VÝHRADNĚ z bloků s ověřenými čísly o korpusu "
    "a s výsledkem výpočtu nad databází, nikdy je nedopočítávej z úryvků. "
    "Úryvků dostáváš jen několik a celkový obraz z nich složit nejde — "
    "spočítat knihy zmíněné v úryvcích a vydávat to za obsah databáze "
    "je chyba. Když ověřená čísla na otázku neodpovídají, řekni to přímo "
    "a odkaž uživatele na stránku /korpus.\n"
    "Piš plynulou češtinou a názvy těch bloků necituj doslova."
)


def history_messages(prior: list[dict]) -> list[dict]:
    """Předchozí tahy konverzace do formátu chat completions.

    Ořezává se dvakrát: počtem zpráv i délkou každé z nich. Odpovědi
    reasoning modelu bývají dlouhé a bez stropu by kontext rostl s každým
    tahem, dokud by nepřerostl rozpočet klíče.
    """
    out = []
    for m in (prior or [])[-config.HISTORY_MESSAGES:]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > config.HISTORY_CHARS:
            content = content[:config.HISTORY_CHARS] + " […]"
        out.append({"role": m["role"], "content": content})
    return out


def dost_relevantni(hits: list[dict]) -> list[dict]:
    """Vyhodí chunky, které reranker označil za nerelevantní (P4 + P7-B).

    ZAHAZUJÍ SE JEDNOTLIVÉ CHUNKY, nejen se hlídá ten nejlepší — a to je
    podstatný rozdíl. Kdyby se jen porovnával maximum a pak se poslalo
    všechno, zůstal by přesně mechanismus P4: dotaz má jednu dobrou trefu
    a k ní sedm chunků se skóre ~1e-05, a model si citaci připíše k jednomu
    z těch sedmi. Odsud se šum do promptu vůbec nedostane, takže není k čemu
    fabrikovat citaci.

    Když po filtru nezbyde nic, `answer()` spadne do své existující větve
    „v poznámkách jsem nic nenašel" — a když je `extra` neprázdné (ověřená
    čísla nebo výsledek SQL z P1b), odpoví se z něj, protože to je
    autoritativní zdroj, ne dohledaný text.

    ČTE SE VÝHRADNĚ `rerank_score`. `rrf_score` je funkce POŘADÍ a leží
    vždycky kolem 0,016–0,033, tedy přesně v pásmu, kde jsou nízká rerank
    skóre — jakákoliv záloha na něj by porovnávala nesouměřitelná čísla.

    SELHÁVÁ SE OTEVŘENĚ: chunk bez `rerank_score` (reranking vypnutý nebo
    starší odpověď retrievalu) se PONECHÁ. Bez signálu se chováme jako dřív;
    zahodit kontext kvůli chybějícímu poli by z drobné změny v retrievalu
    udělalo tiché „nic jsem nenašel" na každý dotaz.
    """
    prah = config.ANSWER_MIN_RERANK
    if prah <= 0:
        return hits
    out = [h for h in hits
           if h.get("rerank_score") is None or float(h["rerank_score"]) >= prah]
    zahozeno = len(hits) - len(out)
    if zahozeno:
        log.info("rerank prah %.3f: zahozeno %d z %d chunku (nejlepsi zbyle %s)",
                 prah, zahozeno, len(hits),
                 max((h.get("rerank_score") for h in out
                      if h.get("rerank_score") is not None), default=None))
    return out


def podle_jistoty(hits: list[dict]) -> tuple[list[dict], bool]:
    """Rozdělí chunky na tři pásma a vrátí `(chunky, slaba_opora)`.

    DVOUSTUPŇOVÉ ODMÍTNUTÍ (návrh z D6, zaveden 2026-09-18). `dost_relevantni()`
    zná jen dva stavy: buď chunk projde, nebo se o něm uživatel nikdy nedozví.
    Na účetním korpusu se ukázalo, že ten druhý stav je drahý — práh 0,1 tam
    odmítl 7 ze 42 zodpověditelných dotazů a uživateli řekl „nic jsem nenašel"
    o dokumentu, který v korpusu prokazatelně leží (měření viz komentář
    u `config.ANSWER_WEAK_RERANK`).

    Pásma:
      `>= ANSWER_MIN_RERANK`   — normální odpověď, beze změny proti dřívějšku,
      `[WEAK, MIN)`            — odpověď S VÝHRADOU; použije se JEN když nad
                                 hlavním prahem neleží NIC,
      `< ANSWER_WEAK_RERANK`   — šum, zahazuje se dál (ochrana P4 zůstává).

    PROČ SE SLABÉ PÁSMO BERE AŽ KDYŽ SILNÉ JE PRÁZDNÉ, a ne jako doplněk.
    Kdyby se přimíchávalo k dobrým trefám, vznikl by přesně mechanismus P4:
    jedna dobrá trefa a k ní pár chunků těsně nad šumem, ke kterým si model
    připíše citaci. Slabé pásmo je nouzový režim pro dotaz, který by jinak
    skončil mlčky — ne přílepek k odpovědi, která stojí sama.

    VRACÍ SE PŘÍZNAK, NE JEN CHUNKY, protože volající musí vědět, že má
    odpověď označit. Tichý přechod do slabého režimu by z dvoustupňového
    odmítnutí udělal prosté snížení prahu, a to je právě to, co se v P4
    nesmí stát.
    """
    prah = config.ANSWER_MIN_RERANK
    silne = dost_relevantni(hits)
    if silne or prah <= 0:
        return silne, False

    slaby = config.ANSWER_WEAK_RERANK
    # `slaby >= prah` by pásmo obrátilo naruby; ber to jako vypnuto.
    if slaby <= 0 or slaby >= prah:
        return silne, False

    # Chunk BEZ skóre sem nepatří: `dost_relevantni()` ho propouští do silného
    # pásma (selhává otevřeně), takže když jsme tady, žádný takový neexistuje.
    slabe = [h for h in hits
             if h.get("rerank_score") is not None
             and slaby <= float(h["rerank_score"]) < prah]
    if not slabe:
        return silne, False

    log.info("slaba opora: nad prahem %.3f nic, v pasmu %.3f-%.3f je %d chunku "
             "(nejlepsi %.4f) -> odpoved s vyhradou misto mlceni",
             prah, slaby, prah, len(slabe),
             max(float(h["rerank_score"]) for h in slabe))
    return slabe, True


VERIFY_SYSTEM = (
    "Dostaneš PODKLADY a ODPOVĚĎ, která z nich měla vzniknout. Tvůj jediný "
    "úkol je ověřit, jestli každé věcné tvrzení odpovědi DOSLOVA vyplývá "
    "z podkladů.\n"
    "Pravidla:\n"
    "- Číslo, datum, částka, lhůta, měna nebo jméno, které v podkladech "
    "není, je CHYBA — i kdyby to byla pravda ve skutečném světě.\n"
    "- Tvrzení silnější, než co podklad říká, je CHYBA. Když podklad uvádí "
    "použití nějaké hodnoty a odpověď z toho udělá maximum nebo pravidlo, "
    "je to CHYBA.\n"
    "- Věta „v podkladech to není“ CHYBA NENÍ, to je správná odpověď.\n"
    "- Sloh, formulace ani úplnost tě nezajímají. Jen opora v podkladech.\n"
    "Odpověz PŘESNĚ takto:\n"
    "první řádek: OK nebo CHYBA\n"
    "druhý řádek (jen u CHYBA): které tvrzení a proč, jednou větou."
)


def over_odpoved(text: str, podklady: str) -> tuple[bool | None, str]:
    """Druhé volání modelu: vyplývá každé tvrzení odpovědi z podkladů?

    Návrh 1 z POZADAVKY P4, zvolený poté, co měření na účetní sadě vyloučilo
    prahování (`ANSWER_MIN_RERANK` nechytí fabrikaci nad RELEVANTNÍM chunkem,
    protože skóre měří vztah chunku k dotazu, ne přítomnost faktu).

    Vrací `(verdikt, duvod)`, kde verdikt je TROJSTAVOVÝ:
      `True`  — tvrzení mají oporu,
      `False` — kontrola našla tvrzení bez opory, `duvod` říká které,
      `None`  — kontrola NEPROBĚHLA (chyba volání, nerozluštitelný výstup).

    `None` se nesmí slít s `True`. „Neověřeno" a „ověřeno OK" jsou dvě různé
    věci a v denním reportu se musí dát rozlišit — jinak by výpadek kontroly
    vypadal jako samé čisté odpovědi. Táž úvaha jako u `n_nad_prahem`.

    SELHÁVÁ SE OTEVŘENĚ. Když volání spadne, vrátí se `None` a odpověď jde
    uživateli tak jako tak: blokovat ji kvůli nedostupnému kontrolorovi by
    z pojistky udělalo nový SPOF (P6). Do logu ale jde WARNING, protože
    tichá degradace je přesně P8.

    PARSUJE SE PRVNÍ ŘÁDEK, NE KLÍČOVÁ SLOVA V TEXTU. Hledat „CHYBA" kdekoliv
    v odpovědi by znamenalo měřit formulaci místo rozhodnutí — na to projekt
    najel 2026-08-18 třikrát za jediný den (detektor fabrikace nahlásil 1 ze 3,
    skutečnost byla 2 ze 3). Když první řádek nedává ani OK, ani CHYBA, je to
    `None`, ne odhad.
    """
    if not podklady.strip():
        return None, "bez podkladů není co ověřovat"
    try:
        r = httpx.post(
            config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
            json={"model": config.ANSWER_VERIFY_MODEL,
                  "messages": [
                      {"role": "system", "content": VERIFY_SYSTEM},
                      {"role": "user",
                       "content": f"PODKLADY:\n{podklady}\n\nODPOVĚĎ:\n{text}"},
                  ],
                  "max_tokens": config.ANSWER_VERIFY_MAX_TOKENS,
                  "timeout": config.ANSWER_VERIFY_TIMEOUT},
            timeout=config.ANSWER_VERIFY_TIMEOUT + config.ANSWER_TIMEOUT_MARGIN)
        if r.status_code != 200:
            log.warning("overeni odpovedi neprobehlo: LiteLLM %d: %s",
                        r.status_code, r.text[:200])
            return None, "kontrola nedostupná"
        out = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:                       # noqa: BLE001 — fail open
        log.warning("overeni odpovedi neprobehlo: %r", e)
        return None, "kontrola selhala"

    radky = [x.strip() for x in out.splitlines() if x.strip()]
    prvni = radky[0].upper() if radky else ""
    duvod = radky[1] if len(radky) > 1 else ""
    if prvni.startswith("OK"):
        return True, ""
    if prvni.startswith("CHYBA"):
        log.warning("overeni naslo tvrzeni bez opory: %s", duvod or out[:200])
        return False, duvod or "kontrola neuvedla důvod"
    log.warning("overeni vratilo nerozlustitelny vystup: %s", out[:200])
    return None, "kontrola odpověděla mimo tvar"


def zkontroluj_fallback(pozadovany: str, vraceny: str) -> bool:
    """WARNING do logu, když odpověď přišla od jiného modelu, než jsme chtěli.

    LiteLLM při úspěchu primárního aliasu vrací v poli `model` JMÉNO ALIASU
    (`reasoning`), zatímco po propadu na fallback vrací konkrétní model
    (`google/gemma-4-26b-a4b-it:free`). Neshoda tedy znamená, že primární
    model selhal a odpovídal někdo jiný.

    PROČ TO VŮBEC JE (P8, 2026-08-17): big-pickle měl od 13. 8. vyčerpanou
    free kvótu a všechno tiše odpovídala `gemma:free`. Nikde to nebylo vidět
    — LiteLLM zapíše do spend logu JEN úspěšný fallback, selhaný primární
    pokus nezanechá řádek, a uživatel nedostane žádnou chybu. Trvalo čtyři
    dny, než se to provalilo, a to až nepřímo: gemma dostala `max_tokens`
    kalibrované pro big-pickle, zacyklila se na 8000 tokenů a do Telegramu
    přišlo 24 000 znaků nesmyslu.

    Tichý propad kvality je horší než hlasitá chyba — proto WARNING.

    POZOR: tohle chování LiteLLM je POZOROVANÉ (třikrát, 2026-08-17/18), ne
    zaručené dokumentací. Kdyby začal vracet konkrétní model i u primárního,
    hlásilo by to propad pokaždé — otravné, ale neškodné a snadno poznatelné.
    """
    if not vraceny or vraceny == pozadovany:
        return False
    log.warning("model se propadl na fallback: chtel jsem %r, odpovedel %r "
                "(nejcasteji vycerpana kvota primarniho modelu, viz P8)",
                pozadovany, vraceny)
    return True


class Odpoved(NamedTuple):
    """Odpověď i se stopou po rozhodnutích, která k ní vedla.

    Do 2026-08-24 se vracela trojice `(text, model, ms)`. Stopa přibyla
    proto, že P4, P7-B i P8 jsou shodně chyby, které V TEXTU ODPOVĚDI VIDĚT
    NEJSOU: u P7-B neprojde prahem ani jeden chunk a uživatel dostane
    zdvořilé „nic jsem nenašel", u P8 odpovídá čtyři dny jiný model, než
    který jsme chtěli. Obojí je v okamžiku odpovědi ZNÁMÉ — jen se to
    zahazovalo. Odsud to jde do `message` a v `31-denni-report.py` z toho
    vzniká alert.

    `model` je ten, který SKUTEČNĚ odpověděl (LiteLLM vrací po propadu
    konkrétní model místo aliasu, viz `zkontroluj_fallback`). Požadovaný
    alias je `config.ANSWER_MODEL`, takže se druhý sloupec neukládá —
    `stopa["fallback"]` říká, jestli se ty dva lišily.
    """
    text: str
    model: str
    ms: int
    stopa: dict


def _stopa(kandidatu: int, nad_prahem: int, max_rerank: float | None,
           odmitnuto: bool, fallback: bool, odseknuto: bool = False,
           denik_dni: int | None = None, slaba_opora: bool = False,
           overeno: bool | None = None) -> dict:
    """`odseknuto`, `denik_dni` i `slaba_opora` mají default, protože
    v předčasném návratu (nic nad prahem) nemají co říct: model se nezavolal,
    takže se nemá kde odseknout, deníkový blok se do `extra` nedostal — jinak
    by se ten návrat vůbec neprovedl — a do slabého pásma se nedostalo nic.

    `slaba_opora` je tu proto, že bez ní by dvoustupňové odmítnutí bylo
    z reportu k nerozeznání od normální odpovědi: `odmitnuto` je False,
    `n_nad_prahem` nenulové, `max_rerank` nízké. Přesně ten druh rozdílu,
    který se podle P8 musí dát vyčíst z dat, ne hádat z textu odpovědi.

    `overeno` je TROJSTAVOVÉ (True/False/None) a jeho default je None,
    protože „neověřeno" není totéž co „ověřeno OK" — viz `over_odpoved()`."""
    return {"n_kandidatu": kandidatu, "n_nad_prahem": nad_prahem,
            "max_rerank": max_rerank, "odmitnuto": odmitnuto,
            "fallback": fallback, "odseknuto": odseknuto,
            "denik_dni": denik_dni, "slaba_opora": slaba_opora,
            "overeno": overeno}


def zaznamenej(kanal: str, dotaz: str, odp: "Odpoved | None",
               hits: list[dict] | None = None, chyba: str = "") -> None:
    """Uloží dotaz a odpověď z kanálu, který nemá vlastní konverzační vlákno.

    NIKDY NEVYHODÍ VÝJIMKU, a to je celý smysl téhle funkce. Telegram i MCP
    do 2026-08-24 odpovídaly bez databáze úplně; kdyby je zápis mohl shodit,
    udělal by z auditní stopy novou závislost té jediné cesty, kterou
    uživatel používá denně — a výpadek Postgresu by se projevil jako mlčící
    bot. Záznam smí selhat tiše do logu, odpověď ne. Je to tatáž úvaha jako
    u `detect_language()`, jen obrácená: tam tichý propad ŠKODIL, protože
    se zapisoval do indexu; tady je tichý propad správně, protože se jím nic
    neřídí.

    Konverzace je jedna na (kanál, den) — viz `db.konverzace_kanalu()`.
    """
    try:
        cid = db.konverzace_kanalu(kanal, date.today().isoformat())
        db.add_message(cid, "user", dotaz)
        if odp is None:
            db.add_message(cid, "assistant", "Dotaz selhal: %s" % chyba)
            return
        cits = [{"source_path": h["source_path"],
                 "heading_path": h.get("heading_path"),
                 "chunk_id": h["chunk_id"],
                 "rerank_score": h.get("rerank_score")}
                for h in (hits or [])]
        db.add_message(cid, "assistant", odp.text, cits, odp.model, odp.ms,
                       stopa=odp.stopa)
    except Exception:
        log.exception("zaznam dotazu z kanalu %r do DB selhal (odpoved doruce"
                      "na, jen se neulozila)", kanal)


def answer(query: str, hits: list[dict], prior: list[dict] | None = None,
           extra: str = "", denik_dni: int | None = None) -> Odpoved:
    """Vrátí `Odpoved(text, model, latence_ms, stopa)`.

    `prior` jsou předchozí zprávy konverzace. Pozor na hranici: slouží jen
    generování odpovědi. Vyhledávání dostává surový text dotazu, takže
    u doplňujícího dotazu bez podstatného jména („a proč?") se sice model
    zorientuje, ale chunky se dohledávají podle té krátké fráze.

    `denik_dni` řídí deníkovou cestu z P15 a má TŘI stavy, ne dva:
      `None` — rozhodni heuristikou `je_denikovy_prehled()` a rozsah vezmi
               z `obdobi_z_dotazu()`; tohle je výchozí chování,
      `0`    — uživatel deník VÝSLOVNĚ vypnul (rozbalovátko na webu),
      `> 0`  — deník zapnut na tolik dnů, bez ohledu na text dotazu.

    Rozdíl mezi `None` a `0` je podstatný: u `None` se ještě hádá, u `0` se
    hádat nesmí. Kdyby to byl jeden stav, nešlo by heuristiku vypnout.
    """
    kandidatu = len(hits)
    # Maximum se bere PŘED filtrem, a to je podstatné: po filtru je u P7-B
    # seznam prázdný, takže by se do stopy uložilo None a z reportu by
    # nešlo poznat rozdíl mezi „reranker nic nenabídl" a „nabídl 0,0215,
    # což je o řád pod prahem 0,1". Právě tenhle rozdíl je celá diagnóza.
    nejlepsi = max((float(h["rerank_score"]) for h in hits
                    if h.get("rerank_score") is not None), default=None)
    hits, slaba_opora = podle_jistoty(hits)
    ctx = "\n\n".join(
        f"[{i+1}] {h['source_path']}"
        + (f" — {h['heading_path']}" if h.get("heading_path") else "")
        + f"\n{h['content']}"
        for i, h in enumerate(hits))
    if je_agregacni(query):
        # P14 oprava (nalezená až živým ověřením po prvním nasazení):
        # `kanal_facts()` počítané až NÍŽ by na dotaz „kdy jsem vložil první
        # záznam z telegramu" nikdy nedoběhlo — ten dotaz má skoro nulové
        # rerank skóre (žádná poznámka o sobě netvrdí, že je první svého
        # kanálu), takže `ctx` vyjde prázdné a funkce by se vrátila o řádek
        # níž, DŘÍV, než se fakta o kanálech vůbec spočítají. Řešení je
        # stejné jako u P1b: vlít je do `extra`, který se do rozhodnutí
        # počítá — a udělat to TADY, ne v každém volajícím zvlášť (P1b to
        # dělá jen v `main.py`, takže Telegram a MCP z něj dodnes nic nemají).
        kf = kanal_facts()
        if kf:
            extra = (extra + "\n" if extra else "") + kf

    # P15: deník jako celek. MUSÍ to být TADY, ne v callerech — `extra` se
    # dodnes plní jen v `main.py`, takže Telegram a MCP z P1b nikdy nic
    # nedostaly. Přesně ta past, na kterou narazil P14 o dva bloky výš.
    #
    # Neprázdné `extra` obchází `dost_relevantni()` úplně, takže se
    # ANSWER_MIN_RERANK nesahá a dál dělá svou práci pro P4. To je celý
    # trik: deníkové zápisy nejsou dohledaný text, ale ověřený podklad —
    # stejná kategorie jako spočítané číslo z P1b.
    denik_dnu = None
    obdobi = None
    if denik_dni == 0:
        pass                                  # výslovně vypnuto, nehádej
    elif denik_dni:
        do = date.today()
        dni = max(1, min(denik_dni, config.DENIK_MAX_DNI))
        obdobi = (do - timedelta(days=dni - 1), do)
    elif je_denikovy_prehled(query):
        obdobi = obdobi_z_dotazu(query)

    if obdobi:
        od, do = obdobi
        blok = denik_kontext(od, do)
        if not blok and denik_dni:
            # Jen u VÝSLOVNÉHO požadavku. „V tom rozsahu nemáš zápisy" je
            # správná a užitečná odpověď, ale u heuristiky by tenhle blok
            # nechal falešně pozitivní nález obejít práh a model by
            # odpovídal nad prázdnem místo poctivého „nic jsem nenašel".
            blok = ("Deníkové zápisy za období %s až %s: v tom rozsahu NENÍ "
                    "ANI JEDEN zápis. Řekni to uživateli přímo a neodvozuj "
                    "nic o jiných obdobích.\n"
                    % (od.isoformat(), do.isoformat()))
        if blok:
            extra = (extra + "\n" if extra else "") + blok
            denik_dnu = (do - od).days + 1
            log.info("denikova cesta: %s az %s (%d dnu), %d znaku do promptu",
                     od.isoformat(), do.isoformat(), denik_dnu, len(blok))

    if not ctx and not extra:
        return Odpoved("V poznámkách jsem k tomu nic nenašel.", "", 0,
                       _stopa(kandidatu, 0, nejlepsi, True, False))

    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += history_messages(prior)
    facts = corpus_facts()
    uryvky = f"Úryvky z poznámek:\n\n{ctx}\n\n" if ctx else ""
    # Výhrada patří do UŽIVATELSKÉ zprávy, ne do SYSTEM: system prompt je
    # pro všechny dotazy stejný a tahle instrukce platí jen pro tenhle jeden.
    # Dává se PŘED úryvky, aby ji model četl dřív, než si o nich udělá obrázek.
    vyhrada = (
        "POZOR: úryvky níž mají k otázce jen SLABOU VAZBU — reranker jim dal "
        "nízké skóre a je dost možné, že odpověď v korpusu vůbec není. "
        "Začni odpověď větou, že si nejsi jistý a uživatel si to má ověřit "
        "ve zdroji. Když v úryvcích odpověď opravdu není, řekni to rovnou "
        "a nic nedomýšlej.\n\n"
    ) if slaba_opora else ""
    msgs.append({"role": "user",
                 "content": dnesni_datum()
                            + (f"{facts}\n" if facts else "")
                            + (f"{extra}\n" if extra else "")
                            + vyhrada + uryvky + f"Otázka: {query}"})

    # `timeout` V TĚLE požadavku je deadline pro LiteLLM, `timeout=` u httpx
    # je deadline klienta — a ten MUSÍ být delší, jinak se Kryton vzdá dřív,
    # než mu LiteLLM stihne chybu ohlásit. Stejná úvaha jako u long pollingu
    # v telegram.py, kde se `http_timeout` taky drží nad serverovým `timeout`.
    #
    # Bez toho v těle nastane přesně incident z 2026-08-17 (P8): Kryton se
    # vzdal po 180 s, ale LiteLLM mlel dál CELKEM 743 s, doběhlo na 8000
    # tokenů a výsledek si uložilo do cache — takže opakovaný dotaz ho vrátil
    # obratem a uživateli přišlo 24 000 znaků nesmyslu v šesti zprávách.
    # Práce po deadlinu je jedna škoda, mina v cache na hodinu druhá.
    #
    # STROP NA VÝSTUP je u deníkových dotazů jiný, a je to změřená potřeba,
    # ne pohodlí: tabulka den po dni stojí ~70 výstupních tokenů na den,
    # takže 29 dnů narazilo na `finish_reason=length` už při 2000. Zvyšovat
    # `ANSWER_MAX_TOKENS` globálně nelze — bylo 2026-08-18 SNÍŽENO z 8000
    # jako přímá oprava P8, kde gemma dostala strop šitý pro big-pickle
    # a vyrobila 24 000 znaků nesmyslu.
    strop = (config.DENIK_ANSWER_MAX_TOKENS if denik_dnu
             else config.ANSWER_MAX_TOKENS)
    t0 = time.time()
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.ANSWER_MODEL,
              "messages": msgs,
              "max_tokens": strop,
              "timeout": config.ANSWER_TIMEOUT},
        timeout=config.ANSWER_TIMEOUT + config.ANSWER_TIMEOUT_MARGIN)
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(f"LiteLLM {r.status_code}: {r.text[:200]}")
    d = r.json()
    propadl = zkontroluj_fallback(config.ANSWER_MODEL, d.get("model") or "")
    msg = d["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    fin = d["choices"][0].get("finish_reason")
    odseknuto = False
    if not text:
        # Reasoning model spotřeboval token budget na reasoning_content.
        # Změřeno u big-pickle: při nízkém max_tokens vrátí prázdný content
        # s finish_reason=length. Radši to řekni, než vrátit prázdno.
        text = (f"(model nevrátil odpověď, finish_reason={fin} — "
                f"zvyš ANSWER_MAX_TOKENS)")
    elif fin == "length":
        # P15: NEPRÁZDNÁ odpověď na stropu se dosud vracela jako hotová.
        # Prázdnou větev výš měl kód od začátku, tuhle ne — a je zrádnější:
        # tabulka sentimentu, která končí u 20. srpna, vypadá dokončeně.
        # Je to táž kategorie jako P4 a P8, tedy „věrohodný nesmysl je horší
        # než přiznané selhání", jen na výstupní straně.
        odseknuto = True
        log.warning("odpoved odseknuta na stropu %d tokenu (denik_dni=%s); "
                    "cela odpoved: %s", strop, denik_dnu, text)
        text += ("\n\n⚠️ Odpověď je odseknutá na limitu %d tokenů — "
                 "zužte období nebo otázku." % strop)

    # Ověřovací druhé volání (P4 varianta B). Ověřuje se proti TÝMŽ podkladům,
    # které dostal model — tedy včetně `extra`, ne jen proti úryvkům. Ověřená
    # čísla z P1b a deníkové bloky v `ctx` nejsou, takže by je kontrola bez
    # nich hlásila jako tvrzení bez opory a zahltila by falešnými nálezy.
    #
    # Varování se PŘIPOJUJE, odpověď se nemaže. Kontrola sama může mít falešně
    # pozitivní nález a schovat správnou odpověď by uživateli vzalo možnost
    # posoudit ji podle citací. Přiznaná pochybnost je přesně to, co tenhle
    # projekt staví proti věrohodnému nesmyslu — stejný vzorec jako `odseknuto`
    # o pár řádků výš.
    overeno = None
    if config.ANSWER_VERIFY:
        podklady = ((extra + "\n\n") if extra else "") + ctx
        overeno, duvod = over_odpoved(text, podklady)
        if overeno is False:
            text += ("\n\n⚠️ Ověření odpovědi neprošlo: %s — ber ji jako "
                     "nepodloženou a zkontroluj zdroj." % duvod)

    return Odpoved(text, d.get("model", config.ANSWER_MODEL), ms,
                   _stopa(kandidatu, len(hits), nejlepsi, False, propadl,
                          odseknuto, denik_dnu, slaba_opora, overeno))


# ---------------------------------------------------------------------------
# Zápis poznámek
# ---------------------------------------------------------------------------

SAFE = re.compile(r"[^0-9A-Za-z\-_]+")


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = SAFE.sub("-", t).strip("-").lower()
    return (t or "poznamka")[:60]


def _root() -> Path:
    return Path(config.MARKDOWN_ROOT)


def safe_path(rel: str) -> Path:
    """Zabrání path traversalu i zápisu do .git.

    Píšeme do adresáře, který je AUTORITATIVNÍ zdroj dat, takže tady se
    nešetří: cokoliv mimo MARKDOWN_ROOT nebo do tečkového adresáře je chyba.
    """
    p = (_root() / rel).resolve()
    root = _root().resolve()
    if not str(p).startswith(str(root) + "/"):
        raise ValueError("cesta mimo MARKDOWN_ROOT")
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        raise ValueError("cesta do skryteho adresare")
    if p.suffix != ".md":
        raise ValueError("jen soubory .md")
    return p


def capture(text: str, title: str | None = None, kanal: str = "web",
            otazka: str | None = None) -> str:
    """Nová poznámka, nebo připsání do denního zápisu když není titulek.

    `kanal` (P14) jde jen do `inbox.kanal` — NE do samotného markdownu.
    Denní zápis je jeden soubor na den se sdílenými `## HH:MM` bloky, takže
    zápis od telegramu ráno a od webu večer skončí ve STEJNÉM dokumentu;
    frontmatter je vlastnost dokumentu, ne jednotlivého zápisu, takže by
    tam jedna hodnota kanálu lhala o druhém zápisu. `inbox` řádek je oproti
    tomu vždycky jeden na `capture()`, takže kanál sedí přesně.

    `otazka` (P15) je naopak otázka dne, na kterou tenhle zápis odpovídá,
    a ta DO MARKDOWNU PATŘÍ — na rozdíl od kanálu je vlastností právě toho
    jednoho bloku, ne dokumentu. Píše se do TĚLA bloku, ne do nadpisu; proč,
    viz komentář u zápisu níž. Do 2026-09-09 se neukládala
    nikde: `DENNI_OTAZKY` má desítky variant vybíraných náhodně
    a do deníku padla jen odpověď, takže „sentiment odpovědí na otázky dne"
    šel zodpovědět jen v souhrnu a odpověď se nedala spárovat s otázkou.
    Zpětně to dohnat nelze, proto to jde do markdownu, který je
    autoritativní zdroj — a tím i do indexu a do promptu.

    U poznámky S TITULKEM se `otazka` ignoruje: pojmenovaná poznámka není
    odpověď na otázku dne.
    """
    if title:
        rel = f"{date.today().isoformat()}-{slug(title)}.md"
        p = safe_path(rel)
        if p.exists():                      # nikdy nepřepisuj mlčky
            rel = f"{date.today().isoformat()}-{slug(title)}-{int(time.time())}.md"
            p = safe_path(rel)
        p.write_text(f"# {title}\n\n{text.strip()}\n", encoding="utf-8")
    else:
        rel = f"{config.DENIK_DIR}/{date.today().isoformat()}.md"
        p = safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M")
        # Otázka jde do TĚLA bloku, ne do nadpisu `## HH:MM`, a to je vědomé
        # rozhodnutí: `heading_path` (chunker ho skládá z ATX nadpisů) váží
        # reranker a zobrazuje se DOSLOVA v citacích. Šest generických otázek
        # ze `DENNI_OTAZKY` opakovaných napříč všemi dny by ho zředilo o text,
        # který o obsahu zápisu nic neříká, a z citace „2026-09-08 > 18:15"
        # by udělalo stodvacetiznakový řádek.
        #
        # V těle je otázka pořád v obsahu chunku, takže ji model při odpovědi
        # vidí — a to je všechno, co P15 potřebuje. Navíc se to lidsky lepší
        # čte v repozitáři poznámek.
        #
        # `" ".join(split())` slije odřádkování do mezer, aby řádek s otázkou
        # zůstal jeden odstavec.
        blok = f"## {stamp}\n\n"
        if otazka:
            blok += "*Otázka dne: %s*\n\n" % " ".join(otazka.split())
        blok += text.strip() + "\n"
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8").rstrip() + f"\n\n{blok}",
                         encoding="utf-8")
        else:
            p.write_text(f"# {date.today().isoformat()}\n\n{blok}",
                         encoding="utf-8")
    db.add_inbox(rel, text, kanal)
    trigger_reindex()
    return rel
