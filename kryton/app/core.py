"""Klienti, autentizace a zápis poznámek."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import unicodedata
from datetime import date
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("kryton")

# ---------------------------------------------------------------------------
# Autentizace: podepsaná session cookie. Bez externího identity provideru,
# pro jednoho uživatele je to dost. Heslo ani secret nikdy neopustí server.
# ---------------------------------------------------------------------------

def _sign(payload: str) -> str:
    mac = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256)
    return mac.hexdigest()


def make_session() -> str:
    exp = str(int(time.time()) + config.SESSION_HOURS * 3600)
    return f"{exp}.{_sign(exp)}"


def valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, sig = cookie.rsplit(".", 1)
    # compare_digest kvůli timing attacku — je to levné a jinak by to byl
    # zbytečně slabý bod.
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def check_password(given: str) -> bool:
    return hmac.compare_digest(given or "", config.AUTH_PASSWORD)


# ---------------------------------------------------------------------------
# Retrieval a LiteLLM
# ---------------------------------------------------------------------------

def search(query: str, limit: int = None, rewrite: bool | None = None) -> dict:
    body = {"query": query, "limit": limit or config.CONTEXT_CHUNKS}
    if rewrite is not None:
        body["rewrite"] = rewrite
    r = httpx.post(config.RETRIEVAL_URL.rstrip("/") + "/search", json=body, timeout=180)
    r.raise_for_status()
    return r.json()


def trigger_reindex() -> None:
    """Po zápisu poznámky. Selhání se jen zaloguje — poznámka je uložená,
    což je to podstatné; index se dorovná při dalším běhu."""
    if not config.REINDEX_AFTER_WRITE:
        return
    try:
        httpx.post(config.RETRIEVAL_URL.rstrip("/") + "/reindex", timeout=10)
    except httpx.HTTPError as e:
        log.warning("reindex se nepodarilo spustit: %s", e)


SYSTEM = (
    "Jsi asistent nad osobními poznámkami uživatele. Odpovídej česky a POUZE "
    "na základě dodaného kontextu.\n"
    "Za každým tvrzením uveď odkaz na zdroj ve tvaru [1], [2] podle čísel "
    "úryvků níže.\n"
    "Když kontext na otázku neodpovídá, řekni to přímo — nedomýšlej si. "
    "Je lepší přiznat, že v poznámkách odpověď není, než ji vymyslet."
)


def answer(query: str, hits: list[dict]) -> tuple[str, str, int]:
    """Vrátí (odpověď, model, latence_ms)."""
    ctx = "\n\n".join(
        f"[{i+1}] {h['source_path']}"
        + (f" — {h['heading_path']}" if h.get("heading_path") else "")
        + f"\n{h['content']}"
        for i, h in enumerate(hits))
    if not ctx:
        return ("V poznámkách jsem k tomu nic nenašel.", "", 0)

    t0 = time.time()
    r = httpx.post(
        config.LITELLM_URL.rstrip("/") + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
        json={"model": config.ANSWER_MODEL,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user",
                            "content": f"Úryvky z poznámek:\n\n{ctx}\n\nOtázka: {query}"}],
              "max_tokens": config.ANSWER_MAX_TOKENS},
        timeout=config.ANSWER_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(f"LiteLLM {r.status_code}: {r.text[:200]}")
    d = r.json()
    msg = d["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        # Reasoning model spotřeboval token budget na reasoning_content.
        # Změřeno u big-pickle: při nízkém max_tokens vrátí prázdný content
        # s finish_reason=length. Radši to řekni, než vrátit prázdno.
        fin = d["choices"][0].get("finish_reason")
        text = (f"(model nevrátil odpověď, finish_reason={fin} — "
                f"zvyš ANSWER_MAX_TOKENS)")
    return text, d.get("model", config.ANSWER_MODEL), ms


# ---------------------------------------------------------------------------
# Zápis poznámek
# ---------------------------------------------------------------------------

SAFE = re.compile(r"[^0-9A-Za-z\-_]+")


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = SAFE.sub("-", t).strip("-").lower()
    return (t or "poznamka")[:60]


def _root() -> Path:
    return Path(config.MARKDOWN_ROOT)


def safe_path(rel: str) -> Path:
    """Zabrání path traversalu i zápisu do .git.

    Píšeme do adresáře, který je AUTORITATIVNÍ zdroj dat, takže tady se
    nešetří: cokoliv mimo MARKDOWN_ROOT nebo do tečkového adresáře je chyba.
    """
    p = (_root() / rel).resolve()
    root = _root().resolve()
    if not str(p).startswith(str(root) + "/"):
        raise ValueError("cesta mimo MARKDOWN_ROOT")
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        raise ValueError("cesta do skryteho adresare")
    if p.suffix != ".md":
        raise ValueError("jen soubory .md")
    return p


def capture(text: str, title: str | None = None) -> str:
    """Nová poznámka, nebo připsání do denního zápisu když není titulek."""
    if title:
        rel = f"{date.today().isoformat()}-{slug(title)}.md"
        p = safe_path(rel)
        if p.exists():                      # nikdy nepřepisuj mlčky
            rel = f"{date.today().isoformat()}-{slug(title)}-{int(time.time())}.md"
            p = safe_path(rel)
        p.write_text(f"# {title}\n\n{text.strip()}\n", encoding="utf-8")
    else:
        rel = f"denik/{date.today().isoformat()}.md"
        p = safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M")
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8").rstrip()
                         + f"\n\n## {stamp}\n\n{text.strip()}\n", encoding="utf-8")
        else:
            p.write_text(f"# {date.today().isoformat()}\n\n## {stamp}\n\n{text.strip()}\n",
                         encoding="utf-8")
    db.add_inbox(rel, text)
    trigger_reindex()
    return rel
