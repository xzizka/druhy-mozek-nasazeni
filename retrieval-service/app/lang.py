"""Jazyk dokumentu -> fulltextová konfigurace Postgresu.

Mapa musí sedět na `sql/03-multilang.sql`: sloupec `document.lang` má CHECK
`IN ('cs','en','de','la')` a `chunk.ts_config` je `regconfig`. Kdyby se sem
dostal jazyk, který CHECK nezná, INSERT spadne — proto se všechno cizí
normalizuje na default a nikdy se nepředává dál syrové.

Konfigurace `latin` není vestavěná, vytváří ji ta migrace (COPY = simple
+ unaccent). Na instanci bez migrace by `'latin'::regconfig` selhalo.

Modul záměrně nic neimportuje — sahá do něj i `config`, takže jakýkoliv
import zpátky by udělal cyklus.
"""
from __future__ import annotations

TS_CONFIG = {
    "cs": "czech",
    "en": "english",
    "de": "german",
    "la": "latin",
}

LANGS = frozenset(TS_CONFIG)

# Co ještě přijmout na vstupu. Frontmatter píše člověk, takže `en-US`,
# `CS` nebo `deu` nejsou chyba, kterou má smysl trestat.
_ALIASES = {
    "cze": "cs", "ces": "cs", "czech": "cs", "cesky": "cs",
    "eng": "en", "english": "en",
    "ger": "de", "deu": "de", "german": "de", "deutsch": "de",
    "lat": "la", "latin": "la", "latina": "la",
}


def normalize(value: str | None, default: str | None = None) -> str | None:
    """Kód jazyka na kanonický tvar, jinak `default`.

    Bere `cs`, `CS`, `cs-CZ`, `cs_CZ` i `czech`. Nic jiného nedovoluje —
    tichá záměna za default je lepší než pád indexace na CHECK constraintu,
    ale volající, kterému na tom záleží (explicitní `lang` v requestu),
    si rozdíl pozná tím, že dostane zpátky `default`.
    """
    if not value:
        return default
    code = str(value).strip().lower().replace("_", "-").split("-", 1)[0]
    if code in LANGS:
        return code
    return _ALIASES.get(code, default)


def ts_config(lang: str | None) -> str:
    """Jazyk -> jméno regconfig. Nezná-li ho, vrací 'czech'."""
    return TS_CONFIG.get(lang or "", "czech")
