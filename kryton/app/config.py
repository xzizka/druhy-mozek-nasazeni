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

# Strop na výstupní tokeny pro `reasoning` — a tím i pro celý fallback
# řetěz za ním, protože hodnota z requestu má přednost před `max_tokens`
# u aliasu v litellm-config.yaml. Strop tedy určuje TENHLE řádek.
#
# HISTORIE, protože to číslo dvakrát změnilo smysl:
#
# 1) Původně stovky. Big-pickle je reasoning model a tokeny utrácí na
#    reasoning_content dřív, než začne psát; při max_tokens=300 vrátil
#    PRÁZDNÝ content s finish_reason=length. Proto tisíce.
# 2) 2026-08-09 zvýšeno 3000 -> 8000. Dotaz „udělej sumarizaci knih podle
#    jazyka, kolik je kterých" spotřeboval přesně 3000, tedy celý strop,
#    a vrátil prázdný content.
# 3) 2026-08-18 SNÍŽENO 8000 -> 2000, viz níž.
#
# SNÍŽENÍ (P8 bod b). Těch 8000 bylo šité na big-pickle, ale aplikovalo se
# i na fallbacky, které to nepotřebují — a byla to PŘÍMÁ PŘÍČINA incidentu
# 2026-08-17: po vyčerpání kvóty big-pickle dostala gemma 8000 tokenů
# volnosti, na které stavěná není, mlela 743 s, vyrobila 24 000 znaků
# a výsledek se uložil do cache. Do Telegramu pak přišlo šest zpráv za
# sebou. Oprava (c) — posílání deadlinu v těle requestu — škodu jen
# zmenšila; tenhle řádek ji ruší.
#
# Snížit šlo teprve po kroku 2, kdy `reasoning` přešel z big-pickle na
# `openai/gpt-oss-120b`, který má řádově menší apetit na uvažování.
# Změřeno 2026-08-18 skriptem `scripts/26-eval-answer-max-tokens.py`,
# 11 volání přes obě cesty, které tenhle strop používají:
#
#   odpověď nad úryvky (core.py)   6 dotazů, max 534 tokenů celkem
#   generování SQL (analytics.py)  5 dotazů, max 248 tokenů celkem
#   z toho na uvažování            median 178, maximum 304
#   finish_reason=length            0x  (všech 11 skončilo `stop`)
#
# Klíčové: i ta agregační otázka z bodu 2), kvůli které se zvyšovalo na
# 8000, spotřebovala jen 349 tokenů a dokončila se. Důvod pro 8000 tedy
# zmizel s big-picklem.
#
# 2000 je maximum 534 krát skoro čtyři. Rezerva je záměrná — měřená sada
# nikdy nepokryje všechny budoucí dotazy a tenhle strop je POSLEDNÍ
# ochrana proti zacyklení, ne cílová hodnota. Vyhovuje všem třem modelům
# v řetězu: gpt-oss-120b (534), gemma (běžně pod 500) i gpt-oss-20b
# (uvažování 241-368 v měření z 2026-08-18).
ANSWER_MAX_TOKENS = int(os.environ.get("ANSWER_MAX_TOKENS", "2000"))
ANSWER_TIMEOUT = float(os.environ.get("ANSWER_TIMEOUT", "180"))

# O kolik déle než `ANSWER_TIMEOUT` čeká HTTP klient. `ANSWER_TIMEOUT` se
# posílá i V TĚLE požadavku jako deadline pro LiteLLM, a klient musí být
# shovívavější, jinak se Kryton vzdá dřív, než mu LiteLLM stihne chybu
# ohlásit (stejná úvaha jako u long pollingu v telegram.py).
#
# Proč to vzniklo (P8, 2026-08-17): deadline se LiteLLM neposílal vůbec.
# Kryton se vzdal po 180 s, ale LiteLLM mlelo dál celkem 743 s, doběhlo na
# 8000 tokenů a výsledek si uložilo do cache — opakovaný dotaz ho pak vrátil
# obratem a do Telegramu přišlo 24 000 znaků nesmyslu v šesti zprávách.
ANSWER_TIMEOUT_MARGIN = float(os.environ.get("ANSWER_TIMEOUT_MARGIN", "15"))

# Kolik chunků poslat modelu jako kontext. Retrieval vrací RESULT_LIMIT=8.
CONTEXT_CHUNKS = int(os.environ.get("CONTEXT_CHUNKS", "8"))

