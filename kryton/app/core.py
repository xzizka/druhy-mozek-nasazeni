"""Klienti, autentizace a zápis poznámek."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import unicodedata
from datetime import date
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
           odmitnuto: bool, fallback: bool) -> dict:
    return {"n_kandidatu": kandidatu, "n_nad_prahem": nad_prahem,
            "max_rerank": max_rerank, "odmitnuto": odmitnuto,
            "fallback": fallback}


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
           extra: str = "") -> Odpoved:
    """Vrátí `Odpoved(text, model, latence_ms, stopa)`.

    `prior` jsou předchozí zprávy konverzace. Pozor na hranici: slouží jen
    generování odpovědi. Vyhledávání dostává surový text dotazu, takže
    u doplňujícího dotazu bez podstatného jména („a proč?") se sice model
    zorientuje, ale chunky se dohledávají podle té krátké fráze.
    """
    kandidatu = len(hits)
    # Maximum se bere PŘED filtrem, a to je podstatné: po filtru je u P7-B
    # seznam prázdný, takže by se do stopy uložilo None a z reportu by
    # nešlo poznat rozdíl mezi „reranker nic nenabídl" a „nabídl 0,0215,
    # což je o řád pod prahem 0,1". Právě tenhle rozdíl je celá diagnóza.
    nejlepsi = max((float(h["rerank_score"]) for h in hits
                    if h.get("rerank_score") is not None), default=None)
    hits = dost_relevantni(hits)
    ctx = "\n\n".join(
        f"[{i+1}] {h['source_path']}"
        + (f" — {h['heading_path']}" if h.get("heading_path") else "")
        + f"\n{h['content']}"
        for i, h in enumerate(hits))
    if not ctx and not extra:
        return Odpoved("V poznámkách jsem k tomu nic nenašel.", "", 0,
                       _stopa(kandidatu, 0, nejlepsi, True, False))

    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += history_messages(prior)
    facts = corpus_facts() + kanal_facts()
    uryvky = f"Úryvky z poznámek:\n\n{ctx}\n\n" if ctx else ""
    msgs.append({"role": "user",
                 "content": dnesni_datum()
                            + (f"{facts}\n" if facts else "")
                            + (f"{extra}\n" if extra else "")
                            + uryvky + f"Otázka: {query}"})

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
    t0 = time.time()
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.ANSWER_MODEL,
              "messages": msgs,
              "max_tokens": config.ANSWER_MAX_TOKENS,
              "timeout": config.ANSWER_TIMEOUT},
        timeout=config.ANSWER_TIMEOUT + config.ANSWER_TIMEOUT_MARGIN)
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(f"LiteLLM {r.status_code}: {r.text[:200]}")
    d = r.json()
    propadl = zkontroluj_fallback(config.ANSWER_MODEL, d.get("model") or "")
    msg = d["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        # Reasoning model spotřeboval token budget na reasoning_content.
        # Změřeno u big-pickle: při nízkém max_tokens vrátí prázdný content
        # s finish_reason=length. Radši to řekni, než vrátit prázdno.
        fin = d["choices"][0].get("finish_reason")
        text = (f"(model nevrátil odpověď, finish_reason={fin} — "
                f"zvyš ANSWER_MAX_TOKENS)")
    return Odpoved(text, d.get("model", config.ANSWER_MODEL), ms,
                   _stopa(kandidatu, len(hits), nejlepsi, False, propadl))


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


def capture(text: str, title: str | None = None, kanal: str = "web") -> str:
    """Nová poznámka, nebo připsání do denního zápisu když není titulek.

    `kanal` (P14) jde jen do `inbox.kanal` — NE do samotného markdownu.
    Denní zápis je jeden soubor na den se sdílenými `## HH:MM` bloky, takže
    zápis od telegramu ráno a od webu večer skončí ve STEJNÉM dokumentu;
    frontmatter je vlastnost dokumentu, ne jednotlivého zápisu, takže by
    tam jedna hodnota kanálu lhala o druhém zápisu. `inbox` řádek je oproti
    tomu vždycky jeden na `capture()`, takže kanál sedí přesně.
    """
    if title:
        rel = f"{date.today().isoformat()}-{slug(title)}.md"
        p = safe_path(rel)
        if p.exists():                      # nikdy nepřepisuj mlčky
            rel = f"{date.today().isoformat()}-{slug(title)}-{int(time.time())}.md"
            p = safe_path(rel)
        p.write_text(f"# {title}\n\n{text.strip()}\n", encoding="utf-8")
    else:
        rel = f"denik/{date.today().isoformat()}.md"
        p = safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M")
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8").rstrip()
                         + f"\n\n## {stamp}\n\n{text.strip()}\n", encoding="utf-8")
        else:
            p.write_text(f"# {date.today().isoformat()}\n\n## {stamp}\n\n{text.strip()}\n",
                         encoding="utf-8")
    db.add_inbox(rel, text, kanal)
    trigger_reindex()
    return rel
