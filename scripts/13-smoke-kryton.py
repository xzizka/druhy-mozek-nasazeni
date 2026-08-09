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


def _answer(q, hits, prior=None):
    SEEN["prior"] = prior
    return ("Odpověď s citací [1].", "reasoning", 1234)


core.answer = _answer
core.trigger_reindex = lambda: None

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

c = TestClient(main.app)

print("== bez přihlášení ==")
for path in ["/", "/historie", "/inbox", "/zachytit"]:
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