# Minimální `rerank_score`, aby chunk šel modelu jako kontext (P4 + P7-B).
# 0 nebo méně = vypnuto, chová se jako dřív.
#
# Změřeno 2026-08-17 (`scripts/21-eval-rerank-prah.py`, 16 dotazů, 84 skóre):
#   trefy, kde retrieval našel správný dokument: 0,332 – 0,998
#   šum (temporální, agregační, neexistující):   0,000017 – 0,021
# Mezi 0,021 a 0,332 neleží nic, prahy 0,05–0,3 dávaly shodně 0 chybných
# řezů. Vybráno 0,1: geometrický střed mezery (√(0,021 × 0,332) ≈ 0,084),
# a hlavně utne i jediný změřený případ odpovědi ze ŠPATNÉHO zdroje
# (německý dokument na českou otázku, 0,0507), který by 0,05 propustil.
#
# PROVIZORNÍ ČÍSLO: korpus měl při měření 13 dokumentů a 19 chunků, takže
# střední pásmo 0,05–0,5 (zásahy slabé, ale ještě užitečné) v něm skoro
# nemá jak vzniknout — 2 skóre z 84. Po nárůstu korpusu pusť skript znovu.
ANSWER_MIN_RERANK = float(os.environ.get("ANSWER_MIN_RERANK", "0.1"))

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
# Nahrávání dokumentů (P2, etapa 1: md a txt; etapa 2: PDF; etapa 3: DOCX)
# ---------------------------------------------------------------------------
# Podadresář pod MARKDOWN_ROOT pro text vytažený z nahraných souborů.
# Patří do `.gitignore` repozitáře poznámek — stejný vzorec jako `_scale/`,
# takže se text nesynchronizuje na GitHub. Indexer ho vezme sám, protože
# `_scan()` prochází `root.rglob("*.md")` relativně ke kořeni.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "_uploads")
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))

# Strop na počet stránek PDF — ochrana proti skenům s desetitisíci stránkami.
# DOCX nemá v XML pojem "stránka" bez plného vyrenderování, jeho ochrana proti
# zip bombě je proto samostatná (_DOCX_MAX_UNCOMPRESSED v ingest.py).
UPLOAD_MAX_PAGES = int(os.environ.get("UPLOAD_MAX_PAGES", "500"))

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

# ---------------------------------------------------------------------------
# MCP server (/mcp) — core.search()+core.answer() a core.capture() vystavené
# externím agentům (OpenWork a dalším MCP klientům). Jiná autentizace než
# web UI: sdílený token v Authorization hlavičce, viz mcp_server.py.
# ---------------------------------------------------------------------------
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
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

# P8 bod (d): strop na délku JEDNÉ odeslané zprávy. Dřív `_send()` krájel
# text po 4000 znacích ve smyčce, takže zacyklená odpověď z 2026-08-17
# (~24 000 znaků) dorazila jako šest zpráv za sebou — mlčky, bez signálu,
# že je něco špatně.
#
# 1024 je volba uživatele (2026-08-19), ne odvozená z měření. Pro srovnání:
# skutečné odpovědi v tabulce `message` mají 130-595 znaků a strop
# ANSWER_MAX_TOKENS=2000 pustí řádově 6000. Práh tedy NENÍ jen pojistka
# proti anomálii — u delších legitimních odpovědí se zkrácení projeví taky.
# Proto `_send()` celou odpověď loguje: v Telegramu se nic nedohledá,
# `telegram.py` do DB nepíše.
#
# Telegram sám povoluje 4096 znaků na zprávu; tenhle strop je přísnější
# a s tím API limitem nesouvisí.
TELEGRAM_MAX_ZNAKU = int(os.environ.get("TELEGRAM_MAX_ZNAKU", "1024"))

# Krok 2: denní otázka. UTC, ne lokální čas — brain běží v UTC, jako
# všechno ostatní v tomhle projektu (viz kryton-backup.timer). 18 UTC =
# 20:00 letního času (CEST). POZOR: neni DST-aware, v zime (CET, UTC+1)
# se posune fakticky na 19:00 mistniho — snadno zmenitelne bez zasahu do kodu.
TELEGRAM_DAILY_QUESTION_HOUR_UTC = int(os.environ.get("TELEGRAM_DAILY_QUESTION_HOUR_UTC", "18"))

