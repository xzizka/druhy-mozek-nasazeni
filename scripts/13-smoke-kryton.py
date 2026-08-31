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
from datetime import date, datetime, timedelta, timezone
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
db.add_inbox = lambda p, e, kanal="web": None
db.kanal_stats = lambda: []
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


def _add_message(cid, role, content, citations=None, model=None,
                 latency_ms=None, stopa=None):
    _msgs.append({"id": len(_msgs) + 1, "role": role, "content": content,
                  "citations": citations or [], "model": model,
                  "latency_ms": latency_ms, "created_at": datetime.now(),
                  "rating": None, "stopa": stopa, "cid": cid})
    return len(_msgs)


db.add_message = _add_message

# Deterministické UUID z (kanál, den) — stub jen vrací totéž, co dostane,
# aby šlo v testu ověřit, že Telegram i MCP píšou každý do svého vlákna.
KANALY = []


def _konverzace_kanalu(kanal, den):
    KANALY.append((kanal, den))
    return "kanal-%s-%s" % (kanal, den)


db.konverzace_kanalu = _konverzace_kanalu

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
    return core.Odpoved("Odpověď s citací [1].", "reasoning", 1234,
                        {"n_kandidatu": 1, "n_nad_prahem": 1,
                         "max_rerank": 0.987, "odmitnuto": False,
                         "fallback": False})


_PUVODNI_ANSWER = core.answer
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

# --- stub úložiště (P2) ------------------------------------------------
from app import ingest, storage  # noqa: E402

_s3 = {}
storage.enabled = lambda: True
storage.exists = lambda key: key in _s3


def _put_original(data, key, original_name, mime):
    _s3[key] = data
    return key


storage.put_original = _put_original

_backups = {}   # key -> (data, last_modified)


def _put_backup(data, key):
    _backups[key] = (data, datetime.now(timezone.utc))


def _get_backup(key):
    return _backups[key][0]


def _list_backups(prefix=None):
    prefix = prefix or storage.config.BACKUP_S3_PREFIX
    return sorted(
        [{"key": k, "size": len(v[0]), "last_modified": v[1]}
         for k, v in _backups.items() if k.startswith(prefix)],
        key=lambda b: b["last_modified"])


def _delete_backup(key):
    _backups.pop(key, None)


storage.put_backup = _put_backup
storage.get_backup = _get_backup
storage.list_backups = _list_backups
storage.delete_backup = _delete_backup

_uploads = []
_upload_seq = [0]


def _add_upload(**kw):
    # ID stabilní přes UPSERT (stejný source_path = stejné ID, jako
    # ON CONFLICT ... DO UPDATE ... RETURNING id v reálné DB), a NIKDY
    # se neopakuje — `len(_uploads) + 1` po smazání/přepsání kolidovalo.
    kw = dict(kw)
    kw["created_at"] = datetime.now()
    existing = next((u for u in _uploads if u["source_path"] == kw["source_path"]), None)
    if existing:
        kw["id"] = existing["id"]
        _uploads.remove(existing)
    else:
        _upload_seq[0] += 1
        kw["id"] = _upload_seq[0]
    _uploads.append(kw)
    return kw["id"]


def _delete_upload(upload_id):
    for i, u in enumerate(_uploads):
        if u["id"] == upload_id:
            return _uploads.pop(i)["source_path"]
    return None


db.add_upload = _add_upload
db.uploads = lambda limit=200: list(reversed(_uploads))
db.delete_upload = _delete_upload

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

c = TestClient(main.app)

print("== bez přihlášení ==")
for path in ["/", "/historie", "/inbox", "/zachytit", "/korpus", "/nahrat"]:
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
check("stránka konverzace scroluje na poslední příspěvek",
      "scrollTo(0,document.body.scrollHeight)" in r.text)

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

