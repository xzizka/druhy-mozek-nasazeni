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

# ---------------------------------------------------------------------------
# Nahrávání dokumentů (P2, etapa 1: md a txt)
# ---------------------------------------------------------------------------
# Podadresář pod MARKDOWN_ROOT pro text vytažený z nahraných souborů.
# Patří do `.gitignore` repozitáře poznámek — stejný vzorec jako `_scale/`,
# takže se text nesynchronizuje na GitHub. Indexer ho vezme sám, protože
# `_scan()` prochází `root.rglob("*.md")` relativně ke kořeni.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "_uploads")
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))

# 0 = vlastní poznámka, 1 = importované, 2 = automatický sync z venku
# (komentář u retrieval.document). Filtr je `trust_level <= p_max_trust`,
# takže vyšší číslo = menší důvěra. Nahrané dokumenty jsou „importované".
UPLOAD_TRUST = int(os.environ.get("UPLOAD_TRUST", "1"))

# Profil úložiště. Jméno se ukládá ke každému nahranému souboru, aby po
# migraci na jiný endpoint šlo poznat, kde který originál leží.
S3_PROFILE = os.environ.get("S3_PROFILE", "backblaze")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
# Prázdné = odvodí se z endpointu (u Backblaze je region v hostiteli).
S3_REGION = os.environ.get("S3_REGION", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_KEY_PREFIX = os.environ.get("S3_KEY_PREFIX", "originals/")

# Zálohy DB (kryton, litellm) a šifrovaného balíku secrets. Prázdný bucket
# = stejný jako S3_BUCKET — oddělené jméno je jen kvůli budoucí možnosti
# přepnout zálohy na jiný bucket bez zásahu do kódu, ne proto, že by se
# to dělalo dnes. Prefix odděluje obor klíčů od `S3_KEY_PREFIX` (originály).
BACKUP_S3_BUCKET = os.environ.get("BACKUP_S3_BUCKET", "")
BACKUP_S3_PREFIX = os.environ.get("BACKUP_S3_PREFIX", "db-backups/")
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))

# Telegram můstek (krok 1: jen text — core.capture / core.search+core.answer).
# Prázdný token nebo nulové ID = vypnuto, žádné volání na Telegram API.
#
# TELEGRAM_ALLOWED_USER_ID je JEDINÁ autentizace kanálu — bez ní by bot
# odpovídal komukoliv, kdo ho na Telegramu najde. Kontroluje se na KAŽDÉ
# zprávě v telegram.py, ne jen jednou při startu.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))
# Jak dlouho drží Telegram spojení otevřené při long pollingu, než vrátí
# prázdnou odpověď. HTTP timeout na klientovi musí být delší (viz telegram.py).
TELEGRAM_POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30"))

# Krok 2: denní otázka. UTC, ne lokální čas — brain běží v UTC, jako
# všechno ostatní v tomhle projektu (viz kryton-backup.timer). 6 UTC =
# 8:00 letního času (CEST) — snadno změnitelné bez zásahu do kódu.
TELEGRAM_DAILY_QUESTION_HOUR_UTC = int(os.environ.get("TELEGRAM_DAILY_QUESTION_HOUR_UTC", "6"))
