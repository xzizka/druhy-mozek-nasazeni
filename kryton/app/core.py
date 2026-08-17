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


def answer(query: str, hits: list[dict], prior: list[dict] | None = None,
           extra: str = "") -> tuple[str, str, int]:
    """Vrátí (odpověď, model, latence_ms).

    `prior` jsou předchozí zprávy konverzace. Pozor na hranici: slouží jen
    generování odpovědi. Vyhledávání dostává surový text dotazu, takže
    u doplňujícího dotazu bez podstatného jména („a proč?") se sice model
    zorientuje, ale chunky se dohledávají podle té krátké fráze.
    """
    ctx = "\n\n".join(
        f"[{i+1}] {h['source_path']}"
        + (f" — {h['heading_path']}" if h.get("heading_path") else "")
        + f"\n{h['content']}"
        for i, h in enumerate(hits))
    if not ctx and not extra:
        return ("V poznámkách jsem k tomu nic nenašel.", "", 0)

    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += history_messages(prior)
    facts = corpus_facts()
    uryvky = f"Úryvky z poznámek:\n\n{ctx}\n\n" if ctx else ""
    msgs.append({"role": "user",
                 "content": dnesni_datum()
                            + (f"{facts}\n" if facts else "")
                            + (f"{extra}\n" if extra else "")
                            + uryvky + f"Otázka: {query}"})

    t0 = time.time()
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.ANSWER_MODEL,
              "messages": msgs,
              "max_tokens": config.ANSWER_MAX_TOKENS},
        timeout=config.ANSWER_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(f"LiteLLM {r.status_code}: {r.text[:200]}")
    d = r.json()
    msg = d["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        # Reasoning model spotřeboval token budget na reasoning_content.
        # Změřeno u big-pickle: při nízkém max_tokens vrátí prázdný content
        # s finish_reason=length. Radši to řekni, než vrátit prázdno.
        fin = d["choices"][0].get("finish_reason")
        text = (f"(model nevrátil odpověď, finish_reason={fin} — "
                f"zvyš ANSWER_MAX_TOKENS)")
    return text, d.get("model", config.ANSWER_MODEL), ms


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


def capture(text: str, title: str | None = None) -> str:
    """Nová poznámka, nebo připsání do denního zápisu když není titulek."""
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
    db.add_inbox(rel, text)
    trigger_reindex()
    return rel