# 8000 -> 2000 v commitu 214fee3 (2026-08-18, krok 3): gpt-oss-120b má proti
# big-pickle řádově menší apetit na reasoning_content (medián 109 tokenů proti
# 402 a víc), a strop 8000 byl přímou příčinou zacyklení gemmy 2026-08-17.
# Tenhle check zůstal šest dní na staré hodnotě a smoke test byl celou tu dobu
# červený, aniž si toho kdo všiml — nic ho totiž nespouští automaticky.
check("ANSWER_MAX_TOKENS snížený na 2000 (P8 bod b, krok 3)",
      core.config.ANSWER_MAX_TOKENS == 2000, str(core.config.ANSWER_MAX_TOKENS))

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

print("== P2: kódování a formáty ==")
check("utf-8 se pozná",
      ingest.dekoduj("Příliš žluťoučký".encode("utf-8")) == ("Příliš žluťoučký", "utf-8"))
_t, _e = ingest.dekoduj("Příliš žluťoučký".encode("cp1250"))
check("český cp1250 se dekóduje správně", (_t, _e) == ("Příliš žluťoučký", "cp1250"),
      "%r %s" % (_t, _e))

try:
    ingest.extrahuj(b"data", "soubor.doc")
    check(".doc (mimo rozsah) odmítne", False, "prošel")
except ingest.IngestError as ex:
    check(".doc (mimo rozsah) odmítne srozumitelně",
          "docx" in str(ex).lower(), str(ex))
for pripona in (".exe", ".jpg", ""):
    try:
        ingest.extrahuj(b"data", "soubor" + pripona)
        check("nepodporovaná přípona %r odmítnuta" % pripona, False, "prošla")
    except ingest.IngestError:
        check("nepodporovaná přípona %r odmítnuta" % pripona, True)
try:
    ingest.extrahuj(b"   \n  ", "prazdny.txt")
    check("soubor bez textu odmítnut", False, "prošel")
except ingest.IngestError:
    check("soubor bez textu odmítnut", True)

print("== P2 etapa 2: PDF ==")


def _minimalni_pdf(texty):
    """Syntakticky platné PDF s jednou stránkou na text, bez závislostí —
    offsety v xref se počítají, ne odhadují, aby to pypdf přečetl napoprvé."""
    n = len(texty)
    objs = {}
    objs[1] = b"<</Type/Catalog/Pages 2 0 R>>"
    kids = " ".join("%d 0 R" % (3 + i) for i in range(n))
    objs[2] = ("<</Type/Pages/Kids[%s]/Count %d>>" % (kids, n)).encode()
    font_num = 3 + 2 * n
    for i, text in enumerate(texty):
        page_num, content_num = 3 + i, 3 + n + i
        content = (("BT /F1 24 Tf 72 700 Td (%s) Tj ET" % text) if text else "").encode("ascii")
        objs[page_num] = (
            "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            "/Resources<</Font<</F1 %d 0 R>>>>/Contents %d 0 R>>"
            % (font_num, content_num)).encode()
        objs[content_num] = (("<</Length %d>>\nstream\n" % len(content)).encode()
                             + content + b"\nendstream")
    objs[font_num] = b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += ("%d 0 obj" % num).encode() + objs[num] + b"endobj\n"
    xref_offset = len(out)
    maxnum = max(objs)
    out += ("xref\n0 %d\n" % (maxnum + 1)).encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, maxnum + 1):
        out += ("%010d 00000 n \n" % offsets[num]).encode()
    out += ("trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF"
            % (maxnum + 1, xref_offset)).encode()
    return bytes(out)


PDF_OK = _minimalni_pdf(["Ahoj svete", "Druha stranka"])
text_pdf, enc_pdf = ingest.extrahuj(PDF_OK, "zprava.pdf")
check("PDF: obě stránky se vytáhly",
      "Ahoj svete" in text_pdf and "Druha stranka" in text_pdf, text_pdf)
check("PDF: rozpozná se jako 'pdf'", enc_pdf == "pdf", enc_pdf)

try:
    ingest.extrahuj(_minimalni_pdf(["", "", ""]), "sken.pdf")
    check("PDF bez textové vrstvy (sken) odmítnut", False, "prošel")
except ingest.IngestError as ex:
    check("PDF bez textové vrstvy (sken) odmítnut srozumitelně",
          "sken" in str(ex).lower(), str(ex))

