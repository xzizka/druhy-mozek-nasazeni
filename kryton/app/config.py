"""Konfigurace Krytona. Vychází z kryton.container."""
import os

DATABASE_URL = os.environ["DATABASE_URL"]
RETRIEVAL_URL = os.environ.get("RETRIEVAL_URL", "http://retrieval:8080")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
MARKDOWN_ROOT = os.environ.get("MARKDOWN_ROOT", "/data/markdown")

# Alias, ne konkrétní model — výměna modelu je pak konfigurační změna
# v litellm-config.yaml, ne v kódu.
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "reasoning")

# reasoning je big-pickle, tedy reasoning model: tokeny utrácí na
# reasoning_content dřív, než začne psát odpověď. Změřeno, že při
# max_tokens=300 vrátil PRÁZDNÝ content s finish_reason=length.
# Proto tisíce, ne stovky.
#
# ZVÝŠENO 2026-08-09 z 3000 na 8000. Dotaz „udělej sumarizaci knih podle
# jazyka, kolik je kterých" spotřeboval ve spend logu přesně 3000 výstupních
# tokenů, tedy celý strop, a vrátil prázdný content. Pro srovnání: běžné
# dotazy na tomtéž korpusu utratily 202 a 468 tokenů. Agregační otázky nutí
# reasoning model uvažovat dlouho, protože odpověď z dodaných úryvků složit
# nejde — na to je stránka /korpus, která bere čísla z databáze.
#
# Hodnota z requestu má přednost před `max_tokens: 4000` u aliasu reasoning
# v litellm-config.yaml, takže strop určuje TENHLE řádek, ne LiteLLM.
ANSWER_MAX_TOKENS = int(os.environ.get("ANSWER_MAX_TOKENS", "8000"))
ANSWER_TIMEOUT = float(os.environ.get("ANSWER_TIMEOUT", "180"))

# Kolik chunků poslat modelu jako kontext. Retrieval vrací RESULT_LIMIT=8.
CONTEXT_CHUNKS = int(os.environ.get("CONTEXT_CHUNKS", "8"))

# Kolik předchozích zpráv konverzace přiložit k doplňujícímu dotazu.
# 6 = tři dvojice otázka/odpověď. Strop je tu proto, že odpovědi bývají
# dlouhé a bez něj by kontext rostl každým tahem, až by přerostl
# ANSWER_MAX_TOKENS i rozpočet klíče.
HISTORY_MESSAGES = int(os.environ.get("HISTORY_MESSAGES", "6"))
HISTORY_CHARS = int(os.environ.get("HISTORY_CHARS", "1500"))

# Heslo z podman secretu. Bez něj se aplikace odmítne spustit — port 3001
# je publikovaný na 0.0.0.0 a firewall pouští celý segment 10.20.0.0/24,
# takže běh bez autentizace by znamenal poznámky otevřené celému homelabu.
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "720"))

# Po zápisu poznámky zavolat retrieval /reindex. Inkrementální běh nad
# nezměněným korpusem je nula, takže je to zdarma — a bez toho je index
# zastaralý až do dalšího ručního reindexu.
REINDEX_AFTER_WRITE = os.environ.get("REINDEX_AFTER_WRITE", "1") not in ("0", "false", "no")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "3001"))

# ---------------------------------------------------------------------------
# Analytika (P1b): dotazy typu „kolik", na které se z osmi úryvků odpovědět
# nedá, se spočítají nad databází retrievalu.
#
# Jede se pod rolí `platform_ro`, která má na schéma `retrieval` jen SELECT
# (sql/01-bootstrap.sql, sql/02-retrieval.sql). To je ta podstatná pojistka —
# kontrola SQL v analytics.py je jen druhá vrstva, ne ta hlavní. Bez tohohle
# DSN se analytika prostě vypne a Kryton běží dál.
# ---------------------------------------------------------------------------
ANALYTICS_DATABASE_URL = os.environ.get("ANALYTICS_DATABASE_URL", "")

# SQL psát je těžší než tahat klíčová slova, takže default je `reasoning`,
# ne `cheap`. Neběží to v horké cestě — jen když se někdo zeptá na počty —
# takže pomalejší model tu nevadí. big-pickle navíc nemá denní strop.
ANALYTICS_MODEL = os.environ.get("ANALYTICS_MODEL", "reasoning")
ANALYTICS_SQL_TIMEOUT_MS = int(os.environ.get("ANALYTICS_SQL_TIMEOUT_MS", "10000"))
ANALYTICS_MAX_ROWS = int(os.environ.get("ANALYTICS_MAX_ROWS", "200"))
