"""Analytika nad korpusem (P1b): otázka -> SQL -> číslo.

Existuje kvůli měřenému selhání: na dotaz „kolik je kterých knih" model
spočítal knihy zmíněné uvnitř osmi dodaných úryvků a vydal to za obsah
databáze. Agregaci nad tisícem dokumentů z osmi úryvků složit nejde —
musí se spočítat.

**Bezpečnost stojí na roli, ne na kontrole řetězce.** Připojuje se jako
`platform_ro`, která má na schéma `retrieval` jen SELECT. Kontrola SQL níž
je druhá vrstva pro zjevné nesmysly a pro to, aby chyba přišla dřív
a srozumitelněji; kdyby ji někdo obešel, databáze pořád nic zapsat nedovolí.

Modelem psané SQL může být syntakticky správně a sémanticky mimo, proto se
výsledek do `/korpus` nezapisuje sám — připíná ho člověk kliknutím. Ukládá
se k němu otisk korpusu, aby šlo poznat, že číslo mezitím zestaralo.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from psycopg_pool import ConnectionPool

from . import config

log = logging.getLogger("kryton")

_pool: ConnectionPool | None = None

# Povolen jediný SELECT/WITH. Středník uvnitř by pustil druhý příkaz.
_SELECT_ONLY = re.compile(r"\A\s*(select|with)\b", re.I)
_ZAKAZANE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|"
    r"vacuum|reindex|call|do)\b", re.I)


def enabled() -> bool:
    return bool(config.ANALYTICS_DATABASE_URL)


def init() -> None:
    global _pool
    if not enabled() or _pool is not None:
        return
    _pool = ConnectionPool(config.ANALYTICS_DATABASE_URL, min_size=0, max_size=2,
                           kwargs={"autocommit": False}, open=True)
    log.info("analytika zapnuta (role platform_ro)")


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


class AnalyticsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Schéma a otisk korpusu
# ---------------------------------------------------------------------------

def schema_description() -> str:
    """Tabulky a sloupce schématu `retrieval` pro prompt.

    Introspekce, ne natvrdo psaný seznam — schéma se mění (sloupec `lang`
    přidala až migrace 03-multilang.sql) a zastaralý popis by model tiše
    sváděl k dotazům na neexistující sloupce.
    """
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = 'retrieval' "
            "ORDER BY table_name, ordinal_position").fetchall()
        conn.rollback()
    tables: dict[str, list[str]] = {}
    for t, c, typ in rows:
        tables.setdefault(t, []).append(f"{c} {typ}")
    return "\n".join("retrieval.%s(%s)" % (t, ", ".join(cols))
                     for t, cols in tables.items())


def fingerprint() -> str:
    """Otisk stavu korpusu. Změní se při každé indexaci, která něco udělala.

    Bere se z databáze, ne z `/stats`, aby otisk nezávisel na tom, jestli
    retrieval zrovna odpovídá.
    """
    with _pool.connection() as conn:
        n, ts = conn.execute(
            "SELECT count(*), coalesce(max(updated_at)::text, '-') "
            "FROM retrieval.document").fetchone()
        conn.rollback()
    return f"{n}:{ts}"


# ---------------------------------------------------------------------------
# Spuštění SQL
# ---------------------------------------------------------------------------

def check_sql(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise AnalyticsError("model nevrátil žádné SQL")
    if ";" in sql:
        raise AnalyticsError("víc příkazů v jednom dotazu")
    if not _SELECT_ONLY.match(sql):
        raise AnalyticsError("dotaz nezačíná SELECT ani WITH")
    zakazane = _ZAKAZANE.search(sql)
    if zakazane:
        raise AnalyticsError("zakázané klíčové slovo %r" % zakazane.group(0))
    return sql


def run_sql(sql: str) -> tuple[list[str], list[list]]:
    """Spustí ověřený SELECT a vrátí (sloupce, řádky).

    Obalení do poddotazu vynutí strop řádků i na dotazu, který si žádný
    LIMIT nedal — jinak by `SELECT * FROM chunk` poslal do prohlížeče
    sto tisíc řádků.
    """
    sql = check_sql(sql)
    with _pool.connection() as conn:
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = %s",
                         (config.ANALYTICS_SQL_TIMEOUT_MS,))
            cur = conn.execute("SELECT * FROM (%s) AS _q LIMIT %s"
                               % (sql, config.ANALYTICS_MAX_ROWS))
            cols = [d.name for d in (cur.description or [])]
            rows = [[_scalar(v) for v in r] for r in cur.fetchall()]
            return cols, rows
        finally:
            conn.rollback()


def _scalar(v):
    """Do JSONu i do šablony musí jít něco jednoduchého."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# Otázka -> SQL