_puv_max_stran = ingest.config.UPLOAD_MAX_PAGES
ingest.config.UPLOAD_MAX_PAGES = 2
try:
    ingest.extrahuj(_minimalni_pdf(["a", "b", "c"]), "moc-stranek.pdf")
    check("strop počtu stránek se vynutí", False, "prošel")
except ingest.IngestError as ex:
    check("strop počtu stránek se vynutí", "stránek" in str(ex), str(ex))
ingest.config.UPLOAD_MAX_PAGES = _puv_max_stran

print("== P2 etapa 3: DOCX ==")
import io as _io
import zipfile as _zipfile


def _minimalni_docx(odstavce):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % o for o in odstavce)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (ns, body))
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


DOCX_OK = _minimalni_docx(["První odstavec.", "Druhý odstavec, s diakritikou."])
text_docx, enc_docx = ingest.extrahuj(DOCX_OK, "smlouva.docx")
check("DOCX: oba odstavce se vytáhly",
      "První odstavec." in text_docx and "Druhý odstavec" in text_docx, text_docx)
check("DOCX: rozpozná se jako 'docx'", enc_docx == "docx", enc_docx)

try:
    ingest.extrahuj(b"tohle neni zip", "poskozeny.docx")
    check("poškozený DOCX (není ZIP) odmítnut", False, "prošel")
except ingest.IngestError:
    check("poškozený DOCX (není ZIP) odmítnut", True)

_prazdny_zip = _io.BytesIO()
with _zipfile.ZipFile(_prazdny_zip, "w") as _zf:
    _zf.writestr("neco-jineho.txt", "x")
try:
    ingest.extrahuj(_prazdny_zip.getvalue(), "bez-obsahu.docx")
    check("DOCX bez word/document.xml odmítnut", False, "prošel")
except ingest.IngestError as ex:
    check("DOCX bez word/document.xml odmítnut srozumitelně",
          "document.xml" in str(ex), str(ex))

_puv_limit = ingest.__dict__["_DOCX_MAX_UNCOMPRESSED"]
ingest._DOCX_MAX_UNCOMPRESSED = 1024 * 1024
_bomba = _minimalni_docx(["A" * (5 * 1024 * 1024)])
try:
    ingest.extrahuj(_bomba, "bomba.docx")
    check("zip bomba v DOCX se odmítne", False, "prošel")
except ingest.IngestError as ex:
    check("zip bomba v DOCX se odmítne", "MB" in str(ex), str(ex))
ingest._DOCX_MAX_UNCOMPRESSED = _puv_limit

print("== P2 etapa 2+3: celý tok nahrání (uloz) pro PDF a DOCX ==")
# Sdílený stub _uploads/_s3 předpokládají pozdější sekce prázdný před prvním
# nahráním (viz "P2: celý tok nahrání" níž), proto se stav kolem téhle
# kontroly zálohuje a vrací, ne jen doplňuje.
_uploads_zaloha, _s3_zaloha = list(_uploads), dict(_s3)
_uploads.clear()
_s3.clear()

res_pdf = ingest.uloz(PDF_OK, "zprava.pdf")
check("PDF: celý tok (S3 + evidence + trust)",
      res_pdf["encoding"] == "pdf" and len(_uploads) == 1 and len(_s3) == 1
      and "trust: 1" in Path(MD, res_pdf["source_path"]).read_text(encoding="utf-8"),
      res_pdf)

res_docx = ingest.uloz(DOCX_OK, "smlouva.docx")
check("DOCX: celý tok (S3 + evidence + trust)",
      res_docx["encoding"] == "docx" and len(_uploads) == 2 and len(_s3) == 2
      and "trust: 1" in Path(MD, res_docx["source_path"]).read_text(encoding="utf-8"),
      res_docx)

_uploads.clear(); _uploads.extend(_uploads_zaloha)
_s3.clear(); _s3.update(_s3_zaloha)

print("== P2: klíč objektu a region ==")
check("klíč objektu je odvozený z hashe",
      storage.object_key("abc123", ".TXT") == "originals/abc123.txt",
      storage.object_key("abc123", ".TXT"))
