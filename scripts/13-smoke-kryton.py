#!/usr/bin/env python3
"""Smoke test Krytona: projde všechny routy s odstubovanou DB, retrievalem a LLM.

Nepotřebuje běžící postgres, retrieval ani LiteLLM — stubuje se `app.db`
a dvě funkce z `app.core`. Zápis poznámek se ale testuje doopravdy, proti
dočasnému adresáři, protože slug i ochrana cest jsou to, co může tiše ublížit.

Vzniklo poté, co se ukázalo, že Kryton byl celý napsaný, postavený do obrazu
a nasazený, ale ANI JEDNOU nespuštěný — a hned první běh odhalil, že se tělo
každé stránky escapuje podruhé a v prohlížeči by se zobrazil zdroják HTML.

    ./scripts/13-smoke-kryton.py                 # potřebuje Python 3.10+
    docker run --rm -i -v "$PWD/kryton:/srv/kryton:ro" python:3.13-slim sh -c \\
        'pip install -q -r /srv/kryton/requirements.txt; cat > /s.py; python /s.py' \\
        < scripts/13-smoke-kryton.py

Návratový kód 0 = vše prošlo, 1 = něco selhalo.
"""
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

# Zdrojáky Krytona jsou vedle scripts/, ale v kontejneru bývají jinde.
SRC = os.environ.get("KRYTON_SRC") or str(Path(__file__).resolve().parent.parent / "kryton")
if not Path(SRC, "app", "main.py").exists() and Path("/srv/kryton/app/main.py").exists():
    SRC = "/srv/kryton"

MD = tempfile.mkdtemp(prefix="kryton-smoke-")

os.environ["DATABASE_URL"] = "postgresql://stub/stub"
os.environ["AUTH_PASSWORD"] = "tajne-heslo"
os.environ["SESSION_SECRET"] = "session-secret-pro-test"
os.environ["MARKDOWN_ROOT"] = MD

sys.path.insert(0, SRC)

from app import core, db  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'CHYBA'} {name}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


# --- stub DB -----------------------------------------------------------
CID = "11111111-2222-3333-4444-555555555555"
_msgs = []
_inbox = [{"id": 1, "source_path": "denik/2026-08-09.md", "excerpt": "něco",
           "created_at": datetime.now()}]

db.init = lambda: None
db.close = lambda: None
db.ping = lambda: True
db.new_conversation = lambda title: CID
db.conversations = lambda limit=50: [
    {"id": CID, "title": "Testovací konverzace", "created_at": datetime.now(),
     "messages": len(_msgs)}]
# Kopie, ne živý seznam: skutečné db.messages() staví nový seznam z řádků,
# takže prior je snapshot. Bez copy() by test měřil vlastní stub.
db.messages = lambda cid: list(_msgs)
db.stats = lambda: {"conversations": 1, "messages": len(_msgs)}
db.set_feedback = lambda mid, rating, useful=None, note=None: _msgs and None
db.add_inbox = lambda p, e: None
db.inbox_open = lambda limit=100: _inbox
db.inbox_done = lambda i: None

SMAZANO = []


def _delete_conversation(cid):
    # Psycopg na nevalidní UUID vyhodí výjimku, ne prázdný výsledek —
    # stub to napodobuje, aby se testovala i chybová větev routy.
    if cid != CID:
        raise ValueError('invalid input syntax for type uuid: "%s"' % cid)
    SMAZANO.append(cid)
    return 1


db.delete_conversation = _delete_conversation


def _add_message(cid, role, content, citations=None, model=None, latency_ms=None):
    _msgs.append({"id": len(_msgs) + 1, "role": role, "content": content,
                  "citations": citations or [], "model": model,
                  "latency_ms": latency_ms, "created_at": datetime.now(),
                  "rating": None})
    return len(_msgs)


db.add_message = _add_message

# --- stub retrieval a LLM ----------------------------------------------
HIT = {"chunk_id": "c1", "document_id": "d1", "source_path": "poznamky/test.md",
       "heading_path": "Kapitola > Sekce", "content": "Obsah úryvku.",
       "rerank_score": 0.987}
SEEN = {}
core.search = lambda q, limit=None, rewrite=None, **kw: (
    SEEN.__setitem__("rewrite", rewrite), {"results": [HIT]})[1]