# ---------------------------------------------------------------------------

SQL_PROMPT = (
    "Jsi SQL analytik nad PostgreSQL databází osobních poznámek.\n"
    "Napiš JEDEN dotaz SELECT, který odpoví na otázku uživatele.\n"
    "Pravidla:\n"
    "- výhradně SELECT nebo WITH, nikdy nic, co zapisuje;\n"
    "- používej jen tabulky a sloupce ze schématu níž, nic si nevymýšlej;\n"
    "- tabulky piš plně kvalifikovaně, tedy `retrieval.document`;\n"
    "- pojmenuj vypočtené sloupce srozumitelně česky přes AS;\n"
    "- když se na otázku ze schématu odpovědět NEDÁ, vrať prázdné sql.\n"
    "Odpověz JEDINÝM řádkem JSON, nic jiného:\n"
    '{"sql":"<dotaz>","vysvetleni":"<jedna věta česky>"}\n\n'
    "Schéma:\n%s\n\nOtázka: %s"
)

_JSON = re.compile(r"\{.*\}", re.S)


def generate_sql(question: str) -> tuple[str, str]:
    """Vrátí (sql, vysvětlení). Prázdné sql = na otázku se odpovědět nedá."""
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.ANALYTICS_MODEL,
              "max_tokens": config.ANSWER_MAX_TOKENS,
              "messages": [{"role": "user",
                            "content": SQL_PROMPT % (schema_description(), question)}]},
        timeout=config.ANSWER_TIMEOUT)
    if r.status_code != 200:
        raise AnalyticsError(f"LiteLLM {r.status_code}: {r.text[:160]}")
    out = (r.json()["choices"][0]["message"].get("content") or "").strip()
    m = _JSON.search(out)
    if not m:
        raise AnalyticsError("model nevrátil JSON: %r" % out[:120])
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise AnalyticsError("nečitelný JSON od modelu: %s" % e)
    return (d.get("sql") or "").strip(), (d.get("vysvetleni") or "").strip()


def answer_question(question: str) -> dict:
    """Celá cesta otázka -> SQL -> výsledek.

    Nikdy nevyhazuje ven: volající to pouští u každého agregačního dotazu
    a selhaná analytika nesmí shodit odpověď. Chyba se vrátí v `error`
    a odpověď pak vznikne bez ní, jen z úryvků a faktů.
    """
    out = {"sql": "", "explain": "", "cols": [], "rows": [], "error": None,
           "fingerprint": ""}
    try:
        sql, explain = generate_sql(question)
        out["explain"] = explain
        if not sql:
            out["error"] = "na tuhle otázku data v databázi neodpovídají"
            return out
        out["sql"] = check_sql(sql)
        out["cols"], out["rows"] = run_sql(out["sql"])
        out["fingerprint"] = fingerprint()
    except Exception as e:
        log.warning("analytika selhala u %r: %s", question[:60], e)
        out["error"] = str(e)[:300]
    return out


def as_context(res: dict) -> str:
    """Výsledek do promptu. Prázdný řetězec, když není co dodat."""
    if res.get("error") or not res.get("cols"):
        return ""
    hlavicka = " | ".join(res["cols"])
    radky = "\n".join(" | ".join("" if v is None else str(v) for v in r)
                      for r in res["rows"][:50])
    return ("Výsledek výpočtu nad databází (spočítáno právě teď):\n"
            "%s\n%s\n" % (hlavicka, radky))