_puv_ep, _puv_reg = storage.config.S3_ENDPOINT, storage.config.S3_REGION
storage.config.S3_ENDPOINT = "https://s3.eu-central-003.backblazeb2.com"
storage.config.S3_REGION = ""
check("region se odvodí z endpointu", storage.region() == "eu-central-003",
      storage.region())
storage.config.S3_REGION = "rucne-zadany"
check("ručně zadaný region má přednost", storage.region() == "rucne-zadany")
storage.config.S3_ENDPOINT, storage.config.S3_REGION = _puv_ep, _puv_reg

print("== zálohy DB a secrets (app/storage.py) ==")
K1 = storage.config.BACKUP_S3_PREFIX + "kryton/kryton-cerstva.dump"
K2 = storage.config.BACKUP_S3_PREFIX + "kryton/kryton-stara.dump"
K3 = storage.config.BACKUP_S3_PREFIX + "litellm/litellm-cerstva.dump"

storage.put_backup(b"cerstvy dump", K1)
check("put_backup + get_backup je round-trip", storage.get_backup(K1) == b"cerstvy dump")

storage.put_backup(b"stary dump", K2)
_backups[K2] = (b"stary dump", datetime.now(timezone.utc) - timedelta(days=40))
storage.put_backup(b"jiny prefix", K3)

seznam = storage.list_backups(storage.config.BACKUP_S3_PREFIX + "kryton/")
check("list_backups filtruje podle prefixu", {b["key"] for b in seznam} == {K1, K2},
      str(seznam))
check("list_backups seřadí od nejstarší", [b["key"] for b in seznam] == [K2, K1],
      str([b["key"] for b in seznam]))

hranice = datetime.now(timezone.utc) - timedelta(days=30)
stare = [b for b in seznam if b["last_modified"] < hranice]
check("retenční filtr najde přesně jednu starou zálohu",
      len(stare) == 1 and stare[0]["key"] == K2, str(stare))
for b in stare:
    storage.delete_backup(b["key"])
check("delete_backup smazal starou, čerstvá i jiný prefix zůstaly",
      K2 not in _backups and K1 in _backups and K3 in _backups)

print("== P2: celý tok nahrání ==")
DATA = "# Poznámka\n\nObsah nahraného dokumentu.\n".encode("utf-8")
res = ingest.uloz(DATA, "Můj Dokument.md")
check("text jde do _uploads/", res["source_path"].startswith("_uploads/"),
      res["source_path"])
cesta = Path(MD, res["source_path"])
check("markdown soubor vznikl", cesta.exists(), str(cesta))
obsah = cesta.read_text(encoding="utf-8")
check("frontmatter nese trust pro importované", "trust: 1" in obsah, obsah[:60])
check("frontmatter nepředjímá jazyk", "lang:" not in obsah)
check("název souboru je bez diakritiky", "muj-dokument" in res["source_path"],
      res["source_path"])
check("originál je na S3 pod hashem obsahu",
      res["s3_key"] == "originals/%s.md" % res["sha256"], res["s3_key"])
check("originál se opravdu uložil", _s3.get(res["s3_key"]) == DATA)
check("zapsáno do evidence", len(_uploads) == 1 and _uploads[0]["s3_profile"])

res2 = ingest.uloz(DATA, "Můj Dokument.md")
check("stejný obsah nevytvoří druhý objekt",
      res2["s3_key"] == res["s3_key"] and res2["source_path"] == res["source_path"]
      and len(_s3) == 1 and len(_uploads) == 1)

_puv_max = core.config.UPLOAD_MAX_BYTES
core.config.UPLOAD_MAX_BYTES = 16
try:
    ingest.uloz(b"x" * 100, "velky.txt")
    check("strop velikosti se vynutí", False, "prošlo")
except ingest.IngestError as ex:
    check("strop velikosti se vynutí", "strop" in str(ex), str(ex))
core.config.UPLOAD_MAX_BYTES = _puv_max

print("== P2: stránka nahrávání ==")
r = c.get("/nahrat")
check("GET /nahrat", r.status_code == 200 and "Nahrát dokument" in r.text)
check("výpis ukáže nahraný soubor", "Můj Dokument.md" in r.text)

