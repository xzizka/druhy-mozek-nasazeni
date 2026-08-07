"""Klient LiteLLM aliasu `cheap`: přepis dotazu na klíčová slova a detekce jazyka.

Návrh to tak zamýšlel — `litellm-config.yaml` má na konci přímo příklad
virtual key pro retrieval-service s `"models":["cheap"]`. Věta v README
o tom, že retrieval vědomě nemá přístup na LiteLLM, se týká EMBEDDINGŮ
(bulk indexace by z gateway udělala bottleneck), ne přepisu dotazu.

DVĚ POUŽITÍ, JEDNA INFRASTRUKTURA:

  terms()           dotazová strana — klíčová slova + jazyk dotazu naráz
  detect_language() indexační strana — jazyk dokumentu bez `lang:`
                    ve frontmatteru

Jazyk se u přepisu veze v TÉMŽE volání jako klíčová slova, takže detekce na
dotazové straně stojí nula sekund navíc a cachuje se spolu s nimi.

CENA: měřeno, že `cheap` (gemini-2.5-flash přes OpenRouter) má medián ~3,6 s
a v dávkovém testu 1 volání ze 6 vrátilo 429. Přepis tedy latenci dotazu
znatelně zvyšuje. Proto:

  - krátký timeout, po něm se pokračuje bez LLM
  - JAKÁKOLIV chyba => deterministický fallback z keywords.py, nikdy
    ne selhání dotazu; `cheap` po dohodě nemá v LiteLLM fallback, takze
    429 se musi resit tady
  - cache, protože dotazy se opakují
  - vypínatelné per request (`rewrite: false`)

Na indexační straně platí totéž se stejným závěrem: nedetekovaný jazyk je
`DEFAULT_LANG`, ne chyba. Indexace se kvůli nedostupné gateway zastavit nesmí.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import NamedTuple

import httpx

from . import config, keywords
from . import lang as langs

log = logging.getLogger("rewrite")


class Terms(NamedTuple):
    """Výsledek přepisu. `lang` je None, když se jazyk nepodařilo určit."""
    text: str
    source: str                 # 'llm' | 'cache' | 'local'
    lang: str | None


# POZOR NA NORMALIZACI TVARŮ — změřeno, že přehnaná stojí lexikální větev.
#
# Dřívější znění promptu žádalo 1. pád jednotného čísla bez rozlišení jazyka.
# U češtiny je to správně: hunspell lemmatizuje dotaz i dokument, takže na tvaru
# nezáleží, a trigramová větev (nelemmatizovaná) na základním tvaru vydělá.
#
# U angličtiny to ale shodu ZABIJE. Snowball `faster` na `fast` neredukuje:
#
#   to_tsvector('english','databases running faster fast')
#       -> 'databas':1 'fast':4 'faster':3 'run':2
#
# Model vrátil `database run fast`, z toho vznikne 'databas' & 'run' & 'fast',
# dokument obsahuje 'faster' — a AND semantika vrátí nulu. Ověřeno: s tvary
# z textu (`databases running faster`) shoda projde.
#
# Pravidlo je tedy per jazyk: normalizovat jen češtinu, jinde nechat tvary
# z dotazu a spolehnout se na to, že stemmer jede na obou stranách stejně.
# U latiny, která nestemuje vůbec, je to nutnost.
REWRITE_PROMPT = (
    "Z dotazu vyber klíčová slova pro fulltextové hledání v poznámkách "
    "a urči jazyk dotazu.\n"
    "Pravidla pro klíčová slova: jen slova, která se pravděpodobně vyskytují "
    "v textu; zahoď tázací slova, předložky a spojky; NEPŘEKLÁDEJ, ponech je "
    "v jazyce dotazu; zachovej diakritiku; zachovej identifikátory a názvy "
    "přesně (např. maintenance_work_mem).\n"
    "Tvary slov: je-li dotaz česky, převeď je do 1. pádu jednotného čísla. "
    "V jiném jazyce je NECH přesně tak, jak jsou v dotazu — přehnaná "
    "normalizace shodu naopak zabije (anglické 'faster' se nesmí měnit "
    "na 'fast').\n"
    "Jazyk je jeden z: cs, en, de, la.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"lang":"<kód>","keywords":"<slova oddělená mezerou>"}\n\n'
    "Dotaz: "
)

DETECT_PROMPT = (
    "Urči jazyk následujícího textu. Možnosti: cs (čeština), en (angličtina), "
    "de (němčina), la (latina). Když to není přesně žádný z nich, vyber "
    "nejbližší.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"lang":"<kód>"}\n\n'
    "Text:\n"
)

# Model rád obalí JSON do code fence nebo přidá větu okolo. Bere se první
# `{` až poslední `}` — na jednořádkovou odpověď to stačí a je to odolnější
# než trvat na čistém JSONu.
_JSON = re.compile(r"\{.*\}", re.S)

_cache: dict[str, tuple[str, str | None]] = {}
_lock = threading.Lock()
_CACHE_MAX = 512


def _cached(q: str) -> tuple[str, str | None] | None:
    with _lock:
        return _cache.get(q)


def _store(q: str, terms_text: str, detected: str | None) -> None:
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()          # jednoduché a dost dobré; není to hot path
        _cache[q] = (terms_text, detected)


def _call(prompt: str, max_tokens: int) -> str:
    """Jedno volání na `cheap`. Při čemkoliv nestandardním vyhodí výjimku."""
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.REWRITE_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": 0},
        timeout=config.REWRITE_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:120]}")
    out = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not out:
        raise RuntimeError("prazdna odpoved")
    return out


def _json_field(out: str, key: str) -> str | None:
    """Vytáhne skalární klíč z JSON odpovědi. Nepovede-li se, None."""
    m = _JSON.search(out)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return str(value).strip() if value is not None else None


def _clean_terms(raw: str) -> str:
    """Uvozovky, odrážky a víc řádků pryč. Prázdno nebo román = selhání."""
    out = raw.splitlines()[0].strip().strip('"\'` ').lstrip("-•* ") if raw else ""
    if not out or len(out) > 300:
        raise RuntimeError(f"nepouzitelna odpoved: {out[:80]!r}")
    return out


def terms(query: str, use_llm: bool | None = None,
          fallback_lang: str | None = None) -> Terms:
    """Klíčová slova pro p_query a jazyk dotazu.

    `fallback_lang` vybírá stopword list pro lokální extrakci, když se na LLM
    nejde nebo selže. Je to jen fallback — vrácený `lang` zůstane None, aby
    volající poznal, že se nic nedetekovalo, a mohl použít vlastní default.
    """
    fb = fallback_lang or config.DEFAULT_LANG
    enabled = config.REWRITE_ENABLED if use_llm is None else use_llm
    if not enabled or not config.LITELLM_API_KEY:
        return Terms(keywords.extract(query, fb), "local", None)

    hit = _cached(query)
    if hit is not None:
        return Terms(hit[0], "cache", hit[1])

    try:
        out = _call(REWRITE_PROMPT + query, 120)
        raw = _json_field(out, "keywords")
        detected = langs.normalize(_json_field(out, "lang"))
        # Když model formát nedodržel, ber celou odpověď jako klíčová slova.
        # Zhoršený výsledek je pořád lepší než ztracený dotaz.
        cleaned = _clean_terms(raw if raw is not None else out)
        # BEZPEČNOSTNÍ SÍŤ: prožeň výstup ještě lokálním stopword filtrem.
        #
        # Prompt sice říká „zahoď tázací slova", ale model to nedodrží vždy —
        # změřeno na latinském dotazu „Ubi manet amicorum memoria?", kde vrátil
        # `ubi manet amicorum memoria`. Slovo `ubi` v dokumentu není a AND
        # semantika kvůli němu shodila celou lexikální větev, přestože zbylá
        # tři slova sedí. Jedno funkční slovo navíc = mlčící větev.
        #
        # Filtruje se JEN když je jazyk známý; `extract` na neznámý jazyk
        # nefiltruje nic a nikdy nevrátí prázdno, takže to nemůže uškodit.
        if detected:
            cleaned = keywords.extract(cleaned, detected)
        _store(query, cleaned, detected)
        return Terms(cleaned, "llm", detected)
    except Exception as e:
        log.warning("prepis pres %s selhal (%s), pouzivam lokalni extrakci",
                    config.REWRITE_MODEL, e)
        return Terms(keywords.extract(query, fb), "local", None)


def detect_language(sample: str) -> str | None:
    """Jazyk textu přes `cheap`. None = neurčeno, volající vezme svůj default.

    Volá indexer u dokumentů, které nemají `lang:` ve frontmatteru. Posílá se
    jen začátek textu — na určení jazyka stačí pár vět a celý dokument by byl
    plýtvání tokeny i časem.

    Vrací výhradně kódy z `lang.LANGS`. Cokoliv jiného je None, protože do
    `document.lang` se to stejně nesmí dostat: CHECK constraint by INSERT
    shodil a celý dokument by se neindexoval.
    """
    if not config.DETECT_LANG_ENABLED or not config.LITELLM_API_KEY:
        return None
    text = sample.strip()
    if not text:
        return None

    try:
        out = _call(DETECT_PROMPT + text[:config.DETECT_LANG_CHARS], 20)
    except Exception as e:
        log.warning("detekce jazyka pres %s selhala (%s)", config.REWRITE_MODEL, e)
        return None

    detected = langs.normalize(_json_field(out, "lang"))
    if detected is None:
        # Model mohl odpovědět holým kódem místo JSONu — to je použitelné.
        first = out.strip().strip('"\'`.').split()
        detected = langs.normalize(first[0] if first else None)
    if detected is None:
        log.warning("detekce jazyka vratila nepouzitelnou odpoved: %r", out[:80])
    return detected