def _answer(q, hits, prior=None, extra=""):
    SEEN["prior"] = prior
    SEEN["extra"] = extra
    return ("Odpověď s citací [1].", "reasoning", 1234)


core.answer = _answer
core.trigger_reindex = lambda: None

# Tvar odpovídá skutečnému /stats retrievalu (ověřeno 2026-08-09).
STATS = {
    "documents": 979, "chunks": 114183, "chunks_without_embedding": 0,
    "documents_unfinished": 0, "hnsw_index_present": True,
    "documents_by_lang": {"cs": 400, "de": 177, "en": 301, "la": 101},
    "chunks_by_ts_config": {"czech": 46362, "english": 31852,
                            "german": 24824, "latin": 11145},
    "indexer": {"running": False, "last_result": {
        "new": 0, "changed": 0, "unchanged": 979, "deleted": 0,
        "chunks_embedded": 0, "chunks_recycled": 0, "seconds": 1.3}},
}
core.corpus_stats = lambda: STATS

# --- stub analytiky (P1b) ----------------------------------------------
from app import analytics  # noqa: E402

FP = "979:2026-08-09 05:11:10+00"
AN = {"sql": "SELECT lang, count(*) AS pocet FROM retrieval.document GROUP BY lang",
      "explain": "Počty dokumentů podle jazyka.", "cols": ["lang", "pocet"],
      "rows": [["cs", 400], ["en", 301]], "error": None, "fingerprint": FP}

analytics.init = lambda: None
analytics.close = lambda: None
analytics.enabled = lambda: True
analytics.fingerprint = lambda: FP
analytics.answer_question = lambda q: dict(AN)

_metrics = []


def _add_metric(cid, q, sql, cols, rows, fp):
    _metrics.append({"id": len(_metrics) + 1, "conversation_id": cid,
                     "question": q, "label": None, "sql": sql, "cols": cols,
                     "rows": rows, "fingerprint": fp,
                     "computed_at": datetime.now(), "pinned_at": None})
    return len(_metrics)


def _pin_metric(mid, label=None):
    for m in _metrics:
        if m["id"] == mid:
            m["pinned_at"] = datetime.now()
            m["label"] = label or m["label"]


def _delete_metric(mid):
    before = len(_metrics)
    _metrics[:] = [m for m in _metrics if m["id"] != mid]
    return before - len(_metrics)


db.add_metric = _add_metric
db.pin_metric = _pin_metric
db.delete_metric = _delete_metric
db.metrics_for_conversation = lambda cid: [
    m for m in _metrics if m["conversation_id"] == cid and not m["pinned_at"]]
db.pinned_metrics = lambda: [dict(m) for m in _metrics if m["pinned_at"]]

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

c = TestClient(main.app)

print("== bez přihlášení ==")
for path in ["/", "/historie", "/inbox", "/zachytit", "/korpus"]:
    r = c.get(path, follow_redirects=False)
    check(f"GET {path} přesměruje na přihlášení",
          r.status_code == 303 and r.headers.get("location") == "/prihlasit",
          f"{r.status_code} {r.headers.get('location')}")

r = c.get("/healthz")
check("GET /healthz", r.status_code == 200 and r.json()["status"] == "ok", r.text[:80])

print("== přihlášení ==")
r = c.post("/prihlasit", data={"password": "spatne"}, follow_redirects=False)
check("špatné heslo nevydá cookie",
      r.status_code == 200 and "kryton_session" not in r.cookies, str(r.status_code))

r = c.post("/prihlasit", data={"password": "tajne-heslo"}, follow_redirects=False)
check("správné heslo přesměruje a nastaví cookie",
      r.status_code == 303 and "kryton_session" in r.cookies, str(r.status_code))

print("== přihlášený provoz ==")
r = c.get("/")
check("GET / se vyrenderuje", r.status_code == 200 and "Kryton" in r.text, str(r.status_code))
# Regrese: tělo stránky se dřív escapovalo podruhé a stránka se zobrazovala
# jako vlastní zdroják. Formulář musí být ZNAČKA, ne text.
check("tělo stránky je HTML, ne escapovaný text",
      "<textarea" in r.text and "&lt;textarea" not in r.text)