r = c.post("/nahrat", files={"soubor": ("pokus.txt", "Nějaký český text".encode("cp1250"),
                                        "text/plain")})
check("POST /nahrat uloží soubor", r.status_code == 200 and "Nahráno jako" in r.text,
      str(r.status_code))
check("stránka hlásí použité kódování", "cp1250" in r.text)

r = c.post("/nahrat", files={"soubor": ("neplatny.pdf", b"tohle neni platne PDF",
                                        "application/pdf")})
check("neplatný PDF obsah odmítne srozumitelně a nespadne",
      r.status_code == 200 and "Nahrání se nepovedlo" in r.text, str(r.status_code))

fresh3 = TestClient(main.app)
r = fresh3.post("/nahrat", files={"soubor": ("x.txt", b"data", "text/plain")},
                follow_redirects=False)
check("nahrávání vyžaduje přihlášení", r.headers.get("location") == "/prihlasit")

print("== P3: mazání nahraného dokumentu (index ANO, S3 originál NIKDY) ==")
cil = next(u for u in _uploads if u["source_path"].startswith("_uploads/pokus-"))
cesta_pokus = Path(MD, cil["source_path"])
check("soubor pokus.txt před smazáním existuje", cesta_pokus.exists(), str(cesta_pokus))

fresh4 = TestClient(main.app)
r = fresh4.post("/nahrat/smazat", data={"upload_id": cil["id"]}, follow_redirects=False)
check("mazání bez přihlášení přesměruje", r.headers.get("location") == "/prihlasit")
check("soubor bez přihlášení zůstal", cesta_pokus.exists())

pocet_pred = len(_uploads)
r = c.post("/nahrat/smazat", data={"upload_id": cil["id"]}, follow_redirects=False)
check("POST /nahrat/smazat přesměruje na /nahrat",
      r.status_code == 303 and r.headers.get("location") == "/nahrat", str(r.status_code))
check("řádek zmizel z evidence", len(_uploads) == pocet_pred - 1)
check("soubor zmizel z disku", not cesta_pokus.exists())
check("S3 originál ZŮSTAL (nikdy se nemaže)", cil["s3_key"] in _s3)

r = c.post("/nahrat/smazat", data={"upload_id": 999999}, follow_redirects=False)
check("smazání neexistujícího id nespadne", r.status_code == 303)

print("== ochrana cest ==")
for bad in ["../mimo.md", "denik/../../mimo.md", ".git/config.md", "poznamka.txt"]:
    try:
        core.safe_path(bad)
        check(f"safe_path odmítne {bad!r}", False, "propustil")
    except ValueError:
        check(f"safe_path odmítne {bad!r}", True)

print("== Telegram můstek (krok 1: jen text) ==")
from app import telegram  # noqa: E402
import httpx as _httpx  # noqa: E402

# NEJDŘÍV test na SKUTEČNÉ (neopatchované) _call — ověřuje přesně tu chybu,
# která se stala živě: httpx nese token přímo v URL a `log.exception()`
# by ho jinak zapsal do journalu při každém síťovém zádrhelu.
_TAJNY_MARKER = "TAJNY-TOKEN-MARKER-x7z9"
_puv_httpx_post = _httpx.post


def _boom(*a, **kw):
    raise _httpx.HTTPError(
        "mock chyba s url https://api.telegram.org/bot%s/getUpdates" % _TAJNY_MARKER)


_httpx.post = _boom
_puv_token0 = core.config.TELEGRAM_BOT_TOKEN
core.config.TELEGRAM_BOT_TOKEN = _TAJNY_MARKER
try:
    telegram._call("getUpdates", timeout=0)
    check("_call vyhodí výjimku při síťové chybě", False, "neshodilo se")
except RuntimeError as e:
    text_vyjimky = str(e) + str(e.__cause__ or "") + str(e.__context__ or "")
    check("chybová zpráva (vč. chained cause/context) neobsahuje token",
          _TAJNY_MARKER not in text_vyjimky, text_vyjimky)
_httpx.post = _puv_httpx_post
core.config.TELEGRAM_BOT_TOKEN = _puv_token0

