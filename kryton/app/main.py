"""Kryton — webové UI second brainu.

Server-rendered HTML, šablony inline. Žádný build step, žádný JS toolchain;
vejde se do MemoryMax=800M a je to jeden stack s retrievalem.
"""
from __future__ import annotations

import logging

from fastapi import Cookie, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from markupsafe import Markup

from . import config, core, db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kryton")

app = FastAPI(title="Kryton", version="1.0")
env = Environment(autoescape=True)   # autoescape: obsah poznámek jde do HTML
COOKIE = "kryton_session"

BASE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ t }} — Kryton</title>
<style>
:root{color-scheme:light dark}
body{font:16px/1.55 system-ui,sans-serif;max-width:52rem;margin:0 auto;padding:1rem 1.2rem 4rem}
a{color:inherit}nav{display:flex;gap:1rem;margin-bottom:1.5rem;font-size:.9rem;opacity:.8}
textarea,input{font:inherit;width:100%;box-sizing:border-box;padding:.5rem;
 border:1px solid #8886;border-radius:.4rem;background:transparent;color:inherit}
button{font:inherit;padding:.45rem 1rem;border-radius:.4rem;border:1px solid #8886;
 background:#8882;color:inherit;cursor:pointer}
.msg{padding:.8rem 1rem;border-radius:.5rem;margin:.8rem 0;background:#8881}
.msg.user{background:#8883}
.cit{font-size:.85rem;opacity:.85;margin-top:.6rem;padding-top:.5rem;border-top:1px solid #8883}
.cit li{margin:.2rem 0}
.meta{font-size:.78rem;opacity:.6;margin-top:.4rem}
.row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
pre{white-space:pre-wrap;font:inherit;margin:0}
.err{background:#f005;padding:.6rem 1rem;border-radius:.4rem}
</style></head><body>
<nav><a href="/"><b>Kryton</b></a><a href="/zachytit">Zachytit</a>
<a href="/historie">Historie</a><a href="/inbox">Inbox</a>
<span style="flex:1"></span><a href="/odhlasit">Odhlásit</a></nav>
{{ body }}
</body></html>"""


def page(title: str, body_html: str) -> HTMLResponse:
    # Markup je tu POVINNÝ, ne kosmetika. `body_html` je výstup render(),
    # tedy HTML, které Jinja s autoescape=True už jednou proescapovala.
    # Bez Markup ho vnější šablona escapuje podruhé a v prohlížeči se místo
    # stránky objeví její zdroják — `<h1>Nadpis</h1>` jako viditelný text.
    # Bezpečné to je právě proto, že vnitřní render escapuje všechna
    # dosazovaná data; značky pocházejí výhradně ze šablon v tomhle souboru.
    return HTMLResponse(env.from_string(BASE).render(t=title, body=Markup(body_html)))


def render(tpl: str, **kw) -> str:
    return env.from_string(tpl).render(**kw)


def logged_in(cookie) -> bool:
    return core.valid_session(cookie)


LOGIN = """<h1>Kryton</h1>{% if err %}<p class="err">{{ err }}</p>{% endif %}
<form method="post" action="/prihlasit"><p><input type="password" name="password"
 placeholder="Heslo" autofocus></p><p><button>Přihlásit</button></p></form>"""


@app.on_event("startup")
def _startup():
    if not config.AUTH_PASSWORD or not config.SESSION_SECRET:
        # Port 3001 je publikovaný na 0.0.0.0 a firewall pouští celý segment
        # 10.20.0.0/24. Běh bez hesla by znamenal poznámky otevřené homelabu,
        # proto se raději odmítnu spustit, než abych tiše běžel nechráněný.
        raise RuntimeError("chybi AUTH_PASSWORD nebo SESSION_SECRET — "
                           "nespoustim se bez autentizace")
    db.init()
    log.info("start: retrieval=%s litellm=%s model=%s",
             config.RETRIEVAL_URL, config.LITELLM_URL, config.ANSWER_MODEL)


@app.on_event("shutdown")
def _shutdown():
    db.close()


@app.get("/healthz")
def healthz():
    return {"status": "ok" if db.ping() else "degraded", "db": db.ping()}


@app.get("/prihlasit", response_class=HTMLResponse)
def login_form():
    return page("Přihlášení", render(LOGIN, err=None))


@app.post("/prihlasit")
def login(password: str = Form("")):
    if not core.check_password(password):
        return page("Přihlášení", render(LOGIN, err="Špatné heslo."))
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(COOKIE, core.make_session(), httponly=True, samesite="lax",
                 max_age=config.SESSION_HOURS * 3600)
    return r


@app.get("/odhlasit")
def logout():
    r = RedirectResponse("/prihlasit", status_code=303)
    r.delete_cookie(COOKIE)
    return r


ASK = """<form method="post" action="/dotaz">
<p><textarea name="query" rows="3" autofocus
 placeholder="Na co se chceš zeptat svých poznámek?"></textarea></p>
<div class="row"><button>Zeptat se</button>
<label><input type="checkbox" name="rewrite" value="1" checked style="width:auto"> přepis dotazu přes LLM</label></div>
</form>
{% if convs %}<h2>Poslední konverzace</h2><ul>
{% for c in convs %}<li><a href="/konverzace/{{ c.id }}">{{ c.title or "(bez názvu)" }}</a>
<span class="meta">{{ c.created_at.strftime("%d.%m. %H:%M") }} · {{ c.messages }} zpráv</span></li>{% endfor %}
</ul>{% endif %}"""


@app.get("/", response_class=HTMLResponse)
def index(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return page("Dotaz", render(ASK, convs=db.conversations(10)))


CONV = """<h1>{{ title or "Konverzace" }}</h1>
{% for m in msgs %}
<div class="msg {{ m.role }}"><pre>{{ m.content }}</pre>
{% if m.citations %}<div class="cit"><b>Zdroje:</b><ol>
{% for c in m.citations %}<li><code>{{ c.source_path }}</code>{% if c.heading_path %}
 — {{ c.heading_path }}{% endif %}{% if c.rerank_score is not none %}
 <span class="meta">rerank {{ "%.3f"|format(c.rerank_score) }}</span>{% endif %}</li>{% endfor %}
</ol></div>{% endif %}
{% if m.role == "assistant" %}<div class="meta">{{ m.model }} · {{ m.latency_ms }} ms
{% if m.rating == 1 %}· ohodnoceno 👍{% elif m.rating == -1 %}· ohodnoceno 👎{% endif %}</div>
<form method="post" action="/hodnoceni" class="row" style="margin-top:.5rem">
<input type="hidden" name="message_id" value="{{ m.id }}">
<input type="hidden" name="conversation_id" value="{{ cid }}">
<button name="rating" value="1">👍</button><button name="rating" value="-1">👎</button></form>
{% endif %}</div>
{% endfor %}
<form method="post" action="/dotaz"><input type="hidden" name="conversation_id" value="{{ cid }}">
<p><textarea name="query" rows="2" placeholder="Doplňující dotaz…"></textarea></p>
<div class="row"><button>Zeptat se</button>
<label><input type="checkbox" name="rewrite" value="1" checked style="width:auto"> přepis dotazu přes LLM</label></div>
</form>"""


@app.get("/konverzace/{cid}", response_class=HTMLResponse)
def conversation(cid: str, kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    msgs = db.messages(cid)
    title = next((c["title"] for c in db.conversations(200) if c["id"] == cid), None)
    return page(title or "Konverzace", render(CONV, msgs=msgs, cid=cid, title=title))


@app.post("/dotaz")
def ask(query: str = Form(...), conversation_id: str = Form(None),
        rewrite: str = Form(None), kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    q = query.strip()
    if not q:
        return RedirectResponse("/", status_code=303)

    # Historie se čte PŘED zápisem nové otázky — ta jde modelu zvlášť,
    # spolu s úryvky, a nesmí v kontextu figurovat dvakrát.
    prior = db.messages(conversation_id) if conversation_id else []
    cid = conversation_id or str(db.new_conversation(q))
    db.add_message(cid, "user", q)
    try:
        res = core.search(q, rewrite=bool(rewrite))
        hits = res["results"]
        text, model, ms = core.answer(q, hits, prior=prior)
        cits = [{"source_path": h["source_path"], "heading_path": h.get("heading_path"),
                 "chunk_id": h["chunk_id"], "rerank_score": h.get("rerank_score")}
                for h in hits]
        db.add_message(cid, "assistant", text, cits, model, ms)
    except Exception as e:
        log.exception("dotaz selhal")
        db.add_message(cid, "assistant", f"Dotaz selhal: {e}")
    return RedirectResponse(f"/konverzace/{cid}", status_code=303)


@app.post("/hodnoceni")
def feedback(message_id: int = Form(...), rating: int = Form(...),
             conversation_id: str = Form(...), kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    db.set_feedback(message_id, 1 if rating > 0 else -1)
    return RedirectResponse(f"/konverzace/{conversation_id}", status_code=303)


CAPTURE = """<h1>Zachytit</h1>{% if saved %}<p class="err" style="background:#0a05">
Uloženo do <code>{{ saved }}</code>.</p>{% endif %}
<form method="post" action="/zachytit">
<p><input name="title" placeholder="Titulek (nepovinný — bez něj se přípíše do deníku)"></p>
<p><textarea name="text" rows="10" autofocus placeholder="Text poznámky (markdown)"></textarea></p>
<button>Uložit</button></form>"""


@app.get("/zachytit", response_class=HTMLResponse)
def capture_form(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return page("Zachytit", render(CAPTURE, saved=None))


@app.post("/zachytit", response_class=HTMLResponse)
def capture(text: str = Form(""), title: str = Form(""),
            kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    if not text.strip():
        return page("Zachytit", render(CAPTURE, saved=None))
    rel = core.capture(text, title.strip() or None)
    return page("Zachytit", render(CAPTURE, saved=rel))


HIST = """<h1>Historie</h1><ul>{% for c in convs %}
<li><a href="/konverzace/{{ c.id }}">{{ c.title or "(bez názvu)" }}</a>
<span class="meta">{{ c.created_at.strftime("%d.%m.%Y %H:%M") }} · {{ c.messages }} zpráv</span></li>
{% endfor %}</ul>{% if not convs %}<p>Zatím nic.</p>{% endif %}"""


@app.get("/historie", response_class=HTMLResponse)
def history(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return page("Historie", render(HIST, convs=db.conversations(100)))


INBOX = """<h1>Inbox</h1><p class="meta">Zachycené poznámky, které jsi ještě nezpracoval.</p>
{% for i in items %}<div class="msg"><code>{{ i.source_path }}</code>
<span class="meta">{{ i.created_at.strftime("%d.%m. %H:%M") }}</span>
<pre style="margin-top:.4rem;opacity:.85">{{ i.excerpt }}</pre>
<form method="post" action="/inbox/hotovo" style="margin-top:.5rem">
<input type="hidden" name="item_id" value="{{ i.id }}"><button>Zpracováno</button></form></div>
{% endfor %}{% if not items %}<p>Prázdný.</p>{% endif %}"""


@app.get("/inbox", response_class=HTMLResponse)
def inbox(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return page("Inbox", render(INBOX, items=db.inbox_open()))


@app.post("/inbox/hotovo")
def inbox_done(item_id: int = Form(...), kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    db.inbox_done(item_id)
    return RedirectResponse("/inbox", status_code=303)


@app.get("/stats")
def stats(kryton_session: str = Cookie(None)):
    # Na rozdíl od /healthz tohle za autentizaci patří: PublishPort je
    # 0.0.0.0:3001 a firewall pouští celý segment 10.20.0.0/24, takže bez
    # kontroly by kdokoliv v homelabu viděl, kolik toho mám v poznámkách.
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return db.stats()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