# Krok 3: přepis hlasovek (app/stt.py). Prázdný klíč = STT vypnuté, bot na
# hlasovku odpoví, že přepis zatím neumí, místo aby padal na chybějícím klíči.
#
# STT_BASE_URL/STT_MODEL, ne pevně zadrátovaný poskytovatel — stejná úvaha
# jako u S3_ENDPOINT/S3_PROFILE výše. Výchozí hodnoty cílí na OpenRouter,
# ne na LiteLLM ani přímo na Groq: LiteLLM samo transkripci neumí, jen by
# proxovalo k dalšímu poskytovateli (nový účet, nový secret). OpenRouter má
# od 2026-07-22 vlastní /audio/transcriptions se STEJNÝM klíčem jako chat
# (viz OPENROUTER_API_KEY v litellm-config.yaml) — secret STT_API_KEY se
# proto v quadletu mountuje ze stejného `openrouter_api_key`, žádný nový.
#
# POZOR: model `openai/whisper-1` i to, že OpenRouter vrací JSON (ne holý
# text jako Groq), zatím NENÍ ověřeno živě — první nasazení chce jedno
# ruční volání na skutečnou hlasovku, než se krok 3 označí za hotový.
STT_API_KEY = os.environ.get("STT_API_KEY", "")
STT_BASE_URL = os.environ.get("STT_BASE_URL",
                               "https://openrouter.ai/api/v1/audio/transcriptions")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-1")
# Prázdné = necháno na automatické detekci Whisperu. Výchozí "cs" dává smysl
# pro česky psaný druhý mozek, ale jde přepsat/vypnout přes env.
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "cs")
STT_TIMEOUT = float(os.environ.get("STT_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Google Keep (P13). Jednosměrně, jen čtení — viz app/keep.py.
# ---------------------------------------------------------------------------
# Prázdný token = sync vypnutý. Kryton se kvůli němu NIKDY neodmítne
# spustit: Keep je doplňkový zdroj, ne podmínka provozu, na rozdíl od
# AUTH_PASSWORD nebo SESSION_SECRET.
#
# KEEP_MASTER_TOKEN je gpsoauth master token, tedy PLNÝ PŘÍSTUP K ÚČTU,
# ne jen ke Keepu a ne heslo aplikace. Proto podman secret, nikdy
# Environment (to by ho vypsalo `podman inspect` i `env` v kontejneru).
KEEP_EMAIL = os.environ.get("KEEP_EMAIL", "")
KEEP_MASTER_TOKEN = os.environ.get("KEEP_MASTER_TOKEN", "")

# Podadresář pod MARKDOWN_ROOT. Na rozdíl od `_uploads/` a `_scale/` PATŘÍ
# do gitu: obsah Keepu nikde jinde než v Google cloudu není a poznámka
# smazaná týdenním úklidem musí zůstat dohledatelná v historii repozitáře.
KEEP_DIR = os.environ.get("KEEP_DIR", "keep")

# Archiv se neindexuje (volba uživatele 2026-08-20). Zarchivování poznámky
# je tím pádem z pohledu druhého mozku totéž co smazání — projeví se při
# nejbližším týdenním úklidu.
KEEP_INCLUDE_ARCHIVED = os.environ.get("KEEP_INCLUDE_ARCHIVED", "0") == "1"

# Natvrdo do frontmatteru, kde je hodnota autoritativní. Keepové poznámky
# bývají tři slova a autodetekce jazyka na takové délce je loterie —
# špatný odhad rozbije stemming a s ním celou lexikální větev hledání.
KEEP_LANG = os.environ.get("KEEP_LANG", "cs")

# 2 = automatický sync z venku (komentář u retrieval.document). Dnes je to
# JEN filtr `trust_level <= p_max_trust` v hybrid_search, kde max_trust je
# vždy 2 — na váhu v odpovědi to zatím nemá vliv, jen připravuje možnost
# keepové útržky odfiltrovat.
KEEP_TRUST = int(os.environ.get("KEEP_TRUST", "2"))

# Pojistka týdenního úklidu. Musí být překročené OBĚ meze zároveň: samotné
# procento je u malé sbírky k ničemu (u deseti poznámek je 20 % jedna
# poznámka), samotné absolutní číslo zase u velké.
KEEP_DELETE_MIN_ABS = int(os.environ.get("KEEP_DELETE_MIN_ABS", "5"))
KEEP_DELETE_MAX_PODIL = float(os.environ.get("KEEP_DELETE_MAX_PODIL", "0.2"))