_tg_sent = []
telegram._call = lambda method, http_timeout=15, **params: (
    _tg_sent.append((method, params)) or [])

_puv_token, _puv_id = core.config.TELEGRAM_BOT_TOKEN, core.config.TELEGRAM_ALLOWED_USER_ID
core.config.TELEGRAM_BOT_TOKEN, core.config.TELEGRAM_ALLOWED_USER_ID = "", 0
check("enabled() je False bez tokenu/id", not telegram.enabled())
core.config.TELEGRAM_BOT_TOKEN, core.config.TELEGRAM_ALLOWED_USER_ID = "test-token", 819345451
check("enabled() je True s tokenem i id", telegram.enabled())

BOT_MSG = {"from": {"is_bot": True}}
check("_je_odpoved_na_bota pozná reply na bota",
      telegram._je_odpoved_na_bota({"reply_to_message": BOT_MSG}))
check("_je_odpoved_na_bota odmítne reply na člověka",
      not telegram._je_odpoved_na_bota({"reply_to_message": {"from": {"is_bot": False}}}))
check("_je_odpoved_na_bota odmítne zprávu bez reply",
      not telegram._je_odpoved_na_bota({}))

_tg_sent.clear()
telegram._handle_message({"from": {"id": 999999}, "chat": {"id": 999999},
                          "text": "cizí zpráva"})
check("neautorizovaný uživatel se ignoruje (nic se nepošle)", _tg_sent == [], str(_tg_sent))

_tg_sent.clear()
telegram._handle_message({"from": {"id": 819345451}, "chat": {"id": 819345451},
                          "sticker": {}})
check("zpráva bez textu dostane placeholder o hlasu",
      len(_tg_sent) == 1 and "hlas" in _tg_sent[0][1]["text"], str(_tg_sent))

_tg_sent.clear()
ZNACKA = "TELEGRAM-ODPOVED-ZNACKA-8b3f"
telegram._handle_message({"from": {"id": 819345451}, "chat": {"id": 819345451},
                          "text": ZNACKA, "reply_to_message": BOT_MSG})
check("reply na bota jde do core.capture, ne do core.answer",
      len(_tg_sent) == 1 and "Zaznamenáno" in _tg_sent[0][1]["text"], str(_tg_sent))
soubory = list(Path(MD, "denik").glob("*.md")) if Path(MD, "denik").exists() else []
check("zaznamenaný text se opravdu zapsal na disk",
      any(ZNACKA in f.read_text(encoding="utf-8") for f in soubory), str(soubory))

_tg_sent.clear()
telegram._handle_message({"from": {"id": 819345451}, "chat": {"id": 819345451},
                          "text": "čerstvý dotaz bez reply"})
check("čerstvá zpráva (bez reply) jde do core.search+core.answer",
      len(_tg_sent) == 1 and "Odpověď s citací" in _tg_sent[0][1]["text"], str(_tg_sent))

core.config.TELEGRAM_BOT_TOKEN, core.config.TELEGRAM_ALLOWED_USER_ID = _puv_token, _puv_id

print("== Telegram krok 2: plán denní otázky (čistá logika, bez sítě) ==")
core.config.TELEGRAM_DAILY_QUESTION_HOUR_UTC = 6
telegram._posledni_odeslano[0] = None
check("před hodinou X se nepošle",
      not telegram._mel_bych_poslat_otazku(datetime(2026, 1, 1, 5, 59, tzinfo=timezone.utc)))
check("po hodině X se pošle (poprvé ten den)",
      telegram._mel_bych_poslat_otazku(datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)))
telegram._posledni_odeslano[0] = datetime(2026, 1, 1).date()
check("stejný den podruhé se nepošle",
      not telegram._mel_bych_poslat_otazku(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)))
check("další den po hodině X se pošle znovu",
      telegram._mel_bych_poslat_otazku(datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)))
telegram._posledni_odeslano[0] = None

print("== stopa odpovědi (P4/P7-B/P8: co v textu odpovědi vidět není) ==")