r = c.post("/dotaz", data={"query": "Proč záleží na pořadí slovníků?", "rewrite": "1"},
           follow_redirects=False)
check("POST /dotaz přesměruje do konverzace",
      r.status_code == 303 and "/konverzace/" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")

check("nová konverzace jde bez historie", SEEN["prior"] == [], repr(SEEN["prior"]))
check("zaškrtnutý přepis dojde do retrievalu", SEEN["rewrite"] is True, repr(SEEN["rewrite"]))

r = c.get(f"/konverzace/{CID}")
ok = r.status_code == 200 and "Odpověď s citací" in r.text and "test.md" in r.text
check("konverzace ukáže odpověď i citace", ok, str(r.status_code))
check("rerank skóre se vyrenderuje", "0.987" in r.text or "0,987" in r.text)

print("== doplňující dotaz ==")
r = c.post("/dotaz", data={"query": "A proč?", "conversation_id": CID, "rewrite": "1"},
           follow_redirects=False)
check("follow-up přesměruje", r.status_code == 303, str(r.status_code))
prior = SEEN["prior"]
check("follow-up dostal historii", bool(prior) and len(prior) == 2, repr(len(prior or [])))
check("historie neobsahuje právě položenou otázku",
      all(m["content"] != "A proč?" for m in prior))
check("historie je ve správném pořadí",
      [m["role"] for m in prior] == ["user", "assistant"], str([m["role"] for m in prior]))

msgs = core.history_messages(prior)
check("history_messages dá čistý chat formát",
      all(set(m) == {"role", "content"} for m in msgs) and len(msgs) == 2, repr(msgs[:1]))
long = [{"role": "user", "content": "x" * 9000}] * 20
out = core.history_messages(long)
check("historie je omezená počtem", len(out) == core.config.HISTORY_MESSAGES, str(len(out)))
check("historie je omezená délkou",
      all(len(m["content"]) <= core.config.HISTORY_CHARS + 6 for m in out),
      str(max(len(m["content"]) for m in out)))
check("prázdné zprávy se z historie vypustí",
      core.history_messages([{"role": "user", "content": "   "}]) == [])

r = c.post("/hodnoceni", data={"message_id": 2, "rating": "1", "conversation_id": CID},
           follow_redirects=False)
check("POST /hodnoceni", r.status_code == 303, str(r.status_code))

r = c.get("/historie")
check("GET /historie", r.status_code == 200 and "Testovací konverzace" in r.text)

r = c.get("/inbox")
check("GET /inbox", r.status_code == 200 and "denik/2026-08-09.md" in r.text)

r = c.post("/inbox/hotovo", data={"item_id": 1}, follow_redirects=False)
check("POST /inbox/hotovo", r.status_code == 303)

print("== zachycení poznámky (skutečný zápis na disk) ==")
r = c.post("/zachytit", data={"text": "Tělo poznámky", "title": "Žluťoučký kůň"})
check("zachycení s titulkem", r.status_code == 200 and "Uloženo" in r.text, str(r.status_code))
files = [p.name for p in Path(MD).glob("*.md")]
check("soubor vznikl s ASCII slugem", any("zlutoucky-kun" in f for f in files), str(files))

denik_path = Path(MD, "denik", f"{date.today().isoformat()}.md")
r = c.post("/zachytit", data={"text": "Zápis do deníku", "title": ""})
check("zachycení bez titulku jde do deníku",
      r.status_code == 200 and denik_path.exists(), str(denik_path))

c.post("/zachytit", data={"text": "Druhý zápis", "title": ""})
denik = denik_path.read_text(encoding="utf-8")
check("druhý zápis se přípíše, nepřepíše",
      "Zápis do deníku" in denik and "Druhý zápis" in denik)

print("== korpus ==")
r = c.get("/korpus")
ok = (r.status_code == 200 and "čeština" in r.text and "979" in r.text
      and "114183" in r.text and "46362" in r.text)
check("GET /korpus ukáže čísla z retrievalu", ok, str(r.status_code))
check("jazyky jsou seřazené podle počtu",
      r.text.index("čeština") < r.text.index("angličtina") < r.text.index("němčina"))
check("stav indexu se vypíše", "HNSW" in r.text and "Poslední indexace" in r.text)


def _boom():
    raise RuntimeError("spojeni odmitnuto")


core.corpus_stats = _boom
r = c.get("/korpus")
check("nedostupný retrieval nezhodí stránku",
      r.status_code == 200 and "Retrieval neodpovídá" in r.text, str(r.status_code))
core.corpus_stats = lambda: STATS

check("ANSWER_MAX_TOKENS zvednutý na 8000", core.config.ANSWER_MAX_TOKENS == 8000,
      str(core.config.ANSWER_MAX_TOKENS))

print("== P1a: rozpoznání agregačních dotazů ==")
for q in ["Kolik je kterých knih?",
          "Udělej mi sumarizaci knih podle jazyka. Kolik je kterých?",
          "kolik mam poznamek",          # bez diakritiky
          "Seřaď dokumenty podle délky",
          "Jaké je rozložení jazyků?"]:
    check("agregační: %r" % q[:36], core.je_agregacni(q))
for q in ["Čím se ladí latence dotazu u HNSW indexu?", "A proč?",
          "Jak nastavit maintenance_work_mem při stavbě indexu?"]:
    check("běžný: %r" % q[:36], not core.je_agregacni(q))

f = core.corpus_facts()
check("fakta o korpusu nesou skutečná čísla",
      "Ověřená čísla o korpusu" in f and "979" in f and "čeština 400" in f,
      repr(f[:90]))
check("hlavička faktů není interní nadpis verzálkami",
      "FAKTA O KORPUSU" not in f)
check("fakta jsou krátká (do ~600 znaků)", len(f) < 600, str(len(f)))
core.corpus_stats = _boom
check("nedostupný retrieval fakta jen vynechá", core.corpus_facts() == "")
core.corpus_stats = lambda: STATS

_msgs.clear()
_add_message(CID, "user", "Kolik je kterých knih?")
r = c.get("/konverzace/%s" % CID)
check("agregační dotaz ukáže odkaz na /korpus",
      "/korpus" in r.text and "souhrn nebo počty" in r.text)
_msgs.clear()
_add_message(CID, "user", "Čím se ladí latence dotazu?")
r = c.get("/konverzace/%s" % CID)
check("běžný dotaz odkaz neukazuje", "souhrn nebo počty" not in r.text)

print("== P1b: bezpečnost generovaného SQL ==")
check("check_sql pustí SELECT", analytics.check_sql("SELECT 1;") == "SELECT 1")
check("check_sql pustí WITH",
      analytics.check_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"))
for bad in ["DELETE FROM retrieval.document", "SELECT 1; DROP TABLE x",
            "UPDATE retrieval.document SET lang='cs'", "",
            "TRUNCATE retrieval.chunk", "COPY x FROM '/etc/passwd'",
            "SELECT 1 UNION SELECT 1; INSERT INTO x VALUES (1)"]:
    try:
        analytics.check_sql(bad)
        check("check_sql odmítne %r" % bad[:30], False, "propustil")
    except analytics.AnalyticsError:
        check("check_sql odmítne %r" % bad[:30], True)

ctx = analytics.as_context(AN)
check("výsledek jde do promptu jako tabulka",
      "lang | pocet" in ctx and "cs | 400" in ctx, repr(ctx[:60]))
check("selhaná analytika do promptu nic nedá",
      analytics.as_context({"error": "boom", "cols": [], "rows": []}) == "")

print("== P1b: tok od dotazu k připnutí ==")
_msgs.clear()
_metrics.clear()
r = c.post("/dotaz", data={"query": "Kolik je kterých dokumentů podle jazyka?",
                           "rewrite": "1"}, follow_redirects=False)
check("agregační dotaz spustil analytiku", len(_metrics) == 1, str(len(_metrics)))
check("spočítaný výsledek šel modelu jako podklad",
      "Výsledek výpočtu" in (SEEN.get("extra") or ""), repr(SEEN.get("extra"))[:60])

_msgs.clear()
r = c.post("/dotaz", data={"query": "Čím se ladí latence?", "rewrite": "1"},
           follow_redirects=False)
check("běžný dotaz analytiku nespouští", len(_metrics) == 1, str(len(_metrics)))
check("běžný dotaz nemá podklad navíc", not SEEN.get("extra"), repr(SEEN.get("extra")))

r = c.get("/konverzace/%s" % CID)
check("konverzace nabízí připnutí i SQL",
      "Připnout na /korpus" in r.text and "SELECT lang" in r.text)

r = c.post("/metrika/pripnout",
           data={"metric_id": 1, "label": "Dokumenty podle jazyka",
                 "zpet": "/konverzace/%s" % CID}, follow_redirects=False)
check("připnutí přesměruje zpět",
      r.status_code == 303 and r.headers.get("location") == "/konverzace/%s" % CID,
      str(r.headers.get("location")))
check("metrika je připnutá", _metrics[0]["pinned_at"] is not None)
check("připnutá metrika už se v konverzaci nenabízí",
      "Připnout na /korpus" not in c.get("/konverzace/%s" % CID).text)

r = c.get("/korpus")
check("/korpus ukáže připnutou metriku",
      "Dokumenty podle jazyka" in r.text and "400" in r.text)
check("čerstvá metrika není označená za zastaralou", "zastaralé" not in r.text)

analytics.fingerprint = lambda: "1000:2026-08-10 00:00:00+00"
check("změna korpusu označí metriku za zastaralou", "zastaralé" in c.get("/korpus").text)
analytics.fingerprint = lambda: FP

print("== P1b: přesměrování po akci ==")
check("cizí URL se zahodí", main._bezpecne_zpet("https://zlo.example/x") == "/korpus")
check("protokolově relativní URL se zahodí",
      main._bezpecne_zpet("//zlo.example/x") == "/korpus")
check("lokální cesta projde", main._bezpecne_zpet("/konverzace/x") == "/konverzace/x")

r = c.post("/metrika/smazat", data={"metric_id": 1, "zpet": "/korpus"},
           follow_redirects=False)
check("odepnutí metriku smaže", not _metrics and r.status_code == 303)

fresh2 = TestClient(main.app)
r = fresh2.post("/metrika/pripnout", data={"metric_id": 1}, follow_redirects=False)
check("připnutí vyžaduje přihlášení", r.headers.get("location") == "/prihlasit")

print("== mazání konverzace ==")
r = c.get("/historie")
check("historie nabízí mazání", "/konverzace/smazat" in r.text and "Smazat" in r.text)
r = c.get(f"/konverzace/{CID}")
check("konverzace nabízí mazání", "Smazat konverzaci" in r.text)
r = c.post("/konverzace/smazat", data={"conversation_id": CID}, follow_redirects=False)
check("POST /konverzace/smazat přesměruje na historii",
      r.status_code == 303 and r.headers.get("location") == "/historie",
      f"{r.status_code} {r.headers.get('location')}")
check("mazání se propsalo do DB", SMAZANO == [CID], repr(SMAZANO))

r = c.post("/konverzace/smazat", data={"conversation_id": "neni-uuid"},
           follow_redirects=False)
check("neplatné id nezhodí aplikaci", r.status_code == 303, str(r.status_code))

fresh = TestClient(main.app)
r = fresh.post("/konverzace/smazat", data={"conversation_id": CID},
               follow_redirects=False)
check("mazání vyžaduje přihlášení",
      r.headers.get("location") == "/prihlasit" and SMAZANO == [CID], repr(SMAZANO))

print("== ochrana cest ==")
for bad in ["../mimo.md", "denik/../../mimo.md", ".git/config.md", "poznamka.txt"]:
    try:
        core.safe_path(bad)
        check(f"safe_path odmítne {bad!r}", False, "propustil")
    except ValueError:
        check(f"safe_path odmítne {bad!r}", True)

print("== XSS / escaping ==")
_msgs.clear()
_add_message(CID, "user", "<script>alert(1)</script>")
r = c.get(f"/konverzace/{CID}")
check("obsah zprávy se escapuje", "<script>alert(1)</script>" not in r.text
      and "&lt;script&gt;" in r.text)

print("== odhlášení ==")
r = c.get("/odhlasit", follow_redirects=False)
check("GET /odhlasit", r.status_code == 303)

print()
if FAIL:
    print(f"SELHALO {len(FAIL)}: {', '.join(FAIL)}")
    sys.exit(1)
print("VŠE PROŠLO")
