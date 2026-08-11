"""Kryton — webové UI second brainu.

Server-rendered HTML, šablony inline. Žádný build step, žádný JS toolchain;
vejde se do MemoryMax=800M a je to jeden stack s retrievalem.
"""
from __future__ import annotations

import logging

from fastapi import Cookie, FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from markupsafe import Markup

from . import analytics, config, core, db, ingest, storage, telegram

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kryton")

# httpx na INFO loguje "HTTP Request: <metoda> <URL> ...", a Telegram Bot
# API nese token PŘÍMO V URL (https://api.telegram.org/bot<TOKEN>/metoda) -
# na rozdíl od LiteLLM, kde klíč jde v Authorization hlavičce, ne v URL.
# Bez tohohle by se token zapisoval do journalu při KAŽDÉM volání pollingu.
logging.getLogger("httpx").setLevel(logging.WARNING)

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
table{border-collapse:collapse;width:100%;margin:.6rem 0}
th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #8883}
th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}
tr.sum td,tr.sum th{border-top:2px solid #8886;border-bottom:none;font-weight:600}
</style></head><body>
<nav><a href="/"><b>Kryton</b></a><a href="/zachytit">Zachytit</a>
<a href="/nahrat">Nahrát</a><a href="/historie">Historie</a>
<a href="/inbox">Inbox</a><a href="/korpus">Korpus</a>
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
    analytics.init()
    telegram.start_background()
    log.info("start: retrieval=%s litellm=%s model=%s analytika=%s uloziste=%s telegram=%s",
             config.RETRIEVAL_URL, config.LITELLM_URL, config.ANSWER_MODEL,
             "zapnuta" if analytics.enabled() else "vypnuta",
             ("%s/%s" % (config.S3_PROFILE, config.S3_BUCKET))
             if storage.enabled() else "vypnuto",
             "zapnuty" if telegram.enabled() else "vypnuty")


@app.on_event("shutdown")
def _shutdown():
    analytics.close()
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


# Tabulka výsledku metriky. Sdílená mezi konverzací a /korpus, aby se
# nerozešly — očekává proměnnou `m` se `cols` a `rows`.
TAB_METRIKY = """<table><tr>{% for c in m.cols %}<th>{{ c }}</th>{% endfor %}</tr>
{% for r in m.rows %}<tr>{% for v in r %}<td>{{ v if v is not none else "" }}</td>{% endfor %}</tr>{% endfor %}
</table>{% if not m.rows %}<p class="meta">Dotaz nevrátil žádné řádky.</p>{% endif %}
<details><summary class="meta">ukázat SQL</summary><pre class="meta">{{ m.sql }}</pre></details>"""

# Mazání je nevratné (CASCADE bere zprávy i hodnocení), takže potvrzení.
# Inline JS je tu jediný na celé aplikaci — na potvrzovací mezistránku
# to nestojí a bez JS se prostě smaže rovnou, což je pořád vědomý klik.
SMAZAT = ("onsubmit=\"return confirm('Smazat konverzaci i s odpověďmi "
          "a hodnocením? Nejde to vrátit.')\"")

SMAZAT_NAHRANY = ("onsubmit=\"return confirm('Smazat dokument z hledání? "
                  "Originál zůstane na S3, nahrání jde zopakovat.')\"")

CONV = """<h1>{{ title or "Konverzace" }}</h1>
{% if agregacni %}<p class="msg" style="background:#fc04">
Tenhle dotaz vypadá na souhrn nebo počty nad celým korpusem. Přesná čísla
jsou na <a href="/korpus"><b>/korpus</b></a> — berou se přímo z databáze.
Odpověď níž vychází z nalezených úryvků a z faktů o korpusu, ne z projití
všech dokumentů.</p>{% endif %}
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
{% for m in metriky %}<div class="msg">
<b>Spočítáno nad databází</b>
<div class="meta">{{ m.question }}</div>
""" + TAB_METRIKY + """
<form method="post" action="/metrika/pripnout" class="row" style="margin-top:.6rem">
<input type="hidden" name="metric_id" value="{{ m.id }}">
<input type="hidden" name="zpet" value="/konverzace/{{ cid }}">
<input name="label" placeholder="Název na /korpus (nepovinný)" style="flex:1">
<button>Připnout na /korpus</button></form>
<form method="post" action="/metrika/smazat" style="margin-top:.4rem">
<input type="hidden" name="metric_id" value="{{ m.id }}">
<input type="hidden" name="zpet" value="/konverzace/{{ cid }}">
<button>Zahodit</button></form></div>
{% endfor %}
<form method="post" action="/dotaz"><input type="hidden" name="conversation_id" value="{{ cid }}">
<p><textarea name="query" rows="2" placeholder="Doplňující dotaz…"></textarea></p>
<div class="row"><button>Zeptat se</button>
<label><input type="checkbox" name="rewrite" value="1" checked style="width:auto"> přepis dotazu přes LLM</label></div>
</form>
<form method="post" action="/konverzace/smazat" style="margin-top:2rem" """ + SMAZAT + """>
<input type="hidden" name="conversation_id" value="{{ cid }}">
<button>Smazat konverzaci</button></form>
<script>scrollTo(0,document.body.scrollHeight)</script>"""


@app.get("/konverzace/{cid}", response_class=HTMLResponse)
def conversation(cid: str, kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    msgs = db.messages(cid)
    title = next((c["title"] for c in db.conversations(200) if c["id"] == cid), None)
    # Odkaz na /korpus se počítá z POSLEDNÍ otázky, ne z celé konverzace —
    # jinak by upozornění viselo do konce konverzace i po změně tématu.
    posledni = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    return page(title or "Konverzace",
                render(CONV, msgs=msgs, cid=cid, title=title,
                       agregacni=core.je_agregacni(posledni),
                       metriky=db.metrics_for_conversation(cid)))


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
        # Agregační dotaz se navíc spočítá nad databází. Výsledek jde modelu
        # jako další ověřený podklad — odpověď tak stojí na spočítaném čísle,
        # ne na odhadu z úryvků. Když analytika selže, jede se dál bez ní.
        extra = ""
        if core.je_agregacni(q) and analytics.enabled():
            res_an = analytics.answer_question(q)
            extra = analytics.as_context(res_an)
            if res_an.get("sql") and not res_an.get("error"):
                db.add_metric(cid, q, res_an["sql"], res_an["cols"],
                              res_an["rows"], res_an["fingerprint"])

        res = core.search(q, rewrite=bool(rewrite))
        hits = res["results"]
        text, model, ms = core.answer(q, hits, prior=prior, extra=extra)
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


@app.post("/konverzace/smazat")
def conversation_delete(conversation_id: str = Form(...),
                        kryton_session: str = Cookie(None)):
    # POST, ne GET: mazání odkazem by šlo spustit prefetchem prohlížeče
    # nebo náhodným průchodem historie. Kolize s GET /konverzace/{cid}
    # není — liší se metodou.
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    try:
        n = db.delete_conversation(conversation_id)
    except Exception as e:
        # Nejčastěji neplatné UUID z ručně upraveného formuláře.
        log.warning("mazani konverzace %r selhalo: %s", conversation_id, e)
        n = 0
    log.info("smazana konverzace %s (%s radku)", conversation_id, n)
    return RedirectResponse("/historie", status_code=303)


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


NAHRAT = """<h1>Nahrát dokument</h1>
<p class="meta">Text se zaindexuje jako <b>importovaný</b> dokument
(nižší důvěra než vlastní poznámky), originál se uloží na S3.
Podporované formáty: markdown, prostý text, PDF a Word (.docx). Strop
{{ max_mb }} MB, u PDF navíc {{ max_pages }} stránek.</p>
{% if not s3 %}<p class="err">Úložiště S3 není nakonfigurované — nahrávání
je vypnuté.</p>{% endif %}
{% if err %}<p class="err">{{ err }}</p>{% endif %}
{% if ok %}<p class="msg" style="background:#0a05">Nahráno jako
<code>{{ ok.source_path }}</code> — {{ ok.size }} B, kódování
<b>{{ ok.encoding }}</b>, originál na S3 jako <code>{{ ok.s3_key }}</code>.</p>{% endif %}
<form method="post" action="/nahrat" enctype="multipart/form-data">
<p><input type="file" name="soubor" accept=".md,.markdown,.txt,.text,.pdf,.docx" required></p>
<button>Nahrát</button></form>
{% if items %}<h2>Nahrané dokumenty</h2>
<table><tr><th>soubor</th><th class="n">velikost</th><th>kódování</th><th>uloženo</th><th></th></tr>
{% for u in items %}<tr>
<td>{{ u.original_name }}<div class="meta"><code>{{ u.source_path }}</code></div></td>
<td class="n">{{ (u.size_bytes / 1024) | round(1) }} kB</td>
<td>{{ u.encoding }}</td>
<td class="meta">{{ u.created_at.strftime("%d.%m. %H:%M") }} · {{ u.s3_profile }}</td>
<td><form method="post" action="/nahrat/smazat" """ + SMAZAT_NAHRANY + """>
<input type="hidden" name="upload_id" value="{{ u.id }}"><button>Smazat</button></form></td>
</tr>{% endfor %}</table>{% endif %}"""


def _stranka_nahrat(err=None, ok=None):
    return page("Nahrát", render(
        NAHRAT, err=err, ok=ok, s3=storage.enabled(),
        max_mb=int(config.UPLOAD_MAX_BYTES / 1e6),
        max_pages=config.UPLOAD_MAX_PAGES, items=db.uploads(50)))


@app.get("/nahrat", response_class=HTMLResponse)
def upload_form(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    return _stranka_nahrat()


@app.post("/nahrat", response_class=HTMLResponse)
async def upload(soubor: UploadFile = File(...),
                 kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    try:
        data = await soubor.read()
        vysledek = ingest.uloz(data, soubor.filename or "")
    except ingest.IngestError as e:
        # Očekávaná chyba (formát, velikost, kódování) — patří uživateli.
        return _stranka_nahrat(err="Nahrání se nepovedlo: %s" % e)
    except Exception as e:
        log.exception("nahrani selhalo")
        return _stranka_nahrat(err="Nahrání selhalo: %s" % e)
    return _stranka_nahrat(ok=vysledek)


@app.post("/nahrat/smazat")
def upload_delete(upload_id: int = Form(...), kryton_session: str = Cookie(None)):
    """Smaže dokument z hledání (soubor + řádek `upload`), NIKDY ze S3.

    Pořadí je vědomé: nejdřív smazat řádek (atomicky vrátí source_path),
    pak soubor, pak reindex. Kdyby cokoliv z posledních dvou kroků selhalo,
    dokument aspoň zmizí z `/nahrat` a příště se dá smazat znovu (soubor
    zmizelý ze stromu si reindex stejně domyslí); horší by bylo smazat
    soubor a nechat po něm osiřelý řádek.
    """
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    source_path = db.delete_upload(upload_id)
    if source_path:
        try:
            core.safe_path(source_path).unlink(missing_ok=True)
        except ValueError as e:
            # source_path pochazi z DB, ne primo od uzivatele, takze by
            # tohle nemelo nastat - ale kdyby, aspon se vi proc soubor zustal.
            log.warning("smazani souboru pro upload %s selhalo: %s", upload_id, e)
        core.trigger_reindex()
    log.info("smazan upload %s (%s)", upload_id, source_path or "nenalezen")
    return RedirectResponse("/nahrat", status_code=303)


HIST = """<h1>Historie</h1>
{% for c in convs %}<div class="row" style="border-bottom:1px solid #8883;padding:.45rem 0">
<div style="flex:1"><a href="/konverzace/{{ c.id }}">{{ c.title or "(bez názvu)" }}</a>
<div class="meta">{{ c.created_at.strftime("%d.%m.%Y %H:%M") }} · {{ c.messages }} zpráv</div></div>
<form method="post" action="/konverzace/smazat" """ + SMAZAT + """>
<input type="hidden" name="conversation_id" value="{{ c.id }}">
<button>Smazat</button></form></div>
{% endfor %}{% if not convs %}<p>Zatím nic.</p>{% endif %}"""


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


CORPUS = """<h1>Korpus</h1>
<p class="meta">Přesná čísla z databáze. Na tohle se schválně neptá model —
agregaci nad tisícem dokumentů z osmi úryvků složit nejde a odpověď by byla
sebejistý odhad.</p>
{% if err %}<p class="err">{{ err }}</p>{% else %}
<table>
<tr><th>jazyk</th><th class="n">dokumentů</th><th class="n">chunků</th></tr>
{% for r in rows %}<tr><td>{{ r.name }} <span class="meta">{{ r.code }}</span></td>
<td class="n">{{ r.docs }}</td><td class="n">{{ r.chunks }}</td></tr>
{% endfor %}
<tr class="sum"><th>celkem</th><td class="n">{{ total_docs }}</td><td class="n">{{ total_chunks }}</td></tr>
</table>
<h2>Stav indexu</h2>
<table>
<tr><td>HNSW index postavený</td><td class="n">{{ "ano" if hnsw else "NE" }}</td></tr>
<tr><td>chunky bez embeddingu</td><td class="n">{{ no_emb }}</td></tr>
<tr><td>nedokončené dokumenty</td><td class="n">{{ unfinished }}</td></tr>
<tr><td>indexace právě běží</td><td class="n">{{ "ano" if running else "ne" }}</td></tr>
</table>
{% if last %}<h2>Poslední indexace</h2><p class="meta">{{ last }}</p>{% endif %}
{% endif %}
{% if metriky %}<h2>Připnuté metriky</h2>
<p class="meta">Spočítané nad databází a ručně potvrzené. Když se korpus
od výpočtu změnil, je u metriky upozornění — číslo pak neber jako platné,
dokud ho nepřepočítáš.</p>
{% for m in metriky %}<div class="msg">
<b>{{ m.label or m.question }}</b>
{% if m.stale %} <span class="err" style="padding:.1rem .4rem">zastaralé</span>{% endif %}
""" + TAB_METRIKY + """
<div class="meta">spočítáno {{ m.computed_at.strftime("%d.%m.%Y %H:%M") }}</div>
<form method="post" action="/metrika/smazat" style="margin-top:.4rem">
<input type="hidden" name="metric_id" value="{{ m.id }}">
<input type="hidden" name="zpet" value="/korpus">
<button>Odepnout</button></form></div>
{% endfor %}{% endif %}"""


@app.get("/korpus", response_class=HTMLResponse)
def corpus(kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    try:
        s = core.corpus_stats()
    except Exception as e:
        # Retrieval může být dole nebo uprostřed reindexu. Stránka s chybovou
        # hláškou je lepší než 500 — zbytek Krytona na tomhle nezávisí.
        log.warning("/stats retrievalu nedostupne: %s", e)
        return page("Korpus", render(CORPUS, err="Retrieval neodpovídá: %s" % e,
                                     metriky=_pinned()))

    by_lang = s.get("documents_by_lang") or {}
    by_ts = s.get("chunks_by_ts_config") or {}
    rows = [{"code": c, "name": core.LANG_NAMES.get(c, c), "docs": by_lang[c],
             "chunks": by_ts.get(core.LANG_TS.get(c, ""), 0)}
            for c in sorted(by_lang, key=lambda c: -by_lang[c])]

    ix = s.get("indexer") or {}
    lr = ix.get("last_result") or {}
    last = ""
    if lr:
        last = ("nových %s, změněných %s, beze změny %s, smazaných %s; "
                "embeddingů %s, recyklovaných chunků %s; %s s"
                % (lr.get("new", 0), lr.get("changed", 0), lr.get("unchanged", 0),
                   lr.get("deleted", 0), lr.get("chunks_embedded", 0),
                   lr.get("chunks_recycled", 0), lr.get("seconds", 0)))

    return page("Korpus", render(
        CORPUS, err=None, rows=rows,
        total_docs=s.get("documents", 0), total_chunks=s.get("chunks", 0),
        hnsw=s.get("hnsw_index_present"), no_emb=s.get("chunks_without_embedding", 0),
        unfinished=s.get("documents_unfinished", 0), running=ix.get("running"),
        last=last, metriky=_pinned()))


def _pinned() -> list[dict]:
    """Připnuté metriky s příznakem, jestli je korpus mezitím jinde.

    Otisk se počítá jednou na stránku. Když se ho nepodaří zjistit (vypnutá
    analytika, nedostupná DB), radši nic neoznačím než abych označil všechno.
    """
    ms = db.pinned_metrics()
    now = ""
    if ms and analytics.enabled():
        try:
            now = analytics.fingerprint()
        except Exception as e:
            log.warning("otisk korpusu se nepodarilo zjistit: %s", e)
    for m in ms:
        m["stale"] = bool(now and m.get("fingerprint") and m["fingerprint"] != now)
    return ms


def _bezpecne_zpet(zpet: str) -> str:
    """Jen lokální cesta. Bez toho by šlo formulářem odeslat cizí URL."""
    return zpet if zpet.startswith("/") and not zpet.startswith("//") else "/korpus"


@app.post("/metrika/pripnout")
def metric_pin(metric_id: int = Form(...), label: str = Form(""),
               zpet: str = Form("/korpus"), kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    db.pin_metric(metric_id, label.strip())
    log.info("metrika %s pripnuta na /korpus", metric_id)
    return RedirectResponse(_bezpecne_zpet(zpet), status_code=303)


@app.post("/metrika/smazat")
def metric_delete(metric_id: int = Form(...), zpet: str = Form("/korpus"),
                  kryton_session: str = Cookie(None)):
    if not logged_in(kryton_session):
        return RedirectResponse("/prihlasit", status_code=303)
    db.delete_metric(metric_id)
    return RedirectResponse(_bezpecne_zpet(zpet), status_code=303)


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