# Testuje se SKUTEČNÝ `core.answer` (uschovaný do `_PUVODNI_ANSWER` ještě
# před nasazením stubu), ne stub. Jde to bez sítě: když pod prahem nezbyde
# ani jeden chunk, funkce se vrátí dřív, než by zavolala LiteLLM — a právě
# tahle větev je P7-B.
_puv_prah = core.config.ANSWER_MIN_RERANK
core.config.ANSWER_MIN_RERANK = 0.1
_slabe = [dict(HIT, chunk_id="c9", rerank_score=0.0215)]
_odp = _PUVODNI_ANSWER("temporální dotaz", _slabe)
check("pod prahem se neodpoví a stopa to řekne",
      _odp.stopa["odmitnuto"] is True and _odp.stopa["n_nad_prahem"] == 0,
      str(_odp.stopa))
check("max_rerank se bere PŘED filtrem, jinak by v P7-B bylo None",
      _odp.stopa["max_rerank"] == 0.0215, str(_odp.stopa))
check("n_kandidatu je počet PŘED filtrem", _odp.stopa["n_kandidatu"] == 1,
      str(_odp.stopa))
core.config.ANSWER_MIN_RERANK = _puv_prah

check("stopa dojde až do db.add_message (webová cesta /dotaz)",
      any(m.get("stopa") and m["stopa"].get("max_rerank") == 0.987
          for m in _msgs if m["role"] == "assistant"),
      str([m.get("stopa") for m in _msgs]))

print("== záznam z kanálů bez vlákna (Telegram, MCP) ==")

KANALY.clear()
_pocet_pred = len(_msgs)
core.zaznamenej("telegram", "dotaz z mobilu", _answer("x", [HIT]), [HIT])
check("zaznamenej uloží otázku i odpověď", len(_msgs) - _pocet_pred == 2,
      "%d nových" % (len(_msgs) - _pocet_pred))
check("obojí jde do téhož denního vlákna kanálu",
      len(KANALY) == 1 and KANALY[0][0] == "telegram", str(KANALY))
check("odpověď nese citace i stopu",
      _msgs[-1]["citations"] and _msgs[-1]["stopa"], str(_msgs[-1])[:160])

_pocet_pred = len(_msgs)
core.zaznamenej("telegram", "dotaz, co spadl", None, chyba="RuntimeError('x')")
check("selhaná odpověď se uloží taky, ne že zmizí",
      len(_msgs) - _pocet_pred == 2 and "Dotaz selhal" in _msgs[-1]["content"],
      _msgs[-1]["content"][:80])

# NEJDŮLEŽITĚJŠÍ CHECK CELÉ ZMĚNY. Telegram do 2026-08-24 odpovídal bez
# databáze úplně; kdyby ho zápis mohl shodit, byl by výpadek Postgresu
# k nerozeznání od nefunkčního bota. Auditní stopa se smí ztratit, odpověď ne.
def _rozbita_db(*a, **kw):
    raise RuntimeError("postgres je dole")


_puv_kk, db.konverzace_kanalu = db.konverzace_kanalu, _rozbita_db
_spadlo = False
try:
    core.zaznamenej("telegram", "dotaz pri rozbite DB", _answer("x", [HIT]), [HIT])
except Exception:
    _spadlo = True
db.konverzace_kanalu = _puv_kk
check("rozbitá DB NESHODÍ zaznamenej (odpověď se doručí i tak)", not _spadlo)

_tg_sent.clear()
KANALY.clear()
_puv_token2 = core.config.TELEGRAM_BOT_TOKEN
_puv_id2 = core.config.TELEGRAM_ALLOWED_USER_ID
core.config.TELEGRAM_BOT_TOKEN = "token"
core.config.TELEGRAM_ALLOWED_USER_ID = 819345451
_pocet_pred = len(_msgs)
telegram._handle_message({"from": {"id": 819345451}, "chat": {"id": 819345451},
                          "text": "kolik mam poznamek"})
check("Telegram dotaz se OPRAVDU uloží (dřív se neukládal vůbec)",
      len(_msgs) - _pocet_pred == 2 and KANALY and KANALY[0][0] == "telegram",
      "%d novych, kanaly=%s" % (len(_msgs) - _pocet_pred, KANALY))
