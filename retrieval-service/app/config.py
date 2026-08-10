"""Konfigurace z prostředí. Všechno pochází z retrieval.container."""
import os

from . import lang as _lang

DATABASE_URL = os.environ["DATABASE_URL"]

EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://infinity:7997")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3")

# Retrieval fáze: kandidáti z RRF -> rerank -> výsledek.
RRF_CANDIDATES = int(os.environ.get("RRF_CANDIDATES", "60"))
RERANK_TOP_K = int(os.environ.get("RERANK_TOP_K", "20"))
RESULT_LIMIT = int(os.environ.get("RESULT_LIMIT", "8"))

MARKDOWN_ROOT = os.environ.get("MARKDOWN_ROOT", "/data/markdown")

# Jazyk, když ho nikdo neurčí: dokument bez `lang:` ve frontmatteru, dotaz bez
# `lang` v requestu a bez detekce z rewrite. Musí být z lang.LANGS, jinak by
# INSERT spadl na CHECK constraintu document_lang_ck.
DEFAULT_LANG = _lang.normalize(os.environ.get("DEFAULT_LANG"), "cs")

# Rozměr musí odpovídat sloupci chunk.embedding halfvec(1024) a modelu bge-m3.
# Neshoda se projeví až chybou při INSERT, proto se kontroluje i za běhu.
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))

# Chunkování. Změřeno na nasazeném Infinity: chunk ~2 kB stojí 1,7-2,1 s,
# krátký text 0,10-0,15 s, a dávkování NEPOMÁHÁ (dávka 5 dlouhých = 5x jeden).
# Menší chunky tedy znamenají lineárně méně práce na dokument a zároveň
# ostřejší zásah pro reranker. 1200 znaků je kompromis.
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "1200"))
CHUNK_MIN_CHARS = int(os.environ.get("CHUNK_MIN_CHARS", "80"))

# Dávka pro /embeddings. Infinity běží s --batch-size 8; větší dávka jen
# zvětší HTTP payload, výpočet nezrychlí.
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "8"))

# 0 = vlastní poznámka, 1 = importované, 2 = automatický sync z venku.
TRUST_LEVEL = int(os.environ.get("TRUST_LEVEL", "0"))

HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "300"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

# Přepis dotazu na klíčová slova přes LiteLLM alias `cheap`.
# Návrh to zamýšlel — viz příklad virtual key v litellm-config.yaml.
# Měřeno: `cheap` má medián ~3,6 s, takže to latenci dotazu znatelně zvedá.
# Při jakékoliv chybě se použije deterministická extrakce z keywords.py.
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
REWRITE_MODEL = os.environ.get("REWRITE_MODEL", "cheap")
REWRITE_ENABLED = os.environ.get("REWRITE_ENABLED", "1") not in ("0", "false", "no")
# Měřeno na nasazeném systému: ustálené volání `cheap` trvá 1,1-4,8 s, ale
# PRVNÍ po restartu 8,5 s (navazuje se spojení na upstream). Původních 8 s
# tedy spolehlivě uřízlo právě ten cold start.
#
# Zvýšení skoro nic nestojí, protože timeout se platí taky: při 8 s zaplatíš
# 8 s A dostaneš horší klíčová slova z lokální extrakce, kdežto počkat do 8,5 s
# dá lepší výsledek za srovnatelnou cenu. Rozdíl se projeví až u skutečně
# zaseknutého volání, a to je vzácné.
REWRITE_TIMEOUT = float(os.environ.get("REWRITE_TIMEOUT", "12"))

# Detekce jazyka dokumentu při indexaci — týmž aliasem `cheap`. Týká se JEN
# dokumentů bez `lang:` ve frontmatteru; explicitní deklarace se nikdy
# nepřehlasuje a nestojí volání.
#
# Cena: jedno volání (~2-3 s) na dokument, a to jen u NEW a CHANGED.
# UNCHANGED se přeskakuje dřív, takže ustálený inkrementální běh nedetekuje
# nic. Při prvním naplnění velkého korpusu bez frontmatteru to ale je znatelná
# položka vedle embeddingů — proto vypínač.
DETECT_LANG_ENABLED = os.environ.get("DETECT_LANG_ENABLED", "1") not in ("0", "false", "no")
# Na určení jazyka stačí pár vět; posílat celý dokument je plýtvání tokeny.
DETECT_LANG_CHARS = int(os.environ.get("DETECT_LANG_CHARS", "1200"))

# Context window expansion (app/expand.py). K finálním výsledkům hledání
# dotáhne sousední chunky (ordinal ± EXPAND_WINDOW ze stejného dokumentu),
# protože chunkování je bez overlapu a hranice chunku je otázka rozpočtu
# CHUNK_CHARS, ne významu — odpověď se na ní umí rozseknout uprostřed
# myšlenky. Aplikuje se AŽ na finální (přerankované, oříznuté) výsledky,
# takže rerank dál hodnotí atomické chunky, na které je změřený.
#
# Výchozí okno 1 je konzervativní start: přidá nejvýš 2 sousední chunky
# na hit (~2× CHUNK_CHARS navíc), což zvedá jen délku promptu pro
# ANSWER_MODEL, ne cenu reranku (ten uz probehl na kratsim textu).
EXPAND_ENABLED = os.environ.get("EXPAND_ENABLED", "1") not in ("0", "false", "no")
EXPAND_WINDOW = int(os.environ.get("EXPAND_WINDOW", "1"))