check("uživateli pořád odejde odpověď",
      len(_tg_sent) == 1 and "Odpověď s citací" in _tg_sent[0][1]["text"],
      str(_tg_sent))
core.config.TELEGRAM_BOT_TOKEN = _puv_token2
core.config.TELEGRAM_ALLOWED_USER_ID = _puv_id2

print("== XSS / escaping ==")
_msgs.clear()
_add_message(CID, "user", "<script>alert(1)</script>")
r = c.get(f"/konverzace/{CID}")
check("obsah zprávy se escapuje", "<script>alert(1)</script>" not in r.text
      and "&lt;script&gt;" in r.text)

print("== odhlášení ==")
r = c.get("/odhlasit", follow_redirects=False)
check("GET /odhlasit", r.status_code == 303)

print("== MCP server (/mcp) ==")
import asyncio as _asyncio
import threading as _threading

import uvicorn as _uvicorn
from fastmcp import Client as _MCPClient

from app import mcp_server  # noqa: E402

TOKEN = "smoke-test-token"
main.config.MCP_BEARER_TOKEN = TOKEN


def _over(token):
    return _asyncio.run(mcp_server._SdilenyToken().verify_token(token))


check("verify_token přijme správný token", _over(TOKEN) is not None)
check("verify_token odmítne špatný token", _over("neco-jineho") is None)
main.config.MCP_BEARER_TOKEN = ""
check("verify_token odmítne cokoliv, když secret chybí",
      _over(TOKEN) is None and _over("") is None)
main.config.MCP_BEARER_TOKEN = TOKEN

res = mcp_server.hledat.fn("kde je HNSW?")
check("nástroj hledat() zavolá core.search+core.answer",
      res["odpoved"] == "Odpověď s citací [1]."
      and res["citace"][0]["source_path"] == "poznamky/test.md",
      res)

res = mcp_server.zachytit.fn("Poznámka z MCP nástroje.", "Zkouška MCP")
cesta_mcp = Path(MD, res["ulozeno_do"])
check("nástroj zachytit() opravdu zapsal soubor",
      cesta_mcp.exists() and "Poznámka z MCP nástroje." in cesta_mcp.read_text(encoding="utf-8"),
      res)

_mcp_port = 18931
_mcp_server_cfg = _uvicorn.Config(main.app, host="127.0.0.1", port=_mcp_port,
                                   log_level="warning", lifespan="on")
_mcp_uvicorn = _uvicorn.Server(_mcp_server_cfg)
_mcp_thread = _threading.Thread(
    target=lambda: _asyncio.run(_mcp_uvicorn.serve()), daemon=True)
_mcp_thread.start()
import time as _time
_time.sleep(1.5)
_MCP_URL = f"http://127.0.0.1:{_mcp_port}/mcp"


async def _mcp_over_http():
    async with _MCPClient(_MCP_URL, auth=TOKEN) as cl:
        r = await cl.call_tool("hledat", {"dotaz": "test"})
        ok_spravny = r.data["odpoved"] == "Odpověď s citací [1]."
    try:
        async with _MCPClient(_MCP_URL, auth="spatny-token") as cl:
            await cl.call_tool("hledat", {"dotaz": "test"})
        odmitl_spatny = False
    except Exception:
        odmitl_spatny = True
    try:
        async with _MCPClient(_MCP_URL) as cl:
            await cl.call_tool("hledat", {"dotaz": "test"})
        odmitl_bez = False
    except Exception:
        odmitl_bez = True
    return ok_spravny, odmitl_spatny, odmitl_bez


_ok_spravny, _odmitl_spatny, _odmitl_bez = _asyncio.run(_mcp_over_http())
check("MCP přes skutečné HTTP: správný token projde a zavolá nástroj", _ok_spravny)
check("MCP přes skutečné HTTP: špatný token odmítnut", _odmitl_spatny)
check("MCP přes skutečné HTTP: chybějící token odmítnut", _odmitl_bez)
_mcp_uvicorn.should_exit = True
_mcp_thread.join(timeout=5)

print()
if FAIL:
    print(f"SELHALO {len(FAIL)}: {', '.join(FAIL)}")
    sys.exit(1)
print("VŠE PROŠLO")
